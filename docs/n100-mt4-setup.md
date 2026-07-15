# Windows N100 + MT4 lightweight setup

This setup assumes the N100 PC runs Windows and MetaTrader 4 stays native on
Windows for stability. The personal server apps run inside WSL2 with Docker,
using `docker-compose.n100.yml` to keep CPU and memory usage low.

## Recommended layout

- Windows 11 Pro or Home on the N100 PC.
- MT4 installed directly on Windows.
- Ubuntu 24.04 in WSL2 for this repo.
- Docker Engine inside WSL2, or Docker Desktop if you prefer GUI management.
- Tailscale for private remote access.
- Caddy + Cloudflare DNS challenge for public access when you can forward `80`/`443`.
- Cloudflare Tunnel for public access when you do not want port forwarding.

The lightest practical path is:

```text
Windows
  MT4 native
  WSL2 Ubuntu
    Docker Compose
      portal-web
      system-agent
      book-memo
      youtube-memo
```

## Windows prep

Open PowerShell as Administrator:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot if Windows asks for it. Then open Ubuntu from the Start menu and create
the Linux username.

Optional but recommended: cap WSL2 so MT4 always has room. Create this file on
Windows:

```text
C:\Users\<your-windows-user>\.wslconfig
```

Suggested N100 values for an 8 GB machine:

```ini
[wsl2]
memory=3GB
processors=2
swap=1GB
localhostForwarding=true
```

Suggested values for a 16 GB machine:

```ini
[wsl2]
memory=5GB
processors=3
swap=2GB
localhostForwarding=true
```

Apply the WSL limit:

```powershell
wsl --shutdown
```

Then reopen Ubuntu.

## Install Docker inside WSL2

Inside Ubuntu:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin htop
sudo usermod -aG docker "$USER"
```

Close Ubuntu and reopen it so the Docker group is applied.

Start Docker:

```bash
sudo service docker start
```

If you do not want to start Docker manually after every reboot, add this near the
end of `~/.profile` inside Ubuntu:

```bash
if ! service docker status >/dev/null 2>&1; then
  sudo service docker start >/dev/null 2>&1
fi
```

## Clone and configure the repo

Inside Ubuntu:

```bash
git clone <repo-url> personal-server
cd personal-server
cp .env.example .env
nano .env
```

Minimum environment values:

```text
ALADIN_TTB_KEY=
DELETE_PASSWORD=
FILE_MANAGER_PASSWORD=
FILE_MANAGER_ACCESS_PASSWORD=
SECURITY_LOG_PATH=/app/data/logs/security-events.txt
SECURITY_LOG_TIMEZONE=Asia/Seoul
FILE_MAX_UPLOAD_MB=50
FILE_BLOCKED_EXTENSIONS=app,bat,cmd,com,dll,dmg,exe,jar,js,msi,php,ps1,sh,vbs
FILE_ALLOWED_EXTENSIONS=
FILE_MANAGER_AUTH_REQUIRED=true
APP_ENV=production
SYSTEM_AGENT_URL=http://system-agent:8010
HOST_METRICS_PATH=/data/system/host-metrics.json
HOST_METRICS_STALE_SECONDS=900
```

`/files`는 파일함 전용 비밀번호로 먼저 인증해야 합니다. 기본 파일함 비밀번호는
운영 환경에서는 `.env`의 `FILE_MANAGER_ACCESS_PASSWORD`에 사용할 비밀번호를 직접 설정할
수 있습니다. 파일 삭제에는 별도로 `DELETE_PASSWORD`가 필요합니다.

For a 24/7 N100 box, keep `FILE_MANAGER_AUTH_REQUIRED=true` or
`APP_ENV=production` so `/files` does not open accidentally when the password is
missing.

`DELETE_PASSWORD` is required for destructive actions such as deleting files,
saved news, YouTube memos, books, chapters, and book memos.

If you are moving data from another machine, copy the `data/` folder into:

```text
~/personal-server/data/
```

## Start the N100 stack

This starts the app containers used by the personal server:

- `portal-web`
- `system-agent`
- `crawler-worker`
- `book-memo`
- `youtube-memo`

If you enable public HTTPS, the N100 override also starts `caddy`.


Even in the N100 stack, the app ports are bound to `127.0.0.1`, so you can
still open the apps locally from the machine itself:
Operational entry point is `https://len.pe.kr`, while `127.0.0.1` is only the local bind address inside the N100 machine.

- `http://127.0.0.1:8000`
- `http://127.0.0.1:8001`
- `http://127.0.0.1:8002`
- `http://127.0.0.1:8003`
- `http://127.0.0.1:18010/health`

```bash
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
```

Open from Windows:

```text
http://127.0.0.1:8000
http://127.0.0.1:8002
http://127.0.0.1:8003
http://127.0.0.1:18010/health
```

## Windows host metrics

`system-agent` reads `data/system/host-metrics.json` when it exists. If the
file is missing or stale, the admin page still works and simply shows a
warning.

