# Loki 관측성 실습 Task 4 구현·검증 보고

## 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-02 | 서울 기준 |
| 기준 커밋 | `838af2a` | Task 3 완료 시점 |
| 구현 커밋 | `5d13aad` | `기능: Loki 실습 운영 도구와 안전 롤백 검증 추가` |
| 변경 파일 | `infra/k8s/tools/observability-lab.sh`, `tests/test_k8s_observability_lab_tools.py`, `infra/k8s/README.md`, `docs/operations-reference.md` | 구현 커밋의 4개 파일 |
| 실행 환경 | 맥 로컬 저장소 | stub으로 외부 명령 대체; 실제 K3s 접속 없음 |
| 제외 범위 | N100 운영 적용, 실제 `kubectl` 실행, push·PR·병합 | 본 Task에서 미수행 |

## 핵심 요약

전용 도구에 `--check`, `--apply`, `--verify`, `--rollback [--delete-data]` 모드와 `observability_lab=PASS|FAIL` 결과를 추가함. 모든 클러스터 접근 전에 지정한 현재 Kubernetes context와 실제 context의 일치를 확인함. 기본 롤백은 고정 이름의 실습 자원만 삭제하며 전용 PVC와 namespace를 보존함.

## 상세 검증 결과

| 구분 | 명령·근거 | 결과 | 비고 |
|---|---|---|---|
| RED | `python3 -m unittest tests.test_k8s_observability_lab_tools -v` | 도구 파일 부재로 9개 중 7개 예상 실패 확인 | 구현 전 실행함 |
| GREEN | 동일 명령 | 11건 통과 | stubbed `sudo k3s kubectl`로 명령 순서·대상·실패 경로 확인함 |
| 문서·도구 계약 | `python3 -m unittest tests.test_k8s_observability_lab_tools tests.test_documentation_index tests.test_documentation_links -v` | 40건 통과 | 문서 링크와 기존 내용 계약 포함함 |
| K3s 계약 묶음 | `python3 tests/run_service_tests.py --suite k8s-contracts` | 823건 통과, 641.625초 | 로컬 실행; 실제 클러스터 적용 검증 아님 |
| Maintenance 묶음 | `python3 tests/run_service_tests.py --suite maintenance` | 528건 통과, 166.213초 | 로컬 실행 |
| 셸 구문 | `bash -n infra/k8s/tools/observability-lab.sh` | 통과 | 구문 검사 |
| 변경 범위 하네스 | `python3 scripts/run_change_harness.py --input .superpowers/sdd/2026-10-01-loki-observability-lab/task-4-change-paths.txt --agent-context --check-result maintenance=success` | `ready_for_review` | 최초 실행은 필수 maintenance 결과 미기록으로 `verification_incomplete`였음 |
| 변경 diff | `git diff --check`, `git diff HEAD^ HEAD --check` | 통과 | 공백 오류 없음 |

계획서의 `run_service_tests.py --group`는 현재 실행기에서 지원하지 않아 인자 오류(exit 2)로 종료됨. 실제 지원 옵션 `--suite`로 K3s 계약·maintenance 묶음을 실행해 모두 통과함.

## 검토 결과

| 대상 | 확인 내용 | 결과 |
|---|---|---|
| apply | 첫 namespace 생성에 server dry-run 선행; 각 manifest에 server dry-run 후 적용 | stub 명령 순서로 확인함 |
| verify | 세 Deployment Available, 전용 PVC Bound, 두 Grafana ConfigMap, 내부 Loki `/ready`, 고정 샘플 selector 조회 | stub 응답으로 확인함. 로그 본문은 출력하지 않음 |
| rollback | 고정된 실습 Deployment·설정·Grafana ConfigMap만 삭제; namespace와 PVC 기본 보존 | stub 명령 목록으로 확인함 |
| 데이터 삭제 | `--rollback --delete-data`에서만 `observability-lab/loki-lab-data` PVC 삭제 | stub 명령 목록으로 확인함 |
| 운영 보호 | monitoring 설치·제거 도구와 기존 monitoring PVC를 호출하지 않음 | stub 명령 목록과 구현 diff로 확인함 |

## 확인 필요 사항

- 실제 N100의 context·StorageClass·가용 메모리·디스크·네트워크 정책 동작, Loki·Alloy 로그 조회는 운영 사전 점검 및 별도 배포 승인 뒤 확인 필요함.
- 첫 namespace 생성 후 후속 manifest 적용 실패 시 namespace가 남을 수 있음. 실제 상태를 조회한 뒤 조치 필요함.
- 이 보고서의 통과 결과는 로컬 테스트 결과임. PR CI, 독립 검토 최종 판정, N100 운영 상태는 별도로 확인 필요함.

## 후속 조치

1. 독립 검토에서 고정 리소스 경계, context 검사, named rollback과 남은 운영 위험을 확인 필요함.
2. 저장소 통합 검증·PR CI 완료 후 운영 대상과 실행 순서를 확정하고, 실제 N100 적용 직전 별도 승인을 확인 필요함.
