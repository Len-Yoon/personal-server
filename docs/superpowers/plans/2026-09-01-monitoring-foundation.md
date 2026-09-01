# K3s Monitoring 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100 K3s에 내부 전용 Prometheus·Grafana 기반 K3s Monitoring을 안전하게 설치·검증·삭제할 수 있는 운영 도구와 설정을 제공함.

**Architecture:** `monitoring` namespace의 `kube-prometheus-stack` Helm release를 version `88.6.1`로 고정함. values 파일은 ClusterIP Grafana, local-path PVC, 7일 Prometheus retention, Alertmanager 비활성화를 선언함. 사전점검·설치·검증·삭제 도구는 기본적으로 읽기 전용 또는 dry-run이며, 명시적인 인자로만 클러스터 상태를 변경함.

**Tech Stack:** K3s v1.36+, Helm 3, kube-prometheus-stack 88.6.1, Prometheus, Grafana, Bash, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-01-monitoring-foundation-design.md`

## Global Constraints

- `monitoring` namespace와 `personal-server-monitoring` Helm release만 생성·수정·삭제함.
- Grafana는 `ClusterIP`만 사용하며 Ingress, NodePort, LoadBalancer, Caddy, 80/443을 사용하지 않음.
- Prometheus PVC는 5Gi/7일 retention, Grafana PVC는 1Gi이며 K3s `local-path` StorageClass를 사용함.
- Secret 값·Grafana 관리자 비밀번호·토큰을 Git·`.env`·명령 출력에 기록하지 않음.
- `--apply`와 `--uninstall` 이외의 명령은 K3s·Helm 상태를 변경하지 않음.
- Portal·Compose·Caddy·서버 기동·Windows bootstrap·스케줄러·Flux를 변경하지 않음.
- 외부 공개는 하지 않으며 Grafana 접속은 `kubectl port-forward --address 127.0.0.1`만 사용함.

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `infra/k8s/monitoring/values.n100.yaml` | 고정 chart version에 전달할 N100 관측 설정 계약 |
| `infra/k8s/tools/monitoring-preflight.sh` | 설치 전 읽기 전용 K3s·StorageClass·Helm·용량·chart 접근 점검 |
| `infra/k8s/tools/monitoring-install.sh` | render, 명시적 apply, 설치 후 기본 안전 검증 |
| `infra/k8s/tools/monitoring-verify.sh` | PVC, 핵심 workload, Service 유형, Prometheus target, localhost Grafana 연결 점검 |
| `infra/k8s/tools/monitoring-uninstall.sh` | 명시적 release/namespace 삭제 및 PVC 보존 여부 확인 |
| `tests/test_k8s_monitoring_values.py` | Helm values 계약과 안전 경계 검증 |
| `tests/test_k8s_monitoring_tools.py` | 실행 가능한 command stub으로 도구별 동작·금지 동작 검증 |
| `infra/k8s/README.md` | 운영자 실행 순서·내부 접속·Secret 취급 문서화 |

### Task 1: 고정 관측 설정 계약

**Files:**
- Create: `infra/k8s/monitoring/values.n100.yaml`
- Create: `tests/test_k8s_monitoring_values.py`

**Interfaces:**
- Produces: Helm 입력 파일 `values.n100.yaml`; 이후 도구가 `CHART_VERSION=88.6.1`과 함께 참조함.
- Consumes: K3s `local-path` StorageClass와 chart `prometheus-community/kube-prometheus-stack`.

- [ ] **Step 1: 실패하는 values 계약 테스트 작성**

`tests/test_k8s_monitoring_values.py`에서 YAML을 읽어 아래 운영 계약을 검증함.

```python
def test_values_keep_grafana_internal_and_persistent():
    values = load_values()
    assert values["grafana"]["service"]["type"] == "ClusterIP"
    assert values["grafana"]["ingress"]["enabled"] is False
    assert values["grafana"]["persistence"]["enabled"] is True
    assert values["grafana"]["persistence"]["storageClassName"] == "local-path"
    assert values["grafana"]["persistence"]["size"] == "1Gi"

def test_values_keep_prometheus_retention_and_local_storage():
    spec = load_values()["prometheus"]["prometheusSpec"]
    assert spec["retention"] == "7d"
    assert spec["storageSpec"]["volumeClaimTemplate"]["spec"]["storageClassName"] == "local-path"
    assert spec["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"] == "5Gi"
```

- [ ] **Step 2: 테스트가 파일 부재로 실패하는지 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_values -v`

Expected: `FAIL` 또는 `ERROR`이며 `values.n100.yaml` 부재가 원인임.

- [ ] **Step 3: 최소 values 구현**

`infra/k8s/monitoring/values.n100.yaml`에 다음 계약을 선언함.

