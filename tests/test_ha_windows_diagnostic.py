"""Real PS AST query fixtures and bounded diagnostic process tests; no native queries."""
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
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ha_windows_diagnostic', ROOT / 'infra/k8s/tools/n100_ha_windows_diagnostic.py')
DIAG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAG)
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import legacy_guard
from ha_control.windows import WindowsFence
from ha_control.controller import TransitionBlocked

# Same raw dataset for independent source pipeline and extracted pipeline calls.
# Hidden/disabled/subfolder/COM tasks and inaccessible process fields must stay.
FIXTURE = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $env:HA_DIAG_DRIVER
$source=[IO.File]::ReadAllText($env:HA_DIAG_SOURCE)
$sha=[Security.Cryptography.SHA256]::Create()
try {$pin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))).Replace('-','').ToLowerInvariant()}
finally {$sha.Dispose()}
$script:trace=@();$script:failTask=$false
function Get-CimInstance($ClassName,$ErrorAction) {
 $script:trace+=@('cim:'+$ClassName+':'+$ErrorAction)
 if($ClassName -ceq 'Win32_Service') {
  return @([pscustomobject]@{Name='z';State='Stopped';StartMode='Disabled';PathName='PRIVATE_SERVICE'},[pscustomobject]@{Name='A';State='Running';StartMode='Auto';PathName='PRIVATE_OTHER'})
 }
 if($ClassName -cne 'Win32_Process'){throw 'unexpected_query'}
 return @([pscustomobject]@{ProcessId=21;ExecutablePath=$null;CommandLine=$null},[pscustomobject]@{ProcessId=4;ExecutablePath='PRIVATE_PATH';CommandLine='PRIVATE_ARGUMENTS'})
}
function Get-ScheduledTask($ErrorAction) {
 $script:trace+=@('tasks:'+$ErrorAction)
 if($script:failTask){throw 'PRIVATE_EXCEPTION_TOKEN'}
 return @([pscustomobject]@{TaskPath='\child\';TaskName='z';State='Disabled';Hidden=$true;Actions=@{ClassId='PRIVATE_COM'}},[pscustomobject]@{TaskPath='\';TaskName='A';State='Ready';Hidden=$false;Actions=@{Execute='PRIVATE_EXEC'}})
}
"""


def report():
    return dict(schema_version=1, kind='ha-enumeration-diagnostic', authority=False, ready=False,
                scope='windows-enumeration-only', source_sha256='a' * 64, status='measured',
                phases=[dict(phase=p, status='measured', reason='none', count=2, elapsed_ms=1.0) for p in DIAG.PHASES])


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        if not Path(cls.pwsh).is_file():
            raise AssertionError('PowerShell required; never skip AST fixtures')

    def ps(self, tail, source=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.ps1'
            path.write_text(DIAG.SOURCE.read_text() if source is None else source)
            env = dict(os.environ, HA_DIAG_DRIVER=str(DIAG.DRIVER), HA_DIAG_SOURCE=str(path))
            script = base64.b64encode((FIXTURE + tail).encode('utf-16le')).decode()
            result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', script],
                                    env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            return result.stdout.decode('utf-8-sig')

    def test_actual_ast_queries_equal_original_raw_records_and_keep_all_properties(self):
        result = self.ps(r"""
$queries=Get-HaDiagnosticQueries $source $pin
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$t,[ref]$e)
$original=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst]},$true))
$seen=0
foreach($name in $queries.Keys) {
 $script:trace=@();$actual=@(& $queries[$name]);$actualTrace=$script:trace
 $literal=$queries[$name].ToString()
 $loop=@($original|Where-Object {$_.Condition.Extent.Text -ceq ('('+$literal+')')})
 if($loop.Count -ne 1){throw 'original_foreach_required'}
 $script:trace=@();$reference=@(& ([ScriptBlock]::Create($loop[0].Condition.Extent.Text)))
 if(($actual|ConvertTo-Json -Depth 8 -Compress) -cne ($reference|ConvertTo-Json -Depth 8 -Compress)){throw 'raw_records_differ'}
 if(($actualTrace -join ',') -cne ($script:trace -join ',')){throw 'calls_differ'}
 if($actual.Count -ne 2){throw 'records_filtered'}
 # Same-count replacement must fail full-record comparison, not pass on count.
 $replacement=@($reference[0],$reference[0])
 if(($replacement|ConvertTo-Json -Depth 8 -Compress) -ceq ($actual|ConvertTo-Json -Depth 8 -Compress)){throw 'replacement_undetected'}
 $seen++
}
@{phases=$seen;raw_equal=$true;replacement_detected=$true}|ConvertTo-Json -Compress
""")
        self.assertEqual(json.loads(result), dict(phases=3, raw_equal=True, replacement_detected=True))

    def test_partial_failure_and_no_private_provider_data_in_report(self):
        result = self.ps(r"""
