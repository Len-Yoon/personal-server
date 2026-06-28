# personal-server

개인용 서비스들을 Docker Compose로 묶어 운영하는 프로젝트입니다.

## Services

- `portal-web` (`8000`): 개인 서버 메인 포털
- `portal-web /files` (`8000`): 웹 파일 업로드/다운로드 파일함
- `crawler-worker` (`8001`): Google News RSS 수집, AI 요약, 저장 뉴스 관리
- `youtube-memo` (`8002`): YouTube 링크별 메모장
- `book-memo` (`8003`): 책 검색, 독서 진행률, 목차별 코멘트, 독서 메모
- `nginx-proxy-manager` (`80`, `81`, `443`): 외부 도메인/SSL 프록시

## Data

런타임 데이터는 루트 `data/` 아래에 둡니다.

- `data/crawler-worker/news_summaries.sqlite3`: 저장 뉴스/요약 DB
- `data/youtube-memo/youtube_memo.sqlite3`: YouTube 영상/메모 DB
- `data/book-memo/book_memo.sqlite3`: 책장/목차/독서 메모 DB
- `data/files/`: 웹 파일함 업로드 파일
- `data/npm/`: Nginx Proxy Manager 데이터
- `data/logs/`: 서비스 로그용 공유 디렉터리

## Environment

`.env.example`을 참고해 `.env`를 만들고 OpenAI 키를 설정합니다.

```text
OPENAI_API_KEY=
OPENAI_SUMMARY_MODEL=gpt-5-mini
ALADIN_TTB_KEY=
DELETE_PASSWORD=
FILE_STORAGE_PATH=/data/files
FILE_MANAGER_PASSWORD=
```

## Run

```bash
docker compose up -d --build
```

개별 서비스만 다시 빌드할 수도 있습니다.

```bash
docker compose up -d --build crawler-worker
docker compose up -d --build youtube-memo
```

## Notes

- 삭제 기능은 `.env`의 `DELETE_PASSWORD`를 확인합니다.
- 현재 Compose는 개발 편의상 서비스 디렉터리를 `/app`에 bind mount합니다.
- 운영 안정성을 더 높일 때는 `--reload` 없는 운영용 Compose 파일을 분리하는 것을 권장합니다.
- SQLite 스키마가 커지면 간단한 migration/version 테이블을 추가하는 것이 좋습니다.
- `book-memo` 책 검색은 `ALADIN_TTB_KEY`가 있으면 알라딘을 우선 사용하고, 실패하면 Google Books, Open Library 순서로 fallback합니다.
