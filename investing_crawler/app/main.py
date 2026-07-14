from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from .obsidian_writer import merge_daily_markdown
from .rss_collector import build_investing_rss_feed_urls, collect_investing_news


def run() -> int:
    vault_path = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not vault_path:
        print("OBSIDIAN_VAULT_PATH is required", file=sys.stderr)
        return 1

    news_dir = os.getenv("OBSIDIAN_NEWS_DIR", "뉴스/Investing").strip()
    source_url = os.getenv("INVESTING_NEWS_URL", build_investing_rss_feed_urls()).strip()
    limit = int(os.getenv("INVESTING_NEWS_LIMIT", "50"))
    timezone_name = os.getenv("INVESTING_NEWS_TIMEZONE", "Asia/Seoul").strip()
    collected_at = datetime.now(ZoneInfo(timezone_name))
    output_dir = Path(vault_path) / Path(news_dir)
    output_file = output_dir / f"{collected_at:%Y-%m-%d}.md"

    try:
        articles = collect_investing_news(limit=limit, feed_url=source_url)
    except Exception as exc:
        print(f"Investing.com collection failed: {exc}", file=sys.stderr)
        return 1
    if not articles:
        print("Investing.com returned no news items", file=sys.stderr)
        return 1

    existing = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    content = merge_daily_markdown(existing, articles, collected_at, source_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(output_file, content)
    print(f"Saved {len(articles)} Investing.com news items to {output_file}")
    return 0


def _atomic_write(path: Path, content: str) -> None:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        handle.write(content)
    temporary_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(run())
