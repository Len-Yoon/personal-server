# 포털 상태 시간 파서 P1 수정 보고서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 포털 상태 시간 파서 P1 수정 보고서 |
| 작성일 | 2026-08-25 |
| 기준 자료 | KST 표시 통일 최종 릴리스 P1 지적, Task 1 관련 보고서 및 진행 현황 |
| 목적 | 포털 상태 시각의 naive ISO 8601·RFC 822 해석을 KST 표시 기준으로 통일함 |
| 작업 범위 | `portal-web/app/services/admin_status.py`, `tests/test_portal_dashboard.py` |
| 제외 범위 | 저장·정렬, 서버 기동, 스케줄러, 보안 이벤트 저장 형식 |

## 2. 핵심 요약

- 시간대가 없는 ISO 8601 값은 기존 KST 가정 대신 UTC로 해석하여 KST로 변환함.
- RFC 822 UTC 값도 ISO 파싱 실패 시 표준 파서로 해석하여 KST 표시값을 반환함.
- 시간대 인식 ISO, 기존 `YYYY-MM-DD HH:MM:SS KST`, 빈 값 및 파싱 불가 값의 기존 계약을 유지함.

## 3. TDD 및 변경 결과

| 단계 | 수행 내용 | 결과 |
|---|---|---|
| RED | naive ISO `2026-07-09T01:02:03`과 RFC 822 `Fri, 10 Jul 2026 15:58:15 +0000` 회귀 테스트 추가 | 각각 `2026-07-09 01:02`, `unknown`으로 실패 확인 |
| GREEN | ISO 우선 파싱 후 RFC 822 표준 파서 보완, naive 값에 UTC 지정 | 두 신규 테스트와 기존 aware ISO·KST 레거시·오류 입력 테스트 통과 |

| 구분 | 파일 | 변경 내용 | 검증 결과 |
|---|---|---|---|
| 표시 파서 | `portal-web/app/services/admin_status.py` | ISO 실패 시 RFC 822 파서를 사용하고 naive 시각은 UTC로 지정 | 일치 |
| 회귀 테스트 | `tests/test_portal_dashboard.py` | naive ISO UTC 해석 및 RFC 822 UTC KST 변환 테스트 추가 | 일치 |

## 4. 검증 결과

| 검증 항목 | 명령 | 결과 |
|---|---|---|
| 단위 RED | `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_treats_naive_iso_as_utc tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_formats_rfc822_utc_as_kst -v` | 수정 전 2건 실패 확인 |
| 단위 GREEN | `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_is_formatted_for_display tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_treats_naive_iso_as_utc tests.test_portal_dashboard.PortalDashboardTests.test_admin_status_checked_at_formats_rfc822_utc_as_kst -v` | 3건 통과 |
| 포털 회귀 | `python3 tests/run_service_tests.py --suite portal` | 97건 통과 |
| 공백 검사 | `git diff --check` | 통과 |

## 5. 확인 필요 사항

- 포털 테스트 실행 시 기존 Starlette `TemplateResponse` deprecation warning이 출력됨. 본 변경과 무관하며 서버 기동 코드 변경 금지 범위에 따라 미조치함.

## 6. 후속 조치

- 추가 조치 없음.

## 7. 에이전트 운영 기록

| 역할 | 사용 여부 | 담당 범위 | 결과 |
|---|---|---|---|
| 주 에이전트 | 사용 | 원인 분석, TDD, 구현, 검증, 보고서 작성 | 완료 |
| 하위 에이전트 | 미사용 | 사용자 지시에 따라 하위 에이전트 사용 제외 | 해당 없음 |