$script:failTask=$true
Invoke-HaEnumerationDiagnostic $source $pin | ConvertTo-Json -Depth 6 -Compress
""")
        self.assertNotIn('PRIVATE_', result)
        value = json.loads(result)
        DIAG.validate(result, value['source_sha256'])
        self.assertEqual(value['status'], 'partial')
        self.assertEqual([r['count'] for r in value['phases']], [2, None, 2])
        self.assertEqual(value['phases'][1]['reason'], 'query_failed')

    def test_empty_success_is_distinct_from_failed_query(self):
        result = self.ps(r"""
function Get-ScheduledTask($ErrorAction) {return @()}
Invoke-HaEnumerationDiagnostic $source $pin | ConvertTo-Json -Depth 6 -Compress
""")
        value = DIAG.validate(result, hashlib.sha256(DIAG.SOURCE.read_bytes()).hexdigest())
        self.assertEqual(value['phases'][1]['count'], 0)
        self.assertEqual(value['status'], 'measured')

    def test_source_pin_parse_query_and_duplicate_rejections_precede_any_queries(self):
        source = DIAG.SOURCE.read_text()
        mutants = [source.replace('Sort-Object ProcessId', 'Sort-Object ProcessId -Descending'),
                   source + '\nfunction Get-LegacyWindowsObservation {}', source + '\nfunction {',
                   source.replace('$services = @();', '$unused = (Get-CimInstance Win32_Process -ErrorAction Stop | Sort-Object ProcessId); $services = @();')]
        for text in mutants:
            with self.subTest(mutant=text[-35:]):
                result = self.ps(r"""
$rejected=$false
try {Invoke-HaEnumerationDiagnostic $source $pin|Out-Null} catch {$rejected=$true}
if(-not $rejected -or $script:trace.Count){throw 'source_not_rejected_before_query'}
'rejected'
""", text)
                self.assertEqual(result.strip(), 'rejected')
        result = self.ps(r"""
