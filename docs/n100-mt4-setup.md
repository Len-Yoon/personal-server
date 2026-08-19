# N100 Windows·WSL2 운영 환경

> 서비스·도메인·환경변수의 최신 목록은 [운영 참조](operations-reference.md)를 우선함. 이 문서는 N100의 최초 준비, 자원 한도, 자동 시작과 점검 절차만 다룸.

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 대상 | Windows 기반 N100 개인 서버 |
| 실행 구조 | Windows + Ubuntu-24.04 WSL2 + Docker Compose |
| 저장소 위치 | `C:\personal-server` / WSL `/mnt/c/personal-server` |
| 운영 원칙 | MT4는 Windows에서 실행하고 웹 서비스는 WSL2 Docker에서 실행 |
| 자동화 | Windows bootstrap, GitHub Actions self-hosted runner |

## 2. 구성과 자원 기준

```text
Windows N100
├─ MT4 (Windows 네이티브)
├─ Windows bootstrap (host metrics, Compose·Tunnel 확인, 5분 HomeOps 점검)
├─ GitHub Actions self-hosted runner (main push 배포)
└─ Ubuntu-24.04 WSL2
   └─ Docker Compose
      ├─ portal-web / system-agent / homeops-executor
      ├─ crawler-worker / youtube-memo / book-memo
      └─ caddy
```

N100 override의 컨테이너 자원 한도는 다음과 같음. 이는 Docker가 사용할 수 있는 상한이며 Windows 전체 메모리 사용량과 동일하지 않음.

| 서비스 | CPU 한도 | 메모리 한도 |
|---|---:|---:|
| `portal-web` | 0.25 | 160 MB |
| `system-agent` | 0.15 | 96 MB |
| `homeops-executor` | 0.10 | 64 MB |
| `crawler-worker` | 0.50 | 320 MB |
| `youtube-memo` | 0.25 | 160 MB |
| `book-memo` | 0.35 | 192 MB |
| `caddy` | 별도 Compose 제한 없음 | 별도 Compose 제한 없음 |

## 3. 최초 준비

### 3.1 Windows와 WSL2

관리자 PowerShell에서 Ubuntu-24.04를 설치함.

```powershell
wsl --install -d Ubuntu-24.04
```

MT4와 Windows 여유 자원을 우선하려면 `C:\Users\<Windows사용자>\.wslconfig`에 WSL2 한도를 설정함. 아래 값은 권장 예시이며 실제 메모리 용량에 맞춰 조정 필요함.

```ini
[wsl2]
memory=3GB
processors=2
swap=1GB
localhostForwarding=true
```

설정 변경 후 PowerShell에서 WSL을 재시작함.

```powershell
wsl --shutdown
```

### 3.2 Docker와 저장소

Ubuntu에서 Docker를 설치하고 사용자를 Docker 그룹에 추가함.

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Ubuntu를 다시 연 뒤 Docker가 동작하는지 확인함.

```bash
docker version
docker compose version
```

저장소는 배포 workflow와 Windows bootstrap이 사용하는 경로인 `C:\personal-server`에 둠. WSL에서는 `/mnt/c/personal-server`로 접근함. `.env`와 `data/`는 Git 추적 대상이 아니므로 운영 PC에서만 생성·보관함.

```bash
cd /mnt/c
git clone <저장소 URL> personal-server
cd /mnt/c/personal-server
cp .env.example .env
mkdir -p data/system data/logs
```

## 4. 운영 환경변수

`.env.example`을 기준으로 `.env`를 작성함. 실제 토큰·비밀번호·chat ID·공유 비밀값은 문서와 Git에 기록하지 않음.

| 범주 | 필수 또는 권장 변수 | 비고 |
|---|---|---|
| 관리자 인증 | `ADMIN_STATUS_PASSWORD`, `FILE_MANAGER_ACCESS_PASSWORD`, `DELETE_PASSWORD` | 서로 다른 충분히 긴 값 권장 |
| HomeOps | `HOMEOPS_EXECUTOR_SHARED_SECRET`, `HOMEOPS_SCHEDULER_SECRET` | 서로 다른 임의 문자열, 비어 있으면 해당 기능이 동작하지 않음 |
| 뉴스 알림 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 기존 뉴스 전용 채팅방 값 |
| HomeOps 알림 | `HOMEOPS_TELEGRAM_BOT_TOKEN`, `HOMEOPS_TELEGRAM_CHAT_ID`, `HOMEOPS_ADMIN_URL` | 서버 상태 전용 채팅방 값 |
| 공개 경로 | `NEWS_SERVICE_URL`, `YOUTUBE_MEMO_URL`, `BOOK_MEMO_URL`, `*_HOSTNAME` | 포털 링크 및 호스트 라우팅 |
| Cloudflare | `CADDY_EMAIL`, `CLOUDFLARE_API_TOKEN` | Caddy를 공개 HTTPS 진입점으로 사용할 때만 필요 |

HomeOps 두 공유 비밀값은 안전한 임의 문자열을 생성해 입력함.

```bash
openssl rand -hex 32
```

`HOMEOPS_EXECUTOR_SHARED_SECRET`은 `portal-web`과 `homeops-executor`가 동일한 값을 사용해야 함. 컨테이너 재생성 뒤에도 `.env`가 유지되면 같은 값으로 다시 주입됨.

