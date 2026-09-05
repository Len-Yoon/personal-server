# N100 GitHub 자동배포 안내

> 서비스·도메인·환경변수의 최신 기준은 [운영 참조](operations-reference.md)를 우선함. 이 문서는 N100 self-hosted runner 배포 절차와 장애 대응만 다룸.

## 현재 방식

N100 자체에 GitHub Actions self-hosted runner를 설치합니다. 기능 브랜치 PR을 병합해 `main`이 변경되면 CI가 실행됩니다. CI가 성공하면 GitHub Actions는 CI가 검증한 정확한 `workflow_run.head_sha`만 대상으로 변경을 분류합니다. 안전 범위에 해당하는 변경만 N100에서 `scripts/deploy-n100-safe.sh`로 배포합니다.

이 자동 배포는 revision 고정 방식임. 배포 중 원격 `main`의 더 최신 커밋으로 바꾸지 않으며, 기존의 수동 배포 스크립트 `scripts/deploy-n100.sh`도 수정하거나 호출하지 않음.

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

`.github/workflows/deploy-n100.yml`은 `main`에서 완료된 `CI` workflow가 성공한 뒤 동작합니다. 기본 변경 경로는 기능 브랜치 PR 병합이며, 긴급 복구를 제외한 `main` 직접 push는 사용하지 않음.

```yaml
runs-on: [self-hosted, Windows, X64]
```

1. 개발 PC에서 기능 브랜치를 push하고 PR CI·Agent Review를 통과시킨 뒤, 사용자 승인으로 PR을 `main`에 병합합니다. `main` CI가 실패하거나 취소되면 N100에 배포하지 않습니다.
2. 변경 분류 job은 GitHub-hosted runner에서 CI의 `workflow_run.head_sha`를 checkout하여 검사합니다. 이 job이 먼저 실행되므로 차단 변경은 N100 runner를 사용하지 않음.
3. 문서만 변경된 경우에는 `skip`으로 끝납니다. 허용 서비스 코드만 변경된 경우에는 해당 서비스 이름만 선택됩니다. 허용 범위와 차단 범위가 섞이면 `blocked`로 실패하며 자동 배포하지 않음.
4. `deploy`일 때에만 N100 Runner가 `N100_SAFE_DEPLOY_SHA`와 선택된 서비스 목록을 WSL에 전달합니다.
5. `scripts/deploy-n100-safe.sh`는 해당 SHA가 `origin/main`의 조상인지 확인한 뒤 detached checkout으로 배포합니다. `git reset --hard`는 사용하지 않음.
6. 선택한 서비스의 Compose 설정, 컨테이너 health, loopback `/health`를 확인합니다.
7. 첫 배포 또는 health 확인이 성공하면 그 SHA를 마지막 정상 revision으로 기록합니다.
8. 새 revision의 health가 실패하고 직전 정상 revision이 있으면, 그 revision을 한 번만 배포·health 확인하여 복구합니다. 복구까지 실패하면 workflow는 실패로 끝남.

### 자동 배포 허용 서비스

- `crawler-worker`
- `youtube-memo`
- `book-memo`
- `car-care-worker`

### 자동 배포 제외

다음 변경은 1차 CD에서 자동 적용하지 않음. 하나라도 포함되면 Actions의 변경 분류 job이 차단함.

- Portal, Caddy, system-agent, homeops-executor
- K3s, Secret, PVC, `data/`, credential 및 `.env`
- backup 및 scheduler
- 서버 bootstrap, Docker Compose 공통 설정, 배포·운영 스크립트의 그 밖의 경로

Portal과 K3s의 전환·배포는 이 workflow의 대상이 아님. 별도의 승인된 운영 절차로 진행 필요.

## 확인과 장애 대응

GitHub 저장소의 `Actions → Deploy N100`에서 실행 결과를 확인합니다. 실패 지점은 job 로그의 다음 값을 기준으로 확인함.

| 화면 표시 | 의미 | 조치 |
|---|---|---|
| `action=blocked` | 자동 배포 제외 경로가 함께 변경됨 | 자동 적용하지 말고 별도 운영 절차와 검토로 처리 필요 |
| `safe_cd_stage=preflight` | SHA, 서비스 목록 또는 N100 작업공간 사전조건을 확인하지 못함 | Actions 로그와 N100 Runner 상태 확인 필요 |
| `safe_cd_stage=deploy` | 선택 서비스의 Compose 배포가 실패함 | N100 Compose 상태와 해당 서비스 로그 확인 필요 |
| `safe_cd_stage=health` | 컨테이너 또는 loopback health 확인이 실패함 | 해당 서비스의 health endpoint 및 로그 확인 필요 |
| `safe_cd_stage=rollback` | 새 revision 실패 후 직전 정상 revision 복구를 시도함 | 복구 성공 여부를 확인하고 실패 시 수동 복구 필요 |

N100에서 Runner가 `Offline`이면 `services.msc`에서 저장소에 연결된 Actions Runner 서비스를 확인합니다.

Docker 또는 배포 실패 시 N100에서 다음 명령을 실행합니다.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml ps"
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && docker compose -f docker-compose.yml -f docker-compose.n100.yml logs --tail=100"
```

Windows 작업 스케줄러 설정은 이 workflow와 별개로 유지됩니다. HomeOps 정기 점검, backup 및 scheduler는 이 workflow가 변경하지 않음.

### 백업 자격 증명

N100 PVC 백업용 rclone 자격 증명 등록은 별도의 일회성 수동 작업임. 입력값은 화면·로그·Git에 남기지 않으며, 등록 과정과 실행 과정에서 마스킹되어야 함. 이 자동 배포 workflow는 자격 증명을 읽거나 등록하지 않음.

자동 배포 제외 대상 또는 긴급 수동 배포가 필요할 때는 승인된 운영 절차를 사용합니다. 기존 수동 Compose 배포가 필요한 경우에는 다음 명령을 사용합니다.

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "cd /mnt/c/personal-server && bash ./scripts/deploy-n100.sh"
```

## 관련 파일

- `.github/workflows/ci.yml`: pull request와 main/master push의 단위 테스트
- `.github/workflows/deploy-n100.yml`: CI 성공 후 safe scope 분류와 N100 self-hosted 배포
- `scripts/classify-n100-safe-deployment.py`: CI revision의 허용·차단·건너뜀 분류
- `scripts/deploy-n100-safe.sh`: revision 고정 배포, health 및 직전 정상 revision 1회 복구
- `scripts/verify-n100-safe-deployment-health.sh`: 선택된 허용 서비스의 health 확인
- `scripts/deploy-n100.sh`: 자동 배포와 별개인 기존 수동 Compose 배포 진입점
