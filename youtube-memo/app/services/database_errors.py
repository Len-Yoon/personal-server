"""Stable SQLite contention responses without exposing storage details."""
import re
import sqlite3
from html import escape

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

# Preserve only ordinary editor fields; credentials and arbitrary submitted fields
# must never be reflected into a recovery page.
_RETRY_FIELDS = frozenset({
    "isbn", "external_id", "title", "authors", "publisher", "published_date",
    "description", "thumbnail", "preview_url", "source", "titles", "url",
    "reading_status", "current_page", "current_chapter", "progress_percent",
    "redirect_to", "expected_version", "expected_versions", "request_id",
    "done_chapter_ids", "is_done", "comment", "chapter_id", "memo_title",
    "content", "page", "tags",
})
_EDITOR_PATH = re.compile(r"/(?:books|videos|memos|chapters)(?:/[0-9]+)?(?:/[a-z-]+)?(?:/bulk)?\Z")
_BUSY_MESSAGE = "다른 저장 작업이 진행 중입니다. 입력 내용은 유지됩니다. 잠시 후 다시 저장해주세요."


def is_database_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    # Synthetic/older driver errors may lack SQLite's structured error code.
    return str(error).lower() in {"database is locked", "database table is locked", "database schema is locked"}


async def database_error_response(request: Request, error: sqlite3.OperationalError):
    if not is_database_busy(error):
        return JSONResponse({"detail": "저장소 작업을 완료하지 못했습니다.", "code": "database_unavailable"},
                            status_code=500, headers={"Cache-Control": "no-store"})
    headers = {"Cache-Control": "no-store", "Retry-After": "2"}
    if (request.method == "POST" and _EDITOR_PATH.fullmatch(request.url.path)
            and "text/html" in request.headers.get("accept", "")):
        # Form parsing already completed in these routes. Retain repeated fields
        # and the original revision/request ID so retries preserve write safety.
        form = await request.form()
        hidden = "".join(
            f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(value, quote=True)}">'
            for name, value in form.multi_items()
            if name in _RETRY_FIELDS and isinstance(value, str)
        )
        deleting = request.url.path.endswith("/delete")
        message = _BUSY_MESSAGE
        if deleting:
            message = "다른 저장 작업이 진행 중입니다. 아직 삭제를 완료하지 못했습니다. 대상이 맞는지 확인한 후 다시 시도해주세요."
        label = "같은 항목 삭제 다시 시도" if deleting else "같은 내용으로 다시 저장"
        return HTMLResponse(
            '<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>저장 재시도</title><main><h1>잠시 후 다시 저장해주세요</h1>'
            f'<p>{message}</p><form method="post" action="{escape(request.url.path, quote=True)}">'
            f'{hidden}<button type="submit">{label}</button></form>'
            '<p>이 화면을 닫지 않으면 제출한 내용으로 다시 시도할 수 있습니다.</p></main></html>',
            status_code=503, headers=headers,
        )
    return JSONResponse({"detail": _BUSY_MESSAGE, "code": "database_busy", "retryable": True},
                        status_code=503, headers=headers)
