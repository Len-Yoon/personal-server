#!/usr/bin/env python3
"""Run every service test suite with the import path used by CI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TestSuite:
    name: str
    command: tuple[str, ...]
    pythonpath: str | None = None
    client_command: tuple[str, ...] | None = None


SUITES = (
    TestSuite(
        "portal",
        (
            "-m",
            "unittest",
            "tests.test_file_access",
            "tests.test_portal_dashboard",
            "tests.test_portal_security",
            "tests.test_homeops",
            "tests.test_homeops_notifier",
            "tests.test_portfolio",
        ),
        "portal-web",
    ),
    TestSuite("system-agent", ("-m", "unittest", "tests.system_agent.test_metrics"), "system-agent"),
    TestSuite(
        "crawler-worker",
        (
            "-m",
            "unittest",
            "tests.crawler_worker.test_datetime_format",
            "tests.crawler_worker.test_investing_news_rss",
            "tests.crawler_worker.test_nasdaq_relevance",
            "tests.crawler_worker.test_news_routes",
            "tests.crawler_worker.test_news_service",
            "tests.crawler_worker.test_rss_news",
            "tests.crawler_worker.test_telegram_notifier",
        ),
        "crawler-worker",
        ("--test", "tests/news_auto_refresh_client.test.mjs"),
    ),
    TestSuite("homeops-executor", ("-m", "unittest", "tests.homeops_executor.test_docker_ops"), "homeops-executor"),
    TestSuite(
        "youtube-memo",
        ("-m", "unittest", "tests.youtube_memo.test_ui_contract", "tests.youtube_memo.test_video_titles"),
        "youtube-memo",
    ),
    TestSuite(
        "book-memo",
        ("-m", "unittest", "tests.book_memo.test_ui_contract", "tests.book_memo.test_book_service"),
        "book-memo",
    ),
    TestSuite(
        "car-care-worker",
        (
            "-m",
            "unittest",
            "tests.car_care_worker.test_store",
            "tests.car_care_worker.test_maintenance",
            "tests.car_care_worker.test_vehicle_monitor",
            "tests.car_care_worker.test_telegram",
            "tests.car_care_worker.test_hyundai",
            "tests.car_care_worker.test_oauth_callback",
            "tests.car_care_worker.test_main",
        ),
        "car-care-worker",
    ),
    TestSuite(
        "maintenance",
        (
            "-m",
            "unittest",
            "tests.test_compose_config",
            "tests.test_documentation_index",
            "tests.test_deploy_n100",
            "tests.test_maintenance",
            "tests.test_run_service_tests",
            "tests.test_verify_change_scope",
            "tests.test_windows_bootstrap",
        ),
    ),
    TestSuite("web-client", ("--test", "tests/file_explorer_client.test.mjs", "tests/news_auto_refresh_client.test.mjs")),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=[suite.name for suite in SUITES], action="append")
    parser.add_argument("--list", action="store_true", help="list available suites and exit")
    return parser.parse_args()


def _run_suite(suite: TestSuite) -> int:
    environment = os.environ.copy()
    if suite.pythonpath is not None:
        service_path = str(ROOT / suite.pythonpath)
        previous_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (service_path, previous_pythonpath)))

    executable = "node" if suite.name == "web-client" else sys.executable
    commands = [(executable, *suite.command)]
    if suite.client_command is not None:
        commands.append(("node", *suite.client_command))

    for command in commands:
        result = subprocess.run(command, cwd=ROOT, env=environment)
        if result.returncode != 0:
            print(f"[FAIL] {suite.name} (exit {result.returncode})")
            return result.returncode

    print(f"[PASS] {suite.name}")
    return 0


def main() -> int:
    args = _parse_args()
    if args.list:
        print("\n".join(suite.name for suite in SUITES))
        return 0

    selected_names = set(args.suite or ())
    selected_suites = [suite for suite in SUITES if not selected_names or suite.name in selected_names]
    failures = sum(_run_suite(suite) != 0 for suite in selected_suites)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
