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
`homeops-executor`는 포트를 공개하지 않고 Docker 내부 네트워크에서만 `portal-web`의 요청을 받음.

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
| HomeOps | `HOMEOPS_EXECUTOR_SHARED_SECRET`, `HOMEOPS_SCHEDULER_SECRET`, `HOMEOPS_DB_PATH`, `HOMEOPS_APPROVAL_TTL_SECONDS` | 내부 실행기 인증, 정기 점검 인증, 장애 이력, 승인 만료 |
| HomeOps 알림 | `HOMEOPS_TELEGRAM_BOT_TOKEN`, `HOMEOPS_TELEGRAM_CHAT_ID`, `HOMEOPS_ADMIN_URL` | HomeOps 전용 Telegram 수신처와 관리자 상태 URL |

`ADMIN_STATUS_PASSWORD`가 설정되면 관리자 상태는 이 값을 우선 사용함. 파일함 삭제는 파일함 접근 세션과 삭제 비밀번호를 모두 요구함. 책·YouTube 메모는 쓰기 로그인 세션을 요구하며, 로그인되지 않은 브라우저의 쓰기 폼은 현재 페이지를 보존한 로그인 화면으로 이동함. 삭제 시 비밀번호를 다시 입력하지 않으며, YouTube 메모 수정은 별도 확인을 유지함.

## 4. HomeOps 운영 정책

HomeOps는 personal-server Compose 서비스만 진단하며 Docker 소켓은 `homeops-executor`에만 마운트됨. 임의 명령·파일 삭제·Windows/WSL/Docker 엔진 재시작·네트워크 변경은 수행하지 않음.

Telegram 알림은 정상 점검마다 발송하지 않음. 컨테이너 재시작 시작·복구 확인·복구 실패와 Windows host metrics의 메모리 사용률이 90% 이상으로 3회 연속 관측되거나 정상화된 경우에만 발송함. 알림 전송 실패는 복구 흐름을 중단하지 않음.

- 관리자 수동 진단은 정상 결과도 `no_action` 이력으로 저장할 수 있음. 정기 점검의 정상 결과는 이력을 저장하지 않음.
- 동일 서비스가 `unhealthy`로 3회 연속 진단되면 컨테이너만 재시작함.
- CPU 85% 이상 또는 컨테이너 메모리 제한의 90% 이상은 치명 로그(`fatal`, `panic`, `OOM`, `out of memory` 등)가 같은 진단에 함께 있을 때만 비정상으로 판정함. 단순한 정상 작업 부하는 재시작하지 않음.
- 재시작 뒤 health를 확인해 `verified` 또는 `failed`로 저장하며, 자동 재시작 뒤 10분 쿨다운과 서비스별 최근 1시간 최대 2회 제한을 적용함. 제한 도달 시 재시작하지 않고 Telegram 알림 및 이력만 남김.
- SQLite에는 UTC 기준 시각을 저장하고, 관리자 상태의 HomeOps 이력과 보안 이벤트는 `Asia/Seoul` 기준 `KST`로 표시함.

## 5. 운영 확인 명령

N100 Windows PowerShell에서는 WSL을 통해 실행함.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml ps"
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml logs --tail=100"
```

HomeOps 진단 결과가 `응답 없음`으로 표시되면 먼저 두 컨테이너가 같은 실행기 공유 비밀값을 받았는지 확인함. 실제 값은 출력하지 않음.

```bash
cd /mnt/c/personal-server
PORTAL_SECRET=$(docker compose -f docker-compose.yml -f docker-compose.n100.yml exec -T portal-web sh -c 'printf %s "$HOMEOPS_EXECUTOR_SHARED_SECRET"')
EXECUTOR_SECRET=$(docker compose -f docker-compose.yml -f docker-compose.n100.yml exec -T homeops-executor sh -c 'printf %s "$HOMEOPS_EXECUTOR_SHARED_SECRET"')
if [ -n "$PORTAL_SECRET" ] && [ "$PORTAL_SECRET" = "$EXECUTOR_SECRET" ]; then echo MATCH; else echo MISMATCH; fi
unset PORTAL_SECRET EXECUTOR_SECRET
```

`MISMATCH`이면 `.env`의 `HOMEOPS_EXECUTOR_SHARED_SECRET`을 한 줄의 임의 문자열로 설정한 뒤 `portal-web`, `homeops-executor`를 함께 재생성해야 함. 자세한 배포 절차는 [N100 GitHub 자동 배포](n100-github-auto-deploy.md)를 따름.

host metrics가 오래되었다면 Windows PowerShell에서 파일의 갱신 시각과 내용을 확인함.

```powershell
Get-Item C:\personal-server\data\system\host-metrics.json | Select-Object LastWriteTime, Length
Get-Content C:\personal-server\data\system\host-metrics.json
```

정상 자동배포는 개발 PC에서 `main`으로 push한 뒤 GitHub Actions의 `Deploy N100` 결과를 확인하는 방식임. 수동 배포와 장애 대응은 [N100 GitHub 자동배포 안내](n100-github-auto-deploy.md)를 참고함.