$rejected=$false
try {Invoke-HaEnumerationDiagnostic $source ('0'*64)|Out-Null} catch {$rejected=$true}
if(-not $rejected -or $script:trace.Count){throw 'pin_bypassed'}
'rejected'
""")
        self.assertEqual(result.strip(), 'rejected')

    def test_source_statements_and_full_guard_are_never_executed(self):
        source = 'throw "PRIVATE_TOP_LEVEL"\n' + DIAG.SOURCE.read_text()
        source = source.replace('$services = @();', 'throw "PRIVATE_GUARD_BODY"; $services = @();')
        value = json.loads(self.ps('Invoke-HaEnumerationDiagnostic $source $pin | ConvertTo-Json -Depth 6 -Compress', source))
        self.assertEqual(value['status'], 'measured')

    def test_response_schema_rejects_authority_and_malformed_data(self):
        good = report()
        self.assertEqual(DIAG.validate(json.dumps(good), 'a' * 64), good)
        bads = []
        for key, value in [('authority', True), ('ready', True), ('verified', True), ('schema_version', True), ('status', 'complete')]:
            item = deepcopy(good); item[key] = value; bads.append(item)
        for value in (float('nan'), float('inf'), -1, True, 'secret'):
            item = deepcopy(good); item['phases'][0]['elapsed_ms'] = value; bads.append(item)
        item = deepcopy(good); item['phases'][1] = item['phases'][0]; bads.append(item)
        item = deepcopy(good); item['phases'][0]['count'] = True; bads.append(item)
        item = deepcopy(good); item['status'] = 'partial'; bads.append(item)
        for bad in bads:
            with self.assertRaises(DIAG.DiagnosticError):
                DIAG.validate(json.dumps(bad), 'a' * 64)
        with self.assertRaises(DIAG.DiagnosticError):
            DIAG.validate('{"ready":false,"ready":true}', 'a' * 64)

    def test_production_guard_and_native_bridge_reject_diagnostic(self):
        value = report()
        with self.assertRaises(legacy_guard.GuardBlocked):
            legacy_guard.verify(value)
        bridge = object.__new__(WindowsFence)
        bridge.process = type('Child', (), {'poll': lambda self: None, 'stdin': io.StringIO()})()
        bridge.answers = __import__('queue').Queue(); bridge.answers.put(json.dumps(value))
        bridge.plan_sha256 = 'a' * 64; bridge.plan_data = {'cluster': 'fixture', 'legacy_linux_sha256': 'b' * 64}
        with self.assertRaises(TransitionBlocked):
            bridge.request('observe', {'old': 'A', 'generation': 1})

    def test_real_child_output_cap_timeout_input_block_and_stderr_redaction(self):
        good = DIAG.capture([sys.executable, '-I', '-c', 'import sys;sys.stdin.buffer.read();sys.stderr.write("PRIVATE_TOKEN");print("ok")'], b'input')
        self.assertEqual(good.strip(), b'ok')
        for code, payload, reason in [
            ('import sys;sys.stdout.write("x"*20000);sys.stdout.flush()', b'', 'response_rejected'),
            ('import time;time.sleep(20)', b'', 'diagnostic_timeout'),
            ('import time;time.sleep(20)', b'x'*1048576, 'diagnostic_timeout'),
            ('import sys;sys.stderr.write("PRIVATE_TOKEN");sys.exit(1)', b'', 'query_unavailable'),
        ]:
            with self.subTest(reason=reason):
                started = time.monotonic()
                with self.assertRaisesRegex(DIAG.DiagnosticError, '^'+reason+'$'):
                    DIAG.capture([sys.executable, '-I', '-c', code], payload, timeout=0.3)
                self.assertLess(time.monotonic() - started, 3)

    def test_non_windows_cli_never_launches_child_or_prints_exception(self):
        with patch.object(DIAG.sys, 'platform', 'darwin'), patch.object(DIAG.subprocess, 'Popen') as run, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(DIAG.main(['--collect-local', '--source-sha256', 'a'*64, '--driver-sha256', 'b'*64]), 1)
            self.assertEqual(json.loads(output.getvalue())['reason'], 'native_windows_required')
            run.assert_not_called()

    def test_collect_executes_verified_driver_bytes_with_fixed_environment(self):
        source_pin = hashlib.sha256(DIAG.SOURCE.read_bytes()).hexdigest()
        driver_pin = hashlib.sha256(DIAG.DRIVER.read_bytes()).hexdigest()
        value = report(); value['source_sha256'] = source_pin
        with patch.object(DIAG.sys, 'platform', 'win32'), patch.object(DIAG, 'system_directory', return_value=Path('/fixture/Windows/System32')), patch.dict(os.environ, {'SystemRoot': 'PRIVATE_UNTRUSTED_ROOT', 'PSModulePath': 'PRIVATE_UNTRUSTED_MODULE', 'PRIVATE_TOKEN': 'secret'}), patch.object(DIAG, 'capture', return_value=json.dumps(value).encode()) as run, patch.object(DIAG.time, 'monotonic', side_effect=[10, 10.01, 10.1]):
            result = DIAG.collect(source_pin, driver_pin)
            args, kwargs = run.call_args
            self.assertEqual(args[0][1:4], ['-NoProfile', '-NonInteractive', '-EncodedCommand'])
            self.assertEqual(base64.b64decode(args[0][4]).decode('utf-16le'), DIAG.DRIVER.read_text())
            self.assertEqual(json.loads(args[1]), {'source': DIAG.SOURCE.read_text(), 'source_sha256': source_pin})
            self.assertNotIn('PRIVATE_', json.dumps(kwargs))
            self.assertEqual(set(kwargs['environment']), {'SystemRoot', 'WINDIR', 'PSModulePath', 'PATH'})
            self.assertEqual(args[0][0], '/fixture/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
            self.assertLess(kwargs['timeout'], DIAG.TIMEOUT)
            self.assertEqual(result['driver_sha256'], driver_pin)
        for source, driver in [('0'*64, driver_pin), (source_pin, '0'*64)]:
            with patch.object(DIAG.sys, 'platform', 'win32'), patch.object(DIAG, 'capture') as run:
                with self.assertRaisesRegex(DIAG.DiagnosticError, '^source_rejected$'):
                    DIAG.collect(source, driver)
                run.assert_not_called()

    def test_system_directory_is_read_from_native_api_not_environment(self):
        class Kernel:
            def GetSystemDirectoryW(self, buffer, size):
                self.size = size
                buffer.value = '/fixture/System32'
                return len(buffer.value)
        kernel = Kernel()
        with patch.object(DIAG.ctypes, 'windll', type('Native', (), {'kernel32': kernel})(), create=True), patch.dict(os.environ, {'SystemRoot': 'PRIVATE_ROOT'}):
            self.assertEqual(DIAG.system_directory(), Path('/fixture/System32'))
            self.assertEqual(kernel.size, 32768)
        for value in (0, 32768):
            with patch.object(DIAG.ctypes, 'windll', type('Native', (), {'kernel32': type('Kernel', (), {'GetSystemDirectoryW': lambda *args: value})()})(), create=True):
                with self.assertRaises(DIAG.DiagnosticError):
                    DIAG.system_directory()

    def test_real_driver_entrypoint_reads_bounded_input_and_redacts_failures(self):
        pin = hashlib.sha256(DIAG.SOURCE.read_bytes()).hexdigest()
        payload = json.dumps({'source': DIAG.SOURCE.read_text(), 'source_sha256': pin}).encode()
        with patch.dict(os.environ, {'HA_DIAG_DRIVER': str(DIAG.DRIVER), 'HA_DIAG_SOURCE': str(DIAG.SOURCE)}):
            for request, expected_code in [(payload, 0), (b'x'*262145, 1), (b'{"source":"PRIVATE_SECRET"}', 1)]:
                code = FIXTURE + '\n& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:HA_DIAG_DRIVER)))'
                encoded = base64.b64encode(code.encode('utf-16le')).decode()
                result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], input=request, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, expected_code, result.stderr.decode(errors='replace'))
                text = result.stdout.decode('utf-8-sig')
                self.assertNotIn('PRIVATE_', text)
                if expected_code == 0:
                    self.assertEqual(DIAG.validate(text, pin)['status'], 'measured')
                else:
                    self.assertEqual(json.loads(text), {'status': 'blocked', 'reason': 'diagnostic_input_rejected'})


if __name__ == '__main__':
    unittest.main()
