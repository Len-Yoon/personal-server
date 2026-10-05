# SLI SLO 에러 버짓 운영 기준 초안

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | SLI SLO 에러 버짓 운영 기준 초안 |
| 작성일 | 2026-09-14 |
| 운영 상태 대조 | 2026-09-23 |
| 기준 자료 | 공개 상태 점검 workflow, PrometheusRule, crawler 뉴스 수집 metrics, 운영 참조 |
| 목적 | 현재 수집 중인 신호를 기준으로 30일 SLO와 에러 버짓의 측정·검토 기준을 정의함 |
| 상태 | 초안. 30일 기준선 수집 뒤 목표값을 재검토함 |

## 핵심 요약

현재 관측 원본은 공개 health, K3s Portal Ready, 뉴스 freshness와 Portal HTTP 요청 수·오류·지연시간 지표임. Portal HTTP 계측은 구현·운영 반영되었으며 다른 Compose 서비스에 같은 계측이 있다고 가정하지 않음. 이 문서의 30일 목표값은 여전히 초안임. 일일 SLO 수집기는 freshness 다중 시계열 집계 보완을 운영 적용하고 자동 실행을 활성화했으며, 2026-09-23 예약 실행이 성공함. [원인 분석](reviews/20260921_SLO증적실패_원인분석.md)과 [운영 적용 검증 결과](reviews/20260922_뉴스_SLO_운영적용_검증결과.md)를 구분해 참조함.

2026-09-27 읽기 전용 점검에서 일일 증적은 9월 16~27일의 12건만 확인됐고, 공개 감시 workflow는 9월 24~26일 각각 6·6·7회 실행됨. 5분 간격의 30일 전체 관측을 전제로 한 성공률·에러 버짓은 현재 산정할 수 없음. 최신 일일 기록의 `public_health`, `portal_ready`, `crawler_freshness`는 모두 `failed`였으나 현재 공개 Portal health의 별도 1회 조회는 HTTP 200이었음. 일일 기록이 과거 24시간 구간을 평가한다는 점과 현재 상태를 구분함. 당시 공개 health 응답 코드는 증적에 저장되지 않아 확인 필요함.

2026-09-28 읽기 전용 재점검에서 세 세부 항목은 계속 `failed`이고, Job의 `overall=ok`는 자료 수집 성공만 뜻함. Portal의 24시간 가용 replica 최솟값 0은 정기 백업 완료 시각과 겹치므로 계획 정지 영향으로 추정하되 원시 값을 성공으로 바꾸지 않음. 뉴스 freshness의 24시간 최솟값 0은 15분 초과 수집 공백을 의미함. 공개 health의 02:15 KST 비-200 코드는 과거 로그에 없어서 확인 필요이며, [운영 반영 사전검토](reviews/20260928_Book_SLO_뉴스백업_운영반영_사전검토.md)의 진단 이미지는 이후 비-200 코드만 기록하도록 준비됨. 세 항목의 실패가 곧 30일 SLO 목표 위반을 뜻하지는 않으며, 목표 비율은 원시 관측과 계획 정지 해석을 확정한 뒤 별도 산정함.

## 용어와 공통 원칙

| 용어 | 정의 |
|---|---|
| SLI | 서비스 신뢰성을 수치로 관측하는 지표임 |
| SLO | SLI가 기간 내 달성해야 하는 목표값임 |
| 에러 버짓 | `100% - SLO`에 해당하는 허용 실패량임 |
| 관측 불가 | 수집 실패·누락 등으로 SLI를 계산할 수 없는 상태임. 성공으로 간주하지 않음 |

- 측정 기간은 최근 30일 rolling window로 사용함.
- 기준선 수집 기간에는 SLO 위반을 배포 차단이나 자동 변경 조건으로 사용하지 않음.
- 실제 사용자 요청량·응답시간 계측을 추가하기 전까지 health 성공은 사용자 기능 전체 성공을 의미하지 않음.
- 누락된 관측값은 분모에서 제외하지 않음. 데이터 수집 이상으로 별도 기록하고 원인을 확인함.

## 1차 SLO 대상

| 대상 | SLI | 데이터 원본 | 제안 SLO | 30일 에러 버짓 | 상태 |
|---|---|---|---:|---:|---|
| 공개 Portal | 완료된 외부 health 점검 중 `https://len.pe.kr/health`가 성공한 비율 | GitHub Actions `Public Portal Uptime Monitor` | 99.5% | 약 43회 점검 실패 또는 약 215분 | 30일 관측 누락으로 산정 보류 |
| K3s Portal | Prometheus 관측값 중 `portal-web`의 available replica가 1 이상인 비율 | kube-state-metrics | 99.5% | 약 216분 | 30일 원시 지표 미보존·계획 정지 해석 보류 |
| 뉴스 수집 | 초기화된 뉴스 수집이 30분 이내 성공했고 연속 실패도 3회 미만인 관측 비율 | `crawler_news_collection_*` metrics | 99.0% | 약 432분 | 30일 원시 지표 미보존 |
| Compose 웹 서비스 | HTTP 성공률 및 p95 응답시간 | 없음 | 확정하지 않음 | 산정 불가 | 2차 계측 필요 |

