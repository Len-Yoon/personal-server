param([string]$Plan,[string]$Approval,[switch]$Go)
# Separate profile. No installation, ACL mutation, WSL, cached graph or legacy
# receipt conversion. See docs/20261109_N100_강제소유권_구조전환.md.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

function Assert-EnforcedDeadline($Watch) {
    if($Watch.Elapsed.TotalMilliseconds -ge 2000){throw 'enforced_observation_expired'}
}
function Get-EnforcedHash([byte[]]$Bytes) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Assert-EnforcedConfigProfile($Value){
    Assert-EnforcedKeys $Value @('name','definition_sha256','capability_name','capability_sid','host_hypervisor_tcb_acknowledged','operational_cutover_accepted','io_acceptance_sha256','external_capability_review_sha256','acceptance_scope','isolation_receipt_sha256')
    foreach($key in @('name','definition_sha256','capability_name','capability_sid','io_acceptance_sha256','external_capability_review_sha256','acceptance_scope','isolation_receipt_sha256')){if($Value.$key -isnot [string]){throw 'config_profile_rejected'}}
    if($Value.name -cne 'windows-managed-config-v1' -or $Value.definition_sha256 -cne '61fb12a5c53fa5a76c067f50e6a1ea56a915ebd3d9af86fcb67ff36cd48bd0ef' -or
        $Value.capability_name -cne 'vmWorkerProcess' -or $Value.capability_sid -cne 'S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704' -or
        $Value.host_hypervisor_tcb_acknowledged -isnot [bool] -or -not $Value.host_hypervisor_tcb_acknowledged -or $Value.operational_cutover_accepted -isnot [bool]){throw 'config_profile_rejected'}
    if($Value.acceptance_scope -cnotin @('isolated-predeployment','production')){throw 'config_acceptance_scope_rejected'}
    foreach($key in @('io_acceptance_sha256','external_capability_review_sha256','isolation_receipt_sha256')){if($Value.$key -isnot [string] -or ($Value.$key -cne '' -and $Value.$key -cnotmatch '^[0-9a-f]{64}$')){throw 'config_acceptance_rejected'}}
    if(($Value.acceptance_scope -ceq 'isolated-predeployment' -and $Value.operational_cutover_accepted) -or
        ($Value.acceptance_scope -ceq 'production' -and ($Value.isolation_receipt_sha256 -cne '' -or ($Value.operational_cutover_accepted -and ($Value.io_acceptance_sha256 -ceq '' -or $Value.external_capability_review_sha256 -ceq ''))))){throw 'config_acceptance_rejected'}
    return $Value
}
function Get-EnforcedConfigProfileHash($Value){
    $null=Assert-EnforcedConfigProfile $Value
    $ordered=[ordered]@{};foreach($key in @('acceptance_scope','capability_name','capability_sid','definition_sha256','external_capability_review_sha256','host_hypervisor_tcb_acknowledged','io_acceptance_sha256','isolation_receipt_sha256','name','operational_cutover_accepted')){$ordered[$key]=$Value.$key}
    return Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes(($ordered|ConvertTo-Json -Compress)))
}

function Get-EnforcedNamespaceClosureHash($Value){
    $keys=@('version','names','sd_sha256','children_sha256','classification_sha256')
    if($Value.version -ceq 'direct-children-observed-v1'){
        $keys+=@('descriptor_profile','comparator_sha256','apply_receipt_sha256','plan_sha256','service_identity_sha256')
        if($Value.descriptor_profile -cne 'strict-canonical-observed-v1' -or $Value.comparator_sha256 -cne '56cc20aaf52bd7ea7b1b0f88d562a15d1f28ffb3aac7ad4b0e0df2ec3e1dac3e'){throw 'namespace_observed_profile_rejected'}
        foreach($key in @('apply_receipt_sha256','plan_sha256','service_identity_sha256')){if($Value.$key -isnot [string] -or $Value.$key -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_observed_binding_rejected'}}
    }
    Assert-EnforcedKeys $Value $keys
    if($Value.version -isnot [string] -or $Value.version -cnotin @('direct-children-v1','direct-children-observed-v1') -or $Value.names -isnot [array] -or $Value.names.Count -lt 2 -or $Value.names.Count -gt 9 -or $Value.names[0] -cne 'root/virtualization/v2'){throw 'namespace_closure_rejected'}
    $prior='';foreach($name in $Value.names){if($name -isnot [string] -or ($name -cne 'root/virtualization/v2' -and $name -cnotmatch '^root/virtualization/v2/[a-z_][a-z0-9_]{0,63}$') -or [string]::CompareOrdinal($prior,$name) -ge 0){throw 'namespace_closure_rejected'};$prior=$name}
    foreach($key in @('sd_sha256','children_sha256','classification_sha256')){if($Value.$key -isnot [array] -or $Value.$key.Count -ne $Value.names.Count){throw 'namespace_closure_rejected'};foreach($hash in $Value.$key){if($hash -isnot [string] -or $hash -cnotmatch '^[0-9a-f]{64}$'){throw 'namespace_closure_rejected'}}}
    $leafNames=@($Value.names|Select-Object -Skip 1|ForEach-Object{($_ -split '/')[-1]})
    $rootChildren=Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $leafNames -Compress)))
    $empty=Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes('[]'))
    for($i=0;$i -lt $Value.names.Count;$i++){if($Value.children_sha256[$i] -cne $(if($i -eq 0){$rootChildren}else{$empty})){throw 'namespace_closure_children_rejected'}}
    $ordered=[ordered]@{};foreach($key in @($keys|Sort-Object -CaseSensitive)){$ordered[$key]=$Value.$key}
    return Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes(($ordered|ConvertTo-Json -Depth 4 -Compress)))
}
function Assert-EnforcedNamespaceBytes([byte[]]$Bytes,$writers){
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new($Bytes,0);Assert-EnforcedDescriptor $sd $writers 1343029278
}
function Assert-EnforcedNamespaceLive($Config,$Policy,$Writers,$Watch){
    $cfg=$Config.windows_enforced;$closure=$null;$names=@('root/virtualization/v2')
    if($cfg.PSObject.Properties['namespace_closure']){
        if((Get-EnforcedNamespaceClosureHash $cfg.namespace_closure) -cne (Get-EnforcedNamespaceClosureHash $Policy.namespace_closure)){throw 'namespace_closure_policy_changed'}
        $closure=$cfg.namespace_closure;$names=@($closure.names)
    }
    for($i=0;$i -lt $names.Count;$i++){
        Assert-EnforcedDeadline $Watch
        $children=@(Get-CimInstance -Namespace $names[$i] -ClassName '__NAMESPACE' -OperationTimeoutSec 1 -ErrorAction Stop|ForEach-Object{$_.Name}|Sort-Object -CaseSensitive)
        if($children.Count -gt 8 -or @($children|Sort-Object -Unique).Count -ne $children.Count){throw 'namespace_closure_live_children_rejected'}
        foreach($child in $children){if($child -isnot [string] -or $child -cnotmatch '^[a-z_][a-z0-9_]{0,63}$'){throw 'namespace_closure_live_children_rejected'}}
        if($null -eq $closure){if($children.Count -ne 0){throw 'namespace_closure_required'}}else{
            $hash=Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $children -Compress)))
            if($hash -cne $closure.children_sha256[$i]){throw 'namespace_closure_live_children_changed'}
        }
        Assert-EnforcedDeadline $Watch
        $security=Get-CimInstance -Namespace $names[$i] -ClassName '__SystemSecurity' -OperationTimeoutSec 1 -ErrorAction Stop
        $answer=Invoke-CimMethod -InputObject $security -MethodName 'GetSD' -OperationTimeoutSec 1 -ErrorAction Stop
        if($answer.ReturnValue -ne 0 -or $null -eq $answer.SD){throw 'hyperv_namespace_policy_unverified'}
        Assert-EnforcedDeadline $Watch
        $bytes=[byte[]]$answer.SD;Assert-EnforcedNamespaceBytes $bytes $Writers
        if($null -ne $closure -and (Get-EnforcedHash $bytes) -cne $closure.sd_sha256[$i]){throw 'namespace_closure_live_sd_changed'}
    }
}

