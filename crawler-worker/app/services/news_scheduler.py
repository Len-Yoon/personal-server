from __future__ import annotations

import os
from threading import Event, Thread
from typing import Callable

from app.services.news_archive import collect_korean_news


def _interval_seconds() -> int:
    return max(1, int(os.getenv("NEWS_REFRESH_INTERVAL_SECONDS", "300")))


class InvestingNewsScheduler:
    def __init__(
        self,
        interval_seconds: int | None = None,
        collect_news: Callable[..., dict] = collect_korean_news,
    ) -> None:
        self._interval_seconds = interval_seconds or _interval_seconds()
        self._collect_news = collect_news
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = Thread(target=self._run, daemon=True, name="investing-news-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_once(self) -> None:
        try:
            self._collect_news(
                category="KR_WORLD",
                limit=24,
                force_refresh=True,
            )
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self.run_once()
