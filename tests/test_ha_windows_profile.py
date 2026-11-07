"""Pinned component AST/helper fixtures; never native Windows acceptance."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ha_windows_profile', ROOT / 'infra/k8s/tools/n100_ha_windows_profile.py')
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import legacy_guard

FIXTURE = r'''
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $env:HA_PROFILE_DRIVER
$source=[IO.File]::ReadAllText($env:HA_PROFILE_SOURCE)
$pin='f07a7cf479aba1bbab238a8b3a7a7db4816145ecc71d1603348e85925d6d55b9'
$script:trace=@()
function Get-CimInstance($ClassName,$ErrorAction){$script:trace+=@($ClassName);return @()}
function Get-ScheduledTask($ErrorAction){$script:trace+=@('tasks');return @()}
'''


def report():
    return dict(schema_version=1, kind='ha-windows-component-diagnostic', authority=False,
                ready=False, source_sha256=PROFILE.APPROVED_SOURCE, status='measured',
                phases=[dict(phase=p, calls=1 if p in PROFILE.PHASES[:7] else 0,
                             count=0, elapsed_ms=0.0,
                             status='measured' if p in PROFILE.PHASES[:7] else 'not_run',
                             reason='none') for p in PROFILE.PHASES])


class WindowsProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        if not Path(cls.pwsh).is_file():
            raise AssertionError('PowerShell required; no skipped AST fixtures')

    def ps(self, tail, source=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.ps1'
            path.write_text(PROFILE.SOURCE.read_text() if source is None else source)
            env = dict(os.environ, HA_PROFILE_DRIVER=str(PROFILE.DRIVER), HA_PROFILE_SOURCE=str(path))
            encoded = base64.b64encode((FIXTURE + tail).encode('utf-16le')).decode()
            result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                    env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            return result.stdout.decode('utf-8-sig')

    def test_exact_ast_helpers_bodies_queries_and_startup_range(self):
        value = self.ps(r'''
$parts=Get-HaProfileParts $source $pin
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$t,[ref]$e)
$obs=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Get-LegacyWindowsObservation'},$true))[0]
foreach($name in $parts.queries.Keys){
 $literal='('+$parts.queries[$name]+')'
 $loop=@($obs.FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst] -and $a.Condition.Extent.Text -ceq $literal},$true))
 if($loop.Count -ne 1 -or $loop[0].Body.Extent.Text -cne $parts.loops[$name].body){throw 'body_not_exact'}
 $actual=@(& ([ScriptBlock]::Create($parts.queries[$name])))
 $reference=@(& ([ScriptBlock]::Create($loop[0].Condition.Extent.Text)))
 if(($actual|ConvertTo-Json -Depth 10) -cne ($reference|ConvertTo-Json -Depth 10)){throw 'raw_query_changed'}
}
foreach($definition in $parts.definitions){
 if(-not $source.Contains($definition)){throw 'helper_changed'}
 if($definition -match 'function (Get-LegacyWindowsObservation|Get-LegacyWriterProof|Invoke-LegacyReader|Assert-LegacyBundle)'){throw 'authority_definition_loaded'}
}
if(-not $source.Contains($parts.startup) -or $parts.startup.Contains('$principal =')){throw 'startup_boundary_changed'}
if(-not $parts.argv.StartsWith("Add-Type -TypeDefinition @'`nusing System;")) {throw 'interop_declaration_changed'}
if($parts.argv.Contains('Get-LegacyWindowsObservation') -or $parts.argv.Contains('Get-LegacyWriterProof')){throw 'interop_not_isolated'}
@{helpers=$parts.definitions.Count;loops=$parts.loops.Count;startup_exact=$true}|ConvertTo-Json -Compress
''')
        self.assertEqual(json.loads(value), dict(helpers=10, loops=3, startup_exact=True))

    def test_complete_helper_pin_precedes_any_source_or_provider_execution(self):
        for text in [PROFILE.SOURCE.read_text() + '\nthrow "PRIVATE_TOP_LEVEL"',
                     PROFILE.SOURCE.read_text().replace('Sort-Object ProcessId', 'Sort-Object ProcessId -Descending'),
                     PROFILE.SOURCE.read_text() + '\nfunction Get-LegacyRegistry {}']:
            value = self.ps(r'''
$sha=[Security.Cryptography.SHA256]::Create()
try{$callerPin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
$rejected=$false
try{Invoke-HaWindowsProfile $source $callerPin|Out-Null}catch{$rejected=$true}
if(-not $rejected -or $script:trace.Count){throw 'hard_pin_bypassed'}
'rejected'
''', text)
            self.assertEqual(value.strip(), 'rejected')

    def test_actual_registry_recursion_code_and_reference_helpers_preserve_raw_values(self):
        value = self.ps(r'''
$parts=Get-HaProfileParts $source $pin
foreach($definition in $parts.definitions){. ([ScriptBlock]::Create($definition))}
class MemoryRegistryKey {
 [string]$Name
 [hashtable]$Values
 [hashtable]$Children
 [bool]$Disposed=$false
 MemoryRegistryKey([string]$name,[hashtable]$values,[hashtable]$children){$this.Name=$name;$this.Values=$values;$this.Children=$children}
 [string[]]GetValueNames(){return [string[]]@($this.Values.Keys)}
 [object]GetValue([string]$name,[object]$fallback,[object]$options){return $this.Values[$name]}
 [object]GetValueKind([string]$name){return 'String'}
 [string[]]GetSubKeyNames(){return [string[]]@($this.Children.Keys)}
 [object]OpenSubKey([string]$name,[bool]$write){if($write){throw 'write_forbidden'};return $this.Children[$name]}
 [void]Dispose(){$this.Disposed=$true}
}
$script:hashCalls=@();$script:signatureCalls=@()
function Get-LegacyCodePath([string]$Command){return $Command.Trim('"')}
function Get-FileHash($LiteralPath,$Algorithm,$ErrorAction){$script:hashCalls+=@($LiteralPath);return @{Hash=('A'*64)}}
function Get-AuthenticodeSignature($LiteralPath,$ErrorAction){$script:signatureCalls+=@($LiteralPath);return @{Status='Valid';SignerCertificate=@{Subject='O=Microsoft Corporation'}}}
$env:SystemRoot='/fixture';$env:ProgramFiles='/fixture/programs'
$child=[MemoryRegistryKey]::new('fixture\InprocServer32',@{''='/fixture\taskschd.dll'},@{})
$key=[MemoryRegistryKey]::new('fixture',@{ImagePath='/fixture\service.dll';Opaque='PRIVATE_VALUE'},@{child=$child})
$raw=Get-LegacyRegistry $key
if($raw.values.Opaque[1] -cne 'PRIVATE_VALUE' -or $raw.code_count -ne 2 -or -not $raw.provider_verified -or -not $child.Disposed){throw 'registry_values_changed'}
if($script:hashCalls.Count -ne 2 -or $script:signatureCalls.Count -ne 2){throw 'source_reads_omitted'}
$refs=@(Get-LegacyReferenceHashes 'run /fixture/private.py' '')
if($refs.Count -ne 1 -or $refs[0] -cne ('a'*64)){throw 'reference_read_changed'}
$blocked=$false;try{Get-LegacyRegistry $key 17|Out-Null}catch{$blocked=$true}
if(-not $blocked){throw 'depth_gate_removed'}
@{registry_values_preserved=$true;hash_calls=3;signature_calls=2;depth_blocked=$true}|ConvertTo-Json -Compress
''')
        self.assertNotIn('PRIVATE_', value)
        self.assertTrue(json.loads(value)['registry_values_preserved'])

    def test_component_partial_failure_does_not_emit_source_provider_data(self):
        output = self.ps(r'''
function Get-ScheduledTask($ErrorAction){throw 'PRIVATE_TASK_EXCEPTION'}
Invoke-HaWindowsProfile $source $pin|ConvertTo-Json -Depth 6 -Compress
''')
        self.assertNotIn('PRIVATE_', output)
        value = PROFILE.validate(output, PROFILE.APPROVED_SOURCE)
        self.assertEqual(value['status'], 'partial')
        rows = {r['phase']: r for r in value['phases']}
        self.assertEqual(rows['services_query']['status'], 'measured')
        self.assertEqual(rows['tasks_query']['status'], 'blocked')
        self.assertEqual(rows['tasks']['status'], 'not_run')
        self.assertEqual(rows['startup']['status'], 'blocked')
        for key in ('complete', 'verified', 'nonce', 'plan_sha256'):
            self.assertNotIn(key, value)
        with self.assertRaises(legacy_guard.GuardBlocked):
            legacy_guard.verify(value)

    def test_exact_task_process_bodies_preserve_order_records_and_helper_failures(self):
        output = self.ps(r'''
$parts=Get-HaProfileParts $source $pin
foreach($definition in $parts.definitions){. ([ScriptBlock]::Create($definition))}
$script:config=@{legacy_root='__diagnostic_unapproved_root__'}
function Get-LegacyCodeHash($Command){return ('a'*64)}
function Test-LegacyProviderCode($Command){return $false}
function Get-FileHash($LiteralPath,$Algorithm,$ErrorAction){return @{Hash=('B'*64)}}
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){return '<private-task/>'}
function Get-ScheduledTask($ErrorAction){
 return @([pscustomobject]@{TaskName='z';TaskPath='\child\';State='Disabled';Settings=@{Enabled=$false};Actions=@([pscustomobject]@{Execute='/fixture/app.exe';Arguments='/fixture/run.py';WorkingDirectory='/fixture'})},
          [pscustomobject]@{TaskName='A';TaskPath='\';State='Ready';Settings=@{Enabled=$true};Actions=@([pscustomobject]@{Execute='/fixture/other.exe';Arguments='';WorkingDirectory=''})})
}
function Get-CimInstance($ClassName,$ErrorAction){
 return @([pscustomobject]@{ProcessId=212;ParentProcessId=211;Name='private-z';CommandLine='PRIVATE_ARGUMENT';ExecutablePath='/fixture/z.exe';CreationDate=[datetime]'2026-01-01Z';SessionId=1},
          [pscustomobject]@{ProcessId=211;ParentProcessId=1;Name='private-a';CommandLine='PRIVATE_OTHER';ExecutablePath='/fixture/a.exe';CreationDate=[datetime]'2026-01-01Z';SessionId=1})
}
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$t,[ref]$e)
$obs=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Get-LegacyWindowsObservation'},$true))[0]
foreach($name in @('tasks','processes')){
 $raw=@{};$raw[$name]=@(& ([ScriptBlock]::Create($parts.queries[$name])))
 $original=@($obs.FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst] -and $a.Body.Extent.Text -ceq $parts.loops[$name].body},$true))[0].Extent.Text
 $actual='foreach ('+$parts.loops[$name].variable+' in $raw[$name]) '+$parts.loops[$name].body
 $outputs=@()
 foreach($code in @($original,$actual)){
  $Watch=[Diagnostics.Stopwatch]::StartNew();$tasks=@();$processes=@();$ownParent=$false
  $servicePids=@{};$serviceImages=@{};$serviceProviders=@{}
  . ([ScriptBlock]::Create($code))
  $outputs+=,(Get-Variable -Name $name -ValueOnly)
 }
 if(($outputs[0]|ConvertTo-Json -Depth 10 -Compress) -cne ($outputs[1]|ConvertTo-Json -Depth 10 -Compress)){throw 'records_or_order_changed'}
 if($outputs[0].Count -ne 2){throw 'records_filtered'}
}
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){throw 'PRIVATE_XML_FAILURE'}
$blocked=0
$rawTasks=@(Get-ScheduledTask Stop)
foreach($code in @($parts.loops.tasks.body,$parts.loops.tasks.body)){
 $Watch=[Diagnostics.Stopwatch]::StartNew()
 try{. ([ScriptBlock]::Create('foreach ($task in $rawTasks) '+$code))}catch{if($_.Exception.Message -ceq 'PRIVATE_XML_FAILURE'){$blocked++}}
}
if($blocked -ne 2){throw 'failure_changed'}
@{records_equal=$true;order_equal=$true;helper_failure_preserved=$true}|ConvertTo-Json -Compress
''')
        self.assertNotIn('PRIVATE_', output)
        self.assertTrue(json.loads(output)['records_equal'])

    def test_metric_schema_rejects_authority_private_reason_and_nonfinite_values(self):
        good = report()
        self.assertEqual(PROFILE.validate(json.dumps(good), PROFILE.APPROVED_SOURCE), good)
        bads = []
        for key, value in [('authority', True), ('verified', True), ('ready', True)]:
            item = deepcopy(good); item[key] = value; bads.append(item)
        for value in (float('nan'), -1, True, float('inf')):
            item = deepcopy(good); item['phases'][0]['elapsed_ms'] = value; bads.append(item)
        item = deepcopy(good); item['phases'][0]['reason'] = 'PRIVATE_ERROR'; bads.append(item)
        item = deepcopy(good); item['phases'][0]['calls'] = 0; bads.append(item)
        for bad in bads:
            with self.assertRaises(PROFILE.DiagnosticError):
                PROFILE.validate(json.dumps(bad), PROFILE.APPROVED_SOURCE)

    def test_actual_component_hash_and_task_xml_wrappers_measure_success_and_failure(self):
        output = self.ps(r'''
$file=Join-Path ([IO.Path]::GetTempPath()) (([guid]::NewGuid().ToString())+'.exe')
[IO.File]::WriteAllText($file,'synthetic bytes')
try {
 function Get-CimInstance($ClassName,$ErrorAction){
  if($ClassName -ceq 'Win32_Service'){return @()}
  return [pscustomobject]@{ProcessId=211;ParentProcessId=1;Name='synthetic';CommandLine='PRIVATE_ARGUMENT';ExecutablePath=$file;CreationDate=[datetime]'2026-01-01Z';SessionId=1}
 }
 function Get-ScheduledTask($ErrorAction){return [pscustomobject]@{TaskName='PRIVATE_TASK';TaskPath='\';Actions=@()}}
 Invoke-HaWindowsProfile $source $pin|ConvertTo-Json -Depth 6 -Compress
} finally {[IO.File]::Delete($file)}
''')
        self.assertNotIn('PRIVATE_', output)
        value = PROFILE.validate(output, PROFILE.APPROVED_SOURCE)
        rows = {row['phase']: row for row in value['phases']}
        self.assertEqual(rows['hash']['calls'], 1)
        self.assertEqual(rows['hash']['count'], 1)
        self.assertEqual(rows['hash']['status'], 'measured')
        # Native Windows provider-root identity is unavailable on macOS; the
        # successful real file hash must not turn that failure into completion.
        self.assertIsNone(rows['processes']['count'])
        self.assertEqual(rows['processes']['status'], 'blocked')
        self.assertEqual(rows['task_xml']['calls'], 1)
        self.assertIsNone(rows['task_xml']['count'])
        self.assertEqual(rows['task_xml']['status'], 'blocked')

    def test_wrapper_preserves_private_return_and_error_and_original_deadline(self):
        output = self.ps(r'''
$parts=Get-HaProfileParts $source $pin
foreach($definition in $parts.definitions){. ([ScriptBlock]::Create($definition))}
$script:profileRows=@{hash=@{phase='hash';calls=0;count=0;elapsed_ms=0.0;status='not_run';reason='none'}}
$private=[pscustomobject]@{value='PRIVATE_RETURN'}
$returned=Invoke-HaProfileCall hash {return $private}
if(-not [object]::ReferenceEquals($private,$returned)){throw 'return_changed'}
$caught=$false
try{Invoke-HaProfileCall hash {throw 'PRIVATE_FAILURE'}|Out-Null}catch{$caught=($_.Exception.Message -ceq 'PRIVATE_FAILURE')}
if(-not $caught -or $script:profileRows.hash.count -ne $null -or $script:profileRows.hash.calls -ne 2){throw 'error_changed'}
$expired=$false
try{Assert-LegacyDeadline ([pscustomobject]@{Elapsed=[pscustomobject]@{TotalSeconds=2.001}})}catch{$expired=($_.Exception.Message -ceq 'legacy_guard_expired')}
if(-not $expired){throw 'deadline_widened'}
@{return_preserved=$true;error_preserved=$true;deadline_preserved=$true}|ConvertTo-Json -Compress
''')
        self.assertNotIn('PRIVATE_', output)
        self.assertTrue(json.loads(output)['deadline_preserved'])

    def test_local_windows_only_and_exact_verified_driver_environment(self):
        driver_pin = hashlib.sha256(PROFILE.DRIVER.read_bytes()).hexdigest()
        value = report()
        with patch.object(PROFILE.sys, 'platform', 'win32'), \
                patch.object(PROFILE.native, 'system_directory', return_value=Path('/fixture/Windows/System32')), \
                patch.object(PROFILE, 'capture', return_value=value) as capture:
            result = PROFILE.collect(PROFILE.APPROVED_SOURCE, driver_pin)
        args, kwargs = capture.call_args
        self.assertEqual(base64.b64decode(args[0][-1]).decode('utf-16le'), PROFILE.DRIVER.read_text())
        self.assertEqual(set(kwargs['environment']), {'SystemRoot', 'WINDIR', 'PSModulePath', 'PATH'})
        self.assertLess(kwargs['timeout'], 8)
        self.assertEqual(result['driver_sha256'], driver_pin)
        with patch.object(PROFILE.sys, 'platform', 'darwin'), patch.object(PROFILE, 'capture') as capture, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(PROFILE.main(['--collect-local', '--source-sha256', PROFILE.APPROVED_SOURCE,
                                          '--driver-sha256', driver_pin]), 1)
        self.assertEqual(json.loads(output.getvalue())['reason'], 'native_windows_required')
        capture.assert_not_called()
        with patch.object(PROFILE.sys, 'platform', 'win32'), patch.object(PROFILE, 'capture') as capture:
            with self.assertRaisesRegex(PROFILE.DiagnosticError, 'source_rejected'):
                PROFILE.collect('0' * 64, driver_pin)
            with self.assertRaisesRegex(PROFILE.DiagnosticError, 'source_rejected'):
                PROFILE.collect(PROFILE.APPROVED_SOURCE, '0' * 64)
        capture.assert_not_called()

    def test_real_driver_entrypoint_bounds_input_and_private_failures(self):
        payload = json.dumps({'source': PROFILE.SOURCE.read_text(),
                              'source_sha256': PROFILE.APPROVED_SOURCE}).encode()
        code = FIXTURE + '\n& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:HA_PROFILE_DRIVER)))'
        encoded = base64.b64encode(code.encode('utf-16le')).decode()
        for request, expected in [(payload, 0), (b'x' * 262145, 1),
                                  (b'{"source":"PRIVATE_SECRET"}', 1)]:
            env = dict(os.environ, HA_PROFILE_DRIVER=str(PROFILE.DRIVER), HA_PROFILE_SOURCE=str(PROFILE.SOURCE))
            result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                    input=request, env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, expected, result.stderr.decode(errors='replace'))
            output = result.stdout.decode('utf-8-sig')
            self.assertNotIn('PRIVATE_', output)
            if expected == 0:
                lines = output.splitlines()
                self.assertEqual(len(lines), 15)
                for line in lines[:-1]:
                    PROFILE.validate_progress(line, PROFILE.APPROVED_SOURCE)
                self.assertEqual(PROFILE.validate(lines[-1], PROFILE.APPROVED_SOURCE)['status'], 'partial')
            else:
                self.assertEqual(json.loads(output), dict(status='blocked', reason='diagnostic_input_rejected'))

    def test_stream_timeout_retains_only_validated_progress_and_reaps_child(self):
        progress_report = report(); progress_report['status'] = 'progress'
        envelope = dict(event='progress', active_phase='tasks_query', report=progress_report)
        code = 'import time;print(' + repr(json.dumps(envelope)) + ',flush=True);time.sleep(20)'
        spawned = []
        original = subprocess.Popen
        def create(*args, **kwargs):
            child = original(*args, **kwargs); spawned.append(child); return child
        started = time.monotonic()
        with patch.object(PROFILE.subprocess, 'Popen', side_effect=create):
            with self.assertRaises(PROFILE.StreamError) as caught:
                PROFILE.capture([sys.executable, '-I', '-c', code], b'', PROFILE.APPROVED_SOURCE, timeout=0.3)
        self.assertEqual(caught.exception.args, ('diagnostic_timeout',))
        self.assertEqual(caught.exception.last_progress, envelope)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(all(child.poll() is not None for child in spawned))
        with patch.object(PROFILE, 'collect', side_effect=caught.exception), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(PROFILE.main(['--collect-local', '--source-sha256', PROFILE.APPROVED_SOURCE,
                                          '--driver-sha256', 'a' * 64]), 1)
        value = json.loads(output.getvalue())
        self.assertEqual(value['status'], 'blocked')
        self.assertFalse(value['authority'])
        self.assertEqual(value['last_progress'], envelope)

    def test_stream_rejects_private_malformed_and_over_limit_lines_without_retaining_them(self):
        progress_report = report(); progress_report['status'] = 'progress'
        envelope = dict(event='progress', active_phase='tasks_query', report=progress_report)
        good = json.dumps(envelope)
        private = deepcopy(envelope); private['active_phase'] = 'PRIVATE_PATH'
        streams = [good + '\n' + json.dumps(private) + '\n',
                   good + '\nPRIVATE_EXCEPTION\n', 'x' * 16385 + '\n',
                   (good + '\n') * 17, ((' ' * 6000) + good + '\n') * 16]
        for stream in streams:
            with self.subTest(size=len(stream)):
                code = 'import sys;sys.stdout.write(' + repr(stream) + ');sys.stdout.flush()'
                with self.assertRaises(PROFILE.StreamError) as caught:
                    PROFILE.capture([sys.executable, '-I', '-c', code], b'', PROFILE.APPROVED_SOURCE, timeout=1)
                self.assertEqual(caught.exception.args, ('response_rejected',))
                self.assertIsNone(caught.exception.last_progress)
                self.assertNotIn('PRIVATE_', str(caught.exception))

    def test_stream_requires_final_report_and_accepts_inclusive_nested_metrics(self):
        final = report()
        final['phases'][7].update(calls=4, count=4, elapsed_ms=10000, status='measured')
        progress_report = deepcopy(final); progress_report['status'] = 'progress'
        progress = dict(event='progress', active_phase='startup', report=progress_report)
        stream = json.dumps(progress) + '\n' + json.dumps(final) + '\n'
        code = 'import sys;sys.stdout.write(' + repr(stream) + ')'
        actual = PROFILE.capture([sys.executable, '-I', '-c', code], b'', PROFILE.APPROVED_SOURCE, timeout=1)
        self.assertEqual(actual, final)
        driver_pin = hashlib.sha256(PROFILE.DRIVER.read_bytes()).hexdigest()
        with patch.object(PROFILE.sys, 'platform', 'win32'), \
                patch.object(PROFILE.native, 'system_directory', return_value=Path('/fixture/Windows/System32')), \
                patch.object(PROFILE, 'capture', return_value=deepcopy(final)):
            value = PROFILE.collect(PROFILE.APPROVED_SOURCE, driver_pin)
        self.assertGreater(value['phases'][7]['elapsed_ms'], value['elapsed_ms'])
        code = 'print(' + repr(json.dumps(progress)) + ')'
        with self.assertRaises(PROFILE.StreamError) as caught:
            PROFILE.capture([sys.executable, '-I', '-c', code], b'', PROFILE.APPROVED_SOURCE, timeout=1)
        self.assertEqual(caught.exception.args, ('query_unavailable',))
        self.assertEqual(caught.exception.last_progress, progress)

    def test_inherited_stdout_reports_incomplete_cleanup_without_blocking_close(self):
        # This descendant exits itself; capture must kill only its direct child
        # and must not block on the reader's buffered-stream lock.
        code = ('import subprocess,sys;subprocess.Popen([sys.executable,"-I","-c",'
                '"import time;time.sleep(1.5)"])')
        existing = {thread.ident for thread in threading.enumerate()}
        started = time.monotonic()
        with self.assertRaises(PROFILE.StreamError) as caught:
            PROFILE.capture([sys.executable, '-I', '-c', code], b'', PROFILE.APPROVED_SOURCE, timeout=0.15)
        self.assertEqual(caught.exception.args, ('cleanup_incomplete',))
        self.assertLess(time.monotonic() - started, 1)
        workers = [thread for thread in threading.enumerate()
                   if thread.ident not in existing and thread.name.startswith('ha-profile-')]
        self.assertTrue(workers)
        for worker in workers:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_stream_input_limit_and_blocked_writer_remain_bounded(self):
        with patch.object(PROFILE.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(PROFILE.StreamError, 'response_rejected'):
                PROFILE.capture([], b'x' * 262145, PROFILE.APPROVED_SOURCE, timeout=0.1)
        spawn.assert_not_called()
        started = time.monotonic()
        with self.assertRaisesRegex(PROFILE.StreamError, 'diagnostic_timeout'):
            PROFILE.capture([sys.executable, '-I', '-c', 'import time;time.sleep(20)'],
                            b'x' * 262144, PROFILE.APPROVED_SOURCE, timeout=0.15)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == '__main__':
    unittest.main()
