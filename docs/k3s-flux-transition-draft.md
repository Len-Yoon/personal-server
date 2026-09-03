# K3s·Flux 전환 초안

> 상태: 검토 전용 초안. 이 문서와 `infra/k8s/`의 파일은 클러스터 적용 대상이 아니다.

## 목적과 경계

현재 `personal-server`의 Compose 기반 운영을 유지한 채, 추후 별도 승인으로 수행할 K3s·Flux 전환의 계약을 정의한다. 이 작업 브랜치는 Portal 단일 writer를 보장하기 위해 Compose 배포·Windows bootstrap의 marker 처리와 상태 사전검증을 변경한다. 아직 클러스터 cutover를 실행하거나 Flux를 연결하지 않는다.

이 초안에서 하지 않는 일은 다음과 같다.

- 실제 N100에서의 Compose 중지·재기동, Portal 상태 migration 또는 Caddy upstream 전환
- Docker Caddy의 호스트 포트·TLS 소유권 변경
- WSL 마운트 변경
- Flux 설치, bootstrap, GitRepository 연결 또는 Flux 리소스 적용
- Kubernetes Secret 생성·값 기록, PVC/PV/Deployment/Service의 클러스터 적용
- GitHub push, PR 생성 또는 원격 저장소 생성

현재 K3s에는 기본 Traefik과 ServiceLB가 없고, Docker Caddy가 호스트 `80`·`443`을 계속 점유한다. 따라서 초기 전환 단계에서 Kubernetes `Ingress` 및 `LoadBalancer` Service는 사용하지 않는다. K3s 애플리케이션은 승인 후에만 NodePort를 Caddy의 **향후 백엔드 계약**으로 사용한다. Caddy 설정 변경과 Docker↔K3s 연결 검증은 별도 변경으로 승인받는다.

## 목표 구조

현재 `personal-server` 저장소에서 검토하는 대상 경로는 아래와 같다. 이 경로는 초안일 뿐이며 Flux가 읽지 않는다. 별도 GitOps 저장소는 만들지 않았고, 향후 저장소 경계도 별도 승인 사항이다.

```text
clusters/
  n100/
    infra/
      namespaces/
      storage/
    apps/
      portal-web/
```

`infra/k8s/`는 위 구조의 최소 예시를 담는다. 리소스 예시는 Kustomize에 포함되지 않는 `.yaml.tmpl` 파일이며, 루트 Kustomize는 의도적으로 비어 있다. Git 연결 방식을 결정하기 전에도 환경별 Secret은 Git에 넣지 않는다.

## 1차 전환 범위

N100 사전 검증에서 `/mnt/c` 아래 데이터의 소유권·권한이 WSL 파일 공유 특성상 `0777`로 관찰되었다. 2026-08-28 N100 호스트 직접 검증에서 native ext4 후보 `/var/lib/rancher/k3s/storage`는 **통과(경로 준비 완료)**로 확인했다. `/dev/sdd` ext4의 `rw` 마운트에서 경로는 `root:root`·`0750`이고, K3s 노드 `desktop-utu2qat`은 Ready 상태다. `kube-system/local-path-config`의 기본 경로도 이 후보와 일치한다.

추가 scratch 검증에서 `local-path` 64Mi ReadWriteOnce PVC와 Pod를 생성해 PV가 이 경로 하위에 Bound되고 Pod가 Ready인 것을 확인했다. 임시 볼륨에서 파일 I/O와 SQLite `BEGIN IMMEDIATE` 잠금 충돌도 통과했다. 검증 후 임시 namespace와 PV 삭제 완료를 확인했으며, 운영 데이터·Compose·Caddy·Flux는 변경하지 않았다.

이는 K3s `local-path` 동적 provisioner의 기본 경로·동적 볼륨·일반 SQLite 잠금 동작만 확인한 결과다. Portal 이미지와 빈 동적 볼륨의 별도 smoke도 통과했지만, 앱 데이터 복사·복원, 앱별 UID/GID, crawler 프로필 잠금, 단일 writer cutover, Caddy 전환, Secret 주입은 수행하지 않았다. 동적 PVC를 실제로 만들고 바인딩하는 것은 별도 승인 후 진행한다. 대화형 셸에서 `EXIT trap`에 의존하지 않고 검증 리소스별 삭제와 잔존 여부를 명시적으로 확인해야 한다.

