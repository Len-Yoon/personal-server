# K3s Telegram SRE Release Gate Safety Fix Report

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | K3s Telegram SRE Release Gate Safety Fix Report |
| 작성일 | 2026-09-02 |
| 작업 브랜치 | `codex/sre-telegram-alerting-design` |
| 기준 자료 | `final-rereview.md` |
| 목적 | Alertmanager effective-config gate, Helm rollback 검증, Telegram polling 로그 판정 오류를 안전하게 보완함 |
| 실행 제한 | N100 접근, 실제 Secret 값 조회·출력, 네트워크, Helm/Kubernetes apply, Telegram 전송, push, merge를 수행하지 않음 |

## 2. 핵심 요약

- N100 운영자가 제공한 권한 제한 임시 로컬 `alertmanager.yaml`만 입력받아 `amtool check-config` 및 필수 Alertmanager 계약을 비출력 방식으로 검증하도록 보완함.
- 실제 Kubernetes Secret의 `.data` 또는 값은 조회·출력하지 않으며, `amtool`·로컬 파일·계약 중 하나라도 사용할 수 없거나 검증에 실패하면 preflight를 실패 처리함.
- Helm rollback은 revision 번호 동일성 대신 rollback 전 Helm values와 rendered manifest snapshot, 그리고 `deployed` 상태의 동등성을 확인하도록 변경함.
- Telegram polling 예외는 polling 실패로만 기록하고, 실제 `send_message` 거부만 delivery 실패로 기록하도록 분리함. 모든 로그는 예외 유형·고정 사유만 기록하며 payload·token을 기록하지 않음.

## 3. 근본 원인 분석

| 구분 | 기존 원인 | 영향 | 조치 |
|---|---|---|---|
| Alertmanager gate | Git의 비밀값 없는 계약 파일과 Secret key/byte metadata만 확인함. 운영자가 seed한 실제 설정의 route, receiver, Bearer wiring을 실행 조건으로 확인하지 않음. | malformed 또는 연결이 틀린 설정도 설치 전 통과 가능함. | 운영자 제공 로컬 설정 파일을 입력받아 `amtool check-config`와 비출력 계약 검증을 수행함. |
| Helm rollback | rollback 후 release가 새 revision을 생성하는 Helm 동작과 달리, 이전 revision 번호 동일성을 요구함. | 정상 rollback을 실패로 오인하고 불필요한 rollback을 반복할 수 있음. | 사전 values·manifest snapshot과 현재 values·manifest를 `cmp -s`로 비교하고 `deployed` 상태를 확인함. |
| polling log | polling 예외 후 `delivered=False`가 되어 동일 cycle에 `telegram_delivery_failed`도 기록됨. | polling 장애를 Telegram sendMessage 거부로 오인할 수 있음. | polling 실패 여부를 별도 상태로 유지하여 delivery 로그를 억제함. |

## 4. 변경 범위 및 제외 범위

| 구분 | 파일 | 변경 내용 |
|---|---|---|
| Preflight | `infra/k8s/tools/sre-telegram-preflight.sh` | `--alertmanager-config-file` 및 환경변수 입력 지원, `amtool`·route/group/repeat/resolved/relay/Bearer gate 추가 |
| Install | `infra/k8s/tools/sre-telegram-install.sh` | apply 전 config-file gate 전달, Helm values·manifest snapshot 및 deployed/content equivalence rollback 검증 추가 |
| Relay | `sre-telegram-relay/app/main.py` | polling 예외와 message delivery 거부 로그 분리 |
| Test | `tests/test_k8s_sre_telegram_tools.py`, `tests/test_sre_telegram_relay.py` | local file 비노출·fail-closed·new revision rollback·content drift·로그 분리 회귀 테스트 추가 |
| Guide | `infra/k8s/README.md` | N100 운영자 임시 로컬 파일 절차, fail-closed 조건, rollback 검증 기준 반영 |

다음 영역은 변경하지 않음: Compose, Portal, Caddy, 외부 Ingress/NodePort/LoadBalancer, 서버 기동, Windows bootstrap, 기존 scheduler, 실제 N100 Secret·cluster 상태.

## 5. TDD 수행 결과

### 5.1 RED

다음 회귀 테스트를 production code 변경 전에 추가하고 실행함.

| 테스트 | 기존 결과 | 재현 내용 |
|---|---|---|
| local config non-echo gate | 실패 | `amtool check-config` 호출 및 실제 로컬 설정 계약 검증이 없음 |
| `amtool` rejection fail-closed | 실패 | `amtool` 실패가 preflight FAIL로 연결되지 않음 |
| atomic rollback with new revision | 실패 | revision이 새 번호인 정상 restored state를 old revision 불일치로 처리함 |
| rollback content drift | 실패 | rollback 전 values·manifest와 복구 후 content 동등성을 확인하지 않음 |
| polling failure log classification | 실패 | `telegram_polling_failed`와 `telegram_delivery_failed`가 같은 polling 예외에서 함께 기록됨 |

RED 실행 결과는 7건 중 6건 실패였음. message delivery-only 로그 테스트 1건은 기존 delivery path가 이미 올바르게 분류되어 통과했으며, polling 오류와의 분리 회귀 기준으로 유지함.

### 5.2 GREEN

