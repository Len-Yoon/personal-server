# 뉴스 수집 장애 관측성 설계서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 뉴스 수집 장애 관측성 설계서 |
| 작성일 | 2026-09-07 |
| 대상 | `crawler-worker`의 정기 뉴스 수집 |
| 목적 | 수집 실패를 컨테이너 생존 상태와 분리해 감지하고 기존 SRE Telegram 경로로 전달함 |

## 핵심 요약

현재 수집 예외가 숨겨져 있어 `/health`와 Docker healthcheck가 정상이어도 뉴스 수집 실패를 확인할 수 없음. 수집 결과를 비밀값 없는 상태 파일로 기록하고, 내부 인증된 Prometheus 지표와 Alertmanager를 통해 기존 SRE relay로 장애·복구를 알림.

## 설계 결정

| 구분 | 결정 | 비고 |
|---|---|---|
| 상태 저장 | `news_collection_status.json`에 마지막 시도·성공·실패 시각과 실패 횟수 저장 | UTC ISO 8601, 원자적 저장 |
| 수집 실패 | 정기 수집 경로에서 원본 수집 예외를 성공으로 처리하지 않음 | 사용자 요청 경로의 기존 fallback 동작은 유지 |
| 지표 노출 | `/internal/metrics`에서 Prometheus 형식 제공 | bearer token 없거나 불일치 시 404 |
| 접근 경계 | K3s `ServiceMonitor`만 `compose-crawler` bridge를 통해 내부 수집 | token 값은 Git·로그·문서에 저장하지 않음 |
| 경고 | 최근 성공 15분 초과 또는 3회 연속 실패 시 `NewsCollectionStale` | 시작 후 15분 유예 |
| 알림 | 기존 Alertmanager → SRE relay → Telegram 재사용 | 장애·복구 모두 한국어로 전달 |
| 인증 시딩 | monitoring namespace Secret `crawler-news-metrics`의 `bearer_token`과 crawler runtime `NEWS_METRICS_BEARER_TOKEN`에 동일한 승인된 값을 별도 절차로 주입 | 값 자체는 Git·문서·로그에 기록하지 않음 |
| 자동 조치 | 자동 재시작·자동 복구를 수행하지 않음 | 관측과 알림만 수행 |

## 지표 계약

```text
crawler_news_collection_initialized
crawler_news_collection_last_attempt_timestamp_seconds
crawler_news_collection_first_attempt_timestamp_seconds
crawler_news_collection_last_success_timestamp_seconds
crawler_news_collection_last_failure_timestamp_seconds
crawler_news_collection_failures_total
crawler_news_collection_consecutive_failures
crawler_news_refresh_interval_seconds
```

`crawler_news_collection_first_attempt_timestamp_seconds`는 초기 수집 후 15분 유예를 적용하기 위한 기준 시각임.

기사 제목·URL·원문 예외·Telegram token·chat ID는 지표, 라벨, 알림에 포함하지 않음.

## 검증·롤백

- 원본 수집 예외 시 실패 상태만 증가하는지 테스트함.
- 빈 기사 목록은 정상 수집 성공으로 처리하는지 테스트함.
- 지표 응답이 bearer token 없이 노출되지 않는지 테스트함.
- `ServiceMonitor`, `PrometheusRule`, relay 한국어 표현의 정적 계약을 테스트함.
- 적용 대상은 새 `ServiceMonitor`와 갱신된 `PrometheusRule`이며 Secret 값은 별도 승인된 방식으로만 시딩함.
- 검증은 `kubectl -n monitoring get servicemonitor crawler-news-observability`, `kubectl -n monitoring get prometheusrule sre-telegram-k3s-alerts` 및 인증된 metrics 확인으로 수행함.
- 롤백은 `ServiceMonitor` 제거와 `NewsCollectionStale` 규칙 제거 후 crawler 직전 이미지 복귀 순서로 수행함.
- 실제 적용은 배포 전 별도 승인 후 수행함. 롤백은 crawler 직전 이미지로 복귀하고 새 `ServiceMonitor`와 `PrometheusRule`만 제거함.
