"""New isolated test module prototype; no SCM calls and no tracked writes."""
import json,os,shutil,subprocess,unittest
import hashlib,importlib.util,sys,tempfile
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/"infra/k8s/ha_control/stopped_service_installer"
FIXTURE=ROOT/"tests/fixtures/ha_stopped_identity_reservation.ps1"
class StoppedIdentityReservationTests(unittest.TestCase):
 def test_compiled_negative_guards_and_no_native_calls(self):
  pwsh=os.environ.get('HA_PWSH') or shutil.which('pwsh')
  if not pwsh:
   repo=next((p for p in HERE.parents if (p/'AGENTS.md').exists()),None)
   candidate=repo/'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh' if repo else None
   pwsh=str(candidate) if candidate and candidate.exists() else None
  if not pwsh:pwsh='pwsh'  # Required in CI; absence must not silently skip the core gate.
  run=subprocess.run([pwsh,'-NoProfile','-File',str(FIXTURE),'-SourceRoot',str(HERE)],capture_output=True,text=True,check=True)
  value=json.loads(run.stdout);self.assertGreaterEqual(value['tests_passed'],36);self.assertFalse(value['native_calls_performed']);self.assertFalse(value['service_created']);self.assertEqual(value['compile'],'PASS');self.assertEqual(value['native_sddl_acl_service_integration'],'NOT_RUN')
 def test_host_replacement_requires_explicit_native_intent(self):
  candidate=ROOT/'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh'
  pwsh=os.environ.get('HA_PWSH') or shutil.which('pwsh') or (str(candidate) if candidate.exists() else 'pwsh')
  run=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-File',str(HERE/'replace-host.ps1'),
                      '-Request','test-input','-RequestSha256','0'*64,'-SupportSha256','0'*64],
                     capture_output=True,text=True,timeout=20)
  self.assertEqual(run.returncode,1,run.stderr)
  self.assertEqual(json.loads(run.stdout),{
   'scope':'stopped_service_host_replacement','status':'BLOCKED','failed_stage':'native_intent',
   'reconcile_required':False,'service_started':False,'authority_issued':False,
  })
  self.assertNotIn('test-input',run.stdout+run.stderr)
 def test_phase_one_has_no_phase_two_plan_dependencies(self):
  source=(HERE/'reserve-identity.ps1').read_text();self.assertNotIn("'plan_sha256'",source);self.assertNotIn("'service_args'",source);self.assertIn("args_present=$false",source);self.assertIn("ready_to_start=$false",source)
 def test_no_service_start_stop_delete_api_exists(self):
  source=(HERE/'StoppedService.cs').read_text();self.assertNotIn('StartService',source);self.assertNotIn('ControlService',source);self.assertNotIn('DeleteService',source);self.assertNotIn('ChangeServiceConfigW',source)
class SourceProvenanceTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.repo=Path(self.temp.name)
  self.git('init','-q')
  source=self.repo/'infra/k8s/ha_control';source.mkdir(parents=True)
  (source/'windows-service-host.cs').write_bytes(b'origin host\n');(source/'windows-service-preflight.ps1').write_bytes(b'preflight\n')
  self.git('add','.');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','origin');self.origin=self.git('rev-parse','HEAD').decode().strip()
  pins={'compiler':{'fixture':'a'*64},'references':{'fixture':'b'*64}}
  self.receipt={'source_commit':self.origin,'source_sha256':hashlib.sha256(b'origin host\n').hexdigest(),**pins,'pins_sha256':hashlib.sha256((json.dumps(pins,sort_keys=True,indent=2)+'\n').encode()).hexdigest()}
  (source/'windows-service-host.cs').write_bytes(b'different selected host\n');self.git('add','.');self.git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','changed');self.changed=self.git('rev-parse','HEAD').decode().strip()
  self.receipt_path=self.repo/'receipt.json';self.output=self.repo/'proof.json';self.write_receipt(self.receipt)
 def git(self,*args):return subprocess.check_output(['git',*args],cwd=self.repo,stderr=subprocess.PIPE)
 def write_receipt(self,value):
  raw=(json.dumps(value,indent=3)+'\n').encode();self.receipt_path.write_bytes(raw);return raw
 def cli(self,selected):
  return subprocess.run([sys.executable,'-O',str(HERE/'prove_source.py'),'--selected',selected,'--build-receipt',str(self.receipt_path),'--output',str(self.output)],cwd=self.repo,capture_output=True,text=True)
 def test_optimized_python_rejects_different_selected_source(self):
  result=self.cli(self.changed);self.assertNotEqual(result.returncode,0);self.assertFalse(self.output.exists())
 def test_optimized_python_rejects_source_and_compiler_receipt_pin_mismatch(self):
  for key in ('source_sha256','pins_sha256'):
   with self.subTest(key=key):
    receipt=dict(self.receipt);receipt[key]='0'*64;self.write_receipt(receipt);result=self.cli(self.origin);self.assertNotEqual(result.returncode,0);self.assertFalse(self.output.exists())
 def test_exact_receipt_bytes_pinned_and_create_new_output_preserved(self):
  raw=self.receipt_path.read_bytes();result=self.cli(self.origin);self.assertEqual(result.returncode,0,result.stderr);proof=json.loads(self.output.read_bytes());self.assertEqual(proof['original_build_receipt_sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(proof['source_commit'],self.origin);self.assertEqual(proof['build_origin_commit'],self.origin);self.assertFalse(proof['original_receipt_relabelled']);saved=self.output.read_bytes();self.assertNotEqual(self.cli(self.origin).returncode,0);self.assertEqual(self.output.read_bytes(),saved)
 def test_receipt_is_read_once_for_both_parse_and_hash(self):
  spec=importlib.util.spec_from_file_location('stopped_prove_source_under_test',HERE/'prove_source.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  raw=self.receipt_path.read_bytes();real_check=subprocess.check_output
  def local_git(*args,**kwargs):kwargs['cwd']=self.repo;return real_check(*args,**kwargs)
  with mock.patch.object(module.subprocess,'check_output',side_effect=local_git),mock.patch.object(module.Path,'read_bytes',side_effect=[raw,b'{"source_commit":"changed"}']) as reads:
   module.main(['--selected',self.origin,'--build-receipt',str(self.receipt_path),'--output',str(self.output)])
   self.assertEqual(reads.call_count,1)
  self.assertEqual(json.loads(self.output.read_bytes())['original_build_receipt_sha256'],hashlib.sha256(raw).hexdigest())

if __name__=='__main__':unittest.main()
