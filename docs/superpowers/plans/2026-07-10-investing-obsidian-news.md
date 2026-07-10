# Investing.com Obsidian 뉴스 수집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Playwright Chromium으로 한국어 Investing.com 뉴스 목록의 제목·시간·링크·출처만 하루 한 번 수집해 Obsidian Vault의 날짜별 Markdown 파일로 저장한다.

**Architecture:** 기존 `crawler-worker` API와 분리된 `investing-crawler` 일회성 컨테이너를 추가한다. Python 소스 디렉터리는 `investing_crawler`로 두고, 순수 HTML 파서와 Markdown 저장기를 먼저 테스트한다. Playwright 실행기는 목록 페이지 하나만 열어 파서에 HTML을 전달한다. N100에서는 Windows 작업 스케줄러가 `docker compose run --rm investing-crawler`를 하루 한 번 실행한다.

**Tech Stack:** Python 3.11, Playwright Python, BeautifulSoup4, Docker Compose, unittest.

> **Implementation amendment (2026-07-11):** Cloudflare 보안 확인으로 Playwright 직접 수집이 운영 불가함을 확인하여, 구현은 Google News RSS의 `site:kr.investing.com/news` 검색과 `Investing.com 한국어` 출처 필터 방식으로 전환했다. 아래 브라우저 관련 초기 단계는 전환 기록 문서(`docs/superpowers/specs/2026-07-11-investing-google-news-rss-amendment.md`)로 대체한다.

## Global Constraints

- Investing.com 한국어 목록에서 제목·게시 시간·링크·출처만 수집한다.
- 기사 상세 본문, AI 요약, 외부 AI API, Obsidian 앱 자동 실행은 포함하지 않는다.
- CAPTCHA·로그인·봇 차단 우회 코드는 넣지 않는다.
- Chromium은 단일 페이지에서만 실행하고 작업 완료 후 종료한다.
- 기존 `crawler-worker` RSS/API 흐름과 MT4 실행에는 변경을 가하지 않는다.
- 사용자 작업트리의 기존 `scripts/windows-bootstrap.ps1` 수정은 보존한다.

---

### Task 1: 순수 Investing 뉴스 파서와 Markdown 저장기 테스트 작성

**Files:**
- Create: `investing_crawler/app/__init__.py`
- Create: `investing_crawler/app/news_parser.py`
- Create: `investing_crawler/app/obsidian_writer.py`
- Test: `tests/investing_crawler/test_news_parser.py`
- Test: `tests/investing_crawler/test_obsidian_writer.py`

**Interfaces:**
- `parse_news_html(html: str, limit: int = 50) -> list[dict[str, str]]`
- `render_daily_markdown(news: list[dict[str, str]], collected_at: datetime, source_url: str) -> str`
- `merge_daily_markdown(existing: str, news: list[dict[str, str]], collected_at: datetime, source_url: str) -> str`

- [ ] **Step 1: Write failing parser tests**

```python
def test_parse_news_html_extracts_title_link_time_and_source():
    html = """
    <article>
      <a href="/news/stock-market-news/article-1">첫 번째 뉴스</a>
      <span class="byline">By Investing.com</span>
      <time datetime="2026-07-10T05:10:00Z">5분 전</time>
    </article>
    """
    assert parse_news_html(html) == [{
        "title": "첫 번째 뉴스",
        "url": "https://kr.investing.com/news/stock-market-news/article-1",
        "published_label": "5분 전",
        "published_at": "2026-07-10T05:10:00Z",
        "source": "Investing.com",
    }]

def test_parse_news_html_deduplicates_urls_and_honors_limit():
    html = """<a href="/news/a">A</a><a href="/news/a">A again</a><a href="/news/b">B</a>"""
    assert [item["title"] for item in parse_news_html(html, limit=1)] == ["A"]
```

- [ ] **Step 2: Run parser tests to verify they fail**

Run: `python -m unittest tests.investing_crawler.test_news_parser -v`
Expected: FAIL because the parser module and functions are not implemented.

- [ ] **Step 3: Write failing Markdown tests**

