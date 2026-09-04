# N100 무인 K3s 전환 실행기 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 세 Compose 서비스의 K3s 전환 기반을 만들고, 자동 배포·재부팅이 Docker writer를 다시 만들지 못하게 함.

**Architecture:** 저장소에는 marker 계약과 검증 가능한 root release artifact만 둠. 설치된 runner는 root-owned native ext4 경로에서만 실행되며 `/mnt/c`·사용자 홈은 runtime 입력으로 사용하지 않음.

**Tech Stack:** Bash, Python standard library, unittest, systemd `LoadCredentialEncrypted=`, K3s, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-04-unattended-k3s-transition-runner-design.md`

## Global Constraints

- 대상은 `crawler-worker`, `youtube-memo`, `book-memo`만 허용함.
- Portal, Caddy 자체, Cloudflare Tunnel, crawler scheduler, HomeOps, system-agent, car-care는 변경하지 않음.
- rclone config·passphrase·age identity·Telegram credential은 Git, `.env`, CLI, 로그에 기록하지 않음.
- 실제 data/public-route cutover는 PR 이후 별도 N100 운영 승인 범위임.

---

### Task 1: 서비스별 runtime marker 계약

**Files:**
- Create: `scripts/runtime-service-state.sh`
- Create: `tests/test_runtime_service_state.py`
- Modify: `tests/test_verify_change_scope.py`

**Interfaces:** `load_service_runtime_state <project-root>`는 저장소 경로를 신뢰하지 않고 `/var/lib/personal-server/k3s-runtime-services.state`만 읽음. 고정 anchor(`/var/lib`)와 상위 디렉터리는 root 소유·비사용자쓰기·실제 디렉터리여야 하며, state 파일은 root 신뢰 디렉터리 내 regular non-symlink 파일만 허용함. 고정 anchor가 신뢰된 상태에서만 아직 설치되지 않은 native state parent/file을 clean absent로 보고 모두 compose로 처리하며, anchor/상위 경로가 없거나 접근 불가하면 nonzero로 거부함. 허용 행은 세 서비스의 `=compose|k3s`뿐이며 duplicate·unknown·empty·malformed row는 nonzero로 거부함. 테스트는 명시적 test-only fixture seam을 사용함.

- [ ] **Step 1: Write the failing tests**

```python
def test_unknown_service_in_runtime_state_is_rejected(tmp_path):
    state = tmp_path / "data" / "k3s-runtime-services.state"
    state.parent.mkdir()
    state.write_text("crawler-worker=compose\nunknown=k3s\n", encoding="utf-8")
    assert run_state_loader(tmp_path).returncode != 0

def test_missing_runtime_state_defaults_all_services_to_compose(tmp_path):
    assert run_state_loader(tmp_path).stdout.splitlines() == [
        "crawler-worker=compose", "youtube-memo=compose", "book-memo=compose"
    ]
```

- [ ] **Step 2: Verify RED** — `python3 -m unittest tests/test_runtime_service_state.py -v` must fail because parser is absent.
- [ ] **Step 3: Implement strict parsing** — fixed shell arrays and `case`; never turn state text into a path or command.
- [ ] **Step 4: Verify GREEN** — run `python3 -m unittest tests/test_runtime_service_state.py tests/test_verify_change_scope.py -v`.
- [ ] **Step 5: Commit** — `git add scripts/runtime-service-state.sh tests/test_runtime_service_state.py tests/test_verify_change_scope.py && git commit -m "feat: 서비스별 K3s runtime 상태 계약 추가"`.

### Task 2: 자동 배포·부팅·health의 Compose 이중 writer 방지

**Files:**
- Modify: `scripts/deploy-n100.sh`
- Modify: `scripts/windows-bootstrap.sh`
- Modify: `scripts/verify-n100-deployment-health.sh`
- Modify: `tests/test_deploy_n100.py`
- Create: `tests/test_runtime_service_deployment_contract.py`

**Interfaces:** Task 1 parser를 source함. `k3s` marker 서비스는 Compose `up`·Compose running requirement에서 제외함. 같은 서비스는 K3s Deployment Ready를 요구하고 Compose writer가 존재하면 health가 실패해야 함.

- [ ] **Step 1: Write failing tests**

```python
def test_deploy_omits_only_k3s_marked_service():
    assert "crawler-worker" not in rendered_compose_services({"crawler-worker": "k3s"})
    assert "youtube-memo" in rendered_compose_services({"crawler-worker": "k3s"})

