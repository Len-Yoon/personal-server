# P1 일 단위 SLO 증적 설계

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | P1 일 단위 SLO 증적 설계 |
| 작성일 | 2026-09-15 |
| 기준 자료 | `docs/slo-baseline.md`, Portal HTTP metrics, crawler freshness metrics, 월간 SRE 감사 |
| 목적 | Prometheus 7일 보존을 늘리지 않고 최근 30일의 SLO 검토 근거를 보관함 |
| 상태 | 승인 완료. 구현 계획 작성 전 |

## 핵심 요약

`monitoring` namespace에 전용 ConfigMap 하나를 두고, 매일 02:15 KST에 실행하는 읽기 전용 수집 CronJob이 저카디널리티 일별 기록을 최대 30건까지 갱신함. 수집기는 내부 Prometheus query API와 공개 Portal health만 조회하며, Portal PVC·Secret·운영 데이터·Prometheus retention은 변경하지 않음. 수집 실패나 결측은 성공으로 보정하지 않고 `unobservable`로 기록함.

## 범위와 제외 범위

| 구분 | 처리 |
|---|---|
| 일별 보관소 | `monitoring/slo-daily-evidence` ConfigMap의 `records.json` 하나만 사용함 |
| 보관 수 | KST 날짜 기준 최근 30건만 유지함 |
| 실행 주기 | 매일 02:15 KST. 일일 백업 03:00 및 월간 감사 03:30과 겹치지 않음 |
| 조회 원본 | 내부 Prometheus query API, 공개 `https://len.pe.kr/health` |
| 월간 활용 | 월간 SRE 감사가 ConfigMap을 읽어 30일 coverage·정상·실패·관측 불가 일수를 결과에 추가함 |
| 제외 | Prometheus retention, Secret, PVC, Portal 운영 데이터, Caddy, Tunnel ingress, 자동 배포 차단, 자동 복구 |

## 일별 기록 계약

`records.json`은 JSON 배열이며 각 원소는 아래 고정 필드만 사용함. UTC 시각은 저장·비교에만 사용하고, 날짜 경계와 보고 표시는 KST를 기준으로 처리함.

| 필드 | 형식 | 설명 |
|---|---|---|
| `date` | `YYYY-MM-DD` | KST 기준 관측 날짜. 배열 내 중복 불가 |
| `collected_at` | UTC ISO 8601 | 수집 완료 시각 |
| `overall` | `ok` 또는 `unobservable` | 모든 필수 원본을 읽고 검증했는지 여부 |
| `public_health` | `ok`, `failed`, `unobservable` | 공개 Portal health 1회 확인 결과 |
| `portal_ready` | `ok`, `failed`, `unobservable` | 최근 24시간 Portal Ready SLI 확인 결과 |
| `portal_http` | 객체 또는 `null` | 요청 수, 5xx 수, 5xx 비율, p95 초 단위 값 |
| `crawler_freshness` | `ok`, `failed`, `unobservable` | 최근 24시간 crawler freshness SLI 확인 결과 |
| `missing` | 문자열 배열 | 관측 불가 원본 이름. 빈 배열이면 `overall=ok` |

`portal_http` 객체는 `requests`, `server_errors`, `server_error_ratio`, `p95_seconds` 네 수치만 포함함. 요청 수가 0이면 비율과 p95는 `null`로 기록하고, 이 값은 성공·실패 SLO 판정에 사용하지 않음. 수치는 음수·NaN·무한대·문자열을 허용하지 않음.

## 수집·판정 흐름

1. 수집기는 KST 기준 직전 24시간 구간을 계산함.
2. Prometheus query API에서 Portal HTTP 요청 수·5xx 수·histogram p95, `portal-web` Ready, crawler freshness를 각각 조회함.
3. 공개 Portal health는 HTTPS로 한 번 조회하고 HTTP 200만 `ok`로 기록함. 이는 5분 외부 감시를 대체하는 가용성 산정값이 아니라 일별 교차 확인 증적임.
4. 원본별 응답 구조·필수 메트릭·수치 범위를 검증함. 어떤 필수 Prometheus 질의가 실패하거나 결과가 없으면 해당 원본과 `overall`을 `unobservable`로 기록함.
5. 동일 KST 날짜의 기존 기록은 원자적으로 교체하고, 날짜 내림차순 정렬 후 최근 30건만 남김. 날짜가 지난 기록은 복원하지 않음.
6. ConfigMap 읽기·patch가 실패하면 성공 기록을 만들지 않고 Job을 실패 종료함.

