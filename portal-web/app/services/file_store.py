import os
from pathlib import Path
from typing import Any

from app.services.security import append_security_event


PROJECT_DATA_ROOT = next(
    (
        parent / "data"
        for parent in Path(__file__).resolve().parents
        if (parent / "docker-compose.yml").exists()
    ),
    Path("/data"),
)
DEFAULT_STORAGE_PATH = PROJECT_DATA_ROOT / "files"
STORAGE_PATH = Path(os.getenv("FILE_STORAGE_PATH", DEFAULT_STORAGE_PATH))
CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = int(os.getenv("FILE_MAX_UPLOAD_MB", "25")) * CHUNK_SIZE
MAX_UPLOAD_FILES = int(os.getenv("FILE_MAX_UPLOAD_FILES", "10"))
MAX_UPLOAD_TOTAL_BYTES = int(os.getenv("FILE_MAX_UPLOAD_TOTAL_MB", "30")) * CHUNK_SIZE
MAX_DOWNLOAD_FILES = int(os.getenv("FILE_MAX_DOWNLOAD_FILES", "100"))
MAX_DOWNLOAD_TOTAL_BYTES = int(os.getenv("FILE_MAX_DOWNLOAD_TOTAL_MB", "500")) * CHUNK_SIZE
BLOCKED_EXTENSIONS = {
    extension.strip().lower().lstrip(".")
    for extension in os.getenv(
        "FILE_BLOCKED_EXTENSIONS",
        "app,bat,cmd,com,dll,dmg,exe,jar,js,msi,php,ps1,sh,vbs",
    ).split(",")
    if extension.strip()
}
ALLOWED_EXTENSIONS = {
    extension.strip().lower().lstrip(".")
    for extension in os.getenv("FILE_ALLOWED_EXTENSIONS", "").split(",")
    if extension.strip()
}


def ensure_storage() -> None:
    STORAGE_PATH.mkdir(parents=True, exist_ok=True)


