"""Run real local faults and verify recovery, evidence and bounded ownership."""
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'book-memo/tools'
CLI = TOOLS / 'incident_recovery_lab.py'


def load_lab():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return importlib.import_module('incident_recovery_lab')


def silent_actor(*_args):
    time.sleep(30)


def crashed_actor(*_args):
    os._exit(7)


class IncidentRecoveryTests(unittest.TestCase):
    def test_cause_timing_includes_evidence_verification(self):
        lab=load_lab()
        original=lab.command
        def delayed(*args, **kwargs):
            result=original(*args, **kwargs)
            if args[4]=='evidence':
                time.sleep(.15)
            return result
        with patch.object(lab,'command',delayed):
            case=lab.run_scenario('sqlite_full',time.monotonic()+20,lambda:False)
        self.assertTrue(case['valid'],case['reason'])
        self.assertGreaterEqual(case['timings']['automatic_cause_check_seconds'],.15)

    def test_start_failure_preserves_failure_and_removes_fixture(self):
        lab=load_lab()
        with patch('multiprocessing.process.BaseProcess.start',side_effect=OSError('start failed')):
            case=lab.run_scenario('sqlite_full',time.monotonic()+10,lambda:False)
        self.assertFalse(case['valid'])
        self.assertEqual(case['reason'],'controller_failed')
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_actor_crash_is_failure_and_reaped(self):
        lab=load_lab()
        with patch.object(lab,'search_actor',crashed_actor):
            case=lab.run_scenario('dependency_timeout',time.monotonic()+10,lambda:False)
        self.assertFalse(case['valid'])
        self.assertFalse(case['children_exited_cleanly'])
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_fixture_removal_error_keeps_partial_evidence(self):
        lab=load_lab()
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'fixture'; fixture.mkdir()
            with patch.object(tempfile,'mkdtemp',return_value=str(fixture)), \
                 patch.object(lab.shutil,'rmtree',side_effect=OSError('remove failed')):
                case=lab.run_scenario('sqlite_full',time.monotonic()+20,lambda:False)
            self.assertFalse(case['valid'])
            self.assertEqual(case['reason'],'cleanup_failed')
            self.assertTrue(case['cleanup_ok'])
            self.assertFalse(case['fixture_removed'])
            self.assertTrue(case['observations']['recovery']['integrity_ok'])
            self.assertNotIn('cleanup_verified',[e['event'] for e in case['events']])
            shutil.rmtree(fixture)

    def cli(self, *args, env=None, timeout=40):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=timeout)

    def test_actual_dependency_fault_recovers_same_servers(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'result.json'
            env = dict(os.environ, DEMO_MODE='1', NEWS_SEARCH_URL='https://invalid.example',
                       HTTP_PROXY='http://127.0.0.1:1', BOOK_MEMO_DB_PATH='/nonexistent/untouchable.sqlite3')
            p = self.cli('--scenario', 'dependency_timeout', '--output', str(out), env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            report = json.loads(out.read_text())
            self.assertTrue(report['valid'])
            case = report['cases'][0]
            self.assertTrue(case['cleanup_ok'])
            self.assertTrue(case['fixture_removed'])
            self.assertEqual(case['observations']['failure']['statuses'], ['ok','ok','unavailable'])
            self.assertEqual(case['observations']['recovery']['statuses'], ['ok']*3)
            events = [e['event'] for e in case['events']]
            self.assertEqual(events, load_lab().EVENTS)
            elapsed = [e['elapsed_seconds'] for e in case['events']]
            self.assertEqual(elapsed, sorted(elapsed))
            self.assertLess(case['timings']['detection_seconds'], 12)
            self.assertNotIn(str(Path.home()), out.read_text())
            self.assertNotIn('invalid.example', out.read_text())

    def test_actual_db_faults_preserve_data_and_retry_exactly_once(self):
        for scenario, code in [('sqlite_busy',5), ('sqlite_full',13)]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                out=Path(root)/'result.json'
                p=self.cli('--scenario',scenario,'--output',str(out))
                self.assertEqual(p.returncode,0,p.stderr)
                case=json.loads(out.read_text())['cases'][0]
                self.assertEqual(case['observations']['failure']['error_code'],code)
                self.assertTrue(case['observations']['failure']['unchanged'])
                for name in ['data_preserved','exactly_once','integrity_ok']:
                    self.assertTrue(case['observations']['recovery'][name])
                self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_existing_and_symlink_results_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'old.json'; target.write_text('original')
            link=Path(root)/'link.json'; link.symlink_to(target)
            for out in [target,link]:
                self.assertNotEqual(self.cli('--output',str(out)).returncode,0)
            self.assertEqual(target.read_text(),'original')

    def test_invalid_configuration_creates_no_result(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'invalid.json'
            for flag,value in [('--deadline','181'),('--deadline','nan'),('--repeats','4'),('--repeats','0')]:
                self.assertNotEqual(self.cli('--output',str(out),flag,value).returncode,0)
                self.assertFalse(out.exists())

    def test_timeout_leaves_partial_evidence_and_reaps_owned_actor(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'partial.json'
            p=self.cli('--output',str(out),'--deadline','.2')
            self.assertNotEqual(p.returncode,0)
            report=json.loads(out.read_text())
            self.assertFalse(report['valid'])
            self.assertEqual(report['reason'],'deadline')
            self.assertGreater(report['remaining_cases'],0)
            self.assertTrue(all(c['cleanup_ok'] and c['fixture_removed'] for c in report['cases']))

    def test_silent_actor_deadline_is_bounded_and_fixture_removed(self):
        lab=load_lab()
        with patch.object(lab,'search_actor',silent_actor):
            case=lab.run_scenario('dependency_timeout',time.monotonic()+.2,lambda:False)
        self.assertFalse(case['valid'])
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])
        self.assertEqual(case['reason'],'deadline')

    @unittest.skipUnless(os.name=='posix','POSIX signals')
    def test_sigterm_preserves_partial_evidence_and_other_process(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'interrupt.json'
            other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
            p=subprocess.Popen([sys.executable,str(CLI),'--output',str(out)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if out.exists() and out.stat().st_size:
                        break
                    time.sleep(.02)
                p.send_signal(signal.SIGTERM); p.communicate(timeout=15)
                report=json.loads(out.read_text())
                self.assertFalse(report['valid'])
                self.assertEqual(report['reason'],'interrupted')
                self.assertIsNone(other.poll())
                self.assertTrue(all(c['cleanup_ok'] for c in report['cases']))
            finally:
                if p.poll() is None:
                    p.kill(); p.communicate()
                other.terminate(); other.wait(timeout=5)