## Prometheus 질의 기준

| 대상 | 기준 |
|---|---|
| Portal HTTP | `portal_http_requests_total` 및 `portal_http_request_duration_seconds_bucket`의 직전 24시간 increase로 요청 수·5xx·p95를 계산함 |
| Portal Ready | `kube_deployment_status_replicas_available{namespace="personal-server",deployment="portal-web"}`가 직전 24시간에 1 이상인 관측 비율을 판정함 |
| crawler freshness | `crawler_news_collection_initialized`, 마지막 성공 시각, 연속 실패 수를 기존 `NewsCollectionStale`의 15분·3회 기준으로 직전 24시간에 판정함 |

Prometheus 7일 보존 안에서만 일별 값을 추출하며, 30일 조회는 ConfigMap의 일별 기록만 사용함. 새 label, 사용자 식별자, 요청 본문, 인증 정보는 수집하지 않음.

## Kubernetes 구성과 권한

- `slo-daily-evidence` ServiceAccount는 `monitoring/slo-daily-evidence` ConfigMap에 `get`, `patch`만 가짐.
- CronJob은 `concurrencyPolicy: Forbid`, `backoffLimit: 0`, 제한된 실행 시간, non-root, read-only root filesystem, `/tmp` emptyDir, privilege escalation 금지, 모든 Linux capability drop을 사용함.
- Prometheus는 기존 내부 Service DNS로만 조회함. ConfigMap 외 hostPath·PVC·Secret volume을 mount하지 않음.
- 월간 감사는 해당 ConfigMap에 `get`만 추가하며 기록을 변경하지 않음.

## 월간 보고 계약

월간 감사 결과에는 기존 필드를 유지한 채 `slo_evidence` 결과를 추가함. 값은 `passed`, `failed`, `unobservable` 중 하나이며, status ConfigMap에는 다음 수치만 기록함: `slo_days_recorded`, `slo_days_ok`, `slo_days_unobservable`. 개별 HTTP 경로·내부 주소·원본 오류·Secret 값은 기록하거나 Telegram으로 전달하지 않음.

30일 미만의 초기 수집 구간은 `unobservable`로 보고하며, SLO 위반·에러 버짓 소진을 배포 차단이나 자동 복구 조건으로 사용하지 않음.

## 오류 처리와 검증

| 상황 | 처리 |
|---|---|
| Prometheus 질의 실패·형식 오류 | 해당 일자를 `unobservable`로 기록하고 Job 실패 |
| 공개 health 비-200 | `public_health=failed`로 기록하며 Prometheus 증적이 정상이면 Job은 성공 종료 |
| ConfigMap patch 실패 | 기록 성공으로 간주하지 않고 Job 실패 |
| 날짜 중복 | 같은 KST 날짜의 최신 검증 기록으로 교체 |
| 31건 이상 | 가장 오래된 날짜부터 제거 |

회귀 테스트는 정상 기록, 결측, 잘못된 수치, 날짜 중복 교체, 30건 경계, 최소 RBAC, Secret·PVC mount 부재, 월간 감사의 읽기 전용 집계 및 상태 계약을 검증함.

## 검토 결과

- 기존 매일 백업·약 5분 공개 감시·매월 1일 내부 감사의 실행 경계를 유지함.
- ConfigMap은 30개의 집계값만 보관하므로 Prometheus retention 또는 운영 저장소 확장이 필요하지 않음.
- 실제 공개 가용성의 고해상도 SLI는 기존 GitHub Actions가 계속 담당하며, P1은 30일 검토용 증적을 보완함.

## 확인 필요 사항

- 30일 기준선 수집 후 99.5%/99.0% 목표와 에러 버짓 임계값을 재검토 필요함.
- ConfigMap 기반 증적이 30일을 초과하는 장기 감사 요구를 충족하지 않으므로, 장기 보존 요구가 생기면 별도 저장소 설계 필요함.

## 후속 조치

1. 수집기·ConfigMap·CronJob·최소 RBAC·월간 감사 읽기 집계를 구현함.
2. 수동 Job 성공 후에만 일일 CronJob을 활성화함.
3. 30일 기준선 수집 뒤 SLO 목표와 월간 보고 형식을 조정할지 검토함.