공개 Portal의 약 43회는 30일을 5분 간격으로 모두 실행한 8,640회 관측을 전제로 한 환산값임. workflow 실행 누락, GitHub Actions 장애, 점검 결과 보존 정책은 별도로 확인 필요함.

## SLI 계산 기준

### 1. 공개 Portal 가용성

```text
SLI = 성공한 외부 health 점검 횟수 / 완료된 외부 health 점검 횟수 × 100
```

- 성공: workflow의 `Check public Portal health` 단계가 성공한 경우임.
- 실패: 해당 단계가 실패한 경우임.
- 관측 불가: workflow 자체가 실행되지 않았거나 결과를 조회할 수 없는 경우임. 성공률에서 제외하지 않고 별도 누락 건수로 기록함.

### 2. K3s Portal Ready 가용성

```promql
avg_over_time(
  (
    kube_deployment_status_replicas_available{namespace="personal-server",deployment="portal-web"} >= bool 1
  )[30d:]
) * 100
```

- `portal-web`의 desired replica가 0인 계획 정지 상태는 현재 운영 기준에서 정의되지 않았으므로 발생 시 SLO 계산을 보류하고 확인 필요로 기록함.
- Prometheus target down은 Portal 장애와 구분해 별도 관측성 장애로 관리함.

### 3. 뉴스 수집 freshness

```promql
avg_over_time(
  (
    (crawler_news_collection_initialized == bool 1)
    * (time() - crawler_news_collection_last_success_timestamp_seconds <= bool 1800)
    * (crawler_news_collection_consecutive_failures < bool 3)
  )[30d:]
) * 100
```

- 수집 성공 시각이 없는 초기화 구간은 SLO 측정 시작 전 상태로 처리함.
- 위 식은 30일 목표 초안의 수치 조건 예시이며 현재 운영 수집기에 적용한 질의가 아님. 여러 시계열을 하나의 서비스 SLI로 집계하는 방식과 30일 보존은 추가 검증 필요함. `and` 집합 연산 대신 수치 조건을 곱해 거짓인 조건을 0으로 반영함.
- `NewsCollectionStale` 경고는 30분 조건을 사용하며, 현재 일일 증적 collector의 freshness 기준은 15분·연속 실패 3회 미만임. 경고 기준과 증적 수집 기준을 혼동하지 않음.
- 이 SLI는 기사 품질이나 분류 정확도가 아니라 수집 freshness만 측정함.

## 에러 버짓 운영 기준

| 버짓 소진 수준 | 운영 조치 | 배포 판단 |
|---:|---|---|
| 0% ~ 50% | 주간 추이 확인 | 정상 진행 가능 |
| 50% 초과 ~ 80% 이하 | 장애 원인과 반복 패턴 검토 | 기능 배포 전 관련 health·rollback 기준 확인 필요 |
| 80% 초과 | 안정성 개선 항목 우선 처리 | 신규 기능 배포는 운영자 검토 필요 |
| 100% 초과 | 인시던트 기록 및 재발 방지 조치 수립 | 자동 배포 차단은 2차 계측·운영 합의 후 결정함 |

현재 단계에서는 에러 버짓을 자동 배포 차단에 연결하지 않음. 기존 안전 배포의 CI·health·rollback 절차를 유지하며, 실제 30일 기준선과 오탐·누락 원인을 확인한 뒤 정책 변경 여부를 결정함.

## 월간 검토 양식

| 항목 | 기록값 |
|---|---|
| 측정 기간 | 시작일 ~ 종료일 |
| 공개 Portal SLI | % 및 실패·관측 불가 횟수 |
| K3s Portal SLI | % 및 Prometheus target 이상 여부 |
| 뉴스 수집 SLI | % 및 stale·연속 실패 발생 횟수 |
| 에러 버짓 소진 | 대상별 % |
| 주요 원인 | 사실 기반으로 기록함 |
| 조치 결과 | 완료·진행 중·확인 필요 중 하나로 기록함 |
| 다음 기간 변경 | 목표값, 계측, 경고 기준 변경 여부 |

## 2차 계측 범위

Portal HTTP 메트릭·scrape·HTTP 대시보드는 구현되어 있음. 아래 표는 계측 경계를 정리한 것이며, 다른 서비스 확대 및 별도 SLO·버짓 대시보드는 후속 설계 대상으로 구분함.

Portal 홈의 검색 질의가 있는 `GET /` 요청은 Prometheus에서 고정된 `route="/search"` 라벨로 분리함. 검색어 자체는 지표에 포함하지 않으며 일반 홈 요청은 `route="/"`로 유지함. Grafana에 검색 p95 패널을 추가하지만, 코드 병합만으로 운영 계측이 시작된 것은 아님. 2026-09-27 외부 시험 질의 8회의 응답은 모두 HTTP 200, 왕복 시간은 0.207~0.417초였음. 이 작은 표본은 운영 검색 p95나 30일 SLO가 아님. 기존 Portal 전체 p95 알림은 유지하고 검색 전용 경보 임계값은 운영 기준선 확인 뒤 결정함.

