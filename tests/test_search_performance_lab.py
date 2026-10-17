"""Exercise the local search lab's actual sockets, result accounting and cleanup."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "portal-web/tools/search_performance_lab.py"


def load_tool():
    # Importable name lets multiprocessing's spawn import the same real module.
    import importlib
    if str(TOOL.parent) not in sys.path:
        sys.path.insert(0, str(TOOL.parent))
    return importlib.import_module("search_performance_lab")


def crashing_server(*_args):
    os._exit(7)


def stalled_load(connection, *_args):
    connection.send({"type": "started", "phase": "measurement", "at": time.monotonic()})
    time.sleep(30)


def buffered_load(connection, *_args):
    at = time.monotonic()
    for _ in range(30):
        connection.send({"type": "started", "phase": "measurement", "at": at})
        connection.send({"type": "sample", "phase": "measurement", "at": at,
                         "row": {"latency_seconds": 0, "statuses": ["ok"] * 3, "valid": True}})
    time.sleep(30)


class SearchPerformanceLabTests(unittest.TestCase):
    def test_cli_exists(self):
        self.assertTrue(TOOL.is_file(), "local performance runner is missing")

    def test_nearest_rank_and_empty_samples(self):
        lab = load_tool()
        self.assertEqual(lab.percentile([4, 1, 3, 2], .50), 2)
        self.assertEqual(lab.percentile([4, 1, 3, 2], .99), 4)
        with self.assertRaises(ValueError):
            lab.percentile([], .95)

    def test_partial_samples_do_not_use_planned_count_as_denominator(self):
        lab = load_tool()
        rows = [{"latency_seconds": .2, "statuses": ["ok"] * 3, "valid": True},
                {"latency_seconds": .3, "statuses": ["ok", "ok", "unavailable"], "valid": True}]
        result = lab.summarize(rows, 1.0, planned=30, started=3)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["not_started"], 27)
        self.assertEqual(result["throughput_per_second"], 2)
        self.assertEqual(result["all_ok_ratio"], .5)
        self.assertEqual(result["partial_failure_ratio"], .5)

    def test_malformed_ok_payload_is_not_counted_as_success(self):
        result = load_tool().summarize(
            [{"latency_seconds": .2, "statuses": ["ok"] * 3, "valid": False}],
            1.0, planned=1, started=1)
        self.assertEqual(result["all_ok_ratio"], 0)
        self.assertEqual(result["contract_violations"], 1)

    def test_startup_failure_reaps_owned_child(self):
        import multiprocessing
        from unittest.mock import patch
        lab = load_tool()
        before = {p.pid for p in multiprocessing.active_children()}
        with patch.object(lab, "server_process", crashing_server):
            result = lab.run_cell("normal", 1, 1, time.monotonic() + 5)
        self.assertFalse(result["valid"])
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_partial_measurement_elapsed_excludes_cleanup(self):
        from unittest.mock import patch
        lab = load_tool()
        real_cleanup = lab.stop_processes
        cleanup_duration = []

        def slow_cleanup(*args):
            start = time.monotonic()
            result = real_cleanup(*args)
            cleanup_duration.append(time.monotonic() - start)
            return result

        # A real stalled child requires the five-second shutdown escalation.
        with patch.object(lab, "load_process", stalled_load), patch.object(lab, "stop_processes", slow_cleanup):
            result = lab.run_cell("normal", 1, 30, time.monotonic() + 1)
        self.assertEqual(result["summary"]["started"], 1)
        self.assertEqual(result["summary"]["cancelled"], 1)
        self.assertGreater(cleanup_duration[0], 4)
        self.assertLess(result["summary"]["elapsed_seconds"], 1.5)

    def test_cutoff_drains_already_emitted_samples(self):
        from multiprocessing.connection import Connection
        from unittest.mock import patch
        lab = load_tool()
        original = Connection.recv
        stopped = False

        def receive(connection):
            nonlocal stopped
            event = original(connection)
            if isinstance(event, dict) and event.get("type") == "started":
                stopped = True
                # Let the tiny fixed event batch finish writing before cutoff.
                time.sleep(.05)
            return event

        with patch.object(lab, "load_process", buffered_load), patch.object(Connection, "recv", receive):
            result = lab.run_cell("normal", 1, 30, time.monotonic() + 5, lambda: stopped)
        self.assertEqual(result["reason"], "interrupted")
        self.assertEqual(result["summary"]["completed"], 30)
        self.assertEqual(result["summary"]["not_started"], 0)
        self.assertTrue(result["cleanup_ok"])

    def test_environment_has_no_parent_urls_demo_or_proxy(self):
        lab = load_tool()
        from unittest.mock import patch
        with patch.dict(os.environ, {"NEWS_SEARCH_URL": "https://invalid.example", "DEMO_MODE": "1",
                                     "hTtP_pRoXy": "https://invalid.example", "PRIVATE_TEST_TOKEN": "private"}):
            env = lab.clean_environment()
        self.assertEqual(env["DEMO_MODE"], "0")
        self.assertNotIn("NEWS_SEARCH_URL", env)
        self.assertNotIn("PRIVATE_TEST_TOKEN", env)
        self.assertFalse(any(key.lower().endswith("_proxy") and key.lower() != "no_proxy" for key in env))

    def run_cli(self, *args, env=None, timeout=25):
        return subprocess.run([sys.executable, str(TOOL), *args], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=timeout)

    def test_limits_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            for option, value in (("--concurrency", "11"), ("--count", "31"),
                                  ("--repeats", "4"), ("--deadline", "601"),
                                  ("--deadline", "nan"), ("--count", "0")):
                target = Path(directory) / "invalid.json"
                result = self.run_cli("--output", str(target), option, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(target.exists())

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.json"
            target.write_text("original")
            result = self.run_cli("--smoke", "--output", str(target))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), "original")

    def test_real_smoke_ignores_poisoned_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "smoke.json"
            env = dict(os.environ, NEWS_SEARCH_URL="https://invalid.example",
                       YOUTUBE_SEARCH_URL="https://invalid.example", BOOKS_SEARCH_URL="https://invalid.example",
                       DEMO_MODE="1", HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1")
            result = self.run_cli("--smoke", "--output", str(target), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(target.read_text())
            self.assertTrue(report["valid"])
            cell = report["cells"][0]
            self.assertEqual(cell["summary"]["completed"], 5)
            self.assertEqual(cell["summary"]["all_ok_ratio"], 1)
            self.assertTrue(cell["cleanup_ok"])
            self.assertIn("load", cell["resources"])
            self.assertIn("server", cell["resources"])
            self.assertNotIn("invalid.example", target.read_text())
            self.assertNotIn(str(Path.home()), target.read_text())

    def test_real_fault_preserves_healthy_sources(self):
        for scenario in ("http_failure", "timeout", "delayed"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "fault.json"
                result = self.run_cli("--scenario", scenario, "--concurrency", "1", "--count", "1",
                                      "--repeats", "1", "--output", str(target))
                self.assertEqual(result.returncode, 0, result.stderr)
                cell = json.loads(target.read_text())["cells"][0]
                row = cell["samples"][0]
                self.assertEqual(row["statuses"][:2], ["ok", "ok"])
                self.assertEqual(row["statuses"][2], "ok" if scenario == "delayed" else "unavailable")
                self.assertTrue(row["valid"])
                self.assertEqual(cell["warmup"]["completed"], 5)
                self.assertTrue(cell["cleanup_ok"])

    def test_deadline_preserves_invalid_partial_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "partial.json"
            result = self.run_cli("--scenario", "timeout", "--count", "30", "--deadline", "1",
                                  "--output", str(target), timeout=15)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(target.read_text())
            self.assertFalse(report["valid"])
            self.assertEqual(report["reason"], "deadline")
            self.assertTrue(report["cells"][0]["cleanup_ok"])
            self.assertGreater(report["remaining_cells"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX signals required")
    def test_sigterm_records_partial_evidence_without_killing_other_process(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "interrupt.json"
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
            child = subprocess.Popen([sys.executable, str(TOOL), "--scenario", "timeout",
                                      "--output", str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                # The reserved file is initialized only after signal handlers are installed.
                for _ in range(100):
                    if target.exists() and target.stat().st_size:
                        break
                    time.sleep(.02)
                child.send_signal(signal.SIGTERM)
                child.communicate(timeout=15)
                report = json.loads(target.read_text())
                self.assertFalse(report["valid"])
                self.assertEqual(report["reason"], "interrupted")
                self.assertIsNone(unrelated.poll())
                self.assertTrue(all(cell["cleanup_ok"] for cell in report["cells"]))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
                unrelated.terminate()
                unrelated.wait(timeout=5)
