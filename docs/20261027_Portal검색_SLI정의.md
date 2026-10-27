# Portal 검색 SLI 정의

## 1. 문서 정보

| 항목 | 내용 | 비고 |
|---|---|---|
| 작성일 | 2026-10-27 | Asia/Seoul |
| 대상 | Portal 통합 검색의 HTTP 응답·지연·의존 서비스 결과 | 기존 계측 재사용 |
| 기준 자료 | [계측 코드](../portal-web/app/services/http_metrics.py), [요청 분류](../portal-web/app/main.py), [통합 검색](../portal-web/app/services/global_search.py) | 실행 소스·관측 시각은 실제 평가 때 고정함 |
| 상태 | 정의·측정 한계 정리 | 새 운영 경보·목표·수집 일정 활성화 아님 |

## 2. 핵심 요약

검색의 HTTP 응답 성공과 Book/YouTube 등 의존 검색의 완전한 성공은 다른 신호임. 현재 HTTP 계측은 응답 코드와 완료 시간만 알 수 있으므로 일부 결과를 못 가져온 HTTP 200을 업무 전체 성공으로 판정하지 않음. 표본·수집 범위가 부족하면 목표 달성 여부를 보류함.

## 3. 상세 내용

| SLI | 분자·분모·단위 | 측정·제외 조건 | 현재 한계·비고 |
|---|---|---|---|
| 검색 HTTP 응답 성공 | 분자: 2xx 완료 요청 counter 증가량. 분모: 모든 완료 요청 counter 증가량. 단위: 요청 비율 | `method="GET",route="/search"`; 빈 q·포트폴리오 host·일반 홈·metrics 요청은 검색 분류에서 제외 | 3xx/4xx/5xx는 2xx 성공 분자에 넣지 않음. 완료하지 못한 요청·Tunnel/Caddy에서 끝난 실패는 앱 counter에 없음 |
| 검색 HTTP 5xx | 분자: `status_code=~"5.."` 증가량. 분모: 모든 완료 검색 요청 증가량. 단위: 요청 비율 | 위와 같은 범위·시간 창. 분모 0이면 산정 불가 | 5xx 0은 검색 업무 전체 성공 증거가 아님 |
| 검색 응답 지연 | 같은 기간 histogram bucket의 증가량을 합산한 분포의 p95. 단위: 초 | `portal_http_request_duration_seconds_bucket{method="GET",route="/search"}`; 동일 le별 합산 후 계산 | 일별 p95 평균 금지. coarse bucket 보간값이며 앱 처리 시간임. 인터넷 왕복 시간과 구분 |
| 의존 검색 완전성 | 분자: complete counter 증가량. 분모: complete+partial+failed 증가량. 단위: 검색 비율 | 신규 `portal_search_requests_total`, cancelled·빈검색·데모는 분모 제외/별도 표시 | 저장소 구현과 운영 적용 구분. HTTP 200을 업무 전체 성공으로 대용하지 않음 |
| 관측 범위 | 분자: 유효하고 신선한 표본. 분모: 요청한 평가 grid의 전체 표본. 단위: 표본 비율 | missing·stale·nonfinite·미래 시각을 unknown으로 표시 | 아래 Portal replica 도구의 coverage는 검색 counter coverage를 직접 대체하지 않음 |

예시 질의는 아래와 같음. 분모가 0이거나 시계열·scrape가 누락되면 정상값으로 채우지 않음. `increase`는 scrape 간 counter reset을 고려한 추정 증가량이므로 원시 요청 원장의 정수 집계와 구분함. 동일 서비스에 대한 중복 scrape가 없는지 확인한 뒤 합산함.

```promql
sum(increase(portal_http_requests_total{method="GET",route="/search",status_code=~"2.."}[7d]))
/
sum(increase(portal_http_requests_total{method="GET",route="/search"}[7d]))

histogram_quantile(0.95,
  sum by (le) (increase(portal_http_request_duration_seconds_bucket{method="GET",route="/search"}[7d]))
)
```

### Portal replica 표본 분석의 별도 계약

