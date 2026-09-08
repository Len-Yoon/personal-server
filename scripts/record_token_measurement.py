#!/usr/bin/env python3
"""Validate one token measurement from stdin and append it to a local JSONL file."""

import argparse
import json
import re
import sys
from pathlib import Path

from summarize_token_measurements import (
    MEASUREMENT_CONDITION_FIELDS,
    TOKEN_FIELDS,
    _parse_record,
)


RECORD_FIELDS = (*MEASUREMENT_CONDITION_FIELDS, "recorded_at", *TOKEN_FIELDS)
SHA256_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SENSITIVE_IDENTIFIER_PATTERN = re.compile(
    r"authorization|bearer|token|password|secret", re.IGNORECASE
)


def parse_input(contents: str) -> dict[str, str | int]:
    lines = [line for line in contents.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("input must contain one JSON object")
    try:
        record = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise ValueError("input must contain valid JSON") from error
    if not isinstance(record, dict):
        raise ValueError("input must contain one JSON object")

    # Reuse the summarizer's field and UTC validation rules, then whitelist the
    # persisted fields so arbitrary input (including accidental secrets) is not
    # copied to the measurement log.
    _parse_record(record, 1)
    for field in MEASUREMENT_CONDITION_FIELDS:
        value = record[field]
        if (
            not IDENTIFIER_PATTERN.fullmatch(value)
            or SENSITIVE_IDENTIFIER_PATTERN.search(value)
        ):
            raise ValueError(f"line 1: {field} contains an unsafe identifier")
    fingerprint = record["prompt_fingerprint"]
    if not SHA256_FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("line 1: prompt_fingerprint must be a SHA-256 digest")
    return {field: record[field] for field in RECORD_FIELDS}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        record = parse_input(sys.stdin.read())
        with args.output.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"input_error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
