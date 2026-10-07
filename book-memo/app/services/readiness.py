"""Local, read-only readiness checks; never creates a missing database."""
import os
import sqlite3
from pathlib import Path


def database_ready(path: Path, schema_ready) -> bool:
    try:
        if not path.is_file() or not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK):
            return False
        storage = os.statvfs(path.parent)
        if storage.f_bavail * storage.f_frsize <= 0:
            return False
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            if not schema_ready(connection):
                return False
            connection.execute('SELECT id FROM books LIMIT 1').fetchone()
        finally:
            connection.close()
        return True
    except (OSError, sqlite3.Error):
        return False