## 5. 시작·자동 시작

### 5.1 최초 수동 시작

```bash
cd /mnt/c/personal-server
docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.n100.yml ps
```

서비스 포트는 N100에서 `127.0.0.1`에만 바인드됨. `homeops-executor`는 외부 포트를 열지 않음.

| 로컬 확인 주소 | 서비스 |
|---|---|
| `http://127.0.0.1:8000` | 포털·파일함·관리자 상태 |
| `http://127.0.0.1:8001` | 뉴스 허브 |
| `http://127.0.0.1:8002` | YouTube 메모 |
| `http://127.0.0.1:8003` | 책 메모 |
| `http://127.0.0.1:18010/health` | system-agent health |

### 5.2 Windows bootstrap 등록

Windows PowerShell에서 다음 명령을 한 번 실행함. 작업 등록 시 Windows 계정 비밀번호 입력이 필요할 수 있음.

```powershell
powershell -ExecutionPolicy Bypass -File C:\personal-server\scripts\windows-bootstrap.ps1 -InstallTask
```

bootstrap 동작은 다음과 같음.

1. 로그인 후 120초 동안 WSL과 Docker 준비를 기다림.
2. Windows CPU·메모리·디스크·uptime을 `data/system/host-metrics.json`에 기록함.
3. Docker Compose 서비스를 실행 상태로 맞추고 Cloudflare Tunnel 프로세스가 없으면 시작함.
4. 5분마다 host metrics·Compose·Tunnel을 다시 확인하고 HomeOps 내부 정기 점검을 호출함.
5. 하루 한 번 SQLite 백업, 보존기간 경과 보안 로그·뉴스 archive 정리를 실행함.

이 bootstrap은 Windows·WSL·Docker 엔진·전체 Docker 스택을 재시작하지 않음.

## 6. HomeOps 운영

- 수동 진단: `admin.len.pe.kr/admin/status` 로그인 → HomeOps에서 대상 선택 → 상태 진단 시작 → 필요한 이력만 승인·실행함.
- 정기 점검: 5분마다 실행되며 정상 결과는 이력에 저장하지 않음.
- 자동 조치: 동일 컨테이너 비정상이 3회 연속 확인된 경우에만 해당 컨테이너를 재시작함.
- 자원 조건: CPU 85% 또는 컨테이너 메모리 90% 이상만으로는 재시작하지 않으며, 같은 진단의 치명 로그가 함께 필요함.
- 제한: 서비스별 10분 쿨다운, 최근 1시간 최대 2회 자동 재시작임.
- Windows 전체 메모리 90% 이상 3회 연속은 Telegram 경고만 전송하며 Windows를 재부팅하지 않음.
- 관리자 화면의 HomeOps 이력 시각은 KST로 표시됨. SQLite 저장값은 UTC로 유지됨.

자세한 권한 경계와 제한은 [HomeOps 설계](superpowers/specs/2026-08-19-homeops-approved-operations-design.md)를 참고함.

## 7. 공개 HTTPS

Cloudflare Tunnel을 기본 공개 경로로 사용하면 포트포워딩 없이 WSL `cloudflared`가 localhost 포트로 전달함. 설정 절차는 [Cloudflare Tunnel 운영 가이드](cloudflare-tunnel.md)를 참고함.

외부 `80`·`443` 인바운드가 가능한 경우에는 Caddy를 공개 HTTPS 진입점으로 선택할 수 있음. 설정 절차는 [Caddy + Cloudflare 운영 가이드](caddy-cloudflare.md)를 참고함.

N100 Compose에는 `caddy` 서비스가 포함되어 있으나, 실제 공개 유입 경로는 Tunnel 또는 Caddy 중 하나로 정해 운영함.

## 8. 일상 점검과 장애 확인

### 상태와 로그

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml ps"
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml logs --tail=100"
Get-Item C:\personal-server\data\system\host-metrics.json | Select-Object LastWriteTime, Length
```

### 자동 배포

정상 운영에서는 개발 PC에서 `main`으로 push한 뒤 GitHub Actions `Deploy N100` 성공 여부를 확인함. N100 작업 디렉터리의 추적 파일을 직접 수정하지 않음. 수동 배포와 Runner 장애 대응은 [N100 GitHub 자동 배포](n100-github-auto-deploy.md)를 참고함.

### 자원 확인

```bash
cd /mnt/c/personal-server
docker stats
```

MT4 자원이 부족할 때 `crawler-worker` 중지는 운영자가 명시적으로 결정할 사항임. `docker compose stop`, `down`, Windows 재부팅은 HomeOps 자동 조치 범위가 아님.

## 9. 확인 필요 사항

- N100의 실제 메모리 용량에 맞는 `.wslconfig` 상한 확인 필요함.
- Cloudflare Tunnel 또는 Caddy 중 현재 공개 유입 경로 확인 필요함.
- GitHub Actions Runner 서비스가 WSL2와 Docker에 접근 가능한 Windows 사용자 계정으로 실행되는지 확인 필요함.
- HomeOps와 뉴스 Telegram 채팅방이 분리되어 있는지, 각 `.env` 변수가 올바른 채팅 ID를 가리키는지 확인 필요함.
