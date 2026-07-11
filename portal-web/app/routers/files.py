import os
import secrets
import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services import file_store
from app.services.host_urls import portal_home_url, request_host_from_headers
from app.services.security import (
    append_security_event,
    auth_rate_limited,
    clear_auth_failures,
    record_auth_failure,
)


router = APIRouter(prefix="/files")
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
templates.env.filters["filesize"] = file_store.format_size
FILE_ACCESS_COOKIE = "file_manager_access"


@router.post("/login")
def file_login(request: Request, password: str = Form(""), next_path: str = Form("")):
    client = _client_id(request)
    if auth_rate_limited("file_access", client):
        append_security_event("file_access_rate_limited", client=client)
        return _file_login_response(
            request,
            "잠시 후 다시 시도해주세요.",
            status_code=429,
            next_path=next_path,
        )

    configured_password = _file_access_password()
    if not configured_password:
        append_security_event("file_access_password_missing")
        return _file_login_response(
            request,
            "파일함 비밀번호가 설정되지 않았습니다.",
            status_code=403,
            next_path=next_path,
        )

    if not secrets.compare_digest(password, configured_password):
        record_auth_failure("file_access", client)
        append_security_event("file_access_password_failed", client=client)
        return _file_login_response(
            request,
            "파일함 비밀번호가 올바르지 않습니다.",
            status_code=403,
            next_path=next_path,
        )

    clear_auth_failures("file_access", client)
    append_security_event("file_access_granted", client=client)
    response = RedirectResponse(url=_directory_url(next_path), status_code=303)
    response.set_cookie(
        FILE_ACCESS_COOKIE,
        _file_access_cookie_value(configured_password),
        httponly=True,
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@router.get("")
def files_home(request: Request, path: str = ""):
    if not _has_file_access(request):
        return _file_login_response(request, next_path=path)

    try:
        directory = file_store.get_directory(path)
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return templates.TemplateResponse(
        "files.html",
        {
            "request": request,
            "title": "파일함",
            "directory": directory,
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
        },
    )


@router.post("/upload")
def upload_file(request: Request, path: str = Form(""), upload: UploadFile = File(...)):
    _require_file_access(request)
    try:
        file_store.save_upload(path, upload)
    except FileExistsError as exc:
        append_security_event("file_upload_blocked", path=path, reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        append_security_event("file_upload_blocked", path=path, reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect_to_directory(path)


@router.post("/uploads")
def upload_files(request: Request, path: str = Form(""), uploads: list[UploadFile] = File(...)):
    _require_file_access(request)
    try:
        file_store.save_uploads(path, uploads)
    except FileExistsError as exc:
        append_security_event("file_upload_blocked", path=path, reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        append_security_event("file_upload_blocked", path=path, reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect_to_directory(path)


@router.post("/folders")
def create_folder(request: Request, path: str = Form(""), name: str = Form("")):
    _require_file_access(request)
    try:
        file_store.create_directory(path, name)
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail="이미 같은 이름의 폴더가 있습니다.") from exc
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect_to_directory(path)


@router.get("/download")
def download_file(request: Request, path: str):
    _require_file_access(request)
    try:
        file_path = file_store.get_download_path(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    append_security_event("file_downloaded", path=path)
    return FileResponse(file_path, filename=file_path.name)


@router.post("/delete")
def delete_item(request: Request, path: str = Form(""), delete_password: str = Form("")):
    _require_file_access(request)
    _require_delete_password(request, delete_password)

    try:
        file_store.delete_item(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    parent = "/".join(path.strip("/").split("/")[:-1])
    return _redirect_to_directory(parent)


@router.post("/delete-bulk")
def delete_items(
    request: Request,
    current_path: str = Form(""),
    paths: list[str] = Form(...),
    delete_password: str = Form(""),
):
    _require_file_access(request)
    _require_delete_password(request, delete_password)

    try:
        for path in paths:
            file_store.delete_item(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, "redirect": _directory_url(current_path)}


def _redirect_to_directory(path: str) -> RedirectResponse:
    return RedirectResponse(url=_directory_url(path), status_code=303)


def _directory_url(path: str) -> str:
    url = "/files"
    if path:
        url = f"{url}?{urlencode({'path': path})}"
    return url


def _file_access_password() -> str:
    return os.getenv("FILE_MANAGER_ACCESS_PASSWORD", "").strip()


def _file_access_cookie_value(password: str) -> str:
    return hmac.new(
        password.encode("utf-8"),
        b"personal-server-file-manager",
        hashlib.sha256,
    ).hexdigest()


def _has_file_access(request: Request) -> bool:
    cookie = request.cookies.get(FILE_ACCESS_COOKIE, "")
    expected = _file_access_cookie_value(_file_access_password())
    return bool(cookie) and hmac.compare_digest(cookie, expected)


def _require_file_access(request: Request) -> None:
    if not _has_file_access(request):
        raise HTTPException(status_code=401, detail="파일함 인증이 필요합니다.")


def _file_login_response(
    request: Request,
    error: str = "",
    status_code: int = 200,
    next_path: str = "",
):
    return templates.TemplateResponse(
        "file_login.html",
        {
            "request": request,
            "title": "파일함 인증",
            "error": error,
            "next_path": next_path,
            "portal_home_url": portal_home_url(request_host_from_headers(request.headers)),
        },
        status_code=status_code,
    )


def _require_delete_password(request: Request, password: str) -> None:
    configured_password = (
        os.getenv("DELETE_PASSWORD", "").strip()
        or os.getenv("FILE_MANAGER_PASSWORD", "").strip()
    )
    client = _client_id(request)

    if auth_rate_limited("file_delete", client):
        append_security_event("delete_password_rate_limited", client=client)
        raise HTTPException(status_code=429, detail="삭제 비밀번호 실패가 반복되어 잠시 후 다시 시도해주세요.")

    if not configured_password:
        append_security_event("delete_password_missing_config")
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 설정되지 않았습니다.")

    if not secrets.compare_digest(password, configured_password):
        record_auth_failure("file_delete", client)
        append_security_event("delete_password_failed")
        raise HTTPException(status_code=403, detail="삭제 비밀번호가 올바르지 않습니다.")
    clear_auth_failures("file_delete", client)


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"