1차 전환 후보는 다음 네 서비스로만 제한한다.

- `portal-web`
- `crawler-worker`
- `youtube-memo`
- `book-memo`

다음 서비스와 인프라는 이번 초안의 대상에서 제외한다: `system-agent`, `homeops-executor`, `car-care-worker`, Docker Caddy. 이들은 host/Docker socket, OAuth·Telegram 상태, 또는 호스트 `80`·`443` 소유권 등 별도 설계가 필요하다.

## 영속 데이터와 Local PV 계약

Compose 데이터는 cutover 전까지 원본 위치와 Docker named volume을 계속 사용한다. Kubernetes가 원본을 동시에 쓰도록 연결하지 않는다. 특히 SQLite는 Compose와 Kubernetes가 동시에 접근하면 안 된다.

| 데이터 범주 | 전환 원칙 | Kubernetes 계약 | 적용 전 필수 확인 |
| --- | --- | --- | --- |
| `portal-web` | `data/files` | `local-path` 동적 PVC 후보(보류) | 용량, 데이터 복사·복원, UID/GID, 단일 writer |
| `crawler-worker` | `data/crawler-worker` | `local-path` 동적 PVC 후보(보류) | 위 항목 + Playwright 프로필 잠금 |
| `youtube-memo` | `data/youtube-memo` | `local-path` 동적 PVC 후보(보류) | 위 항목 + SQLite 무결성 |
| `book-memo` | `data/book-memo` | `local-path` 동적 PVC 후보(보류) | 위 항목 + SQLite 무결성 |
| Caddy named volume | Docker Caddy가 유지하는 동안 Kubernetes에 마운트하지 않음 | 별도 단계에서만 검토 | Docker volume 위치와 복구 절차 |
| car-care OAuth named volume | Compose와 동시 마운트 금지 | 전용 PV/PVC 및 Secret 계약 분리 | 토큰 파일 권한, 재인증 절차, 복구 검증 |
| SQLite 파일 | 서비스 중지 후 단일 writer 상태에서 복사 | PVC 하나를 하나의 서비스에만 연결 | `quick_check`, 파일 소유권, 롤백 가능성 |

`infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`은 K3s 기본 `local-path`와 용량 placeholder를 기록하는 PVC 계약이며 활성 Kustomize 트리에 포함되지 않는다. PV, custom StorageClass, host path, node affinity는 이 초안에 넣지 않는다.

## 단일 writer 게이트

각 SQLite 파일은 한 시점에 Compose 또는 K3s 중 하나의 writer만 가져야 한다. 전환 시에는 백업·`quick_check`를 먼저 수행하고, 해당 Compose 서비스만 정지한 뒤 복사본을 검증한다. PVC를 연결한 K3s Pod가 정상임을 확인하기 전까지 원본과 복사본을 동시에 쓰지 않는다. `crawler-worker`의 프로필·아카이브 파일도 같은 서비스의 단일 writer 경계로 취급한다.

## Secret 계약

Secret 값과 실제 Secret 리소스는 이 저장소에 작성하지 않는다. 배포 시점에는 SOPS/age 또는 승인된 외부 Secret Manager를 우선 검토하고, 수동 `kubectl` seed는 비상 절차로만 사용한다.

각 애플리케이션은 이름만 고정한 Secret 참조를 사용하고, 값·토큰·개인키·OAuth 산출물은 Git과 로그에 남기지 않는다. `portal-web` 예시는 `portal-web-runtime`이라는 참조명만 사용한다. Secret 생성 방식, 회전, 누출 대응, 복구 책임자는 Flux 적용 승인을 받기 전 보안 검토에서 확정한다.

## 네트워크와 Caddy 경계

1. Docker Caddy는 계속 호스트 `80`·`443`의 유일한 소유자다.
2. 초기 K3s 서비스는 `ClusterIP` 또는 검토된 NodePort만 사용한다. 이 초안의 `portal-web` NodePort는 외부 공개가 아닌 향후 Caddy 백엔드 후보 계약이다.
3. Caddy가 NodePort로 통신 가능한지, NodePort가 외부 네트워크에 불필요하게 노출되지 않는지, 방화벽과 kube-proxy 설정이 의도대로인지 별도 점검한다.
4. 위 검증과 명시적 승인이 전까지 Caddyfile 변경, Kubernetes Ingress, `LoadBalancer` Service를 만들거나 적용하지 않는다.

