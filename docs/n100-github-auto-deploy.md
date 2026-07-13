# N100 GitHub 자동배포 안내

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | N100 GitHub 자동배포 안내 |
| 작성일 | 2026-07-13 |
| 작성자 | Codex |
| 기준 자료 | `docker-compose.yml`, `docker-compose.n100.yml`, `.github/workflows/deploy-n100.yml`, `scripts/deploy-n100.sh` |
| 목적 | `main` push 후 N100 서버 자동 반영 방법 정리 |
| 비고 | 레지스트리와 self-hosted runner는 사용하지 않음 |

## 핵심 요약

`main` 브랜치에 push되면 GitHub Actions가 N100 서버에 SSH 접속을 수행하고,
서버 배포 디렉터리에서 `scripts/deploy-n100.sh`를 실행함.
N100은 이미 체크아웃된 저장소와 `.env`, `data/`를 유지한 채
Docker Compose 스택만 다시 올림.

## 상세 내용

### 1. 필요한 GitHub Secrets

| 항목 | 내용 | 비고 |
|---|---|---|
| `N100_SSH_HOST` | N100 서버 접속 호스트 | 필수 |
| `N100_SSH_PORT` | SSH 포트 | 기본값 `22` 사용 가능 |
| `N100_SSH_USER` | SSH 사용자명 | 필수 |
| `N100_SSH_KEY` | 배포용 개인키 | 필수 |
| `N100_SSH_KNOWN_HOSTS` | `known_hosts` 내용 | 필수 |
| `N100_APP_DIR` | N100 배포 경로 | 필수 |

### 2. 서버 전제

| 항목 | 내용 | 비고 |
|---|---|---|
| 저장소 체크아웃 | 배포 경로에 Git 저장소가 이미 있어야 함 | 최초 1회 수동 준비 필요 |
| `.env` | 서버에 별도 보관 | Git에 포함하지 않음 |
| `data/` | 서버에 유지 | DB, 파일함, 로그 보관용 |
| Docker | 설치 필요 | `docker compose` 실행 가능해야 함 |
| 권한 | 배포 사용자에게 해당 경로 쓰기 권한 필요 | 필수 |

### 3. 배포 흐름

| 순서 | 내용 | 비고 |
|---|---|---|
| 1 | `main`에 push 발생 | GitHub Actions 트리거 |
| 2 | Actions가 SSH 키를 로드함 | Secrets 사용 |
| 3 | Actions가 N100 서버에 SSH 접속함 | known_hosts 검증 사용 |
| 4 | 서버에서 `git fetch --prune origin` 실행함 | 최신 원격 브랜치 동기화 |
| 5 | 서버에서 `git reset --hard origin/main` 실행함 | 배포 디렉터리 기준 맞춤 |
| 6 | `docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build` 실행함 | 서비스 재기동 |
| 7 | `docker compose ... ps`로 상태를 확인함 | 최종 확인 |

### 4. 수동 최초 준비

N100에 처음 적용할 때는 배포 경로에 저장소 checkout을 먼저 준비해야 함.
그 이후부터는 GitHub push만으로 자동 반영 가능함.

배포 전에 아래 항목을 한 번 확인해야 함.

- `.env`가 배포 경로에 존재하는지 확인함
- `data/` 경로가 존재하는지 확인함
- Docker Compose가 정상 동작하는지 확인함
- `N100_SSH_KNOWN_HOSTS` 값이 정확한지 확인함

### 5. 확인 기준

| 항목 | 정상 기준 | 비고 |
|---|---|---|
| GitHub Actions | `main` push 시 실행됨 | 확인 필요 |
| SSH 접속 | 서버 접속 성공 | 확인 필요 |
| 코드 반영 | `origin/main` 기준으로 맞춰짐 | 확인 필요 |
| 서비스 재기동 | Compose 스택이 다시 올라옴 | 확인 필요 |
| 기존 데이터 | `.env`와 `data/`가 유지됨 | 확인 필요 |

## 검토 결과

- 자동배포는 레지스트리 없이 구성 가능함.
- N100은 빌드만 수행하므로 추가 인프라가 적음.
- 운영 경로와 비밀값은 서버에 남겨둘 수 있음.

## 확인 필요 사항

- 배포용 SSH 계정이 N100에서 어떤 경로를 기준으로 쓰는지 확인 필요함.
- `N100_SSH_PORT`를 22 이외 값으로 쓸지 확인 필요함.
- `N100_SSH_KNOWN_HOSTS`를 GitHub Secrets에 어떻게 등록할지 확인 필요함.

## 후속 조치

- 첫 배포 전에 N100 배포 경로에 저장소 checkout을 완료해야 함.
- `main` push 후 Actions 로그와 N100 컨테이너 상태를 함께 확인해야 함.
- 배포 실패 시 서버의 `docker compose ps`와 GitHub Actions 로그를 우선 확인해야 함.
