# N100 GitHub 자동배포 안내

> 서비스·도메인·환경변수의 최신 기준은 [운영 참조](operations-reference.md)를 우선함. 이 문서는 N100 self-hosted runner 배포 절차와 장애 대응만 다룸.

## 현재 방식

N100 자체에 GitHub Actions self-hosted runner를 설치합니다. `main` 브랜치에서 실행된 CI가 성공하면 GitHub Actions가 N100에서 직접 실행되고, `C:\personal-server`의 `scripts/deploy-n100.sh`가 Docker Compose 서비스를 재빌드·재기동합니다.

N100은 Windows self-hosted runner가 로컬에서 배포를 실행하므로 GitHub-hosted runner의 SSH 접근, 포트포워딩, 배포용 개인키가 필요하지 않음.

## Runner 최초 등록

저장소의 GitHub 화면에서 다음 메뉴로 이동합니다.

```text
Settings → Actions → Runners → New self-hosted runner → Windows → x64
```

N100의 관리자 PowerShell에서 Runner를 `C:\actions-runner`에 설치한 뒤, GitHub가 표시하는 일회성 토큰으로 등록합니다.

```powershell
cd C:\actions-runner
.\config.cmd --url https://github.com/Len-Yoon/personal-server --token <새로 발급한 토큰>
```

등록 질문에는 Runner 이름을 `N100`으로 지정하고, 작업 폴더는 기본값 `_work`를 사용합니다. 서비스 설치 질문에는 `Y`를 선택합니다. GitHub Runner 서비스가 `Running`이고 시작 유형이 `Automatic`인지 확인합니다.

WSL2 배포를 사용하는 경우 Runner Windows 서비스의 로그온 계정은 WSL 배포판을 설치한 Windows 사용자(`.\window`)로 설정해야 합니다. `NETWORK SERVICE` 계정은 해당 사용자의 WSL 배포판이나 Docker 소켓을 사용할 수 없습니다.

Runner 토큰은 채팅, 저장소, 문서에 기록하지 않습니다. 토큰이 노출되면 GitHub에서 즉시 새 토큰을 발급합니다.

## N100 전제조건

`C:\personal-server`에 다음 항목이 있어야 합니다.

```text
C:\personal-server\.git
C:\personal-server\.env
C:\personal-server\data
C:\personal-server\docker-compose.yml
C:\personal-server\docker-compose.n100.yml
```

또한 N100에서 WSL2와 Docker가 다음처럼 동작해야 합니다.

```powershell
Get-Command docker
wsl -l -v
wsl.exe -d Ubuntu-24.04 -- bash -lc "docker version && docker compose version"
```

Runner Windows 서비스도 WSL2를 설치한 Windows 사용자 계정으로 실행해야 합니다.
`services.msc`에서 해당 Runner 서비스의 `로그온` 계정은 WSL 배포판을 설치한 Windows
사용자로 설정합니다. 계정에 Windows 로그인 비밀번호가 없으면 서비스가 로그온 오류
1069로 시작하지 않을 수 있습니다.

`.env`와 `data`는 운영 데이터이므로 GitHub에 올리지 않고 N100에만 보관합니다.

## 자동배포 흐름

`.github/workflows/deploy-n100.yml`은 `main`에서 완료된 `CI` workflow가 성공한 경우에만 반응하며 다음 Runner 라벨을 요구합니다.

```yaml
runs-on: [self-hosted, Windows, X64]
```

1. `main`에서 실행된 CI가 성공합니다. 실패하거나 취소된 CI는 N100에 배포하지 않습니다.
2. N100 Runner 서비스가 배포 작업을 받습니다.
3. `C:\personal-server`의 존재와 `.env`, `data`, Compose 파일을 확인합니다.
4. Ubuntu-24.04 WSL에서 `scripts/deploy-n100.sh`를 실행합니다.
5. 스크립트가 `git fetch --prune origin`과 `git reset --hard origin/main`으로 **추적되는 코드 파일만** 최신 main에 맞춤.
6. Compose 설정을 검증한 뒤 `docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build portal-web homeops-executor system-agent crawler-worker youtube-memo book-memo caddy`를 실행합니다.
7. workflow가 모든 Compose 서비스의 실행 상태와 `portal-web`, `system-agent`, `crawler-worker`, `youtube-memo`, `book-memo`, `homeops-executor` health 상태를 확인합니다. 이 단계가 실패하면 배포 workflow도 실패로 표시됩니다.

같은 브랜치의 배포가 겹치면 기존 배포가 끝난 뒤 다음 배포가 실행됩니다.
배포 스크립트는 원격 `main`을 기준으로 `git reset --hard`를 수행하므로
`C:\personal-server`에서 수동으로 수정한 추적 파일은 보존되지 않음.
운영 데이터인 `.env`와 `data/`는 별도 경로라 유지됩니다.

## 확인과 장애 대응

GitHub 저장소의 `Actions → Deploy N100`에서 실행 결과를 확인합니다. N100에서 Runner가
`Offline`이면 `services.msc`에서 저장소에 연결된 Actions Runner 서비스를 확인합니다.

Docker 또는 배포 실패 시 N100에서 다음 명령을 실행합니다.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml ps"
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml logs --tail=100"
```

Windows 작업 스케줄러 설정은 이 workflow의 상시 서비스 배포와 별개로 유지됩니다.
HomeOps 정기 점검은 Windows bootstrap이 5분마다 호출하며, 이 workflow가 그 주기를 변경하지 않음.

자동배포가 아닌 수동 배포가 필요할 때는 다음 명령을 사용합니다.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && bash ./scripts/deploy-n100.sh"
```

## 관련 파일

- `.github/workflows/ci.yml`: pull request와 main/master push의 단위 테스트
- `.github/workflows/deploy-n100.yml`: CI 성공 후 N100 self-hosted 배포와 health 확인
- `scripts/deploy-n100.sh`: N100에서 실행되는 단일 배포 진입점