def get_directory(relative_path: str = "") -> dict[str, Any]:
    ensure_storage()
    current_path = _safe_path(relative_path)
    if not current_path.exists():
        raise FileNotFoundError("폴더를 찾을 수 없습니다.")
    if not current_path.is_dir():
        raise NotADirectoryError("폴더 경로가 아닙니다.")

    directories = []
    files = []
    for item in sorted(current_path.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
        if item.name.startswith("."):
            continue

        stat = item.stat()
        entry = {
            "name": item.name,
            "path": _relative_path(item),
            "modified_at": stat.st_mtime,
        }
        if item.is_dir():
            entry["type"] = "folder"
            directories.append(entry)
        elif item.is_file():
            entry["type"] = "file"
            entry["size"] = stat.st_size
            files.append(entry)

    return {
        "current_path": _relative_path(current_path),
        "parent_path": _parent_relative_path(current_path),
        "breadcrumbs": _breadcrumbs(current_path),
        "directories": directories,
        "files": files,
    }


def save_upload(relative_path: str, upload: Any) -> int:
    ensure_storage()
    directory = _safe_path(relative_path)
    if not directory.is_dir():
        raise NotADirectoryError("업로드할 폴더가 아닙니다.")

    filename = _safe_name(upload.filename or "")
    if not filename:
        raise ValueError("파일 이름이 비어 있습니다.")
    _validate_upload_name(filename)

    destination = _safe_path(str(Path(relative_path) / filename))
    if destination.exists():
        raise FileExistsError("이미 같은 이름의 파일이 있습니다.")

    size = 0
    try:
        with destination.open("wb") as output:
            while True:
                chunk = upload.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise ValueError(f"파일은 {MAX_UPLOAD_BYTES // CHUNK_SIZE}MB 이하만 업로드할 수 있습니다.")
                output.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise

    try:
        append_security_event(
            "file_uploaded",
            path=str(Path(relative_path) / filename),
            size=size,
            content_type=getattr(upload, "content_type", ""),
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return size


def save_uploads(relative_path: str, uploads: list[Any]) -> int:
    if len(uploads) > MAX_UPLOAD_FILES:
        raise ValueError(f"한 번에 최대 {MAX_UPLOAD_FILES}개 파일만 업로드할 수 있습니다.")

    saved_count = 0
    total_size = 0
    saved_paths: list[Path] = []
    try:
        for upload in uploads:
            size = save_upload(relative_path, upload)
            saved_count += 1
            total_size += size
            saved_paths.append(_safe_path(str(Path(relative_path) / _safe_name(upload.filename or ""))))
            if total_size > MAX_UPLOAD_TOTAL_BYTES:
                raise ValueError(
                    f"일괄 업로드 파일의 합계는 {MAX_UPLOAD_TOTAL_BYTES // CHUNK_SIZE}MB 이하만 허용됩니다."
                )
    except Exception:
        for saved_path in saved_paths:
            saved_path.unlink(missing_ok=True)
        raise
    return saved_count


def validate_download_limits(relative_paths: list[str]) -> None:
    file_count = 0
    total_size = 0
    for relative_path in relative_paths:
        item_path = get_download_item_path(relative_path)
        if item_path.is_dir():
            children = (child for child in item_path.rglob("*") if child.is_file())
        else:
            children = (item_path,)
        for child in children:
            file_count += 1
            total_size += child.stat().st_size
            if file_count > MAX_DOWNLOAD_FILES:
                raise ValueError(f"한 번에 최대 {MAX_DOWNLOAD_FILES}개 파일만 다운로드할 수 있습니다.")
            if total_size > MAX_DOWNLOAD_TOTAL_BYTES:
                raise ValueError(
                    f"다운로드 원본 파일의 합계는 {MAX_DOWNLOAD_TOTAL_BYTES // CHUNK_SIZE}MB 이하만 허용됩니다."
                )


def create_directory(relative_path: str, name: str) -> None:
    ensure_storage()
    directory_name = _safe_name(name)
    if not directory_name:
        raise ValueError("폴더 이름을 입력해주세요.")

    target = _safe_path(str(Path(relative_path) / directory_name))
    target.mkdir(parents=False, exist_ok=False)


def get_download_path(relative_path: str) -> Path:
    path = _safe_path(relative_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("파일을 찾을 수 없습니다.")
    return path


def get_download_item_path(relative_path: str) -> Path:
    path = _safe_path(relative_path)
    if not path.exists():
        raise FileNotFoundError("다운로드할 파일 또는 폴더를 찾을 수 없습니다.")
    return path


def delete_item(relative_path: str) -> None:
    path = _safe_path(relative_path)
    if path == STORAGE_PATH.resolve():
        raise ValueError("파일함 루트는 삭제할 수 없습니다.")
    if not path.exists():
        raise FileNotFoundError("삭제할 항목을 찾을 수 없습니다.")
    item_type = "folder" if path.is_dir() else "file"
    if path.is_dir():
        _delete_directory(path)
        append_security_event("file_deleted", path=relative_path, item_type=item_type)
        return
    path.unlink()
    append_security_event("file_deleted", path=relative_path, item_type=item_type)


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _safe_path(relative_path: str) -> Path:
    storage_root = STORAGE_PATH.resolve()
    path = (storage_root / relative_path.strip("/")).resolve()
    if path != storage_root and storage_root not in path.parents:
        raise ValueError("허용되지 않는 경로입니다.")
    return path


def _safe_name(name: str) -> str:
    cleaned = Path(name.strip()).name
    if cleaned in {"", ".", ".."}:
        return ""
    return cleaned


def _validate_upload_name(filename: str) -> None:
    extension = Path(filename).suffix.lower().lstrip(".")
    if not extension:
        raise ValueError("확장자가 없는 파일은 업로드할 수 없습니다.")
    if ALLOWED_EXTENSIONS and extension not in ALLOWED_EXTENSIONS:
        raise ValueError("허용되지 않은 파일 형식입니다.")
    if extension in BLOCKED_EXTENSIONS:
        raise ValueError("보안 정책상 차단된 파일 형식입니다.")


def _delete_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _delete_directory(child)
        else:
            child.unlink()
    path.rmdir()


def _relative_path(path: Path) -> str:
    storage_root = STORAGE_PATH.resolve()
    relative = path.resolve().relative_to(storage_root)
    return "" if str(relative) == "." else relative.as_posix()


def _parent_relative_path(path: Path) -> str | None:
    storage_root = STORAGE_PATH.resolve()
    resolved = path.resolve()
    if resolved == storage_root:
        return None
    return _relative_path(resolved.parent)


def _breadcrumbs(path: Path) -> list[dict[str, str]]:
    parts = []
    current = Path()
    for part in Path(_relative_path(path)).parts:
        current = current / part
        parts.append({"name": part, "path": current.as_posix()})
    return parts
