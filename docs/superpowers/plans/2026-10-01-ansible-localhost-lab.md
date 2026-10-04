# Ansible Localhost Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** N100 WSL 한 대에서 운영 서비스와 분리된 loopback Compose 앱을 Ansible로 멱등 배치·검증·롤백하는 실습을 추가함.

**Architecture:** `infra/ansible-lab`은 localhost inventory, collection dependency, 전용 Compose template, deploy·rollback playbook을 보관함. Ansible은 user-owned lab directory와 project name만 다루고 sample app은 `127.0.0.1`에만 바인딩됨. 실행 전 collection·Docker Compose·포트 충돌을 fail-closed preflight로 확인함.

**Tech Stack:** Ansible Core, `community.docker` collection, Docker Compose v2, Nginx sample container, YAML, Python unittest.

**Spec:** `docs/superpowers/specs/2026-10-01-loki-ansible-lab-design.md`

## Global Constraints

### 실행 상태 — 2026-10-04 문서 대조

기준 커밋 `f66a42a` ([PR #331](https://github.com/Len-Yoon/personal-server/pull/331) 병합). 아래 원래 실행 절차의 체크박스는 최초 계획이며, 실제 완료 판정은 이 표를 기준으로 함.

| 단계 | 구현·검토 상태 | 검증 근거·남은 조건 |
|---|---|---|
| Task 1 기본 구조 | 구현·독립 검토 완료 기록 있음 (`cb2f437`) | localhost·loopback·권한 경계 계약 5건 통과 기록 |
| Task 2 lifecycle | 구현·최종 독립 검토 완료 | 파일·Compose 소유·고정 변수·이름 충돌·양방향 조회 계약을 보완하고 독립 검토 승인됨 |
| Task 2 계약 증명 | 보완·검증 완료 (`3d866df`) | 정상 label로 동수·상이름 사례를 구성하고 count·sort·label 결과를 개별 평가함. site·rollback 조건 우회 변이 20/20 검출함 |
| Task 2 실동작 | 실제 리허설 완료 | syntax-check 2개·check mode 통과. 최초 changed=5·2회차 changed=0·rollback changed=2·재배치 changed=5, 모두 failed=0 |
| Task 3 CI 통합 | 완료 | `infra/ansible-lab/` infrastructure·maintenance 분류 및 계약 테스트 1회 등록 완료. maintenance 546건, PR CI·Trivy·독립 검토 통과 후 `f66a42a` 병합됨 |
| N100 적용 | 승인된 리허설 완료 | 동일 커밋 동기화와 재배치 후 HTTP 정상. [운영 검증 결과](../../reviews/20261004_Loki_Ansible_실습_N100운영검증결과.md) 참조 |

실행기 묶음 옵션은 `--suite`임. 아래 최초 계획의 `--group` 표기는 실제 실행 시 `--suite`로 교정함. 면접에서는 저장소 검증 완료와 실제 멱등성·롤백 리허설 결과를 구분함. [DevOps 포트폴리오](../../portfolio-devops.md)를 참조함.

- inventory는 `localhost ansible_connection=local` 하나만 포함하고 SSH, Windows, remote host, `become`을 사용하지 않음.
- sample Compose project·user-home-relative lab directory·loopback port만 관리함.
- 운영 Compose·K3s·PVC·Secret·Caddy·Tunnel·bootstrap·scheduler·기존 deploy/backup 도구를 수정·호출하지 않음.
- `community.docker` collection·Docker Compose v2는 확인만 하고 자동 설치하지 않음.
- 변수·template·문서·test output에 비밀번호, token, actual account name, internal address를 기록하지 않음.
- rollback은 lab project와 lab directory만 대상으로 명시 실행함.

## Review Focus

- inventory/playbook에 localhost 외 대상, `become`, SSH key, 실제 사용자명·비밀값이 없는지 확인함.
- Compose port가 `127.0.0.1` only이고 public ingress·network·host mount가 없는지 확인함.
- deploy가 preflight 없이 실행되거나 collection을 자동 설치하지 않는지 확인함.
- 두 번째 apply의 change result와 rollback resource scope가 정직하게 검증되는지 확인함.

### Task 1: Ansible lab 구조와 static safety contract

**Files:** Create `infra/ansible-lab/ansible.cfg`, `infra/ansible-lab/inventory/localhost.ini`, `infra/ansible-lab/group_vars/all.yml`, `infra/ansible-lab/collections/requirements.yml`, `infra/ansible-lab/playbooks/site.yml`, `infra/ansible-lab/playbooks/rollback.yml`, `infra/ansible-lab/templates/compose.yaml.j2`, `infra/ansible-lab/templates/index.html.j2`, `tests/test_ansible_lab_contract.py`.

**Produces:** fixed `personal-server-ansible-lab` project name, user-home-relative lab directory, loopback-only sample HTTP service, `preflight|deploy|verify|rollback` tags.

- [ ] YAML/INI static failure test를 먼저 작성함: one localhost inventory, `become: false`, fixed project name, `127.0.0.1` binding, resource limits, no privileged/host mount/secret-like variable/operational Compose path를 확인함.
- [ ] Run: `python3 -m unittest tests.test_ansible_lab_contract -v`. Expected: file absence FAIL.
- [ ] Ansible config·inventory·collection requirement·group vars·Compose and fixed response templates를 구현함. Compose에는 pinned sample image, read-only filesystem, dropped capabilities, cpu/memory/pid limits만 포함함.
- [ ] Run: `python3 -m unittest tests.test_ansible_lab_contract -v`. Expected: PASS.

### Task 2: preflight·deploy·verify·rollback idempotency

**Files:** Modify `infra/ansible-lab/playbooks/site.yml`, `infra/ansible-lab/playbooks/rollback.yml`, `tests/test_ansible_lab_contract.py`; create `infra/ansible-lab/README.md`.

**Produces:** preflight→deploy→verify and explicit rollback with an honest local evidence format.

- [ ] 실패 test로 deploy 전 `ansible-galaxy collection list community.docker`, `docker compose version`, port/directory conflict preflight, `community.docker.docker_compose_v2` lifecycle, fixed project·directory only rollback을 정의함.
- [ ] preflight failure does not start the container; template/file task reports real state; `uri` health check only calls loopback URL; rollback uses project `absent` and lab directory `absent` only.
- [ ] Run: `python3 -m unittest tests.test_ansible_lab_contract -v && ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml --syntax-check && ansible-playbook -i infra/ansible-lab/inventory/localhost.ini infra/ansible-lab/playbooks/site.yml --check --diff`. Expected: static/syntax PASS and check mode does not start container.
- [ ] Run rehearsal twice then rollback: deploy/verify, deploy/verify, rollback. Expected: second apply reports zero changes and rollback removes lab only. If collection/image is unavailable, record runtime rehearsal as unverified; do not install it automatically.

### Task 3: CI scope·documentation·integrated verification

**Files:** Modify `scripts/verify_change_scope.py`, `tests/test_verify_change_scope.py`, `tests/ci_test_matrix.json`, `tests/test_run_service_tests.py`, `docs/operations-reference.md`, `docs/20260921_프로젝트보완_개발계획.md`.

**Produces:** `infra/ansible-lab/` is infrastructure requiring maintenance, and its contract test runs exactly once in existing maintenance group.

- [ ] `infra/ansible-lab/playbooks/site.yml` classification failure test를 작성하고 infrastructure prefix에 lab path를 추가함.
- [ ] maintenance test command과 `test_run_service_tests.py` contract를 동기화함.
- [ ] Run: `python3 -m unittest tests.test_ansible_lab_contract tests.test_verify_change_scope tests.test_run_service_tests tests.test_documentation_index tests.test_documentation_links -v && python3 tests/run_service_tests.py --group k8s-contracts && python3 tests/run_service_tests.py --group maintenance && git diff --check`. Expected: PASS.
- [ ] Run change harness, independent operational review, then result harness with `k8s-contracts=success` and `maintenance=success`. Expected: `ready_for_review`.
- [ ] Existing branch에 PR을 만들고 CI·Agent Review·Trivy 성공 후 병합함. N100 lab application remains a separate explicit operational approval.
