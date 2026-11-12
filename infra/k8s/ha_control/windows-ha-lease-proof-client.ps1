param(
    [string]$PipeName,
    [uint32]$ServerPid,
    [string]$ServiceSid
)

$ErrorActionPreference = 'Stop'
$watch = $null
$BudgetMs = 0
$pipe = $null
function Remaining {
    $left = $BudgetMs - [int][Math]::Ceiling($watch.Elapsed.TotalMilliseconds)
    if ($left -le 0) { throw 'lease_proof_deadline' }
    return $left
}
try {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or -not [Environment]::Is64BitProcess -or
        [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        $PipeName -cnotmatch '\An100ha-lease-proof-[a-f0-9]{32}\z' -or
        $ServerPid -eq 0) {
        throw 'lease_proof_context'
    }
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if ($ServiceSid -cnotmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -or
            $current.User.Value -cne $ServiceSid -or
            [Diagnostics.Process]::GetCurrentProcess().SessionId -ne 0) { throw 'lease_proof_service' }
    } finally { $current.Dispose() }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class HaLeaseProofPeer {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint pid);

    public static void Verify(SafePipeHandle pipe, uint expected) {
        uint actual;
        if (!GetNamedPipeServerProcessId(pipe, out actual) || actual != expected)
            throw new InvalidOperationException();
    }
}
'@
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    [Console]::WriteLine('ready')
    while ($null -ne ($line = [Console]::ReadLine())) {
        $watch = [Diagnostics.Stopwatch]::StartNew()
        if ($line.Length -gt 2053 -or $line -cnotmatch '\A([1-9][0-9]{0,3}) ([\x20-\x7e]{2,2048})\z') {
            throw 'lease_proof_request'
        }
        $BudgetMs = [int]$Matches[1]
        if ($BudgetMs -gt 2000) { throw 'lease_proof_budget' }
        $request = $Matches[2]
        $rights = [IO.Pipes.PipeAccessRights]::ReadWrite -bor [IO.Pipes.PipeAccessRights]::Synchronize
        $pipe = [IO.Pipes.NamedPipeClientStream]::new('.', $PipeName, $rights,
            [IO.Pipes.PipeOptions]::Asynchronous, [Security.Principal.TokenImpersonationLevel]::Identification,
            [IO.HandleInheritability]::None)
        try {
            $pipe.Connect((Remaining))
            [HaLeaseProofPeer]::Verify($pipe.SafePipeHandle, $ServerPid)
            $requestBytes = [Text.Encoding]::ASCII.GetBytes($request + "`n")
            $write = $pipe.WriteAsync($requestBytes, 0, $requestBytes.Length)
            if (-not $write.Wait((Remaining))) { throw 'lease_proof_write' }
            $buffer = [byte[]]::new(4096)
            $offset = 0
            $frame = $null
            while ($offset -lt $buffer.Length) {
                $read = $pipe.ReadAsync($buffer, $offset, $buffer.Length - $offset)
                if (-not $read.Wait((Remaining)) -or $read.Result -le 0) { throw 'lease_proof_read' }
                $end = [Array]::IndexOf($buffer, [byte]10, $offset, $read.Result)
                $offset += $read.Result
                if ($end -ge 0) {
                    if ($end -ne $offset - 1) { throw 'lease_proof_frame' }
                    [HaLeaseProofPeer]::Verify($pipe.SafePipeHandle, $ServerPid)
                    $null = Remaining
                    $frame = [Text.UTF8Encoding]::new($false, $true).GetString($buffer, 0, $end)
                    break
                }
            }
            if ($null -eq $frame) { throw 'lease_proof_size' }
        } finally {
            $pipe.Dispose()
            $pipe = $null
        }
        [Console]::WriteLine($frame)
    }
} catch { exit 1 } finally {
    if ($null -ne $pipe) { $pipe.Dispose() }
}