[slo-sample-summary.py](../infra/k8s/tools/slo-sample-summary.py)는 저장한 Prometheus matrix JSON을 읽기만 함. 서비스 전체 가동 시간이나 검색 성공률을 계산하는 도구가 아님.

| 입력·출력 | 계약 | 비고 |
|---|---|---|
| 기간 | UTC epoch 정수 start/end, 15~300초 step, 최대 7일, 기간은 step 배수 | 양 끝 평가점 포함, 최대 40,321개 |
| 필수 질의 | available/desired replica와 각각 동일 selector의 `timestamp(...)` | namespace=personal-server·deployment=portal-web, 단일 series 및 원본 label 일치 요구 |
| 원본 시각 | 평가 시각보다 미래이면 unknown, 원본 age 60초 초과이면 unknown | 정확히 60초는 허용. 별도 timestamp가 없어도 unknown |
| 실패 | available 0 | desired 0·available 0은 계획 정지 후보로만 세고 원시 실패에서 빼지 않음 |
| 관측 실패 | missing·nonfinite·stale·future_timestamp | desired 누락도 결합 grid 전체를 unknown으로 보류함 |
| 비율 | observed_sample_success_percent와 coverage_percent를 함께 표시 | unknown이 있으면 complete_grid_success_percent는 null |
| 금지 해석 | bad × step을 정확한 중단 시간으로 환산, desired 0을 계획 정지로 확정, 표본 비율을 30일 SLO·에러 버짓으로 사용 | budget은 항상 not_calculable |
| 입력 보호 | 최대 32MiB, API success/matrix·단일 series·grid·중복/순서·replica 수·일치 label 검증 | 응답 warning/info는 보류. 잘못된 입력 오류에 labels·경로·예외 원문을 출력하지 않음 |

입력 top-level은 `schema_version:1`, `start`, `end`, `step_seconds`, `queries`임. queries에는 `available`, `desired`, `available_timestamp`, `desired_timestamp`의 API response를 넣음. 앞의 두 PromQL selector는 각각 `kube_deployment_status_replicas_available`·`kube_deployment_spec_replicas`이고, 뒤의 두 질의는 동일 selector를 `timestamp(...)`로 감쌈. 실제 수집 시 selector·평가 시각·응답 출처를 별도로 확인해야 하며 오프라인 형식 검사만으로 원본 진위를 보장하지 않음.

```sh
python3 infra/k8s/tools/slo-sample-summary.py --input /tmp/portal-sample-input.json
```

## 4. 검토 결과

2026-10-27 후속 구현은 [검색·관측·격리 복원 기록](reviews/20261027_신뢰성3종고도화_배포전검증.md)을 따름. 요청4/소스결과21 고정 series를 초기0으로 노출하고 실제 요청만 계수함. 호출자 취소와 서비스 시간 초과·부분 실패를 구분하며 단독 검색 실습 로더를 유지함. 프로세스 재시작 시 counter가 초기화되므로 increase/reset·수집 누락 대조가 필요함.

검색어·사용자 식별자·URL query를 label로 추가하지 않음. 기존 검색 경로·앱 메트릭·운영 설정을 변경하지 않음. 새 표본 도구는 독립 회귀·실데이터 분석과 구현 미참여 전문 검토를 거치며 최종 결과는 [통합 기록](reviews/20261027_V1_신뢰성고도화_검증결과.md)에 구분함.

## 5. 확인 필요 사항

검색의 충분한 자연 요청 수·수집 완전성·새 계측의 운영 반영·운영 목표 수치는 확인 필요함. [기존 잠정 검토선](slo-baseline.md)의 7일 100건과 p95 2초는 검토 조건이지 이번에 승인·활성화한 경보가 아님. 인위적 검색 트래픽을 자연 기준선에 섞지 않음.

## 6. 후속 조치

기존 수집을 유지하며 유효한 실제 요청 분포가 확보될 때 지연 목표를 검토함. 새 지표의 운영 배포·scrape·실제 요청 근거를 확인하기 전에 의존 검색 완전성 SLI 값을 채우지 않음. 사용자 직접 설명·측정 경험은 에이전트 도구 실행과 별도로 기록함.
