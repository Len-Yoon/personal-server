# Crawler Worker K3s 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 뉴스 수집기를 Docker Compose 단일 writer에서 K3s 단일 writer로 안전하게 전환할 수 있는 코드·검증·운영 절차를 제공함.

**Architecture:** crawler 전용 PVC·replica 0 Deployment·ClusterIP Service·prepare/cutover 도구를 Book/YouTube 패턴으로 추가함. runtime state와 Caddy는 Docker hostname 또는 검증된 K3s Service endpoint를 하나만 선택하며, ServiceMonitor는 K3s Service에서 기존 인증 metrics를 수집함.

**Tech Stack:** Bash, Python 3, Docker Buildx OCI archive, K3s/containerd, Kubernetes YAML, Prometheus Operator, Python unittest.

**Spec:** `docs/superpowers/specs/2026-09-17-crawler-worker-k3s-cutover-design.md`

## Global Constraints

- Docker와 K3s가 `data/crawler-worker`를 동시에 쓰지 않음.
- Secret·Telegram 값·개인 뉴스 데이터는 읽거나 출력하거나 Git에 저장하지 않음.
- immutable Linux AMD64 image digest만 허용함.
- Portal·Book Memo·YouTube Memo·차량관리와 기존 PVC는 변경하지 않음.
- 실제 N100 Docker 중지, data 복사, Caddy/Tunnel 재기동은 구현 커밋과 별도로 사용자 승인을 받아 수행함.
- 구현 전 `AGENTS.md`의 crawler-worker 전환 최소 변경 예외를 사용자 승인으로 추가해야 함. 예외가 없으면 코드 구현을 시작하지 않음.

## File Structure

| 파일 | 역할 |
|---|---|
| `infra/k8s/apps/crawler-worker.yaml` | crawler PVC·inert Deployment·ClusterIP Service 선언 |
| `infra/k8s/tools/crawler-worker-prepare.sh` | image/Secret/PVC 및 replica 0 준비를 검증·적용 |
| `infra/k8s/tools/crawler-worker-cutover.sh` | 전체 데이터 복사·SQLite digest·단일 writer 전환을 fail-closed로 수행 |
| `infra/k8s/sre-telegram/crawler-news-observability.yaml` | K3s crawler Service의 인증 metrics 수집 선언 |
| `caddy/Caddyfile` | `CRAWLER_WORKER_UPSTREAM` 기반 공개 upstream 선택 |
| `docker-compose.n100.yml` | Caddy 기본 Compose upstream 및 Docker crawler `depends_on` 정합화 |
| `scripts/deploy-n100.sh`, `scripts/windows-bootstrap.sh` | runtime state별 crawler endpoint 검증·Caddy 재생성 |
| `scripts/verify-n100-deployment-health.sh` | K3s crawler Deployment·Service·Endpoint·PVC health 확인 |
| `tests/test_k8s_crawler_worker_migration.py` | manifest·prepare·cutover single-writer·data 계약 |
| `tests/test_runtime_service_deployment_contract.py` | runtime state·Caddy·health 계약 |

### Task 0: 운영 범위 승인과 변경 계약

**Files:**
- Modify: `AGENTS.md` — 사용자 승인 후 crawler-worker 전환의 최소 변경 예외만 추가
- Modify: `scripts/verify_change_scope.py` — crawler 전환 도구 변경에 `maintenance`와 `crawler-worker` 검사를 모두 요구
- Test: `tests/test_verify_change_scope.py`

- [ ] **Step 1: crawler-worker 전환에 필요한 파일을 명시함.**

`infra/k8s/apps/crawler-worker.yaml`, `infra/k8s/tools/crawler-worker-prepare.sh`, `infra/k8s/tools/crawler-worker-cutover.sh`, `infra/k8s/sre-telegram/crawler-news-observability.yaml`, Caddy/runtime/deployment health 스크립트, `scripts/verify_change_scope.py`, 직접 테스트·문서만 허용함. Portal·Book·YouTube·기존 운영 데이터는 제외함.

- [ ] **Step 2: 변경 범위 테스트를 먼저 추가하고 실패를 확인함.**

```python
def test_crawler_k3s_cutover_scope_requires_crawler_and_maintenance_checks(self):
    evidence = inspect_paths(
        "infra/k8s/tools/crawler-worker-cutover.sh",
        executed_checks=("maintenance", "crawler-worker"),
    )
    self.assertEqual(evidence["missing_checks"], [])
```

Run: `python3 -m unittest tests.test_verify_change_scope -v`

Expected: crawler 전환 예외가 없으면 실패함.

- [ ] **Step 3: 승인된 최소 예외와 테스트를 일치시킴.**

`infra/k8s/apps/crawler-worker.yaml`, `infra/k8s/tools/crawler-worker-prepare.sh`, `infra/k8s/tools/crawler-worker-cutover.sh`는 infrastructure 경로이지만 crawler 전환 전용 정책 목록으로 분류해 `maintenance`와 `crawler-worker`를 함께 요구함. 실제 N100 데이터·Caddy·Tunnel 변경은 별도 사용자 승인 없이는 금지하고, 코드·테스트·문서 준비만 허용하는 문구로 제한함.

