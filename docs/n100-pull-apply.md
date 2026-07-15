# N100 Pull 적용 안내

## 문서 정보

| 항목 | 내용 |
|---|---|
| 대상 | N100 운영 PC |
| 목적 | Git pull 후 Investing 뉴스 저장 경로를 동일하게 적용함 |
| 공통 경로 | `./data/obsidian/vault` |
| 컨테이너 경로 | `/vault` |
| 스케줄러 변경 | 없음 |

## 적용 순서

PowerShell에서 프로젝트 폴더로 이동한 뒤에 실행함.

```powershell
git pull
```

`.env`는 Git에 포함되지 않으므로 N100에서 아래 설정을 확인함.

```env
OBSIDIAN_VAULT_PATH=./data/obsidian/vault
OBSIDIAN_NEWS_DIR=뉴스/Investing
INVESTING_NEWS_LIMIT=50
INVESTING_NEWS_TIMEZONE=Asia/Seoul
```

기존 N100 `.env`에 `C:\...` 형태의 `OBSIDIAN_VAULT_PATH`가 있으면 위 상대경로로 변경함. 별도 외부 Vault를 사용하지 않는 경우 해당 상대경로를 그대로 유지함.

## 적용 확인

프로젝트 폴더에서 Compose 설정을 먼저 검증함.

```powershell
docker compose config
```

오류가 없으면 Investing crawler만 일회 실행함.

```powershell
docker compose run --rm investing-crawler
```

정상 실행 후 파일이 아래 경로에 생성되는지 확인함.

```text
<프로젝트 폴더>\data\obsidian\vault\뉴스\Investing\YYYY-MM-DD.md
```

## 확인 결과

| 확인 항목 | 정상 기준 |
|---|---|
| Compose 설정 | `docker compose config` 오류 없음 |
| 뉴스 수집 | `investing-crawler` 종료 코드 `0` |
| Markdown 저장 | 날짜별 `.md` 파일 생성됨 |
| 운영 서비스 | 기존 운영 서비스와 스케줄러 설정 변경 없음 |

## 확인 필요 사항

- `.env`는 Git pull로 자동 갱신되지 않으므로 N100에서 경로 설정을 최초 1회 확인해야 함.
- 기존 외부 Vault를 계속 사용할 경우 `OBSIDIAN_VAULT_PATH`에 해당 절대경로를 유지할 수 있음.
- 본 문서의 확인 명령은 Investing crawler만 대상으로 하며, 서버 전체 재시작을 수행하지 않음.
