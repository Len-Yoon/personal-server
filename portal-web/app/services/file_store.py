import errno
import os
import stat
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    visible_items = (
        item for item in current_path.iterdir()
        if not item.name.startswith(".") and not item.is_symlink()
    )
    for item in sorted(visible_items, key=lambda path: (path.is_file(), path.name.lower())):
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
    size = 0
    try:
        output = destination.open("xb")
    except FileExistsError:
        raise FileExistsError("이미 같은 이름의 파일이 있습니다.") from None

    try:
        with output:
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
            children = iter_download_files(item_path)
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


def iter_download_files(directory: Path) -> Iterator[Path]:
    for child in _checked_descendants(directory):
        if child.is_file():
            yield child


def write_download_archive(archive: zipfile.ZipFile, relative_paths: list[str]) -> None:
    file_count = 0
    total_size = 0

    def add_file(relative_path: str, archive_name: str) -> None:
        nonlocal file_count, total_size
        with _open_storage_item(relative_path) as source_fd:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError("일반 파일만 다운로드할 수 있습니다.")
            file_count += 1
            if file_count > MAX_DOWNLOAD_FILES:
                raise ValueError(f"한 번에 최대 {MAX_DOWNLOAD_FILES}개 파일만 다운로드할 수 있습니다.")
            if total_size + source_stat.st_size > MAX_DOWNLOAD_TOTAL_BYTES:
                raise ValueError(
                    f"다운로드 원본 파일의 합계는 {MAX_DOWNLOAD_TOTAL_BYTES // CHUNK_SIZE}MB 이하만 허용됩니다."
                )
            modified = time.localtime(source_stat.st_mtime)[:6]
            if not 1980 <= modified[0] <= 2107:
                modified = (1980, 1, 1, 0, 0, 0)
            info = zipfile.ZipInfo(archive_name, date_time=modified)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (source_stat.st_mode & 0xFFFF) << 16
            with os.fdopen(os.dup(source_fd), "rb") as source:
                with archive.open(info, "w") as output:
                    while chunk := source.read(CHUNK_SIZE):
                        total_size += len(chunk)
                        if total_size > MAX_DOWNLOAD_TOTAL_BYTES:
                            raise ValueError(
                                f"다운로드 원본 파일의 합계는 {MAX_DOWNLOAD_TOTAL_BYTES // CHUNK_SIZE}MB 이하만 허용됩니다."
                            )
                        output.write(chunk)

    storage_root = STORAGE_PATH.resolve()
    for relative_path in relative_paths:
        item_path = get_download_item_path(relative_path)
        archive_name = Path(relative_path).as_posix().lstrip("/")
        if item_path.is_dir():
            for child in iter_download_files(item_path):
                child_relative = child.relative_to(storage_root).as_posix()
                child_name = child.relative_to(item_path).as_posix()
                add_file(child_relative, f"{archive_name}/{child_name}")
        else:
            add_file(relative_path, archive_name)


@contextmanager
def _open_storage_item(relative_path: str) -> Iterator[int]:
    storage_root = STORAGE_PATH.resolve()
    descriptor = os.open(storage_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(relative_path.strip("/")).parts:
            if part == "..":
                raise ValueError("허용되지 않는 경로입니다.")
            child = _open_child_fd(descriptor, part)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _open_child_fd(parent_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("심볼릭 링크 경로는 허용되지 않습니다.") from exc
        raise


def _preflight_directory_fd(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        child_fd = _open_child_fd(directory_fd, name)
        try:
            mode = os.fstat(child_fd).st_mode
            if stat.S_ISDIR(mode):
                _preflight_directory_fd(child_fd)
            elif not stat.S_ISREG(mode):
                raise ValueError("일반 파일과 폴더만 삭제할 수 있습니다.")
        finally:
            os.close(child_fd)


def _delete_directory_fd(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        child_fd = _open_child_fd(directory_fd, name)
        try:
            mode = os.fstat(child_fd).st_mode
            if stat.S_ISDIR(mode):
                _delete_directory_fd(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(mode):
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise ValueError("일반 파일과 폴더만 삭제할 수 있습니다.")
        finally:
            os.close(child_fd)


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
    if path.is_dir():
        for _ in _checked_descendants(path):
            pass

    parts = Path(relative_path.strip("/")).parts
    parent_path = Path(*parts[:-1]).as_posix() if len(parts) > 1 else ""
    with _open_storage_item(parent_path) as parent_fd:
        target_fd = _open_child_fd(parent_fd, parts[-1])
        try:
            mode = os.fstat(target_fd).st_mode
            if stat.S_ISDIR(mode):
                _preflight_directory_fd(target_fd)
                _delete_directory_fd(target_fd)
                os.rmdir(parts[-1], dir_fd=parent_fd)
                item_type = "folder"
            elif stat.S_ISREG(mode):
                os.unlink(parts[-1], dir_fd=parent_fd)
                item_type = "file"
            else:
                raise ValueError("일반 파일과 폴더만 삭제할 수 있습니다.")
        finally:
            os.close(target_fd)
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
    path = storage_root
    for part in Path(relative_path.strip("/")).parts:
        if part == "..":
            raise ValueError("허용되지 않는 경로입니다.")
        path = path / part
        if path.is_symlink():
            raise ValueError("심볼릭 링크 경로는 허용되지 않습니다.")
    resolved = path.resolve()
    if resolved != storage_root and storage_root not in resolved.parents:
        raise ValueError("허용되지 않는 경로입니다.")
    return resolved


def _checked_descendants(directory: Path) -> Iterator[Path]:
    for child in directory.rglob("*"):
        if child.is_symlink():
            raise ValueError("심볼릭 링크 항목은 허용되지 않습니다.")
        yield child


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