- [ ] **Step 4: 변경 범위 계약을 통과시킴.**

Run: `python3 -m unittest tests.test_verify_change_scope -v`

Expected: PASS.

### Task 1: K3s 준비 자산과 전환 계약

**Files:**
- Create: `infra/k8s/apps/crawler-worker.yaml`
- Create: `infra/k8s/tools/crawler-worker-prepare.sh`
- Create: `infra/k8s/tools/crawler-worker-cutover.sh`
- Create: `tests/test_k8s_crawler_worker_migration.py`

**Interfaces:**
- `crawler-worker-prepare.sh --go|--bind-existing --image docker.io/library/personal-server-crawler-worker@sha256:<digest>`는 Secret 존재와 imported image를 확인하고 replica 0으로 준비함.
- `crawler-worker-cutover.sh --check|--prepare|--go|--rollback --source <absolute-dir> --image <digest>`는 `--check`·`--prepare`에서 writer 상태를 바꾸지 않으며, `--go`·`--rollback`만 명시적으로 writer 상태를 바꿈.

- [ ] **Step 1: fail-closed 전환 테스트를 작성함.**

```python
def test_manifest_is_inert_nonroot_and_mounts_crawler_data_pvc(self):
    self.assertEqual(deployment["spec"]["replicas"], 0)
    self.assertEqual(container["image"], "personal-server-crawler-worker:unconfigured-do-not-run")
    self.assertIn({"name": "crawler-worker-data", "mountPath": "/data/crawler-worker"}, container["volumeMounts"])

def test_go_stops_docker_before_copy_and_starts_only_k3s_writer(self):
    result, calls = run_cutover("--go")
    self.assertEqual(result.returncode, 0)
    self.assertLess(index(calls, "docker stop"), index(calls, "data copy"))
    self.assertLess(index(calls, "data copy"), index(calls, "--replicas=1"))
```

- [ ] **Step 2: 자산 부재로 RED를 확인함.**

Run: `python3 -m unittest tests.test_k8s_crawler_worker_migration -v`

Expected: manifest·prepare·cutover 파일 부재로 실패함.

- [ ] **Step 3: Book Memo의 안전 제어 흐름을 crawler 고유 계약으로 제한해 구현함.**

PVC `crawler-worker-data`, port `8001`, mount `/data/crawler-worker`, Secret `crawler-worker-runtime`만 허용함. 현재 writer가 사용하는 `news_archive.json`·`news_collection_status.json`을 포함한 전체 directory digest를 비교하고, 실제 존재하는 SQLite 파일에만 `quick_check`를 적용함. helper Pod UID precondition과 실패 시 양쪽 writer 중지 원칙을 유지함.

- [ ] **Step 4: 전환 계약을 GREEN으로 만듦.**

Run: `python3 -m unittest tests.test_k8s_crawler_worker_migration -v`

Expected: PASS.

### Task 2: Caddy·runtime-state·health 정합화

**Files:**
- Modify: `caddy/Caddyfile`
- Modify: `docker-compose.n100.yml`
- Modify: `scripts/deploy-n100.sh`
- Modify: `scripts/windows-bootstrap.sh`
- Modify: `scripts/verify-n100-deployment-health.sh`
- Modify: `tests/test_runtime_service_deployment_contract.py`
- Modify: `tests/test_deploy_n100.py`
- Modify: `tests/test_windows_bootstrap.py`

**Interfaces:**
- `CRAWLER_WORKER_UPSTREAM`은 Compose 상태에서만 `crawler-worker:8001` 기본값을 사용함.
- K3s 상태에서는 ready/available Deployment, rollout, Service selector, ClusterIP, port `8001`, target port `http`, Endpoint, Bound PVC가 모두 검증돼야 Caddy를 재생성할 수 있음.

- [ ] **Step 1: crawler K3s에서 Docker loopback/hostname을 사용하면 실패하는 테스트를 추가함.**

```python
def test_crawler_k3s_health_uses_kubernetes_endpoint_not_docker_port(self):
    result, calls = self._run_health_check(
        "crawler-worker=k3s\nyoutube-memo=k3s\nbook-memo=k3s\n"
    )
    self.assertEqual(result.returncode, 0)
    self.assertIn("service/crawler-worker", calls)
    self.assertNotIn("127.0.0.1:8001/health", calls)
```

- [ ] **Step 2: 기존 Docker 고정 뉴스 경로가 RED를 만드는지 확인함.**

Run: `python3 -m unittest tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap -v`

Expected: crawler K3s endpoint/Caddy 계약 부재로 실패함.

- [ ] **Step 3: 기존 memo endpoint helper를 일반화하지 않고 crawler 전용 resolver를 추가함.**

