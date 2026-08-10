# 뉴스 허브 UI 구현 보고서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 작업명 | News Hub UI — 나스닥 중요도 표시 및 Editorial Atlas 적용 |
| 작업일 | 2026-08-10 |
| 작업 범위 | `crawler-worker` 라우트 화면 템플릿, 스타일, 회귀 테스트 |
| 제외 범위 | 서버 실행 코드, 스케줄러, RSS 수집, 15분 수집 주기, Telegram 전송 정책, API 형식 |

## 핵심 요약

- `KR_WORLD`(Investing.com 한국어) 기사에 저장된 `nasdaq_relevance.level` 및 `reasons`를 화면에 표시함.
- `alert` 기사는 `텔레그램 알림 대상`, `archive` 기사는 `보관 전용`으로 시각 구분함.
- 저장 뉴스 화면에서도 `KR_WORLD` 기사만 분류 상태를 표시하며, IT·AI 기사는 표시 대상에서 제외함.
- 자동 새로고침으로 목록이 갱신되는 경우에도 동일한 상태·사유 표시가 유지되도록 클라이언트 렌더링을 반영함.
- crawler-worker 전체 회귀 48건을 통과함.

## TDD 결과

| 단계 | 수행 내용 | 결과 |
|---|---|---|
| RED | KR_WORLD의 alert/archive 표시 및 보관함의 KR_WORLD 한정 표시 테스트 2건 추가 | 기능 미구현 상태에서 2건 실패 확인 |
| GREEN | Jinja 템플릿 및 자동 새로고침 렌더러에 분류 상태·사유 표시 추가 | 대상 테스트 2건 통과 |
| 검토 보완 RED | 독립 검토에서 확인된 손상된 `reasons` 정수값의 화면 500 회귀 테스트 2건 추가 | 기능 보완 전 2건이 500으로 실패 확인 |
| 검토 보완 GREEN | 허용 등급의 mapping과 비문자열 sequence 사유만 템플릿에서 렌더링하도록 제한 | 관련 테스트 4건 통과 |
| 회귀 | `PYTHONPATH=.. python3 -m unittest discover -s ../tests/crawler_worker -p 'test_*.py'` | 48건 통과 |

## 상세 변경

| 구분 | 파일 | 변경 내용 | 검증 결과 |
|---|---|---|---|
| 분류 표시 | `crawler-worker/app/templates/search.html` | `KR_WORLD` 카드에 alert/archive 상태, 사유, 데이터 속성 표시 및 자동 새로고침 렌더링 반영 | 일치 |
| 보관함 표시 | `crawler-worker/app/templates/saved.html` | `KR_WORLD` 보관 기사에만 동일 분류 표시 | 일치 |
| 시각 방향 | `crawler-worker/app/templates/base.html`, `crawler-worker/app/static/css/style.css` | Editorial Atlas의 종이 질감, 얇은 구획선, 신호색 기반 상태 표현을 crawler-worker 범위에 한정 적용 | 일치 |
| 회귀 방지 | `tests/crawler_worker/test_news_routes.py` | 렌더링 상태·사유와 비대상 카테고리 미표시 계약 추가 | 일치 |

## 검토 결과

1차 독립 코드 검토에서 다음 Important 항목을 확인함.

| 등급 | 검토 항목 | 조치 |
|---|---|---|
| Important | 손상된 보관 JSON의 `nasdaq_relevance.reasons`가 정수인 경우 Jinja 반복 처리로 두 화면이 500을 반환할 수 있음 | `nasdaq_relevance`를 mapping 및 `alert`/`archive` 허용 등급으로 제한하고, 사유는 mapping·문자열이 아닌 sequence일 때만 반복하도록 보완함. KR_WORLD와 보관함의 500 방지 회귀 테스트 2건을 추가함. |

1차 판정은 보완 전 `Ready-to-merge 아님`이었음. 보완 커밋 후 동일 검토자에게 재검토 요청 예정.

## 확인 필요 사항

- 브라우저 실기동 화면 확인은 수행하지 않음. FastAPI 템플릿 응답과 crawler-worker 자동화 회귀로 검증함.
- 기존 Starlette `TemplateResponse` 호출 방식에서 발생하는 deprecation warning은 이번 변경 범위와 무관하며, 테스트 통과에 영향 없음.

## 후속 조치

- 독립 코드 검토 결과 확인 후 중요도 이슈가 있으면 수정 및 재검증 예정.
