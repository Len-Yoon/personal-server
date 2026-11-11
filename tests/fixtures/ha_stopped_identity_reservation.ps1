param([Parameter(Mandatory=$true)][string]$SourceRoot)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$files=@('StrictJson.cs','StoppedService.cs','PrivateIo.cs','ProductServiceSecurity.cs');$texts=@($files|ForEach-Object{[regex]::Replace([IO.File]::ReadAllText((Join-Path $SourceRoot $_)),'using [A-Za-z0-9_.]+;','')});$using='using System;using System.IO;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;using System.Security.AccessControl;using System.Security.Cryptography;using Microsoft.Win32.SafeHandles;'
$fake=@'

public sealed class FakeScm:TrustedStoppedV1.IApi {
 public bool Present;public string FailAt;public List<string> Calls=new List<string>();
 void Step(string name){Calls.Add(name);if(name==FailAt)throw new TrustedStoppedV1.Failure(999);}
 public bool Exists(string name){Step("exists");return Present;}
 public void Create(string name,string image){Step("create");}
 public void Seal(string name){Step("seal");}public void Sid(){Step("sid");}
 public TrustedStoppedV1.Snapshot Read(string name){Step("read");throw new TrustedStoppedV1.Failure(998);}
 public void Dispose(){Calls.Add("dispose");}
}
'@
Add-Type -TypeDefinition ($using+"`n"+($texts -join "`n")+"`n"+$fake)

