param(
    [Parameter(Mandatory=$true)][string]$ServiceName,
    [switch]$Provision
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($ServiceName -cnotmatch '^N100HA_[0-9a-f]{24}_controller$') {
    throw 'cutover_authority_service_identity'
}
$service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -OperationTimeoutSec 5
if ($null -eq $service -or $service.Name -cne $ServiceName) { throw 'cutover_authority_service_missing' }
$controller = [Security.Principal.NTAccount]::new('NT SERVICE', $ServiceName).
    Translate([Security.Principal.SecurityIdentifier])
$admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
if ($programData -ine 'C:\ProgramData') { throw 'cutover_authority_wsl_alias_unverified' }
$target = Join-Path $programData 'N100HA-Cutover'
$directoryAcl = [Security.AccessControl.DirectorySecurity]::new()
$directoryAcl.SetOwner($admins)
$directoryAcl.SetAccessRuleProtection($true, $false)
$allow = [Security.AccessControl.AccessControlType]::Allow
$both = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$none = [Security.AccessControl.PropagationFlags]::None
foreach ($writer in @($admins, $system)) {
    $directoryAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $writer, 'FullControl', $both, $none, $allow))
}
$directoryAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $controller, 'ReadAndExecute', $both, $none, $allow))
# Legacy launcher principals need to enumerate this one directory only.
$directoryAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $users, 'ReadAndExecute', [Security.AccessControl.InheritanceFlags]::None, $none, $allow))

$danger = [Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
foreach ($path in @('C:\', $programData)) {
    $parent = Get-Item -LiteralPath $path -Force
    if (-not $parent.PSIsContainer -or ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'cutover_authority_parent_untrusted'
    }
    $unexpected = @((Get-Acl -LiteralPath $path).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.AccessControlType -eq $allow -and $_.IdentityReference -notin @($admins, $system) -and
            -not ($_.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
            ($_.FileSystemRights -band $danger) })
    if ($unexpected.Count -ne 0) { throw 'cutover_authority_parent_replaceable' }
}
try { $item = Get-Item -LiteralPath $target -Force -ErrorAction Stop }
catch [System.Management.Automation.ItemNotFoundException] {
    if (-not $Provision) { throw 'cutover_authority_directory_missing' }
    $null = [IO.Directory]::CreateDirectory($target, $directoryAcl)
    $item = Get-Item -LiteralPath $target -Force
}
if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'cutover_authority_directory_untrusted'
}
$actual = Get-Acl -LiteralPath $target
$sections = [Security.AccessControl.AccessControlSections]'Owner,Access'
if ($actual.GetSecurityDescriptorSddlForm($sections) -cne $directoryAcl.GetSecurityDescriptorSddlForm($sections)) {
    throw 'cutover_authority_acl_unverified'
}
if (@(Get-ChildItem -LiteralPath $target -Force).Count -ne 0) {
    throw 'cutover_authority_directory_not_empty'
}
[ordered]@{directory_ready=$true;authority_marker_present=$false;
           wsl_alias_bound=$true;service_identity_bound=$true} | ConvertTo-Json -Compress
