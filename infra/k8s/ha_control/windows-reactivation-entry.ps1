param(
    [Parameter(Mandatory=$true)][string]$DriverPath,
    [Parameter(Mandatory=$true)][string]$DriverSha256,
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][string]$PlanSha256,
    [Parameter(Mandatory=$true)][string]$AttemptId
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-HaPinnedInput([string]$Path, [string]$Digest) {
    if ($Digest -cnotmatch '^[a-f0-9]{64}$' -or $Path -notmatch '^[A-Za-z]:\\') {
        throw 'legacy_native_configuration_invalid'
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item -isnot [IO.FileInfo] -or $item.Length -gt 1048576) { throw 'legacy_native_configuration_invalid' }
    $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
    $cursor = $item
    while ($null -ne $cursor) {
        if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'legacy_native_configuration_invalid'
        }
        # FileInfo.Directory and DirectoryInfo.Parent return .NET objects
        # without the PowerShell provider's PSIsContainer property.
        if ($cursor -is [IO.DirectoryInfo]) {
            $cursor = $cursor.Parent
        } elseif ($cursor -is [IO.FileInfo]) {
            $cursor = $cursor.Directory
        } else {
            throw 'legacy_native_configuration_invalid'
        }
    }
    foreach ($member in @($item, $item.Directory)) {
        $acl = Get-Acl -LiteralPath $member.FullName
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) { throw 'legacy_native_configuration_owner' }
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) {
                throw 'legacy_native_configuration_acl'
            }
        }
    }
    $file = [IO.File]::Open($item.FullName, 'Open', 'Read', 'Read')
    try {
        if ($file.Length -gt 1048576) { throw 'legacy_native_configuration_invalid' }
        $memory = New-Object IO.MemoryStream
        try {
            $file.CopyTo($memory)
            $bytes = $memory.ToArray()
        } finally { $memory.Dispose() }
    } finally { $file.Dispose() }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $actual = [BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    if ($actual -cne $Digest) { throw 'legacy_native_configuration_digest' }
    return [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF)
}

$configuration = (Read-HaPinnedInput $PlanPath $PlanSha256) | ConvertFrom-Json
if ($configuration.windows_driver_sha256 -cne $DriverSha256) { throw 'legacy_native_configuration_digest' }
$driver = [scriptblock]::Create((Read-HaPinnedInput $DriverPath $DriverSha256))
. $driver
Invoke-HaReactivationSession $configuration.windows_reactivation $PlanSha256 $AttemptId