```bash
resolve_crawler_worker_caddy_upstream() {
  if [[ "$CRAWLER_WORKER_RUNTIME_MODE" != k3s ]]; then
    export CRAWLER_WORKER_UPSTREAM="crawler-worker:8001"
    return 0
  fi
  CRAWLER_WORKER_UPSTREAM=$(require_k3s_service_endpoint \
    crawler-worker crawler-worker-data 8001 "Crawler Worker") || return 1
  export CRAWLER_WORKER_UPSTREAM
}
```

모든 Caddy 재생성 직전에 crawler·YouTube·Book endpoint를 resolve함. 하나라도 불완전하면 실행 중인 Caddy를 유지하고 실패함.

- [ ] **Step 4: runtime-state 회귀를 통과시킴.**

Run: `python3 -m unittest tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap tests.test_n100_safe_deployment -v`

Expected: PASS.

### Task 3: K3s native metrics와 경고 연속성

**Files:**
- Modify: `infra/k8s/sre-telegram/crawler-news-observability.yaml`
- Modify: `infra/k8s/sre-telegram/prometheus-rule.yaml` only if labels/queries need a contract update
- Modify: `tests/test_k8s_sre_telegram_manifests.py`
- Modify: `tests/test_k8s_monitoring_tools.py`

- [ ] **Step 1: K3s crawler Service label을 찾는 실패 테스트를 작성함.**

```python
def test_crawler_service_monitor_selects_native_k3s_service(self):
    monitor = read_service_monitor("crawler-news-observability")
    self.assertEqual(monitor["spec"]["selector"]["matchLabels"], {
        "app.kubernetes.io/name": "crawler-worker"
    })
    self.assertEqual(monitor["spec"]["endpoints"][0]["port"], "http")
```

- [ ] **Step 2: Compose bridge label에서 RED를 확인함.**

Run: `python3 -m unittest tests.test_k8s_sre_telegram_manifests tests.test_k8s_monitoring_tools -v`

Expected: native crawler Service selector 부재로 실패함.

- [ ] **Step 3: ServiceMonitor selector만 native Service label로 전환함.**

metrics path `/internal/metrics`, bearer Secret reference `crawler-news-metrics`, interval, `NewsCollectionStale` 의미를 바꾸지 않음.

- [ ] **Step 4: 관측성 계약을 GREEN으로 만듦.**

Run: `python3 -m unittest tests.test_k8s_sre_telegram_manifests tests.test_k8s_monitoring_tools -v`

Expected: PASS.

### Task 4: 문서·변경 하네스·독립 검토

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/operations-reference.md`
- Modify: `docs/cloudflare-tunnel.md`
- Modify: `docs/operations-roadmap.md`
- Modify: `tests/test_documentation_index.py`

- [ ] **Step 1: 준비와 실제 cutover를 분리하는 문서 계약을 작성함.**

준비 단계에서는 Docker가 production writer이며 public Tunnel은 바뀌지 않음을 기록함. 실제 cutover에는 유지보수 진입·진행 중 복구 종료, HomeOps runtime marker 읽기 적용, root-owned state marker 선갱신·crawler 복구 제외 확인, Docker 중지, 전체 data digest, K3s readiness·Docker 중지 재확인, native metrics target, Caddy internal health, Tunnel 전환, 외부 health 3회, rollback 제한을 순서대로 기록함.

- [ ] **Step 2: 문서·정적 검사를 실행함.**

Run:

```bash
python3 -m unittest tests.test_documentation_index -v
docker compose -f docker-compose.yml -f docker-compose.n100.yml config --quiet
git diff --check
```

Expected: PASS.

- [ ] **Step 3: 변경 하네스와 관련 테스트를 실행함.**

Run:

```bash
git diff --name-status -z --find-renames HEAD > /tmp/crawler-k3s-change-paths.z
python3 scripts/run_change_harness.py --input /tmp/crawler-k3s-change-paths.z --input-format git-name-status-z --agent-context
python3 -m unittest tests.test_k8s_crawler_worker_migration tests.test_runtime_service_deployment_contract tests.test_deploy_n100 tests.test_windows_bootstrap tests.test_n100_safe_deployment tests.test_k8s_sre_telegram_manifests tests.test_k8s_monitoring_tools tests.test_documentation_index -v
```

Expected: related checks PASS. 전체 test runner의 기존 환경 실패는 변경과의 비연관 근거를 분리해 기록함.

- [ ] **Step 4: 독립 운영 검토를 완료함.**

검토 범위는 Docker/K3s 이중 writer, Secret 출력, sentinel image, endpoint fail-closed, Caddy 재생성 순서, native ServiceMonitor, Books·YouTube·Portal 비변경 여부임.

## Execution Gate

코드 검증·독립 검토·저장소 병합 뒤에도 다음은 별도 사용자 승인 전까지 수행하지 않음.

1. N100 이미지 import, Secret/PVC/manifest 적용
2. Docker crawler 중지 또는 data copy
3. root-owned runtime state 변경
4. Caddy 재생성 또는 Cloudflare Tunnel ingress 변경
5. 외부 `news.len.pe.kr` 전환 검증
