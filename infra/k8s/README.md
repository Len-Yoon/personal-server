# K3s GitOps 검토 초안

> **NOT FOR APPLY:** 이 디렉터리는 K3s·Flux 전환 설계 검토용이다. 현재 클러스터, Flux, Caddy, Compose에 적용하거나 연결하지 않는다.

이 구조는 현재 저장소에서 검토하는 `clusters/n100` 골격이다. 별도 GitOps 저장소는 만들지 않았으며, 이 저장소의 자동 배포 흐름은 Compose 전용으로 유지한다.

## 안전 경계

- 모든 Kubernetes 리소스 예시는 `.yaml.tmpl` 파일에만 있으며 `draft.personal-server.io/not-for-apply: "true"` annotation을 포함한다.
- 루트 `kustomization.yaml`은 의도적으로 비어 있으며 Flux `Kustomization` 리소스가 아니다. `kubectl apply -k infra/k8s`를 실행해도 리소스를 렌더링하지 않는다.
- `.yaml.tmpl` 파일은 Kustomize `resources`에 절대 포함하지 않는다. 별도 승인, 실제 값 확인, 독립 검토가 완료된 뒤에만 새 작업 경로로 복사해 검토한다.
- Secret 리소스와 비밀값은 포함하지 않는다. 참조 이름만 사용한다.
- K3s 기본 `local-path` 동적 provisioner가 native ext4 경로에 할당하는 방식을 사용한다. scratch PVC·Pod와 파일 I/O·SQLite 잠금은 통과했지만, 실제 앱 PVC 템플릿은 비활성 상태이며 용량·데이터 복사·복원·UID/GID·단일 writer cutover는 별도 승인 게이트로 남긴다.
- 1차 전환 후보는 `portal-web`, `crawler-worker`, `youtube-memo`, `book-memo`뿐이다. `system-agent`, `homeops-executor`, `car-care-worker`, Caddy는 제외한다.
- SQLite와 파일 상태는 서비스별 단일 writer를 보장하기 전에는 PVC 후보로 사용하지 않는다.
- Secret은 키 이름과 참조 계약만 두며 값은 만들거나 기록하지 않는다. seed는 승인된 SOPS/age 또는 Secret Manager 절차를 사용한다.
- Ingress와 `LoadBalancer` Service는 포함하지 않는다. `portal-web`의 NodePort는 Docker Caddy를 바꾸지 않는 미래 백엔드 계약일 뿐이다.

## 정적 검토

클러스터 연결이나 적용 없이 다음 명령만 사용할 수 있다.

```bash
KUBECONFIG=/nonexistent kubectl kustomize infra/k8s
git diff --check
```

`kubectl apply`, `flux bootstrap`, `flux reconcile`, Compose 제어 명령은 이 초안의 범위 밖이다.
