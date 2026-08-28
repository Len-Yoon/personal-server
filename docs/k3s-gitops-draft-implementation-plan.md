# K3s GitOps 전환 초안 구현 계획

> **For Codex:** 이 계획은 검토 전용 초안 작성 범위를 기록한다. 실제 클러스터·Flux·Compose·Caddy에 적용하지 않는다.

**목표:** 현재 Compose 운영을 변경하지 않고, 향후 K3s·Flux 전환을 검토할 문서와 정적 매니페스트 계약을 `docs/` 및 `infra/k8s/`에 남긴다.

**범위 제외:** GitHub push·PR, 별도 GitOps 저장소 생성, Flux bootstrap·연결, Kubernetes 적용, Secret 생성, PVC 생성, Caddy 및 Windows bootstrap 변경, Compose 중지·배포 변경.

**성공 기준:** 모든 리소스 예시는 `.yaml.tmpl`에만 있고 검토 전용/적용 금지 표기를 포함한다. K3s 기본 `local-path` 동적 PVC 계약만 기록하며, 실제 앱 데이터 복사·복원·cutover는 보류한다. 1차 후보는 `portal-web`, `crawler-worker`, `youtube-memo`, `book-memo`로 제한하고, `system-agent`, `homeops-executor`, `car-care-worker`, Caddy는 제외한다. 루트 `kustomization.yaml`은 의도적으로 비어 `KUBECONFIG=/nonexistent kubectl kustomize infra/k8s`가 0줄을 출력한다. Secret 값·리소스·Ingress·LoadBalancer·Flux 리소스는 없다.

## 작성 단계

### 1. 전환 설계와 경계를 문서화

**파일:**

- 생성: `docs/k3s-flux-transition-draft.md`

**내용:** Compose 및 Docker Caddy의 현행 소유권, Local PV 확인 항목, Secret 비밀값 비저장 원칙, 서비스별 cutover·롤백, Flux bootstrap 순서와 검증 계획을 기록한다.

### 2. 검토 전용 Kustomize 골격 작성

**파일:**

- 생성: `infra/k8s/README.md`
- 생성: `infra/k8s/kustomization.yaml`
- 생성: `infra/k8s/clusters/n100/infra/namespaces/namespace.yaml.tmpl`
- 생성: `infra/k8s/clusters/n100/infra/namespaces/kustomization.yaml.tmpl`

**내용:** `clusters/n100` 구조를 표시하되, 현재 저장소와 Flux를 연결하지 않는다. 루트 Kustomize는 비워 두며 리소스 예시는 `.yaml.tmpl`로만 보관한다.

### 3. 저장소 계약을 placeholder로 작성

**파일:**

- 생성: `infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl`
- 생성: `infra/k8s/clusters/n100/infra/storage/kustomization.yaml.tmpl`

**내용:** K3s 기본 `local-path` StorageClass를 참조하는 네 서비스의 PVC 후보만 예시로 둔다. custom StorageClass, PV, host path, node affinity를 만들지 않으며, 용량·데이터 복사·복원·단일 writer는 확인 게이트로 둔다. Secret과 실제 데이터를 넣지 않는다.

### 4. 1차 전환 범위와 portal-web의 비배포 예시 작성

**파일:**

- 생성: `infra/k8s/clusters/n100/apps/portal-web/deployment.yaml.tmpl`
- 생성: `infra/k8s/clusters/n100/apps/portal-web/service.yaml.tmpl`
- 생성: `infra/k8s/clusters/n100/apps/portal-web/kustomization.yaml.tmpl`
- 생성: `infra/k8s/clusters/n100/apps/transition-scope.yaml.tmpl`

**내용:** 범위 계약에 1차 네 서비스와 제외 대상을 명시한다. `replicas: 0`과 placeholder 이미지를 사용해 실행 가능한 배포 정의가 아닌 계약임을 나타낸다. 예시는 활성 Kustomize 트리에 포함하지 않는다. NodePort는 미래 Caddy 백엔드 후보로만 기록하고 Ingress·LoadBalancer를 만들지 않는다.

### 5. 정적 검증과 독립 검토

**검증 명령:**

```bash
KUBECONFIG=/nonexistent kubectl kustomize infra/k8s
git diff --check
rg -n '^(kind: (Secret|Ingress)|  type: LoadBalancer|.*fluxcd)' infra/k8s docs/k3s-*.md
git diff --name-only
```

**기대 결과:** Kustomize 명령이 0줄을 출력하고 공백 검사가 성공하며, 의도하지 않은 금지 리소스가 없다. 독립 검토자는 범위 외 파일, template의 활성 트리 참조 여부, placeholder의 실제화 여부, Secret·네트워크 경계와 정적 검사 결과를 확인한다.

추가로 템플릿에는 실제 Secret 값·PV 경로가 없고, 단일 writer·Secret seed·향후 PV 소유권/권한/WSL 검증 게이트가 명시되어야 한다.

## 실행 이후 보류 항목

- 실제 Local PV 경로·UID/GID·WSL 마운트 적합성 확인
- Caddy와 NodePort의 연결·방화벽·외부 노출 검증
- 암호화 백업의 최신 복원 검증
- Git 저장소 경계와 Flux bootstrap의 별도 승인
- 서비스별 데이터 cutover 및 롤백 승인
