# N100 GitHub 자동배포 안내

> 서비스·도메인·환경변수의 최신 기준은 [운영 참조](operations-reference.md)를 우선함. 이 문서는 N100 self-hosted runner 배포 절차와 장애 대응만 다룸.

## 현재 방식

N100 자체에 GitHub Actions self-hosted runner를 설치합니다. 기능 브랜치 PR을 병합해 `main`이 변경되면 CI가 실행됩니다. `main` push로 시작한 자동 배포 workflow는 해당 push의 정확한 `github.sha` CI가 성공할 때까지 GitHub API로 대기합니다. 성공한 경우에만 push의 정확한 `before`→`github.sha` 전체 변경을 분류하고, 안전 범위에 해당하는 변경만 N100에서 `scripts/deploy-n100-safe.sh`로 배포합니다.

이 자동 배포는 revision 고정 방식임. 배포 중 원격 `main`의 더 최신 커밋으로 바꾸지 않으며, 기존의 수동 배포 스크립트 `scripts/deploy-n100.sh`도 수정하거나 호출하지 않음.

N100은 Windows self-hosted runner가 로컬에서 배포를 실행하므로 GitHub-hosted runner의 SSH 접근, 포트포워딩, 배포용 개인키가 필요하지 않음.

## N100 제한형 운영 작업

GitHub 저장소에서 `Actions → N100 Operations → Run workflow`를 선택해 고정된 운영 작업 하나를 수동 실행함. 이 workflow는 현재 `main`의 최신 커밋과 일치하고 성공한 `main` push CI가 확인된 경우에만 실행되며, N100에 SSH로 접속하지 않음. `main`이 그 사이 변경되거나 CI가 성공하지 않으면 fail-closed로 중단함.

| operation | 변경 여부 | 허용 범위 |
|---|---|---|
| `diagnose` | 읽기 전용 | Docker, cloudflared, 공개·로컬 health, K3s 상태 확인 |
| `verify_news_observability` | 읽기 전용 | crawler health, runtime token·Secret key 존재 여부, 고정 관측성 리소스 확인 |
| `deploy_safe_crawler` | 변경 | 기존 안전 배포 도구로 현재 검증된 main SHA의 `crawler-worker`만 배포 |
| `apply_news_observability` | 변경 | 검증된 두 manifest의 고정 리소스만 제한형 root helper로 적용 |

실행 순서는 반드시 `diagnose → verify_news_observability → 필요한 변경 operation`으로 진행함. 진단 또는 검증이 실패하면 변경 작업을 실행하지 않음. `diagnose`·`verify_news_observability`는 읽기 전용임. `deploy_safe_crawler`의 health 검증과 직전 정상 revision 1회 rollback은 기존 안전 배포 도구에만 있음.
요약 순서는 `diagnose → verify → 필요한 변경`임.
`apply_news_observability`는 제한형 helper가 client dry-run 후 두 리소스를 순서대로 apply함. helper는 health/undo/자동 rollback을 수행하지 않음. 실제 apply 중 첫 리소스가 적용된 뒤 다음 리소스에서 실패하는 partial apply 가능성이 있음. apply 실패 대응은 `verify_news_observability`와 Kubernetes 리소스 확인 후 운영자 판단으로 제한하며 자동 복구를 약속하지 않음. 어떤 변경 operation도 실패 시 후속 변경을 수행하지 않음.

### 수동 호스트 단계: root helper 일회성 설치

아래 명령은 GitHub Actions가 실행하지 않으며, N100 관리자만 N100 WSL 호스트에서 일회성으로 수행하는 수동 호스트 단계임. 명령·문서·로그에 토큰, Secret 값 또는 기타 비밀값을 입력·기록하지 않음.

```bash
cd /mnt/c/personal-server
sudo ./infra/k8s/tools/install-n100-k3s-operations-helper.sh
```

설치기는 generic broad k3s 권한이 이미 허용되어 있거나 권한 조회 결과가 예상과 다르면 설치를 차단함. 설치 후 root helper는 `diagnose`, `verify_news_observability`, `apply_news_observability` 세 가지 정확한 root operation만 허용하며, 일반적인 `sudo k3s`·임의 kubectl·임의 명령은 허용하지 않음. 설치 또는 사후 권한 점검이 실패하면 기존 파일을 복원하고, 복원까지 실패한 경우 관리자 확인용 보호 백업을 남긴 뒤 실패로 종료함.

### 실제 N100 smoke checklist