```yaml
alertmanager:
  enabled: false
grafana:
  service:
    type: ClusterIP
  ingress:
    enabled: false
  persistence:
    enabled: true
    storageClassName: local-path
    size: 1Gi
prometheus:
  prometheusSpec:
    retention: 7d
    storageSpec:
      volumeClaimTemplate:
        spec:
          storageClassName: local-path
          accessModes: [ReadWriteOnce]
          resources:
            requests:
              storage: 5Gi
```

명시적 request/limit을 Prometheus, Grafana, kube-state-metrics에 선언하고 Alertmanager는 비활성화함.

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_values -v`

Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add infra/k8s/monitoring/values.n100.yaml tests/test_k8s_monitoring_values.py
git commit -m "feat: 내부 관측 Helm 설정 추가"
```

### Task 2: 설치 전 읽기 전용 사전점검

**Files:**
- Create: `infra/k8s/tools/monitoring-preflight.sh`
- Modify: `tests/test_k8s_monitoring_tools.py`

**Interfaces:**
- Consumes: `values.n100.yaml`, `sudo k3s kubectl`, `helm`, `df`.
- Produces: `monitoring_preflight=PASS|FAIL`, `chart_version=88.6.1`; PASS만 exit 0.

- [ ] **Step 1: 실패하는 사전점검 실행 테스트 작성**

명령 stubs에서 K3s node가 NotReady일 때 아래 동작을 검증함.

```python
result = run_tool("monitoring-preflight.sh", kubectl_nodes="n100 NotReady")
assert result.returncode == 1
assert result.stdout.rstrip().endswith("monitoring_preflight=FAIL")
```

StorageClass가 `local-path`이 아니거나 Helm 3이 없을 때도 같은 fail-closed 결과를 검증함.

- [ ] **Step 2: 테스트가 스크립트 부재로 실패하는지 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools -v`

Expected: FAIL 또는 ERROR이며 `monitoring-preflight.sh` 부재가 원인임.

- [ ] **Step 3: 최소 사전점검 구현**

`monitoring-preflight.sh`는 인자 없이 다음 읽기 전용 조건을 검사함.

```bash
sudo k3s kubectl get nodes --no-headers
sudo k3s kubectl get storageclass local-path
helm version --short
helm show chart prometheus-community/kube-prometheus-stack --version 88.6.1
df -Pk /var/lib/rancher/k3s/storage
```

PASS 시 `monitoring_preflight=PASS`와 `chart_version=88.6.1`만 출력함. Helm repository 추가·업데이트, namespace 생성, release 설치는 실행하지 않음.

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools -v`

Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add infra/k8s/tools/monitoring-preflight.sh tests/test_k8s_monitoring_tools.py
git commit -m "feat: K3s Monitoring 사전점검 추가"
```

### Task 3: 명시적 설치·검증·삭제 도구

**Files:**
- Create: `infra/k8s/tools/monitoring-install.sh`
- Create: `infra/k8s/tools/monitoring-verify.sh`
- Create: `infra/k8s/tools/monitoring-uninstall.sh`
- Modify: `tests/test_k8s_monitoring_tools.py`
- Modify: `infra/k8s/README.md`

**Interfaces:**
- Consumes: preflight PASS, values 파일, Helm 3, K3s `local-path`.
- Produces: `monitoring_install=PASS|FAIL`, `monitoring_verify=PASS|FAIL`, `monitoring_uninstall=PASS|FAIL` final lines.

- [ ] **Step 1: 실패하는 설치 안전성 테스트 작성**

실행 stub으로 다음 계약을 먼저 검증함.

```python
assert run_tool("monitoring-install.sh").returncode == 2
assert "--apply" in run_tool("monitoring-install.sh").stderr
assert run_tool("monitoring-uninstall.sh").returncode == 2
assert "--uninstall" in run_tool("monitoring-uninstall.sh").stderr
```

`--apply`일 때 Helm 호출이 정확히 `upgrade --install personal-server-monitoring prometheus-community/kube-prometheus-stack --namespace monitoring --create-namespace --version 88.6.1 --values infra/k8s/monitoring/values.n100.yaml --wait --timeout 10m`인지 검증함. verify는 Grafana Service type이 ClusterIP가 아니면 실패해야 함.

- [ ] **Step 2: 테스트가 스크립트 부재로 실패하는지 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_tools -v`

Expected: FAIL 또는 ERROR이며 설치·검증·삭제 스크립트 부재가 원인임.

- [ ] **Step 3: 최소 운영 도구 구현**

각 도구는 `set -Eeuo pipefail`을 사용하고 final line을 한 번만 출력함.