## 데이터 cutover와 롤백 순서

서비스별로 독립 승인하며, 한 번에 모든 서비스를 옮기지 않는다.

1. 암호화 백업의 원격 보관과 별도 경로 복원 검증 상태를 재확인한다.
2. 대상 서비스의 데이터·볼륨·SQLite 점검 항목과 예상 중단 시간을 승인받는다.
3. 해당 Compose 서비스만 정지한 뒤 데이터 복사본을 만들고 SQLite `quick_check`와 파일 소유권을 확인한다.
4. 승인된 용량과 데이터 복사본을 기준으로 해당 앱의 `local-path` 동적 PVC를 생성·바인딩하고, Secret을 승인된 주입 경로로 제공한다. 이 초안에서는 custom PV를 만들지 않는다.
5. Kubernetes 워크로드를 내부 검증용으로만 기동하고 데이터·로그·헬스 체크를 검증한다.
6. 별도 승인 후 Caddy 백엔드를 한 서비스만 전환한다. 전환 전 Compose 컨테이너는 롤백 가능한 상태로 유지한다.
7. 오류 시 Caddy를 이전 Compose 백엔드로 되돌리고, Kubernetes 워크로드를 중지한다. 데이터가 Kubernetes에서 변경되었다면 임의 재전환하지 말고 마지막 일관된 백업에서 복구 여부를 판단한다.

### 안전한 Secret seed 절차

1. 비밀번호 관리자에서 값을 직접 읽고 Git 평문·셸 history·로그에 남기지 않는다.
2. `stringData` 기반 dry-run 또는 SOPS/age 복호화 결과를 stdin/권한 `0600` 임시 경로로만 전달한다.
3. 생성 전 이름·키 목록만 검토하고, 생성 후 값 없이 Secret metadata와 애플리케이션 환경변수 이름만 확인한다.
4. 임시 파일·복호화 산출물을 즉시 폐기하고, 백업·로그·Git diff에 값이 없는지 확인한다.
5. 키 회전과 복구 테스트를 별도 승인하고, OAuth 토큰은 이 1차 범위에 넣지 않는다.

필수 키 목록은 기존 Compose 계약을 기준으로 별도 운영 문서에서 관리한다. 이 초안에는 키 이름 외의 값·토큰을 기록하지 않는다.

### Portal Secret shadow smoke

`infra/k8s/tools/portal-secret-shadow-smoke.sh`는 N100 일반 WSL 터미널에서 직접 실행하는 **isolated manual smoke** 절차다. 실행 전 K3s Secret 암호화가 Enabled인지 확인하고, 매 실행마다 고유 namespace·이미지·immutable Secret과 default-deny NetworkPolicy를 만든 뒤 네 개의 portal 핵심 키 주입과 Pod loopback `/health`만 검증한다. 실행 시작 시 출력되는 `portal_secret_shadow_run_id`는 비정상 중단 시 정리용 식별자다. 성공·실패 모두 임시 namespace와 정확한 이미지 참조를 삭제하고 잔존 여부를 확인한다.

비정상 중단으로 정리가 남았을 때만 다음을 실행한다. `RUN_ID`에는 시작 시 출력된 값만 넣는다.

`bash infra/k8s/tools/portal-secret-shadow-smoke.sh --cleanup RUN_ID`

```bash
bash infra/k8s/tools/portal-secret-shadow-smoke.sh
```

터미널이 중단되어 자동 정리가 완료되지 않은 경우에는 실행에 사용한 ID를 지정해 다음 복구 정리를 수행한다. 지정한 namespace, 정확한 containerd 이미지 참조와 일치하는 로컬 임시 Docker tag만 삭제하고 각각의 부재를 확인한다.

```bash
bash infra/k8s/tools/portal-secret-shadow-smoke.sh --cleanup RUN_ID
```

이 smoke는 선택적 HomeOps/portfolio configuration(optional HomeOps/portfolio), data copy, Caddy routing, actual cutover을 검증하지 않는다. Service, Ingress, NodePort, PV/PVC를 만들지 않으며, 기존 Compose·Caddy·스케줄러 운영을 변경하지 않는다. 실제 전환과 데이터 이동은 별도 승인과 검증이 필요한 작업이다.

