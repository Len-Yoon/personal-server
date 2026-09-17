# YouTube Memo K3s 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** YouTube Memo를 Docker 단일 writer에서 K3s 단일 writer로 안전하게 전환할 수 있는 코드·검증·운영 절차를 제공함.

**Architecture:** YouTube Memo 전용 PVC·replica 0 Deployment·ClusterIP Service·prepare/cutover 도구를 Book Memo 패턴으로 추가함. Caddy는 runtime state에 따라 Docker DNS 또는 검증된 K3s Service endpoint를 동적으로 선택하며, 전환 도구는 Docker 중지→전체 데이터 복사→digest 확인→K3s 기동 순서를 fail-closed로 강제함.

**Tech Stack:** Bash, Python 3, Docker Buildx OCI archive, K3s/containerd, Kubernetes YAML, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-17-youtube-memo-k3s-cutover-design.md`

## Global Constraints

- 실제 N100 Docker 중지·PVC 데이터 복사·Caddy 재기동·Tunnel ingress 변경은 구현 단계에서 수행하지 않음.
- Docker와 K3s가 같은 `data/youtube-memo` 데이터를 동시에 쓰지 않음.
- immutable Linux AMD64 image digest만 허용하고 Secret 값·개인 데이터는 읽거나 출력하지 않음.
- Books·Portal 운영 경로 및 데이터는 변경하지 않음.
- K3s 상태에서 Compose YouTube Memo 기동 또는 Docker loopback 8002 health 요구를 금지함.

---

## File Structure

| 파일 | 역할 |
|---|---|
| `infra/k8s/apps/youtube-memo.yaml` | YouTube Memo PVC·inert Deployment·ClusterIP Service 선언 |
| `infra/k8s/tools/youtube-memo-prepare.sh` | image/Secret/PVC 및 replica 0 준비를 검증·적용 |
| `infra/k8s/tools/youtube-memo-cutover.sh` | 데이터 전체 복사·digest·단일 writer 상태 전환을 fail-closed로 수행 |
| `caddy/Caddyfile` | `YOUTUBE_MEMO_UPSTREAM` 기반 upstream 선택 |
| `docker-compose.n100.yml` | Caddy의 Docker YouTube 의존성 제거 및 기본 upstream 제공 |
| `scripts/deploy-n100.sh` | YouTube K3s endpoint 확인 후 Caddy 재생성 |
| `scripts/windows-bootstrap.sh` | 부팅·복구 시 K3s YouTube를 Docker로 재기동하지 않음 |
| `scripts/verify-n100-deployment-health.sh` | K3s YouTube의 Deployment·Service·Endpoint·PVC 검증 |
| `scripts/verify-n100-safe-deployment-health.sh` | K3s YouTube를 Docker safe health 대상에서 제외 |
| `tests/test_k8s_youtube_memo_migration.py` | manifest/prepare/cutover의 single-writer·데이터 검증 계약 |
| `tests/test_runtime_service_deployment_contract.py` | runtime state와 Caddy/health의 YouTube K3s 계약 |
| `tests/test_deploy_n100.py`, `tests/test_windows_bootstrap.py`, `tests/test_n100_safe_deployment.py` | 배포·부팅·safe deployment 회귀 계약 |
| `docs/operations-reference.md`, `docs/cloudflare-tunnel.md` | 실제 전환 승인 전/후 운영 절차 및 공개 경로 문서 |

### Task 1: YouTube Memo K3s 준비·전환 자산

**Files:**
- Create: `infra/k8s/apps/youtube-memo.yaml`
- Create: `infra/k8s/tools/youtube-memo-prepare.sh`
- Create: `infra/k8s/tools/youtube-memo-cutover.sh`
- Create: `tests/test_k8s_youtube_memo_migration.py`

**Interfaces:**
- `youtube-memo-prepare.sh --go|--bind-existing --image docker.io/library/personal-server-youtube-memo@sha256:<digest>`는 Secret 존재와 imported image만 확인하고 Deployment를 replica 0으로 준비함.
- `youtube-memo-cutover.sh --check|--prepare|--go|--rollback --source <absolute-dir> --database youtube_memo.sqlite3 --image <digest>`는 `--go`에서만 writer 상태를 바꿀 수 있음.

- [ ] **Step 1: Book Memo 계약을 기준으로 YouTube 전용 실패 테스트를 작성함.**

```python
def test_manifest_is_inert_nonroot_and_uses_youtube_data_pvc(self):
    deployment = find_document("Deployment")
    assert deployment["spec"]["replicas"] == 0
    assert container["image"] == "personal-server-youtube-memo:unconfigured-do-not-run"
    assert container["volumeMounts"] == [
        {"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]

def test_go_stops_docker_before_copy_and_starts_only_k3s_writer(self):
    result, calls = run_cutover("--go")
    assert result.returncode == 0
    assert command_index(calls, "docker stop") < command_index(calls, "copy") < command_index(calls, "--replicas=1")
```

- [ ] **Step 2: 새 테스트가 자산 부재로 실패하는지 확인함.**

Run: `python3 -m unittest tests.test_k8s_youtube_memo_migration -v`

Expected: FAIL because YouTube manifest and tools do not exist.

- [ ] **Step 3: Book Memo 도구의 fail-closed 제어 흐름을 서비스 고유 값으로 최소 복제함.**

```yaml
metadata:
  name: youtube-memo
  namespace: personal-server
spec:
  replicas: 0
  strategy:
    type: Recreate
```

`youtube-memo-data`, port `8002`, mount `/data/youtube-memo`, Secret `youtube-memo-runtime`, database `youtube_memo.sqlite3`만 허용함. 데이터 directory 전체 digest·SQLite quick_check·helper Pod UID precondition·복구 시 양쪽 writer 중지 원칙을 Book Memo와 동일하게 유지함.

- [ ] **Step 4: 새 전환 테스트를 통과시킴.**

Run: `python3 -m unittest tests.test_k8s_youtube_memo_migration -v`

Expected: PASS.

### Task 2: Caddy·배포·health runtime-state 정합화

**Files:**
- Modify: `caddy/Caddyfile`
- Modify: `docker-compose.n100.yml`
- Modify: `scripts/deploy-n100.sh`
- Modify: `scripts/windows-bootstrap.sh`
- Modify: `scripts/verify-n100-deployment-health.sh`
- Modify: `scripts/verify-n100-safe-deployment-health.sh`
- Modify: `tests/test_runtime_service_deployment_contract.py`
- Modify: `tests/test_deploy_n100.py`
- Modify: `tests/test_windows_bootstrap.py`
- Modify: `tests/test_n100_safe_deployment.py`

**Interfaces:**
- `YOUTUBE_MEMO_UPSTREAM` defaults to `youtube-memo:8002` only for Compose runtime.
- K3s runtime resolution requires ready/available Deployment, rollout success, Service selector `youtube-memo`, ClusterIP, port `8002`, target port `http`, Endpoint address/port and Bound PVC before Caddy is recreated.

- [ ] **Step 1: K3s YouTube에서 Docker를 참조하면 실패하는 regression test를 작성함.**

```python
def test_youtube_memo_k3s_health_uses_kubernetes_endpoint_not_docker_port(self):
    result, calls = run_health_check("crawler-worker=compose\\nyoutube-memo=k3s\\nbook-memo=k3s\\n")
    assert result.returncode == 0
    assert "service/youtube-memo" in calls
    assert "127.0.0.1:8002/health" not in calls

def test_caddy_refuses_recreation_when_youtube_k3s_endpoint_is_not_ready(self):
    result = run_deploy_with_unready_youtube()
    assert result.returncode != 0
    assert "Caddy" not in result.recreate_calls
```

- [ ] **Step 2: 테스트가 현재 Docker 고정 경로 때문에 실패하는지 확인함.**

Run: `python3 -m unittest tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap tests.test_n100_safe_deployment -v`

Expected: FAIL because YouTube K3s has no Caddy/health specialization.

- [ ] **Step 3: Book Memo의 검증 함수를 일반 서비스 helper로 좁게 확장함.**

```bash
resolve_youtube_memo_caddy_upstream() {
  if [[ "$YOUTUBE_MEMO_RUNTIME_MODE" != k3s ]]; then
    export YOUTUBE_MEMO_UPSTREAM="youtube-memo:8002"
    return 0
  fi
  require_k3s_service_endpoint youtube-memo youtube-memo-data 8002
  export YOUTUBE_MEMO_UPSTREAM="$service_cluster_ip:$service_port"
}
```

Caddy는 `reverse_proxy {env.YOUTUBE_MEMO_UPSTREAM}`를 사용함. Caddy `depends_on.youtube-memo`을 삭제함. 모든 Caddy 재생성 직전에 Book과 YouTube upstream을 모두 resolve하여, 어느 하나라도 K3s 상태가 불완전하면 기존 Caddy를 건드리지 않음.

- [ ] **Step 4: runtime-state 관련 테스트를 통과시킴.**

Run: `python3 -m unittest tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap tests.test_n100_safe_deployment -v`

Expected: PASS.

### Task 3: 운영 문서·변경 범위·통합 검증

**Files:**
- Modify: `docs/operations-reference.md`
- Modify: `docs/cloudflare-tunnel.md`
- Modify: `docs/README.md`
- Modify: `AGENTS.md` only if a user-approved, YouTube-specific N100 cutover exception is needed before actual operation
- Test: `tests/test_documentation_index.py`

- [ ] **Step 1: 준비 단계와 실제 전환 단계를 분리하는 문서 계약을 작성함.**

문서는 준비 단계에서 Docker가 production이고 Tunnel이 변경되지 않음을 명시함. 실제 전환 명령에는 Secret 값이 아닌 Secret 존재 확인, Docker 중지, 데이터 digest, K3s readiness, root-owned state marker, Caddy 내부 health, Tunnel 변경, 외부 health 3회, 롤백 제한을 순서대로 기록함.

- [ ] **Step 2: 문서 색인과 정적 계약을 실행함.**

Run: `python3 -m unittest tests.test_documentation_index -v && docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet`

Expected: PASS.

- [ ] **Step 3: 변경 경로 하네스와 관련 테스트를 실행함.**

Run:
```bash
git diff --name-status -z --find-renames HEAD > /tmp/youtube-memo-change-paths.z
python3 scripts/run_change_harness.py --input /tmp/youtube-memo-change-paths.z --input-format git-name-status-z --agent-context
python3 -m unittest tests.test_k8s_youtube_memo_migration tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap tests.test_n100_safe_deployment tests.test_documentation_index -v
```

Expected: all targeted checks PASS.

- [ ] **Step 4: 독립 검토를 수행하고 결과를 기록함.**

검토 범위는 이중 writer, Secret 출력, sentinel image, K3s endpoint fail-closed, Caddy 재생성 순서, Docker 자동 재기동, Books·Portal 비변경 여부임.

## Execution Gate

코드가 검증·독립 검토를 통과한 뒤에도 다음은 사용자 별도 승인 전까지 수행하지 않음.

1. 원격 main 병합 또는 N100 동기화
2. N100 image import·Secret/PVC/manifest 적용
3. Docker YouTube Memo 중지 또는 data copy
4. root-owned runtime state 변경
5. Caddy 재생성 또는 Cloudflare Tunnel ingress 변경
