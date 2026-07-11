param(
    [string]$ComposeFile = "docker-compose.yml",
    [string]$N100ComposeFile = "docker-compose.n100.yml"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
if (-not $env:OBSIDIAN_VAULT_PATH -and (Test-Path -LiteralPath $envFile)) {
    $vaultLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^OBSIDIAN_VAULT_PATH=(.*)$' } | Select-Object -First 1
    if ($vaultLine -match '^OBSIDIAN_VAULT_PATH=(.*)$') {
        $env:OBSIDIAN_VAULT_PATH = $Matches[1].Trim()
    }
}

if (-not $env:OBSIDIAN_VAULT_PATH) {
    throw "OBSIDIAN_VAULT_PATH 환경변수를 먼저 설정하세요."
}

docker compose -f $ComposeFile -f $N100ComposeFile run --rm investing-crawler
exit $LASTEXITCODE
