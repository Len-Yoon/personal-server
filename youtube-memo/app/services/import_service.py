"""Authenticated, bounded JSON import with a session-bound preview contract."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlsplit

from app.services import memo_service as service

parse_tags = service.parse_tags

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from app.services.database_errors import is_database_busy

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
        return templates.TemplateResponse(request=request, name="import_library.html", context={"request": request, "title": "JSON 가져오기"})

    @app.post("/api/import/preview")
    async def preview(request: Request):
        require_write(request)
        raw, payload = await _read_payload(request)
        result = await run_in_threadpool(preview_import, payload)
        return JSONResponse({**result, "preview_token": _preview_token(raw, request.cookies.get(session_cookie, ""))},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/import/commit")
    async def commit(request: Request):
        require_write(request)
        raw, payload = await _read_payload(request)
        _verify_preview(request.headers.get("X-Import-Preview", ""), raw, request.cookies.get(session_cookie, ""))
        try:
            result = await run_in_threadpool(commit_import, payload)
        except (ValueError, sqlite3.Error) as error:
            if isinstance(error, sqlite3.Error) and is_database_busy(error):
                raise
            raise HTTPException(status_code=400, detail="가져오지 못했습니다. 기존 기록은 유지됩니다.") from error
        return JSONResponse(result, headers={"Cache-Control": "no-store"})


_VIDEO_FIELDS = ("id", "youtube_id", "url", "title", "created_at", "updated_at")
_MEMO_FIELDS = ("id", "video_id", "title", "content", "created_at", "updated_at")


def validate_payload(payload):
    _keys(payload, ("metadata", "records"))
    _keys(payload["metadata"], ("service", "schema_version", "video_count", "memo_count"))
    metadata = payload["metadata"]
    if metadata["service"] != "youtube-memo" or type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise _invalid()
    if not isinstance(payload["records"], list) or len(payload["records"]) > MAX_RECORDS:
        raise _invalid()
    _integer(metadata["video_count"], maximum=MAX_RECORDS)
    _integer(metadata["memo_count"], maximum=MAX_CHILDREN)
    if metadata["video_count"] != len(payload["records"]):
        raise _invalid()
    videos, memos, youtube_ids = set(), set(), set()
    children = 0
    for video in payload["records"]:
        _keys(video, (*_VIDEO_FIELDS, "memos"))
        _unique_id(video["id"], videos)
        _text(video["youtube_id"], 11, True)
        if not service.re.fullmatch(r"[A-Za-z0-9_-]{11}", video["youtube_id"]) or video["youtube_id"] in youtube_ids:
            raise _invalid()
        youtube_ids.add(video["youtube_id"])
        _url(video["url"])
        if service.extract_youtube_id(video["url"]) != video["youtube_id"]:
            raise _invalid()
        _text(video["title"], required=True)
        for field in ("created_at", "updated_at"):
            _timestamp(video[field])
        if not isinstance(video["memos"], list):
            raise _invalid()
        children += len(video["memos"])
        if children > MAX_CHILDREN:
            raise _invalid()
        for memo in video["memos"]:
            _keys(memo, (*_MEMO_FIELDS, "tags", "timestamps"))
            _unique_id(memo["id"], memos)
            _integer(memo["video_id"], 1)
            if memo["video_id"] != video["id"]:
                raise _invalid()
            _text(memo["title"], required=True)
            _text(memo["content"], required=True)
            _tags(memo["tags"])
            expected = [item for item in service.memo_timestamp_segments(memo["content"]) if item["seconds"] is not None]
            if memo["timestamps"] != expected:
                raise _invalid()
            for field in ("created_at", "updated_at"):
                _timestamp(memo[field])
    if children != metadata["memo_count"]:
        raise _invalid()


def preview_import(payload):
    validate_payload(payload)
    service.init_db()
    with service._connect() as connection:
        existing = {row["youtube_id"] for row in connection.execute("SELECT youtube_id FROM videos")}
    skipped = sum(video["youtube_id"] in existing for video in payload["records"])
    return {"record_count": len(payload["records"]), "new_count": len(payload["records"]) - skipped,
            "skip_count": skipped, "duplicate_policy": "skip_existing"}


def commit_import(payload):
    validate_payload(payload)
    service.init_db()
    imported = skipped = 0
    with service._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for video in payload["records"]:
            if connection.execute("SELECT 1 FROM videos WHERE youtube_id = ?", (video["youtube_id"],)).fetchone():
                skipped += 1
                continue
            fields = _VIDEO_FIELDS[1:]
            cursor = connection.execute(f"INSERT INTO videos ({', '.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                        [video[field] for field in fields])
            video_id = cursor.lastrowid
            for memo in video["memos"]:
                fields = _MEMO_FIELDS[2:]
                cursor = connection.execute(f"INSERT INTO memos (video_id, {', '.join(fields)}) VALUES (?,{','.join('?' for _ in fields)})",
                                            [video_id, *(memo[field] for field in fields)])
                service._replace_tags(connection, cursor.lastrowid, memo["tags"])
            imported += 1
    return {"imported_count": imported, "skip_count": skipped, "duplicate_policy": "skip_existing"}
