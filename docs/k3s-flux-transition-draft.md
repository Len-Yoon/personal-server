# K3s·Flux 전환 초안

> 상태: 검토 전용 초안. 이 문서와 `infra/k8s/`의 파일은 클러스터 적용 대상이 아니다.

## 목적과 경계

현재 `personal-server`의 Compose 기반 운영을 유지한 채, 추후 별도 승인으로 수행할 K3s·Flux 전환의 계약을 정의한다. 현재 저장소의 `main` 배포 흐름은 Compose 전용으로 유지한다.

이 초안에서 하지 않는 일은 다음과 같다.

- Compose 중지, 재기동 또는 배포 흐름 변경
- Docker Caddy 설정·포트·라우트 변경
- Windows bootstrap 또는 WSL 마운트 변경
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

이는 K3s local-path provisioner의 기본 경로 준비와 노드 접근성만 확인한 결과이며, 실제 앱 데이터를 위한 Static Local PV 승인은 아니다. 앱별 UID/GID 읽기·쓰기, SQLite 잠금, WSL 런타임 접근성, 단일 writer와 복원 검증은 아직 미통과다. 따라서 Static Local PV 게이트는 계속 차단 상태로 유지하며, 이 조건을 모두 확인하기 전에는 PV/PVC를 만들거나 바인딩하지 않는다.

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
| `portal-web` | `data/files` | Static Local PV/PVC 후보(보류) | 실제 경로, UID/GID, 읽기·쓰기 권한, 여유 공간, WSL 적합성 |
| `crawler-worker` | `data/crawler-worker` | Static Local PV/PVC 후보(보류) | 위 항목 + Playwright 프로필 잠금 |
| `youtube-memo` | `data/youtube-memo` | Static Local PV/PVC 후보(보류) | 위 항목 + SQLite 무결성 |
| `book-memo` | `data/book-memo` | Static Local PV/PVC 후보(보류) | 위 항목 + SQLite 무결성 |
| Caddy named volume | Docker Caddy가 유지하는 동안 Kubernetes에 마운트하지 않음 | 별도 단계에서만 검토 | Docker volume 위치와 복구 절차 |
| car-care OAuth named volume | Compose와 동시 마운트 금지 | 전용 PV/PVC 및 Secret 계약 분리 | 토큰 파일 권한, 재인증 절차, 복구 검증 |
| SQLite 파일 | 서비스 중지 후 단일 writer 상태에서 복사 | PVC 하나를 하나의 서비스에만 연결 | `quick_check`, 파일 소유권, 롤백 가능성 |

`infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`의 경로와 노드 이름은 의도적으로 확인용 placeholder다. 이 template은 활성 Kustomize 트리에 포함되지 않는다. `/mnt/c` 경로의 `0777` 관찰 결과만으로 권한을 승인하지 않으며, 실제 Linux 파일시스템 경로로 이전할지 또는 WSL 공유 마운트를 사용할지 검증 후 결정한다.

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
4. 확인된 경로와 권한으로 PV/PVC를 만들고, Secret을 승인된 주입 경로로 제공한다.
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

## Flux bootstrap 순서

아래는 실행 순서 문서일 뿐 Flux 리소스를 포함하지 않는다.

1. Git 저장소 경계, 기본 브랜치 보호, 배포 권한과 Secret 관리 방식을 검토·승인한다.
2. K3s 접근과 Caddy 포트 경계, Static Local PV 경로·소유권·권한·WSL 적합성 게이트, 복원 절차를 사전 검증한다. 이 게이트가 닫힌 동안에는 PV/PVC를 적용하지 않는다.
3. Flux CLI와 컨트롤러 설치를 별도 변경으로 승인하고, 최소 권한 Git 인증을 준비한다.
4. `flux bootstrap`의 대상 저장소와 권한을 별도 승인한 뒤에만 연결한다. 이 초안을 자동 연결 대상으로 취급하지 않는다.
5. namespace → storage → Secret 주입 기반 → 단일 앱 순으로 Kustomization 의존성을 선언한다.
6. 각 단계마다 reconcile 결과·Pod 상태·PVC 바인딩·서비스 내부 접근을 검증하고, Caddy 전환은 마지막 별도 승인 단계로 둔다.

## 적용 전 검증 계획

| 단계 | 검증 | 통과 기준 |
| --- | --- | --- |
| 정적 초안 | `KUBECONFIG=/nonexistent kubectl kustomize infra/k8s` | 의도적으로 0줄을 출력해 활성 리소스가 없음을 확인 |
| 저장소 경계 | diff 및 금지 리소스 점검 | Secret, Ingress, LoadBalancer, Flux 리소스와 Compose/Caddy 변경이 없음 |
| 노드 저장소 | `/mnt/c` 0777 보류 상태와 native ext4 후보 `/var/lib/rancher/k3s/storage`의 통과(경로 준비 완료) 확인: `/dev/sdd` ext4 `rw`, `root:root`·`0750`, 노드 `desktop-utu2qat` Ready, local-path 기본 경로 일치 | 앱별 UID/GID 읽기·쓰기, 단일 노드 고정, SQLite·프로필 파일 잠금, WSL 접근성·복원 검증이 모두 확인될 때까지 Static Local PV 게이트는 계속 차단 상태 |
| 단일 writer | Compose/K3s 동시 writer와 SQLite 프로세스 확인 | 대상 서비스마다 writer가 정확히 하나이고 `quick_check` 통과 |
| Secret seed | dry-run, metadata, 로그·Git diff 점검 | 값·토큰이 노출되지 않고 승인된 주입 경로가 재현됨 |
| 복구 | 임시 경로 복원, SQLite `quick_check`, 파일 존재 확인 | 데이터가 암호화 백업에서 독립 복원됨 |
| 네트워크 | Caddy→NodePort 경로 및 외부 노출 점검 | 80/443 소유권이 Caddy에 남고 의도하지 않은 공개가 없음 |
| cutover | 서비스별 기능·로그·롤백 점검 | 단일 writer, 정상 응답, 검증된 롤백 경로 |
