# Loki Observability Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 운영 관측 스택과 분리된 N100 K3s Loki 로그 관측 실습 환경을 추가함.

**Architecture:** `observability-lab` namespace에 단일 Loki·전용 PVC·비민감 샘플 앱·Kubernetes API 기반 Alloy 수집기를 배치함. Grafana는 `monitoring` namespace의 선언형 data source·dashboard ConfigMap만 통해 내부 Loki Service를 읽음. 전용 도구가 server dry-run, 준비 상태, allowlist 로그 조회, 안전한 롤백을 담당함.

**Tech Stack:** Kubernetes, Grafana Loki 3.7.0, Grafana Alloy, Grafana provisioning ConfigMap, Bash, Python unittest, PyYAML.

**Spec:** `docs/superpowers/specs/2026-10-01-loki-ansible-lab-design.md`

## Global Constraints

### 실행 상태 — 2026-10-04 문서 대조

기준 커밋 `52f0e2a`. 아래 원래 실행 절차의 체크박스는 최초 계획이며, 실제 완료 판정은 이 표를 기준으로 함. 검사 증적은 해당 단계 실행에 한정되며 최종 브랜치 전체 통과를 의미하지 않음.

| 단계 | 구현·검토 상태 | 검증 근거·남은 조건 |
|---|---|---|
| Task 1 CI 등록 | 완료 기록 있음 (`854ce53`, `b5d92e8`) | 관련 검사 67건 및 독립 검토 통과. 기존 `infra/k8s/` 분류를 재사용해 좁은 prefix는 추가하지 않음 |
| Task 2 manifest | 보안 보완·재검토 완료 기록 있음 (`3740d09`, `a147e8c`) | namespace·RBAC·NetworkPolicy·보안 계약 통과. local-path 1Gi는 hard quota 아님 |
| Task 3 Grafana | 구현·독립 검토 완료 기록 있음 (`838af2a`) | 직접 계약 16건, maintenance 528건, K8s 812건 통과 기록. Helm chart 렌더 미검증 |
| Task 4 전용 도구 | 구현·독립 검토 완료 기록 있음 (`5d13aad`, `2a0a4fe`) | 도구 11건, K8s 823건, maintenance 528건 통과 기록. manifest별 dry-run 순서 테스트 보강 권고 있음 |
| Task 5 최종 통합 | 미완료 | Ansible CI 통합과 함께 최종 하네스·전체 검사·PR·CI·병합 수행 필요 |
| N100 적용 | 미수행 | 실제 CNI·Grafana label·로그 조회·디스크 보호 확인 및 운영 적용 승인 필요 |

실행기 묶음 옵션은 `--suite`임. 아래 최초 계획의 `--group` 표기는 실제 실행 시 `--suite`로 교정함. 면접 제출 설명은 [DevOps 포트폴리오](../../portfolio-devops.md)를 따름.

- 기존 `monitoring` Helm release, `monitoring-install.sh`, `monitoring-uninstall.sh`를 Loki 설치·제거에 사용하지 않음.
- Loki·Alloy·샘플 앱은 non-root, read-only root filesystem, capability drop, privilege escalation 금지, request/limit을 사용함.
- 로그 수집 대상은 `observability-lab`의 allowlist 샘플 Pod로 한정하고 hostPath, Docker socket, Secret, ClusterRole, cluster-admin을 사용하지 않음.
- Grafana data source는 `grafana_datasource: "1"` label과 내부 ClusterIP URL만 사용하며 인증 정보·외부 URL을 포함하지 않음.
- 기본 rollback은 workload·data source·dashboard만 제거하고 Loki PVC는 보존함. 데이터 삭제는 별도 명시 flag로만 허용함.
- 실제 N100 적용은 저장소 검증·PR 병합 범위 밖이며 별도 운영 승인 전까지 수행하지 않음.

## Review Focus

- Collector Role이 `observability-lab` 외 namespace 또는 `pods`·`pods/log` 외 리소스에 접근하지 않는지 확인함.
- 외부 노출 Service type, Ingress, NodePort, 무제한 보존·저장소·자원이 없는지 확인함.
- Grafana data source가 chart 88.6.1 sidecar label 계약을 지키고 Prometheus data source를 대체하지 않는지 확인함.
- rollback이 기존 `monitoring` release·PVC 또는 운영 namespace 자원을 삭제하지 않는지 확인함.

### Task 1: CI 분류와 Loki contract test 등록

**Files:** Modify `scripts/verify_change_scope.py`, `tests/test_verify_change_scope.py`, `tests/ci_test_matrix.json`, `tests/test_run_service_tests.py`; create `tests/test_k8s_observability_lab.py`, `tests/test_k8s_observability_lab_tools.py`.

**Produces:** `infra/k8s/observability-lab/` 변경을 infrastructure·maintenance로 분류하고, 두 contract test가 `k8s-contracts` group에 정확히 한 번 등록됨.

- [ ] 새 path가 unclassified가 아니고 `maintenance`를 요구하는 실패 test를 작성함.
- [ ] `INFRASTRUCTURE_PREFIXES`에 `infra/k8s/observability-lab/`를 추가함.
- [ ] CI matrix와 `test_run_service_tests.py`의 `k8s-contracts` 명령에 두 test module을 한 번씩 동기화함.
- [ ] Run: `python3 -m unittest tests.test_verify_change_scope tests.test_run_service_tests -v`. Expected: PASS.