$count=0
function Need([bool]$v){if(-not $v){throw 'guard_test_failed'}}
function Reject([scriptblock]$action){$denied=$false;try{& $action}catch{$denied=$true};Need $denied;$script:count++}
$iid='1'*64;$name='N100HA_'+$iid.Substring(0,24)+'_controller';$hostPath='C:\Private\installation-11111111-1111-4111-8111-111111111111\host\windows-service-host.exe';$image='"'+$hostPath+'"'
[TrustedStoppedV1.Contract]::Inputs($iid,$name,$hostPath);$count++
foreach($bad in @('C:\Private\..\windows-service-host.exe','C:\Private\windows-service-host.exe --start','C:\Private\x:ads\windows-service-host.exe','C:\Private\x.\windows-service-host.exe',('C:\Private\x'+"`n"+'\windows-service-host.exe'))){Reject {[TrustedStoppedV1.Contract]::Inputs($iid,$name,$bad)}}
Reject {[TrustedStoppedV1.Contract]::Inputs(('x'*64),$name,$hostPath)};Reject {[TrustedStoppedV1.Contract]::Inputs($iid,'ExistingService',$hostPath)}
function Good{ $s=[TrustedStoppedV1.Snapshot]::new();$s.Name=$name;$s.Image=$image;$s.Account='NT SERVICE\'+$name;$s.Type=16;$s.Start=3;$s.ErrorControl=1;$s.State=1;$s.Pid=0;$s.SidType=1;return $s }
[TrustedStoppedV1.Contract]::VerifyFields((Good),$name,$image);$count++
$mutations=@{Name='other';Image=($image+' --extra');Account='LocalSystem';Type=[uint32]32;Start=[uint32]2;ErrorControl=[uint32]0;State=[uint32]4;Pid=[uint32]123;SidType=[uint32]0;Triggers=[uint32]1;Actions=[uint32]1;Delayed=$true;NonCrash=$true}
foreach($key in $mutations.Keys){$s=Good;$s.$key=$mutations[$key];Reject {[TrustedStoppedV1.Contract]::VerifyFields($s,$name,$image)}}
foreach($bad in @('{"a":1,"a":2}','{"a":1,}','{"a":1.5}','{"a":true}junk','{"a":"\u0000"}')){Reject {[TrustedStoppedV1.Json]::Object($bad,1024)|Out-Null}}
$api=[FakeScm]::new();$api.Present=$true;Reject {[TrustedStoppedV1.Contract]::Register($api,$iid,$name,$hostPath,[Action[string]]{param($e)})|Out-Null};Need (($api.Calls -join ',') -ceq 'exists')
foreach($at in @('create','seal','sid','read')){$api=[FakeScm]::new();$api.FailAt=$at;Reject {[TrustedStoppedV1.Contract]::Register($api,$iid,$name,$hostPath,[Action[string]]{param($e)})|Out-Null};$expected=switch($at){create{'exists,create'}seal{'exists,create,seal'}sid{'exists,create,seal,sid'}read{'exists,create,seal,sid,read'}};Need (($api.Calls -join ',') -ceq $expected)}
$api=[FakeScm]::new();Reject {[TrustedStoppedV1.Contract]::Register($api,$iid,$name,$hostPath,[Action[string]]{param($e)if($e -ceq 'created'){throw 'durable_event_failure'}})|Out-Null};Need (($api.Calls -join ',') -ceq 'exists,create')
# Exercise the exact ACE predicate used on both SCM descriptor readbacks.
$scmSid=[TrustedStoppedV1.Contract]::ServiceSid($name)
Need ($scmSid -ceq 'S-1-5-80-3354449512-1843507217-285173831-784620642-3327000656');$count++
$scmSids=[string[]]@('S-1-5-18','S-1-5-32-544',$scmSid)
$scmMasks=[int[]]@(0xF01FF,0xF01FF,0x20000);$scmFlags=[byte[]]@(0,0,0)
[TrustedStoppedV1.Contract]::VerifyDaclFacts($name,'S-1-5-32-544',0x9004,$scmSids,$scmMasks,$scmFlags);$count++
Need ([TrustedStoppedV1.Contract]::ServiceSddl($name).EndsWith('(A;;0x20000;;;'+$scmSid+')'));$count++
foreach($extra in @(0,2,16,32,0x40000,0x80000,0x10000000)){
 $mask=if($extra -eq 0){0}else{0x20000 -bor $extra}
 Reject {[TrustedStoppedV1.Contract]::VerifyDaclFacts($name,'S-1-5-32-544',0x9004,$scmSids,[int[]]@(0xF01FF,0xF01FF,$mask),$scmFlags)}
}
Reject {[TrustedStoppedV1.Contract]::VerifyDaclFacts($name,'S-1-5-32-544',0x9004,$scmSids,$scmMasks,[byte[]]@(0,0,16))}
Reject {[TrustedStoppedV1.Contract]::VerifyDaclFacts($name,'S-1-5-32-544',0x9004,[string[]]@('S-1-5-18','S-1-5-32-544','S-1-5-11'),$scmMasks,$scmFlags)}
Reject {[TrustedStoppedV1.Contract]::VerifyDaclFacts($name,'S-1-5-32-544',0x9004,[string[]]@('S-1-5-18','S-1-5-32-544'),[int[]]@(0xF01FF,0xF01FF),[byte[]]@(0,0))}
$profile='strict-canonical-observed-v1';$rootSid='S-1-5-80-1-2-3-4-5';$rootSids=[string[]]@('S-1-5-18','S-1-5-32-544',$rootSid);$rootMasks=[int[]]@(2032127,2032127,131232);$rootFlags=[byte[]]@(3,3,0)
[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,$rootSids,$rootMasks,$rootFlags);$count++
Need ([TrustedStoppedV1.NewPrivate]::InstallationRootAcl($rootSid,$profile).EndsWith('(A;;0x200a0;;;'+$rootSid+')'));$count++
foreach($badMask in @(131104,131233,197055,1179817,(131232 -bor 1048576),-2147483648)){$m=[int[]]@(2032127,2032127,$badMask);Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,$rootSids,$m,$rootFlags)}}
foreach($badFlags in @(1,2,3,8,16,255)){$f=[byte[]]@(3,3,$badFlags);Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,$rootSids,$rootMasks,$f)}}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,[string[]]@('S-1-5-18','S-1-5-32-544','S-1-5-80-9-9-9-9-9'),$rootMasks,$rootFlags)}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,[string[]]@('S-1-5-18','S-1-5-18',$rootSid),$rootMasks,$rootFlags)}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x8004,$rootSids,$rootMasks,$rootFlags)}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-21-1',0x9004,$rootSids,$rootMasks,$rootFlags)}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts('raw-exact-v1',$rootSid,'S-1-5-32-544',0x9004,$rootSids,$rootMasks,$rootFlags)}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,[string[]]@('S-1-5-18','S-1-5-32-544'),[int[]]@(2032127,2032127),[byte[]]@(3,3))}
Reject {[TrustedStoppedV1.NewPrivate]::ValidateInstallationRootFacts($profile,$rootSid,'S-1-5-32-544',0x9004,$rootSids,[int[]]@(2032126,2032127,131232),$rootFlags)}
Need ([TrustedStoppedV1.Contract]::CanonicalInstallationRoot('C:\ProgramData',$iid) -ceq ('C:\ProgramData\N100HA_'+$iid.Substring(0,24)));$count++
Reject {[TrustedStoppedV1.Contract]::CanonicalInstallationRoot('C:\ProgramData',('x'*64))}
Reject {[TrustedStoppedV1.Contract]::CanonicalInstallationRoot('C:\ProgramData\..',$iid)}
$tokens=$null;$errors=$null;$ps=[IO.File]::ReadAllText((Join-Path $SourceRoot 'reserve-identity.ps1'));$ast=[Management.Automation.Language.Parser]::ParseInput($ps,[ref]$tokens,[ref]$errors);Need ($errors.Count -eq 0);$count++
Need ($ps -notmatch 'Start-Service|Stop-Service|DeleteService|StartServiceW|ControlService|SetSD');Need ($ps -notmatch "File\([^\r\n]*'service.args'");Need ($ps -match 'ready_to_start=\$false' -and $ps -match 'args_present=\$false');$count++
Need ($ps -match 'CanonicalInstallationRoot' -and $ps -match 'CommonApplicationData' -and $ps -match '\$parent -ceq \$programData' -and $ps -notmatch 'Join-Path \$parent \(''installation-''');$count++
# Execute the real Entry traversal against DirectoryInfo fixtures. ProgramData
# models its normal public read ACEs, which only ancestor mode can accept.
$entryDefinition=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Entry'},$false))[0]
. ([scriptblock]::Create($entryDefinition.Extent.Text))
$script:fakeProgramData=[IO.Path]::Combine([IO.Path]::GetTempPath(),'mock-programdata');$mockRoot=[IO.Path]::Combine($script:fakeProgramData,'N100HA_fixture');$script:entryCalls=[Collections.Generic.List[object]]::new()
function Get-Item{param([string]$LiteralPath,[switch]$Force);return [IO.DirectoryInfo]::new($LiteralPath)}
function Assert-PreflightEntry{param($Entry,$Writers,$Readers,[bool]$Private,[bool]$Ancestor);$script:entryCalls.Add([pscustomobject]@{path=$Entry.FullName;is_private=$Private;is_ancestor=$Ancestor});if($Entry.FullName -ceq $script:fakeProgramData -and $Private){throw 'modeled_programdata_normal_read_ace'}}
Entry $mockRoot $rootSid $false 0
Need ($script:entryCalls.Count -ge 3 -and $script:entryCalls[0].path -ceq $mockRoot -and $script:entryCalls[0].is_private -and -not $script:entryCalls[0].is_ancestor);$count++
Need ($script:entryCalls[1].path -ceq $script:fakeProgramData -and -not $script:entryCalls[1].is_private -and $script:entryCalls[1].is_ancestor);$count++
foreach($call in @($script:entryCalls|Select-Object -Skip 1)){Need (-not $call.is_private -and $call.is_ancestor)};$count++
# Existing file/host parent policy retains its original private-depth default.
Reject {Entry $mockRoot $rootSid}
# Verify the real Hold function passes its selected depth into this traversal,
# and the production root invocation selects zero rather than relying on default.
$holdDefinition=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Hold'},$false))[0]
$constructor='$h=[TrustedStoppedV1.Held]::new($path,$dir);';Need ($holdDefinition.Extent.Text.Contains($constructor))
# Replace only the native handle constructor; execute the unmodified real Hold
# parameter binding and Entry call, without a Windows native API invocation.
$holdFixture=$holdDefinition.Extent.Text.Replace($constructor,'$h=[pscustomobject]@{path=$path;directory=$dir};');. ([scriptblock]::Create($holdFixture));$held=[Collections.Generic.List[object]]::new();$script:entryCalls.Clear();$result=Hold $mockRoot $true $rootSid 0
Need ($held.Count -eq 1 -and $result.path -ceq $mockRoot -and $script:entryCalls[0].is_private -and -not $script:entryCalls[1].is_private -and $script:entryCalls[1].is_ancestor);$count++
Need ($ps -match 'Hold \$script:installRoot \$true \$sid 0');$count++
Need ($ps.IndexOf("'reservation-intent.json'") -lt $ps.IndexOf('::Register('));Need ($ps -match 'CreateNew|NewPrivate' -and $ps -match 'automatic_stop_or_delete=\$false');$count++
[ordered]@{tests_passed=$count;native_calls_performed=$false;service_created=$false;compile='PASS';scope='portable_metadata_json_and_partial_preserve_negative_guards';native_sddl_acl_service_integration='NOT_RUN'}|ConvertTo-Json -Compress