1. GitHub Actions에서 `N100 Operations` 실행 전 `main` 최신 CI가 성공했는지 확인함.
2. `diagnose`를 실행해 Docker, cloudflared, 로컬·공개 health, K3s 상태를 확인함.
3. `verify_news_observability`를 실행해 crawler health와 리소스·key 존재 여부를 확인함. Secret 값은 출력하지 않음.
4. 변경이 필요할 때만 해당 변경 operation을 실행하고 Actions 로그의 `n100_operation`·`n100_step` 결과를 확인함.
5. 실패 사례(오래된 main, CI 실패, Runner Offline, helper 권한 부재, health 실패, 고정 리소스 불일치)는 Actions 로그에서 원인을 확인하고 추가 변경 없이 중단함. Runner가 Offline이면 N100 Windows Runner 서비스 상태를 확인 필요함.

이 경로의 운영 경계는 서버 bootstrap, scheduler, Caddy, Cloudflare Tunnel 설정, Kubernetes Secret 값·생성, PVC, 운영 데이터에 있음. 해당 영역은 계속 제외되며 이 workflow에서 변경하지 않음.

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

`.github/workflows/deploy-n100.yml`은 `main` push로 시작합니다. 기본 변경 경로는 기능 브랜치 PR 병합이며, 긴급 복구를 제외한 `main` 직접 push는 사용하지 않음. 배포 workflow는 해당 push의 CI 성공을 GitHub API에서 확인하기 전까지 N100 runner를 할당하지 않음.

```yaml
runs-on: [self-hosted, Windows, X64]
```

1. 개발 PC에서 기능 브랜치를 push하고 PR CI·Agent Review를 통과시킨 뒤, 사용자 승인으로 PR을 `main`에 병합합니다. `main` CI가 실패하거나 취소되면 N100에 배포하지 않습니다.
2. 변경 분류 job은 push event의 정확한 `before` SHA와 `github.sha`를 사용합니다. `before` SHA가 없거나 초기화 값이면 fail-closed로 중단합니다. GitHub API는 정확히 `github.sha`와 일치하는 `CI` push run을 최대 10분 대기하고, 성공하지 않으면 중단합니다. CI 성공 후 `before`부터 `github.sha`까지 전체 변경 경로를 GitHub-hosted runner에서 검사합니다. 이 job이 먼저 실행되므로 차단 변경은 N100 runner를 사용하지 않음.
3. 문서만 변경된 경우에는 `skip`으로 끝납니다. 허용 서비스 코드만 변경된 경우에는 해당 서비스 이름만 선택됩니다. 허용 범위와 차단 범위가 섞이면 `blocked`로 실패하며 자동 배포하지 않음.
4. `deploy`일 때에만 N100 Runner가 `N100_SAFE_DEPLOY_SHA`와 선택된 서비스 목록을 WSL에 전달합니다.
5. `scripts/deploy-n100-safe.sh`는 해당 SHA가 `origin/main`의 조상이며 현재 최신 `origin/main` SHA와 정확히 일치하는지 확인합니다. 더 최신 main이 있으면 안전하게 중단합니다. `git checkout`과 `git reset --hard`는 사용하지 않음.
6. 배포할 revision의 선택 서비스 소스(`Dockerfile`, `requirements.txt`, `app/`)만 `git archive`로 runner 전용 release 디렉터리에 추출합니다. 임시 Compose override는 해당 서비스의 build context와 `/app` bind mount만 release 디렉터리로 바꾸며, 운영 작업공간의 Compose 파일·`.env`·`data`는 그대로 유지합니다.
7. 선택한 서비스의 Compose 설정, 컨테이너 health, loopback `/health`가 최대 90초 동안 모두 정상인지 확인합니다.
8. 첫 배포 또는 health 확인이 성공하면 그 SHA를 마지막 정상 revision으로 기록합니다.
9. 새 revision의 health가 실패하고 직전 정상 revision이 있으면, 그 revision의 독립 release 디렉터리로 한 번만 배포·health 확인하여 복구합니다. 복구까지 실패하면 workflow는 실패로 끝남.

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

Telegram 알림도 1차 CD 범위에서 제외됨. 자동 배포 결과와 차단·복구 판단은 GitHub Actions 단계와 로그가 운영 신호임. Telegram 연동은 별도 설계·검토 후 진행 필요.

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
- `.github/workflows/deploy-n100.yml`: main push의 정확한 CI 성공 확인 후 safe scope 분류와 N100 self-hosted 배포
- `scripts/classify-n100-safe-deployment.py`: CI revision의 허용·차단·건너뜀 분류
- `scripts/deploy-n100-safe.sh`: revision 고정 배포, health 및 직전 정상 revision 1회 복구
- `scripts/verify-n100-safe-deployment-health.sh`: 선택된 허용 서비스의 health 확인
- `scripts/deploy-n100.sh`: 자동 배포와 별개인 기존 수동 Compose 배포 진입점
