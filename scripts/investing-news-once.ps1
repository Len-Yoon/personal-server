param(
    [string]$ComposeFile = "docker-compose.yml",
    [string]$N100ComposeFile = "docker-compose.n100.yml"
)

$ErrorActionPreference = "Stop"

if (-not $env:OBSIDIAN_VAULT_PATH) {
    throw "OBSIDIAN_VAULT_PATH 환경변수를 먼저 설정하세요."
}

docker compose -f $ComposeFile -f $N100ComposeFile run --rm investing-crawler
exit $LASTEXITCODE
