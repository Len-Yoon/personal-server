# Helm 상태 version 호환성 수정 보고서

## 변경 내용

| 항목 | 내용 |
|---|---|
| 원인 | 실제 Helm 상태 JSON이 `info.version`에 숫자 `3`을 반환할 수 있으나 설치 도구가 `info.revision`만 요구하여 기존 릴레이 리소스 생성 후 중단됨 |
| 수정 | 배포 상태를 먼저 확인한 뒤 숫자형 `revision`을 우선 사용하고, 없을 때 숫자형 `version`을 이전 revision으로 단 한 번 대체하도록 수정함 |
| 실패 방식 | 배포 상태가 아니거나 두 필드 모두 숫자가 아니면 기존과 같이 fail-closed 처리함 |
| 범위 | 설치 도구의 Helm 상태 캡처 및 해당 통합 회귀 테스트로 제한함 |

## TDD 검증

| 단계 | 결과 |
|---|---|
| RED | `version: 3` Helm 상태 stub에서 rollback 호출이 없어 테스트 실패 확인함 |
| GREEN | `version: 3`을 이전 revision `3`으로 캡처하도록 수정 후 테스트 통과함 |

## 검증 결과

| 검사 | 결과 |
|---|---|
| 대상 회귀 테스트 2건 | 통과 (`Ran 2 tests`, `OK`) |
| Bash 구문 검사 | 통과 (`bash -n infra/k8s/tools/sre-telegram-install.sh`) |
| diff 공백 검사 | 통과 (`git diff --check`) |

## 변경 파일

- `infra/k8s/tools/sre-telegram-install.sh`
- `tests/test_k8s_sre_telegram_tools.py`
- `.superpowers/sdd/helm-status-version-fix-report.md`

## 확인 필요 사항

- N100 실환경 배포 및 네트워크 연동 검증은 요청 범위에서 제외하여 수행하지 않음.
- 서버 기동 및 스케줄러 영역은 변경하지 않음.
- 하위 에이전트는 사용하지 않음. 상위 작업에서 지정한 단일 구현 범위를 직접 수행함.

## 검토 반영

독립 검토에서 확인된 느슨한 `version` 키 매칭을 수정함. Helm JSON의 최상위 `info` 객체만 별도로 추출한 뒤 해당 객체의 `status`, `revision`, `version`만 검사하도록 제한함. `chart.metadata.version`만 존재하는 상태는 rollback revision을 만들지 않고 fail-closed 처리함.

추가 회귀 테스트는 deployed 상태와 중첩 `chart.metadata.version`만 포함한 Helm status stub을 사용하며, `helm rollback` 미호출을 검증함.

추가 검증에서 해당 회귀 테스트·기존 revision/version 테스트 3건은 통과함. 전체 35건 일괄 실행은 기존 apply 테스트 9건이 이미지 import 단계에서 간헐적으로 실패하여 통과하지 못함. 실패 로그상 Helm 상태 파싱 이전의 `sudo k3s ctr -n k8s.io images import -` 단계에서 중단되며, 개별 재실행한 변경 영향 테스트는 통과함. 전체 suite 결과는 확인 필요로 유지함.
