# P1 관측성 수집 복구 설계

## 목적

N100 수동 SLO 수집에서 확인된 Portal Ready 복수 시계열과 crawler freshness 미발견 문제를 수정함. 수집 실패를 성공으로 보정하지 않으며, 검증 성공 전 CronJob 자동 실행을 활성화하지 않음.

## 범위

| 구분 | 처리 |
|---|---|
| Portal Ready | Prometheus 응답이 하나의 수치가 되도록 전체 최솟값을 집계함 |
| crawler 관측성 | 기존 Service, ServiceMonitor, 사전 시딩된 Secret은 유지하고 Prometheus discovery 경계를 복구함 |
| SLO 수집기 | 기존 `unobservable` fail-closed 계약을 유지함 |
| 운영 적용 | 이미지 재빌드·반입, 중지 상태 수동 Job, 증적 검증, 성공 시 활성화를 수행함 |

## 제외 범위

- Portal PVC, Secret 값, 운영 데이터, Caddy, Tunnel ingress를 변경하지 않음.
- metrics Secret을 새로 만들거나 복제하지 않음.
- 월간 SRE 감사 수동 실행과 Telegram 실제 전송은 이 변경의 자동 검증에 포함하지 않음. 해당 실행은 격리 복구 훈련을 함께 수행하므로 별도 승인 범위임.

## 설계

1. `portal_ready` PromQL을 전체 `min`으로 감싸 kube-state-metrics의 중복 시계열도 하나의 scalar로 반환하게 함.
2. crawler ServiceMonitor가 Prometheus target으로 발견되지 않는 원인을 dropped target 메타데이터와 Service/EndpointSlice 계약으로 확인함. 저장소 매니페스트와 실제 리소스가 다를 때에만 ServiceMonitor 또는 Service의 비밀값 없는 discovery 계약을 최소 수정함.
3. 수집기는 Portal Ready와 crawler freshness 모두 scalar 값이 있을 때만 `overall=ok`를 기록함. 누락·오류는 기존처럼 `unobservable` 및 Job 실패로 유지함.
4. 배포는 새 main 커밋 동기화, 필요한 이미지 반입, 최초/갱신 매니페스트 안전 절차, 수동 Job 성공과 증적 구조 검증, CronJob 활성화 순으로 수행함.

## 성공 기준

- Portal Ready와 crawler freshness 쿼리가 각각 하나의 Prometheus scalar를 반환함.
- Prometheus target 목록에 crawler 대상이 `up` 상태로 존재함.
- 수동 SLO Job이 성공하고 최신 KST 날짜의 증적은 최대 30건, 중복 없음, 내림차순이며 `overall=ok`임.
- CronJob 활성화 후 외부 health가 10초 간격 3회 모두 HTTP 200임.

## 실패 처리

- 수동 Job 실패·관측 불가·증적 검증 실패 시 CronJob은 `suspend=true`로 유지함.
- 명령 응답이 불완전하면 같은 변경 명령을 반복하지 않고 이미지·리소스·Job 상태를 먼저 읽음.
