# Task 3 P1 숨김 처리 오류 수정 결과 보고

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | Task 3 P1 숨김 처리 오류 수정 결과 보고 |
| 작성일 | 2026-08-25 |
| 기준 자료 | `task-3-brief.md`, `task-3-parser-fix-report.md`, `task-3-report.md`, `progress.md` |
| 목적 | 파싱 불가 메모 생성 시각의 원문 노출을 방지하고 KST 표시 계약을 검증함 |
| 비고 | SQLite 저장값·정렬, main·템플릿, 서버 기동 및 스케줄러는 변경하지 않음 |

## 2. 핵심 요약

| 구분 | 결과 |
|---|---|
| 원인 | 두 서비스 formatter의 파싱 실패 예외 처리에서 입력 원문을 반환함 |
| 조치 | `TypeError` 또는 `ValueError` 발생 시 빈 문자열을 반환하도록 변경함 |
| 표시 계약 | naive UTC 및 시간대 인식 ISO 값은 KST `YYYY-MM-DD HH:MM` 표시를 유지함 |
| 오류 입력 | `None`, 빈 문자열, 파싱 불가 값 및 UTC-like 값은 빈 문자열로 비표시됨 |

## 3. 상세 변경 내용

| 서비스 | 변경 파일 | 변경 내용 | 검증 결과 |
|---|---|---|---|
| book-memo | `book-memo/app/services/datetime_format.py` | 파싱 실패 시 원문 반환을 빈 문자열 반환으로 변경함 | 일치 |
| youtube-memo | `youtube-memo/app/services/datetime_format.py` | 파싱 실패 시 원문 반환을 빈 문자열 반환으로 변경함 | 일치 |
| book-memo | `tests/book_memo/test_ui_contract.py` | 유효 naive/aware KST 표시 및 파싱 불가 값 비표시를 검증하고, DB의 UTC-like 오류 값을 넣은 상세 페이지 HTTP 200·원문/UTC/초 미노출을 검증함 | 일치 |
| youtube-memo | `tests/youtube_memo/test_ui_contract.py` | 유효 naive/aware KST 표시 및 파싱 불가 값 비표시를 검증하고, DB의 UTC-like 오류 값을 넣은 상세 페이지 HTTP 200·원문/UTC/초 미노출을 검증함 | 일치 |

## 4. TDD 수행 결과

| 단계 | 결과 |
|---|---|
| RED | `2026-07-09 01:02:03 UTC` 및 `not-a-datetime` 입력이 원문으로 반환되고, book·YouTube 상세 HTML에 UTC-like 원문이 노출되는 실패를 확인함 |
| GREEN | 두 formatter의 예외 반환을 빈 문자열로 최소 변경한 후 UI 계약 테스트 38건이 통과함 |

## 5. 검토 결과

| 검증 항목 | 결과 |
|---|---|
| `python3 -m unittest tests.book_memo.test_ui_contract tests.youtube_memo.test_ui_contract` | 38건 통과 |
| `python3 tests/run_service_tests.py --suite book-memo` | 23건 통과 |
| `python3 tests/run_service_tests.py --suite youtube-memo` | 24건 통과 |
| `git diff --check` | 공백 오류 없음 |

## 6. 확인 필요 사항

- Starlette `TemplateResponse` 호출 형식 관련 DeprecationWarning이 기존 테스트 실행 시 출력됨. 본 작업의 시간 표시 범위와 무관하며, 서버 기동 코드 변경 금지 범위에 따라 미조치함.

## 7. 후속 조치

- 없음.
