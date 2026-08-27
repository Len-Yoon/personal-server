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

## 영속 데이터와 Local PV 계약

Compose 데이터는 cutover 전까지 원본 위치와 Docker named volume을 계속 사용한다. Kubernetes가 원본을 동시에 쓰도록 연결하지 않는다. 특히 SQLite는 Compose와 Kubernetes가 동시에 접근하면 안 된다.

| 데이터 범주 | 전환 원칙 | Kubernetes 계약 | 적용 전 필수 확인 |
| --- | --- | --- | --- |
| 서비스별 `data/` 하위 데이터 | 백업 검증 후 서비스별로 정지·복사·검증 | `local` PV + 명시적 PVC 바인딩 | 실제 경로, UID/GID, 읽기·쓰기 권한, 여유 공간 |
| Caddy named volume | Docker Caddy가 유지하는 동안 Kubernetes에 마운트하지 않음 | 별도 단계에서만 검토 | Docker volume 위치와 복구 절차 |
| car-care OAuth named volume | Compose와 동시 마운트 금지 | 전용 PV/PVC 및 Secret 계약 분리 | 토큰 파일 권한, 재인증 절차, 복구 검증 |
| SQLite 파일 | 서비스 중지 후 단일 writer 상태에서 복사 | PVC 하나를 하나의 서비스에만 연결 | `quick_check`, 파일 소유권, 롤백 가능성 |

`infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`의 경로와 노드 이름은 의도적으로 확인용 placeholder다. 이 template은 활성 Kustomize 트리에 포함되지 않는다. 실제 경로, 권한, WSL 마운트 적합성(안정적인 Linux 파일시스템 여부와 Kubernetes 런타임의 접근 가능 여부)을 확인·기록하기 전에는 별도 작업 경로로 복사하거나 값을 대체하면 안 된다. `/mnt/c` 같은 Windows 공유 마운트를 Local PV로 채택할지 역시 성능·권한·파일 잠금 검증이 끝난 뒤 결정한다.

## Secret 계약

Secret 값과 실제 Secret 리소스는 이 저장소에 작성하지 않는다. 배포 시점에는 승인된 외부 주입 방식(예: SOPS/age, External Secrets 또는 수동 `kubectl` 생성 중 선택)을 별도로 결정한다.

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

## Flux bootstrap 순서

아래는 실행 순서 문서일 뿐 Flux 리소스를 포함하지 않는다.

1. Git 저장소 경계, 기본 브랜치 보호, 배포 권한과 Secret 관리 방식을 검토·승인한다.
2. K3s 접근과 Caddy 포트 경계, Local PV 경로·권한·WSL 적합성, 복원 절차를 사전 검증한다.
3. Flux CLI와 컨트롤러 설치를 별도 변경으로 승인하고, 최소 권한 Git 인증을 준비한다.
4. `flux bootstrap`의 대상 저장소와 권한을 별도 승인한 뒤에만 연결한다. 이 초안을 자동 연결 대상으로 취급하지 않는다.
5. namespace → storage → Secret 주입 기반 → 단일 앱 순으로 Kustomization 의존성을 선언한다.
6. 각 단계마다 reconcile 결과·Pod 상태·PVC 바인딩·서비스 내부 접근을 검증하고, Caddy 전환은 마지막 별도 승인 단계로 둔다.

## 적용 전 검증 계획

| 단계 | 검증 | 통과 기준 |
| --- | --- | --- |
| 정적 초안 | `KUBECONFIG=/nonexistent kubectl kustomize infra/k8s` | 의도적으로 0줄을 출력해 활성 리소스가 없음을 확인 |
| 저장소 경계 | diff 및 금지 리소스 점검 | Secret, Ingress, LoadBalancer, Flux 리소스와 Compose/Caddy 변경이 없음 |
| 노드 저장소 | 실제 경로·소유권·WSL 접근성 점검 | 서비스 계정이 읽기·쓰기 가능하고 단일 노드 고정이 확인됨 |
| 복구 | 임시 경로 복원, SQLite `quick_check`, 파일 존재 확인 | 데이터가 암호화 백업에서 독립 복원됨 |
| 네트워크 | Caddy→NodePort 경로 및 외부 노출 점검 | 80/443 소유권이 Caddy에 남고 의도하지 않은 공개가 없음 |
| cutover | 서비스별 기능·로그·롤백 점검 | 단일 writer, 정상 응답, 검증된 롤백 경로 |
