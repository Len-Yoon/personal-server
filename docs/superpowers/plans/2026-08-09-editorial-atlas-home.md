# Editorial Atlas 메인 화면 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 서비스 진입과 통합 검색을 보존하면서 개인 서버 메인 화면을 Editorial Atlas 디자인으로 전환함.

**Architecture:** 메인 화면 전용 클래스와 템플릿 데이터를 추가하여 다른 서비스 화면에 영향을 주지 않음. 라우터는 현재 연도와 화면 클래스만 제공하고, 기존 서비스 URL·검색·이벤트 기록 로직은 그대로 유지함. CSS는 `atlas-*` 접두사로 범위를 제한함.

**Tech Stack:** Python 3, FastAPI, Jinja2, vanilla CSS, vanilla JavaScript, unittest.

## Global Constraints

- 서버 기동 영역 및 스케줄러 영역은 수정하지 않음.
- 서비스 URL, 통합 검색 범위, 이벤트 기록 API 호출을 변경하지 않음.
- 공개 메인 화면에 실제 서버 상태, 파일 경로, 보안 이벤트를 표시하지 않음.
- 자동매매 결과지 카드는 비활성 상태를 유지하며 실제 링크 동작을 추가하지 않음.
- 현재 연도 표기는 Python `datetime.now().year` 값으로 렌더링함.
- 검증 명령은 `PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v`를 사용함.

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `portal-web/app/routers/dashboard.py` | 현재 연도와 메인 전용 CSS 클래스를 제공하고 기존 서비스·검색 데이터를 유지함 |
| `portal-web/app/templates/base.html` | 선택적 페이지/컨테이너 클래스를 렌더링함 |
| `portal-web/app/templates/dashboard.html` | Editorial Atlas 정보 구조와 기존 서비스 카드·통합 검색·이벤트 기록을 렌더링함 |
| `portal-web/app/static/css/style.css` | 범위 제한된 반응형 Editorial Atlas 스타일을 제공함 |
| `tests/test_portal_dashboard.py` | 동적 연도, 서비스 링크, 통합 검색, 비활성 카드의 회귀를 검증함 |

### Task 1: 메인 화면 데이터 계약과 실패 테스트 추가

**Files:**
- Modify: `portal-web/app/routers/dashboard.py:1-109`
- Modify: `tests/test_portal_dashboard.py:1-246`

**Interfaces:**
- Consumes: `dashboard(request: Request, q: str = "")`와 기존 `services` 목록.
- Produces: 템플릿 컨텍스트 `current_year: int`, `body_class: str`, `main_class: str`.

- [ ] **Step 1: 실패하는 렌더링 테스트를 작성함**

`PortalDashboardTests`에 아래 테스트를 추가함.

```python
from datetime import datetime

def test_dashboard_renders_editorial_atlas_with_existing_entry_points(self):
    app = self.load_app()
    with TestClient(app) as client:
        response = client.get("/")

    self.assertEqual(response.status_code, 200)
    self.assertIn('class="atlas-body"', response.text)
    self.assertIn(f"PRIVATE WORKSPACE · {datetime.now().year}", response.text)
    for url in ("/news", "/memo", "/books", "/files", "/admin/status"):
        self.assertIn(f'href="{url}"', response.text)
    self.assertIn('data-service-status="나중에"', response.text)
    self.assertIn('aria-disabled="true"', response.text)
    self.assertIn('class="atlas-search"', response.text)
    self.assertIn('name="q"', response.text)
    self.assertIn('data-track-event="global_search_submitted"', response.text)
```

- [ ] **Step 2: 테스트가 실패하는지 확인함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_dashboard_renders_editorial_atlas_with_existing_entry_points
```

Expected: FAIL. 현재 템플릿에는 `atlas-body`, 동적 연도, 비활성 카드 접근성 속성이 없음.

- [ ] **Step 3: 라우터에 전용 컨텍스트를 최소 추가함**

`dashboard.py`에 import를 추가하고 기존 `TemplateResponse` 컨텍스트에 아래 값을 추가함. 서비스 목록, `search_all` 호출, 호스트별 URL 분기는 변경하지 않음.

```python
from datetime import datetime

