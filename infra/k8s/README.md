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

## SRE Pod 자동복구 실습 (운영자 전용)

`infra/k8s/tools/sre-pod-recovery-lab.sh`는 K3s에서 Pod 자동복구 동작을 확인하는 일회성 운영자 실습 도구다. GitOps resource가 아니며 production deploy가 아니다. N100에서 실행할 때는 운영자 승인과 클러스터 접근 권한을 확인한다.

실습 시작:

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --run
```

정상 완료 시 `PASS` 출력과 함께 다음 증거를 확인한다.

실습 리소스는 sre-recovery-lab-<run-id> namespace에만 생성된다.

- 실행별 `sre-recovery-lab-<run-id>` namespace가 생성되어 다른 namespace와 격리됨
- liveness sentinel로 비정상 상태를 유도한 뒤 같은 Deployment의 Pod가 재시작됨 (`restartCount` 증가)
- `restartCount`가 증가한 선택 Pod가 `Ready` 조건으로 복구됨
- 실행 종료 시 해당 실습 namespace만 정리됨

비정상 중단으로 정리되지 않은 실행만 run ID를 지정해 정리한다.

```bash
bash infra/k8s/tools/sre-pod-recovery-lab.sh --cleanup <run-id>
```

이 실습은 Portal, Compose, Caddy, scheduler를 변경하지 않는다. production Deployment·Service·Secret·PVC와 GitOps 리소스를 변경하지 않으며, 실습 namespace 밖의 리소스도 변경하지 않는다. 적용 전환이나 자동 배포를 수행하지 않으므로 Portal·Compose·Caddy·scheduler 운영에는 효과가 없다.

이 실습은 다른 namespace, Portal, Compose, Caddy, scheduler를 변경하거나 재시작하지 않는다.

## Monitoring 운영 도구

N100에서 Monitoring을 설치·검증·삭제할 때는 다음 순서를 따른다.

1. `monitoring-preflight.sh`를 실행한다.
2. `monitoring-install.sh --render`로 Helm template만 확인한다.
3. 운영자 승인 후 `monitoring-install.sh --apply`를 실행한다.
4. `monitoring-verify.sh`로 PVC, Pod, Grafana Service를 검증한다.
5. 필요할 때만 `monitoring-verify.sh --port-forward-check`로 localhost 접속을 확인한다.

Grafana는 ClusterIP Service로만 제공한다. 운영자 접속은 다음 일회성 port-forward만 사용한다.

```bash
sudo k3s kubectl -n monitoring port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80
```

설치 도구는 `--apply`가 명시된 경우에만 Helm release를 생성하며, `--render`는 Helm template만 수행한다. 삭제는 `monitoring-uninstall.sh --uninstall`로 수행하고 PVC와 namespace는 기본적으로 보존한다. 데이터까지 삭제할 때만 `--delete-data`를 추가한다.

Grafana 및 Prometheus PVC에는 Helm resource keep 정책을 적용하여 기본 uninstall에서 데이터를 보존한다. `--delete-data`를 지정한 경우에만 두 PVC와 namespace를 삭제한다.

Grafana 관리자 비밀번호는 Kubernetes Secret에서 운영자가 직접 확인한다. 비밀번호와 Secret 데이터는 채팅, Git, 문서, 명령 로그에 기록하거나 출력하지 않는다. Caddy, Compose, Portal, 서버 기동, Windows bootstrap, scheduler 및 외부 공개 Ingress/NodePort/LoadBalancer는 이 도구로 변경하지 않는다.

## N100 SRE 상태 점검 (읽기 전용)

`infra/k8s/tools/sre-health-audit.sh`는 N100에서 수동 실행하는 읽기 전용 상태 점검 도구다. K3s 노드 중 하나 이상이 `Ready`인지 확인하고, 현재 Compose 설정에서 산출한 모든 서비스 컨테이너가 실행 중인지와 설정된 Docker health check가 `healthy`인지 확인한다. health check가 없는 실행 중 컨테이너는 정상으로 처리한다.

```bash
bash infra/k8s/tools/sre-health-audit.sh
bash infra/k8s/tools/sre-health-audit.sh --help
```

최종 줄은 기계 판독용 `sre_health=PASS` 또는 `sre_health=FAIL`이며, PASS일 때만 종료 코드 0을 반환한다. 점검은 `k3s kubectl get nodes`, `docker compose config --services`, `docker compose ps --all` 조회만 수행하며 Compose 기동·중지·재시작, Kubernetes 리소스 변경, Secret 접근, 외부 네트워크 호출을 수행하지 않는다. N100 운영 환경에서 실행하기 전 대상 호스트와 읽기 권한을 확인한다.
