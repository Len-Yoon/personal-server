param(
    [string]$WslDistro = "Ubuntu-24.04",
    [string]$ProjectPath = "/mnt/c/personal-server",
    [string]$TunnelName = "personal-server"
)

$ErrorActionPreference = "Stop"

# Keep the WSL-side setup idempotent so repeated Task Scheduler runs are safe.
$bashScript = @'
set -e
cd __PROJECT_PATH__

# Bring the compose stack back if Docker restarted or the containers were stopped.
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d

# cloudflared is a separate process, so start it only when it is missing.
if command -v cloudflared >/dev/null 2>&1; then
  if ! pgrep -f "cloudflared tunnel run __TUNNEL_NAME__" >/dev/null 2>&1; then
    nohup cloudflared tunnel run __TUNNEL_NAME__ >/tmp/cloudflared-__TUNNEL_NAME__.log 2>&1 &
  fi
else
  echo "cloudflared is not installed."
fi
'@.Replace('__PROJECT_PATH__', $ProjectPath).Replace('__TUNNEL_NAME__', $TunnelName)

wsl.exe -d $WslDistro -- bash -lc $bashScript