def test_health_rejects_compose_writer_for_k3s_service():
    assert run_health({"book-memo": "k3s"}, running=["book-memo"]).returncode != 0
```

- [ ] **Step 2: Verify RED** — `python3 -m unittest tests/test_runtime_service_deployment_contract.py -v` must fail before implementation.
- [ ] **Step 3: Implement minimum marker-aware service arrays** — preserve Portal branches and non-target services unchanged.
- [ ] **Step 4: Verify GREEN** — run `python3 -m unittest tests/test_runtime_service_state.py tests/test_runtime_service_deployment_contract.py tests/test_deploy_n100.py -v`.
- [ ] **Step 5: Commit** — commit only the three startup/health scripts and tests with `fix: 전환 서비스 Compose 재기동 방지`.

### Task 3: root transition runner release artifact와 policy validator

**Files:**
- Create: `infra/k8s/transition-runner/policy/runner-policy.json`
- Create: `infra/k8s/transition-runner/runner/personal-server-transition-runner`
- Create: `infra/k8s/transition-runner/systemd/personal-server-transition.service`
- Create: `infra/k8s/tools/validate-transition-runner-policy.py`
- Create: `tests/test_k8s_transition_runner_policy.py`
- Create: `tests/test_k8s_transition_runner_artifacts.py`

**Interfaces:** Policy fixes allowlisted services, namespace, PVCs, `sha256:<64 lowercase hex>` images, lifecycle phases and positive timeouts. Validator is `python3 infra/k8s/tools/validate-transition-runner-policy.py <policy-path>`. Runner accepts no arbitrary service, path, command, repository or config arguments.

- [ ] **Step 1: Write failing tests**

```python
def test_policy_rejects_mutable_image_tag(tmp_path):
    assert validate(valid_policy(image="personal-server-book-memo:latest")).returncode != 0

def test_runner_never_references_user_writable_runtime_paths():
    assert "/mnt/c" not in read_runner_artifact()
    assert ".config/rclone" not in read_runner_artifact()
```

- [ ] **Step 2: Verify RED** — run the two transition-runner test modules; they must fail because artifacts are absent.
- [ ] **Step 3: Implement policy and validator** — standard-library JSON only; reject unknown keys, traversal, duplicate services, mutable tags and nonpositive timeouts.
- [ ] **Step 4: Implement fixed runner and unit** — fixed phases `preflight`, `backup`, `stop-compose`, `copy-pvc`, `start-k3s`, `verify-private`, `record`; use per-service `flock` below `/var/lib/personal-server-transition/locks`; unit uses root user, `LoadCredentialEncrypted=`, `PrivateTmp=yes`, `ProtectHome=yes`, fixed ExecStart.
- [ ] **Step 5: Verify GREEN and commit** — focused tests must pass, then commit `feat: root 전환 실행기 release artifact 추가`.

### Task 4: controlled N100 installer와 통합 검토

**Files:**
- Create: `infra/k8s/tools/install-transition-runner.sh`
- Create: `infra/k8s/tools/transition-runner-preflight.sh`
- Create: `docs/n100-transition-runner-install.md`
- Create: `tests/test_k8s_transition_runner_install_tools.py`
- Modify: `docs/k3s-flux-transition-draft.md`
- Modify: `infra/k8s/README.md`

**Interfaces:** Installer requires `--apply`, root UID, native ext4 destination, release digest and 0600 encrypted credential files. Preflight defaults to read-only and prints names/statuses only, never values.

- [ ] **Step 1: Write failing tests**

```python
def test_installer_refuses_apply_without_explicit_flag():
    assert run("infra/k8s/tools/install-transition-runner.sh").returncode != 0

def test_preflight_never_prints_credential_contents():
    assert "rclone-config-passphrase=" not in run_preflight().stdout
```

- [ ] **Step 2: Verify RED** — installer test module must fail because tools are absent.
- [ ] **Step 3: Implement** — verify artifact SHA-256 before atomic root-owned install to `/usr/local/libexec/personal-server-transition` and `/etc/personal-server-transition`; never execute the source repository as root after installation.
- [ ] **Step 4: Verify GREEN** — run installer/policy/artifact tests and `bash -n` on the three shell artifacts.
- [ ] **Step 5: Integrated review and PR** — run all focused tests, `git diff --check`, NUL name-status change harness with known checks only, and independent Terra review. Commit documentation, push one PR, and do not merge automatically.

## Execution Boundary

This plan ends at tested release artifacts and a reviewed PR. One-time root installation, credential seed and every actual data/public-route cutover require separate N100 operating approval.
