"""Opt-in shared opaque sessions; disk contains only hashes, never bearer tokens."""

import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from contextlib import closing
from urllib.parse import quote


def sqlite_wal_is_patched() -> bool:
    """Known upstream fixes for the 2026 WAL-reset write/checkpoint race."""
    version = sqlite3.sqlite_version_info
    return (version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0)
            or (3, 44, 6) <= version < (3, 45, 0))


def shared_wal_runtime_allowed() -> bool:
    return os.getenv("APP_ENV", "").strip().lower() in {"development", "test"} or sqlite_wal_is_patched()


class SQLiteSessionStore:
    def __init__(self, path: Path, max_entries: int = 1024):
        self.path = Path(path)
        self.max_entries = max(1, max_entries)
        self._keeper = None

    def start(self) -> None:
        """Keep idle WAL sidecars available for a read-only online backup reader."""
        if self._keeper is None:
            if not self.ready():
                raise RuntimeError("shared_session_state_unavailable")
            keeper = self._connect(create=True)
            try:
                keeper.execute("SELECT count(*) FROM sessions").fetchone()
            except Exception:
                keeper.close()
                raise
            self._keeper = keeper

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _check_path(self, create: bool) -> None:
        if not self.path.is_absolute():
            raise ValueError("session state path must be absolute")
        # Inspect every component before mkdir/open: resolve() would conceal symlinks.
        for component in reversed((self.path.parent, *self.path.parent.parents)):
            if component.is_symlink():
                raise ValueError("session state symlink boundary")
            if component.exists() and not component.is_dir():
                raise ValueError("session state parent must be a directory")
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = self.path.parent.stat()
        if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
            raise ValueError("session state parent must be owned and non-shared")
        if create:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        for candidate in (self.path, *(Path(f"{self.path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                if candidate == self.path:
                    raise
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise ValueError("session state must be a private owned regular file")

    def _connect(self, *, create: bool = False, readonly: bool = False):
        self._check_path(create)
        uri = f"file:{quote(str(self.path), safe='/')}?mode={'ro' if readonly else 'rw'}"
        connection = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=250")
            if create:
                connection.execute("PRAGMA journal_mode=WAL")
                for table in ("sessions", "confirmations"):
                    connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (token_hash TEXT PRIMARY KEY, scope TEXT NOT NULL, payload_hash TEXT NOT NULL, expires_at REAL NOT NULL)")
            return connection
        except Exception:
            connection.close()
            raise

    def ready(self) -> bool:
        """Verify private storage, schema and a short writer transaction without tokens."""
        try:
            with closing(self._connect(create=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for table in ("sessions", "confirmations"):
                    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
                    expected = [("token_hash", "TEXT", 0, 1), ("scope", "TEXT", 1, 0),
                                ("payload_hash", "TEXT", 1, 0), ("expires_at", "REAL", 1, 0)]
                    if [(row[1], row[2], row[3], row[5]) for row in columns] != expected:
                        return False
                    connection.execute(f"UPDATE {table} SET expires_at=expires_at WHERE 0")
                connection.commit()
                return True
        except (OSError, ValueError, sqlite3.Error):
            return False

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _create(self, table: str, scope: str, payload: str, max_age_seconds: int) -> str:
        now = time.time()
        with closing(self._connect(create=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
                count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count >= self.max_entries:
                    connection.execute(f"DELETE FROM {table} WHERE token_hash IN (SELECT token_hash FROM {table} ORDER BY expires_at, rowid LIMIT ?)", (count - self.max_entries + 1,))
                for _ in range(8):
                    token = secrets.token_urlsafe(32)
                    try:
                        connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", (self._hash(token), scope, self._hash(payload), now + max_age_seconds))
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise RuntimeError("unable to issue unique session token")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return token

    def create(self, scope: str, max_age_seconds: int) -> str:
        return self._create("sessions", scope, "", max_age_seconds)

    def revoke_all(self) -> None:
        """Operator-only revocation after credential rotation or before restore activation."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM sessions")
                connection.execute("DELETE FROM confirmations")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def validate(self, scope: str, token: str) -> bool:
        if not isinstance(token, str) or not token or len(token) > 512:
            return False
        try:
            with closing(self._connect(readonly=True)) as connection:
                row = connection.execute("SELECT 1 FROM sessions WHERE token_hash=? AND scope=? AND expires_at>=?", (self._hash(token), scope, time.time())).fetchone()
                return row is not None
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            return False

    def create_confirmation(self, scope: str, payload: str, max_age_seconds: int) -> str:
        return self._create("confirmations", scope, payload, max_age_seconds)

    def consume_confirmation(self, scope: str, payload: str, token: str) -> bool:
        if not isinstance(token, str) or not token or len(token) > 512:
            return False
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute("DELETE FROM confirmations WHERE token_hash=? AND scope=? AND payload_hash=? AND expires_at>=?", (self._hash(token), scope, self._hash(payload), time.time()))
                consumed = cursor.rowcount == 1
                connection.commit()
                return consumed
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Explicitly revoke shared sessions and confirmations.")
    parser.add_argument("--revoke-all", action="store_true", required=True)
    parser.parse_args()
    path = os.getenv("AUTH_SESSION_STATE_PATH", "").strip()
    try:
        if not path:
            raise ValueError("shared session storage is not configured")
        if not shared_wal_runtime_allowed():
            raise ValueError("unsupported sqlite runtime")
        SQLiteSessionStore(Path(path)).revoke_all()
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "portal_session_revocation=FAIL\n")
    print("portal_session_revocation=PASS")
