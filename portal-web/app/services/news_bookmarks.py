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


def delete(bookmark_id):
    with _connect() as conn:
        conn.execute('DELETE FROM bookmarks WHERE id=?', (bookmark_id,))
