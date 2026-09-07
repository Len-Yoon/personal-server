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
| `apply_news_observability` | 변경 | 현재 workflow checkout의 두 manifest를 제한형 root helper가 객체 정체성 검증 후 적용 |

## 금지 범위

- 서버 시작 방식, scheduler, Caddy, Cloudflare Tunnel 설정을 수정하지 않음.
- Secret 생성·수정·출력, PVC·운영 데이터 변경, 임의 kubectl 명령, 임의 Docker 명령을 제공하지 않음.
- sudo·rclone·Telegram·metrics bearer 값은 저장·출력·전달하지 않음.
- workflow input은 operation choice 하나만 받으며, branch·SHA·파일 경로·명령 문자열은 받지 않음.

## 구조

```text
Codex → GitHub workflow_dispatch → main CI 통과 SHA 확인 → N100 Windows self-hosted runner
                                      ↓
                                  wsl.exe -d Ubuntu-24.04
                                      ↓
                          scripts/run-n100-operations.sh <fixed operation>
                                      ↓
            root 소유 제한형 helper → 고정된 진단·검증·검증된 적용만 실행
```

Workflow는 runner label `self-hosted, Windows, X64`를 요구함. `refs/heads/main`의 현재 GitHub SHA와 동일한, 성공한 push CI가 있을 때만 동작함. checkout은 credential을 보존하지 않음. WSL은 `bash --noprofile --norc`로 고정 script만 실행하며 사용자 입력·경로를 셸 문자열로 조합하지 않음. 읽기 전용 작업은 운영 checkout `/mnt/c/personal-server`만 참조하고, manifest 적용은 Actions checkout의 검증된 revision 데이터를 stdin으로만 helper에 전달함.

## 실패 처리

- 알 수 없는 operation, WSL 미설치, 제한형 helper 권한 부재, 필수 서비스·Secret key·리소스 부재는 실패로 종료함.
- `diagnose`는 개별 health 실패를 수집해 요약 후 실패로 종료함.
- 변경 작업은 각각 시작 전에 preflight를 수행하며, 실패 시 후속 변경을 수행하지 않음.
- 배포는 기존 안전 배포 도구의 revision 고정·health·rollback 동작을 그대로 사용함.
- manifest 적용은 helper 내부에서 client dry-run 후 실제 apply를 수행함. helper는 stdin의 정확히 두 YAML 문서만 허용하고, symlink·다중 문서·`kind: List`·API/kind/namespace/name 불일치를 거부함.

## 보안 경계

- Actions 로그는 상태, 이름, exit status만 출력함. 환경변수와 Secret 내용은 출력하지 않으며 xtrace·`printenv`·`docker inspect`·Secret YAML/JSON 출력은 금지함.
- runtime token은 컨테이너 내부에서 비어 있지 않은지만 확인하며 값을 읽어 host에 반환하지 않음.
- Kubernetes Secret은 `bearer_token` key 존재 여부만 JSONPath 결과를 `/dev/null`로 보내 확인함.
- 권한은 root 소유 `/usr/local/libexec/personal-server/n100-k3s-operations` helper의 정확한 operation 인자만 허용하는 sudoers 항목으로 축소함. runner 사용자는 helper·sudoers를 수정할 수 없음. 일반 `sudo -n k3s`는 사용하지 않음.
- helper는 `diagnose`, `verify_news_observability`, `apply_news_observability`만 처리하며, 배포는 비권한 기존 안전 배포 도구만 사용함. 설치는 N100 관리자가 일회성으로 수행하는 별도 절차임.
- workflow 권한은 `actions: read`, `contents: read`로 제한하며, 모든 변경 operation은 기존 안전 배포 workflow와 동일한 concurrency group에서 `cancel-in-progress: false`로 직렬화함.

## 검증

1. operation allowlist·입력 거부·금지 경로를 단위 테스트함.
2. workflow가 수동 실행만 허용하고 Windows runner·고정 operation·concurrency를 사용하는지 계약 테스트함.
3. shell script와 helper가 고정된 argv·stdin 객체 정체성만 허용하는지 mock 기반 계약 테스트함.
4. 실제 N100에서는 `diagnose`를 먼저 실행하고, `verify_news_observability`와 P4 상태를 확인함.

## 성공 기준

- GitHub Actions에서 고정된 네 작업만 선택 가능함.
- runner가 WSL에서 실행되며 arbitrary command injection·root 경로 주입 경로가 없음.
- Secret 값이 workflow 로그·테스트 산출물에 포함되지 않음.
- `diagnose`와 `verify_news_observability`가 원격 SSH 없이 결과를 반환함.
- P4 manifest 적용은 사전 시딩된 Secret을 변경하지 않고, 정확히 두 고정 리소스만 검증된 stdin으로 적용함.
