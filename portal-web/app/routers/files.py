import os
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi import Depends, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.services import file_store
from app.services.security import (
    append_security_event,
    auth_rate_limited,
    clear_auth_failures,
    record_auth_failure,
)
from shared.host_urls import portal_home_url, request_host_from_headers


security = HTTPBasic(auto_error=False)


def require_file_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    password = os.getenv("FILE_MANAGER_PASSWORD", "")
    client = _client_id(request)

    if auth_rate_limited("file_auth", client):
        append_security_event("file_auth_rate_limited", client=client)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="로그인 실패가 반복되어 잠시 후 다시 시도해주세요.",
        )

    if not password:
        if _file_auth_required():
            append_security_event("file_auth_blocked", reason="password_not_configured")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="파일함 관리자 비밀번호가 설정되지 않았습니다.",
            )
        return

    is_valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, "len")
        and secrets.compare_digest(credentials.password, password)
    )
    if not is_valid:
        record_auth_failure("file_auth", client)
        append_security_event("file_auth_failed", username=credentials.username if credentials else "")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="파일함 로그인이 필요합니다.",
            headers={"WWW-Authenticate": "Basic"},
        )
    clear_auth_failures("file_auth", client)


router = APIRouter(prefix="/files", dependencies=[Depends(require_file_auth)])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["filesize"] = file_store.format_size


@router.get("")
def files_home(request: Request, path: str = ""):
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
def upload_file(path: str = Form(""), upload: UploadFile = File(...)):
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
def upload_files(path: str = Form(""), uploads: list[UploadFile] = File(...)):
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
def create_folder(path: str = Form(""), name: str = Form("")):
    try:
        file_store.create_directory(path, name)
    except FileExistsError as exc:
        raise HTTPException(status_code=400, detail="이미 같은 이름의 폴더가 있습니다.") from exc
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect_to_directory(path)


@router.get("/download")
def download_file(path: str):
    try:
        file_path = file_store.get_download_path(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    append_security_event("file_downloaded", path=path)
    return FileResponse(file_path, filename=file_path.name)


@router.post("/delete")
def delete_item(request: Request, path: str = Form(""), delete_password: str = Form("")):
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


def _file_auth_required() -> bool:
    if _truthy(os.getenv("FILE_MANAGER_AUTH_REQUIRED", "")):
        return True
    return os.getenv("APP_ENV", "").strip().lower() in {"prod", "production"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"
