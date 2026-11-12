param([string]$PipeName)

$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -cne 'Desktop' -or -not [Environment]::Is64BitProcess -or
    $PipeName -cnotmatch '\An100ha-vm-observe-[a-f0-9]{32}\z') { exit 1 }

$rights = [IO.Pipes.PipeAccessRights]::ReadWrite -bor [IO.Pipes.PipeAccessRights]::Synchronize
$pipe = [IO.Pipes.NamedPipeClientStream]::new('.', $PipeName, $rights, [IO.Pipes.PipeOptions]::Asynchronous,
    [Security.Principal.TokenImpersonationLevel]::Identification, [IO.HandleInheritability]::None)
try {
    $pipe.Connect(2000)
    [Console]::WriteLine('ready')
    for ($round = 0; $round -lt 2; $round++) {
        $challenge = [Console]::ReadLine()
        if ($challenge -cnotmatch '\A[a-f0-9]{32}\z') { throw 'fixed_observer_nonce' }
        $nonce = [Text.Encoding]::ASCII.GetBytes($challenge)
        $write = $pipe.WriteAsync($nonce,0,$nonce.Length)
        if (-not $write.Wait(1000)) { throw 'fixed_observer_request_timeout' }
        $response = [byte[]]::new(4096)
        $offset = 0
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($offset -lt $response.Length) {
            $remaining = 3000 - [int]$watch.ElapsedMilliseconds
            if ($remaining -le 0) { throw 'fixed_observer_response_timeout' }
            $read = $pipe.ReadAsync($response,$offset,$response.Length-$offset)
            if (-not $read.Wait($remaining) -or $read.Result -le 0) { throw 'fixed_observer_response_timeout' }
            $lineEnd = [array]::IndexOf($response,[byte]10,$offset,$read.Result)
            $offset += $read.Result
            if ($lineEnd -ge 0) {
                if ($lineEnd -ne $offset-1) { throw 'fixed_observer_response_frame' }
                [Console]::WriteLine([Text.UTF8Encoding]::new($false,$true).GetString($response,0,$lineEnd))
                break
            }
        }
        if ($lineEnd -lt 0) { throw 'fixed_observer_response_size' }
    }
} catch { exit 1 } finally { $pipe.Dispose() }
