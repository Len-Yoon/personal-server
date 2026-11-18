function Initialize-HaRecoveryObservation($Config) {
    # Hyper-V reserves these IDs for wildcards and special partitions.
    $specialVmIds = @(
        '00000000-0000-0000-0000-000000000000',
        'ffffffff-ffff-ffff-ffff-ffffffffffff',
        '90db8b89-0d35-4f79-8ce9-49ea0ac8b7cd',
        'e0e16197-dd56-4a10-9195-5ee7a155a838',
        'a42e7cda-d03f-480c-9cc2-a4de20abb878'
    )
    if ($Config.guest_vm_id -isnot [string] -or
        $Config.guest_vm_id -cnotmatch '\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z' -or
        $Config.guest_vm_id -cin $specialVmIds -or
        $Config.guest_vsock_port -isnot [int] -or $Config.guest_vsock_port -lt 1 -or
        $Config.guest_vsock_port -ge 1024) { throw 'recovery_observer_target' }
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;

public sealed class HaRecoveryGuestEndpoint : EndPoint
{
    private readonly Guid vm;
    private readonly Guid service;

    public HaRecoveryGuestEndpoint(Guid vm, Guid service) { this.vm = vm; this.service = service; }
    public override AddressFamily AddressFamily { get { return (AddressFamily)34; } }

    public override SocketAddress Serialize()
    {
        var address = new SocketAddress(AddressFamily, 36);
        byte[] vmBytes = vm.ToByteArray();
        byte[] serviceBytes = service.ToByteArray();
        for (int i = 0; i < 16; i++) {
            address[4 + i] = vmBytes[i];
            address[20 + i] = serviceBytes[i];
        }
        return address;
    }

    public override EndPoint Create(SocketAddress address)
    {
        if (address.Family != AddressFamily || address.Size != 36)
            throw new ArgumentException("address");
        byte[] vmBytes = new byte[16];
        byte[] serviceBytes = new byte[16];
        for (int i = 0; i < 16; i++) {
            vmBytes[i] = address[4 + i];
            serviceBytes[i] = address[20 + i];
        }
        return new HaRecoveryGuestEndpoint(new Guid(vmBytes), new Guid(serviceBytes));
    }

    private static int Remaining(long deadline)
    {
        long ticks = deadline - Stopwatch.GetTimestamp();
        if (ticks <= 0)
            throw new TimeoutException("guest_observer_deadline");
        return (int)Math.Max(1, ticks * 1000 / Stopwatch.Frequency);
    }

    public static byte[] Observe(Guid vm, Guid service, byte[] request, long deadline)
    {
        using (var socket = new Socket((AddressFamily)34, SocketType.Stream, (ProtocolType)1)) {
            var pending = socket.BeginConnect(new HaRecoveryGuestEndpoint(vm, service), null, null);
            using (var wait = pending.AsyncWaitHandle) {
                if (!wait.WaitOne(Remaining(deadline)))
                    throw new TimeoutException("guest_observer_connect");
                socket.EndConnect(pending);
            }
            socket.SendTimeout = Remaining(deadline);
            if (socket.Send(request) != request.Length)
                throw new InvalidOperationException("guest_observer_send");
            byte[] reply = new byte[1024];
            int received = 0;
            while (received < reply.Length) {
                socket.ReceiveTimeout = Remaining(deadline);
                int count = socket.Receive(reply, received, reply.Length - received, SocketFlags.None);
                if (count == 0) {
                    Remaining(deadline);
                    Array.Resize(ref reply, received);
                    return reply;
                }
                received += count;
            }
            throw new InvalidOperationException("guest_observer_response_size");
        }
    }
}
'@
    $script:recoveryGuestVm = [guid]$Config.guest_vm_id
    $script:recoveryGuestService = [guid]('{0:x8}-facb-11e6-bd58-64006a7986d3' -f $Config.guest_vsock_port)
    $script:recoveryGuestHold = $Config.hold_sha256
    $script:recoveryGuestBoot = $Config.guest_boot_sha256
    $script:recoveryGuestPurpose = $Config.purpose
}

function Get-HaRecoveryObservation([string]$Raw, [long]$Deadline) {
    $challenge = ConvertFrom-Json -InputObject $Raw
    if ($challenge.guest_boot_sha256 -cne $script:recoveryGuestBoot -or
        $challenge.hold_sha256 -cne $script:recoveryGuestHold -or
        $challenge.purpose -cne $script:recoveryGuestPurpose) { throw 'recovery_observer_binding' }
    $encoding = [Text.UTF8Encoding]::new($false, $true)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $nonce = [BitConverter]::ToString($sha.ComputeHash($encoding.GetBytes($Raw))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    $request = [ordered]@{
        guest_boot_sha256=$script:recoveryGuestBoot
        hold_sha256=$script:recoveryGuestHold
        nonce=$nonce
        purpose=$script:recoveryGuestPurpose
        version=1
    }
    $wire = $encoding.GetBytes((ConvertTo-Json -InputObject $request -Compress) + "`n")
    $rawReply = [HaRecoveryGuestEndpoint]::Observe($script:recoveryGuestVm, $script:recoveryGuestService, $wire, $Deadline)
    $text = $encoding.GetString($rawReply)
    $reply = ConvertFrom-Json -InputObject $text
    if ($reply.observation_sha256 -isnot [string] -or $reply.observation_sha256 -cnotmatch '\A[a-f0-9]{64}\z' -or
        ($script:recoveryGuestPurpose -ceq 'held_admission' -and
         $reply.observation_sha256 -cne $script:recoveryGuestHold)) { throw 'recovery_observer_receipt' }
    $request.Insert(3, 'observation_sha256', $reply.observation_sha256)
    if ($text -cne ((ConvertTo-Json -InputObject $request -Compress) + "`n")) { throw 'recovery_observer_binding' }
    [pscustomobject]@{challenge_sha256=$nonce;closed=$true;observation_sha256=$reply.observation_sha256}
}