### Portal operator-only cutover

`infra/k8s/tools/portal-cutover.sh`는 승인된 유지보수 창에서만 실행하는 **operator-only** 절차다. 기본 실행은 명시적 `--go`가 없으면 아무 리소스·컨테이너도 변경하지 않으며, K3s Secret 암호화 Enabled와 엄격한 암호화 백업·복원 증적을 먼저 확인한다. 증적은 기본 24시간(`PORTAL_BACKUP_MAX_AGE_SECONDS=86400`) 이내의 backup·별도 경로 restore 성공을 모두 증명해야 한다. 실행 전 Compose Portal만 writer인지 확인하고, 실행 중에는 해당 서비스만 중지한다.

데이터는 `data/files`에서 새 `local-path` 동적 PVC로 복사되고, 원본·PVC의 `sha256` 매니페스트가 일치해야 다음 단계로 진행한다. Secret은 기존 `.env`에서 네 개의 허용 키만 읽어 권한 `0600` 임시 파일로 전달하며, 값은 로그·Git에 기록하지 않는다. Deployment·NodePort Service·Pod `/health`·Caddy 컨테이너 경로를 검증하지만, 기본적으로 public Caddy route는 전환하지 않는다.

```bash
bash infra/k8s/tools/portal-cutover.sh --go
```

별도 최종 승인이 있는 경우에만 준비 단계가 성공한 뒤 `--switch-caddy`를 별도 호출해 `host.docker.internal:30080`으로 트래픽을 전환한다. `--go`와 `--switch-caddy`를 함께 전달하면 준비를 건너뛰는 위험을 막기 위해 즉시 실패한다. 이 단계는 준비된 K3s Portal과 중지된 Compose Portal을 재확인한 뒤 Caddy만 재생성하고 네 개 Portal 호스트를 확인한다. 실패하면 이전 upstream으로 복구하고 Compose writer를 다시 시작하며 K3s writer를 중지한다.

```bash
bash infra/k8s/tools/portal-cutover.sh --switch-caddy
```

롤백은 별도 명시 호출로만 수행한다. Caddy upstream을 `portal-web:8000`으로 복구하고 Caddy만 재생성한 뒤 Compose Portal을 시작하며, K3s Deployment는 중지 상태로 유지한다.

```bash
bash infra/k8s/tools/portal-cutover.sh --rollback-caddy
```

롤백이 완료되어 Compose Portal이 현재 writer로 확인되었지만 이전 K3s 시도의 Portal PVC가 남은 경우, 별도 정리 명령을 사용한다. 이 명령은 `portal-runtime.mode=compose`, Compose Portal의 running/healthy 상태, K3s Portal writer replica 0개와 실행 Pod 부재를 먼저 확인한다. 또한 이름이 고정된 Portal PVC 두 개만 대상으로 하며, PVC를 참조하는 Pod가 하나라도 남아 있으면 삭제하지 않고 실패한다. Deployment·Service·Secret·Compose bridge 리소스의 삭제 및 부재 검증이 끝난 뒤에만 PVC를 삭제한다. Compose 컨테이너와 로컬 `data/`는 이 명령으로 변경되지 않는다.

```bash
bash infra/k8s/tools/portal-cutover.sh --cleanup-rolledback
```

이 절차는 실제 N100에서 승인된 유지보수 창과 backup 복원 증적이 없으면 실행하지 않는다. `PORTAL_FILES_CAPACITY`, `PORTAL_IMAGE_REF`, `PORTAL_SOURCE_DIR`, `PORTAL_ENV_FILE`은 실행 전 실제 값 확인이 필요하다.

#### Portal backup evidence 계약

`PORTAL_BACKUP_EVIDENCE`는 backup 생성 도구가 성공한 뒤에만 권한 `0600`으로 원자적으로 기록하는 단일 `key=value` 파일이다. `validate-backup-evidence.py`는 빈 줄, 주석, 중복·알 수 없는 키와 값 누락을 모두 거부한다. 이 저장소는 암호화 backup artifact나 키를 생성하지 않으며, 증적만 검증한다.

