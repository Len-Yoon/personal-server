# Loki 관측성 실습 Task 2 검증 결과

## 문서 정보

| 항목 | 내용 |
|---|---|
| 작성일 | 2026-10-01 15:36 KST |
| 기준 커밋 | `b5d92e8` |
| 변경 범위 | `observability-lab` 매니페스트 및 계약 테스트 |
| 제외 범위 | 기존 monitoring Helm release·PVC, N100 운영 적용, kubectl 실행, push·PR |

## 핵심 요약

전용 namespace에 Loki 단일 인스턴스, 전용 1Gi PVC, 동일 namespace 샘플 Pod만 조회하는 Alloy 수집기와 고정 비민감 로그 샘플 앱을 선언함. 독립 보안 검토 후 Loki ingress를 Alloy·Grafana Pod의 TCP 3100으로 제한함. 기존 CI 9그룹 정의는 수정하지 않음.

## 검토 결과

| 구분 | 명령·근거 | 결과 | 비고 |
|---|---|---|---|
| RED | `python3 -m unittest tests.test_k8s_observability_lab -v` | 예상 실패, 5개 테스트에서 manifest 부재 실패 8건 | 구현 전 실행함 |
| GREEN | 동일 명령 | 5건 통과 | YAML 문서 기반 계약 검사함 |
| 관련 회귀 | `python3 -m unittest tests.test_k8s_observability_lab tests.test_run_service_tests -v` | 32건 통과 | CI 9그룹·중복 등록 계약 포함함 |
| maintenance | `python3 tests/run_service_tests.py --suite maintenance` | 528건 통과 | CLI의 실제 인자는 `--suite`임. 계획의 `--group` 시도는 인자 오류로 실행되지 않음 |
| 변경 경로 하네스 | `python3 scripts/run_change_harness.py --input .superpowers/sdd/2026-10-01-loki-observability-lab/task-2-paths.txt --agent-context --check-result maintenance=success` | `ready_for_review` | 초기 실행은 maintenance 검증 미기록으로 `verification_incomplete`였음 |
| 공백 검사 | `git diff --check` | 통과 | 변경 파일 최종 staged diff 검사 별도 수행함 |

## 독립 보안 검토 보완

| 구분 | 내용 | 검증 결과 |
|---|---|---|
| ingress | Loki Pod 대상 NetworkPolicy에 동일 namespace Alloy와 `monitoring`의 해당 release Grafana Pod만 TCP 3100 허용함 | 계약 테스트 RED 6건 예상 실패 후 GREEN 6건 통과 |
| egress | ingress 정책만 선언함. Alloy의 DNS·Kubernetes API·Loki 연결을 제한하는 egress 정책은 추가하지 않음 | YAML 계약으로 `policyTypes: [Ingress]` 확인함 |
| 저장소 경계 | PVC 1Gi는 요청 용량이며 local-path 호스트 디스크 quota가 아님을 annotation에 기록함. 24h compactor retention은 비동기이며 디스크 사용 상한이 아님을 기록함 | 계약 테스트 확인함 |
| Pod 보안 | 세 Deployment의 `hostIPC: false`, `privileged: false`를 명시하고 init/ephemeral container 부재와 capability 전체 drop을 검사함 | 계약 테스트 확인함 |

보완 후 `python3 -m unittest tests.test_k8s_observability_lab tests.test_run_service_tests -v` 33건 통과함. `python3 tests/run_service_tests.py --suite maintenance` 528건 통과함(160.352초). `git diff --check` 통과함. 변경 경로 하네스에 `maintenance=success`를 전달한 결과 `ready_for_review`를 확인함.

## 확인 필요 사항

- N100의 실제 가용 메모리·디스크, local-path 저장소 동작, 컨테이너 이미지 가용성, Loki·Alloy 실행 및 로그 조회는 운영 적용 전 사전 점검·별도 승인 후 확인 필요함.
- local-path PVC의 `1Gi` 요청과 24h retention은 실제 디스크 사용 상한이 아님. 운영 적용 전 디스크 여유와 사용량 감시·중단 기준 확인 필요함.
- NetworkPolicy의 실효성, `monitoring` Grafana Pod label 및 Loki query·Alloy push 성공은 K3s에서 확인 필요함.
- 기존 monitoring·서비스 상태와 N100 외부 health는 본 Task에서 미검증함.

## 후속 조치

독립 검토 담당자가 namespace/RBAC, 수집 allowlist, 저장·보존 상한, 금지 자원 부재와 실제 동작 미검증 사항을 확인할 필요가 있음. 본 Task에서 운영 상태 변경은 수행하지 않음.