2026-09-28 16:33 KST N100 Prometheus를 읽기 전용으로 조회함. 최근 24시간과 7일의 `GET /search` 요청 증가량은 각각 0건이고, 7일 histogram p95는 `NaN`으로 산정 불가함. 지난 7일의 검색 counter 최댓값 1과 1,246개 scrape 표본은 이전 검색 계측 기록이 남아 있음을 보여주지만, 현재 트래픽에서 지연 분포를 추정할 근거는 아님. 따라서 검색 전용 운영 SLO·경보 임계값은 **확인 필요**로 유지함. 설계상 병렬 검색 대기 예산은 1.5초이므로, 실제 검색 요청이 7일에 100건 이상 쌓이면 p95 2초를 *잠정 검토선*으로 비교할 수 있음. 2초는 관측으로 검증된 목표가 아니며, 표본·부분 실패율과 함께 재검토한 뒤에만 경보로 활성화함. 검색 요청을 인위적으로 생성해 실제 사용자 기준선에 섞지 않음.

| 항목 | 목적 | 최소 수집 항목 | 제외 사항 |
|---|---|---|---|
| HTTP 요청 메트릭 | 사용자 체감 성공률·지연시간 측정 | 서비스명, method, route 템플릿, 상태 코드, 요청 수, 지연시간 histogram | query string, 사용자 식별자, 요청·응답 본문, 인증 정보 |
| Prometheus scrape 연결 | 서비스별 SLI 계산 | 내부 전용 metrics endpoint와 ServiceMonitor | 공개 metrics 노출 |
| Grafana SLO 대시보드 | 월간 추이·버짓 확인 | SLI, SLO, 버짓 소진, 관측 불가 | 운영 데이터·Secret 표시 |

## 확인 필요 사항

- GitHub Actions의 완료 workflow 결과를 30일 단위로 집계하는 보관·조회 방식 확인 필요함.
- Prometheus의 실제 scrape interval과 보존 기간이 30일 SLO 계산에 충분한지 확인 필요함. 현재 values 파일에는 retention 7일로 기재되어 있어 30일 K3s·뉴스 SLI를 그대로 계산할 수 없음.
- Portal 외 `news`, `memo`, `books` 공개 endpoint를 외부 SLO 대상으로 포함할지 운영자 결정 필요함.
- 99.5%와 99.0% 목표는 현재 운영 신호를 바탕으로 한 초안이며, 30일 기준선 수집 뒤 조정 필요함.

## 후속 조치

### 2026-10-05 읽기 전용 집계 보강

`infra/k8s/tools/slo-evidence-summary.py`가 기존 `records.json` 배열 또는 해당 ConfigMap JSON을 읽어 기간별 성공·실패·관측 불가·미수집 날짜를 분리함. UTC 수집 시각과 KST 기록 날짜·HTTP 오류 비율 일치·중복 날짜·미래 기록을 검사하며 원본을 변경하지 않음. `overall=ok`는 수집 성공이며 서비스 정상 판정과 구분함.

```sh
python3 infra/k8s/tools/slo-evidence-summary.py --input /tmp/slo-records.json --end-date 2026-10-04 --days 30
```

일일 HTTP 창의 가중 오류 비율은 요청 수 가중치로 계산하되, 창의 겹침·누락이 있으므로 정확한 기간의 서비스 성공률로 사용하지 않음. 일일 p95는 평균하거나 병합하지 않음. 시간 기반 가용성·에러 버짓은 항상 `not_calculable`로 남김. 예정 수집 시각 전 당일을 종료일로 넣지 않으며, 과거 기록 부족을 정상으로 채우지 않음.

실제 30일 집계와 기본 urllib 요청의 HTTP 403 식별자 차이 재현은 [고도화 검증 결과](reviews/20261005_DevOps실습고도화_검증결과.md)를 따름. 2026-10-05 승인 후 새 고정 모니터 User-Agent가 포함된 이미지를 N100에 반입하고 실제 공개 probe 성공과 CronJob image-only 교체를 확인함. 기존 증적 원본·02:15 KST 일정은 보존했고 수동 수집 Job은 실행하지 않음. 다음 정기 수집 전체 성공은 확인 필요함. [운영 적용 결과](reviews/20261005_DevOps실습_운영적용결과.md)에 digest·증적 hash·검증 범위를 기록함.

1. 공개 Portal workflow의 최근 30일 성공·실패·누락을 수동 집계해 기준선을 기록함.
2. Prometheus retention을 변경하지 않고, 우선 7일 기준 K3s Portal·뉴스 수집 SLI를 관찰함.
3. 일일 증적 ConfigMap은 최근 30건을 보관하나 원시 30일 시계열을 대체하지 않음. 30일 기준선이 쌓인 뒤 월간 증적의 누락과 목표 산정 방식을 재검토함.
4. Portal HTTP 메트릭의 7일 기준선과 Prometheus target 상태를 확인한 뒤, 다른 서비스 적용 여부를 결정함.
