# Final Review Important Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 시장 충격 전망성 기사 오탐을 차단하고 변경된 포털·뉴스 허브 CSS가 즉시 갱신되도록 함.

**Architecture:** 나스닥 관련성 분류기는 시장 충격 키워드가 포함된 각 기사 필드를 독립적으로 검사하고, 같은 필드가 전망성 표현이면 알림에서 제외함. 템플릿의 정적 CSS URL 쿼리 버전은 2026-08-10 변경분을 나타내는 새 값으로 갱신함.

**Tech Stack:** Python 3, `unittest`, FastAPI/Jinja2, HTML/CSS

## Global Constraints

- `서버 띄우는 쪽`과 `스케줄러 쪽`은 수정하지 않음.
- 인증 동작을 수정하지 않음.
- 원본 요구사항에 없는 동작을 추가하지 않음.
- 커밋 메시지는 한국어 `유형: 설명` 형식을 사용함.

---

### Task 1: 시장 충격 전망성 기사 분류 보정

**Files:**
- Modify: `tests/crawler_worker/test_nasdaq_relevance.py`
- Modify: `crawler-worker/app/services/nasdaq_relevance.py`

**Interfaces:**
- Consumes: `classify_nasdaq_relevance(article: dict) -> dict[str, object]`
- Produces: 전망성 급락·폭락 문구는 `archive`, 확인된 급락·폭락·서킷브레이커 문구는 `alert`

- [ ] **Step 1: 실패하는 회귀 테스트 작성**

  `나스닥 급락 가능성 커져`, `미국 기술주 폭락 전망`을 각각 분류하여 `level == "archive"`를 확인함. `나스닥 급락`, `미국 기술주 폭락`, `나스닥 서킷브레이커 발동`은 `level == "alert"`를 확인함.

- [ ] **Step 2: RED 확인**

  Run: `python -m unittest tests.crawler_worker.test_nasdaq_relevance -v`

  Expected: 신규 전망성 사례 2건은 `alert != archive`로 실패하고 확인된 사건 사례는 통과함.

- [ ] **Step 3: 최소 구현**

  `MARKET_SHOCK_OUTLOOK_PATTERNS`에 `가능성|전망`을 정의하고, `_has_confirmed_market_shock(article)`에서 각 텍스트 필드별로 시장 충격 패턴 일치 및 전망 패턴 불일치를 확인함. 분류 함수는 기존 전체 텍스트 정규식 분기 대신 이 helper를 호출함.

- [ ] **Step 4: GREEN 확인**

  Run: `python -m unittest tests.crawler_worker.test_nasdaq_relevance -v`

  Expected: 전체 통과함.

### Task 2: CSS 캐시 버전 갱신

**Files:**
- Modify: `portal-web/app/templates/base.html`
- Modify: `crawler-worker/app/templates/base.html`

**Interfaces:**
- Consumes: 브라우저가 요청하는 `/static/css/style.css?v=...`
- Produces: 포털과 뉴스 허브가 `v=20260810-final-review-1` URL을 사용함.

- [ ] **Step 1: 캐시 버전 갱신**

  두 템플릿의 `style.css` 쿼리 버전을 `20260810-final-review-1`로 변경함. 이 변경은 설정성 URL 갱신이므로 별도 소스 문자열 테스트를 추가하지 않고 실제 라우트 렌더링 회귀 테스트로 검증함.

- [ ] **Step 2: 포털·크롤러 라우트 회귀 테스트**

  Run: `python -m unittest tests.test_portal_dashboard tests.crawler_worker.test_news_routes -v`

  Expected: 전체 통과함.

### Task 3: 전체 검증·독립 재검토·커밋

**Files:**
- Create: `.superpowers/sdd/final-review-fixer-report.md`

**Interfaces:**
- Consumes: RED/GREEN 출력, 회귀 테스트 출력, git diff, 독립 리뷰 결과
- Produces: 최종 작업 근거 보고서와 한국어 커밋

- [ ] **Step 1: 변경 범위 및 금지 영역 확인**

  `git diff --check`, `git diff --name-only`, 관련 테스트를 실행하고 서버 실행·스케줄러·인증 파일이 변경되지 않았는지 확인함.

- [ ] **Step 2: 독립 범위 재검토 요청**

  리뷰어에게 시장 충격 분류, CSS 캐시 버전, 금지 영역 준수, 테스트 충분성만 읽기 전용으로 검토 요청함. Critical/Important 지적은 커밋 전 해소함.

- [ ] **Step 3: 보고서 작성 및 최종 재검증**

  `.superpowers/sdd/final-review-fixer-report.md`에 RED/GREEN/회귀 테스트/리뷰 결과를 기록하고 동일 검증 명령을 새로 실행함.

- [ ] **Step 4: 한국어 커밋**

  변경 파일만 명시적으로 스테이징하고 `fix: 시장 충격 알림 오탐 및 CSS 캐시 보정`으로 커밋함.
