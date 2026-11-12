# Prepare the initial Windows observer and one installed controller approval.
param([string]$InputPath, [string]$InputSha256, [string]$IntentBase64,
      [string]$RuntimeSha256, [switch]$Preflight, [switch]$Go)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$encoding = [Text.UTF8Encoding]::new($false,$true)
$hashPattern = '\A[a-f0-9]{64}\z'
$writers = @('S-1-5-32-544','S-1-5-18')
$sections = [Security.AccessControl.AccessControlSections]'Owner,Access'

function Digest([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-Pin($Value) {
    if ($Value -isnot [string] -or $Value -cnotmatch $hashPattern) { throw 'initial_stage_pin' }
}

function Assert-One($Value) {
    if (($Value -isnot [int] -and $Value -isnot [long]) -or $Value -ne 1) { throw 'initial_stage_generation' }
}

function Assert-Fields($Value, [string[]]$Fields) {
    if ($Value -isnot [pscustomobject] -or @($Value.PSObject.Properties).Count -ne $Fields.Count -or
        @($Value.PSObject.Properties.Name | Where-Object { $_ -cnotin $Fields }).Count -ne 0) {
        throw 'initial_stage_fields'
    }
}

function Assert-LocalPath([string]$Path) {
    if ($Path -cnotmatch '\AC:\\[A-Za-z0-9_.\\-]+\z' -or $Path -match '\\\.\.?(\\|$)' -or
        $Path.EndsWith('.') -or [IO.Path]::GetFullPath($Path) -cne $Path) { throw 'initial_stage_path' }
}

function Assert-ReservedLeaf([string]$Path, [string]$Root, [string]$Name = '') {
    Assert-LocalPath $Path
    if ([IO.Path]::GetDirectoryName($Path) -cne $Root -or ($Name -cne '' -and [IO.Path]::GetFileName($Path) -cne $Name)) {
        throw 'initial_stage_reserved_path'
    }
}

function Assert-Access($Entry, [bool]$Leaf, [string]$Reader, [string]$LeafWriter, [bool]$RequireReader) {
    $allowedWriters = if ($LeafWriter -ceq '') { $writers } else { $writers + @($LeafWriter) }
    if ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'initial_stage_path' }
    $sd = [Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $Entry.FullName).Sddl)
    $osRoot = $Entry.FullName -ceq 'C:\' -and $sd.Owner.Value -ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
    if (($sd.Owner.Value -cnotin $allowedWriters -and -not $osRoot) -or $null -eq $sd.DiscretionaryAcl -or
        $sd.DiscretionaryAcl.Count -eq 0) { throw 'initial_stage_acl' }
    $mask = if ($Leaf) { 0x500d0156 } else { 0x100d0040 }
    $readerAccess = 0L
    $directoryRead = 0x20080L # READ_CONTROL and READ_ATTRIBUTES required by the native gate.
    foreach ($ace in $sd.DiscretionaryAcl) {
        if ($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback) { throw 'initial_stage_acl' }
        $inheritedOnly = ([int]$ace.AceFlags -band 8) -ne 0
        if ($RequireReader -and -not $inheritedOnly) {
            if ($ace.AceQualifier -eq 'AccessDenied' -and ([long]$ace.AccessMask -band (0xf0000000L -bor $directoryRead))) {
                throw 'initial_stage_acl'
            }
            if ($ace.AceQualifier -eq 'AccessAllowed' -and $ace.SecurityIdentifier.Value -ceq $Reader) {
                $readerAccess = $readerAccess -bor ([long]$ace.AccessMask)
            }
        }
        if ($ace.AceQualifier -ne 'AccessAllowed' -or $ace.SecurityIdentifier.Value -cin $allowedWriters) { continue }
        if (($Leaf -or -not $inheritedOnly) -and ([long]$ace.AccessMask -band $mask)) { throw 'initial_stage_acl' }
        if ($Leaf -and -not $inheritedOnly -and $Reader -cne '' -and $ace.SecurityIdentifier.Value -cne $Reader -and
            ([long]$ace.AccessMask -band 0x90000001L)) { throw 'initial_stage_acl' }
    }
    if ($RequireReader -and ($readerAccess -band 0x80000000L) -eq 0 -and
        ($readerAccess -band $directoryRead) -ne $directoryRead) { throw 'initial_stage_acl' }
}

