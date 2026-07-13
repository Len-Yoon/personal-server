# GitHub SSH 자동배포 설계

## 목표

`main` 브랜치에 push되면 N100 서버가 자동으로 최신 코드를 반영하고,
현재의 Docker Compose 운영 구성을 그대로 다시 띄우도록 만든다.
로컬 개발은 유지하고, 서버는 빌드와 실행만 담당하도록 분리한다.

## 범위

- 대상은 `main` 브랜치 push 이벤트로 제한한다.
- 배포 방식은 GitHub Actions + SSH + 서버 내 `git pull`/`docker compose up` 조합으로 한다.
- 이미지 레지스트리, self-hosted runner, Kubernetes는 사용하지 않는다.
- `server`와 `scheduler` 역할에 해당하는 기존 Windows/N100 운영 절차는 수정하지 않는다.

## 실행 방식

GitHub Actions가 `main` push를 감지하면 N100 서버에 SSH로 접속한다.
서버의 배포 디렉터리에서 최신 커밋을 가져오고,
`docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build`를 실행한다.

서버에는 다음 전제가 필요하다.

- Docker와 Docker Compose가 설치되어 있어야 한다.
- 배포용 Git 저장소가 이미 체크아웃되어 있어야 한다.
- `.env`와 `data/` 디렉터리는 서버에 유지되어야 한다.
- GitHub Secrets에 SSH 접속 정보와 배포 경로가 저장되어야 한다.

## 보안 및 운영

- SSH 키는 저장소에 두지 않고 GitHub Secrets로만 관리한다.
- 배포 대상 호스트와 사용자명은 환경변수 또는 Secrets로 분리한다.
- 배포 스크립트는 코드 저장소 안에 두어 명령을 버전 관리한다.
- `git reset --hard`는 서버 배포 디렉터리 전용으로만 사용한다.

## 검증

- workflow 파일이 `main` push에만 반응하는지 확인한다.
- workflow가 필요한 Secrets 이름을 모두 참조하는지 확인한다.
- 서버 배포 스크립트가 `git fetch`/`git reset --hard origin/main`/compose 재기동을 포함하는지 확인한다.
- README와 운영 문서에 초기 서버 준비와 배포 확인 절차를 명시한다.