The included Windows bootstrap script installs a user-level startup entry that
waits 1 minute after login, starts the Docker stack, and connects the Cloudflare
Tunnel. It then repeats the same recovery check every 30 minutes with very low
overhead.

Install it with:

```powershell
powershell -ExecutionPolicy Bypass -File C:\personal-server\scripts\windows-bootstrap.ps1 -InstallTask
```

Run the registration once from an elevated PowerShell window if Windows reports
`Access is denied`. After that, the task runs the daemon at each user logon and
recovers the WSL Docker services, including `crawler-worker`, after reboot.

The same script can be run manually with `-Start` to bring the stack up once
right away, or with `-Daemon` to run the always-on lightweight recovery loop.

## Resource Notes

Stop the news crawler during trading hours if MT4 needs every bit of headroom:

```bash
docker compose -f docker-compose.yml -f docker-compose.n100.yml stop crawler-worker
```

If you are using Caddy for public access, keep the apps on localhost and follow `docs/caddy-cloudflare.md` for the proxy setup. In that mode, Caddy handles public HTTPS and reverse proxying.

If you are using Cloudflare Tunnel for public access, keep the apps on localhost and follow `docs/cloudflare-tunnel.md` for the tunnel setup. In that mode, `cloudflared` handles public ingress and no reverse proxy container is required.

## MT4 operating notes

- Keep MT4 installed and running on Windows, not inside WSL2.
- Put the MT4 data folder and this repo on the internal SSD, not a USB drive.
- Avoid unnecessary indicators and high-frequency EA logging.
- Keep Windows power mode on balanced or best performance.
- Disable Windows sleep while trading.
- Leave `crawler-worker` off during trading hours unless it is needed.
- Use the N100 Compose file so the web apps run without `--reload`.
- Keep `FILE_MAX_UPLOAD_MB` conservative so large browser uploads do not compete
  with MT4.
- Prune security logs and backups on a schedule so the SSD does not fill up
  silently.

## Useful commands

Check Docker resource use inside Ubuntu:

```bash
docker stats
htop
```

Stop all personal server apps:

```bash
docker compose -f docker-compose.yml -f docker-compose.n100.yml down
```

Update the apps manually only when GitHub Actions automatic deployment is unavailable:

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
```

정상 운영에서는 Mac 등 개발 PC에서 `main`으로 push하면
[`N100 GitHub 자동배포 안내`](n100-github-auto-deploy.md)에 따라 N100의 self-hosted
Runner가 WSL2 Docker에서 상시 서비스만 자동으로 재배포합니다. 이때

수동으로 일회성 Investing 뉴스 수집만 실행하려면 Windows 작업 스케줄러에서
`scripts/investing-news-once.ps1`를 실행하거나 다음 명령을 사용합니다.

```powershell
powershell -ExecutionPolicy Bypass -File C:\personal-server\scripts\investing-news-once.ps1
```

Restart WSL from Windows PowerShell:

```powershell
wsl --shutdown
```

Back up SQLite data and prune old security logs:

```bash
python3 scripts/maintenance.py backup
python3 scripts/maintenance.py prune-logs
python3 scripts/maintenance.py prune-news
python3 scripts/maintenance.py all
```

Install and run the lightweight Windows bootstrap from the Windows startup entry:

```powershell
powershell -ExecutionPolicy Bypass -File C:\personal-server\scripts\windows-bootstrap.ps1 -InstallTask
```

If file uploads should be included in backups, set:

```text
BACKUP_INCLUDE_FILES=true
BACKUP_RETENTION_DAYS=14
SECURITY_LOG_RETENTION_DAYS=30
NEWS_ARCHIVE_PATH=/data/crawler-worker/news_archive.json
NEWS_RETENTION_DAYS=7
BACKUP_STALE_SECONDS=172800
```

Windows 부트스트랩은 로그인 후 복구 루프에서 하루에 한 번 `maintenance.py all`을
자동 실행합니다. 이 작업은 뉴스 캐시, Obsidian의 날짜별 Investing 뉴스, 보안
로그, 백업을 각 보존기간에 맞춰 정리하며, Obsidian 뉴스는 `YYYY-MM-DD.md`
형식의 파일만 삭제합니다. 보존기간을 지난 파일은 복구할 수 없으므로 필요한
자료는 별도 보관해야 합니다.


## Expected resource shape

The default stack is designed to stay small:

- `portal-web`: 1 worker, no reload, 160 MB cap
- `system-agent`: 1 worker, no reload, 96 MB cap
- `book-memo`: 1 worker, no reload, 192 MB cap
- `youtube-memo`: 1 worker, no reload, 160 MB cap
- `crawler-worker`: optional, 320 MB cap
- `caddy`: optional for public HTTPS, 80/443 exposure

For a Caddy + Cloudflare setup, the apps stay bound to localhost and Caddy handles public HTTPS.

For a Cloudflare Tunnel setup, the apps stay bound to localhost and `cloudflared` handles public ingress.

With WSL2 capped, Windows and MT4 keep the majority of the machine while the
personal server apps stay predictable in the background.