function Assert-ProtectedPath($Entry, [string]$Reader = '', [string]$TrustedWriter = '', [switch]$LeafOnly,
                              [switch]$RequireReader) {
    $depth = 0
    for ($item = $Entry; $null -ne $item; $item = $(if ($item -is [IO.FileInfo]) { $item.Directory } else { $item.Parent })) {
        $allowed = if (-not $LeafOnly -or $depth -eq 0) { $TrustedWriter } else { '' }
        Assert-Access $item ($depth -le 1) $Reader $allowed ($RequireReader -and $depth -eq 0)
        $depth++
    }
}

function File-Identity([string]$Path) {
    $links = @(& $fsutil hardlink list $Path)
    if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw 'initial_stage_file_identity' }
    $identity = @(& $fsutil file queryfileid $Path)
    if ($LASTEXITCODE -ne 0 -or $identity.Count -ne 1 -or $identity[0] -notmatch '0x[0-9a-fA-F]{16,32}') {
        throw 'initial_stage_file_identity'
    }
    return $Matches[0].ToLowerInvariant()
}

function Read-Protected([string]$Path, [string]$Pin, [int]$Limit, [string]$Reader = '',
                        [string]$TrustedWriter = '', [switch]$LeafOnly) {
    Assert-LocalPath $Path
    if ($Pin -cne '') { Assert-Pin $Pin }
    $file = Get-Item -LiteralPath $Path -Force
    if ($file -isnot [IO.FileInfo] -or $file.Length -lt 1 -or $file.Length -gt $Limit) { throw 'initial_stage_file' }
    Assert-ProtectedPath $file $Reader $TrustedWriter -LeafOnly:$LeafOnly
    $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    try {
        $identity = File-Identity $Path
        if ($stream.Length -lt 1 -or $stream.Length -gt $Limit) { throw 'initial_stage_file' }
        $readerStream = [IO.BinaryReader]::new($stream,$encoding,$true)
        try {
            $raw = $readerStream.ReadBytes([int]$stream.Length)
            if ($raw.Length -ne $stream.Length -or $stream.ReadByte() -ne -1 -or
                ($Pin -cne '' -and (Digest $raw) -cne $Pin) -or (File-Identity $Path) -cne $identity) {
                throw 'initial_stage_file_pin'
            }
            Assert-ProtectedPath (Get-Item -LiteralPath $Path -Force) $Reader $TrustedWriter -LeafOnly:$LeafOnly
            return ,$raw
        } finally { $readerStream.Dispose() }
    } finally { $stream.Dispose() }
}

function Assert-Absent([string]$Path) {
    $parent = [IO.Path]::GetDirectoryName($Path)
    $leaf = [IO.Path]::GetFileName($Path)
    if (@(Get-ChildItem -LiteralPath $parent -Force | Where-Object { $_.Name -ieq $leaf }).Count -ne 0) {
        throw 'initial_stage_reconciliation_required'
    }
}

function Json-Bytes($Value) {
    return ,($encoding.GetBytes((ConvertTo-Json -InputObject $Value -Depth 90 -Compress) + "`n"))
}

function File-Access([string]$Reader = '') {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $admins = [Security.Principal.SecurityIdentifier]::new($writers[0])
    $acl.SetOwner($admins)
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($writer in $writers) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($writer),'FullControl','Allow'))
    }
    if ($Reader -cne '') {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($Reader),'Read','Allow'))
    }
    return $acl
}

function Write-New([string]$Path,[byte[]]$Raw,[string]$Reader = '') {
    $acl = File-Access $Reader
    $file = [IO.FileStream]::new($Path,[IO.FileMode]::CreateNew,
        [Security.AccessControl.FileSystemRights]'WriteData,Synchronize',[IO.FileShare]::None,4096,
        [IO.FileOptions]::WriteThrough,$acl)
    try { $file.Write($Raw,0,$Raw.Length); $file.Flush($true) }
    finally { $file.Dispose() }
    $pin = Digest $Raw
    $verified = Read-Protected $Path $pin $Raw.Length $Reader
    [Array]::Clear($verified,0,$verified.Length)
    if ((Get-Acl -LiteralPath $Path).GetSecurityDescriptorSddlForm($sections) -cne $acl.GetSecurityDescriptorSddlForm($sections)) {
        throw 'initial_stage_published_acl'
    }
    return $pin
}

