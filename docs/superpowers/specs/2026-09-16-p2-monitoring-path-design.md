# P2 감시 경로 상태 구분 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | P2 감시 경로 상태 구분 설계 |
| 작성일 | 2026-09-16 |
| 대상 | 공개 health 감시, GitHub Actions, Telegram 전달, SRE Telegram relay |
| 목적 | 서비스 장애와 감시·알림 경로의 실패를 혼동하지 않도록 상태와 증적을 분리함 |
| 제외 | 새 외부 감시 서비스, Telegram 자격 증명, Tunnel ingress, Caddy, 운영 데이터 변경 |

## 핵심 요약

현재 공개 health 감시는 GitHub Actions가 수행하고 Telegram 전송 성공은 기존 장애 Issue 표기로 남김. 그러나 workflow 자체의 실행 실패와 Telegram 전달 실패가 운영자가 보기 쉽게 분리되지 않았으며, 뉴스 수집 경고의 일반 문구가 실제 자동 복구가 없는 동작을 암시함.

P2는 GitHub 밖의 새 감시 의존성을 추가하지 않음. 실행된 공개 감시의 서비스 상태, Telegram 전달 상태, workflow 실행 실패를 각각 별도 Issue 상태로 기록함. GitHub Actions가 아예 실행되지 않은 경우는 자동으로 판정할 수 없으므로 `관측 불가`로 명시함.

## 상태 계약

| 구분 | 판정 원본 | 기록 위치 | Telegram 전송 | 처리 기준 |
|---|---|---|---|---|
| 공개 서비스 장애 | 기존 health step 결과 | 기존 공개 상태 장애 Issue | 기존 장애·복구 전환만 전송 | 실패 서비스 식별자만 기록 |
| Telegram 전달 실패 | 기존 Telegram step의 실패 또는 Secret 누락 | 기존 공개 상태 장애 Issue의 전달 상태 표기 | 추가 전송 없음 | health 점검과 Issue 처리는 계속 수행 |
| workflow 실행 실패 | `Public Portal Uptime Monitor`의 완료된 실패 run | 전용 감시 실행 실패 Issue | 전송하지 않음 | 실행 실패 원인 원문·비밀값은 기록하지 않음 |
| workflow 미실행 | GitHub 밖 독립 신호 없음 | 운영 문서 | 전송하지 않음 | `관측 불가`로 표시하고 자동 장애 판정하지 않음 |

## 구성

### 1. 공개 health workflow

기존 `.github/workflows/public-uptime-monitor.yml`은 네 공개 health 경로와 기존 장애 Issue·Telegram 전환 정책을 유지함. Telegram 전달을 시도하지 못했거나 실패한 경우에는 서비스 정상 여부와 구분되는 고정된 비밀값 없는 상태 표기를 남김.

### 2. workflow 실행 실패 기록

새 GitHub workflow는 `workflow_run` 이벤트로 공개 health workflow의 완료 상태만 읽음. 완료 결과가 실패일 때 전용 Issue를 생성하거나 갱신하고, 이후 성공 run이 확인되면 같은 Issue를 닫음. 서비스 health 실패는 기존 monitor가 `continue-on-error`로 처리하므로, 이 전용 Issue는 monitor 내부 실행·GitHub API·Telegram 전달 경로의 실패를 식별하는 보조 증적으로 사용함.

이 workflow도 GitHub Actions에서 실행되므로 원본 workflow가 전혀 시작하지 않는 경우는 검출할 수 없음. 이를 새로운 서비스나 Secret으로 우회하지 않음.

### 3. 뉴스 수집 Telegram 표현

`NewsCollectionStale`은 최근 성공이 15분을 초과하거나 연속 실패가 3회 이상일 때 발생함. 이 경고는 crawler 자동 재시작을 수행하지 않음. relay의 firing 문구는 `다음 수집 상태를 확인 중입니다.`로 표시하고, resolved 문구는 기존과 같이 정상 복귀를 표시함.

Alertmanager의 지속 경고 재전송 주기는 4시간이며, relay는 동일 fingerprint·동일 상태를 4시간 동안 억제함. 따라서 동일 경고가 지속되면 4시간 단위 재알림은 정상 동작으로 취급함.

## 오류 처리와 안전 경계

| 상황 | 처리 |
|---|---|
| Telegram 전달 실패 | health 상태와 Issue 처리는 유지하고, 전달 실패 상태만 기록함 |
| workflow 실행 실패 | 전용 Issue에 최소 상태만 기록하고 Telegram을 보내지 않음 |
| workflow 미실행 | 자동 장애·복구 판정이나 재실행을 시도하지 않음 |
| NewsCollectionStale 발생 | crawler·Compose·K3s를 자동 재시작하지 않음 |
| 동일 상태 재전송 | 4시간 반복 정책과 fingerprint 상태 보존을 유지함 |

- Bot token, Chat ID, HTTP 응답 본문, 내부 주소, 예외 원문은 Issue·로그·문서·테스트에 기록하지 않음.
- P2는 Portal PVC·Secret·운영 데이터·Caddy·Tunnel ingress·Compose writer를 수정하지 않음.
- N100 적용은 이 설계 단계의 범위 밖이며, 구현 후 테스트·독립 검토·사용자 승인으로만 진행함.

## 검증 기준

1. 공개 health 장애와 Telegram 전달 실패가 서로 다른 Issue 상태로 확인됨.
2. workflow 완료 실패는 전용 Issue로 확인되며, 다음 성공 run에서 닫힘.
3. workflow 미실행은 자동 감지 범위 밖이라는 문서 계약이 존재함.
4. NewsCollectionStale firing 문구가 자동 복구를 암시하지 않음.
5. 기존 4시간 반복 및 중복 억제 계약이 유지됨.
6. 관련 workflow·relay·문서 계약 테스트가 통과함.

## 확인 필요 사항

- GitHub Actions가 완전히 미실행되는 상태는 외부 독립 감시를 추가하지 않는 한 자동 검출할 수 없음.
- 실제 Telegram 단말 수신은 API 성공 증적과 별도로 운영자가 확인함.