### Task 2: 분리 Loki·Alloy·샘플 앱 manifest와 테스트

**Files:** Create `infra/k8s/observability-lab/loki-lab.yaml`; modify `tests/test_k8s_observability_lab.py`.

**Produces:** `observability-lab`, `loki-lab`, `loki-lab-data`, `loki-lab-alloy`, `loki-lab-sample` 고정 이름의 Namespace, Loki ConfigMap/PVC/Deployment/ClusterIP Service, Alloy ConfigMap/ServiceAccount/Role/RoleBinding/Deployment, 비민감 샘플 Deployment.

- [ ] YAML document 기반 실패 test를 먼저 작성함: namespace, Loki PVC 하나, ClusterIP, Loki 3.7.0, 짧은 retention, request/limit, non-root, hostPath·Secret 부재, Alloy Role의 namespace 한정 `pods`·`pods/log` 읽기, allowlist label을 검증함.
- [ ] Run: `python3 -m unittest tests.test_k8s_observability_lab -v`. Expected: manifest 부재로 FAIL.
- [ ] filesystem TSDB·1Gi 이하 PVC·짧은 retention의 single-binary Loki와 `discovery.kubernetes`, `loki.source.kubernetes`, `loki.write`를 사용한 Alloy를 구현함. Alloy는 `app.kubernetes.io/name=loki-lab-sample`만 수집함.
- [ ] 모든 workload에 security context, resource request/limit, 필요한 `emptyDir`만 선언함.
- [ ] Run: `python3 -m unittest tests.test_k8s_observability_lab -v`. Expected: PASS.

### Task 3: Grafana data source·dashboard 선언과 chart 계약

**Files:** Modify `infra/k8s/monitoring/values.n100.yaml`, `tests/test_k8s_monitoring_values.py`, `tests/test_k8s_grafana_portal_http_dashboard.py`, `tests/test_k8s_observability_lab.py`; create `infra/k8s/monitoring/loki-lab-datasource.yaml`, `infra/k8s/monitoring/loki-lab-dashboard.yaml`.

**Produces:** Grafana sidecar datasource setting, `Loki Lab` provisioned data source, allowlist sample-app LogQL panel.

- [ ] Grafana chart 88.6.1 contract에 맞는 실패 test를 작성함: `grafana.sidecar.datasources.enabled=true`, `grafana_datasource: "1"`, Loki data source의 internal URL, `grafana_dashboard: "1"` 및 fixed sample LogQL selector.
- [ ] `values.n100.yaml`에 chart sidecar datasource setting을 명시하고, 기존 Prometheus datasource·Portal dashboard·Grafana service/ingress를 그대로 둠.
- [ ] data source와 dashboard ConfigMap을 `monitoring` namespace에 작성함.
- [ ] Run: `helm template personal-server-monitoring prometheus-community/kube-prometheus-stack --version 88.6.1 --namespace monitoring --values infra/k8s/monitoring/values.n100.yaml >/tmp/monitoring-rendered.yaml && python3 -m unittest tests.test_k8s_monitoring_values tests.test_k8s_grafana_portal_http_dashboard tests.test_k8s_observability_lab -v`. Expected: PASS.

### Task 4: Loki lab 전용 apply·verify·rollback 도구

**Files:** Create `infra/k8s/tools/observability-lab.sh`; modify `tests/test_k8s_observability_lab_tools.py`, `infra/k8s/README.md`, `docs/operations-reference.md`.

**Produces:** `--check|--apply|--verify|--rollback [--delete-data]` mode와 `observability_lab=PASS|FAIL` marker.

- [ ] stubbed command test로 기본 인자 실패, check 읽기 전용, apply의 server dry-run 우선, verify의 Loki `/ready`·fixed selector 조회, rollback의 named resource 한정, `--delete-data`에서만 named Loki PVC 제거를 정의함.
- [ ] tool은 namespace·Grafana Deployment·storageclass를 preflight하고 apply 전 server dry-run을 수행함. Helm install/uninstall 또는 existing monitoring PVC를 호출하지 않음.
- [ ] 기본 rollback은 lab workload와 두 named Grafana ConfigMap만 제거하고 PVC는 유지함.
- [ ] Run: `python3 -m unittest tests.test_k8s_observability_lab_tools tests.test_documentation_index tests.test_documentation_links -v`. Expected: PASS.

### Task 5: 통합 검증·독립 검토·PR

**Files:** Modify `docs/20260921_프로젝트보완_개발계획.md`.

- [ ] Run preflight: `git diff --name-status -z --find-renames origin/main HEAD > /tmp/loki-lab-changes.nul && python3 scripts/run_change_harness.py --input /tmp/loki-lab-changes.nul --input-format git-name-status-z --agent-context`.
- [ ] Run: `python3 tests/run_service_tests.py --group k8s-contracts && python3 tests/run_service_tests.py --group maintenance && git diff --check`. Expected: PASS.
- [ ] 독립 검토에서 namespace/RBAC, no hostPath·Secret·external exposure, Grafana render, named rollback, existing monitoring non-regression, N100 approval separation을 확인함.
- [ ] Run result harness with `--check-result k8s-contracts=success --check-result maintenance=success`. Expected: `ready_for_review`.
- [ ] 기능 브랜치를 push하고 PR CI·Agent Review·Trivy 성공 후 병합함. 실제 N100 적용은 별도 승인 대상으로 남김.
