# 포트폴리오 이력서 UI 고도화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 마크다운 기반 포트폴리오를 절제된 고급감의 전문 이력서 화면으로 개선함.

**Architecture:** 공용 `portfolio.css`에 시각 토큰과 컴포넌트별 스타일을 정의함. 공개 문서, 로그인, 편집기 및 미리보기는 기존 HTML 구조와 공용 클래스를 유지하면서 동일한 스타일 토큰을 공유함.

**Tech Stack:** FastAPI Jinja 템플릿, CommonMark 렌더링, CSS, pytest

## Global Constraints

- 서버 구동 및 스케줄러 영역은 수정하지 않음.
- 외부 폰트·이미지·JavaScript 의존성을 추가하지 않음.
- 기존 마크다운 저장·렌더링 및 라우팅 로직을 변경하지 않음.
- 600px 이하 화면에서 이력서와 관리자 입력 화면의 가독성을 유지함.

---

### Task 1: 이력서 문서 스타일 고도화

**Files:**
- Modify: `portal-web/app/static/css/portfolio.css`
- Test: `tests/test_portfolio.py`

**Interfaces:**
- Consumes: `.portfolio-shell`, `.portfolio-content`, `.portfolio-empty` 및 CommonMark가 생성하는 HTML 요소
- Produces: 공개 이력서에 적용되는 문서형 색상, 여백, 제목, 표, 목록, 인용문, 코드 스타일

- [x] **Step 1: 기존 CSS 적용 범위를 확인하는 테스트 실행**

Run: `pytest tests/test_portfolio.py -v`
Expected: PASS

- [x] **Step 2: 공개 문서 CSS를 갱신**

`portfolio.css`의 색상 토큰을 청회색 배경·네이비 포인트로 변경하고, 문서 카드 상단 장식, 제목·섹션 구분선, 표, 목록, 인용문, 인라인 코드 및 코드 블록을 스타일링함.

- [x] **Step 3: 포트폴리오 테스트 실행**

Run: `pytest tests/test_portfolio.py -v`
Expected: PASS

### Task 2: 관리자 화면 및 반응형 완성도 유지

**Files:**
- Modify: `portal-web/app/static/css/portfolio.css`
- Test: `tests/test_portfolio.py`

**Interfaces:**
- Consumes: `.admin-shell`, `.admin-card`, `.editor-shell`, `form`, `input`, `textarea`, `button`, `.preview`
- Produces: 공개 화면과 통일된 관리자 화면 및 600px 이하 반응형 스타일

- [x] **Step 1: 관리자 공용 요소 스타일을 갱신**

입력 포커스, 버튼 호버, 미리보기, 관리자 카드를 공용 시각 토큰에 맞춰 정돈함.

- [x] **Step 2: 전체 포트폴리오 테스트 실행**

Run: `pytest tests/test_portfolio.py -v`
Expected: PASS

- [x] **Step 3: 변경 파일 점검 및 커밋**

Run: `git diff --check && git status --short`
Expected: 공백 오류 없음, CSS와 계획 파일만 변경됨

Run: `git add portal-web/app/static/css/portfolio.css docs/superpowers/plans/2026-07-17-portfolio-resume-premium-ui.md && git commit -m "style: 포트폴리오 이력서 UI 고도화"`