`compose-local` evidence는 Compose writer를 멈춰 생성한 cutover 전용 증적이다. `k3s-pvc` evidence는 K3s PVC writer를 멈춰 생성한 운영 backup 증적이다. 두 evidence는 `source_digest`가 같아도 상호 재사용하지 않는다. Compose→K3s cutover의 `--go`는 `source_runtime=compose-local`인 evidence만 허용하며, K3s PVC backup evidence는 gate를 통과하지 못한다.

필수 키는 다음과 같다. 모든 시각은 UTC `Z` 형식이어야 하며 backup·restore 시각은 미래가 아니고 최대 연령 이내, `restore_verified_at`는 `backup_completed_at`보다 같거나 늦어야 하며, 증적 만료 시각은 현재보다 미래여야 한다.

```text
schema_version=1
scope=portal
backup_status=success
encrypted=true
backup_completed_at=2026-08-30T00:00:00Z
restore_status=success
restore_verified_at=2026-08-30T00:00:00Z
evidence_expires_at=2026-08-31T00:00:00Z
backup_id=opaque-safe-id
source_runtime=compose-local
```

선택 키는 암호문 artifact의 `artifact_digest=sha256:<64개의 소문자 hex>`, 두 Portal 데이터 트리의 SHA-256 manifest를 결합한 `source_digest=sha256:<64개의 소문자 hex>`, `restore_check=sqlite_quick_check`, `restore_path_check=success`뿐이다. 원본·복원 절대 경로, secret, token, 개인키는 증적에 기록하지 않는다. backup 또는 복원 절차가 실패하면 기존 증적을 갱신하지 않는다.

N100에서는 `infra/k8s/tools/portal-backup-verify.sh`만 증적을 생성한다. 이 도구는 `data/files`와 `data/portal-web-state`의 manifest 결합값이 현재 유효한 증적의 `source_digest`와 같으면 새 암호화 archive를 만들거나 업로드하지 않고 `backup_upload=SKIPPED_UNCHANGED`를 출력한다. 증적이 없거나 만료·무효이거나 데이터가 다르면 stage 후 age 공개 수신자 키로 암호화하고, `gdrive:PersonalServer-encrypted-backups`에 업로드한다. 이어서 Drive에서 별도 임시 경로로 다시 내려받아 복호화하며, 두 데이터 트리의 SHA-256 manifest와 복원된 HomeOps SQLite `quick_check`가 모두 통과할 때만 `.portal-backup-verified`를 권한 `0600`으로 원자 교체한다. 이 변경은 기존 원격 archive의 자동 삭제·보존기간 정리를 수행하지 않는다. age 개인키·rclone 설정·토큰은 명령 인수와 Git에 넣지 않는다.

```bash
bash infra/k8s/tools/portal-backup-verify.sh
```

검증 실행 중 어떤 단계에서든 실패하면 기존 증적을 무효화한다. 따라서 실패하면 `portal_backup_verify=FAIL`만 출력되고 cutover는 차단된다. 내용이 같고 기존 증적이 유효한 경우에만 그 검증 증적을 재사용한다. 성공한 뒤에만 `portal-cutover.sh --go`의 backup 게이트를 통과할 수 있다.

실행 예시는 `--check-nodeport-private`로 사전 확인한 뒤, 승인된 유지보수 창에서 `--go`를 한 번 실행하는 순서로 제시한다. 자동 실행·cron·GitHub Actions 배포 작업은 추가하지 않는다.

```bash
bash infra/k8s/tools/portal-cutover.sh --check-nodeport-private
bash infra/k8s/tools/portal-cutover.sh --go
```

### Portal state and Compose bridge prerequisites

Portal 상태는 `data/files`와 분리하여 `data/portal-web-state`에 유지한다. 이 경로에는 `homeops.sqlite3`, 보안 이벤트 로그, 로그인 rate-limit 상태를 포함하며 Compose와 K3s Portal 모두 `/var/lib/portal`로만 연결한다. 실제 cutover는 파일·상태 각각의 RWO `local-path` PVC에 복사한 뒤 SHA-256 manifest와 HomeOps SQLite `quick_check`가 모두 일치할 때만 계속한다. 기존 `data/logs`에서 전용 상태 경로로의 최초 분리·복원 검증은 별도 유지보수 창에서 완료해야 하며, 빈 상태 경로로 cutover를 시작하지 않는다.