function ConvertTo-EnforcedDiskIdentifier($Value) {
    # Native providers expose either Guid or uppercase string. No RPC or I/O:
    # only one bounded D-format parse per disk in the existing two-second read.
    if($Value -isnot [string] -and $Value -isnot [Guid]){throw 'vhd_identifier_rejected'}
    $text=$Value.ToString();$guid=[Guid]::Empty
    if($text.Length -ne 36 -or $text -cnotmatch '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' -or -not [Guid]::TryParseExact($text,'D',[ref]$guid) -or $guid -eq [Guid]::Empty){throw 'vhd_identifier_rejected'}
    return $guid.ToString('D')
}
function New-EnforcedNonce([long]$Sequence) {
    $bytes=New-Object byte[] 24;$random=[Security.Cryptography.RandomNumberGenerator]::Create()
    try{$random.GetBytes($bytes)}finally{$random.Dispose()}
    return ([BitConverter]::ToString($bytes).Replace('-','').ToLowerInvariant()+$Sequence.ToString('x16'))
}
function Assert-EnforcedSequence($Request,[string]$Instance) {
    if(($Request.sequence -isnot [int] -and $Request.sequence -isnot [long]) -or
        $Request.sequence -lt 1 -or $Request.sequence -gt 9007199254740991 -or
        $Request.sequence -ne ($script:enforcedSequence+1) -or $Request.broker_instance -cne $Instance -or
        $Request.nonce -isnot [string] -or $Request.nonce -cnotmatch '^[0-9a-f]{64}$' -or
        $Request.nonce.Substring(48) -cne ([long]$Request.sequence).ToString('x16')){throw 'enforced_sequence_rejected'}
    $script:enforcedSequence=[long]$Request.sequence
}
function Get-EnforcedCurrentSid {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try{return $identity.User.Value}finally{$identity.Dispose()}
}
function Get-EnforcedFileIdentity([string]$Path){return [EnforcedVmNativeV1]::FileIdentity($Path)}
function Get-EnforcedVmSid([string]$Id){return ([Security.Principal.NTAccount]::new('NT VIRTUAL MACHINE\'+$Id)).Translate([Security.Principal.SecurityIdentifier]).Value}
function Assert-EnforcedKeys($Value,[string[]]$Keys) {
    $actual=@($Value.PSObject.Properties.Name|Sort-Object -CaseSensitive)
    if(($actual -join "`n") -cne (($Keys|Sort-Object -CaseSensitive) -join "`n")){throw 'enforced_shape_rejected'}
}
function Convert-EnforcedJson([string]$Text) {
    # PowerShell 7.5 otherwise converts ISO timestamps to DateTime; 5.1 leaves
    # them as strings. Preserve the same exact protocol representation in both.
    $command=Get-Command Microsoft.PowerShell.Utility\ConvertFrom-Json -ErrorAction Stop
    if($command.Parameters.ContainsKey('DateKind')){return Microsoft.PowerShell.Utility\ConvertFrom-Json -InputObject $Text -DateKind String -ErrorAction Stop}
    return Microsoft.PowerShell.Utility\ConvertFrom-Json -InputObject $Text -ErrorAction Stop
}
function Initialize-EnforcedNative {
    if('EnforcedVmNativeV1' -as [type]){throw 'enforced_type_collision'}
    # Fixed read-only handle APIs. Neither file contents nor SCM configuration
    # are changed. Virtual disks are identified, never hashed as mutable data.
    Microsoft.PowerShell.Utility\Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class EnforcedVmNativeV1 {
 [StructLayout(LayoutKind.Sequential)] struct Info {public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME Created,Accessed,Written;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFile(string p,uint a,uint s,IntPtr q,uint d,uint f,IntPtr t);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr OpenSCManager(string m,string d,uint a);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr OpenService(IntPtr s,string n,uint a);
 [DllImport("advapi32.dll",SetLastError=true)] static extern bool QueryServiceObjectSecurity(IntPtr h,uint i,byte[] b,uint n,out uint r);
 [DllImport("advapi32.dll")] static extern bool CloseServiceHandle(IntPtr h);
 [DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CommandLineToArgvW(string text,out int count);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
 [DllImport("kernelbase.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool DeriveCapabilitySidsFromName(string name,out IntPtr groups,out uint groupCount,out IntPtr caps,out uint capCount);
 static void FreeCapabilityArray(IntPtr array,uint count){if(array==IntPtr.Zero)return;if(count<=16)for(uint i=0;i<count;i++){IntPtr p=Marshal.ReadIntPtr(array,(int)i*IntPtr.Size);if(p!=IntPtr.Zero)LocalFree(p);}LocalFree(array);}
 public static string VmWorkerCapability(){
  IntPtr groups=IntPtr.Zero,caps=IntPtr.Zero;uint ng=0,nc=0;
  try{if(!DeriveCapabilitySidsFromName("vmWorkerProcess",out groups,out ng,out caps,out nc)||ng>16||nc!=1)throw new InvalidOperationException("config_capability_derivation_failed");return new System.Security.Principal.SecurityIdentifier(Marshal.ReadIntPtr(caps)).Value;}finally{FreeCapabilityArray(groups,ng);FreeCapabilityArray(caps,nc);}
 }
 public static string[] Argv(string text){int count;IntPtr memory=CommandLineToArgvW(text,out count);if(memory==IntPtr.Zero)throw new InvalidOperationException();try{if(count<1||count>32)throw new InvalidOperationException();var result=new string[count];for(int i=0;i<count;i++)result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size));return result;}finally{LocalFree(memory);}}
 public static string FileIdentity(string path) {
  using(var h=CreateFile(path,0,7,IntPtr.Zero,3,0x00200000,IntPtr.Zero)) {
   if(h.IsInvalid)throw new Win32Exception();Info i;if(!GetFileInformationByHandle(h,out i)||(i.Attr&0x400)!=0||i.Links!=1)throw new InvalidOperationException();
   return i.Volume.ToString("x8")+":"+i.IndexHigh.ToString("x8")+i.IndexLow.ToString("x8");
  }
 }
 public static byte[] ServiceSecurity(string name) {
  IntPtr scm=OpenSCManager(null,null,1);if(scm==IntPtr.Zero)throw new Win32Exception();
  try{IntPtr service=OpenService(scm,name,0x20000);if(service==IntPtr.Zero)throw new Win32Exception();
   try{uint size;QueryServiceObjectSecurity(service,5,null,0,out size);if(size<20||size>65536)throw new InvalidOperationException();byte[] bytes=new byte[size];if(!QueryServiceObjectSecurity(service,5,bytes,size,out size))throw new Win32Exception();return bytes;}
   finally{CloseServiceHandle(service);}}
  finally{CloseServiceHandle(scm);}
 }
}
'@ -ErrorAction Stop
}
function Assert-EnforcedAllowedMask([string]$Sid,[long]$Mask,[string[]]$Allowed,[long]$RestrictedMask) {
    if((($Mask -band 4294967295) -band $RestrictedMask) -ne 0 -and $Sid -notin $Allowed){throw 'enforced_acl_rejected'}
}
function Get-EnforcedServiceControlMask {
    # Service GENERIC_EXECUTE includes START/STOP/PAUSE/USER_DEFINED_CONTROL.
    # https://learn.microsoft.com/en-us/windows/win32/services/service-security-and-access-rights
    return [long]1879900530 # 0x700d0172; file execute rights use a separate mask.
}
function Get-EnforcedOsVolumeRoot {return [IO.Path]::GetPathRoot([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows))}
function Test-EnforcedOsRootOwner([string]$Owner,[string]$Path,[int]$Depth) {
    return ($Owner -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' -and
        $Depth -gt 1 -and $Path -cmatch '^[A-Za-z]:\\$' -and $Path -ieq (Get-EnforcedOsVolumeRoot))
}
function Assert-EnforcedDescriptor($Descriptor,[string[]]$Writers,[long]$WriteMask,[bool]$IgnoreInheritOnly=$false,[bool]$OsRootOwner=$false) {
    if($null -eq $Descriptor.Owner -or ($Descriptor.Owner.Value -notin $Writers -and
            -not ($OsRootOwner -and $Descriptor.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')) -or
        $null -eq $Descriptor.DiscretionaryAcl -or $Descriptor.DiscretionaryAcl.Count -eq 0){throw 'enforced_acl_rejected'}
    foreach($ace in $Descriptor.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'enforced_acl_rejected'}
        if($IgnoreInheritOnly -and (([int]$ace.AceFlags) -band ([int][Security.AccessControl.AceFlags]::InheritOnly))){continue}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed){
            Assert-EnforcedAllowedMask $ace.SecurityIdentifier.Value ([long]$ace.AccessMask) $Writers $WriteMask
        }
    }
}
function Assert-EnforcedPath([string]$Path,[string[]]$Writers) {
    if($Path -cnotmatch '^[A-Za-z]:\\' -or $Path -match '(^|\\)\.\.($|\\)' -or $Path.Substring(2).Contains(':')){throw 'enforced_path_rejected'}
    $entry=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $depth=0
    while($null -ne $entry){
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'enforced_path_rejected'}
        $acl=Get-Acl -LiteralPath $entry.FullName -ErrorAction Stop
        $descriptor=[Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
        # Protect the object and its immediate creation directory completely.
        # On higher existing ancestors, creating siblings cannot replace the
        # protected child; still deny delete-child, deletion and owner/DACL
        # takeover, and ignore ACEs that do not apply to that ancestor itself.
        if($depth -le 1){Assert-EnforcedDescriptor $descriptor $Writers 1343029590}
        else {Assert-EnforcedDescriptor $descriptor $Writers 269287488 $true (Test-EnforcedOsRootOwner $descriptor.Owner.Value $entry.FullName $depth)}
        $entry=if($entry -is [IO.DirectoryInfo]){$entry.Parent}else{$entry.Directory}
        $depth++
    }
}
function Get-EnforcedPathDescriptor([string]$Path) {
    $acl=Get-Acl -LiteralPath $Path -ErrorAction Stop
    return [Security.AccessControl.RawSecurityDescriptor]::new($acl.Sddl)
}
function Assert-EnforcedDataReaders([string]$Path,[string[]]$Readers,[string[]]$Writers,$Watch,[bool]$Tree=$false) {
    Assert-EnforcedDeadline $Watch
    $root=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $parent=if($root.PSIsContainer){$root.Parent}else{$root.Directory}
    if($null -eq $parent){throw 'sensitive_parent_unverified'}
    # A restrictive directory listing alone does not protect a known filename:
    # Windows may bypass traverse checks. Inspect each config child as well.
    Assert-EnforcedDescriptor (Get-EnforcedPathDescriptor $parent.FullName) $Readers 2415919105 $true
    $pending=[Collections.Generic.Queue[string]]::new();$pending.Enqueue($root.FullName)
    $seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [void]$seen.Add($root.FullName)
    while($pending.Count -gt 0){
        Assert-EnforcedDeadline $Watch
        $entry=Get-Item -LiteralPath ($pending.Dequeue()) -Force -ErrorAction Stop
        if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'enforced_path_rejected'}
        $descriptor=Get-EnforcedPathDescriptor $entry.FullName
        Assert-EnforcedDescriptor $descriptor $Writers 1343029590
        Assert-EnforcedDescriptor $descriptor $Readers 2415919105 $true
        Assert-EnforcedDeadline $Watch
        if($Tree -and $entry.PSIsContainer){
            Get-ChildItem -LiteralPath $entry.FullName -Force -ErrorAction Stop|ForEach-Object {
                Assert-EnforcedDeadline $Watch
                if($seen.Count -ge 4096 -or -not $seen.Add($_.FullName)){throw 'sensitive_tree_limit'}
                $pending.Enqueue($_.FullName)
            }
        }
    }
    Assert-EnforcedDeadline $Watch
}
function Get-EnforcedPgFiles($Config,$Policy,$Watch) {
    # libpq opens these files itself. Never print/read their contents here.
    # Both the object and immediate containing directory exclude non-TCB
    # readers; the dedicated service is a reader, never an authority writer.
    Assert-EnforcedKeys $Config.pg_nodes @('A','B')
    $writers=@('S-1-5-18','S-1-5-32-544')+@($Policy.trusted_admin_sids)
    $readers=$writers+@($Config.windows_enforced.service_sid)
    $stamp=[Collections.Generic.List[string]]::new()
    foreach($node in @('A','B')){
        foreach($name in @('sslrootcert','passfile')){
            Assert-EnforcedDeadline $Watch
            $path=$Config.pg_nodes.$node.$name
            if($path -isnot [string]){throw 'pg_private_file_unverified'}
            Assert-EnforcedPath $path $writers
            $entry=Get-Item -LiteralPath $path -Force -ErrorAction Stop
            if($entry.PSIsContainer -or $entry.Length -gt 1048576 -or $null -eq $entry.Directory){throw 'pg_private_file_unverified'}
            foreach($protected in @($entry.FullName,$entry.Directory.FullName)){
                # FILE_READ_DATA/LIST_DIRECTORY, GENERIC_READ, GENERIC_ALL.
                Assert-EnforcedDescriptor (Get-EnforcedPathDescriptor $protected) $readers 2415919105 $true
            }
            $stamp.Add((Get-EnforcedFileIdentity $path))
            Assert-EnforcedDeadline $Watch
        }
    }
    return ($stamp|ConvertTo-Json -Compress)
}
function Read-EnforcedPrivate([string]$Path,$Watch) {
    Assert-EnforcedDeadline $Watch
    Assert-EnforcedPath $Path @('S-1-5-18','S-1-5-32-544')
    $identity=Get-EnforcedFileIdentity $Path
    $handle=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try{
        if($handle.Length -gt 1048576){throw 'enforced_size_limit'}
        $bytes=New-Object byte[] ([int]$handle.Length)
        $offset=0;while($offset -lt $bytes.Length){$read=$handle.Read($bytes,$offset,$bytes.Length-$offset);if($read -le 0){throw 'enforced_read_failed'};$offset+=$read}
        if((Get-EnforcedFileIdentity $Path) -cne $identity){throw 'enforced_file_changed'}
        Assert-EnforcedDeadline $Watch
        return @{bytes=$bytes;sha256=(Get-EnforcedHash $bytes)}
    }finally{$handle.Dispose()}
}
function Read-EnforcedCodeHash([string]$Path,$Watch) {
    Assert-EnforcedDeadline $Watch
    Assert-EnforcedPath $Path @('S-1-5-18','S-1-5-32-544')
    $identity=Get-EnforcedFileIdentity $Path
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if($item.PSIsContainer -or $item.Length -gt 67108864){throw 'code_size_limit'}
    $hash=(Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if((Get-EnforcedFileIdentity $Path) -cne $identity){throw 'code_file_changed'}
    Assert-EnforcedDeadline $Watch
    return $hash
}
function Get-EnforcedArgv([string]$Command){return [EnforcedVmNativeV1]::Argv($Command)}
function Get-EnforcedJournalPath{return ($Plan.Substring(0,$Plan.LastIndexOf('\')+1)+'journal\controller-state.json')}
function Get-EnforcedJournalChildren([string]$Root){return @(Get-ChildItem -LiteralPath $Root -Force -ErrorAction Stop)}
function Assert-EnforcedJournalGrant([long]$Granted,[bool]$Denied) {
    if($Denied -or (($Granted -band 268435456) -eq 0 -and ($Granted -band 197055) -ne 197055)){throw 'journal_service_modify_unverified'}
}
function Read-EnforcedJournalGrant([string]$Path,[string]$Sid) {
    $descriptor=Get-EnforcedPathDescriptor $Path;$granted=[long]0;$denied=$false
    foreach($ace in $descriptor.DiscretionaryAcl){
        if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'journal_acl_rejected'}
        if(([int]$ace.AceFlags) -band ([int][Security.AccessControl.AceFlags]::InheritOnly)){continue}
        $mask=([long]$ace.AccessMask -band 4294967295)
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessDenied -and ($mask -band 3490513239) -ne 0){$denied=$true}
        if($ace.AceQualifier -eq [Security.AccessControl.AceQualifier]::AccessAllowed -and $ace.SecurityIdentifier.Value -ceq $Sid){$granted=$granted -bor $mask}
    }
    Assert-EnforcedJournalGrant $granted $denied
}
function Assert-EnforcedJournal($Config,$Policy,$Watch) {
    Assert-EnforcedDeadline $Watch
    $path=Get-EnforcedJournalPath;$root=$path.Substring(0,$path.LastIndexOf('\'))
    # The plan parent remains immutable. Only its dedicated journal child is
    # writable by this service, including durable replace temp and lock files.
    $authority=@('S-1-5-18','S-1-5-32-544')+@($Policy.trusted_admin_sids)
    Assert-EnforcedPath ($root.Substring(0,$root.LastIndexOf('\'))) $authority
    $writers=$authority+@($Config.windows_enforced.service_sid)
    $entry=Get-Item -LiteralPath $root -Force -ErrorAction Stop
    if(-not $entry.PSIsContainer -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'journal_root_unverified'}
    Assert-EnforcedDescriptor (Get-EnforcedPathDescriptor $root) $writers 3490513239 $true
    Read-EnforcedJournalGrant $root $Config.windows_enforced.service_sid
    $children=@(Get-EnforcedJournalChildren $root)
    if($children.Count -gt 64){throw 'journal_inventory_rejected'}
    foreach($child in $children){
        Assert-EnforcedDeadline $Watch
        if($child.PSIsContainer -or ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $child.Name -cnotmatch '^(controller-state\.(json|lock)|\.ha-journal-[a-z0-9_]{8})$'){throw 'journal_inventory_rejected'}
        Assert-EnforcedDescriptor (Get-EnforcedPathDescriptor $child.FullName) $writers 3490513239 $true
        Read-EnforcedJournalGrant $child.FullName $Config.windows_enforced.service_sid
        $null=Get-EnforcedFileIdentity $child.FullName
    }
    Assert-EnforcedDeadline $Watch
}
function Assert-EnforcedParentArgv($Actual,$Config) {
    $cfg=$Config.windows_enforced
    if($Actual.Count -lt 11 -or $Actual[4] -cnotin @('initialize','advance','serve')){throw 'controller_parent_unverified'}
    $expected=@($cfg.python_path,'-I','-B',$cfg.launcher_path,$Actual[4],'--plan',$Plan,'--approval',$Approval)
    $expected+=@('--journal',(Get-EnforcedJournalPath))
    $index=11
    if($Actual.Count -gt $index -and $Actual[$index] -ceq '--arm' -and $Actual[4] -ceq 'serve'){$expected+='--arm'}
    if($Actual.Count -ne $expected.Count){throw 'controller_parent_unverified'}
    for($i=0;$i -lt $Actual.Count;$i++){if($Actual[$i] -cne $expected[$i]){throw 'controller_parent_unverified'}}
}
function Assert-EnforcedApproval($Config,$Approved,[string]$PlanHash) {
    if($Config.windows_enforced.PSObject.Properties['config_profile']){
        $selected=Assert-EnforcedConfigProfile $Config.windows_enforced.config_profile
        if(($selected.acceptance_scope -ceq 'isolated-predeployment' -and ($selected.io_acceptance_sha256 -ceq '' -or $selected.isolation_receipt_sha256 -ceq '')) -or ($selected.acceptance_scope -ceq 'production' -and -not $selected.operational_cutover_accepted)){throw 'managed_config_authority_blocked'}
    }
    # Plan/handshake/response remain schema 2. Only this exact authority
    # envelope carries a non-expiring installed-runtime purpose.
    if(($Approved.schema_version -is [int] -or $Approved.schema_version -is [long]) -and $Approved.schema_version -eq 3){
        Assert-EnforcedKeys $Approved @('schema_version','profile','action','authority_kind','installation_id','installed_at','plan_sha256','source_commit','policy_sha256')
        foreach($field in @('profile','action','authority_kind','installation_id','installed_at','plan_sha256','source_commit','policy_sha256')){
            if($Approved.$field -isnot [string]){throw 'enforced_approval_rejected'}
        }
        if($Config.schema_version -ne 2 -or $Config.profile -cne 'enforced-vm-v1' -or
            $Approved.profile -cne 'enforced-vm-v1' -or $Approved.action -cne 'ha-transition' -or
            $Approved.authority_kind -cne 'installed-runtime' -or
            $Config.installation_id -isnot [string] -or $Config.installation_id -cnotmatch '^[0-9a-f]{64}$' -or
            $Approved.installation_id -cne $Config.installation_id -or
            $Approved.plan_sha256 -cne $PlanHash -or $Approved.source_commit -cne $Config.source_commit -or
            $Approved.policy_sha256 -cne $Config.windows_enforced.policy_sha256 -or
            $Approved.installed_at -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'){throw 'enforced_approval_rejected'}
        $installed=[DateTimeOffset]::Parse($Approved.installed_at,[Globalization.CultureInfo]::InvariantCulture)
        if($installed -ge [DateTimeOffset]::UtcNow){throw 'enforced_approval_rejected'}
        return
    }
    Assert-EnforcedKeys $Approved @('schema_version','profile','plan_sha256','source_commit','policy_sha256','action','expires_at')
    if(($Approved.schema_version -isnot [int] -and $Approved.schema_version -isnot [long]) -or
        $Approved.expires_at -isnot [string] -or $Config.schema_version -ne 2 -or $Config.profile -cne 'enforced-vm-v1' -or
        $Approved.schema_version -ne 2 -or $Approved.profile -cne 'enforced-vm-v1' -or
        $Approved.plan_sha256 -cne $PlanHash -or $Approved.source_commit -cne $Config.source_commit -or
        $Approved.policy_sha256 -cne $Config.windows_enforced.policy_sha256 -or $Approved.action -cne 'ha-transition' -or
        $Approved.expires_at -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$'){throw 'enforced_approval_rejected'}
    $expires=[DateTimeOffset]::Parse($Approved.expires_at)
    if($expires -le [DateTimeOffset]::UtcNow -or $expires -gt [DateTimeOffset]::UtcNow.AddHours(24)){throw 'enforced_approval_rejected'}
}
function Assert-EnforcedConfig($Config) {
    if($Config.schema_version -isnot [int] -and $Config.schema_version -isnot [long]){throw 'enforced_shape_rejected'}
    if($Config.schema_version -ne 2 -or $Config.profile -cne 'enforced-vm-v1' -or $Config.node -cnotin @('A','B') -or
        ($Config.generation -isnot [int] -and $Config.generation -isnot [long]) -or $Config.generation -lt 1 -or
        $Config.cluster -cnotmatch '^[A-Za-z0-9_-]{1,100}$' -or $Config.source_commit -cnotmatch '^[0-9a-f]{40}$'){throw 'enforced_shape_rejected'}
    $cfg=$Config.windows_enforced
     $cfgKeys= @('driver_sha256','adapter_sha256','policy_path','policy_sha256','service_name','service_sid','boot_ticks','session_id','python_path','python_sha256','launcher_path','launcher_sha256','service_image_sha256','vm_id','vm_notes_sha256','vm_config_root','disks')
    if($cfg.PSObject.Properties['config_profile']){$cfgKeys+=@('config_profile');$null=Assert-EnforcedConfigProfile $cfg.config_profile}
    if($cfg.PSObject.Properties['namespace_closure']){$cfgKeys+=@('namespace_closure');$null=Get-EnforcedNamespaceClosureHash $cfg.namespace_closure}
    Assert-EnforcedKeys $cfg $cfgKeys
    foreach($name in @('driver_sha256','adapter_sha256','policy_sha256','session_id','python_sha256','launcher_sha256','service_image_sha256','vm_notes_sha256')){if($cfg.$name -cnotmatch '^[0-9a-f]{64}$'){throw 'enforced_shape_rejected'}}
    if($cfg.service_name -cnotmatch '^[A-Za-z0-9_-]{1,80}$' -or $cfg.service_sid -cnotmatch '^S-1-5-80-(\d+-){4}\d+$' -or
        $cfg.boot_ticks -cnotmatch '^\d{1,20}$' -or ([Guid]::Parse($cfg.vm_id)).ToString() -cne $cfg.vm_id){throw 'enforced_shape_rejected'}
    foreach($path in @($cfg.policy_path,$cfg.python_path,$cfg.launcher_path,$cfg.vm_config_root)){
        if($path -cnotmatch '^[A-Za-z]:\\' -or $path -match '(^|\\)\.\.($|\\)' -or $path.IndexOf([char]0) -ge 0 -or $path.Substring(2).Contains(':')){throw 'enforced_path_rejected'}
    }
    $seen=@{}
    if(@($cfg.disks).Count -lt 1 -or @($cfg.disks).Count -gt 8){throw 'enforced_shape_rejected'}
    foreach($disk in $cfg.disks){
        Assert-EnforcedKeys $disk @('path','disk_id','file_identity_sha256')
        if($disk.path -cnotmatch '^[A-Za-z]:\\' -or $disk.path -match '(^|\\)\.\.($|\\)' -or $disk.path.IndexOf([char]0) -ge 0 -or $disk.path.Substring(2).Contains(':') -or
            $disk.file_identity_sha256 -cnotmatch '^[0-9a-f]{64}$' -or ([Guid]::Parse($disk.disk_id)).ToString() -cne $disk.disk_id -or $seen.ContainsKey($disk.path)){throw 'enforced_shape_rejected'}
        $seen[$disk.path]=$true
    }
}
function Get-EnforcedContext($Config,$Policy,$Watch) {
    $cfg=$Config.windows_enforced
    Assert-EnforcedDeadline $Watch
    $principal=Get-EnforcedCurrentSid
    if($principal -cne $cfg.service_sid -or $cfg.service_sid -cnotmatch '^S-1-5-80-(\d+-){4}\d+$' -or
        [Diagnostics.Process]::GetCurrentProcess().SessionId -ne 0){throw 'provisioned_virtual_service_required'}
    if(([Security.Principal.NTAccount]::new('NT SERVICE\'+$cfg.service_name)).Translate([Security.Principal.SecurityIdentifier]).Value -cne $cfg.service_sid){throw 'service_sid_binding_changed'}
    # A cooperating host admin/kernel/SCM/Hyper-V is explicitly TCB; this is not
    # protection from an EA or other actor with those same administrative rights.
    if($Policy.host_admins_tcb_acknowledged -isnot [bool] -or -not $Policy.host_admins_tcb_acknowledged){throw 'explicit_host_tcb_required'}
    $admins=@(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object {$_.SID.Value}|Sort-Object)
    $hyperv=@(Get-LocalGroupMember -SID 'S-1-5-32-578' -ErrorAction Stop | ForEach-Object {$_.SID.Value}|Sort-Object)
    if(($admins -join ',') -cne (($Policy.trusted_admin_sids|Sort-Object)-join ',') -or
        ($hyperv -join ',') -cne (($Policy.hyperv_member_sids|Sort-Object)-join ',') -or $cfg.service_sid -notin $hyperv){throw 'management_membership_changed'}
    foreach($sid in $hyperv){if($sid -notin @($admins+$cfg.service_sid+'S-1-5-18')){throw 'unsupported_management_principal'}}
    $writers=@('S-1-5-18','S-1-5-32-544')+@($admins)+@($cfg.service_sid)
    $sd=[Security.AccessControl.RawSecurityDescriptor]::new([EnforcedVmNativeV1]::ServiceSecurity($cfg.service_name),0)
    Assert-EnforcedDescriptor $sd $writers (Get-EnforcedServiceControlMask)
    Assert-EnforcedNamespaceLive $Config $Policy $writers $Watch
    $service=Get-CimInstance Win32_Service -Filter ("Name='"+$cfg.service_name+"'") -ErrorAction Stop
    if($service.State -cne 'Running' -or $service.ProcessId -le 0 -or $service.StartName -ine ('NT SERVICE\'+$cfg.service_name)){throw 'provisioned_virtual_service_required'}
    $self=Get-CimInstance Win32_Process -Filter ("ProcessId="+$PID) -ErrorAction Stop
    $parent=Get-CimInstance Win32_Process -Filter ("ProcessId="+$self.ParentProcessId) -ErrorAction Stop
    if($null -eq $parent -or $null -eq $parent.CreationDate -or $parent.CreationDate -gt $self.CreationDate -or
        $parent.ExecutablePath -cne $cfg.python_path){throw 'controller_parent_unverified'}
    Assert-EnforcedParentArgv @(Get-EnforcedArgv $parent.CommandLine) $Config
    # Support a direct controller or one exact pinned SCM service-host parent.
    # Unknown additional wrappers are unsupported provisioning, not trusted.
    $hostProcess=$parent
    if($parent.ProcessId -ne $service.ProcessId){
        if($parent.ParentProcessId -ne $service.ProcessId){throw 'fixed_service_controller_required'}
        $hostProcess=Get-CimInstance Win32_Process -Filter ("ProcessId="+$service.ProcessId) -ErrorAction Stop
        if($null -eq $hostProcess -or $null -eq $hostProcess.CreationDate -or $hostProcess.CreationDate -gt $parent.CreationDate){throw 'fixed_service_controller_required'}
    }
    if((Read-EnforcedCodeHash $hostProcess.ExecutablePath $Watch) -cne $cfg.service_image_sha256){throw 'service_host_changed'}
    $serviceArgv=@(Get-EnforcedArgv $service.PathName)
    if($serviceArgv.Count -lt 1 -or $serviceArgv[0] -cne $hostProcess.ExecutablePath){throw 'service_image_unverified'}
    foreach($pair in @(@($cfg.python_path,$cfg.python_sha256),@($cfg.launcher_path,$cfg.launcher_sha256))){
        if((Read-EnforcedCodeHash $pair[0] $Watch) -cne $pair[1]){throw 'controller_source_changed'}
    }
    $boot=(Get-CimInstance Win32_OperatingSystem -Property LastBootUpTime -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks.ToString()
    if($boot -cne $cfg.boot_ticks){throw 'host_boot_changed'}
    Assert-EnforcedDeadline $Watch
    return @{principal_sha256=(Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes($principal)));boot_ticks=$boot;
        service_pid=[long]$service.ProcessId;controller_pid=[long]$parent.ProcessId;
        controller_started=$parent.CreationDate.ToUniversalTime().Ticks.ToString();host_started=$hostProcess.CreationDate.ToUniversalTime().Ticks.ToString()}
}
function Get-EnforcedVmStamp($Vm) {
    return [ordered]@{id=$Vm.Id.ToString();generation=[int]$Vm.Generation;start=$Vm.AutomaticStartAction.ToString();
        location=[string]$Vm.ConfigurationLocation;notes=[string]$Vm.Notes;state=$Vm.State.ToString()}
}
function Assert-EnforcedNoCapability([string]$Path){
    $sd=Get-EnforcedPathDescriptor $Path
    foreach($ace in $sd.DiscretionaryAcl){if($ace -is [Security.AccessControl.KnownAce] -and $ace.SecurityIdentifier.Value.StartsWith('S-1-15-',[StringComparison]::Ordinal)){throw 'capability_outside_config_rejected'}}
}
function Assert-EnforcedManagedAce([string]$Sid,[long]$Mask,[string[]]$Readers,[string]$Capability,[string]$ServiceSid){
    if($Sid -notin $Readers -or ($Sid -ceq $Capability -and $Mask -ne 2032127) -or
        ($Sid -ceq $ServiceSid -and (($Mask -band 4294967295) -band (4294967295 -bxor 1179785)) -ne 0)){throw 'managed_config_role_rejected'}
}
function Assert-EnforcedServiceMetadata($Descriptor,[string]$ServiceSid,[int]$Mask){
 $count=0
 foreach($ace in $Descriptor.DiscretionaryAcl){if($ace.SecurityIdentifier.Value -ceq $ServiceSid){
  if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback -or $ace.AceQualifier -ne 'AccessAllowed' -or [int]$ace.AceFlags -ne 0 -or $ace.AccessMask -ne $Mask){throw 'service_metadata_role_rejected'};$count++
 }}
 if($count -ne 1){throw 'service_metadata_role_missing_or_duplicate'}
}
function Test-EnforcedObservedMetadata($Config){return ($Config.windows_enforced.PSObject.Properties['namespace_closure'] -and $Config.windows_enforced.namespace_closure.version -ceq 'direct-children-observed-v1')}
function Get-EnforcedConfigCapability{return [EnforcedVmNativeV1]::VmWorkerCapability()}
function Assert-EnforcedManagedConfig($Config,$Policy,$Watch){
    $cfg=$Config.windows_enforced;$profile=Assert-EnforcedConfigProfile $cfg.config_profile
    if($Config.installation_id -isnot [string] -or $Config.installation_id -cnotmatch '^[0-9a-f]{64}$'){throw 'managed_config_installation_required'}
    $name='N100HA_'+$Config.installation_id.Substring(0,16)+'_'+$Config.node
    $privateRoot=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) ('N100HA_'+$Config.installation_id.Substring(0,24))
    $expected=Join-Path (Join-Path $privateRoot ('guest-'+$Config.node)) $name
    if($cfg.vm_config_root -ine $expected){throw 'managed_config_scope_rejected'}
    if((Get-EnforcedConfigCapability) -cne $profile.capability_sid){throw 'config_capability_derivation_changed'}
    $base=@('S-1-5-18','S-1-5-32-544')+@($Policy.trusted_admin_sids)
    $writers=$base+@($profile.capability_sid);$readers=$writers+@($cfg.service_sid)
    $root=Get-Item -LiteralPath $cfg.vm_config_root -Force
    $parent=if($root -is [IO.DirectoryInfo]){$root.Parent}else{$root.Directory}
    if($null -eq $parent){throw 'managed_config_parent_required'}
    Assert-EnforcedPath $parent.FullName $base
    Assert-EnforcedNoCapability $parent.FullName
    if(Test-EnforcedObservedMetadata $Config){Assert-EnforcedServiceMetadata (Get-EnforcedPathDescriptor $parent.FullName) $cfg.service_sid 131232}
    $queue=[Collections.Generic.Queue[string]]::new();$queue.Enqueue($root.FullName);$seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    while($queue.Count){
        Assert-EnforcedDeadline $Watch
        $entry=Get-Item -LiteralPath ($queue.Dequeue()) -Force
        if(-not $seen.Add($entry.FullName) -or $seen.Count -gt 4096 -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'managed_config_inventory_rejected'}
        $sd=Get-EnforcedPathDescriptor $entry.FullName
        if(Test-EnforcedObservedMetadata $Config){Assert-EnforcedServiceMetadata $sd $cfg.service_sid $(if($entry -is [IO.DirectoryInfo]){131233}else{131200})}
        if($sd.Owner.Value -notin $base){throw 'managed_config_owner_rejected'}
        foreach($ace in $sd.DiscretionaryAcl){
            if($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback){throw 'managed_config_ace_rejected'}
            if($ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed){throw 'managed_config_ace_rejected'}
            Assert-EnforcedManagedAce $ace.SecurityIdentifier.Value ([long]$ace.AccessMask) $readers $profile.capability_sid $cfg.service_sid
        }
        Assert-EnforcedDescriptor $sd $writers 1343029590
        Assert-EnforcedDescriptor $sd $readers 2415919105 $true
        if($entry -is [IO.DirectoryInfo]){
            $children=@(Get-ChildItem -LiteralPath $entry.FullName -Force|Select-Object -First 4096)
            if($seen.Count+$queue.Count+$children.Count -gt 4096){throw 'managed_config_inventory_limit'}
            foreach($child in $children){$queue.Enqueue($child.FullName)}
        }
    }
    Assert-EnforcedDeadline $Watch
}

function Get-EnforcedVmSnapshot($Config,$Policy,$Watch) {
    $cfg=$Config.windows_enforced
    $context=Get-EnforcedContext $Config $Policy $Watch
    $vm=@(Get-VM -Id ([Guid]::Parse($cfg.vm_id)) -ErrorAction Stop)
    if($vm.Count -ne 1){throw 'fixed_vm_identity_changed'}
    $before=Get-EnforcedVmStamp $vm[0]
    if($cfg.PSObject.Properties['config_profile'] -and ($Config.installation_id -isnot [string] -or $Config.installation_id -cnotmatch '^[0-9a-f]{64}$' -or $vm[0].Name -cne ('N100HA_'+$Config.installation_id.Substring(0,16)+'_'+$Config.node) -or -not $vm[0].Notes.StartsWith('ha-owned-guest-v1:'+$Config.installation_id+':',[StringComparison]::Ordinal))){throw 'managed_config_owned_vm_required'}
    if($before.id -cne $cfg.vm_id -or $before.generation -ne 2 -or $before.start -cne 'Nothing' -or $before.location -ine $cfg.vm_config_root -or
        (Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes($before.notes))) -cne $cfg.vm_notes_sha256){throw 'fixed_vm_identity_changed'}
    $vmSid=Get-EnforcedVmSid $cfg.vm_id
    $dataWriters=@('S-1-5-18','S-1-5-32-544')+@($Policy.trusted_admin_sids)+@($vmSid)
    $dataReaders=$dataWriters+@($cfg.service_sid)
    if($cfg.PSObject.Properties['config_profile']){Assert-EnforcedManagedConfig $Config $Policy $Watch}
    else{Assert-EnforcedPath $cfg.vm_config_root $dataWriters;Assert-EnforcedDataReaders $cfg.vm_config_root $dataReaders $dataWriters $Watch $true}
    $actual=@(Get-VMHardDiskDrive -VM $vm[0] -ErrorAction Stop|ForEach-Object {$_.Path.ToLowerInvariant()}|Sort-Object)
    $expected=@($cfg.disks|ForEach-Object {$_.path.ToLowerInvariant()}|Sort-Object)
    if(($actual -join "`n") -cne ($expected -join "`n")){throw 'vhd_inventory_changed'}
    foreach($disk in $cfg.disks){
        if($cfg.PSObject.Properties['config_profile']){Assert-EnforcedNoCapability $disk.path}
        Assert-EnforcedPath $disk.path $dataWriters
        Assert-EnforcedDataReaders $disk.path $dataReaders $dataWriters $Watch
        if(Test-EnforcedObservedMetadata $Config){Assert-EnforcedServiceMetadata (Get-EnforcedPathDescriptor $disk.path) $cfg.service_sid 131200}
        $vhd=Get-VHD -Path $disk.path -ErrorAction Stop
        if((ConvertTo-EnforcedDiskIdentifier $vhd.DiskIdentifier) -cne $disk.disk_id -or -not [string]::IsNullOrEmpty($vhd.ParentPath)){throw 'vhd_identity_changed'}
        $identity=Get-EnforcedHash ([Text.Encoding]::UTF8.GetBytes((Get-EnforcedFileIdentity $disk.path)))
        if($identity -cne $disk.file_identity_sha256){throw 'vhd_file_changed'}
        Assert-EnforcedDeadline $Watch
    }
    # Detect observed VM/context replacement across the bounded read. These are
    # two live reads within this request, never a reused prior observation.
    $after=@(Get-VM -Id ([Guid]::Parse($cfg.vm_id)) -ErrorAction Stop)
    if($after.Count -ne 1 -or ((Get-EnforcedVmStamp $after[0])|ConvertTo-Json -Compress) -cne ($before|ConvertTo-Json -Compress)){throw 'vm_changed_during_observation'}
    $afterDisks=@(Get-VMHardDiskDrive -VM $after[0] -ErrorAction Stop|ForEach-Object {$_.Path.ToLowerInvariant()}|Sort-Object)
    if(($afterDisks -join "`n") -cne ($expected -join "`n")){throw 'vhd_inventory_changed'}
    $afterContext=Get-EnforcedContext $Config $Policy $Watch
    if(($afterContext|ConvertTo-Json -Depth 4 -Compress) -cne ($context|ConvertTo-Json -Depth 4 -Compress)){throw 'controller_changed_during_observation'}
    Assert-EnforcedDeadline $Watch
    return @{fenced=($before.state -ceq 'Off');context=$context}
}
function Get-EnforcedResponse($Config,$Approved,[string]$PlanHash,[string]$ApprovalHash,[string]$Instance,$Request,$Watch) {
    Assert-EnforcedConfig $Config
    Assert-EnforcedApproval $Config $Approved $PlanHash
    Assert-EnforcedKeys $Request @('action','node','generation','nonce','broker_instance','sequence')
    if($Request.action -cnotin @('observe','fence') -or $Request.node -cne $Config.node -or
        ($Request.generation -isnot [int] -and $Request.generation -isnot [long]) -or
        $Request.generation -ne $Config.generation -or $Request.nonce -cnotmatch '^[0-9a-f]{64}$' -or
        $Request.broker_instance -cne $Instance){throw 'enforced_request_rejected'}
    Assert-EnforcedDeadline $Watch
    Assert-EnforcedSequence $Request $Instance
    $cfg=$Config.windows_enforced
    if((Read-EnforcedCodeHash $PSCommandPath $Watch) -cne $cfg.driver_sha256 -or
        (Read-EnforcedCodeHash (Join-Path $PSScriptRoot 'enforced_windows.py') $Watch) -cne $cfg.adapter_sha256){throw 'running_bundle_changed'}
    $planRead=Read-EnforcedPrivate $Plan $Watch;$approvalRead=Read-EnforcedPrivate $Approval $Watch
    if($planRead.sha256 -cne $PlanHash -or $approvalRead.sha256 -cne $ApprovalHash){throw 'approval_bytes_changed'}
    $policyRead=Read-EnforcedPrivate $cfg.policy_path $Watch
    if($policyRead.sha256 -cne $cfg.policy_sha256){throw 'enforcement_policy_changed'}
    $policy=Convert-EnforcedJson ([Text.Encoding]::UTF8.GetString($policyRead.bytes))
    $policyKeys=@('schema_version','profile','cluster','generation','session_id','service_sid','boot_ticks','vm_id','vm_notes_sha256','vm_config_root','disks','trusted_admin_sids','hyperv_member_sids','host_admins_tcb_acknowledged')
    if($cfg.PSObject.Properties['config_profile']){
        $policyKeys+=@('config_profile');if((Get-EnforcedConfigProfileHash $policy.config_profile) -cne (Get-EnforcedConfigProfileHash $cfg.config_profile)){throw 'config_profile_policy_changed'}
        foreach($path in @($Plan,$Approval,$cfg.policy_path,$cfg.python_path,$cfg.launcher_path,$PSCommandPath,(Join-Path $PSScriptRoot 'enforced_windows.py'))){Assert-EnforcedNoCapability $path;$entry=Get-Item -LiteralPath $path -Force;$parent=if($entry -is [IO.DirectoryInfo]){$entry.Parent}else{$entry.Directory};if($null -eq $parent){throw 'config_profile_private_parent_required'};Assert-EnforcedNoCapability $parent.FullName}
    }
    if($cfg.PSObject.Properties['namespace_closure']){$policyKeys+=@('namespace_closure');if((Get-EnforcedNamespaceClosureHash $policy.namespace_closure) -cne (Get-EnforcedNamespaceClosureHash $cfg.namespace_closure)){throw 'namespace_closure_policy_changed'}}
    Assert-EnforcedKeys $policy $policyKeys
    foreach($name in @('session_id','service_sid','boot_ticks','vm_id','vm_notes_sha256','vm_config_root')){if($policy.$name -cne $cfg.$name){throw 'policy_binding_changed'}}
    if($policy.profile -cne 'enforced-vm-v1' -or $policy.schema_version -ne 1 -or $policy.cluster -cne $Config.cluster -or $policy.generation -ne $Config.generation -or
        ($policy.disks|ConvertTo-Json -Depth 5 -Compress) -cne ($cfg.disks|ConvertTo-Json -Depth 5 -Compress)){throw 'policy_binding_changed'}
    Assert-EnforcedJournal $Config $policy $Watch
    $pgFiles=Get-EnforcedPgFiles $Config $policy $Watch
    $snapshot=Get-EnforcedVmSnapshot $Config $policy $Watch
    if($Request.action -ceq 'fence' -and -not $snapshot.fenced){
        # The only mutating API: exact approved GUID. Lost responses require a
        # new observe; no retries, VM creation, start, checkpoints or host reboot.
        Assert-EnforcedApproval $Config $Approved $PlanHash
        Assert-EnforcedDeadline $Watch
        Stop-VM -Id ([Guid]::Parse($cfg.vm_id)) -TurnOff -Force -Confirm:$false -ErrorAction Stop
        $Watch.Restart()
        $snapshot=Get-EnforcedVmSnapshot $Config $policy $Watch
    }
    Assert-EnforcedApproval $Config $Approved $PlanHash
    Assert-EnforcedJournal $Config $policy $Watch
    if((Get-EnforcedPgFiles $Config $policy $Watch) -cne $pgFiles){throw 'pg_private_file_changed'}
    foreach($binding in @(@($Plan,$PlanHash),@($Approval,$ApprovalHash),@($cfg.policy_path,$cfg.policy_sha256))){
        if((Read-EnforcedPrivate $binding[0] $Watch).sha256 -cne $binding[1]){throw 'authority_bytes_changed'}
    }
    Assert-EnforcedDeadline $Watch
    $response=[ordered]@{schema_version=2;profile='enforced-vm-v1';scope='vm-enforcement-only';status='ok';
        plan_sha256=$PlanHash;approval_sha256=$ApprovalHash;source_sha256=$cfg.driver_sha256;policy_sha256=$cfg.policy_sha256;
        session_id=$cfg.session_id;principal_sha256=$snapshot.context.principal_sha256;boot_ticks=$snapshot.context.boot_ticks;
        broker_instance=$Instance;nonce=$Request.nonce;sequence=[long]$Request.sequence;node=$Config.node;generation=$Config.generation;vm_id=$cfg.vm_id;fenced=[bool]$snapshot.fenced}
    if($cfg.PSObject.Properties['namespace_closure']){$response.namespace_closure_sha256=Get-EnforcedNamespaceClosureHash $cfg.namespace_closure}
    if($cfg.PSObject.Properties['config_profile']){$response.config_profile=$cfg.config_profile.name;$response.config_profile_sha256=Get-EnforcedConfigProfileHash $cfg.config_profile;$response.config_profile_definition_sha256=$cfg.config_profile.definition_sha256;$response.config_acceptance_scope=$cfg.config_profile.acceptance_scope;$response.operational_cutover_accepted=$cfg.config_profile.operational_cutover_accepted}
    return $response
}

if($MyInvocation.InvocationName -cne '.'){
    try{
        if(-not $Go -or [Environment]::OSVersion.Platform -ne 'Win32NT' -or [IntPtr]::Size -ne 8){throw 'native_enforced_service_required'}
        Initialize-EnforcedNative
        $script:enforcedSequence=[long]0
        $watch=[Diagnostics.Stopwatch]::StartNew()
        $planRead=Read-EnforcedPrivate $Plan $watch;$approvedRead=Read-EnforcedPrivate $Approval $watch
        $config=Convert-EnforcedJson ([Text.Encoding]::UTF8.GetString($planRead.bytes))
        $approved=Convert-EnforcedJson ([Text.Encoding]::UTF8.GetString($approvedRead.bytes))
        Assert-EnforcedConfig $config
        Assert-EnforcedApproval $config $approved $planRead.sha256
        $own=Read-EnforcedPrivate $PSCommandPath $watch
        if($own.sha256 -cne $config.windows_enforced.driver_sha256){throw 'driver_source_changed'}
        $instance=([Guid]::NewGuid().ToString('N')+[Guid]::NewGuid().ToString('N'))
        $nonce=New-EnforcedNonce 1
        $request=[pscustomobject]@{action='observe';node=$config.node;generation=$config.generation;nonce=$nonce;broker_instance=$instance;sequence=[long]1}
        $snapshot=Get-EnforcedResponse $config $approved $planRead.sha256 $approvedRead.sha256 $instance $request $watch
        $hello=[ordered]@{}
        foreach($key in @('schema_version','profile','scope','plan_sha256','approval_sha256','source_sha256','policy_sha256','session_id','principal_sha256','boot_ticks','broker_instance')){$hello[$key]=$snapshot[$key]}
        if($snapshot.Contains('config_profile')){foreach($key in @('config_profile','config_profile_sha256','config_profile_definition_sha256','config_acceptance_scope','operational_cutover_accepted')){$hello[$key]=$snapshot[$key]}}
        if($snapshot.Contains('namespace_closure_sha256')){$hello.namespace_closure_sha256=$snapshot.namespace_closure_sha256}
        $hello.guard='ready';$hello.sequence=[long]1;$hello|ConvertTo-Json -Depth 4 -Compress
        while($null -ne ($line=[Console]::In.ReadLine())){
            $watch=[Diagnostics.Stopwatch]::StartNew()
            if($line.Length -gt 4096){throw 'request_limit'}
            $request=Convert-EnforcedJson $line
            Get-EnforcedResponse $config $approved $planRead.sha256 $approvedRead.sha256 $instance $request $watch|ConvertTo-Json -Depth 5 -Compress
        }
    }catch{[Console]::Out.WriteLine('{"schema_version":2,"profile":"enforced-vm-v1","status":"blocked"}');exit 1}
}
