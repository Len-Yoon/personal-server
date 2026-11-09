"""Portable real C# / PowerShell fixtures; never Windows native acceptance."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'infra/k8s/tools/n100-ha-windows-typed-candidate.cs'
DRIVER = SOURCE.with_suffix('.ps1')
FAKES = r'''
namespace TypedFixtures {
 public static class Trace {public static System.Collections.Generic.List<string> Calls=new System.Collections.Generic.List<string>();}
 public sealed class Task : N100HaTypedCandidateV1.ITask {
  public string Name {get;set;} public string Path {get;set;} public string Text {get;set;}
  public int State {get;set;} public int Delay; public int XmlReads; public int Disposed;public bool DisposeThrows;
  public string Xml {get {XmlReads++;if(Delay>0)System.Threading.Thread.Sleep(Delay);return Text;}}
  public void Dispose(){Disposed++;if(DisposeThrows)throw new System.Exception("PRIVATE_TASK_DISPOSE");}
 }
 public sealed class Folder : N100HaTypedCandidateV1.IFolder {
  public string Path {get;set;} public int TaskDelay;public bool Denied;public int Disposed;public int DisposeDelay;public bool DisposeThrows;
  public System.Collections.Generic.List<N100HaTypedCandidateV1.ITask> Tasks=new System.Collections.Generic.List<N100HaTypedCandidateV1.ITask>();
  public System.Collections.Generic.List<N100HaTypedCandidateV1.IFolder> Children=new System.Collections.Generic.List<N100HaTypedCandidateV1.IFolder>();
  public System.Collections.Generic.IList<N100HaTypedCandidateV1.ITask> GetTasks(int flags){Trace.Calls.Add("tasks");if(flags!=1)throw new System.Exception("PRIVATE_HIDDEN");if(TaskDelay>0)System.Threading.Thread.Sleep(TaskDelay);if(Denied)throw new System.Exception("PRIVATE_ACCESS");return Tasks;}
  public System.Collections.Generic.IList<N100HaTypedCandidateV1.IFolder> GetFolders(int flags){if(flags!=0)throw new System.Exception("PRIVATE_FLAGS");return Children;}
  public void Dispose(){Disposed++;if(DisposeDelay>0)System.Threading.Thread.Sleep(DisposeDelay);if(DisposeThrows)throw new System.Exception("PRIVATE_FOLDER_DISPOSE");}
 }
 public sealed class ComCollection {
  public int CountDelay;public int ItemDelay;public int ItemCalls;public object Value;
  public int Count {get{if(CountDelay>0)System.Threading.Thread.Sleep(CountDelay);return 2;}}
  public object this[int index] {get{ItemCalls++;if(ItemDelay>0)System.Threading.Thread.Sleep(ItemDelay);return Value;}}
 }
 public sealed class ComFolder {
  public string Path {get{return "\\";}}public ComCollection Tasks;
  public object GetTasks(int flags){if(flags!=1)throw new System.Exception("PRIVATE_FLAGS");return Tasks;}
  public object GetFolders(int flags){throw new System.Exception("PRIVATE_UNREACHED");}
 }
 public sealed class WmiRow : N100HaTypedCandidateV1.IWmiRow {
  public System.Collections.Generic.Dictionary<string,N100HaTypedCandidateV1.WmiProperty> Values=new System.Collections.Generic.Dictionary<string,N100HaTypedCandidateV1.WmiProperty>();
  public int Disposed;public int FieldDelay;public string ThrowField;public bool DisposeThrows;
  public N100HaTypedCandidateV1.WmiProperty Read(string name){if(FieldDelay>0)System.Threading.Thread.Sleep(FieldDelay);if(name==ThrowField)throw new System.Exception("PRIVATE_FIELD_DENIED");return Values[name];}
  public void Dispose(){Disposed++;if(DisposeThrows)throw new System.Exception("PRIVATE_ROW_DISPOSE");}
 }
 public sealed class WmiRows : N100HaTypedCandidateV1.IWmiRows {
  public System.Collections.Generic.List<WmiRow> Values=new System.Collections.Generic.List<WmiRow>();
  public int MoveDelay;public int ThrowAfter=-1;public int Disposed;public int IteratorDisposed;public int MoveCalls;public bool IteratorDisposeThrows;public bool DisposeThrows;
  public int Count {get{throw new System.Exception("PRIVATE_COUNT_FORBIDDEN");}}
  sealed class Iterator : System.Collections.Generic.IEnumerator<N100HaTypedCandidateV1.IWmiRow> {
   readonly WmiRows rows;int index=-1;public Iterator(WmiRows owner){rows=owner;}
   public N100HaTypedCandidateV1.IWmiRow Current {get{return rows.Values[index];}}
   object System.Collections.IEnumerator.Current {get{return Current;}}
   public bool MoveNext(){rows.MoveCalls++;if(rows.MoveDelay>0)System.Threading.Thread.Sleep(rows.MoveDelay);index++;if(index==rows.ThrowAfter)throw new System.Exception("PRIVATE_PARTIAL");return index<rows.Values.Count;}
   public void Reset(){throw new System.Exception("PRIVATE_RESET_FORBIDDEN");}
   public void Dispose(){rows.IteratorDisposed++;if(rows.IteratorDisposeThrows)throw new System.Exception("PRIVATE_ITERATOR_DISPOSE");}
  }
  public System.Collections.Generic.IEnumerator<N100HaTypedCandidateV1.IWmiRow> GetEnumerator(){return new Iterator(this);}
  System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator(){return GetEnumerator();}
  public void Dispose(){Disposed++;if(DisposeThrows)throw new System.Exception("PRIVATE_ROWS_DISPOSE");}
 }
 public sealed class WmiReader : N100HaTypedCandidateV1.IWmiReader {
  public WmiRows Services=new WmiRows();public WmiRows Processes=new WmiRows();public WmiRows Boot=new WmiRows();public int Disposed;
  public N100HaTypedCandidateV1.IWmiRows Query(N100HaTypedCandidateV1.WmiKind kind){Trace.Calls.Add(kind.ToString());switch(kind){case N100HaTypedCandidateV1.WmiKind.Services:return Services;case N100HaTypedCandidateV1.WmiKind.Processes:return Processes;case N100HaTypedCandidateV1.WmiKind.Boot:return Boot;default:throw new System.Exception("PRIVATE_KIND");}}
  public void Dispose(){Disposed++;}
 }
 public sealed class ComWmiProperty {public object Value {get;set;}public int CIMType {get;set;}public bool IsArray {get;set;}}
 public sealed class ComWmiProperties {
  public System.Collections.Generic.Dictionary<string,ComWmiProperty> Values=new System.Collections.Generic.Dictionary<string,ComWmiProperty>();
  public ComWmiProperty this[string name]{get{return Values[name];}}
 }
 public sealed class ComWmiRow {public ComWmiProperties Properties_ {get;set;}}
 public sealed class ComWmiRows : System.Collections.IEnumerable {
  public object Row;public int MoveDelay;public int Disposed;
  public int Count {get{throw new System.Exception("PRIVATE_NATIVE_COUNT_FORBIDDEN");}}
  sealed class Iterator : System.Collections.IEnumerator,System.IDisposable {
   readonly ComWmiRows owner;int index=-1;public Iterator(ComWmiRows rows){owner=rows;}
   public object Current{get{return owner.Row;}}
   public bool MoveNext(){if(owner.MoveDelay>0)System.Threading.Thread.Sleep(owner.MoveDelay);index++;return index==0;}
   public void Reset(){throw new System.Exception("PRIVATE_NATIVE_RESET");}
   public void Dispose(){owner.Disposed++;}
  }
  public System.Collections.IEnumerator GetEnumerator(){return new Iterator(this);}
 }
 public sealed class ComWmiService {
  public ComWmiRows Rows;public string LastQuery;public string LastLanguage;public int LastFlags;
  public object ExecQuery(string query,string language,int flags){LastQuery=query;LastLanguage=language;LastFlags=flags;return Rows;}
 }
}
'''
PREFIX = r'''
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$source=[IO.File]::ReadAllText($env:HA_TYPED_SOURCE)
Add-Type -TypeDefinition ($source+$env:HA_TYPED_FAKES) -ErrorAction Stop
function MakeTask($Name='PRIVATE_TASK',$Folder='\',$State=1){
 $task=[TypedFixtures.Task]::new();$task.Name=$Name;$task.Path=$Folder.TrimEnd('\')+'\'+$Name;$task.State=$State
 $task.Text='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers/><Principals/><Settings><Enabled>false</Enabled></Settings><Actions><Exec><Command>private.exe</Command><Arguments>PRIVATE_ARGUMENT</Arguments><WorkingDirectory>PRIVATE_DIRECTORY</WorkingDirectory></Exec></Actions></Task>'
 return $task
}
function Project($Task,$Folder='\') {return [N100HaTypedCandidateV1.Collector]::Project($Task,$Folder,[N100HaTypedCandidateV1.Budget]::new(2000))}
function Reason($Call){try{& $Call|Out-Null;return 'accepted'}catch{if($_.Exception.InnerException -is [N100HaTypedCandidateV1.Blocked]){return $_.Exception.InnerException.Reason};throw}}
function MakeWmiRow($Kind){
 $row=[TypedFixtures.WmiRow]::new()
 switch($Kind){
  'service' {foreach($name in @('Name','PathName','StartName','StartMode','ServiceType','State')){$row.Values.Add($name,[N100HaTypedCandidateV1.WmiProperty]::new('PRIVATE_'+$name,8,$false))};$row.Values.Add('ProcessId',[N100HaTypedCandidateV1.WmiProperty]::new([uint32]123,19,$false))}
  'process' {foreach($name in @('ProcessId','ParentProcessId','SessionId')){$row.Values.Add($name,[N100HaTypedCandidateV1.WmiProperty]::new([uint32]123,19,$false))};foreach($name in @('Name','CommandLine','ExecutablePath')){$row.Values.Add($name,[N100HaTypedCandidateV1.WmiProperty]::new('PRIVATE_'+$name,8,$false))};$row.Values.Add('CreationDate',[N100HaTypedCandidateV1.WmiProperty]::new('20261109123045.123456+540',101,$false))}
  'boot' {$row.Values.Add('LastBootUpTime',[N100HaTypedCandidateV1.WmiProperty]::new('20261109123045.123456+540',101,$false))}
 }
 return $row
}
function MakeBasic {
 $reader=[TypedFixtures.WmiReader]::new();$reader.Services.Values.Add((MakeWmiRow service));$reader.Processes.Values.Add((MakeWmiRow process));$reader.Boot.Values.Add((MakeWmiRow boot))
 $folder=[TypedFixtures.Folder]::new();$folder.Path='\';$folder.Tasks.Add((MakeTask))
 return @{reader=$reader;folder=$folder}
}
'''


class WindowsTypedCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        if not Path(cls.pwsh).is_file():
            raise AssertionError('PowerShell required; no skipped typed fixtures')

    def ps(self, body, prefix=PREFIX):
        env = dict(os.environ, HA_TYPED_SOURCE=str(SOURCE), HA_TYPED_DRIVER=str(DRIVER),
                   HA_TYPED_FAKES=FAKES, HA_TYPED_ROOT=str(ROOT))
        encoded = base64.b64encode((prefix + body).encode('utf-16le')).decode()
        result = subprocess.run([self.pwsh, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout.decode('utf-8-sig')

    def test_full_projection_matches_powershell_xml_candidate(self):
        output = self.ps(r'''
. (Join-Path $env:HA_TYPED_ROOT 'infra/k8s/tools/n100-ha-windows-task-candidate.ps1')
$task=MakeTask 'hidden-disabled' '\nested'
$task.Text=$task.Text.Replace('</Actions>','<ComHandler><ClassId>PRIVATE_CLSID</ClassId><Data>opaque</Data></ComHandler><Exec><Command>second.exe</Command></Exec></Actions>')
$a=Project $task '\nested';$b=ConvertFrom-HaTaskCandidate $task '\nested'
function Canonical($record){return [ordered]@{name=$record.TaskName;path=$record.TaskPath;state=$record.State;enabled=$record.Settings.Enabled;xml=$record.CandidateXml;actions=@($record.Actions|ForEach-Object {[ordered]@{execute=$(if($_.PSObject.Properties['Execute']){$_.Execute}else{$null});arguments=$(if($_.PSObject.Properties['Arguments']){$_.Arguments}else{$null});directory=$(if($_.PSObject.Properties['WorkingDirectory']){$_.WorkingDirectory}else{$null});class=$(if($_.PSObject.Properties['ClassId']){$_.ClassId}else{$null})}})}}
if((Canonical $a|ConvertTo-Json -Depth 10 -Compress) -cne (Canonical $b|ConvertTo-Json -Depth 10 -Compress)){throw 'projection_changed'}
if($a.CandidateXml -cne $task.Text -or $task.XmlReads -ne 2){throw 'raw_or_single_read_changed'}
'equal'
''')
        self.assertEqual(output.strip(), 'equal')

    def test_original_ast_full_exec_records_and_export_failure_equal(self):
        self.assertEqual(self.ps(r'''
$source=[IO.File]::ReadAllText((Join-Path $env:HA_TYPED_ROOT 'infra/k8s/ha_control/windows-legacy-guard.ps1'))
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$t,[ref]$e)
$loop=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.ForEachStatementAst] -and $a.Condition.Extent.Text -ceq '(Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName -CaseSensitive)'},$true))[0]
foreach($name in @('Get-LegacyHash','Get-LegacyObjectHash','Expand-LegacyCommand','Test-LegacyCreator')){
 $f=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq $name},$true))[0]
 . ([ScriptBlock]::Create($f.Extent.Text))
}
$script:config=@{legacy_root='__fixture__'}
function Assert-LegacyDeadline($Watch){}
function Test-LegacyProviderCode($Command){return $false}
function Get-LegacyCodeHash($Command){return ('a'*64)}
function Get-LegacyReferenceHashes($Arguments,$WorkingDirectory){return ($Arguments+':'+$WorkingDirectory)}
$inputTask=MakeTask 'python-hidden' '\nested'
$inputTask.Text=$inputTask.Text.Replace('</Actions>','<Exec><Command>second.exe</Command></Exec></Actions>')
$typed=Project $inputTask '\nested'
$reference=[pscustomobject]@{TaskName='python-hidden';TaskPath='\nested\';State='Disabled';Settings=@{Enabled=$false};Actions=@([pscustomobject]@{Execute='private.exe';Arguments='PRIVATE_ARGUMENT';WorkingDirectory='PRIVATE_DIRECTORY'},[pscustomobject]@{Execute='second.exe';Arguments='';WorkingDirectory=''})}
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){return $inputTask.Text}
$outputs=@()
foreach($task in @($typed,$reference)){$tasks=@();$raw=@($task);$Watch=[Diagnostics.Stopwatch]::StartNew();. ([ScriptBlock]::Create('foreach ($task in $raw) '+$loop.Body.Extent.Text));$outputs+=,(ConvertTo-Json -InputObject $tasks -Depth 20 -Compress)}
if($outputs[0] -cne $outputs[1]){throw 'strict_records_changed'}
function Export-ScheduledTask($TaskName,$TaskPath,$ErrorAction){throw 'PRIVATE_EXPORT_FAILED'}
$blocked=0
foreach($task in @($typed,$reference)){try{$raw=@($task);. ([ScriptBlock]::Create('foreach ($task in $raw) '+$loop.Body.Extent.Text))}catch{if($_.Exception.Message -ceq 'PRIVATE_EXPORT_FAILED'){$blocked++}}}
if($blocked -ne 2){throw 'original_failure_changed'}
'equal'
''').strip(), 'equal')

    def test_traversal_hidden_nested_disabled_order_identity_and_cleanup(self):
        self.assertEqual(self.ps(r'''
$root=[TypedFixtures.Folder]::new();$root.Path='\'
$child=[TypedFixtures.Folder]::new();$child.Path='\nested';$root.Children.Add($child)
$a=MakeTask 'z' '\nested';$child.Tasks.Add($a)
foreach($name in @('b','A','é','_','-')){$root.Tasks.Add((MakeTask $name))}
$records=[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))
$expected=@($records|Sort-Object TaskPath,TaskName -CaseSensitive)
if(($records.TaskName -join ',') -cne ($expected.TaskName -join ',') -or $records.Count -ne 6){throw 'order_or_count_changed'}
if($a.Disposed -ne 1 -or $root.Disposed -ne 1 -or $child.Disposed -ne 1){throw 'cleanup_lost'}
if($records[-1].Settings.Enabled -or $records[-1].State -cne 'Disabled'){throw 'disabled_changed'}
$task=MakeTask;$task.Path='\renamed'
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'identity_changed'}
$task=MakeTask 'bad/name'
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'name_changed'}
'traversed'
''').strip(), 'traversed')

    def test_duplicate_cycle_permission_and_deadline_fail_closed(self):
        self.assertEqual(self.ps(r'''
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.Children.Add($root)
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'cycle_accepted'}
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.Tasks.Add((MakeTask));$root.Tasks.Add((MakeTask))
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'duplicate_accepted'}
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.Denied=$true;$failed=$false
try{[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))|Out-Null}catch{$failed=$true}
if(-not $failed -or $root.Disposed -ne 1){throw 'permission_or_cleanup_lost'}
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.TaskDelay=30
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(20))}) -cne 'deadline_exceeded'){throw 'query_deadline_lost'}
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.DisposeDelay=30
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(20))}) -cne 'deadline_exceeded'){throw 'cleanup_deadline_lost'}
$task=MakeTask;$task.Delay=30
if((Reason {[N100HaTypedCandidateV1.Collector]::Project($task,'\',[N100HaTypedCandidateV1.Budget]::new(20))}) -cne 'deadline_exceeded'){throw 'xml_deadline_lost'}
'blocked'
''').strip(), 'blocked')

    def test_cleanup_failure_drains_all_owned_handles_and_com_item_budget(self):
        self.assertEqual(self.ps(r'''
$root=[TypedFixtures.Folder]::new();$root.Path='\';$root.DisposeThrows=$true
$a=[TypedFixtures.Folder]::new();$a.Path='\a';$a.DisposeThrows=$true
$b=[TypedFixtures.Folder]::new();$b.Path='\b';$root.Children.Add($a);$root.Children.Add($b)
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'query_failed' -or $a.Disposed -ne 1 -or $b.Disposed -ne 1){throw 'pending_cleanup_stranded'}
$root=[TypedFixtures.Folder]::new();$root.Path='\';$a=MakeTask 'a';$b=MakeTask 'b';$a.DisposeThrows=$true;$root.Tasks.Add($a);$root.Tasks.Add($b)
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'query_failed' -or $a.Disposed -ne 1 -or $b.Disposed -ne 1 -or $root.Disposed -ne 1){throw 'task_cleanup_stranded'}
# Exercise the real native adapter's reflection boundary with local managed
# stand-ins: a late Count/Item must stop before retrieving the next item.
$nativeType=[N100HaTypedCandidateV1.Collector].GetNestedType('NativeFolder',[Reflection.BindingFlags]::NonPublic)
foreach($phase in @('count','item')){
 $collection=[TypedFixtures.ComCollection]::new();$collection.Value=MakeTask
 if($phase -ceq 'count'){$collection.CountDelay=30}else{$collection.ItemDelay=30}
 $com=[TypedFixtures.ComFolder]::new();$com.Tasks=$collection
 $budget=[N100HaTypedCandidateV1.Budget]::new(20)
 $adapter=[Activator]::CreateInstance($nativeType,[Reflection.BindingFlags]'Instance,Public,NonPublic',$null,@($com,$budget),$null)
 if((Reason {$adapter.GetTasks(1)}) -cne 'deadline_exceeded'){throw 'com_budget_lost'}
 if($collection.ItemCalls -ne $(if($phase -ceq 'count'){0}else{1})){throw 'late_com_items_read'}
}
'cleanup-bounded'
''').strip(), 'cleanup-bounded')

    def test_defaults_text_and_unsafe_xml_equivalence(self):
        self.assertEqual(self.ps(r'''
$task=MakeTask;$good=$task.Text
foreach($value in @('true','false','1','0')){
 $task.Text=$good.Replace('<Enabled>false</Enabled>','<Enabled> '+$value+' </Enabled>').Replace('PRIVATE_ARGUMENT',' &quot;q&quot; &#xA; &amp; ')
 $r=Project $task
 if($r.Settings.Enabled -ne ($value -in @('true','1')) -or $r.Actions[0].Arguments -cne (' "q" '+[char]10+' & ')){throw 'xml_meaning_changed'}
}
$task.Text=$good.Replace('<Settings><Enabled>false</Enabled></Settings>','')
if(-not (Project $task).Settings.Enabled){throw 'default_changed'}
$bads=@('<!DOCTYPE Task [<!ENTITY e SYSTEM "file:///PRIVATE">]>'+$good.Replace('private.exe','&e;'),$good.Replace('http://schemas.microsoft.com/windows/2004/02/mit/task','urn:unknown'),$good.Replace('</Actions>','<Unknown/></Actions>'),$good.Replace('</Arguments>','</Arguments><Arguments>x</Arguments>'),$good.Replace('</Settings>','<Enabled>true</Enabled></Settings>'),$good.Replace('false</Enabled>','yes</Enabled>'),$good.Replace('<Command>private.exe</Command>','<Command><Nested/></Command>'),$good.Replace('</Task>',''),$good.Replace('</Task>','<![CDATA[bad]]></Task>'),$good.Replace('</Actions>','<![CDATA[bad]]></Actions>'),$good.Replace('</Exec>','<![CDATA[bad]]></Exec>'))
foreach($xml in $bads){$task.Text=$xml;if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'unsafe_xml_accepted'}}
'xml-preserved'
''').strip(), 'xml-preserved')

    def test_cumulative_character_action_node_and_output_limits(self):
        self.assertEqual(self.ps(r'''
$task=MakeTask;$task.Text='x'*1048577
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'xml_cap_lost'}
$root=[TypedFixtures.Folder]::new();$root.Path='\'
foreach($name in @('a','b')){$task=MakeTask $name;$task.Text=$task.Text.Replace('</Task>',('<Data>'+('x'*(1048576-$task.Text.Length-13))+'</Data></Task>'));$root.Tasks.Add($task)}
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadAll($root,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'aggregate_cap_lost'}
$task=MakeTask;$task.Text=$task.Text.Replace('</Actions>',(('<Exec><Command>x</Command></Exec>'*1024)+'</Actions>'))
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'action_cap_lost'}
$task=MakeTask;$budget=[N100HaTypedCandidateV1.Budget]::new(2000);$budget.Actions=4096
if((Reason {[N100HaTypedCandidateV1.Collector]::Project($task,'\',$budget)}) -cne 'unsupported_metadata'){throw 'aggregate_actions_lost'}
$task=MakeTask;$task.Text=$task.Text.Replace('<Triggers/>',('<Triggers>'+('<Node/>'*32768)+'</Triggers>'))
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'node_cap_lost'}
'bounded'
''').strip(), 'bounded')

    def test_every_namespace_position_and_com_cdata_match_reference_rejection(self):
        self.assertEqual(self.ps(r'''
. (Join-Path $env:HA_TYPED_ROOT 'infra/k8s/tools/n100-ha-windows-task-candidate.ps1')
$task=MakeTask;$task.Text=$task.Text.Replace('<Triggers/>',('<Triggers>'+('<Node/>'*240)+'</Triggers>'))
$original=$task.Text;$document=[Xml.XmlDocument]::new();$document.LoadXml($original);$count=$document.GetElementsByTagName('*').Count
foreach($index in 0..($count-1)){
 $document=[Xml.XmlDocument]::new();$document.LoadXml($original)
 $node=$document.GetElementsByTagName('*')[$index];$replacement=$document.CreateElement($node.LocalName,'urn:unknown')
 while($node.HasChildNodes){[void]$replacement.AppendChild($node.FirstChild)}
 [void]$node.ParentNode.ReplaceChild($replacement,$node);$task.Text=$document.OuterXml
 if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'typed_namespace_changed'}
 $blocked=$false;try{ConvertFrom-HaTaskCandidate $task '\'|Out-Null}catch{$blocked=($_.Exception.Message -ceq 'candidate_unsupported')}
 if(-not $blocked){throw 'reference_namespace_changed'}
}
$task.Text=$original.Replace('<Exec>','<ComHandler>').Replace('</Exec>','<![CDATA[bad]]></ComHandler>')
if((Reason {Project $task}) -cne 'unsupported_metadata'){throw 'com_cdata_accepted'}
'namespace-equal'
''').strip(), 'namespace-equal')

    def test_basic_raw_fields_nulls_types_and_fixed_sequence(self):
        self.assertEqual(self.ps(r'''
$fixture=MakeBasic
$fixture.reader.Services.Values[0].Values['PathName']=[N100HaTypedCandidateV1.WmiProperty]::new([DBNull]::Value,8,$false)
$fixture.reader.Processes.Values[0].Values['CommandLine']=[N100HaTypedCandidateV1.WmiProperty]::new($null,8,$false)
$fixture.reader.Processes.Values[0].Values['ProcessId']=[N100HaTypedCandidateV1.WmiProperty]::new([int]-1,19,$false)
$fixture.reader.Processes.Values[0].Values['SessionId']=[N100HaTypedCandidateV1.WmiProperty]::new([DBNull]::Value,19,$false)
[TypedFixtures.Trace]::Calls.Clear()
$raw=[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))
if(([TypedFixtures.Trace]::Calls -join ',') -cne 'Services,tasks,Processes,Boot'){throw 'query_sequence_changed'}
if($raw.Services.Count -ne 1 -or $raw.Tasks.Count -ne 1 -or $raw.Processes.Count -ne 1){throw 'rows_missing'}
foreach($name in @('Name','StartName','StartMode','ServiceType','State')){if($raw.Services[0].$name -cne $fixture.reader.Services.Values[0].Values[$name].Value){throw 'service_field_changed'}}
if($null -ne $raw.Services[0].PathName -or $raw.Services[0].ProcessId -ne [uint32]123){throw 'service_null_changed'}
foreach($name in @('Name','ExecutablePath')){if($raw.Processes[0].$name -cne $fixture.reader.Processes.Values[0].Values[$name].Value){throw 'process_field_changed'}}
if($raw.Processes[0].ProcessId -ne [uint32]::MaxValue -or $raw.Processes[0].ParentProcessId -ne [uint32]123 -or $null -ne $raw.Processes[0].SessionId -or $null -ne $raw.Processes[0].CommandLine){throw 'uint_or_null_coerced'}
$expected=([datetime]'2026-11-09T03:30:45Z').ToUniversalTime().Ticks+1234560
if($raw.Processes[0].CreationDateUtcTicks -ne $expected -or $raw.Boot.UtcTicks -ne $expected -or $raw.Processes[0].CreationDate.Kind -ne [DateTimeKind]::Utc){throw 'utc_ticks_changed'}
foreach($rows in @($fixture.reader.Services,$fixture.reader.Processes,$fixture.reader.Boot)){if($rows.Disposed -ne 1 -or $rows.IteratorDisposed -ne 1 -or $rows.Values[0].Disposed -ne 1){throw 'wmi_cleanup_lost'}}
'raw-equivalent'
''').strip(), 'raw-equivalent')

    def test_wmi_bad_nullable_types_dates_and_boot_cardinality_reject(self):
        self.assertEqual(self.ps(r'''
foreach($value in @('1',[double]1,[int64]1)){
 $fixture=MakeBasic;$fixture.reader.Services.Values[0].Values['ProcessId']=[N100HaTypedCandidateV1.WmiProperty]::new($value,19,$false)
 if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'uint_coerced'}
}
foreach($property in @([N100HaTypedCandidateV1.WmiProperty]::new($null,3,$false),[N100HaTypedCandidateV1.WmiProperty]::new($null,8,$true),[N100HaTypedCandidateV1.WmiProperty]::new([int]1,8,$false))){
 $fixture=MakeBasic;$fixture.reader.Services.Values[0].Values['Name']=$property
 if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'nullable_type_lost'}
}
foreach($text in @('20261109123045.123456+***','20261109123045.******+000','20261109123045.123456:000','20260230123045.123456+000','00010101000000.000000+001','99991231235959.999999-001')){
 $property=[N100HaTypedCandidateV1.WmiProperty]::new($text,101,$false)
 if((Reason {[N100HaTypedCandidateV1.Collector]::DmtfUtc($property,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'inexact_date_accepted'}
}
foreach($offset in @('+000','+540','-210','+999')){
 $property=[N100HaTypedCandidateV1.WmiProperty]::new(('20261109123045.123456'+$offset),101,$false)
 $actual=[N100HaTypedCandidateV1.Collector]::DmtfUtc($property,[N100HaTypedCandidateV1.Budget]::new(2000))
 $expected=([datetime]'2026-11-09T12:30:45Z').ToUniversalTime().Ticks+1234560-[int]$offset*[TimeSpan]::TicksPerMinute
 if($actual.Ticks -ne $expected){throw 'offset_changed'}
}
$fixture=MakeBasic;$fixture.reader.Processes.Values[0].Values['CreationDate']=[N100HaTypedCandidateV1.WmiProperty]::new([DBNull]::Value,101,$false)
$raw=[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))
if($null -ne $raw.Processes[0].CreationDate -or $null -ne $raw.Processes[0].CreationDateUtcTicks){throw 'null_date_coerced'}
foreach($mode in @('empty','duplicate','null')){
 $fixture=MakeBasic
 switch($mode){'empty'{$fixture.reader.Boot.Values.Clear()};'duplicate'{$fixture.reader.Boot.Values.Add((MakeWmiRow boot))};'null'{$fixture.reader.Boot.Values[0].Values['LastBootUpTime']=[N100HaTypedCandidateV1.WmiProperty]::new($null,101,$false)}}
 if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'boot_incomplete_accepted'}
}
'invalid-values-blocked'
''').strip(), 'invalid-values-blocked')

    def test_wmi_partial_late_field_cleanup_and_shared_budget(self):
        self.assertEqual(self.ps(r'''
foreach($phase in @('partial','missing','denied','move','field','cleanup')){
 $fixture=MakeBasic;$limit=2000;$reason='query_failed'
 switch($phase){
  'partial'{$fixture.reader.Services.ThrowAfter=1}
  'missing'{[void]$fixture.reader.Services.Values[0].Values.Remove('PathName')}
  'denied'{$fixture.reader.Services.Values[0].ThrowField='StartName'}
  'move'{$fixture.reader.Services.MoveDelay=30;$limit=20;$reason='deadline_exceeded'}
  'field'{$fixture.reader.Services.Values[0].FieldDelay=30;$limit=20;$reason='deadline_exceeded'}
  'cleanup'{$fixture.reader.Services.IteratorDisposeThrows=$true}
 }
 [TypedFixtures.Trace]::Calls.Clear()
 if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new($limit))}) -cne $reason){throw 'partial_or_late_accepted'}
 if($fixture.reader.Services.Disposed -ne 1 -or $fixture.reader.Services.IteratorDisposed -ne 1 -or $fixture.folder.Disposed -ne 1){throw 'failure_cleanup_lost'}
 if(([TypedFixtures.Trace]::Calls -join ',') -cne 'Services'){throw 'continued_after_failure'}
}
# The same 20ms budget spans services and tasks, not a new allowance per phase.
$fixture=MakeBasic;$fixture.reader.Services.MoveDelay=5;$fixture.folder.TaskDelay=15
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(20))}) -cne 'deadline_exceeded'){throw 'phase_budget_reset'}
'wmi-fail-closed'
''').strip(), 'wmi-fail-closed')

    def test_wmi_row_and_shared_string_caps(self):
        self.assertEqual(self.ps(r'''
$fixture=MakeBasic;$row=$fixture.reader.Services.Values[0]
for($index=1;$index -lt 4097;$index++){$fixture.reader.Services.Values.Add($row)}
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'row_cap_lost'}
$fixture=MakeBasic;$fixture.reader.Services.Values[0].Values['Name']=[N100HaTypedCandidateV1.WmiProperty]::new(('x'*65537),8,$false)
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,[N100HaTypedCandidateV1.Budget]::new(2000))}) -cne 'unsupported_metadata'){throw 'field_cap_lost'}
$fixture=MakeBasic;$budget=[N100HaTypedCandidateV1.Budget]::new(2000);$budget.Text(('x'*2097100),2097152)
if((Reason {[N100HaTypedCandidateV1.Collector]::ReadBasic($fixture.reader,$fixture.folder,$budget)}) -cne 'unsupported_metadata'){throw 'shared_string_cap_lost'}
'wmi-bounded'
''').strip(), 'wmi-bounded')

    def test_native_wmi_adapter_fixed_queries_fields_and_late_iterator(self):
        self.assertEqual(self.ps(r'''
$nativeType=[N100HaTypedCandidateV1.Collector].GetNestedType('NativeWmiReader',[Reflection.BindingFlags]::NonPublic)
foreach($kind in @('Services','Processes','Boot')){
 $kindValue=[Enum]::Parse([N100HaTypedCandidateV1.WmiKind],$kind)
 $fixture=MakeWmiRow $(switch($kind){'Services'{'service'};'Processes'{'process'};'Boot'{'boot'}})
 $row=[TypedFixtures.ComWmiRow]::new();$row.Properties_=[TypedFixtures.ComWmiProperties]::new()
 foreach($name in $fixture.Values.Keys){$value=$fixture.Values[$name];$p=[TypedFixtures.ComWmiProperty]::new();$p.Value=$value.Value;$p.CIMType=$value.CimType;$p.IsArray=$false;$row.Properties_.Values.Add($name,$p)}
 $rows=[TypedFixtures.ComWmiRows]::new();$rows.Row=$row
 $service=[TypedFixtures.ComWmiService]::new();$service.Rows=$rows
 $native=[Runtime.Serialization.FormatterServices]::GetUninitializedObject($nativeType)
 $nativeType.GetField('budget',[Reflection.BindingFlags]'Instance,NonPublic').SetValue($native,[N100HaTypedCandidateV1.Budget]::new(2000))
 $nativeType.GetField('service',[Reflection.BindingFlags]'Instance,NonPublic').SetValue($native,$service)
 $result=$native.Query($kindValue);$iterator=$result.GetEnumerator()
 if(-not $iterator.MoveNext()){throw 'row_missing'}
 foreach($name in $fixture.Values.Keys){$p=$iterator.Current.Read($name);if($p.CimType -ne $fixture.Values[$name].CimType -or $p.Value -cne $fixture.Values[$name].Value){throw 'native_field_changed'}}
 $iterator.Dispose();$result.Dispose()
 $expected=switch($kind){'Services'{'SELECT Name,PathName,StartName,StartMode,ServiceType,State,ProcessId FROM Win32_Service'};'Processes'{'SELECT ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath,CreationDate,SessionId FROM Win32_Process'};'Boot'{'SELECT LastBootUpTime FROM Win32_OperatingSystem'}}
 if($service.LastQuery -cne $expected -or $service.LastLanguage -cne 'WQL' -or $service.LastFlags -ne 48 -or $rows.Disposed -ne 1){throw 'fixed_query_or_forward_cleanup_changed'}
}
# Real private adapter MoveNext checks and releases even a late successful row.
$rows.MoveDelay=30
$nativeType.GetField('budget',[Reflection.BindingFlags]'Instance,NonPublic').SetValue($native,[N100HaTypedCandidateV1.Budget]::new(20))
$result=$native.Query([N100HaTypedCandidateV1.WmiKind]::Boot);$iterator=$result.GetEnumerator()
if((Reason {$iterator.MoveNext()}) -cne 'deadline_exceeded'){throw 'native_late_row_accepted'}
$iterator.Dispose();$result.Dispose()
if($rows.Disposed -ne 2){throw 'late_iterator_not_disposed'}
'fixed-native-boundary'
''').strip(), 'fixed-native-boundary')

    def test_basic_bridge_scope_is_not_a_complete_observation(self):
        prefix = r'''
$ErrorActionPreference='Stop'
. $env:HA_TYPED_DRIVER
$source=[IO.File]::ReadAllText($env:HA_TYPED_SOURCE)
$sha=[Security.Cryptography.SHA256]::Create()
try{$pin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
'''
        value = json.loads(self.ps(r'''
$invalid=Invoke-HaWindowsTypedCandidate $source $pin 'PRIVATE_SCOPE'
$basic=Invoke-HaWindowsTypedCandidate $source $pin 'basic'
@{invalid=$invalid;basic=$basic}|ConvertTo-Json -Depth 8 -Compress
''', prefix=prefix))
        self.assertEqual(value['invalid']['reason'], 'input_rejected')
        basic = value['basic']
        self.assertEqual((basic['kind'], basic['scope'], basic['reason']),
                         ('ha-windows-typed-basic-candidate', 'local-basic-enumeration-only', 'native_windows_required'))
        self.assertIs(basic['authority'], False)
        self.assertIs(basic['ready'], False)
        self.assertEqual(basic['equivalence'], 'unknown')
        for key in ('service_count', 'process_count', 'boot_count', 'task_count', 'action_count'):
            self.assertIsNone(basic[key])

    def test_bridge_pin_collision_no_native_claim_and_private_output(self):
        prefix = r'''
$ErrorActionPreference='Stop'
. $env:HA_TYPED_DRIVER
$source=[IO.File]::ReadAllText($env:HA_TYPED_SOURCE)
$sha=[Security.Cryptography.SHA256]::Create()
try{$pin=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($source))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
'''
        output = self.ps(r'''
$bad=Invoke-HaWindowsTypedCandidate ($source+'PRIVATE_SOURCE') $pin
function Add-Type {throw 'PRIVATE_SHADOWED_COMPILER'}
$first=Invoke-HaWindowsTypedCandidate $source $pin
$second=Invoke-HaWindowsTypedCandidate $source $pin
@{bad=$bad;first=$first;second=$second}|ConvertTo-Json -Depth 8 -Compress
''', prefix=prefix)
        self.assertNotIn('PRIVATE', output)
        value = json.loads(output)
        self.assertEqual(value['bad']['reason'], 'source_rejected')
        self.assertEqual(value['first']['reason'], 'native_windows_required')
        self.assertEqual(value['second']['reason'], 'type_collision')
        for report in value.values():
            self.assertIs(report['authority'], False)
            self.assertIs(report['ready'], False)
            self.assertEqual(report['equivalence'], 'unknown')
            self.assertIsNone(report['task_count'])
            self.assertIsNone(report['action_count'])
            self.assertEqual(report['deadline_ms'], 2000)
        self.assertGreaterEqual(value['first']['request_elapsed_ms'], value['first']['preparation_elapsed_ms'])
        self.assertEqual(value['first']['source_sha256'], hashlib.sha256(SOURCE.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
