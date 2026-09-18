# 전체 검색 상태 및 뉴스 수집 최신성 설계

## 목표

Portal 전체 검색에서 서비스 장애와 결과 없음을 구분하고, News Hub에서 최근 수집 상태를 사용자에게 표시함.

## 범위

| 구분 | 포함 | 제외 |
|---|---|---|
| 전체 검색 | 뉴스·YouTube·책 서비스별 성공·응답 없음 상태와 부분 결과 표시 | 검색 색인·검색 순위·공개 URL 변경 |
| 뉴스 최신성 | 마지막 시도·마지막 성공·연속 실패 수의 읽기 전용 API 및 화면 표시 | 수집 스케줄·PVC·수집 규칙·자동복구 변경 |
| 보안 | 내부 endpoint·예외 상세를 화면에 노출하지 않음 | Secret·자격증명·권한 변경 |

## 설계

### 1. 전체 검색 상태

`portal-web`의 검색 서비스는 각 downstream을 독립적으로 호출함. 각 서비스 결과는 다음 계약을 가짐.

| 필드 | 타입 | 의미 |
|---|---|---|
| `items` | `list[dict]` | 정상 응답이면 검색 결과, 결과가 없으면 빈 목록 |
| `status` | `ok` 또는 `unavailable` | 요청 성공 여부 |

네트워크 오류, 시간 초과, 비정상 JSON 또는 결과 구조 오류는 `unavailable`로 처리함. 화면에는 서비스별로 `현재 응답 없음`만 표시하며 URL·예외 종류·내부 호스트는 표시하지 않음. 한 서비스가 `unavailable`이어도 다른 서비스의 결과는 그대로 표시함.

기존 `search_all()`의 외부 반환은 검색 결과와 상태를 함께 포함하는 새 구조로 변경하고, 이를 소비하는 dashboard route·template·테스트를 같은 변경에서 갱신함.

### 2. 뉴스 수집 최신성

`crawler-worker`는 기존 `NewsCollectionStatusStore` snapshot을 읽는 `GET /api/collection-status`를 제공함. 응답은 다음의 secret-free 상태만 포함함.

| 필드 | 의미 |
|---|---|
| `initialized` | 수집 시도가 한 번 이상 기록됐는지 여부 |
| `last_attempt_at` | 마지막 수집 시도 UTC ISO 8601 시각 또는 `null` |
| `last_success_at` | 마지막 성공 UTC ISO 8601 시각 또는 `null` |
| `consecutive_failures` | 연속 실패 횟수 |

News Hub 홈 화면은 상태 API가 아니라 같은 서버의 status store를 직접 읽어, 수집 상태를 제목 아래에 표시함. 표시 시각은 KST `YYYY-MM-DD HH:MM` 형식으로 변환함. 성공 시각이 없으면 `아직 정상 수집 기록 없음`, 연속 실패가 1 이상이면 `최근 수집 실패 N회`를 함께 표시함. 상태 파일 읽기 오류는 기존 store의 기본값을 사용하며 화면 오류를 발생시키지 않음.

## 오류 처리

- 전체 검색의 downstream 실패는 해당 서비스에만 국한하고 HTTP 200 dashboard 응답을 유지함.
- 뉴스 상태 조회는 기존 저장소의 기본 상태를 재사용해 수집 화면 가용성을 우선함.
- 새 API와 화면은 수집 작업·Telegram 알림·Prometheus 메트릭의 동작을 바꾸지 않음.

## 검증 기준

1. 한 검색 서비스가 실패해도 다른 두 서비스의 결과와 `unavailable` 상태가 함께 반환됨.
2. 정상 빈 검색 결과는 `ok` 상태와 빈 목록으로 표시됨.
3. collection-status API는 허용된 네 필드만 반환함.
4. News Hub는 마지막 성공 시각을 KST 형식으로, 연속 실패 상태를 사용자 메시지로 표시함.
5. 기존 검색·뉴스 라우터 관련 테스트와 정적 diff 검사가 통과함.

## 확인 필요 사항

- relay 전용 Prometheus target 추가는 본 변경에서 제외함. relay 장애가 Telegram 전달 장애와 순환될 수 있어 별도 알림 경로 설계가 필요함.
