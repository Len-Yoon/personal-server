#!/usr/bin/env python3
"""Validate JSONL token measurements and summarize observed savings."""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


TOKEN_FIELDS = (
    "baseline_input_tokens",
    "baseline_output_tokens",
    "harness_input_tokens",
    "harness_output_tokens",
)
MEASUREMENT_CONDITION_FIELDS = (
    "task_id",
    "model",
    "measurement_group",
    "prompt_fingerprint",
)


def _parse_record(record: object, line_number: int) -> dict[str, str | int]:
    if not isinstance(record, dict):
        raise ValueError(f"line {line_number}: record must be a JSON object")

    identifiers: dict[str, str] = {}
    for field in MEASUREMENT_CONDITION_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"line {line_number}: {field} must be a non-empty string")
        identifiers[field] = value

    recorded_at = record.get("recorded_at")
    if not isinstance(recorded_at, str):
        raise ValueError(f"line {line_number}: recorded_at must be an ISO-8601 string")
    try:
        timestamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"line {line_number}: recorded_at must be ISO-8601") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"line {line_number}: recorded_at must be UTC with an offset")

    values: dict[str, str | int] = identifiers
    for field in TOKEN_FIELDS:
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"line {line_number}: {field} must be a non-negative integer")
        values[field] = value
    return values


def summarize_measurements(contents: str) -> dict[str, int | float]:
    records = []
    for line_number, line in enumerate(contents.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"line {line_number}: invalid JSON") from error
        records.append(_parse_record(parsed, line_number))

    if not records:
        raise ValueError("input must contain at least one measurement record")

    conditions = {
        field: {record[field] for record in records}
        for field in MEASUREMENT_CONDITION_FIELDS
    }
    mixed_fields = [field for field, values in conditions.items() if len(values) != 1]
    if mixed_fields:
        raise ValueError(
            "all measurement records must share: " + ", ".join(mixed_fields)
        )

    baseline_total = sum(
        int(record["baseline_input_tokens"]) + int(record["baseline_output_tokens"])
        for record in records
    )
    harness_total = sum(
        int(record["harness_input_tokens"]) + int(record["harness_output_tokens"])
        for record in records
    )
    saved = baseline_total - harness_total
    reduction = round((saved / baseline_total) * 100, 1) if baseline_total else 0.0
    return {
        "measurement_count": len(records),
        "baseline_total_tokens": baseline_total,
        "harness_total_tokens": harness_total,
        "saved_tokens": saved,
        "reduction_percent": reduction,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize_measurements(args.input.read_text(encoding="utf-8")), separators=(",", ":")))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"input_error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
