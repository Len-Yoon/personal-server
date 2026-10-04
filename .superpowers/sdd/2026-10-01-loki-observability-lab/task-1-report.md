# Task 1 검증 보고

## 문서 정보

| 항목 | 내용 |
|---|---|
| 작업 | Loki 관측성 실습 CI 범위 분류 및 계약 테스트 등록 |
| 기준 브랜치 | `codex/loki-ansible-lab` |
| 실행 환경 | macOS 로컬 저장소, Python 3.11/3.12, Codex 제한 샌드박스 |
| 구현 커밋 | `854ce53` (`테스트: Loki 관측성 실습 CI 계약 등록`) |
| 운영 적용 | 미수행 |

## 핵심 요약

- 새 실습 경로는 기존 `infra/k8s/` 접두어를 통해 이미 `infrastructure_files`로 분류되고 `maintenance` 검사를 요구함. 중복 접두어 추가는 주 담당의 검토 결정에 따라 제외함.
- 두 계약 테스트 모듈을 기존 `k8s-contracts` 그룹에 각각 한 번 등록함. CI 그룹 수는 9개로 유지됨.
- 계약 테스트 모듈의 실제 검사는 후속 Task 2 구현 대상이며, 이 작업에서는 import 가능한 파일만 생성함.

## RED → GREEN 증거

| 단계 | 명령 | 결과 | 비고 |
|---|---|---|---|
| 분류 확인 | `python3 -m unittest tests.test_verify_change_scope tests.test_run_service_tests -v` | PASS | 기존 상위 접두어로 이미 충족됨. 이 분류 테스트는 RED 근거로 사용하지 않음. |
| RED | `python3 -m unittest tests.test_run_service_tests.ServiceTestRunnerTests.test_observability_lab_contracts_are_registered_once_in_k8s_group -v` | FAIL | `AssertionError: Lists differ: [] != ['k8s-contracts']`로 두 신규 모듈의 미등록 확인함. |
| GREEN | `python3 -m unittest tests.test_verify_change_scope tests.test_run_service_tests -v` | PASS, 67 tests | 실제 분류와 그룹 등록·중복 방지·기존 매트릭스 계약 확인함. |

초기에는 전용 접두어 존재 자체를 확인하는 테스트가 RED였으나 기존 동작을 중복 설정하는 조건임을 확인하고 삭제함. 최종 RED는 CI 등록의 사용자 관찰 가능 결과를 대상으로 함.

## 변경 파일

| 파일 | 변경 내용 | 비고 |
|---|---|---|
| `tests/test_verify_change_scope.py` | observability-lab 경로의 infrastructure·maintenance 분류 회귀 테스트 추가 | 기존 상위 접두어 사용 |
| `tests/test_run_service_tests.py` | 두 모듈의 `k8s-contracts` 단일 등록 테스트 및 기대 명령 동기화 | 9그룹 유지 |
| `tests/ci_test_matrix.json` | `k8s-contracts` 명령에 두 모듈 추가 | 각각 1회 |
| `tests/test_k8s_observability_lab.py` | 후속 계약 테스트용 모듈 생성 | Task 2에서 실제 검사 구현 필요 |
| `tests/test_k8s_observability_lab_tools.py` | 후속 도구 계약 테스트용 모듈 생성 | Task 2에서 실제 검사 구현 필요 |

`scripts/verify_change_scope.py`는 기능이 이미 충족되어 변경하지 않음. N100·운영 서비스·배포 설정 변경 없음.

## 검토 결과

| 검사 | 결과 | 근거 |
|---|---|---|
| 지정 회귀 테스트 | PASS | 67 tests |
| CI 매트릭스 생성·전체 테스트 파일 커버리지 | PASS | `python3 tests/run_service_tests.py --github-matrix` exit 0; `test_matrix_coverage_matches_every_repository_test_file_exactly_once` PASS |
| 변경 범위 하네스 | PASS | `python3 scripts/run_change_harness.py --input .superpowers/sdd/2026-10-01-loki-observability-lab/task-1-paths.txt --check-result maintenance=success --agent-context` → `ready_for_review` |
| 공백 검사 | PASS | `git diff --check` |
| 전체 9그룹 로컬 실행 | 미완료 | `python3 tests/run_service_tests.py` 실행 중 관련 없는 루프백 서버 테스트의 샌드박스 `PermissionError` 확인 후 주 담당 지시에 따라 중단함. |

전체 실행에서 `tests.test_portal_dashboard.PortalDashboardTests.test_search_deadline_interrupts_real_http_drip_body`와 `tests/car_care_worker/test_oauth_callback.py`의 네 테스트(`test_callback_forwards_code_and_state_without_rendering_them`, `test_callback_rejects_missing_parameters`, `test_health_is_available_only_on_the_loopback_callback_server`, `test_unknown_path_is_not_exposed`)가 `ThreadingHTTPServer(("127.0.0.1", 0), ...)` 바인딩 시 `PermissionError: [Errno 1] Operation not permitted`로 실패함. Car Care 묶음을 동일 격리 환경에서 재실행해 67개 중 이 네 건만 동일하게 실패함. 두 테스트 파일과 소켓 권한은 이번 변경 범위 밖이며, 전체 실행은 k8s-contracts/maintenance 그룹 완료 전에 중단되어 해당 그룹의 전체 결과는 확인 필요.

## 확인 필요 사항 및 후속 조치

- Task 2에서 두 신규 계약 테스트 모듈에 실제 매니페스트·도구 검증을 구현하고 같은 그룹을 다시 실행할 필요 있음.
- CI의 정상 네트워크 권한 환경에서 전체 그룹 결과를 최종 확인할 필요 있음.
- 독립 검토 결과는 주 담당에게 인계 후 확정 필요.
