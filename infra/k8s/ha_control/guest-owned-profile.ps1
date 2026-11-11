# Read-only installer-first profile. This file is SHA-pinned by each caller.
function Get-GuestOwnedFields{return @('root_profile','executor_source_commit','executor_script_sha256','reservation_receipt_sha256','reservation_root_identity')}
function Get-GuestOwnedServiceSid([string]$Installation){
 if($Installation -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_owned_installation_rejected'}
 $name='N100HA_'+$Installation.Substring(0,24)+'_controller';$hash=[Security.Cryptography.SHA1]::Create()
 try{$bytes=$hash.ComputeHash([Text.Encoding]::Unicode.GetBytes($name.ToUpperInvariant()))}finally{$hash.Dispose()}
 return 'S-1-5-80-'+((0..4|ForEach-Object{[BitConverter]::ToUInt32($bytes,4*$_).ToString([Globalization.CultureInfo]::InvariantCulture)}) -join '-')
}
function Assert-GuestOwnedDeclaration($Spec,[string]$ActualScriptHash){
 if($Spec.root_profile -cne 'strict-canonical-observed-v1' -or $Spec.executor_source_commit -isnot [string] -or $Spec.executor_source_commit -cnotmatch '^[0-9a-f]{40}$' -or $Spec.reservation_root_identity -isnot [string] -or $Spec.reservation_root_identity -cnotmatch '^[0-9a-f]{8}:[0-9a-f]{16}$'){throw 'guest_owned_profile_rejected'}
 foreach($key in @('executor_script_sha256','reservation_receipt_sha256')){if($Spec.$key -isnot [string] -or $Spec.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_owned_profile_rejected'}}
 if($ActualScriptHash -cne '' -and $Spec.executor_script_sha256 -cne $ActualScriptHash){throw 'guest_owned_executor_changed'}
 $null=Get-GuestOwnedServiceSid $Spec.installation_id
}
function Assert-GuestOwnedRootFacts([string]$Owner,[bool]$Protected,$Aces,[string]$Installation){
 $sid=Get-GuestOwnedServiceSid $Installation
 if($Owner -cnotin @('S-1-5-18','S-1-5-32-544') -or -not $Protected -or @($Aces).Count -ne 3){throw 'guest_owned_root_acl_rejected'}
 $seen=@{}
 foreach($a in $Aces){
  if($a.simple_allow -isnot [bool] -or -not $a.simple_allow -or $seen.ContainsKey($a.sid)){throw 'guest_owned_root_acl_rejected'}
  if($a.sid -ceq $sid){if($a.mask -ne 131232 -or $a.flags -ne 0){throw 'guest_owned_root_acl_rejected'}}
  elseif($a.sid -cin @('S-1-5-18','S-1-5-32-544')){if($a.mask -ne 2032127 -or $a.flags -ne 3){throw 'guest_owned_root_acl_rejected'}}else{throw 'guest_owned_root_acl_rejected'}
  $seen[$a.sid]=$true
 }
 if(-not $seen.ContainsKey($sid) -or -not $seen.ContainsKey('S-1-5-18') -or -not $seen.ContainsKey('S-1-5-32-544')){throw 'guest_owned_root_acl_rejected'}
}
function Assert-GuestOwnedRootAcl([string]$Path,$Spec){
 $entry=Get-Item -LiteralPath $Path -Force
 if($entry -isnot [IO.DirectoryInfo] -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'guest_owned_root_type_rejected'}
 $walk=$entry.Parent;while($null -ne $walk){if($walk.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'guest_owned_root_reparse_rejected'};$walk=$walk.Parent}
 $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
 if($null -eq $sd.DiscretionaryAcl){throw 'guest_owned_root_acl_rejected'}
 $facts=@($sd.DiscretionaryAcl|ForEach-Object{[pscustomobject]@{sid=$_.SecurityIdentifier.Value;mask=$_.AccessMask;flags=[int]$_.AceFlags;simple_allow=($_ -is [Security.AccessControl.CommonAce] -and -not $_.IsCallback -and $_.AceQualifier -eq 'AccessAllowed')}})
 Assert-GuestOwnedRootFacts $sd.Owner.Value ([bool]($sd.ControlFlags -band [Security.AccessControl.ControlFlags]::DiscretionaryAclProtected)) $facts $Spec.installation_id
}
function Assert-GuestOwnedPrivateFile([string]$Path){
 $entry=Get-Item -LiteralPath $Path -Force
 if($entry -isnot [IO.FileInfo] -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'guest_owned_receipt_type_rejected'}
 $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Path).Sddl)
 if($sd.Owner.Value -cnotin @('S-1-5-18','S-1-5-32-544') -or $null -eq $sd.DiscretionaryAcl -or $sd.DiscretionaryAcl.Count -ne 2){throw 'guest_owned_receipt_acl_rejected'}
 $seen=@{};foreach($a in $sd.DiscretionaryAcl){if($a -isnot [Security.AccessControl.CommonAce] -or $a.IsCallback -or $a.AceQualifier -ne 'AccessAllowed' -or $a.SecurityIdentifier.Value -cnotin @('S-1-5-18','S-1-5-32-544') -or $a.AccessMask -ne 2032127 -or [int]$a.AceFlags -notin @(0,16) -or $seen.ContainsKey($a.SecurityIdentifier.Value)){throw 'guest_owned_receipt_acl_rejected'};$seen[$a.SecurityIdentifier.Value]=$true}
}
function Read-GuestOwnedPinned([string]$Path,[string]$Hash){
 Assert-GuestOwnedPrivateFile $Path
 $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
 try{if($stream.Length -lt 2 -or $stream.Length -gt 32768){throw 'guest_owned_receipt_limit'};$bytes=[byte[]]::new([int]$stream.Length);$at=0;while($at -lt $bytes.Length){$n=$stream.Read($bytes,$at,$bytes.Length-$at);if($n -le 0){throw 'guest_owned_receipt_truncated'};$at+=$n}}finally{$stream.Dispose()}
 $h=[Security.Cryptography.SHA256]::Create();try{$actual=[BitConverter]::ToString($h.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
 if($actual -cne $Hash){throw 'guest_owned_receipt_changed'}
 $text=[Text.UTF8Encoding]::new($false,$true).GetString($bytes)
 if($text.Contains('\')){throw 'guest_owned_receipt_escape_rejected'}
 $keys=@([regex]::Matches($text,'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')|ForEach-Object{$_.Groups[1].Value})
 if(@($keys|Sort-Object -Unique).Count -ne $keys.Count){throw 'guest_owned_receipt_duplicate_rejected'}
 Assert-GuestOwnedPrivateFile $Path
 return $text|ConvertFrom-Json
}
function Assert-GuestOwnedReservation($Receipt,$Owned,$Spec){
 $keys=@('format','descriptor_profile','root_service_ace_mask','request_sha256','installation_id','selected_source_commit','build_origin_commit','ownership_receipt_sha256','service_sd_sha256','service_config_sha256','journal_identity_sha256','journal_sd_sha256','ready_to_start','args_present','runtime_authority_issued','namespace_changed','service_started')
 if((($Receipt.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($keys|Sort-Object -CaseSensitive)-join ',')){throw 'guest_owned_reservation_shape_rejected'}
 if($Receipt.format -cne 'fresh-service-identity-reservation-receipt-v1' -or $Receipt.descriptor_profile -cne $Spec.root_profile -or $Receipt.root_service_ace_mask -ne 131232 -or $Receipt.installation_id -cne $Spec.installation_id -or $Receipt.selected_source_commit -cne $Spec.executor_source_commit -or $Receipt.build_origin_commit -cnotmatch '^[0-9a-f]{40}$'){throw 'guest_owned_reservation_rejected'}
 foreach($key in @('request_sha256','ownership_receipt_sha256','service_sd_sha256','service_config_sha256','journal_identity_sha256','journal_sd_sha256')){if($Receipt.$key -isnot [string] -or $Receipt.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'guest_owned_reservation_rejected'}}
 foreach($key in @('ready_to_start','args_present','runtime_authority_issued','namespace_changed','service_started')){if($Receipt.$key -isnot [bool] -or $Receipt.$key){throw 'guest_owned_reservation_phase_rejected'}}
 $ownedKeys=@('schema_version','format','source_commit','installation_id','service_name','service_sid','service_config_sha256','service_image_sha256','new_owned_service','scm_registration_complete')
 if((($Owned.PSObject.Properties.Name|Sort-Object -CaseSensitive)-join ',') -cne (($ownedKeys|Sort-Object -CaseSensitive)-join ',') -or ($Owned.schema_version -isnot [int] -and $Owned.schema_version -isnot [long]) -or $Owned.schema_version -ne 1 -or $Owned.format -cne 'ha-owned-controller-service-v1' -or $Owned.installation_id -cne $Spec.installation_id -or $Owned.source_commit -cne $Spec.executor_source_commit -or $Owned.service_name -cne ('N100HA_'+$Spec.installation_id.Substring(0,24)+'_controller') -or $Owned.service_sid -cne (Get-GuestOwnedServiceSid $Spec.installation_id) -or $Owned.service_config_sha256 -cne $Receipt.service_config_sha256 -or $Owned.service_image_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $Owned.new_owned_service -isnot [bool] -or -not $Owned.new_owned_service -or $Owned.scm_registration_complete -isnot [bool] -or -not $Owned.scm_registration_complete){throw 'guest_owned_service_lineage_rejected'}
}
function Initialize-GuestOwnedNative{
 if('HaGuestOwnedLeaseV1' -as [type]){throw 'guest_owned_native_type_collision'}
 Add-Type -TypeDefinition @'
using System;using System.IO;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;
public sealed class HaGuestOwnedLeaseV1:IDisposable {
 [StructLayout(LayoutKind.Sequential)]struct Info{public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME C,A,W;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
 [DllImport("kernel32.dll")]static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 SafeFileHandle handle;string identity;
 public HaGuestOwnedLeaseV1(string path,string expected){handle=CreateFile(path,0x20000,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);if(handle.IsInvalid)throw new InvalidOperationException("guest_owned_root_open_failed");try{identity=Identity();if(identity!=expected)throw new InvalidOperationException("guest_owned_root_identity_changed");}catch{Dispose();throw;}}
 public string Identity(){Info i;if(!GetFileInformationByHandle(handle,out i)||(i.Attr&0x410)!=16||i.Links!=1)throw new InvalidOperationException("guest_owned_root_identity_rejected");return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");}
 public void AssertCurrent(){if(Identity()!=identity)throw new InvalidOperationException("guest_owned_root_identity_changed");}
 public void Dispose(){if(handle!=null){handle.Dispose();handle=null;}}
}
'@
}
function Open-GuestOwnedRoot([string]$Root,$Spec,[string]$ScriptHash){
 Assert-GuestOwnedDeclaration $Spec $ScriptHash
 Assert-GuestOwnedRootAcl $Root $Spec
 $expected=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$Spec.installation_id.Substring(0,24))
 if($Root -ine $expected){throw 'guest_owned_root_scope_rejected'}
 Initialize-GuestOwnedNative
 $lease=[HaGuestOwnedLeaseV1]::new($Root,$Spec.reservation_root_identity)
 try{
  $receipt=Read-GuestOwnedPinned (Join-Path $Root 'reservation-receipt.json') $Spec.reservation_receipt_sha256
  $owned=Read-GuestOwnedPinned (Join-Path $Root 'namespace-service-ownership.json') $receipt.ownership_receipt_sha256
  Assert-GuestOwnedReservation $receipt $owned $Spec
  Assert-GuestOwnedRootAcl $Root $Spec;$lease.AssertCurrent();return $lease
 }catch{$lease.Dispose();throw}
}
