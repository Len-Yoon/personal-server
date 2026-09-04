# N100 원격 개발 환경 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** macOS에서 N100 WSL의 지속형 Codex 개발 작업을 안전하게 제어하는 명령을 제공함.

**Architecture:** macOS 제어 스크립트가 전용 SSH 키로 Windows WSL 실행기를 호출함. WSL 실행기는 고정 tmux 세션과 사용자 상태 디렉터리만 사용하며, Codex 작업에 외부 변경 금지 규칙을 고정함.

**Tech Stack:** Bash, OpenSSH, Windows OpenSSH, WSL, tmux, Codex CLI, Python unittest

**Spec:** `docs/superpowers/specs/2026-09-04-n100-remote-development-environment-design.md`

## Global Constraints

- 서버 기동·스케줄러·K3s·Compose·Caddy·터널을 수정하지 않음.
- 비밀번호·개인키·토큰·rclone 설정을 Git, 로그, 상태 파일에 저장하지 않음.
- Codex 작업은 로컬 변경·테스트·커밋까지만 허용하며 외부 상태 변경은 사용자 승인으로 남김.
- 모든 시간 기록은 UTC ISO 8601 형식으로 저장함.

---

### Task 1: macOS SSH 제어 명령

**Files:**
- Create: `scripts/n100-remote-dev.sh`
- Test: `tests/test_n100_remote_dev.py`

**Interfaces:**
- Produces: `keygen`, `preflight`, `start`, `status`, `logs`, `stop` 서브명령
- Consumes: `N100_SSH_TARGET`, `N100_WSL_DISTRO`, `N100_REMOTE_REPO`

- [ ] **Step 1: Write failing tests**

```python
def test_keygen_requires_private_key_mode_0600():
    result = run_script("keygen", environment={"HOME": temp_home})
    assert result.returncode == 0
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"

def test_start_rejects_non_regular_or_large_task_file():
    result = run_script("start", "--task-file", directory_path)
    assert result.returncode != 0
```

- [ ] **Step 2: Run tests and verify they fail because the command is absent**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v`

- [ ] **Step 3: Implement minimal host command**

```bash
case "$command" in
  keygen) create_key_if_missing ;;
  start) validate_task_file; send_task; run_remote start ;;
  preflight|status|logs|stop) run_remote "$command" ;;
esac
```

- [ ] **Step 4: Run the host tests and verify pass**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts/n100-remote-dev.sh tests/test_n100_remote_dev.py
git commit -m "feat: N100 원격 개발 제어 명령 추가"
```

### Task 2: WSL 지속 실행기

**Files:**
- Create: `scripts/n100-remote-dev-remote.sh`
- Modify: `tests/test_n100_remote_dev.py`

**Interfaces:**
- Consumes: `preflight|start|status|logs|stop`, 고정 `~/.local/state/personal-server/n100-dev`
- Produces: `n100_remote_dev=PASS|FAIL`, tmux 세션 `personal-server-codex-dev`

- [ ] **Step 1: Write failing tests**

```python
def test_remote_start_contains_fixed_safe_codex_prompt():
    text = remote_script_text()
    assert "Do not push, create a pull request, merge, deploy" in text

def test_remote_stop_only_targets_fixed_tmux_session():
    assert "tmux kill-session -t \"$SESSION_NAME\"" in remote_script_text()
```

- [ ] **Step 2: Run test and verify it fails because the remote script is absent**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v`

- [ ] **Step 3: Implement fixed remote command dispatcher**

```bash
case "$command" in
  preflight) require git; require tmux; require codex; require gh ;;
  start) reject_existing_session; tmux new-session -d -s "$SESSION_NAME" "$fixed_command" ;;
  status|logs|stop) fixed_status_or_log_or_stop ;;
esac
```

- [ ] **Step 4: Run test and shell syntax checks**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v && bash -n scripts/n100-remote-dev.sh scripts/n100-remote-dev-remote.sh`

- [ ] **Step 5: Commit**

```bash
git add scripts/n100-remote-dev-remote.sh tests/test_n100_remote_dev.py
git commit -m "feat: N100 지속형 Codex 실행기 추가"
```

### Task 3: 운영 문서와 실제 연결 사전점검

**Files:**
- Create: `docs/n100-remote-development.md`
- Modify: `infra/k8s/README.md`
- Modify: `tests/test_n100_remote_dev.py`

**Interfaces:**
- Documents: 키를 Windows `administrators_authorized_keys`에 한 번 등록한 뒤 `preflight`와 `start`를 수행하는 절차

- [ ] **Step 1: Write failing documentation contract test**

```python
def test_documentation_never_requests_stored_passwords():
    assert "비밀번호를 파일" not in documentation_text()
    assert "rclone" not in documentation_text()
```

- [ ] **Step 2: Run test and verify it fails because the documentation is absent**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v`

- [ ] **Step 3: Write concise bootstrap and recovery documentation**

```markdown
1. `keygen`으로 공개키를 생성함.
2. Windows 관리자 PowerShell에 공개키를 한 번 등록함.
3. `preflight`가 모두 PASS인지 확인함.
4. `start --task-file`로 작업을 시작하고 `status`/`logs`로 재접속함.
```

- [ ] **Step 4: Run focused tests, syntax and change harness**

Run: `python3 -m unittest tests/test_n100_remote_dev.py -v && bash -n scripts/n100-remote-dev*.sh`

- [ ] **Step 5: Commit**

```bash
git add docs/n100-remote-development.md infra/k8s/README.md tests/test_n100_remote_dev.py
git commit -m "docs: N100 원격 개발 환경 절차 추가"
```
