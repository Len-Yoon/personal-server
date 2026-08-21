# 개인서버 코드 일관성 리팩터링 설계

## 1. 문서 개요

| 항목 | 내용 |
|---|---|
| 문서명 | 개인서버 코드 일관성 리팩터링 설계 |
| 작성일 | 2026-08-21 |
| 기준 자료 | 현재 서비스 코드, `AGENTS.md`, 전체 서비스 테스트 실행 결과 |
| 목적 | 외부 기능을 유지하면서 서비스별 책임 경계와 테스트 가능성을 개선함 |
| 제외 범위 | 서버 기동, Compose, 배포 workflow, `crawler-worker/app/services/news_scheduler.py` |

## 2. 점검 결과

| 대상 | 확인 결과 | 처리 방향 |
|---|---|---|
| `book-memo`, `youtube-memo` | 인증·세션·rate-limit·Origin 검증 보조 로직이 각 `main.py`에 중복됨 | 서비스 내부 인증 모듈로 분리함 |
| `portal-web/app/routers/dashboard.py` | 포털 화면, 관리자 인증, HomeOps 라우트가 한 파일에 혼재함 | 화면·관리자/HomeOps 라우터로 분리함 |
| `crawler-worker/app/services/news_archive.py` | 보관, 정규화, 검색, 알림, 중복 제거가 한 모듈에 집중됨 | 공개 API를 유지한 채 내부 책임을 분리함 |
| 서비스별 `host_urls.py` | `book-memo`와 `youtube-memo` 파일 내용이 동일함 | Docker build context를 바꾸지 않으므로 현재는 공통화하지 않음 |

## 3. 리팩터링 원칙

- 기존 URL, HTTP 상태 코드, 응답 JSON, 템플릿 컨텍스트, 환경 변수명, SQLite 스키마를 변경하지 않음.
- 기존 공개 서비스 함수와 라우터 경로를 유지함.
- 각 단계는 기존 동작을 고정하는 테스트를 먼저 추가하거나 기존 테스트로 보호한 뒤 최소 변경함.
- 서비스 간 루트 공통 패키지는 만들지 않음. 현재 Docker build context가 서비스 디렉터리로 분리되어 있어 Compose·Dockerfile 변경이 필요하기 때문임.
- `news_scheduler.py`, Docker Compose, 서버 기동·배포 파일은 수정하지 않음.

## 4. 목표 구조

```text
book-memo / youtube-memo
  main.py (라우트·템플릿만 담당)
  services/write_auth.py (세션·Origin·rate-limit·안전 redirect)

portal-web
  routers/dashboard.py (포털 화면·서비스 이동·검색)
  routers/admin.py (관리자 상태·HomeOps 인증/작업)

crawler-worker
  services/news_archive.py (기존 공개 API facade)
  services/news_archive_storage.py (파일 읽기·쓰기·보존)
  services/news_archive_processing.py (정규화·병합·검색·분류)
  services/news_archive_notifications.py (알림 대기열·중복 억제·전송)
```

## 5. 단계별 설계

### 5.1 메모 서비스 인증 책임 분리

각 메모 서비스에 `app/services/write_auth.py`를 추가함. 모듈은 서비스별 설정값·쿠키명·공개 origin·rate-limit 저장 경로를 인자로 받아 아래 동작을 제공함.

- 쓰기 로그인 세션 생성·검증·폐기
- 로그인 실패 횟수의 파일 기반 저장과 동시 접근 잠금
- 쓰기 요청 Origin 검증
- 브라우저 로그인 redirect 경로 검증
- 로그인 응답과 보안 헤더 적용에 필요한 보조 정보

`main.py`는 서비스별 URL과 템플릿·도메인 상수만 보유하고, 기존 private helper 이름은 얇은 위임 함수 또는 동일 동작 호출로 유지함. 기존 UI 계약 테스트는 로그인, 로그아웃, Origin 거부, 프로세스 간 rate-limit 유지, redirect 검증을 계속 보장함.

### 5.2 포털 라우터 책임 분리

`dashboard.py`에는 포털 홈, 서비스 이동, 검색만 남김. 관리자 상태 로그인·상태 조회와 HomeOps diagnose/approve/execute/scan 라우트는 `routers/admin.py`로 이동함.

공통 인증 함수는 새 라우터 내부 private helper로 이동하며, 쿠키명, 세션 만료, 보안 이벤트명, HTTP 응답은 변경하지 않음. `main.py`는 두 router를 등록하되 기존 경로를 그대로 제공함.

### 5.3 뉴스 보관 서비스 책임 분리

`news_archive.py`의 공개 함수 `collect_korean_news`, `list_recent_news`, `get_korean_categories` 및 테스트가 사용하는 호환 보조 함수는 유지함. 내부 구현을 세 모듈로 이동함.

| 모듈 | 책임 |
|---|---|
| `news_archive_storage.py` | archive 파일 경로, schema 기본값, 읽기·원자적 저장, 만료 데이터 제거 |
| `news_archive_processing.py` | 기사 정규화, 카테고리·검색, URL 중복 제거, market event 중복 판정 |
| `news_archive_notifications.py` | Telegram 알림 대기열, topic cooldown, digest 선택·전송 결과 반영 |

Facade는 기존 함수 시그니처와 반환 데이터를 유지하고, refresh thread·lock의 수명도 기존과 동일하게 보존함. 스케줄러는 이 facade만 계속 호출하므로 변경하지 않음.

### 5.4 테스트 구조 정리

- 각 리팩터링 단계의 기존 서비스 테스트를 먼저 실행함.
- 이동된 책임의 경계에 단위 테스트를 추가함. 예: 인증 설정별 cookie path와 origin 검증, archive storage의 만료 제거, notification cooldown과 전송 실패 대기열 보존.
- 전체 변경마다 `python3 tests/run_service_tests.py`와 `git diff --check`를 실행함.
- CI workflow는 수정하지 않음. 현재 CI의 서비스별 suite 구성을 유지함.

## 6. 비기능·호환성 기준

| 구분 | 성공 기준 |
|---|---|
| 기능 | 기존 라우트·템플릿·응답·환경 변수·SQLite 데이터가 유지됨 |
| 보안 | 인증 session, Origin 검증, rate-limit, security header 동작이 기존 테스트로 유지됨 |
| 운영 | Compose, Dockerfile, 배포 workflow, scheduler 파일 변경 없음 |
| 품질 | 대형 모듈의 역할이 분리되고, 각 새 모듈의 책임이 파일명과 테스트로 드러남 |
| 검증 | 전체 서비스 테스트와 CI가 통과함 |

## 7. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 인증 helper 이동으로 cookie/session 동작 변경 | 기존 UI 계약 테스트를 먼저 실행하고 helper별 회귀 테스트를 추가함 |
| archive 분리 중 알림 상태 손실 | 기존 archive JSON 키를 변경하지 않고 storage 경계 테스트를 추가함 |
| private helper를 직접 참조하는 기존 테스트 실패 | facade 또는 위임 함수를 유지하고 단계별 테스트로 확인함 |
| 서비스 간 코드 공통화가 배포 구조를 침범 | 서비스 내부 모듈화만 수행하고 Docker·Compose 변경을 하지 않음 |

## 8. 완료 기준

1. 메모 서비스, 포털, 뉴스 보관 서비스가 설계한 책임 경계로 분리됨.
2. 서버 기동·스케줄러·Compose·배포 workflow 변경이 없음.
3. 전체 서비스 테스트와 CI가 통과함.
4. PR 병합 후 N100 배포 health 검증이 통과함.
5. 병합된 작업 브랜치와 분리 작업공간이 자동 정리됨.
