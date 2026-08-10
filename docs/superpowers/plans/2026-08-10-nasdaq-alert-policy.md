# 나스닥 고정밀 알림 구현 계획

**Goal:** 15분 수집 주기를 바꾸지 않고, 뉴스 허브에는 나스닥 영향 기사를 보관하며 텔레그램에는 최상위 사건만 즉시 전송함.

**Architecture:** 신규 순수 분류 모듈이 기사 제목·요약을 판정해 alert 또는 archive 등급과 이유를 반환함. news_archive는 기존 신규 기사 판정 후 알림 등급 기사만 notifier에 전달함. scheduler와 수집 주기는 수정하지 않음.

## Global Constraints

- crawler-worker 스케줄러와 NEWS_REFRESH_INTERVAL_SECONDS 동작을 수정하지 않음.
- Telegram 비밀값을 코드·테스트 출력에 기록하지 않음.
- 일일 발송 건수 상한을 구현하지 않음.
- 동일 URL은 기존 보관 중복 제거로 한 번만 알림 후보가 됨.
- 일반 종목 등락, 목표주가, 단순 전망, 반복 해설은 alert가 아닌 archive여야 함.

### Task 1: 뉴스 중요도 분류와 고정밀 텔레그램 발송

**Files:**
- Create: crawler-worker/app/services/nasdaq_relevance.py
- Modify: crawler-worker/app/services/news_archive.py
- Modify: crawler-worker/app/services/telegram_notifier.py
- Modify: tests/crawler_worker/test_telegram_notifier.py
- Create: tests/crawler_worker/test_nasdaq_relevance.py

**Interfaces:**
- Produces: classify_nasdaq_relevance(article: dict) -> dict with level (alert or archive) and reasons (list[str]).
- Consumes: 새 KR_WORLD 기사 목록과 Telegram 환경 설정.

- [ ] **Step 1: 실패하는 분류 테스트를 작성함**

테스트는 다음 사례를 명시함.

```python
self.assertEqual(
    classify_nasdaq_relevance({"title": "미 연준, 기준금리 동결 결정"})["level"],
    "alert",
)
self.assertIn(
    "연준·금리",
    classify_nasdaq_relevance({"title": "미 연준, 기준금리 동결 결정"})["reasons"],
)
self.assertEqual(
    classify_nasdaq_relevance({"title": "엔비디아 목표주가 상향"})["level"],
    "archive",
)
self.assertEqual(
    classify_nasdaq_relevance({"title": "반도체 수출 제한 확대 가능성"})["level"],
    "alert",
)
```

- [ ] **Step 2: RED를 확인함**

Run:

```bash
cd crawler-worker && PYTHONPATH=.:.. python3 -m unittest ../tests/crawler_worker/test_nasdaq_relevance.py -v
```

Expected: FAIL. 모듈과 함수가 존재하지 않음.

- [ ] **Step 3: 순수 분류 모듈을 구현함**

키워드는 제목과 summary를 함께 검사함.

```python
def classify_nasdaq_relevance(article: dict) -> dict[str, object]:
    text = " ".join(str(article.get(key) or "") for key in ("title_ko", "title", "summary")).casefold()
    if _matches(text, MACRO_PATTERNS):
        return {"level": "alert", "reasons": ["연준·금리"]}
    if _matches(text, SEMICONDUCTOR_SHOCK_PATTERNS):
        return {"level": "alert", "reasons": ["반도체 영향"]}
    if _matches(text, MARKET_SHOCK_PATTERNS):
        return {"level": "alert", "reasons": ["미국 기술주 시장 영향"]}
    return {"level": "archive", "reasons": []}
```

MACRO_PATTERNS는 연준·FOMC·기준금리·CPI·PCE·비농업고용·실업률의 결과 발표 표현을 포함함. SEMICONDUCTOR_SHOCK_PATTERNS는 수출 제한·제재·공급 중단·칩 수출 통제 표현을 포함함. 목표주가, 전망, 일반 주가 표현은 어떤 alert 패턴에도 포함하지 않음.

- [ ] **Step 4: 보관 기사에 분류 결과를 저장하고 알림 대상만 전달함**

news_archive에서 _attach_archive_metadata 결과에 nasdaq_relevance를 추가함. notify_new_investing_articles 호출 전 새 기사 중 classify 결과 level이 alert인 기사만 전달함. 기존 telegram_notifications_initialized의 첫 수집 기준선과 KR_WORLD 범위는 유지함.

- [ ] **Step 5: Telegram 메시지에 분류 이유와 발행 시각을 표시함**

```text
[나스닥 중요 알림]
{title}
이유: {reason}
출처: {source} · {published_at}
{url}
```

이유가 없을 경우 이유 행은 생략하지 않고 이 알림 문구를 보내지 않도록 분류 단계에서 차단함.

- [ ] **Step 6: GREEN과 crawler-worker 회귀 테스트를 실행함**

Run:

```bash
cd crawler-worker && PYTHONPATH=.:.. python3 -m unittest discover -s ../tests/crawler_worker -v
```

Expected: PASS. 스케줄러 테스트와 기존 알림 baseline 테스트는 수정 없이 통과함.

- [ ] **Step 7: 커밋함**

```bash
git add crawler-worker/app/services/nasdaq_relevance.py crawler-worker/app/services/news_archive.py crawler-worker/app/services/telegram_notifier.py tests/crawler_worker/test_nasdaq_relevance.py tests/crawler_worker/test_telegram_notifier.py
git commit -m "feat: 나스닥 중요 뉴스 알림 분류 추가"
```
