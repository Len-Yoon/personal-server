"""Portal smoke fixture. Never import production configuration or data."""
import argparse
import functools
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--fd', required=True, type=int)
    args = parser.parse_args()
    scratch = args.root.resolve()
    if (scratch.parent != Path(tempfile.gettempdir()).resolve()
            or not scratch.name.startswith('portal-browser-smoke-')
            or scratch.stat().st_mode & 0o077):
        raise ValueError('private disposable root required')
    # The caller supplies a clean environment. Set every Portal write path before
    # importing the application, including lazy SQLite and security stores.
    os.environ.update({
        'APP_ENV': 'test', 'DEMO_MODE': 'false',
        'FILE_MANAGER_AUTH_REQUIRED': 'true',
        'FILE_MANAGER_ACCESS_PASSWORD': 'smoke-access-only',
        'DELETE_PASSWORD': 'smoke-delete-only',
        'FILE_STORAGE_PATH': str(scratch / 'files'),
        'HOMEOPS_DB_PATH': str(scratch / 'homeops.sqlite3'),
        'NEWS_BOOKMARK_DB_PATH': str(scratch / 'news-bookmarks.sqlite3'),
        'SECURITY_LOG_PATH': str(scratch / 'security-events.txt'),
        'AUTH_RATE_LIMIT_STATE_PATH': str(scratch / 'rate-limit.json'),
        'PORTFOLIO_CONTENT_PATH': str(scratch / 'portfolio.md'),
        'NEWS_SEARCH_URL': 'http://smoke.invalid/news',
        'YOUTUBE_SEARCH_URL': 'http://smoke.invalid/youtube',
        'BOOKS_SEARCH_URL': 'http://smoke.invalid/books',
        'HOMEOPS_EXECUTOR_URL': 'http://smoke.invalid/disabled',
    })
    sys.path[:0] = [str(ROOT / 'portal-web'), str(ROOT)]
    import httpx
    from app.services import global_search
    original_client = httpx.AsyncClient
    news_calls = 0
    def upstream(request):
        nonlocal news_calls
        if request.url.host != 'smoke.invalid':
            raise RuntimeError('external upstream prohibited')
        if request.url.path == '/news':
            news_calls += 1
            if news_calls == 1:
                return httpx.Response(503)
        return httpx.Response(200, json={'results': [{
            'title': '격리 검색 복구 성공', 'url': '#', 'description': 'local fixture',
        }]})
    # Real global-search fan-out/parser/retry runs against an in-memory upstream.
    global_search.httpx.AsyncClient = functools.partial(original_client, transport=httpx.MockTransport(upstream))
    from app.main import app
    import uvicorn
    uvicorn.run(app, fd=args.fd, log_level='error', access_log=False)


if __name__ == '__main__':
    main()
