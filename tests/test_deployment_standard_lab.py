"""Real Book HTTP releases, rollback, immutable preflight and owned cleanup."""
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

ROOT=Path(__file__).resolve().parents[1]
TOOLS=ROOT/'book-memo/tools'
CLI=TOOLS/'deployment_standard_lab.py'


def lab_module():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0,str(TOOLS))
    return importlib.import_module('deployment_standard_lab')


def silent_actor(*_args):
    time.sleep(30)


def crashed_actor(*_args):
    os._exit(7)


class DeploymentStandardTests(unittest.TestCase):
    def test_git_environment_cannot_redirect_source_metadata(self):
        lab=lab_module()
        expected=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,
                                         env={'PATH':os.defpath},text=True).strip()
        with patch.dict(os.environ,{'GIT_DIR':'/nonexistent/foreign.git','GIT_WORK_TREE':'/nonexistent'}):
            observed=lab.metadata(time.monotonic()+10,lambda:False)
        self.assertEqual(observed['git_sha'],expected)

    def test_actor_keeps_artifact_inventory_unchanged(self):
        lab=lab_module();original=lab.shutil.rmtree
        def verify_then_remove(root):
            try:
                for path in Path(root).iterdir():
                    if path.is_dir():
                        expected=json.loads((path/'release.json').read_text())
                        lab.verify_release(path,expected)
            finally:
                original(root)
        with patch.object(lab.shutil,'rmtree',side_effect=verify_then_remove):
            case=lab.run_scenario('redeploy',time.monotonic()+40,lambda:False)
        self.assertTrue(case['valid'],case['reason'])

    def cli(self,*args,env=None):
        return subprocess.run([sys.executable,str(CLI),*args],cwd=ROOT,env=env,
                              capture_output=True,text=True,timeout=45)

    def test_actual_redeploy_same_payload_new_release_preserves_http_data(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'result.json'
            env=dict(os.environ,BOOK_MEMO_DB_PATH='/nonexistent/untouchable.sqlite3',
                     HTTP_PROXY='http://127.0.0.1:1',PYTHONPATH='/nonexistent',DEMO_MODE='1',
                     GIT_DIR='/nonexistent/foreign.git',GIT_WORK_TREE='/nonexistent')
            p=self.cli('--scenario','redeploy','--output',str(out),env=env)
            self.assertEqual(p.returncode,0,p.stderr)
            case=json.loads(out.read_text())['cases'][0]
            self.assertTrue(case['valid'])
            self.assertTrue(case['deployment_succeeded'])
            self.assertEqual(case['active_release'],'candidate')
            self.assertEqual(case['starts'],2)
            self.assertEqual(case['releases']['baseline']['payload_digest'],case['releases']['candidate']['payload_digest'])
            self.assertNotEqual(case['releases']['baseline']['release_digest'],case['releases']['candidate']['release_digest'])
            self.assertTrue(case['data_preserved'] and case['functional_read_ok'])
            self.assertTrue(case['cleanup_ok'] and case['fixture_removed'] and case['children_exited_cleanly'])
            self.assertNotIn(str(Path.home()),out.read_text())
            self.assertNotIn('untouchable',out.read_text())

    def test_readiness_error_rolls_back_once_but_deployment_stays_failed(self):
        lab=lab_module()
        case=lab.run_scenario('readiness_failure',time.monotonic()+40,lambda:False)
        self.assertTrue(case['valid'],case['reason'])
        self.assertFalse(case['deployment_succeeded'])
        self.assertTrue(case['rollback_succeeded'])
        self.assertEqual(case['rollback_attempts'],1)
        self.assertEqual(case['starts'],3)
        self.assertEqual(case['active_release'],'baseline')
        self.assertEqual(case['observations']['candidate_probe']['health_status'],200)
        self.assertEqual(case['observations']['candidate_probe']['readiness_status'],404)
        self.assertTrue(case['data_preserved'] and case['functional_read_ok'])

    def test_tamper_rejected_before_baseline_stop(self):
        case=lab_module().run_scenario('artifact_tamper',time.monotonic()+40,lambda:False)
        self.assertTrue(case['valid'],case['reason'])
        self.assertFalse(case['deployment_succeeded'])
        self.assertIsNone(case['rollback_succeeded'])
        self.assertEqual(case['starts'],1)
        self.assertEqual(case['rollback_attempts'],0)
        self.assertEqual(case['active_release'],'baseline')
        self.assertTrue(case['observations']['preflight_rejected'])
        self.assertNotIn('previous_stopped',[e['event'] for e in case['events']])

    def test_rollback_failure_does_not_retry(self):
        lab=lab_module();original=lab.start_release;count=0
        def fail_rollback(*args,**kwargs):
            nonlocal count
            count+=1
            if count>=3:
                raise lab.Stopped('test_rollback_failed')
            return original(*args,**kwargs)
        with patch.object(lab,'start_release',fail_rollback):
            case=lab.run_scenario('readiness_failure',time.monotonic()+40,lambda:False)
        self.assertFalse(case['valid'])
        self.assertFalse(case['deployment_succeeded'])
        self.assertEqual(case['rollback_attempts'],1)
        self.assertEqual(count,3)
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_old_actor_stop_failure_never_starts_candidate(self):
        lab=lab_module()
        with patch.object(lab,'stop_release',side_effect=lab.Stopped('stop_failed')):
            case=lab.run_scenario('redeploy',time.monotonic()+40,lambda:False)
        self.assertFalse(case['valid'])
        self.assertEqual(case['starts'],1)
        self.assertFalse(case['deployment_succeeded'])
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_actor_crash_and_silence_are_bounded_failures(self):
        lab=lab_module()
        for actor in [crashed_actor,silent_actor]:
            with self.subTest(actor=actor.__name__),patch.object(lab,'release_actor',actor):
                case=lab.run_scenario('redeploy',time.monotonic()+.5,lambda:False)
            self.assertFalse(case['valid'])
            self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_start_failure_removes_fixture(self):
        lab=lab_module()
        with patch('multiprocessing.process.BaseProcess.start',side_effect=OSError('start failed')):
            case=lab.run_scenario('redeploy',time.monotonic()+20,lambda:False)
        self.assertFalse(case['valid'])
        self.assertTrue(case['cleanup_ok'] and case['fixture_removed'])

    def test_removal_failure_is_not_valid(self):
        lab=lab_module()
        import shutil
        with tempfile.TemporaryDirectory() as root:
            fixture=Path(root)/'fixture';fixture.mkdir()
            with patch.object(tempfile,'mkdtemp',return_value=str(fixture)), \
                 patch.object(lab.shutil,'rmtree',side_effect=OSError('cleanup failed')):
                case=lab.run_scenario('redeploy',time.monotonic()+40,lambda:False)
            self.assertFalse(case['valid'])
            self.assertEqual(case['reason'],'cleanup_failed')
            self.assertFalse(case['fixture_removed'])
            shutil.rmtree(fixture)

    def test_invalid_options_and_existing_result_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'result.json'
            for key,val in [('--deadline','181'),('--deadline','nan'),('--repeats','0'),('--repeats','4')]:
                self.assertNotEqual(self.cli('--output',str(out),key,val).returncode,0)
                self.assertFalse(out.exists())
            out.write_text('original');link=Path(root)/'link.json';link.symlink_to(out)
            for path in [out,link]:
                self.assertNotEqual(self.cli('--output',str(path)).returncode,0)
            self.assertEqual(out.read_text(),'original')

    def test_deadline_preserves_partial_report(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'partial.json'
            self.assertNotEqual(self.cli('--output',str(out),'--deadline','.01').returncode,0)
            report=json.loads(out.read_text())
            self.assertFalse(report['valid'])
            self.assertEqual(report['reason'],'deadline')
            self.assertGreater(report['remaining_cases'],0)
            self.assertTrue(all(c['cleanup_ok'] and c['fixture_removed'] for c in report['cases']))

    @unittest.skipUnless(os.name=='posix','POSIX signals')
    def test_sigterm_preserves_other_process_and_partial_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root)/'partial.json'
            other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            p=subprocess.Popen([sys.executable,str(CLI),'--output',str(out)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if out.exists() and out.stat().st_size:
                        break
                    time.sleep(.02)
                p.send_signal(signal.SIGTERM);p.communicate(timeout=15)
                report=json.loads(out.read_text());self.assertFalse(report['valid'])
                self.assertEqual(report['reason'],'interrupted')
                self.assertIsNone(other.poll())
                self.assertTrue(all(c['cleanup_ok'] for c in report['cases']))
            finally:
                if p.poll() is None:
                    p.kill();p.communicate()
                other.terminate();other.wait(timeout=5)
