"""Real PowerShell candidate fixtures; never native provider equivalence proof."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'infra/k8s/tools/n100-ha-windows-task-candidate.ps1'
SOURCE = ROOT / 'infra/k8s/ha_control/windows-legacy-guard.ps1'
FIXTURE = r'''
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $env:HA_CANDIDATE_DRIVER
function MakeTask($Name='PRIVATE_TASK',$State=1,$Enabled=$false,$Type=0,$Folder='\'){
 if(-not $Folder.EndsWith('\')){$Folder+='\'}
 $actionXml='<Exec><Command>private.exe</Command><Arguments>PRIVATE_ARGUMENT</Arguments><WorkingDirectory>PRIVATE_DIRECTORY</WorkingDirectory></Exec>'
 if($Type -eq 5){$actionXml='<ComHandler><ClassId>PRIVATE_CLSID</ClassId></ComHandler>'}
 elseif($Type -ne 0){$actionXml='<Unsupported/>'}
 $xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers/><Principals/><Settings><Enabled>'+$Enabled.ToString().ToLowerInvariant()+'</Enabled></Settings><Actions>'+$actionXml+'</Actions></Task>'
 [pscustomobject]@{Name=$Name;Path=($Folder+$Name);State=$State;Xml=$xml;Definition=[pscustomobject]@{Settings=[pscustomobject]@{Enabled=$Enabled};Actions=@([pscustomobject]@{Type=$Type;Path='private.exe';Arguments='PRIVATE_ARGUMENT';WorkingDirectory='PRIVATE_DIRECTORY';ClassId='PRIVATE_CLSID'})}}
}
function MakeFolder($Path,$Tasks,$Children){
 $f=[pscustomobject]@{Path=$Path;Tasks=$Tasks;Children=$Children}
 $f|Add-Member ScriptMethod GetTasks {param($flags) if($flags -ne 1){throw 'hidden_omitted'};return $this.Tasks}
 $f|Add-Member ScriptMethod GetFolders {param($flags) if($flags -ne 0){throw 'bad_reserved_flags'};return $this.Children}
 return $f
}
'''


class WindowsTaskCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        if not Path(cls.pwsh).is_file():
            raise AssertionError('PowerShell required; no skipped candidate fixtures')

    def ps(self, body):
        env = dict(os.environ, HA_CANDIDATE_DRIVER=str(DRIVER), HA_CANDIDATE_SOURCE=str(SOURCE))
        encoded = base64.b64encode((FIXTURE + body).encode('utf-16le')).decode()
        result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], env=env, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout.decode('utf-8-sig')

    def test_hidden_disabled_nested_com_full_projection_and_order(self):
        value = json.loads(self.ps(r'''
$a=MakeTask 'z' -Folder '\nested';$b=MakeTask 'A' 3 $true 5
$child=MakeFolder '\nested' @($a) @()
$root=MakeFolder '\' @($b) @($child)
$r=@(Get-HaTaskCandidateRecords $root ([Diagnostics.Stopwatch]::StartNew()))
if($r.Count -ne 2 -or $r[0].TaskName -cne 'A' -or $r[1].TaskPath -cne '\nested\'){throw 'record_lost'}
if($r[1].State -cne 'Disabled' -or $r[1].Settings.Enabled -ne $false){throw 'disabled_lost'}
if($r[0].Actions[0].ClassId -cne $b.Definition.Actions[0].ClassId){throw 'com_lost'}
if($r[1].Actions[0].Execute -cne $a.Definition.Actions[0].Path -or $r[1].Actions[0].Arguments -cne $a.Definition.Actions[0].Arguments -or $r[1].Actions[0].WorkingDirectory -cne $a.Definition.Actions[0].WorkingDirectory){throw 'exec_lost'}
if($r[0].CandidateXml -cne $b.Xml -or $r[1].CandidateXml -cne $a.Xml){throw 'xml_changed'}
@{projection_equal=$true;count=$r.Count}|ConvertTo-Json -Compress
'''))
        self.assertEqual(value, dict(projection_equal=True, count=2))

    def test_original_ast_task_loop_full_records_equal_for_exec(self):
        self.assertEqual(self.ps(r'''
$source=[IO.File]::ReadAllText($env:HA_CANDIDATE_SOURCE)
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$t,[ref]$e)
$literal='(Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName -CaseSensitive)'
$loop=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst] -and $a.Condition.Extent.Text -ceq $literal},$true))
if($loop.Count -ne 1){throw 'original_changed'}
foreach($name in @('Get-LegacyHash','Get-LegacyObjectHash','Expand-LegacyCommand','Test-LegacyCreator')){
 $f=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq $name},$true))
 . ([ScriptBlock]::Create($f[0].Extent.Text))
}
$script:config=@{legacy_root='__fixture__'}
function Assert-LegacyDeadline($Watch){}
function Test-LegacyProviderCode($Command){return $false}
function Get-LegacyCodeHash($Command){return ('a'*64)}
function Get-LegacyReferenceHashes($Arguments,$WorkingDirectory){return ($Arguments+':'+$WorkingDirectory)}
$com=MakeTask 'python-hidden' -Folder '\nested'
$com.Definition.Actions+=@([pscustomobject]@{Type=0;Path='second.exe';Arguments='SECOND';WorkingDirectory=''})
$com.Xml=$com.Xml.Replace('</Actions>','<Exec><Command>second.exe</Command><Arguments>SECOND</Arguments></Exec></Actions>')
$converted=ConvertFrom-HaTaskCandidate $com '\nested'
$reference=[pscustomobject]@{TaskName=$com.Name;TaskPath='\nested\';State='Disabled';Settings=[pscustomobject]@{Enabled=$false};Actions=@($com.Definition.Actions|ForEach-Object {[pscustomobject]@{Execute=$_.Path;Arguments=$_.Arguments;WorkingDirectory=$_.WorkingDirectory}})}
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){return $com.Xml}
$outputs=@()
foreach($inputTask in @($reference,$converted)){
 $tasks=@();$Watch=[Diagnostics.Stopwatch]::StartNew()
 $raw=@($inputTask)
 . ([ScriptBlock]::Create('foreach ($task in $raw) '+$loop[0].Body.Extent.Text))
 $outputs+=,(ConvertTo-Json -InputObject $tasks -Depth 15 -Compress)
}
if($outputs[0] -cne $outputs[1]){throw 'strict_records_differ'}
# The unmodified loop must also fail identically when XML access fails. This
# comparison never substitutes candidate XML for native Export-ScheduledTask.
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){throw 'PRIVATE_XML_DENIED'}
$errors=@()
foreach($inputTask in @($reference,$converted)){
 $tasks=@();$raw=@($inputTask)
 try{. ([ScriptBlock]::Create('foreach ($task in $raw) '+$loop[0].Body.Extent.Text));throw 'failure_lost'}
 catch{$errors+=@($_.Exception.Message)}
}
if($errors.Count -ne 2 -or $errors[0] -cne 'PRIVATE_XML_DENIED' -or $errors[0] -cne $errors[1]){throw 'xml_failure_changed'}
'equal'
''').strip(), 'equal')

    def test_partial_access_error_cycle_duplicate_and_unsupported_fail_closed(self):
        self.assertEqual(self.ps(r'''
$blocked=0
$f=MakeFolder '\' @() @();$f.Children=@($f)
try{Get-HaTaskCandidateRecords $f ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$f=MakeFolder '\' @((MakeTask),(MakeTask)) @()
try{Get-HaTaskCandidateRecords $f ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
$f=MakeFolder '\' @() @();$f|Add-Member -Force ScriptMethod GetFolders {param($flags) throw 'PRIVATE_ACCESS_DENIED'}
try{Get-HaTaskCandidateRecords $f ([Diagnostics.Stopwatch]::StartNew())|Out-Null}catch{$blocked++}
foreach($type in @(6,7)){
 try{ConvertFrom-HaTaskCandidate (MakeTask 'x' 3 $true $type) '\'|Out-Null}catch{$blocked++}
}
if($blocked -ne 5){throw 'partial_success'}
'blocked'
''').strip(), 'blocked')

    def test_deadline_before_and_after_blocking_provider_call(self):
        self.assertEqual(self.ps(r'''
$blocked=0;$w=[Diagnostics.Stopwatch]::StartNew();$w.Stop()
try{Get-HaTaskCandidateRecords (MakeFolder '\' @() @()) $w 0|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_deadline'){$blocked++}}
$f=MakeFolder '\' @() @();$f|Add-Member -Force ScriptMethod GetTasks {param($flags) Start-Sleep -Milliseconds 30;return @()}
try{Get-HaTaskCandidateRecords $f ([Diagnostics.Stopwatch]::StartNew()) 20|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_deadline'){$blocked++}}
if($blocked -ne 2){throw 'deadline_lost'}
'bounded-cooperatively'
''').strip(), 'bounded-cooperatively')

    def test_failure_report_private_output_and_authority_invariants(self):
        output = self.ps(r'''
function New-Object($ComObject){throw 'PRIVATE_ACCESS_DENIED'}
Invoke-HaTaskCandidate|ConvertTo-Json -Compress
''')
        self.assertNotIn('PRIVATE', output)
        value = json.loads(output)
        self.assertEqual((value['status'], value['reason'], value['equivalence']), ('blocked', 'query_failed', 'unknown'))
        self.assertIs(value['authority'], False)
        self.assertIs(value['ready'], False)
        self.assertIsNone(value['task_count'])
        self.assertIsNone(value['action_count'])
        self.assertEqual(value['deadline_mode'], 'cooperative-external-watchdog-required')

    def test_aggregate_metadata_and_action_limits_before_retaining_records(self):
        self.assertEqual(self.ps(r'''
$blocked=0
$a=MakeTask 'a';$b=MakeTask 'b'
foreach($task in @($a,$b)){$task.Xml=$task.Xml.Replace('</Task>',('<Data>'+('x'*(1048576-$task.Xml.Length-13))+'</Data></Task>'))}
try{Get-HaTaskCandidateRecords (MakeFolder '\' @($a,$b) @()) ([Diagnostics.Stopwatch]::StartNew()) 10000|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
# Small fields and XML share one character budget, across all actions/tasks.
$budget=@{characters=2097150;actions=0}
try{ConvertFrom-HaTaskCandidate (MakeTask) '\' ([Diagnostics.Stopwatch]::StartNew()) 2000 $budget|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
$task=MakeTask
$task.Xml=$task.Xml.Replace('</Actions>','<Exec><Command>second.exe</Command></Exec></Actions>')
$budget=@{characters=0;actions=4095}
try{ConvertFrom-HaTaskCandidate $task '\' ([Diagnostics.Stopwatch]::StartNew()) 2000 $budget|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
if($blocked -ne 3){throw 'aggregate_limit_failed'}
'bounded-metadata'
''').strip(), 'bounded-metadata')

    def test_action_deadline_and_final_action_count_cannot_report_late_success(self):
        value = json.loads(self.ps(r'''
$task=MakeTask;$task|Add-Member -Force ScriptProperty Xml {Start-Sleep -Milliseconds 30;return '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>x</Command></Exec></Actions></Task>'}
$blocked=$false
try{ConvertFrom-HaTaskCandidate $task '\' ([Diagnostics.Stopwatch]::StartNew()) 20|Out-Null}catch{$blocked=($_.Exception.Message -ceq 'candidate_deadline')}
if(-not $blocked){throw 'action_deadline_lost'}
function New-Object($ComObject){
 $s=[pscustomobject]@{}
 $s|Add-Member ScriptMethod Connect {}
 $s|Add-Member ScriptMethod GetFolder {param($path) return [pscustomobject]@{}}
 return $s
}
function Get-HaTaskCandidateRecords($Root,$Watch,$DeadlineMilliseconds){
 $r=[pscustomobject]@{}
 $r|Add-Member ScriptProperty Actions {Start-Sleep -Milliseconds 2100;return @()}
 return $r
}
Invoke-HaTaskCandidate|ConvertTo-Json -Compress
'''))
        self.assertEqual((value['status'], value['reason']), ('blocked', 'deadline_exceeded'))
        self.assertGreaterEqual(value['elapsed_ms'], 2000)
        self.assertIsNone(value['task_count'])
        self.assertIsNone(value['action_count'])
        self.assertIs(value['ready'], False)

    def test_xml_defaults_boolean_and_exact_text_without_definition_rpc(self):
        self.assertEqual(self.ps(r'''
$task=MakeTask
$script:xmlReads=0;$script:rawXml=$task.Xml
$task|Add-Member -Force ScriptProperty Xml {$script:xmlReads++;return $script:rawXml}
$task|Add-Member -Force ScriptProperty Definition {throw 'PRIVATE_DEFINITION_RPC'}
$r=ConvertFrom-HaTaskCandidate $task '\'
if($script:xmlReads -ne 1 -or $r.CandidateXml -cne $script:rawXml){throw 'rpc_or_raw_xml_changed'}
$ns='http://schemas.microsoft.com/windows/2004/02/mit/task'
foreach($boolean in @('true','false','1','0')){
 $script:rawXml='<Task xmlns="'+$ns+'"><Settings><Enabled> '+$boolean+' </Enabled></Settings><Actions><Exec><Command> a&amp;b.exe </Command><Arguments> &quot;q&quot; &#xA; </Arguments><WorkingDirectory> c&amp;d </WorkingDirectory></Exec></Actions></Task>'
 $r=ConvertFrom-HaTaskCandidate $task '\'
 if($r.Settings.Enabled -ne ($boolean -in @('true','1'))){throw 'boolean_changed'}
 if($r.Actions[0].Execute -cne ' a&b.exe ' -or $r.Actions[0].Arguments -cne (' "q" '+[char]10+' ') -or $r.Actions[0].WorkingDirectory -cne ' c&d '){throw 'text_changed'}
}
foreach($settingsXml in @('','<Settings/>')){
 $script:rawXml='<Task xmlns="'+$ns+'">'+$settingsXml+'<Actions><Exec><Command>x</Command></Exec></Actions></Task>'
 $r=ConvertFrom-HaTaskCandidate $task '\'
 if(-not $r.Settings.Enabled -or $r.Actions[0].Arguments -cne '' -or $r.Actions[0].WorkingDirectory -cne ''){throw 'xsd_default_changed'}
}
'xml-semantics-preserved'
''').strip(), 'xml-semantics-preserved')

    def test_unsafe_malformed_unknown_and_duplicate_xml_fail_closed(self):
        self.assertEqual(self.ps(r'''
$task=MakeTask;$good=$task.Xml
$bad=@(
 '<!DOCTYPE Task [<!ENTITY private SYSTEM "file:///PRIVATE_PATH">]>'+ $good.Replace('private.exe','&private;'),
 $good.Replace('http://schemas.microsoft.com/windows/2004/02/mit/task','urn:unknown'),
 $good.Replace('<Task ','<Wrong ').Replace('</Task>','</Wrong>'),
 $good.Replace('<Exec>','<Exec xmlns="urn:unknown">'),
 $good.Replace('</Settings>','<Enabled>true</Enabled></Settings>'),
 $good.Replace('<Enabled>false</Enabled>','<Enabled>yes</Enabled>'),
 $good.Replace('</Settings>','</Settings><Settings/>'),
 $good.Replace('</Arguments>','</Arguments><Arguments>duplicate</Arguments>'),
 $good.Replace('<Command>private.exe</Command>','<Command><Nested/></Command>'),
 $good.Replace('<Exec>','<Unknown>').Replace('</Exec>','</Unknown>'),
 $good.Replace('</Exec>','</Exec>nonwhitespace'),
 $good.Replace('</Actions>','</Actions><Unknown/>'),
 $good.Replace('</Task>',''),
 ('x'*1048577)
)
$blocked=0
foreach($xml in $bad){
 $task.Xml=$xml
 try{ConvertFrom-HaTaskCandidate $task '\'|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
}
if($blocked -ne $bad.Count){throw 'unsafe_xml_accepted'}
'xml-rejected'
''').strip(), 'xml-rejected')

    def test_registered_identity_must_match_folder_and_name(self):
        self.assertEqual(self.ps(r'''
$blocked=0
$task=MakeTask 'expected' -Folder '\old'
try{ConvertFrom-HaTaskCandidate $task '\other'|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
$task.Path='\old\renamed'
try{ConvertFrom-HaTaskCandidate $task '\old'|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
foreach($name in @('',' ','nested\task','nested/task','.', '..',("bad"+[char]0+'name'))){
 $task=MakeTask $name
 try{ConvertFrom-HaTaskCandidate $task '\'|Out-Null}catch{if($_.Exception.Message -ceq 'candidate_unsupported'){$blocked++}}
}
if($blocked -ne 9){throw 'identity_mismatch_accepted'}
$task=MakeTask 'Expected' -Folder '\Nested';$task.Path='\NESTED\EXPECTED'
$record=ConvertFrom-HaTaskCandidate $task '\nested'
if($record.TaskPath -cne '\nested\' -or $record.TaskName -cne 'Expected'){throw 'identity_case_changed'}
'identity-checked'
''').strip(), 'identity-checked')

    def test_cli_refuses_unbounded_execution(self):
        result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-File', str(DRIVER)], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        value = json.loads(result.stdout.decode('utf-8-sig'))
        self.assertEqual(value['reason'], 'external_watchdog_required')
        self.assertIs(value['authority'], False)
        self.assertIs(value['ready'], False)


if __name__ == '__main__':
    unittest.main()