"body_class": "atlas-body",
"main_class": "atlas-main",
"current_year": datetime.now().year,
```

- [ ] **Step 4: 테스트를 재실행해 아직 템플릿 때문에 실패하는지 확인함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_dashboard_renders_editorial_atlas_with_existing_entry_points
```

Expected: FAIL. 컨텍스트는 준비됐지만 Template DOM은 아직 이전 구조임.

### Task 2: Editorial Atlas 템플릿과 기존 기능 연결

**Files:**
- Modify: `portal-web/app/templates/base.html:1-28`
- Modify: `portal-web/app/templates/dashboard.html:1-126`
- Test: `tests/test_portal_dashboard.py:11-246`

**Interfaces:**
- Consumes: `body_class`, `main_class`, `current_year`, `services`, `query`, `search_results`, `demo_mode`.
- Produces: 서비스 링크, 통합 검색, 검색 결과 링크, 이벤트 기록 데이터 속성을 유지한 `atlas-page` DOM.

- [ ] **Step 1: 템플릿 계약 검증을 추가해 실패시킴**

Task 1 테스트에 아래 DOM 계약을 추가함.

```python
self.assertIn('class="atlas-service-grid"', response.text)
self.assertIn('class="atlas-service-card"', response.text)
self.assertIn('data-track-event="service_opened"', response.text)
```

- [ ] **Step 2: 테스트 실패를 확인함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_dashboard_renders_editorial_atlas_with_existing_entry_points
```

Expected: FAIL. Editorial Atlas DOM 클래스가 아직 없음.

- [ ] **Step 3: `base.html`에 선택적 클래스만 추가함**

다른 서비스 화면의 기본 렌더링을 바꾸지 않도록 빈 기본값을 사용함.

```html
<body class="{{ body_class|default('') }}">
...
<main class="container {{ main_class|default('') }}">
    {% block content %}{% endblock %}
</main>
```

- [ ] **Step 4: `dashboard.html`을 실제 기능 기반 구조로 교체함**

아래 규칙을 적용함.

```jinja2
<header class="atlas-topbar">
    <span class="atlas-brand">LEN / PERSONAL SERVER</span>
    <span>PRIVATE WORKSPACE · {{ current_year }}</span>
</header>
<div class="atlas-service-grid">
{% for service in services %}
    {% if service.status == "운영중" %}
    <a class="atlas-service-card" href="{{ service.url }}"
       data-track-event="service_opened" data-track-target="{{ service.name }}">
    {% else %}
    <div class="atlas-service-card is-planned"
         data-service-status="{{ service.status }}" aria-disabled="true">
    {% endif %}
    <!-- icon, meta, name, description, status는 기존 service 데이터를 사용 -->
    {% if service.status == "운영중" %}</a>{% else %}</div>{% endif %}
{% endfor %}
</div>
<form class="atlas-search global-search" method="get" action="/"
      data-track-event="global_search_submitted">
    <input type="search" name="q" value="{{ query }}"
           placeholder="제목, 메모, 키워드로 검색" aria-label="통합 검색">
    <button type="submit">통합 검색</button>
</form>
```

기존 `search_results` 반복문과 `data-track-event="search_result_opened"`를 유지함. 기존 JavaScript `trackUserEvent`는 수정하지 않음.

- [ ] **Step 5: 전체 대시보드 테스트를 통과시킴**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v
```

Expected: PASS. 호스트별 URL, 관리자 상태, 검색 URL 정규화 테스트가 모두 통과함.

- [ ] **Step 6: 작업 단위 커밋을 생성함**

```bash
git add portal-web/app/routers/dashboard.py portal-web/app/templates/base.html portal-web/app/templates/dashboard.html tests/test_portal_dashboard.py
git commit -m "feat: 에디토리얼 아틀라스 메인 화면 적용"
```

### Task 3: 범위 제한 스타일과 반응형 검증

**Files:**
- Modify: `portal-web/app/static/css/style.css:1-1400`
- Test: `tests/test_portal_dashboard.py:11-246`

**Interfaces:**
- Consumes: Task 2의 `.atlas-body`, `.atlas-main`, `.atlas-page`, `.atlas-service-grid`, `.atlas-service-card`, `.atlas-search`.
- Produces: 데스크톱 3열, 태블릿 2열, 모바일 1열 레이아웃.

