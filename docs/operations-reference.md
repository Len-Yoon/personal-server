# 운영 참조

## 1. 공개 경로와 서비스

| 공개 도메인 | Docker 서비스 | N100 로컬 포트 | 비고 |
|---|---|---:|---|
| `len.pe.kr` | `portal-web` | 8000 | 메인 포털 |
| `portal.len.pe.kr` | `portal-web` | 8000 | 호환용 별칭 |
| `file.len.pe.kr` | `portal-web` | 8000 | 파일함 |
| `admin.len.pe.kr` | `portal-web` | 8000 | 관리자 상태 |
| `portfolio.len.pe.kr` | `portal-web` | 8000 | 공개 포트폴리오 |
| `news.len.pe.kr` | `crawler-worker` | 8001 | Investing.com 뉴스 허브 |
| `memo.len.pe.kr` | `youtube-memo` | 8002 | YouTube 메모 |
| `books.len.pe.kr` | `book-memo` | 8003 | 책 메모 |

N100 override는 애플리케이션 포트를 `127.0.0.1`에만 바인드함. `system-agent`는 `127.0.0.1:18010`으로만 노출되며, 포털이 Docker 네트워크의 `system-agent:8010`으로 상태를 조회함.

## 2. 공개 HTTPS 선택 기준

| 방식 | 사용할 상황 | 외부 진입 | 주요 설정 |
|---|---|---|---|
| Cloudflare Tunnel | 공유기 포트포워딩을 사용하지 않을 때 | Cloudflare → WSL `cloudflared` → localhost 서비스 포트 | `~/.cloudflared/config.yml` |
| Caddy + Cloudflare DNS-01 | N100의 `80`·`443` 인바운드를 직접 열 수 있을 때 | Cloudflare/DNS → Caddy → Docker 서비스 | `caddy/Caddyfile`, `CADDY_EMAIL`, `CLOUDFLARE_API_TOKEN` |

Windows bootstrap은 Docker 스택을 확인한 뒤 `cloudflared tunnel run` 프로세스가 없으면 시작함. 두 방식을 동시에 공개 경로로 운영할 필요는 없음.

## 3. 환경변수 범주

값은 `.env.example`을 기준으로 `.env`에만 설정함.

| 범주 | 주요 변수 | 용도 |
|---|---|---|
| 공개 경로 | `NEWS_SERVICE_URL`, `YOUTUBE_MEMO_URL`, `BOOK_MEMO_URL`, `*_HOSTNAME` | 포털 링크와 호스트별 라우팅 |
| HTTPS | `CADDY_EMAIL`, `CLOUDFLARE_API_TOKEN` | Caddy DNS-01을 선택한 경우에만 필요 |
| 인증 | `ADMIN_STATUS_PASSWORD`, `FILE_MANAGER_ACCESS_PASSWORD`, `DELETE_PASSWORD`, `PORTFOLIO_ADMIN_PASSWORD` | 관리자·파일함·메모 쓰기·포트폴리오 편집 경계 |
| 뉴스 | `NEWS_REFRESH_INTERVAL_SECONDS`, `NEWS_RETENTION_DAYS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Investing.com 시장 뉴스와 Google News IT·AI 수집 주기·보관·중요 알림 |
| 도서 검색 | `ALADIN_TTB_KEY` | 책 메모의 Aladin 검색 연동 |
| 호스트 상태 | `HOST_METRICS_PATH`, `HOST_METRICS_STALE_SECONDS` | Windows host metrics 파일과 오래됨 판단 |
| 백업·보안 | `BACKUP_*`, `SECURITY_LOG_*`, `AUTH_RATE_LIMIT_*`, `AUTH_SESSION_MAX_ENTRIES` | 보존 기간, 인증 실패 제한, 세션 상한 |

`ADMIN_STATUS_PASSWORD`가 설정되면 관리자 상태는 이 값을 우선 사용함. 파일함 삭제는 파일함 접근 세션과 삭제 비밀번호를 모두 요구함. 책·YouTube 메모는 쓰기 로그인 세션을 요구하며, 로그인되지 않은 브라우저의 쓰기 폼은 현재 페이지를 보존한 로그인 화면으로 이동함. 삭제 시 비밀번호를 다시 입력하지 않으며, YouTube 메모 수정은 별도 확인을 유지함.

## 4. 운영 확인 명령

N100 Windows PowerShell에서는 WSL을 통해 실행함.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml ps"
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml logs --tail=100"
```

host metrics가 오래되었다면 Windows PowerShell에서 파일의 갱신 시각과 내용을 확인함.

```powershell
Get-Item C:\personal-server\data\system\host-metrics.json | Select-Object LastWriteTime, Length
Get-Content C:\personal-server\data\system\host-metrics.json
```

정상 자동배포는 개발 PC에서 `main`으로 push한 뒤 GitHub Actions의 `Deploy N100` 결과를 확인하는 방식임. 수동 배포와 장애 대응은 [N100 GitHub 자동배포 안내](n100-github-auto-deploy.md)를 참고함.
