# 최종 리뷰 Important 보완 보고서

## 문서 정보

| 항목 | 내용 |
|---|---|
| 문서명 | 최종 리뷰 Important 보완 보고서 |
| 작성일 | 2026-08-10 |
| 작성자 | Codex |
| 기준 자료 | 최종 전체 브랜치 리뷰 Important 2건 |
| 목적 | 시장 충격 알림 오탐 및 CSS 캐시 버전 누락 보완 결과 보고 |
| 비고 | 서버 실행, 스케줄러, 인증 동작 제외 |

## 핵심 요약

- `나스닥 급락 가능성 커져`, `미국 기술주 폭락 전망`을 보관 전용(`archive`)으로 분류하도록 보완함.
- 기사 필드 및 충격 키워드 출현을 개별 평가하여 확정 급락·폭락·서킷브레이커는 알림(`alert`)을 유지함.
- 포털과 뉴스 허브 기본 템플릿의 CSS 캐시 버전을 `20260810-final-review-1`로 갱신함.
- 최종 자동화 검증 43건(크롤러 23건, 포털 20건)이 통과함.
- 독립 최종 재검토 결과 Critical/Important/Minor 없음, `Ready to commit: Yes` 판정됨.

## TDD 결과

| 단계 | 수행 내용 | 결과 | 비고 |
|---|---|---|---|
| RED 1 | 전망성 시장 충격 문구 2건과 확정 사건 보호 테스트 추가 후 `python3 -m unittest tests.crawler_worker.test_nasdaq_relevance -v` 실행 | 11건 중 신규 전망성 2건 실패 확인 | 두 사례가 `alert`로 오분류됨 |
| GREEN 1 | 시장 충격 전망성 문구 제외를 기사 필드 단위로 적용 | 11건 통과 | 원 요구 사례 보완 |
| 검토 보완 RED 2 | 확정 사건과 무관한 전망이 같은 필드에 있는 사례 추가 | 13건 중 혼합 사례 2개 하위 사례 실패 확인 | 필드 전체 전망 배제 문제 확인 |
| 검토 보완 GREEN 2 | 전망 한정어 범위를 충격 표현 인접 구간으로 축소 | 13건 통과 | 확정 사건 보존 |
| 검토 보완 RED 3 | `서킷브레이커 발동 가능성/전망`, 전망성 급락 뒤 실제 급락 사례 추가 | 15건 중 3개 하위 사례 실패 확인 | `발동` 및 다중 출현 경계 확인 |
| 검토 보완 GREEN 3 | 시장 주체와 40자 이내인 충격 키워드 출현을 개별 검사하고 각 출현 직후 전망 한정어만 배제 | 15건 통과 | 확정·전망 혼합 문구 보존 |

## 상세 변경

| 구분 | 파일 | 변경 내용 | 검증 결과 | 비고 |
|---|---|---|---|---|
| 분류 로직 | `crawler-worker/app/services/nasdaq_relevance.py` | 시장 충격 주체·사건·전망 정규식을 분리하고 사건 출현 단위로 확인 여부 판정 | 일치 | 기존 거시경제·반도체 분기 유지 |
| 회귀 테스트 | `tests/crawler_worker/test_nasdaq_relevance.py` | 원 요구 2건, 확정 사건 3건, 필드/출현 혼합 및 `발동` 경계 사례 추가 | 일치 | 실제 분류 함수 호출 |
| CSS 캐시 | `portal-web/app/templates/base.html` | CSS 쿼리 버전을 `20260810-final-review-1`로 갱신 | 일치 | 스타일·라우트 로직 미변경 |
| CSS 캐시 | `crawler-worker/app/templates/base.html` | CSS 쿼리 버전을 `20260810-final-review-1`로 갱신 | 일치 | 스타일·라우트 로직 미변경 |

## 검증 결과

| 검증 항목 | 명령 | 결과 | 비고 |
|---|---|---|---|
| 크롤러 관련성·라우트 | `PYTHONPATH=.. python3 -m unittest tests.crawler_worker.test_nasdaq_relevance tests.crawler_worker.test_news_routes -v` (`crawler-worker` 디렉터리) | 23건 통과 | Starlette 기존 deprecation warning 1건 발생 |
| 포털 라우트 | `python3 -m unittest tests.test_portal_dashboard -v` | 20건 통과 | Starlette 기존 deprecation warning 1건 발생 |
| 형식 검증 | `git diff --check` | 통과 | 공백 오류 없음 |
| 변경 범위 | `git diff --name-only` | 요구된 서비스·템플릿·테스트 파일만 확인 | 서버 실행·스케줄러·인증 파일 없음 |

## 독립 검토 결과

| 차수 | 등급 | 검토 내용 | 조치 | 결과 |
|---|---|---|---|---|
| 1차 | Important | 필드 전체 전망 배제로 같은 필드의 확정 사건이 숨겨질 수 있음 | 혼합 필드 회귀 테스트 및 한정어 범위 축소 | 보완 완료 |
| 2차 | Important | `발동`이 낀 전망 문구 및 한 필드 내 다중 충격 출현을 정확히 구분하지 못함 | 사건 출현 단위 판정과 경계 테스트 추가 | 보완 완료 |
| 최종 | 없음 | Critical/Important/Minor 잔여 지적 없음 | 추가 조치 없음 | `Ready to commit: Yes` |

## 확인 필요 사항

- 브라우저 실기동 캐시 교체 확인은 수행하지 않음. 템플릿 URL 변경 및 포털·크롤러 라우트 렌더링 회귀로 검증함.
- 기존 Starlette `TemplateResponse` 호출 방식 deprecation warning은 이번 변경 범위와 무관하며 테스트 결과에 영향 없음.
- 저장소 루트에서 포털·크롤러 라우트를 한 프로세스로 실행할 경우 크롤러의 상대 정적 경로(`app/static`) 때문에 오류가 발생함. 기존 실행 구조를 변경하지 않고 서비스 디렉터리 기준으로 검증함.

## 후속 조치

- 추가 조치 없음.
