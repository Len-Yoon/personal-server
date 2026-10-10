"""Private snapshots survive expiry of the public seven-day news archive."""
import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


@contextmanager
def _connect():
    # Kept outside the file download root and public news archive.
    path = Path(os.getenv('NEWS_BOOKMARK_DB_PATH', '/var/lib/portal/news-bookmarks.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS bookmarks (id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL, saved_at TEXT NOT NULL)')
            yield conn
    finally:
        conn.close()


def save(url, title, note):
    url, title = url.strip(), title.strip()
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid or len(url) > 2048 or any(ord(c) < 32 for c in url) or not title or len(title) > 500 or len(note) > 20000:
        raise ValueError('기사 주소·제목·메모 길이를 확인해주세요.')
    # Store URL only: never fetch it or allow it to influence crawler requests.
    with _connect() as conn:
        conn.execute('INSERT INTO bookmarks(url,title,note,saved_at) VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,note=excluded.note', (url, title, note, datetime.now(timezone.utc).isoformat()))


def listing():
    with _connect() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM bookmarks ORDER BY saved_at DESC,id DESC LIMIT 1000')]


def page(*, q='', page=1, page_size=50):
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200 or len(q) > 200:
        raise ValueError('검색어와 페이지 범위를 확인해주세요.')
    q = q.strip()
    # Treat wildcard characters as literal text and bind all user input.
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    pattern = '%' + escaped + '%'
    where = " WHERE title LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'" if q else ''
    params = (pattern, pattern, pattern) if q else ()
    with _connect() as conn:
        # COUNT and rows must share a snapshot even if another writer commits.
        conn.execute('BEGIN')
        total = conn.execute('SELECT COUNT(*) FROM bookmarks' + where, params).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        items = [dict(row) for row in conn.execute('SELECT * FROM bookmarks' + where + ' ORDER BY saved_at DESC,id DESC LIMIT ? OFFSET ?', params + (page_size, (page-1)*page_size))]
    return {'items': items, 'total': total, 'page': page, 'pages': pages, 'page_size': page_size, 'q': q}


def delete(bookmark_id):
    with _connect() as conn:
        conn.execute('DELETE FROM bookmarks WHERE id=?', (bookmark_id,))