- `amtool check-config` 출력과 오류 출력을 모두 억제하고, operator file을 출력하지 않는 검사만 수행함.
- 로컬 config에서 `route`, `group_by`, `repeat_interval: 4h`, `sre_telegram="true"`, relay receiver/ClusterIP URL, `send_resolved: true`, `type: Bearer`, credentials file 경로를 확인함.
- `amtool`, file readability, syntax, 계약 중 하나라도 실패하면 `check=alertmanager_effective_config status=FAIL`로 종료함.
- Helm failure/interrupt 정리 시 snapshot과 current release의 `helm get values --all --output yaml`, `helm get manifest`, `helm status`를 비출력 비교함. 값·manifest·상태를 모두 확인할 수 없으면 `sre_telegram_helm_restore=UNVERIFIED`을 stderr에 기록하며 설치 성공으로 표시하지 않음.
- polling exception에는 polling log만 남기고, `send_message`가 false를 반환한 경우에만 delivery log를 남김.

## 6. 검증 결과

| 검증 | 결과 | 비고 |
|---|---|---|
| 신규 핵심 회귀 7건 RED | 기대대로 실패 확인 | production code 변경 전 실행함 |
| SRE relay·tool 단위 테스트 60건 | 통과 | local command stub 및 temporary config file만 사용함 |
| SRE relay·manifest·tool·monitoring 관련 회귀군 115건 | 통과 | 실제 N100·network·apply 없이 실행함 |
| Shell syntax | 통과 | `sre-telegram-preflight.sh`, `secret-template.sh`, `install.sh`, `verify.sh` 대상 `bash -n` 실행함 |
| diff whitespace | 통과 | `git diff --check` 실행함 |

## 7. 확인 필요 사항

| 항목 | 내용 |
|---|---|
| N100 `amtool` | 운영자가 N100에서 설치된 `amtool` 버전과 실제 Secret manager 원본의 임시 파일로 preflight를 실행해 확인 필요함. 미설치 시 의도적으로 FAIL됨. |
| 실제 Helm state | 실제 deployed release의 `helm get values`와 rendered manifest가 rollback 뒤 byte-equivalent인지 N100 승인 절차에서 확인 필요함. 본 작업에서는 cluster에 접근하지 않음. |
| 임시 파일 관리 | 운영자는 권한 제한 파일을 사용하고 preflight/apply 후 조직의 Secret 관리 절차에 따라 즉시 폐기 필요함. 도구는 운영자 소유 파일을 삭제하지 않음. |
| external action | N100 preflight, render, apply, verify, Telegram delivery는 승인 범위 밖이므로 수행하지 않음. |

## 8. 후속 조치

1. N100 운영자 승인 후 Secret manager 원본에서 생성한 임시 config file 경로로 preflight를 실행함.
2. preflight PASS 후에만 같은 경로를 `sre-telegram-install.sh --apply --alertmanager-config-file`에 전달함.
3. install FAIL 시 `sre_telegram_helm_restore=UNVERIFIED` 여부를 확인하고, 해당 표기가 있으면 restored state를 성공으로 간주하지 않음.
4. 실제 N100 실행 결과는 Secret 값·payload·token을 포함하지 않는 별도 운영 증적으로 기록 필요함.

## 9. 에이전트 운영 기록

- 구현·검증은 단일 담당자가 수행함.
- 하위 에이전트는 상위 지시의 명시적 금지 조건에 따라 사용하지 않음.
- 동일 파일 동시 수정은 수행하지 않았으며, 변경 범위·제외 범위·성공 기준을 작업 시작 전에 선언함.

## 10. P1 구조적 Alertmanager route 검증 보완

### 10.1 재검토 근거 및 근본 원인

`release-gate-review.md`의 P1 지적을 반영함. 기존 effective-config gate는 `amtool check-config` 후 route matcher, receiver, URL, Bearer 문자열을 독립적으로 검색했음. 따라서 `sre_telegram="true"` route가 noop receiver를 선택하고, 도달하지 않는 별도 route/receiver에 relay URL과 Bearer 설정이 존재하는 문법상 유효 YAML이 PASS할 수 있었음.

### 10.2 TDD 수행 결과

| 단계 | 검증 | 결과 |
|---|---|---|
| RED | `test_preflight_rejects_amtool_valid_config_when_sre_route_does_not_select_relay` | 기존 구현에서 return code `0`으로 PASS하여 우회를 재현함 |
| GREEN | 동일 테스트 및 정상 config/`amtool` 실패 경로 3건 | 3건 통과함 |

### 10.3 보완 내용

- 독립 문자열 검색을 제거하고, 비출력 Python YAML parser로 operator-supplied local config의 구조를 검증함.
- root route의 non-empty `group_by`, `repeat_interval: 4h`를 확인함.
- 모든 `sre_telegram=true` 또는 모호한 `sre_telegram` matcher 경로가 relay receiver를 명시적으로 선택하는지 확인함.
- relay receiver가 하나만 존재하고, webhook이 하나만 존재하며, 해당 webhook이 ClusterIP URL, `send_resolved: true`, `type: Bearer`, expected credentials file을 함께 사용하는지 확인함.
- YAML parser를 사용할 수 없거나 YAML 구조가 예상과 다르면 출력 없이 FAIL 처리함. Kubernetes Secret 값, local config 내용, token, payload는 로그·terminal에 출력하지 않음.

### 10.4 재검증 결과

| 검증 | 결과 |
|---|---|
| SRE relay·manifest·tool 회귀군 | 71건 통과 |
| monitoring 회귀군 | 30건 통과 |
| SRE health audit·pod recovery 회귀군 | 15건 통과 |
| 총 관련 회귀군 | 116건 통과 |
| Shell syntax 및 diff whitespace | 통과 |

### 10.5 추가 확인 필요 사항

- N100에서 `amtool`과 Python YAML parser가 모두 이용 가능해야 하며, 하나라도 없으면 의도적으로 preflight FAIL됨.
- 실제 N100 operator file 검증은 승인된 Secret manager 절차에서만 수행 필요함. 본 보완에서는 N100, 실제 Secret, network, deploy를 수행하지 않음.