function Assert-EmptyJournal([string]$Root,[string]$ServiceSid) {
    $path = Join-Path $Root 'journal'
    $journal = Get-Item -LiteralPath $path -Force
    if ($journal -isnot [IO.DirectoryInfo] -or ($journal.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $path -Force).Count -ne 0) { throw 'initial_stage_journal' }
    $sd = [Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $path).Sddl)
    $allowed = $writers + @($ServiceSid)
    if ($sd.Owner.Value -cnotin $allowed -or $null -eq $sd.DiscretionaryAcl) { throw 'initial_stage_journal' }
    $granted = 0L
    $required = 0x1301bfL
    foreach ($ace in $sd.DiscretionaryAcl) {
        if ($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback) { throw 'initial_stage_journal' }
        if ([int]$ace.AceFlags -band 8) { continue }
        $mask = [long]$ace.AccessMask -band 0xffffffffL
        if ($ace.AceQualifier -eq 'AccessDenied' -and ($mask -band (0xf00d0157L -bor $required))) {
            throw 'initial_stage_journal'
        }
        if ($ace.AceQualifier -eq 'AccessAllowed') {
            if (($mask -band 0xd00d0157L) -and $ace.SecurityIdentifier.Value -cnotin $allowed) {
                throw 'initial_stage_journal'
            }
            if ($ace.SecurityIdentifier.Value -ceq $ServiceSid) { $granted = $granted -bor $mask }
        }
    }
    if (($granted -band 0x10000000) -eq 0 -and ($granted -band $required) -ne $required) { throw 'initial_stage_journal' }
}

