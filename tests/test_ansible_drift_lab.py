"""Exercise response-only drift and safe cleanup without a Docker daemon."""
import importlib.util
import os
import signal
import subprocess
import tempfile
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "infra/ansible-lab/tools/drift-lab.py"


class DriftLabTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(TOOL.is_file(), "response drift tool is missing")
        spec = importlib.util.spec_from_file_location("drift_lab", TOOL)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".local/share/personal-server-ansible-lab"
        self.root.mkdir(parents=True)
        (self.root / ".owner").write_text("personal-server-ansible-lab\n")
        self.original = b"ansible lab sample ready\n"
        self.source_lab = self.home / "source-lab"
        (self.source_lab / "templates").mkdir(parents=True)
        self.template = self.source_lab / "templates/index.html.j2"
        self.template.write_bytes(self.original)
        self.mod.LAB = self.source_lab
        (self.root / "index.html").write_bytes(self.original)
        (self.root / "compose.yaml").write_text("owned compose fixture\n")
        self.calls = []

    def runner(self, args):
        self.calls.append(args)
        body = (self.root / "index.html").read_bytes()
        if "--tags" in args:
            output = "localhost : ok=20 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"
        elif "--check" in args:
            changed = int(body != self.original)
            output = ""
            if changed:
                output = "TASK [Render sample response]\n--- before\n+++ after\n-ansible lab sample drift\n+ansible lab sample ready\nchanged: [localhost]\n"
            output += f"localhost : ok=25 changed={changed} unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"
        else:
            (self.root / "index.html").write_bytes(self.original)
            output = f"localhost : ok=26 changed={int(body != self.original)} unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    def test_check_is_readonly_and_requires_existing_owned_baseline(self):
        result = self.mod.run_drill(self.home, self.runner)
        self.assertEqual(result, {"ownership": "PASS", "baseline": "PASS", "execution": "NOT_RUN"})
        self.assertEqual((self.root / "index.html").read_bytes(), self.original)
        self.assertTrue(all("--check" in args for args in self.calls))

    def test_go_detects_response_diff_restores_and_proves_changed_zero(self):
        result = self.mod.run_drill(self.home, self.runner, go=True)
        self.assertEqual(result["drift_detection"], "PASS")
        self.assertEqual(result["restoration"], "PASS")
        self.assertEqual(result["idempotence"], "PASS")
        self.assertEqual((self.root / "index.html").read_bytes(), self.original)

    def test_failure_and_interrupt_restore_known_response(self):
        for failure in (RuntimeError("private path"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.calls.clear()
                def interrupted(args):
                    if "--diff" in args and (self.root / "index.html").read_bytes() != self.original:
                        raise failure
                    return self.runner(args)
                result = self.mod.run_drill(self.home, interrupted, go=True)
                self.assertEqual(result["execution"], "FAIL")
                self.assertEqual(result["recovery"], "PASS")
                self.assertEqual((self.root / "index.html").read_bytes(), self.original)
                self.assertNotIn("private path", str(result))

    def test_unowned_symlink_missing_extra_and_hardlink_refused_before_runner(self):
        index = self.root / "index.html"
        for scenario in ("owner", "symlink", "missing", "extra", "hardlink"):
            with self.subTest(scenario=scenario):
                self.calls.clear()
                if scenario == "owner":
                    (self.root / ".owner").write_text("foreign\n")
                elif scenario == "symlink":
                    index.unlink()
                    index.symlink_to(self.root / ".owner")
                elif scenario == "missing":
                    index.unlink()
                elif scenario == "extra":
                    (self.root / "foreign").write_text("preserve")
                else:
                    (self.home / "outside").hardlink_to(index)
                result = self.mod.run_drill(self.home, self.runner, go=True)
                self.assertEqual(result["execution"], "FAIL")
                self.assertEqual(self.calls, [])
                if scenario == "extra":
                    (self.root / "foreign").unlink()
                if scenario == "hardlink":
                    (self.home / "outside").unlink()
                if index.is_symlink():
                    index.unlink()
                index.write_bytes(self.original)
                (self.root / ".owner").write_text("personal-server-ansible-lab\n")

    def test_preflight_resource_collision_blocks_injection(self):
        def collision(args):
            return subprocess.CompletedProcess(args, 2, "secret collision details", "")
        result = self.mod.run_drill(self.home, collision, go=True)
        self.assertEqual(result["execution"], "FAIL")
        self.assertEqual((self.root / "index.html").read_bytes(), self.original)
        self.assertNotIn("secret", str(result))

    def test_cleanup_refuses_replaced_file_and_reports_failure(self):
        def replaced(args):
            if "--check" in args and (self.root / "index.html").read_bytes() != self.original:
                (self.root / "index.html").write_bytes(b"foreign replacement\n")
                raise RuntimeError()
            return self.runner(args)
        result = self.mod.run_drill(self.home, replaced, go=True)
        self.assertEqual(result["recovery"], "FAIL")
        self.assertEqual((self.root / "index.html").read_bytes(), b"foreign replacement\n")

    def test_directory_replacement_cannot_redirect_site_or_recovery(self):
        def replaced(args):
            result = self.runner(args)
            if "--diff" in args and (self.root / "index.html").read_bytes() != self.original:
                self.root.rename(self.root.with_name("moved-owned-lab"))
                self.root.mkdir()
                (self.root / "foreign").write_text("preserve")
            return result
        result = self.mod.run_drill(self.home, replaced, go=True)
        self.assertEqual(result["execution"], "FAIL")
        self.assertEqual(result["recovery"], "FAIL")
        self.assertTrue(all("--check" in args for args in self.calls))
        self.assertEqual((self.root / "foreign").read_text(), "preserve")

    def test_partial_response_write_failure_restores_original(self):
        real_write = self.mod.os.write
        failed = False
        def broken_write(fd, content):
            nonlocal failed
            if content == b"ansible lab sample drift\n" and not failed:
                failed = True
                real_write(fd, content[:21])
                raise OSError("private write error")
            return real_write(fd, content)
        with patch.object(self.mod.os, "write", broken_write):
            result = self.mod.run_drill(self.home, self.runner, go=True)
        self.assertEqual(result["execution"], "FAIL")
        self.assertEqual(result["recovery"], "PASS")
        self.assertEqual((self.root / "index.html").read_bytes(), self.original)

    def test_timeout_kills_descendant_before_it_can_write_after_recovery(self):
        self.assertTrue(hasattr(self.mod, "_run_process"), "bounded process-group runner is missing")
        marker = self.home / "late-write"
        child = "import time; from pathlib import Path; time.sleep(0.4); Path(%r).write_text('late')" % str(marker)
        parent = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', %r]); time.sleep(10)" % child
        with self.assertRaises(subprocess.TimeoutExpired):
            self.mod._run_process([sys.executable, "-c", parent], self.home, {}, timeout=0.1)
        time.sleep(0.5)
        self.assertFalse(marker.exists(), "an Ansible descendant wrote after timeout cleanup")

    def test_interrupt_kills_descendant_before_returning_to_recovery(self):
        marker = self.home / "interrupted-late-write"
        child = "import time; from pathlib import Path; time.sleep(0.4); Path(%r).write_text('late')" % str(marker)
        parent = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', %r]); time.sleep(10)" % child
        communicate = subprocess.Popen.communicate
        interrupted = False
        def cancel(process, *args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                time.sleep(0.1)
                raise KeyboardInterrupt()
            return communicate(process, *args, **kwargs)
        with patch.object(subprocess.Popen, "communicate", cancel):
            with self.assertRaises(KeyboardInterrupt):
                self.mod._run_process([sys.executable, "-c", parent], self.home, {})
        time.sleep(0.5)
        self.assertFalse(marker.exists())

    def test_unconfirmed_process_stop_blocks_recovery_writes_and_site(self):
        processes = []
        real_popen, real_killpg = subprocess.Popen, os.killpg
        def capture_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        def runner(args):
            if "--diff" in args and (self.root / "index.html").read_bytes() != self.original:
                self.calls.append(args)
                child = "import time; from pathlib import Path; time.sleep(0.4); Path(%r).write_bytes(b'late overwrite')" % str(self.root / "index.html")
                return self.mod._run_process([sys.executable, "-c", child], self.home, {}, timeout=0.05)
            return self.runner(args)
        try:
            with patch.object(subprocess, "Popen", capture_process), patch.object(
                    self.mod.os, "killpg", side_effect=PermissionError("private kill error")):
                result = self.mod.run_drill(self.home, runner, go=True)
            self.assertEqual(result["execution"], "FAIL")
            self.assertEqual(result["recovery"], "FAIL")
            self.assertEqual((self.root / "index.html").read_bytes(), b"ansible lab sample drift\n")
            self.assertEqual(len(self.calls), 3, "no additional Ansible process may race a live child")
            self.assertTrue(all("--check" in args for args in self.calls))
        finally:
            for process in processes:
                try:
                    real_killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()

    def test_crlf_source_and_response_check_and_go_preserve_original_bytes(self):
        self.original = b"ansible lab sample ready\r\n"
        self.template.write_bytes(self.original)
        (self.root / "index.html").write_bytes(self.original)
        checked = self.mod.run_drill(self.home, self.runner)
        self.assertEqual(checked["execution"], "NOT_RUN")
        self.assertEqual((self.root / "index.html").read_bytes(), b"ansible lab sample ready\r\n")
        def preserve(args):
            body = (self.root / "index.html").read_bytes()
            if "--diff" in args and body != self.original:
                self.assertEqual(body, b"ansible lab sample drift\r\n")
            return self.runner(args)
        result = self.mod.run_drill(self.home, preserve, go=True)
        self.assertEqual(result["execution"], "PASS")
        self.assertEqual(result["idempotence"], "PASS")
        self.assertEqual((self.root / "index.html").read_bytes(), b"ansible lab sample ready\r\n")

    def test_crlf_failure_recovers_exact_captured_original(self):
        self.original = b"ansible lab sample ready\r\n"
        self.template.write_bytes(self.original)
        (self.root / "index.html").write_bytes(self.original)
        def interrupted(args):
            if "--diff" in args and (self.root / "index.html").read_bytes() != self.original:
                raise KeyboardInterrupt()
            return self.runner(args)
        result = self.mod.run_drill(self.home, interrupted, go=True)
        self.assertEqual(result.get("recovery"), "PASS")
        self.assertEqual((self.root / "index.html").read_bytes(), b"ansible lab sample ready\r\n")

    def test_response_must_match_source_bytes_in_both_newline_directions(self):
        for source, response in ((b"ansible lab sample ready\r\n", b"ansible lab sample ready\n"),
                                 (b"ansible lab sample ready\n", b"ansible lab sample ready\r\n")):
            with self.subTest(source=source):
                self.calls.clear()
                self.template.write_bytes(source)
                (self.root / "index.html").write_bytes(response)
                result = self.mod.run_drill(self.home, self.runner, go=True)
                self.assertEqual(result["execution"], "FAIL")
                self.assertEqual(self.calls, [])
                self.assertEqual((self.root / "index.html").read_bytes(), response)

    def test_unapproved_template_content_is_not_a_new_owned_baseline(self):
        for content in (b"ansible lab sample ready\nextra\n", b"ansible lab sample ready\r", b"{{ custom }}\n"):
            with self.subTest(content=content):
                self.calls.clear()
                self.template.write_bytes(content)
                result = self.mod.run_drill(self.home, self.runner, go=True)
                self.assertEqual(result["execution"], "FAIL")
                self.assertEqual(self.calls, [])

    def test_runner_uses_real_english_utf8_locale_despite_invalid_inherited_locale(self):
        code = ("import locale, os; locale.setlocale(locale.LC_ALL, ''); "
                "print(locale.nl_langinfo(locale.CODESET)); print(os.environ['LC_ALL']); "
                "print(os.environ.get('LANGUAGE', 'unset')); print(os.environ.get('LC_MESSAGES', 'unset'))")
        with patch.dict(os.environ, {"LC_ALL": "invalid_user_locale", "LANG": "invalid_user_locale",
                                     "LANGUAGE": "ko", "LC_MESSAGES": "ko_KR.UTF-8"}):
            result = self.mod._runner([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].upper().replace("-", ""), "UTF8")
        self.assertIn(lines[1].lower().replace("-", ""), {"c.utf8", "en_us.utf8"})
        self.assertEqual(lines[2:], ["unset", "unset"])

    def test_absent_supported_utf8_locale_fails_before_ansible_execution(self):
        invocations = []
        def no_locale(args, cwd, env, **kwargs):
            invocations.append(args)
            return subprocess.CompletedProcess(args, 0, "C\nPOSIX\n", "")
        with patch.object(self.mod, "_run_process", no_locale):
            result = self.mod.run_drill(self.home, self.mod._runner, go=True)
        self.assertEqual(result["execution"], "FAIL")
        self.assertEqual(result.get("locale"), "UNAVAILABLE")
        self.assertEqual(invocations, [["locale", "-a"]])
        self.assertEqual((self.root / "index.html").read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
