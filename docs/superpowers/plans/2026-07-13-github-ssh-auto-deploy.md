# GitHub SSH 자동배포 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** `main` push 시 N100 서버가 최신 코드를 자동 반영하고 Docker Compose 스택을 다시 띄우도록 만든다.

**Architecture:** GitHub Actions가 배포 트리거와 SSH 연결을 담당하고, 저장소에는 서버에서 실행할 배포 스크립트를 둔다. 서버는 기존 Compose 오버레이를 그대로 사용해 `git fetch` 후 `docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build`만 수행한다. 레지스트리와 self-hosted runner는 추가하지 않는다.

**Tech Stack:** GitHub Actions, Bash, SSH, Docker Compose, Python `unittest`.

## Global Constraints

- 배포 트리거는 `main` 브랜치 push로 제한한다.
- 이미지 레지스트리, self-hosted runner, Kubernetes는 사용하지 않는다.
- `server`와 `scheduler` 역할의 기존 Windows/N100 운영 절차는 수정하지 않는다.
- SSH 자격 증명은 GitHub Secrets로만 관리한다.
- `docker-compose.yml`과 `docker-compose.n100.yml`의 현재 운영 구조를 유지한다.

---

### Task 1: 서버 배포 스크립트와 workflow 추가

**Files:**
- Create: `scripts/deploy-n100.sh`
- Create: `.github/workflows/deploy-n100.yml`
- Test: `tests/test_deploy_n100.py`

**Interfaces:**
- `scripts/deploy-n100.sh` runs from the repository root on the N100 server.
- The script accepts `PROJECT_ROOT` as an optional first argument and defaults to the current directory.
- GitHub Actions consumes `N100_SSH_HOST`, `N100_SSH_PORT`, `N100_SSH_USER`, `N100_SSH_KEY`, `N100_SSH_KNOWN_HOSTS`, and `N100_APP_DIR`.

- [ ] **Step 1: Write the failing tests**

Add tests that assert:
- `.github/workflows/deploy-n100.yml` triggers on `push` to `main`.
- The workflow references the SSH secrets and calls `bash scripts/deploy-n100.sh`.
- `scripts/deploy-n100.sh` contains `git fetch --prune origin`, `git reset --hard origin/main`, and `docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.test_deploy_n100`
Expected: FAIL because the workflow and script do not exist yet.

- [ ] **Step 3: Implement the minimal deployment flow**

Create `scripts/deploy-n100.sh` with strict shell options, a configurable project root, `git fetch --prune origin`, `git reset --hard origin/main`, `docker compose -f docker-compose.yml -f docker-compose.n100.yml up -d --build`, and a final `docker compose ps` status check. Create `.github/workflows/deploy-n100.yml` with a `main` push trigger, SSH key setup, host key verification, and a remote command that invokes the deployment script in `N100_APP_DIR`.

- [ ] **Step 4: Run focused tests and verify pass**

Run: `python3 -m unittest tests.test_deploy_n100`
Expected: PASS.

- [ ] **Step 5: Commit the topic**

```bash
git add scripts/deploy-n100.sh .github/workflows/deploy-n100.yml tests/test_deploy_n100.py
git commit -m "feat: N100 SSH 자동배포 추가"
```

### Task 2: 운영 문서 반영

**Files:**
- Create: `docs/n100-github-auto-deploy.md`
- Modify: `README.md`
- Modify: `docs/agent-handoff.md`

**Interfaces:**
- Documentation explains the server prerequisites, required Secrets, and first-time manual setup.
- Documentation does not change the Windows scheduler flow or the Caddy/Cloudflare runtime assumptions.

- [ ] **Step 1: Write the failing documentation checks**

Add a test that asserts the README or dedicated deployment doc mentions `main` push auto-deploy, `N100_SSH_HOST`, and the Compose up command.

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python3 -m unittest tests.test_deploy_n100`
Expected: FAIL until the deployment guide exists.

- [ ] **Step 3: Write the deployment guide**

Document the first-time N100 setup, including cloning the repository, keeping `.env`/`data/` on the server, storing SSH secrets in GitHub, and verifying the auto-deploy flow with one manual `git pull` plus compose run. Add a short README pointer to the new guide and note the workflow in the handoff doc.

- [ ] **Step 4: Run focused tests again**

Run: `python3 -m unittest tests.test_deploy_n100`
Expected: PASS.

- [ ] **Step 5: Commit the topic**

```bash
git add docs/n100-github-auto-deploy.md README.md docs/agent-handoff.md
git commit -m "docs: N100 자동배포 운영 방법 정리"
```

### Task 3: Final validation

**Files:**
- Test: `tests/test_deploy_n100.py`

**Interfaces:**
- The deployment workflow and script remain the single source of truth for auto-deploy behavior.

- [ ] **Step 1: Run the relevant test suite**

Run: `python3 -m unittest tests.test_compose_config tests.test_windows_bootstrap tests.test_deploy_n100`
Expected: PASS with no unrelated failures.

- [ ] **Step 2: Check the git diff for scope**

Confirm only deployment-related files changed and no server/scheduler code paths were modified.

- [ ] **Step 3: Stop after verification**

Do not expand scope into image publishing, Kubernetes manifests, or runner installation.