function Assert-ManualHost($Scope, $Start) {
    $service = Get-CimInstance Win32_Service -Filter ("Name='" + $Scope.service_name + "'") -OperationTimeoutSec 5
    if ($null -eq $service -or $service.Name -cne $Scope.service_name -or $service.State -cne 'Stopped' -or
        $service.StartMode -cne 'Manual' -or $service.ProcessId -ne 0 -or $service.ServiceType -cne 'Own Process' -or
        $service.DesktopInteract -or $service.StartName -cne ('NT SERVICE\' + $Scope.service_name) -or
        $service.PathName -cnotmatch '\A"[^"\r\n]+"\z' -or
        (Digest ($encoding.GetBytes($service.PathName))) -cne $Start.service_image_path_sha256) { throw 'initial_stage_service' }
    $hostPath = $service.PathName.Substring(1,$service.PathName.Length - 2)
    if ([IO.Path]::GetFileName($hostPath) -cne 'windows-service-host.exe') { throw 'initial_stage_service' }
    $null = Read-Protected $hostPath $Scope.service_image_sha256 67108864
    $hostRoot = [IO.Path]::GetDirectoryName($hostPath)
    $existing = @(Get-ChildItem -LiteralPath $hostRoot -Force)
    if ($existing.Count -ne 1 -or $existing[0].Name -cne 'windows-service-host.exe') { throw 'initial_stage_service' }
}

try {
    if ($Go.IsPresent -eq $Preflight.IsPresent -or $PSVersionTable.PSEdition -cne 'Desktop' -or
        -not [Environment]::Is64BitProcess -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'initial_stage_scope'
    }
    if ($Preflight -and ($IntentBase64 -cne '' -or $RuntimeSha256 -cne '')) { throw 'initial_stage_scope' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
            $identity.User.Value -cin $writers) { throw 'initial_stage_operator' }
        $operatorSid = $identity.User.Value
    } finally { $identity.Dispose() }
    $fsutil = Join-Path ([Environment]::SystemDirectory) 'fsutil.exe'
    Assert-Pin $InputSha256
    if ($InputPath -cnotmatch '\AC:\\ProgramData\\N100HA-Cutover-Inputs\\[a-f0-9]{40}\\[A-Za-z0-9_.-]+\z') {
        throw 'initial_stage_input_path'
    }
    $root = [IO.Path]::GetDirectoryName($InputPath)
    $bootstrap = ConvertFrom-Json -InputObject ($encoding.GetString((Read-Protected $InputPath $InputSha256 1048576)))
    Assert-Fields $bootstrap @('schema_version','attempt_id','legacy_plan_sha256','plan_template','observer','callback_template','paths')
    Assert-One $bootstrap.schema_version
    Assert-Pin $bootstrap.attempt_id
    Assert-Pin $bootstrap.legacy_plan_sha256
    $attempt = $bootstrap.attempt_id
    $plan = $bootstrap.plan_template
    $obs = $bootstrap.observer
    $callback = $bootstrap.callback_template
    $paths = $bootstrap.paths
    Assert-Fields $obs @('launcher_path','launcher_sha256','server_path','server_sha256','callback_path','callback_sha256',
        'windows_boot_sha256','wsl_boot_sha256','collector_sha256','client_sha256')
    Assert-Fields $callback @('a_disk_paths_sha256','b_disk_paths_sha256','driver_path','driver_sha256','installation_id',
        'reactivation_plan_path','runtime_path','source_root','vm_a','vm_b','vm_observer_path','vm_observer_sha256')
    Assert-Fields $paths @('intent_path','key_path','callback_config_path','observer_input_path','ready_path','plan_path',
        'approval_path','binding_path','preflight_path','scm_verifier_path')
    if ($plan -isnot [pscustomobject] -or $plan.schema_version -ne 2 -or
        ($plan.schema_version -isnot [int] -and $plan.schema_version -isnot [long]) -or
        $plan.profile -cne 'enforced-vm-v1' -or $plan.node -cne 'A' -or
        $plan.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or $plan.cluster -cnotmatch '\A[a-z][a-z0-9_]{0,62}\z' -or
        @($plan.PSObject.Properties.Name | Where-Object {$_ -cin @('initial_cutover_handoff','windows_initial_observer',
            '_approval_action','_installed_runtime')}).Count -ne 0) { throw 'initial_stage_template' }
    Assert-One $plan.generation
    Assert-One $plan.timeline
    Assert-Pin $plan.installation_id
    Assert-Pin $plan.controller_bundle_sha256
    Assert-Fields $plan.initial_wsl_cutover @('schema_version','primary_node','partner_node')
    Assert-One $plan.initial_wsl_cutover.schema_version
    if ($plan.initial_wsl_cutover.primary_node -cne 'A' -or $plan.initial_wsl_cutover.partner_node -cne 'B' -or
        $callback.installation_id -cne $plan.installation_id) { throw 'initial_stage_template' }
    $scope = $plan.windows_enforced
    $start = $plan.initial_service_start
    Assert-Fields $start @('preflight_sha256','service_image_path_sha256','scm_verifier_sha256','trusted_admin_sids')
    $deployment = $plan.pg_enforcement.effects.deployment_binding
    Assert-Fields $deployment @('source_commit','plan_sha256')
    if ($deployment.plan_sha256 -cne $bootstrap.legacy_plan_sha256 -or $deployment.source_commit -cnotmatch '\A[a-f0-9]{40}\z' -or
        $scope.service_name -cne ('N100HA_' + $plan.installation_id.Substring(0,24) + '_controller') -or
        $scope.service_sid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z') { throw 'initial_stage_template' }
    $serviceSid = ([Security.Principal.NTAccount]::new('NT SERVICE',$scope.service_name)).
        Translate([Security.Principal.SecurityIdentifier]).Value
    if ($serviceSid -cne $scope.service_sid -or $operatorSid -ceq $serviceSid) { throw 'initial_stage_service' }
    foreach ($pin in @($scope.service_image_sha256,$start.service_image_path_sha256,$obs.windows_boot_sha256,
        $obs.wsl_boot_sha256,$obs.collector_sha256,$obs.client_sha256,$callback.a_disk_paths_sha256,$callback.b_disk_paths_sha256)) {
        Assert-Pin $pin
    }
    if ($start.trusted_admin_sids -isnot [array] -or @($start.trusted_admin_sids).Count -gt 8 -or
        @($start.trusted_admin_sids | Where-Object {$_ -isnot [string] -or $_ -cnotmatch '\AS-1-(?:[0-9]{1,10}-){1,14}[0-9]{1,10}\z'}).Count -ne 0) {
        throw 'initial_stage_operator'
    }
    if ($callback.source_root -cnotmatch '\A/home/[A-Za-z0-9_.-]+/\.local/state/n100-ha/cutover-source-[a-f0-9]{40}/infra/k8s\z' -or
        $callback.runtime_path -cnotmatch ('\A/home/[A-Za-z0-9_.-]+/\.local/state/n100-ha/' + $attempt + '/runtime\.json\z') -or
        $callback.source_root -match '/\.\.?/' -or $callback.runtime_path -match '/\.\.?/') { throw 'initial_stage_callback' }
    $vmA = [guid]::ParseExact($callback.vm_a,'D')
    $vmB = [guid]::ParseExact($callback.vm_b,'D')
    if ($vmA -eq $vmB -or $vmA -eq [guid]::Empty -or $vmB -eq [guid]::Empty) { throw 'initial_stage_callback' }
    Assert-ReservedLeaf $paths.plan_path $root 'plan.json'
    Assert-ReservedLeaf $paths.approval_path $root 'approval.json'
    Assert-ReservedLeaf $paths.intent_path $root 'initial-cutover-activation.json'
    Assert-ReservedLeaf $paths.key_path $root 'initial-cutover-proof.key'
    Assert-ReservedLeaf $paths.ready_path $root ('lease-observer-ready-' + $attempt + '.json')
    Assert-ReservedLeaf $paths.binding_path $root ('initial-service-' + $attempt + '.json')
    Assert-ReservedLeaf $paths.callback_config_path $root
    Assert-ReservedLeaf $paths.observer_input_path $root
    $destinations = @($paths.plan_path,$paths.approval_path,$paths.intent_path,$paths.key_path,$paths.ready_path,
        $paths.binding_path,$paths.callback_config_path,$paths.observer_input_path)
    if (@($destinations | Sort-Object -Unique).Count -ne $destinations.Count) { throw 'initial_stage_reserved_path' }
    foreach ($path in $destinations) { Assert-Absent $path }
    Assert-ProtectedPath (Get-Item -LiteralPath $root -Force) $serviceSid -RequireReader
    Assert-EmptyJournal $root $serviceSid
    Assert-ReservedLeaf $obs.launcher_path $root 'windows-ha-start-lease-observer.ps1'
    Assert-ReservedLeaf $obs.server_path $root 'windows-ha-lease-proof-server.ps1'
    Assert-ReservedLeaf $obs.callback_path $root 'windows-ha-wsl-lease-observer.ps1'
    $observerLauncher = Read-Protected $obs.launcher_path $obs.launcher_sha256 65536
    $null = Read-Protected $obs.server_path $obs.server_sha256 65536
    $null = Read-Protected $obs.callback_path $obs.callback_sha256 65536
    $legacyRaw = Read-Protected $callback.reactivation_plan_path $bootstrap.legacy_plan_sha256 65536 '' $operatorSid
    $driverRaw = Read-Protected $callback.driver_path $callback.driver_sha256 65536 '' $operatorSid
    $null = Read-Protected $callback.vm_observer_path $callback.vm_observer_sha256 65536 '' $operatorSid
    $null = Read-Protected $paths.preflight_path $start.preflight_sha256 65536
    $null = Read-Protected $paths.scm_verifier_path $start.scm_verifier_sha256 262144
    $null = Read-Protected $scope.python_path $scope.python_sha256 67108864
    $null = Read-Protected $scope.launcher_path $scope.launcher_sha256 1048576
    $legacy = ConvertFrom-Json -InputObject ($encoding.GetString($legacyRaw))
    $reactivation = $legacy.windows_reactivation
    if ($legacy.windows_driver_sha256 -cne $callback.driver_sha256 -or $reactivation.authority_service_name -cne $scope.service_name -or
        $reactivation.authority_cluster -cne $plan.cluster -or $reactivation.authority_source_commit -cne $deployment.source_commit -or
        $reactivation.distribution -cne 'Ubuntu-24.04') { throw 'initial_stage_legacy_plan' }
    Assert-One $reactivation.authority_generation
    $boot = (Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime().Ticks
    if ((Digest ($encoding.GetBytes([string]$boot))) -cne $obs.windows_boot_sha256 -or
        $reactivation.boot_ticks -cne [string]$boot) { throw 'initial_stage_windows_boot' }
    $actualAdmins = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop |
        ForEach-Object { $_.SID.Value } | Sort-Object)
    if (($actualAdmins -join ',') -cne (($start.trusted_admin_sids | Sort-Object) -join ',')) {
        throw 'initial_stage_operator'
    }
    Assert-ManualHost $scope $start
    $taskName = 'N100HA_lease_observer_' + $attempt
    if (@(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ieq $taskName }).Count -ne 0) {
        throw 'initial_stage_reconciliation_required'
    }
    if ($Preflight) {
        [ordered]@{activation_stage_preflight=$true} | ConvertTo-Json -Compress
        return
    }

    Assert-Pin $RuntimeSha256
    if ($IntentBase64.Length -gt 5464 -or $IntentBase64 -cnotmatch '\A[A-Za-z0-9+/]+={0,2}\z') {
        throw 'initial_stage_intent'
    }
    $intentRaw = [Convert]::FromBase64String($IntentBase64)
    if ($intentRaw.Length -lt 1 -or $intentRaw.Length -gt 4096 -or
        [Convert]::ToBase64String($intentRaw) -cne $IntentBase64) { throw 'initial_stage_intent' }
    $intent = ConvertFrom-Json -InputObject ($encoding.GetString($intentRaw))
    Assert-Fields $intent @('schema_version','phase','attempt_id','legacy_plan_sha256','restore_receipt_sha256',
        'fence_sha256','activation_check_sha256','settlement_sha256')
    Assert-One $intent.schema_version
    foreach ($pin in @($intent.restore_receipt_sha256,$intent.fence_sha256,$intent.activation_check_sha256,$intent.settlement_sha256)) {
        Assert-Pin $pin
    }
    $canonicalIntent = [ordered]@{activation_check_sha256=$intent.activation_check_sha256;attempt_id=$attempt;
        fence_sha256=$intent.fence_sha256;legacy_plan_sha256=$bootstrap.legacy_plan_sha256;phase='activate_vm_intent';
        restore_receipt_sha256=$intent.restore_receipt_sha256;schema_version=1;settlement_sha256=$intent.settlement_sha256}
    if ($encoding.GetString($intentRaw) -cne $encoding.GetString((Json-Bytes $canonicalIntent))) { throw 'initial_stage_intent' }

    . ([ScriptBlock]::Create($encoding.GetString($driverRaw)))
    $authority = Get-HaCutoverAuthority $reactivation
    $markerPath = Join-Path $authority.directory 'ha-controller-owner.json'
    $markerRaw = Read-Protected $markerPath '' 4096
    $markerSha = Digest $markerRaw
    $marker = ConvertFrom-Json -InputObject ($encoding.GetString($markerRaw))
    Assert-Fields $marker @('cluster','generation','source_commit','plan_sha256','attempt_id')
    Assert-One $marker.generation
    if ($marker.cluster -cne $plan.cluster -or $marker.source_commit -cne $deployment.source_commit -or
        $marker.plan_sha256 -cne $bootstrap.legacy_plan_sha256 -or $marker.attempt_id -cne $attempt) { throw 'initial_stage_marker' }
    $markerPin = @{path=$markerPath;sha256=$markerSha;acl=(File-Access $serviceSid).GetSecurityDescriptorSddlForm($sections);
        file_id=(Get-HaMarkerFileId $markerPath)}
    Assert-HaOwnedCutoverMarker $markerPin

    $intentSha = Write-New $paths.intent_path $intentRaw $serviceSid
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $key = [byte[]]::new(32)
    $pipeBytes = [byte[]]::new(16)
    try {
        $random.GetBytes($key)
        $random.GetBytes($pipeBytes)
        $pipe = 'n100ha-lease-proof-' + [BitConverter]::ToString($pipeBytes).Replace('-','').ToLowerInvariant()
        $keySha = Write-New $paths.key_path $key $serviceSid
    } finally {
        [Array]::Clear($key,0,$key.Length)
        [Array]::Clear($pipeBytes,0,$pipeBytes.Length)
        $random.Dispose()
    }
    $callback | Add-Member -NotePropertyName runtime_sha256 -NotePropertyValue $RuntimeSha256
    $callbackSha = Write-New $paths.callback_config_path (Json-Bytes $callback)
    $arguments = [ordered]@{PipeName=$pipe;OperatorSid=$operatorSid;ServiceSid=$serviceSid;PlanSha256=$bootstrap.legacy_plan_sha256;
        AttemptId=$attempt;Generation=1;WindowsBootSha256=$obs.windows_boot_sha256;WslBootSha256=$obs.wsl_boot_sha256;
        CollectorSha256=$obs.collector_sha256;ReactivationPlanSha256=$bootstrap.legacy_plan_sha256;MarkerSha256=$markerSha;
        CallbackPath=$obs.callback_path;CallbackSha256=$obs.callback_sha256;CallbackConfigPath=$paths.callback_config_path;
        CallbackConfigSha256=$callbackSha;KeyPath=$paths.key_path;KeySha256=$keySha;ReadyPath=$paths.ready_path}
    $observerInput = [ordered]@{server_path=$obs.server_path;server_sha256=$obs.server_sha256;arguments=$arguments}
    $observerInputSha = Write-New $paths.observer_input_path (Json-Bytes $observerInput)
    Assert-HaOwnedCutoverMarker $markerPin
    $observerOutput = @(& ([ScriptBlock]::Create($encoding.GetString($observerLauncher))) `
        -InputPath $paths.observer_input_path -InputSha256 $observerInputSha -Go)
    if ($observerOutput.Count -ne 1 -or $observerOutput[0] -isnot [string]) { throw 'initial_stage_observer' }
    $started = ConvertFrom-Json -InputObject $observerOutput[0]
    Assert-Fields $started @('observer_ready','observer_pid')
    if ($started.observer_ready -isnot [bool] -or -not $started.observer_ready -or
        ($started.observer_pid -isnot [int] -and $started.observer_pid -isnot [long]) -or
        $started.observer_pid -lt 1 -or $started.observer_pid -gt [int]::MaxValue) { throw 'initial_stage_observer' }
    $ready = ConvertFrom-Json -InputObject ($encoding.GetString((Read-Protected $paths.ready_path '' 1024 '' $operatorSid -LeafOnly)))
    Assert-Fields $ready @('scope','pid','creation_filetime','server_sid','attempt_id','pipe_name','plan_sha256','callback_config_sha256')
    if ($ready.scope -cne 'n100ha-legacy-closure-v1' -or $ready.pid -ne $started.observer_pid -or
        $ready.creation_filetime -isnot [long] -or $ready.creation_filetime -le 0 -or $ready.server_sid -cne $operatorSid -or
        $ready.attempt_id -cne $attempt -or $ready.pipe_name -cne $pipe -or $ready.plan_sha256 -cne $bootstrap.legacy_plan_sha256 -or
        $ready.callback_config_sha256 -cne $callbackSha) { throw 'initial_stage_observer' }
    Assert-HaOwnedCutoverMarker $markerPin

    $start | Add-Member -NotePropertyName binding_path -NotePropertyValue $paths.binding_path
    $plan | Add-Member -NotePropertyName initial_cutover_handoff -NotePropertyValue ([ordered]@{
        attempt_id=$attempt;legacy_plan_sha256=$bootstrap.legacy_plan_sha256;restore_receipt_sha256=$intent.restore_receipt_sha256;
        activation_intent_sha256=$intentSha})
    $plan | Add-Member -NotePropertyName windows_initial_observer -NotePropertyValue ([ordered]@{
        pipe_name=$pipe;server_pid=$started.observer_pid;key_sha256=$keySha;windows_boot_sha256=$obs.windows_boot_sha256;
        wsl_boot_sha256=$obs.wsl_boot_sha256;collector_sha256=$obs.collector_sha256;client_sha256=$obs.client_sha256;
        marker_sha256=$markerSha})
    $planSha = Write-New $paths.plan_path (Json-Bytes $plan) $serviceSid
    $approval = [ordered]@{schema_version=1;action='ha-initial-wsl-cutover';authority_kind='installed-runtime';
        installation_id=$plan.installation_id;source_commit=$plan.source_commit;plan_sha256=$planSha;
        installed_at=[DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'",[Globalization.CultureInfo]::InvariantCulture)}
    $approvalSha = Write-New $paths.approval_path (Json-Bytes $approval) $serviceSid
    [ordered]@{ServiceName=$scope.service_name;ServiceHostSha256=$scope.service_image_sha256;
        PlanPath=$paths.plan_path;PlanSha256=$planSha;ApprovalPath=$paths.approval_path;ApprovalSha256=$approvalSha;
        BindingPath=$paths.binding_path;PreflightPath=$paths.preflight_path;PreflightSha256=$start.preflight_sha256;
        ScmVerifierPath=$paths.scm_verifier_path} | ConvertTo-Json -Compress
} catch {
    $reason = $_.Exception.Message
    if ($reason -cnotmatch '\Ainitial_stage_[a-z0-9_]{1,80}\z') { $reason = 'initial_stage_native_error' }
    [ordered]@{blocked=$reason} | ConvertTo-Json -Compress
}
