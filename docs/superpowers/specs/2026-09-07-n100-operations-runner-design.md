# N100 제한형 운영 실행기 설계

## 목적

Codex가 N100 사설망에 직접 SSH로 접속하지 못하는 환경에서도, 이미 등록된 Windows self-hosted GitHub Actions runner가 로컬 WSL에서 승인된 운영 작업만 실행하고 결과를 GitHub Actions 로그로 반환하도록 함.

## 범위

새 GitHub Actions workflow는 `workflow_dispatch`로만 시작함. 임의 셸 명령, 경로, URL, Secret 값을 입력으로 받지 않음. 실행 가능한 작업은 고정된 operation 식별자로 제한함.

| 작업 | 변경 여부 | 허용 범위 |
|---|---|---|
| `diagnose` | 읽기 전용 | Docker 상태, Cloudflare Tunnel 프로세스, 공개·로컬 health, K3s Portal 상태를 Secret 없이 확인 |
| `deploy_safe_crawler` | 변경 | 기존 `scripts/deploy-n100-safe.sh`만 사용하여 현재 검증된 main SHA의 crawler-worker만 배포 |
| `verify_news_observability` | 읽기 전용 | crawler health, runtime token 존재 여부, Secret key 존재 여부, ServiceMonitor·PrometheusRule 존재 여부를 값 없이 확인 |
| `apply_news_observability` | 변경 | 현재 workflow checkout의 `crawler-news-observability.yaml`과 `prometheus-rule.yaml` 두 파일만 `sudo -n k3s kubectl apply`로 적용 |

## 금지 범위

- 서버 시작 방식, scheduler, Caddy, Cloudflare Tunnel 설정을 수정하지 않음.
- Secret 생성·수정·출력, PVC·운영 데이터 변경, 임의 kubectl 명령, 임의 Docker 명령을 제공하지 않음.
- sudo·rclone·Telegram·metrics bearer 값은 저장·출력·전달하지 않음.
- workflow input은 operation choice 하나만 받으며, branch·SHA·파일 경로·명령 문자열은 받지 않음.

## 구조

```text
Codex → GitHub workflow_dispatch → N100 Windows self-hosted runner
                                      ↓
                                  wsl.exe -d Ubuntu-24.04
                                      ↓
                          scripts/run-n100-operations.sh <fixed operation>
                                      ↓
                       고정된 진단·배포·검증·적용 명령만 실행
```

Workflow는 runner label `self-hosted, Windows, X64`를 요구함. WSL 실행 시 repository의 Actions checkout 경로와 운영 checkout `/mnt/c/personal-server`를 명시적으로 구분함. 읽기 전용 작업은 운영 checkout만 참조하고, manifest 적용은 Actions checkout에 있는 검증된 해당 revision의 두 파일만 참조함.

## 실패 처리

- 알 수 없는 operation, WSL 미설치, `sudo -n k3s` 권한 부재, 필수 서비스·Secret key·리소스 부재는 실패로 종료함.
- `diagnose`는 개별 health 실패를 수집해 요약 후 실패로 종료함.
- 변경 작업은 각각 시작 전에 preflight를 수행하며, 실패 시 후속 변경을 수행하지 않음.
- 배포는 기존 안전 배포 도구의 revision 고정·health·rollback 동작을 그대로 사용함.
- manifest 적용은 client dry-run 후 실제 apply를 수행함. apply 대상은 상수 배열로 고정함.

## 보안 경계

- Actions 로그는 상태, 이름, exit status만 출력함. 환경변수와 Secret 내용은 출력하지 않음.
- runtime token은 컨테이너 내부에서 비어 있지 않은지만 확인하며 값을 읽어 host에 반환하지 않음.
- Kubernetes Secret은 `bearer_token` key 존재 여부만 JSONPath 결과를 `/dev/null`로 보내 확인함.
- 권한은 기존 self-hosted runner 및 제한된 `sudo -n k3s` 경계보다 넓어지지 않음.

## 검증

1. operation allowlist·입력 거부·금지 경로를 단위 테스트함.
2. workflow가 수동 실행만 허용하고 Windows runner·고정 operation·concurrency를 사용하는지 계약 테스트함.
3. shell script의 각 operation이 허용된 명령만 구성하는지 mock 기반 계약 테스트함.
4. 실제 N100에서는 `diagnose`를 먼저 실행하고, `verify_news_observability`와 P4 상태를 확인함.

## 성공 기준

- GitHub Actions에서 고정된 네 작업만 선택 가능함.
- runner가 WSL에서 실행되며 arbitrary command injection 경로가 없음.
- Secret 값이 workflow 로그·테스트 산출물에 포함되지 않음.
- `diagnose`와 `verify_news_observability`가 원격 SSH 없이 결과를 반환함.
- P4 manifest 적용은 사전 시딩된 Secret을 변경하지 않고 두 고정 리소스만 적용함.
