# personal-server

개인용 서비스들을 Docker Compose로 묶어 운영하는 프로젝트입니다.

## Services

- `portal-web` (`8000`): 개인 서버 메인 포털
- `portal-web /files` (`8000`): 웹 파일 업로드/다운로드 파일함
- `crawler-worker` (`8001`): Google News RSS 수집, AI 요약, 저장 뉴스 관리
- `youtube-memo` (`8002`): YouTube 링크별 메모장
- `book-memo` (`8003`): 책 검색, 독서 진행률, 목차별 코멘트, 독서 메모
- `nginx-proxy-manager` (`80`, `81`, `443`): 외부 도메인/SSL 프록시

## Screenshots

### Portal Web

![Portal dashboard](docs/images/portal-dashboard.png)

개인 서버의 메인 허브입니다. 운영 중인 서비스 목록을 한 화면에서 확인하고, 파일함과 메모 서비스로 이동할 수 있습니다. 보안 상태 버튼을 통해 관리자 인증 후 최근 보안 이벤트와 업로드 정책을 확인합니다.

### File Manager

![File manager](docs/images/file-manager.png)

개인 서버에 파일을 올리고 내려받는 웹 파일 관리자입니다. 업로드 크기 제한, 차단 확장자, 덮어쓰기 방지, 일별 보안 로그 기록을 적용했습니다.
윈도우 탐색기처럼 폴더를 만들고, 파일을 드래그 앤 드롭으로 올릴 수 있습니다.

### News Hub

![News hub](docs/images/news-hub.png)

Google News RSS 기반으로 글로벌 뉴스와 시장 뉴스를 수집합니다. 저장한 뉴스 검색과 AI 요약 흐름을 붙일 수 있도록 서비스와 데이터 저장 계층을 분리했습니다.

### YouTube Memo

![YouTube memo](docs/images/youtube-memo.png)

유튜브 링크별로 학습 메모를 관리하는 서비스입니다. 영상 단위로 메모를 쌓고 다시 찾아볼 수 있게 구성했습니다.

### Book Memo

![Book memo](docs/images/book-memo.png)

책 검색, 독서 상태, 진행률, 목차별 코멘트와 독서 메모를 관리합니다. 알라딘, Google Books, Open Library 검색 fallback을 지원합니다.

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
OPENAI_SUMMARY_MODEL=
ALADIN_TTB_KEY=
DELETE_PASSWORD=
FILE_STORAGE_PATH=
FILE_MANAGER_PASSWORD=
SECURITY_LOG_PATH=
SECURITY_LOG_TIMEZONE=
FILE_MAX_UPLOAD_MB=
FILE_BLOCKED_EXTENSIONS=
FILE_ALLOWED_EXTENSIONS=
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
- `portal-web`은 보안 헤더를 응답에 추가하고, 파일함 인증 실패/업로드/다운로드/삭제 이벤트를 `SECURITY_LOG_PATH` 기준의 일별 텍스트 로그로 기록합니다.
- 파일함 업로드는 최대 용량, 차단 확장자, 확장자 없는 파일, 기존 파일 덮어쓰기를 제한합니다.
- 포털의 `보안 상태` 버튼은 관리자 인증 후 최근 보안 이벤트와 업로드 정책을 모달로 보여줍니다.
- 현재 Compose는 개발 편의상 서비스 디렉터리를 `/app`에 bind mount합니다.
- 운영 안정성을 더 높일 때는 `--reload` 없는 운영용 Compose 파일을 분리하는 것을 권장합니다.
- SQLite 스키마가 커지면 간단한 migration/version 테이블을 추가하는 것이 좋습니다.
- `book-memo` 책 검색은 `ALADIN_TTB_KEY`가 있으면 알라딘을 우선 사용하고, 실패하면 Google Books, Open Library 순서로 fallback합니다.
