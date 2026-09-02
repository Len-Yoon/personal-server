# K3s Telegram SRE 알림 최종 수정 보고서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | K3s Telegram SRE 알림 최종 수정 보고서 |
| 작성일 | 2026-09-02 |
| 기준 자료 | 최종 검토 문서, 설계서, 구현 계획 |
| 작업 커밋 | `01e3cb2` |
| 작업 범위 | 최종 검토 P1 3건 및 P2 5건 수정 |
| 비고 | N100, Helm apply, 실 Secret, 네트워크, Telegram 실행하지 않음 |

## 핵심 요약

- 최종 검토에서 지적된 P1 3건과 P2 5건을 모두 코드·매니페스트·운영 도구·회귀 테스트에 반영함.
- RED 회귀 테스트를 먼저 실행해 기존 결함을 확인한 뒤 구현하고 GREEN을 확인함.
- 영향 테스트 111개 통과, 셸 문법 검사·Python 컴파일·`git diff --check` 통과됨.
- 커밋 기준 change harness는 `ready_for_review`로 확인됨.
- 실제 N100 Secret의 유효 설정, Helm chart render, Prometheus DNS, Alertmanager effective config, Telegram firing/resolved 전달은 확인 필요함.

## RED 증거

코드 수정 전 회귀 테스트를 추가하고 다음 명령을 실행함.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_sre_telegram_relay tests.test_k8s_sre_telegram_manifests tests.test_k8s_sre_telegram_tools -v
```

기존 구현에서 다음 실패가 관찰됨.

| 회귀 영역 | RED 결과 |
|---|---|
| Prometheus Service 계약 | 새 relay DNS 계약 테스트를 통과하지 못하는 상태임 |
| Alertmanager 계약 | 비밀값 없는 계약 파일과 runtime Secret mount가 없어 실패함 |
| fingerprint 상태 저장 | `MemoryAlertStateStore`/ConfigMap 상태 저장 API가 없어 import 실패함 |
| restart rule timing | 기존 `for: 15m` 때문에 rolling 15분 계약 테스트 실패함 |
| RBAC namespace 범위 | `personal-server` deny 검증이 없어 도구 테스트 실패함 |
| Helm atomic rollback | upgrade 실패 후 revision 확인·rollback 호출이 없어 도구 테스트 실패함 |

## GREEN 및 변경 내용

| 검토 항목 | 반영 내용 | 검증 |
|---|---|---|
| Prometheus DNS | relay 기본 API를 `personal-server-monitoring-prometheus.monitoring.svc:9090`으로 통일함 | 실제 request URL 회귀 테스트 통과 |
| Alertmanager config contract | 비밀값 없는 `alertmanager-config.contract.yaml` 추가; route matcher, group, 4시간 repeat, relay URL, `send_resolved`, Bearer credentials file 경로를 계약화함 | YAML 계약 테스트 및 preflight/install 정적 계약 검증 통과 |
| Secret 경계 | Alertmanager values는 Secret 이름만 참조하고 runtime Secret mount만 선언함; 실제 Secret 값은 읽거나 출력하지 않음 | Secret 값 비노출 계약 테스트 통과 |
| RBAC deny | `monitoring` 및 `personal-server` 각각에서 Secret, exec/port-forward, workload 변경 권한을 `auth can-i=no`로 확인함 | 양 namespace 실행 테스트 통과 |
| 중복 firing | fingerprint/state를 4시간 TTL로 보관하고 동일 상태 재전송을 억제함; resolved와 상태 전이는 허용함 | 중복, resolved, relay 재시작, 동시 webhook 테스트 통과 |
| 상태 저장 | relay 전용 ConfigMap에 fingerprint hash 기반 비밀값 없는 상태를 저장함 | ConfigMap 재시작 저장 테스트 통과 |
| restart timing | `increase(...[15m]) > 3`의 추가 `for: 15m`을 제거함 | 규칙 timing 계약 테스트 통과 |
| chat ID redaction | 숫자 chat ID를 전역 문자열 치환 대상에서 제거함 | 단일 숫자·긴 숫자 chat ID 상태 집계 테스트 통과 |
| failure logging | polling/Telegram HTTP/Alertmanager delivery/state 오류에 예외 유형·재시도 메타데이터만 기록함 | Secret·response body 비노출 로그 테스트 통과 |
| Helm atomic failure | 실패 시 현재 release status/revision을 확인하고 이전 revision이 아니면 rollback 후 다시 검증함; 검증 실패는 PASS로 처리하지 않음 | upgrade 실패·revision drift·signal rollback 테스트 통과 |

## 검증 결과

| 검증 명령 | 결과 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_sre_telegram_relay tests.test_k8s_sre_telegram_manifests tests.test_k8s_sre_telegram_tools tests.test_k8s_monitoring_tools tests.test_k8s_monitoring_values tests.test_k8s_sre_health_audit tests.test_k8s_sre_pod_recovery_lab -q` | 111개 통과 |
| `bash -n infra/k8s/tools/sre-telegram-preflight.sh infra/k8s/tools/sre-telegram-secret-template.sh infra/k8s/tools/sre-telegram-install.sh infra/k8s/tools/sre-telegram-verify.sh` | 통과 |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile sre-telegram-relay/app/main.py` | 통과 |
| `git diff --check` | 통과 |
| `python3 scripts/run_change_harness.py --input <NUL 경로 기록> --input-format git-name-status-z --check-result maintenance=success --agent-context` | `ready_for_review` |
| `git status --short` | 커밋 후 clean 확인 |

## 변경 파일

- `infra/k8s/README.md`
- `infra/k8s/sre-telegram/alertmanager-config.contract.yaml`
- `infra/k8s/sre-telegram/alertmanager-values.yaml`
- `infra/k8s/sre-telegram/prometheus-rule.yaml`
- `infra/k8s/tools/sre-telegram-install.sh`
- `infra/k8s/tools/sre-telegram-preflight.sh`
- `infra/k8s/tools/sre-telegram-secret-template.sh`
- `infra/k8s/tools/sre-telegram-verify.sh`
- `sre-telegram-relay/app/main.py`
- 관련 Telegram SRE 회귀 테스트 3개 파일

## 확인 필요 사항 및 잔여 우려

- 실제 N100에서 운영자가 seed한 `sre-telegram-alertmanager-config`의 `alertmanager.yaml`이 계약과 일치하는지 `amtool check-config` 및 effective config로 확인 필요함. 본 작업에서는 Secret 값을 읽지 않음.
- kube-prometheus-stack 88.6.1 chart render 및 `alertmanagerSpec.secrets`의 실제 mount 경로는 Helm repository·N100 환경에서 확인 필요함.
- `personal-server-monitoring-prometheus` Service DNS가 실제 클러스터에서 resolve되는지 apply 전 확인 필요함.
- Alertmanager bearer token 값과 relay runtime Secret의 `alertmanager_auth_token` 값 일치 여부는 승인된 N100 Secret manager 내부 절차에서만 확인 필요함.
- Helm atomic rollback이 실제 경합·실패 상황에서 이전 revision과 `deployed` 상태를 복구하는지는 apply 승인 후 별도 검증 필요함.
- Docker image build, K3s apply, Telegram `/상태`, firing/resolved 실전송은 사용자 금지 조건으로 미실행함.

## 후속 조치

1. 승인된 N100에서 Secret 값을 노출하지 않는 방식으로 pre-seed metadata와 `amtool check-config` 결과를 확보함.
2. `--render` 결과와 Service/Secret mount를 확인함.
3. 별도 승인 후에만 `--apply` 및 제한된 firing/resolved 전달 검증을 수행함.