```python
def test_render_daily_markdown_contains_metadata_only():
    output = render_daily_markdown([{
        "title": "첫 번째 뉴스",
        "url": "https://kr.investing.com/news/a",
        "published_label": "5분 전",
        "published_at": "",
        "source": "Investing.com",
    }], datetime(2026, 7, 10, 6, 30), "https://kr.investing.com/news")
    self.assertIn("# Investing.com 한국어 뉴스 - 2026-07-10", output)
    self.assertIn("[첫 번째 뉴스](https://kr.investing.com/news/a)", output)
    self.assertIn("게시 표시: 5분 전", output)
    self.assertNotIn("본문", output)

def test_merge_daily_markdown_does_not_duplicate_existing_url():
    existing = "- [기존 뉴스](https://kr.investing.com/news/a)\n  - 게시 표시: 어제\n  - 출처: Investing.com\n"
    merged = merge_daily_markdown(existing, [{
        "title": "기존 뉴스",
        "url": "https://kr.investing.com/news/a",
        "published_label": "오늘",
        "published_at": "",
        "source": "Investing.com",
    }], datetime(2026, 7, 10, 6, 30), "https://kr.investing.com/news")
    self.assertEqual(merged.count("https://kr.investing.com/news/a"), 1)
```

- [ ] **Step 4: Run Markdown tests to verify they fail**

Run: `python -m unittest tests.investing_crawler.test_obsidian_writer -v`
Expected: FAIL because the writer functions are not implemented.

- [ ] **Step 5: Commit the red tests**

Run: `git add investing_crawler tests/investing_crawler && git commit -m "test: Investing 뉴스 파서와 옵시디언 출력 테스트 추가"`

### Task 2: 파서·Markdown 저장기 최소 구현

**Files:**
- Modify: `investing_crawler/app/news_parser.py`
- Modify: `investing_crawler/app/obsidian_writer.py`
- Test: `tests/investing_crawler/test_news_parser.py`
- Test: `tests/investing_crawler/test_obsidian_writer.py`

**Interfaces:**
- HTML 파서는 `a[href]` 중 `/news/` 경로의 링크만 수집하고, 링크 주변 article/card 텍스트에서 시간과 byline을 찾는다.
- URL은 `https://kr.investing.com` 기준으로 정규화한다.
- writer는 Markdown의 `https://kr.investing.com/` 링크를 읽어 URL 기준으로 중복을 제거한다.

- [ ] **Step 1: Implement `parse_news_html`**

Implement with BeautifulSoup4. For each unique `/news/` anchor, use its closest `article` or parent container, extract the anchor text as title, the first `time[datetime]` as `published_at`, visible time text as `published_label`, and a `By ...` text as source. Use `Investing.com` when no byline exists. Ignore anchors without a non-empty title.

- [ ] **Step 2: Run parser tests**

Run: `python -m unittest tests.investing_crawler.test_news_parser -v`
Expected: PASS.

- [ ] **Step 3: Implement `render_daily_markdown` and `merge_daily_markdown`**

Render one bullet per article with title, URL, published label, and source. Include collection time and source URL in the header. Preserve existing entries, append only unseen URLs, and write the result with a trailing newline.

- [ ] **Step 4: Run Markdown tests**

Run: `python -m unittest tests.investing_crawler.test_obsidian_writer -v`
Expected: PASS.

- [ ] **Step 5: Commit the pure implementation**

Run: `git add investing_crawler/app tests/investing_crawler && git commit -m "feat: Investing 뉴스 메타데이터 파서와 마크다운 저장 추가"`

### Task 3: Playwright 일회성 수집 명령과 오류 처리

**Files:**
- Create: `investing_crawler/app/browser_collector.py`
- Create: `investing_crawler/app/main.py`
- Create: `tests/investing_crawler/test_main.py`
- Modify: `investing_crawler/app/__init__.py`

**Interfaces:**
- `async collect_news_page(url: str, limit: int) -> list[dict[str, str]]`
- `run() -> int` reads environment variables, writes one date-based Markdown file, and returns process status.

- [ ] **Step 1: Write failing collector orchestration tests**

Mock `collect_news_page` and use a temporary Vault directory. Assert that `run()` creates `<vault>/<news_dir>/<YYYY-MM-DD>.md`, passes the configured URL and limit, and returns `0`. Add a second test where the collector returns `[]`; assert that an existing file remains unchanged and `run()` returns `1`.

- [ ] **Step 2: Run orchestration tests to verify they fail**

Run: `python -m unittest tests.investing_crawler.test_main -v`
Expected: FAIL because the command module is not implemented.

- [ ] **Step 3: Implement `collect_news_page`**

Use Playwright async API with one Chromium page. Set a 20-second navigation timeout, block `image`, `media`, `font`, and `stylesheet` requests, navigate with `wait_until="domcontentloaded"`, wait briefly for news anchors, scroll at most twice, then pass `await page.content()` to `parse_news_html`. Close the browser in `finally`.