K3s Portal은 Docker 서비스 DNS 이름이나 `hostNetwork`를 사용하지 않는다. 실행 전 `DOCKER_BRIDGE_GATEWAY`를 실제 Docker `bridge` gateway 주소로 설정하고, Compose는 기존 loopback 바인딩을 유지하면서 gateway 전용 포트만 추가로 열어야 한다. Cutover 도구는 이 값을 현재 bridge 설정과 대조한 후 selector 없는 K3s Service와 EndpointSlice를 생성한다. 대상은 `system-agent`(18010), `homeops-executor`(18011), crawler(18001), YouTube(18002), book(18003)이며, Portal은 `compose-*` Service DNS만 사용한다.

`data/portal-runtime.mode` marker는 `compose`, `cutover`, `k3s` 값만 허용한다. bootstrap과 자동 배포는 `cutover`에서 Portal과 scheduler scan을 모두 기동하지 않으며, `k3s`에서는 Compose Portal을 제외하고 Caddy를 `--no-deps`로만 재기동한다. `k3s`/`cutover`에서 Docker 의존 서비스가 재기동되면 검증된 Docker bridge gateway와 opt-in override를 다시 적용하고 HomeOps executor의 Portal 제어를 제외한다. marker가 없으면 기존 Compose 동작을 사용하고, 알 수 없는 값은 fail-closed로 거부한다.

## Flux bootstrap 순서

아래는 실행 순서 문서일 뿐 Flux 리소스를 포함하지 않는다.

1. Git 저장소 경계, 기본 브랜치 보호, 배포 권한과 Secret 관리 방식을 검토·승인한다.
2. K3s 접근과 Caddy 포트 경계, `local-path` 동적 PVC의 용량·복원·단일 writer 게이트를 사전 검증한다. 이 게이트가 닫힌 동안에는 앱 PVC를 적용하지 않는다.
3. Flux CLI와 컨트롤러 설치를 별도 변경으로 승인하고, 최소 권한 Git 인증을 준비한다.
4. `flux bootstrap`의 대상 저장소와 권한을 별도 승인한 뒤에만 연결한다. 이 초안을 자동 연결 대상으로 취급하지 않는다.
5. namespace → storage → Secret 주입 기반 → 단일 앱 순으로 Kustomization 의존성을 선언한다.
6. 각 단계마다 reconcile 결과·Pod 상태·PVC 바인딩·서비스 내부 접근을 검증하고, Caddy 전환은 마지막 별도 승인 단계로 둔다.

## 적용 전 검증 계획

| 단계 | 검증 | 통과 기준 |
| --- | --- | --- |
| 정적 초안 | `KUBECONFIG=/nonexistent kubectl kustomize infra/k8s` | 의도적으로 0줄을 출력해 활성 리소스가 없음을 확인 |
| 저장소 경계 | diff 및 금지 리소스 점검 | Secret, Ingress, LoadBalancer, Flux 리소스와 Compose/Caddy 변경이 없음 |
| 노드 저장소 | native ext4 경로 준비 및 `local-path` 동적 scratch PVC/Pod 통과: `/dev/sdd` ext4 `rw`, `root:root`·`0750`, 노드 `desktop-utu2qat` Ready, 기본 경로 일치, 파일 I/O와 SQLite `BEGIN IMMEDIATE` 잠금 통과, 임시 namespace와 PV 삭제 완료 | 앱별 데이터 복사·복원, UID/GID, 단일 writer, crawler 잠금, Caddy 전환과 Secret 주입이 확인될 때까지 앱 동적 PVC는 보류 |
| 단일 writer | Compose/K3s 동시 writer와 SQLite 프로세스 확인 | 대상 서비스마다 writer가 정확히 하나이고 `quick_check` 통과 |
| Secret seed | dry-run, metadata, 로그·Git diff 점검 | 값·토큰이 노출되지 않고 승인된 주입 경로가 재현됨 |
| 복구 | 임시 경로 복원, SQLite `quick_check`, 파일 존재 확인 | 데이터가 암호화 백업에서 독립 복원됨 |
| 네트워크 | Caddy→NodePort 경로 및 외부 노출 점검 | 80/443 소유권이 Caddy에 남고 의도하지 않은 공개가 없음 |
| cutover | 서비스별 기능·로그·롤백 점검 | 단일 writer, 정상 응답, 검증된 롤백 경로 |
