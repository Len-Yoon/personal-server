# 뉴스 오류 시간 비표시 수정 보고서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 뉴스 오류 시간 비표시 수정 보고서 |
| 작성일 | 2026-08-25 |
| 작업 범위 | 뉴스 서버 렌더링 및 자동 새로고침의 오류 시간 표시 |
| 기준 자료 | Task 2 brief/report, 자동 새로고침 테스트 후속 보고서, SDD progress 최종 통합 P1 |
| 제외 범위 | 서버 기동, 스케줄러, 기사 저장 원문, 정렬 로직 |
| 목적 | 파싱 불가·`None`·빈 뉴스 시간의 원문 노출 방지 |

## 2. 핵심 요약

- 서버 포맷터가 파싱 불가 시간과 빈 값을 빈 문자열로 반환하도록 변경함.
- Jinja 템플릿은 포맷 결과가 있는 경우에만 시간 요소를 렌더링하도록 변경함.
- 자동 새로고침은 ISO 8601 또는 RFC 822 형식만 처리하고, 형식 불일치 또는 날짜 해석 실패 시 시간을 표시하지 않도록 변경함.
- 유효 ISO 8601·RFC 822 UTC 입력은 기존과 동일하게 KST `YYYY-MM-DD HH:MM`으로 표시됨.

## 3. 원인 및 조치 결과

| 구분 | 원인 | 조치 | 검증 결과 |
|---|---|---|---|
| 서버 포맷터 | 파싱 실패 시 원문을 반환함 | 빈 문자열 반환으로 변경함 | 오류·빈 값 회귀 테스트 통과함 |
| 서버 렌더링 | 원문 존재 여부만 확인한 뒤 필터 결과를 출력함 | 포맷 결과 존재 시에만 시간 요소를 출력함 | 실제 `/category` HTML에서 UTC 원문·초·KST 미노출 확인함 |
| 자동 새로고침 | 브라우저 `Date`의 느슨한 해석과 오류 시 원문 반환이 존재함 | ISO/RFC 822 입력만 허용하고 실패 시 빈 문자열 반환함 | 실제 Node 렌더링 테스트에서 오류 시간 미노출 확인함 |

## 4. RED/GREEN 검증 결과

| 단계 | 실행 명령 | 결과 | 확인 내용 |
|---|---|---|---|
| RED | `PYTHONPATH=crawler-worker python3 -m unittest tests.crawler_worker.test_datetime_format tests.crawler_worker.test_news_routes.CrawlerWorkerNewsRouteTests.test_category_page_hides_unparseable_article_times` | 실패 | 서버 포맷터와 실제 Jinja HTML에 `2026-07-09 01:02:03 UTC` 원문이 노출됨 |
| RED | `node --test tests/news_auto_refresh_client.test.mjs` | 실패 | 브라우저가 비표준 UTC 문자열을 느슨하게 해석하고, 오류 ISO 원문을 반환함 |
| GREEN | 동일 대상 테스트 | 통과 | 서버·Jinja·자동 새로고침 모두 오류 시간 비표시 확인함 |
| 전체 검증 | `python3 tests/run_service_tests.py --suite crawler-worker` | 통과 | Python 75개 및 Node 2개 테스트 통과함 |
| 개별 검증 | `node --test tests/news_auto_refresh_client.test.mjs` | 통과 | 자동 새로고침 Node 2개 테스트 통과함 |
| 형식 검증 | `git diff --check` | 통과 | 공백 오류 없음 |

## 5. 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `crawler-worker/app/services/datetime_format.py` | 파싱 불가·빈 뉴스 시간을 빈 문자열로 반환하도록 변경함 |
| `crawler-worker/app/templates/search.html` | 서버 시간 요소의 조건 렌더링 및 자동 새로고침 오류 시간 비표시 처리 추가함 |
| `tests/crawler_worker/test_datetime_format.py` | `None`·빈 값·파싱 불가 시간의 비표시 회귀 테스트 추가함 |
| `tests/crawler_worker/test_news_routes.py` | 실제 카테고리 HTML의 오류 시간 원문 미노출 회귀 테스트 추가함 |
| `tests/news_auto_refresh_client.test.mjs` | 실제 자동 새로고침 렌더링의 오류 시간 비표시 회귀 테스트 추가함 |

## 6. 확인 필요 사항

- crawler-worker Python 테스트 실행 시 기존 Starlette `TemplateResponse` 인자 순서 관련 `DeprecationWarning`이 발생함. 본 변경과 무관하며 테스트는 통과함.

## 7. 후속 조치

- 없음.