```bash
# install: --render는 helm template만, --apply만 실제 release를 생성함
helm upgrade --install personal-server-monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace --version 88.6.1 \
  --values infra/k8s/monitoring/values.n100.yaml --wait --timeout 10m

# verify: Secret data를 출력하지 않음
sudo k3s kubectl -n monitoring get pvc
sudo k3s kubectl -n monitoring get pods
sudo k3s kubectl -n monitoring get service personal-server-monitoring-grafana -o jsonpath='{.spec.type}'

# uninstall: --uninstall --delete-data가 함께 있어야 PVC를 삭제함
helm uninstall personal-server-monitoring --namespace monitoring --wait --timeout 5m
```

`monitoring-verify.sh --port-forward-check`는 background port-forward를 trap으로 종료하고 `http://127.0.0.1:3000/login` 응답만 확인함. Grafana 로그인 비밀번호를 조회·출력하지 않음.

- [ ] **Step 4: 테스트 통과 및 렌더 확인**

Run: `python3 -m unittest tests.test_k8s_monitoring_values tests.test_k8s_monitoring_tools -v`

Expected: PASS.

Run: `bash -n infra/k8s/tools/monitoring-preflight.sh infra/k8s/tools/monitoring-install.sh infra/k8s/tools/monitoring-verify.sh infra/k8s/tools/monitoring-uninstall.sh`

Expected: PASS.

Run: `helm template personal-server-monitoring prometheus-community/kube-prometheus-stack --version 88.6.1 --namespace monitoring --values infra/k8s/monitoring/values.n100.yaml >/dev/null`

Expected: PASS. 이 명령은 template render만 수행하며 클러스터 리소스를 변경하지 않음.

- [ ] **Step 5: 운영 문서 추가**

`infra/k8s/README.md`에 다음 순서와 금지 사항을 추가함.

```text
1. monitoring-preflight.sh
2. monitoring-install.sh --render
3. 운영자 승인 후 monitoring-install.sh --apply
4. monitoring-verify.sh
5. 필요할 때만 monitoring-verify.sh --port-forward-check
```

Grafana 접속 명령은 `sudo k3s kubectl -n monitoring port-forward --address 127.0.0.1 service/personal-server-monitoring-grafana 3000:80`으로 문서화하고, 비밀번호는 Kubernetes Secret에서 운영자가 직접 확인하되 채팅·Git·명령 로그에 남기지 않도록 명시함.

- [ ] **Step 6: 커밋**

```bash
git add infra/k8s/tools/monitoring-install.sh infra/k8s/tools/monitoring-verify.sh infra/k8s/tools/monitoring-uninstall.sh tests/test_k8s_monitoring_tools.py infra/k8s/README.md
git commit -m "feat: K3s Monitoring 운영 도구 추가"
```

### Task 4: 전체 검증 및 독립 검토

**Files:**
- Modify: 없음

**Interfaces:**
- Consumes: Tasks 1-3의 설정·도구·테스트.
- Produces: review-ready branch와 N100 실행 전 검증 증적.

- [ ] **Step 1: 전체 관련 테스트 실행**

Run: `python3 -m unittest tests.test_k8s_monitoring_values tests.test_k8s_monitoring_tools tests.test_k8s_sre_health_audit tests.test_k8s_sre_pod_recovery_lab -v`

Expected: PASS.

- [ ] **Step 2: 정적 및 변경 범위 검사**

Run: `git diff --check main...HEAD`

Expected: PASS.

Run: `python3 scripts/run_change_harness.py --input <git-name-status-z-file> --input-format git-name-status-z --check-result maintenance=success --agent-context`

Expected: `ready_for_review`.

- [ ] **Step 3: 독립 검토**

검토자는 다음을 확인함.

```text
- `--apply`와 `--uninstall` 외에 K3s·Helm 쓰기 명령이 없는지
- 외부 노출 Service/Ingress/Caddy 변경이 없는지
- Grafana Secret 값이 출력되지 않는지
- uninstall 기본 동작이 PVC를 보존하는지
- chart version·namespace·release 이름이 모든 도구와 문서에서 일치하는지
```

- [ ] **Step 4: 커밋 상태 확인**

Run: `git status --short`

Expected: 빈 출력.

## 계획 자체 점검

- Spec 범위: Task 1은 저장소·접근 정책, Task 2는 사전점검, Task 3은 설치·검증·삭제, Task 4는 검토를 담당하여 모든 설계 요구사항을 포함함.
- 모호성: chart version 88.6.1, namespace `monitoring`, release `personal-server-monitoring`, PVC 크기·retention·접속 방식·삭제 인자를 고정함.
- 범위: Alertmanager, Telegram, 애플리케이션 metrics, 외부 공개는 계획에 포함하지 않음.