- [ ] **Step 4: Implement `run()`**

Read `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_NEWS_DIR`, `INVESTING_NEWS_URL`, `INVESTING_NEWS_LIMIT`, and `INVESTING_NEWS_TIMEZONE`. Retry page collection once. Treat an empty result as failure and do not overwrite the existing Markdown. Use an atomic temporary file replacement for successful writes.

- [ ] **Step 5: Run orchestration tests**

Run: `python -m unittest tests.investing_crawler.test_main -v`
Expected: PASS.

- [ ] **Step 6: Commit the collector command**

Run: `git add investing_crawler/app tests/investing_crawler && git commit -m "feat: Playwright 기반 Investing 뉴스 일회성 수집 명령 추가"`

### Task 4: Docker와 N100 실행 경로 추가

**Files:**
- Create: `investing_crawler/requirements.txt`
- Create: `investing_crawler/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.n100.yml`
- Modify: `.env.example`
- Test: `tests/test_investing_crawler_config.py`

**Interfaces:**
- Docker command: `python -m app.main`
- Compose service: `investing-crawler`
- Host-to-container Vault mount: `${OBSIDIAN_VAULT_PATH}:/vault`

- [ ] **Step 1: Add configuration tests**

Assert that the example environment declares `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_NEWS_DIR`, `INVESTING_NEWS_URL`, and `INVESTING_NEWS_LIMIT`, and that the compose file defines an `investing-crawler` service with a one-shot command and `/vault` mount.

- [ ] **Step 2: Run configuration tests to verify they fail**

Run: `python -m unittest tests.test_investing_crawler_config -v`
Expected: FAIL because the service and environment entries do not exist.

- [ ] **Step 3: Add the dedicated Playwright image**

Base the image on the official Playwright Python image matching the pinned Playwright version. Copy the standalone app, install `requirements.txt`, and use `CMD ["python", "-m", "app.main"]`.

- [ ] **Step 4: Add the one-shot compose service**

Define the service without `restart: unless-stopped`, mount the Vault read/write, pass the five environment variables, and in the N100 override set one worker, CPU `0.50`, memory `512m`, and a finite PID limit. Do not add a dependency on MT4 or change existing service definitions.

- [ ] **Step 5: Run configuration tests**

Run: `python -m unittest tests.test_investing_crawler_config -v`
Expected: PASS.

- [ ] **Step 6: Commit runtime configuration**

Run: `git add investing_crawler docker-compose.yml docker-compose.n100.yml .env.example tests/test_investing_crawler_config.py && git commit -m "feat: Investing 뉴스 크롤러 일회성 Docker 실행 추가"`

### Task 5: 통합 검증과 운영 문서

**Files:**
- Modify: `README.md`
- Modify: `docs/n100-mt4-setup.md`
- Create: `scripts/investing-news-once.ps1`
- Test: `tests/investing_crawler/test_markdown_roundtrip.py`

- [ ] **Step 1: Add the PowerShell one-shot command**

Create a script that validates `OBSIDIAN_VAULT_PATH`, invokes `docker compose -f docker-compose.yml -f docker-compose.n100.yml run --rm investing-crawler`, and returns the container exit code. Do not stop or restart MT4 or other services.

- [ ] **Step 2: Add Markdown round-trip test**

Run the writer twice against a temporary file with the same article and assert that the URL occurs once and the Markdown remains valid.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m unittest discover -s tests -p "test*.py" -v`
Expected: PASS with all existing and new tests.

- [ ] **Step 4: Build and run the crawler smoke test**

Run: `docker compose -f docker-compose.yml -f docker-compose.n100.yml build investing-crawler` then `docker compose -f docker-compose.yml -f docker-compose.n100.yml run --rm investing-crawler` with a temporary Vault path. Expected: process exits `0`, creates one Markdown file, and records at least five news links.

- [ ] **Step 5: Document scheduling and N100 safeguards**

Document a Windows Task Scheduler action for the PowerShell script before trading hours, the Vault mount requirement, the one-shot behavior, and the instruction to verify MT4 latency and memory after the first run.

- [ ] **Step 6: Commit final integration**

Run: `git add README.md docs/n100-mt4-setup.md scripts/investing-news-once.ps1 tests && git commit -m "docs: Investing 뉴스 수집 운영 방법 추가"`
