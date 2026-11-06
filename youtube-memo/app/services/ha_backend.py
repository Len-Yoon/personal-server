"""Optional shared PostgreSQL backend; SQLite remains the default."""
import os

SERVICE = "youtube-memo"
REQUIRED_TABLES = ["videos","memos","memo_tags","memo_revisions","deleted_memos","write_requests"]
VERSIONED_TABLES = ["memos"]


def enabled():
    return "HA_PG_CONFIG" in os.environ


def runtime():
    import ha_runtime
    return ha_runtime


def require_schema():
    runtime().require_schema(SERVICE, "main", REQUIRED_TABLES)
    with runtime().connect(SERVICE) as connection:
        for table in VERSIONED_TABLES:
            row = connection.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = ? AND column_name = 'version'", (table,),
            ).fetchone()
            if not row:
                raise runtime().DatabaseUnavailable("Database schema is not ready")


def begin_write(connection):
    connection.execute("BEGIN" if enabled() else "BEGIN IMMEDIATE")


def search_sql(query):
    if not enabled():
        return query
    # Keep SQLite's case-insensitive search behavior using PostgreSQL operators.
    import re
    query = re.sub(r"([a-z_]+\.title) = \? COLLATE NOCASE", r"LOWER(\1) = LOWER(?)", query)
    return query.replace(" LIKE ", " ILIKE ")