- [ ] **Step 1: 전용 CSS 선택자 존재 테스트를 추가해 실패시킴**

```python
from pathlib import Path

def test_editorial_atlas_styles_are_scoped_to_the_dashboard(self):
    stylesheet = (
        Path(__file__).resolve().parents[1]
        / "portal-web" / "app" / "static" / "css" / "style.css"
    ).read_text(encoding="utf-8")

    self.assertIn(".atlas-body", stylesheet)
    self.assertIn(".atlas-service-grid", stylesheet)
    self.assertIn(".atlas-service-card", stylesheet)
    self.assertIn("@media (max-width: 980px)", stylesheet)
```

- [ ] **Step 2: 테스트 실패를 확인함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard.PortalDashboardTests.test_editorial_atlas_styles_are_scoped_to_the_dashboard
```

Expected: FAIL. `atlas-*` 선택자가 아직 없음.

- [ ] **Step 3: `style.css` 마지막에 범위 제한 스타일을 추가함**

모든 신규 선택자를 `.atlas-body` 또는 `.atlas-*`로 시작함. 배경은 밝은 회백색, 텍스트는 짙은 잉크색, 강조 행동에는 연두색을 사용함. 기존 다크 서비스 화면의 CSS를 변경하지 않음.

```css
.atlas-body { color: #161918; background: #edece7; }
.atlas-body .background-orb { display: none; }
.atlas-body .atlas-main { width: min(1470px, calc(100% - 52px)); padding: 20px 0 38px; }
.atlas-body .atlas-service-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; }
.atlas-body .atlas-service-card { min-height: 223px; border: 1px solid rgba(22, 25, 24, 0.18); }
.atlas-body .atlas-service-card.is-planned { opacity: 0.52; cursor: not-allowed; }
.atlas-body .atlas-search { display: grid; grid-template-columns: 1fr auto; }

@media (max-width: 980px) {
    .atlas-body .atlas-service-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
    .atlas-body .atlas-main { width: min(100% - 32px, 1470px); }
    .atlas-body .atlas-service-grid,
    .atlas-body .atlas-search { grid-template-columns: 1fr; }
}
```

히어로, 카드, 검색, 푸터, `:focus-visible` 상태도 동일 스코프에서 정의함. 키보드 포커스 outline을 제거하지 않음.

- [ ] **Step 4: 단위 테스트를 통과시킴**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest tests.test_portal_dashboard -v
```

Expected: PASS.

- [ ] **Step 5: 세 화면 너비에서 시각 검증함**

검증 대상: 1440px, 768px, 390px.

```text
- 운영 중 서비스 5개는 클릭 가능한 카드로 표시됨.
- 자동매매 결과지 카드는 비활성 상태이며 링크가 없음.
- 검색 입력과 버튼이 390px 화면에서 겹치지 않음.
- Tab 이동 시 서비스 카드와 검색 요소의 포커스가 보임.
- 관리자 상태 카드에는 실제 서버 수치나 인증 전 보안 정보가 없음.
```

- [ ] **Step 6: 포털 회귀 테스트를 실행함**

Run:

```bash
PYTHONPATH=portal-web python3 -m unittest discover -s tests -p 'test_portal_*.py' -v
```

Expected: PASS. 파일 접근, 포털 보안, 포트폴리오, 대시보드 테스트가 모두 통과함.

- [ ] **Step 7: 작업 단위 커밋을 생성함**

```bash
git add portal-web/app/static/css/style.css tests/test_portal_dashboard.py
git commit -m "style: 개인 서버 메인 화면 반응형 디자인 적용"
```

## 최종 검증

- [ ] `git diff --check`로 공백 오류가 없는지 확인함.
- [ ] 포털 회귀 테스트가 통과하는지 확인함.
- [ ] 1440px, 768px, 390px 브라우저 검토를 완료함.
- [ ] 운영 중 카드 5개와 통합 검색의 실제 링크·이벤트 기록 속성이 유지됨.
- [ ] 예정 카드가 비활성이고 인증 전 관리자 상세 정보가 없음.
