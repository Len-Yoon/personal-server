# K3s Portal·Crawler 런타임 업그레이드 설계

## 1. 목적

N100 K3s에서 운영 중인 `portal-web`, `crawler-worker`를 불변 OCI digest 이미지로 안전하게 교체하고, rollout 또는 health 검증 실패 시 직전 이미지로 1회 복구하는 표준 절차를 제공함.

## 2. 범위

| 구분 | 포함 범위 | 제외 범위 |
|---|---|---|
| 이미지 빌드 | macOS에서 Portal·Crawler AMD64 OCI archive 생성 | 레지스트리 push, mutable tag 사용 |
| 이미지 반입 | 기존 archive SHA-256·OCI 플랫폼·digest 검증 후 N100 K3s containerd 반입 | 이미지 삭제·정리 자동화 |
| 런타임 적용 | 실행 중 Deployment의 단일 컨테이너 image 필드 조건부 patch, rollout·health·1회 rollback | Deployment 전체 apply, replica·env·volume·Secret·PVC 변경 |
| 서비스 검증 | K3s Ready·Service/Endpoint·Pod health, Portal 외부 health 3회 | Caddy·Cloudflare Tunnel 재생성·변경 |

## 3. 운영 제약

- 대상 앱은 `portal-web`, `crawler-worker`만 허용함.
- 모든 대상 이미지는 `docker.io/library/personal-server-<app>@sha256:<digest>` 형식의 immutable digest 참조만 허용함.
- `latest`, tag-only 참조, 다른 앱 이미지, digest와 containerd 이미지가 불일치하는 경우 적용하지 않음.
- N100에서 `--check`는 읽기 전용이며, 이미지 반입·Deployment patch·rollout을 수행하지 않음.
- 실제 변경은 `--go`와 명시한 `--expected-current-image`가 모두 있을 때만 수행함. 이 값이 현재 Deployment image와 다르면 경쟁 변경으로 판단하여 중단함.
- 새 이미지 적용 실패 시 시작 시점에 읽은 직전 image로 단 한 번 rollback함. rollback 검증도 실패하면 추가 재시도를 금지하고 실패를 반환함.
- Portal·Crawler PVC, Secret, Caddyfile, Tunnel ingress, Compose writer, K3s Deployment의 image 이외 필드는 수정하지 않음.
- Portal 외부 health는 `https://len.pe.kr/health`를 10초 간격으로 3회 확인하고, 모두 HTTP 200일 때만 성공으로 판단함.
- Crawler는 Service ClusterIP를 변경하지 않고 Deployment rollout과 `/health` endpoint 확인만 수행함.

## 4. 구성 및 인터페이스

### 4.1 이미지 빌드·반입

기존 `infra/k8s/tools/k3s-app-image-build.sh`의 지원 앱 목록에 `portal-web`을 추가함. 빌드 입력은 `--app`, immutable `--tag`, `--output`이며, 기존 방식대로 `linux/amd64` OCI archive와 SHA-256을 생성함.

기존 `infra/k8s/tools/k3s-app-image-import.sh`는 변경하지 않음. N100에서 archive를 반입한 뒤 출력하는 canonical digest image 참조를 업그레이드 도구의 `--image` 입력으로 사용함.

### 4.2 K3s 업그레이드 도구

새 `infra/k8s/tools/k3s-app-upgrade.sh`는 다음 인터페이스를 제공함.

```text
k3s-app-upgrade.sh --check --app <portal-web|crawler-worker> --image <canonical-digest-ref>
k3s-app-upgrade.sh --go --app <portal-web|crawler-worker> --image <canonical-digest-ref> --expected-current-image <current-ref>
```

공통 사전검증은 namespace `personal-server`, 대상 Deployment 존재·replica 1·Ready 1, 정확한 단일 컨테이너 이름, immutable target image의 containerd 존재·AMD64 플랫폼, 대상 Service·Endpoint Ready 상태임. Portal은 실행 중 Pod의 loopback health 및 외부 health 3회를, Crawler는 Service endpoint의 `/health`를 확인함.

`--go`는 현재 image가 `--expected-current-image`와 정확히 같은지 다시 확인한 뒤 JSON Patch의 test+replace 연산으로 `.spec.template.spec.containers/<target>/image`만 변경함. 변경 뒤 `rollout status`와 서비스별 health를 확인함. 실패 시 사전에 읽은 현재 image로 동일 image 필드만 patch하여 1회 rollback하고, rollout·health까지 확인함.

## 5. 오류 처리 및 결과

| 상태 | 처리 | 결과 |
|---|---|---|
| 사전검증 실패 | Deployment를 변경하지 않음 | nonzero, `k3s_app_upgrade=FAIL` |
| image 경쟁 변경 | Deployment를 변경하지 않음 | nonzero, expected/actual을 비밀값 없이 표시 |
| 새 이미지 rollout·health 실패 | 직전 image로 1회 rollback 후 검증 | rollback 성공도 nonzero, `rollback=PASS` 표시 |
| rollback 실패 | 추가 patch·재시도 금지 | nonzero, `rollback=FAIL` 표시 |
| 전체 성공 | target image·rollout·health 모두 확인 | zero, `k3s_app_upgrade=PASS` |

## 6. 검증

- Shell 계약 테스트는 앱 allowlist, mutable/foreign image 거부, `--check` 무변경, expected image 불일치 중단, 단일 image patch, rollout 성공, Portal 3회 외부 health, Crawler 내부 health, rollback 1회·재시도 금지를 포함함.
- 기존 `k3s-app-image-build` 테스트는 Portal 빌드 허용과 다른 앱 거부를 확인함.
- K3s contract 묶음과 maintenance 정적 검사를 실행함.
- 실제 N100 적용은 코드·테스트·문서 병합 뒤 별도 사용자 운영 승인에서만 수행함.

## 7. 성공 기준

1. 새 이미지는 immutable digest와 AMD64 검증을 통과해야 함.
2. 적용 도구는 image 이외 Deployment spec과 PVC·Secret·Caddy·Tunnel·Compose writer를 변경하지 않아야 함.
3. 실패 시 직전 image로 최대 한 번만 rollback하고, rollback 결과를 검증해야 함.
4. Portal은 외부 health 3회 HTTP 200, Crawler는 K3s rollout·Service health 성공을 확인해야 함.
5. `--check`만으로는 N100 런타임 상태가 변경되지 않아야 함.
