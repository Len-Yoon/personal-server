"""Authenticated, bounded JSON import with a session-bound preview contract."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlsplit

from app.services import book_service as service

parse_tags = service._parse_tags

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 1000
MAX_CHILDREN = 10000
PREVIEW_TTL_SECONDS = 600
_PREVIEW_SECRET = secrets.token_bytes(32)


def _invalid() -> ValueError:
    return ValueError("가져오기 파일의 형식 또는 연결 관계가 올바르지 않습니다.")


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _text(value, limit=100000, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (required and not value.strip()):
        raise _invalid()
    return value


def _timestamp(value):
    _text(value, 64, True)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _invalid() from error


def _url(value):
    _text(value, 4000)
    if value and (urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc):
        raise _invalid()


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise _invalid()


def _unique_id(value, seen):
    _integer(value, 1)
    if value in seen:
        raise _invalid()
    seen.add(value)


def _tags(value):
    if not isinstance(value, list) or len(value) > 5:
        raise _invalid()
    for tag in value:
        _text(tag, 30, True)
    parsed = parse_tags(",".join(value))
    if parsed != value:
        raise _invalid()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


async def _read_payload(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="JSON 파일을 전송해주세요.")
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="가져오기 파일은 2 MiB 이내여야 합니다.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                             parse_constant=lambda _: (_ for _ in ()).throw(_invalid()))
        validate_payload(payload)
    except (ValueError, UnicodeDecodeError, TypeError, RecursionError) as error:
        raise HTTPException(status_code=400, detail=str(_invalid())) from error
    return raw, payload


def _signature(raw, session, prefix):
    message = prefix.encode() + b"\0" + session.encode() + b"\0" + hashlib.sha256(raw).digest()
    return hmac.new(_PREVIEW_SECRET, message, hashlib.sha256).hexdigest()


def _preview_token(raw, session):
    prefix = f"{int(time.time())}.{secrets.token_hex(16)}"
    return f"{prefix}.{_signature(raw, session, prefix)}"


def _verify_preview(token, raw, session):
    try:
        issued, nonce, signature = token.split(".")
        age = time.time() - int(issued)
        if not 0 <= age <= PREVIEW_TTL_SECONDS or len(nonce) != 32 or len(signature) != 64:
            raise ValueError()
        if not hmac.compare_digest(signature, _signature(raw, session, f"{issued}.{nonce}")):
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(status_code=409, detail="미리보기가 만료되었거나 파일이 변경되었습니다. 다시 미리보기해주세요.")


def install_routes(app, require_write, session_cookie, templates):
    @app.get("/import")
    def import_page(request: Request):
        require_write(request)
        return templates.TemplateResponse("import_library.html", {"request": request, "title": "JSON 가져오기"})

    @app.post("/api/import/preview")
    async def preview(request: Request):
        require_write(request)
        raw, payload = await _read_payload(request)
        result = preview_import(payload)
        return JSONResponse({**result, "preview_token": _preview_token(raw, request.cookies.get(session_cookie, ""))},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/import/commit")
    async def commit(request: Request):
        require_write(request)
        raw, payload = await _read_payload(request)
        _verify_preview(request.headers.get("X-Import-Preview", ""), raw, request.cookies.get(session_cookie, ""))
        try:
            result = commit_import(payload)
        except (ValueError, sqlite3.Error) as error:
            raise HTTPException(status_code=400, detail="가져오지 못했습니다. 기존 기록은 유지됩니다.") from error
        return JSONResponse(result, headers={"Cache-Control": "no-store"})


_BOOK_FIELDS = ("id", "isbn", "external_id", "title", "authors", "publisher", "published_date", "description",
                "thumbnail", "preview_url", "source", "reading_status", "current_page", "current_chapter",
                "progress_percent", "created_at", "updated_at")
_CHAPTER_FIELDS = ("id", "book_id", "title", "position", "is_done", "comment", "created_at", "updated_at")
_MEMO_FIELDS = ("id", "book_id", "chapter_id", "title", "content", "page", "created_at", "updated_at")


def validate_payload(payload):
    _keys(payload, ("metadata", "records"))
    _keys(payload["metadata"], ("service", "schema_version"))
    if payload["metadata"]["service"] != "book-memo" or type(payload["metadata"]["schema_version"]) is not int or payload["metadata"]["schema_version"] != 1:
        raise _invalid()
    if not isinstance(payload["records"], list) or len(payload["records"]) > MAX_RECORDS:
        raise _invalid()
    books, chapters, memos, isbns = set(), set(), set(), set()
    children = 0
    for book in payload["records"]:
        _keys(book, (*_BOOK_FIELDS, "chapters", "memos"))
        _unique_id(book["id"], books)
        _text(book["isbn"], 256, True)
        if book["isbn"] in isbns:
            raise _invalid()
        isbns.add(book["isbn"])
        for field in ("external_id", "title", "authors", "publisher", "published_date", "description", "source", "reading_status", "current_chapter"):
            _text(book[field], required=field == "title")
        if book["reading_status"] not in {"읽을 예정", "읽는 중", "완료", "보류"}:
            raise _invalid()
        _integer(book["current_page"])
        _integer(book["progress_percent"], maximum=100)
        for field in ("thumbnail", "preview_url"):
            _url(book[field])
        for field in ("created_at", "updated_at"):
            _timestamp(book[field])
        if not isinstance(book["chapters"], list) or not isinstance(book["memos"], list):
            raise _invalid()
        children += len(book["chapters"]) + len(book["memos"])
        if children > MAX_CHILDREN:
            raise _invalid()
        local_chapters = set()
        for chapter in book["chapters"]:
            _keys(chapter, _CHAPTER_FIELDS)
            _unique_id(chapter["id"], chapters)
            local_chapters.add(chapter["id"])
            _integer(chapter["book_id"], 1)
            if chapter["book_id"] != book["id"]:
                raise _invalid()
            _text(chapter["title"], required=True)
            _text(chapter["comment"])
            _integer(chapter["position"])
            _integer(chapter["is_done"], maximum=1)
            for field in ("created_at", "updated_at"):
                _timestamp(chapter[field])
        for memo in book["memos"]:
            _keys(memo, (*_MEMO_FIELDS, "tags"))
            _unique_id(memo["id"], memos)
            _integer(memo["book_id"], 1)
            if memo["book_id"] != book["id"]:
                raise _invalid()
            if memo["chapter_id"] is not None:
                _integer(memo["chapter_id"], 1)
                if memo["chapter_id"] not in local_chapters:
                    raise _invalid()
            _text(memo["title"], required=True)
            _text(memo["content"], required=True)
            _integer(memo["page"])
            _tags(memo["tags"])
            for field in ("created_at", "updated_at"):
                _timestamp(memo[field])


def preview_import(payload):
    validate_payload(payload)
    service.init_db()
    with service._connect() as connection:
        existing = {row["isbn"] for row in connection.execute("SELECT isbn FROM books")}
    skipped = sum(book["isbn"] in existing for book in payload["records"])
    return {"record_count": len(payload["records"]), "new_count": len(payload["records"]) - skipped,
            "skip_count": skipped, "duplicate_policy": "skip_existing"}


def commit_import(payload):
    validate_payload(payload)
    service.init_db()
    imported = skipped = 0
    with service._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for book in payload["records"]:
            if connection.execute("SELECT 1 FROM books WHERE isbn = ?", (book["isbn"],)).fetchone():
                skipped += 1
                continue
            fields = _BOOK_FIELDS[1:]
            cursor = connection.execute(f"INSERT INTO books ({', '.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                        [book[field] for field in fields])
            book_id = cursor.lastrowid
            chapters = {}
            for chapter in book["chapters"]:
                fields = _CHAPTER_FIELDS[2:]
                cursor = connection.execute(f"INSERT INTO book_chapters (book_id, {', '.join(fields)}) VALUES (?,{','.join('?' for _ in fields)})",
                                            [book_id, *(chapter[field] for field in fields)])
                chapters[chapter["id"]] = cursor.lastrowid
            for memo in book["memos"]:
                fields = _MEMO_FIELDS[3:]
                cursor = connection.execute(f"INSERT INTO book_memos (book_id, chapter_id, {', '.join(fields)}) VALUES (?, ?,{','.join('?' for _ in fields)})",
                                            [book_id, chapters.get(memo["chapter_id"]), *(memo[field] for field in fields)])
                connection.executemany("INSERT INTO memo_tags (memo_id, tag, tag_key, position) VALUES (?, ?, ?, ?)",
                                       [(cursor.lastrowid, tag, tag.casefold(), pos) for pos, tag in enumerate(memo["tags"])])
            imported += 1
    return {"imported_count": imported, "skip_count": skipped, "duplicate_policy": "skip_existing"}
