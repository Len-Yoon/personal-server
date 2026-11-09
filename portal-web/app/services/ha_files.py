"""Isolated HA attachment namespace backed by PG and immutable shared blobs.

The approved enforced-vm-v1 profile uses PG chunks in the namespace transaction.
The older local experiment keeps its filesystem backend. Neither backend runs
blob garbage collection when namespace/trash entries are removed.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import zipfile

SERVICE = "portal-web"
STATE_KEY = "file-namespace"
MAX_ENTRIES = 10000
_STORAGE = threading.local()
_RESERVED = {"pgdata", "pg_wal", "pg_xlog", "pg_version", "postgresql.conf", "pg_hba.conf", "id_rsa", "id_ed25519", "credentials", "credentials.json"}
_PRIVATE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".key", ".pem", ".p12", ".pfx"}


def enabled():
    return "HA_PG_CONFIG" in os.environ


def runtime():
    import ha_runtime
    return ha_runtime


def _store():
    from app.services import file_store
    return file_store


def _unavailable():
    return runtime().DatabaseUnavailable("attachment_store_not_ready")


def _guard():
    from ha_runtime.config import load
    config = load(SERVICE)
    if (os.environ.get('APP_ENV') == 'production' and isinstance(config, dict)
            and config.get('profile') == 'enforced-vm-v1'):
        return config  # Server readiness is checked inside the state transaction.
    if os.environ.get("APP_ENV") not in {"test", "development"} or os.environ.get("HA_LOCAL_RUNTIME") != "1":
        raise _unavailable()
    return config


def _path(path, *, root=False):
    if not isinstance(path, str) or path.startswith("/") or "\\" in path:
        raise ValueError("허용되지 않는 경로입니다.")
    parts = path.split("/") if path else []
    if (not parts and not root) or len(parts) > 64 or any(
        not part or part.startswith(".") or len(part.encode()) > 255
        or part.casefold() in _RESERVED or Path(part).suffix.casefold() in _PRIVATE_EXTENSIONS
        or "\x00" in part for part in parts
    ):
        raise ValueError("숨김 경로와 운영 데이터는 사용할 수 없습니다.")
    return "/".join(parts)


def _name(name):
    _path(name)
    if "/" in name:
        raise ValueError("파일 이름을 확인해주세요.")
    return name


def _default():
    return {"format": 1, "version": 0, "entries": {}, "trash": {}, "confirmations": {}, "audit": []}


def _validate(state):
    if (not isinstance(state, dict) or state.get("format") != 1
            or not isinstance(state.get("entries"), dict)
            or not isinstance(state.get("trash"), dict)
            or not isinstance(state.get("confirmations"), dict)
            or type(state.get("version")) is not int):
        raise _unavailable()
    groups = [state["entries"]]
    for token, item in state["trash"].items():
        if (not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token)
                or not isinstance(item, dict) or not isinstance(item.get("entries"), dict)):
            raise _unavailable()
        groups.append(item["entries"])
    if sum(len(group) for group in groups) > MAX_ENTRIES:
        raise _unavailable()
    for group in groups:
        for path, item in group.items():
            try:
                _path(path)
            except ValueError:
                raise _unavailable() from None
            if not isinstance(item, dict) or item.get("type") not in {"folder", "file"}:
                raise _unavailable()
            if item["type"] == "file" and (type(item.get("size")) is not int or item["size"] < 0
                    or not isinstance(item.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", item["sha256"])):
                raise _unavailable()


def _audit(state, event, paths):
    # This protected journal is committed atomically with the namespace change.
    records = state.setdefault("audit", [])
    if not isinstance(records, list):
        raise _unavailable()
    records.append({"event": event, "paths": paths, "at": datetime.now(timezone.utc).isoformat(),
                    "version": state["version"] + 1})
    del records[:-500]


@contextmanager
def _state(*, mutate=False):
    config = _guard()
    pg = isinstance(config, dict) and config.get('profile') == 'enforced-vm-v1'
    with runtime().state_transaction(SERVICE, STATE_KEY, _default()) as state:
        if pg:
            from ha_runtime import blobs
            blobs.require_ready(SERVICE)
        _validate(state)
        before = copy.deepcopy(state) if mutate else None
        previous = getattr(_STORAGE, 'pg', False)
        _STORAGE.pg = pg
        try:
            yield state
        finally:
            _STORAGE.pg = previous
        if mutate and state != before:
            state["version"] += 1
            _validate(state)


@contextmanager
def _root():
    config = _guard()
    if isinstance(config, dict) and config.get('profile') == 'enforced-vm-v1':
        raise _unavailable()
    value = os.environ.get("HA_FILE_OBJECT_ROOT", "")
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts:
        raise _unavailable()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if index != len(path.parts[1:]) - 1:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _unavailable()
        yield descriptor, path
    except FileExistsError:
        raise
    except OSError:
        raise _unavailable() from None
    finally:
        os.close(descriptor)


def ensure_storage():
    config = _guard()
    if isinstance(config, dict) and config.get('profile') == 'enforced-vm-v1':
        with _state():
            pass
        return
    with _root():
        pass


@contextmanager
def _blob_root():
    if getattr(_STORAGE, 'pg', False):
        yield None, None
    else:
        with _root() as root:
            yield root


def _entry(state, path, *, directory=False):
    path = _path(path, root=directory)
    if directory and not path:
        return {"type": "folder"}
    entry = state["entries"].get(path)
    if entry is None:
        raise FileNotFoundError("항목을 찾을 수 없습니다.")
    if directory and entry["type"] != "folder":
        raise NotADirectoryError("폴더 경로가 아닙니다.")
    return entry


def _selected(state, paths):
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise ValueError("선택한 항목이 없습니다.")
    result = []
    for path in paths:
        _path(path)
        _entry(state, path)
        if any(path.startswith(other + "/") for other in paths if other != path):
            raise ValueError("폴더와 그 안의 항목을 동시에 선택할 수 없습니다.")
        result.extend((key, value) for key, value in state["entries"].items()
                      if (key == path or key.startswith(path + "/")) and value["type"] == "file")
    return result


def _verify_blob(root_fd, entry):
    digest = entry.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise _unavailable()
    descriptor = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o222
                or info.st_uid != os.geteuid() or info.st_size != entry.get("size")):
            raise _unavailable()
        hashed = hashlib.sha256()
        while chunk := os.read(descriptor, _store().CHUNK_SIZE):
            hashed.update(chunk)
        if hashed.hexdigest() != digest:
            raise _unavailable()
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _publish(root_fd, root_path, upload, used, total):
    store = _store()
    if getattr(_STORAGE, 'pg', False):
        from ha_runtime import blobs
        maximum = min(store.MAX_UPLOAD_BYTES, store.STORAGE_QUOTA_BYTES - used, blobs.MAX_BLOB_BYTES)
        if total is not None:
            maximum = min(maximum, store.MAX_UPLOAD_TOTAL_BYTES - total)
        def validate(prefix):
            if (prefix.startswith(b'SQLite format 3\x00') or prefix[:4] in (bytes.fromhex('377f0682'), bytes.fromhex('377f0683'))
                    or b'PRIVATE KEY-----' in prefix):
                raise ValueError('운영 데이터와 개인 키는 업로드할 수 없습니다.')
        if maximum < 0:
            raise ValueError('파일함 전체 용량 제한을 초과합니다.')
        result = blobs.put(SERVICE, upload.file, max_bytes=maximum,
                           max_namespace_bytes=min(store.STORAGE_QUOTA_BYTES, blobs.MAX_NAMESPACE_BYTES),
                           validate_prefix=validate)
        return dict(type='file', **result, content_type=str(getattr(upload, 'content_type', '') or '')[:255], modified_at=time.time())
    temporary = "." + secrets.token_hex(24) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    digest, size, first = hashlib.sha256(), 0, b""
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            while chunk := upload.file.read(store.CHUNK_SIZE):
                first = (first + chunk)[:8192]
                size += len(chunk)
                if size > store.MAX_UPLOAD_BYTES or (total is not None and total + size > store.MAX_UPLOAD_TOTAL_BYTES):
                    raise ValueError("업로드 파일 용량 제한을 초과합니다.")
                if used + size > store.STORAGE_QUOTA_BYTES:
                    raise ValueError("파일함 전체 용량 제한을 초과합니다.")
                if shutil.disk_usage(root_path).free - len(chunk) < store.MIN_FREE_BYTES:
                    raise ValueError("디스크 여유 공간이 부족합니다.")
                digest.update(chunk)
                output.write(chunk)
            if (first.startswith(b"SQLite format 3\x00") or first[:4] in (bytes.fromhex("377f0682"), bytes.fromhex("377f0683"))
                    or b"PRIVATE KEY-----" in first):
                raise ValueError("운영 데이터와 개인 키는 업로드할 수 없습니다.")
            output.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        identifier = digest.hexdigest()
        try:
            # Hard-link publication is exclusive; unlinking the temporary name
            # leaves one immutable inode. No mutable destination is overwritten.
            os.link(temporary, identifier, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
        except FileExistsError:
            existing = _verify_blob(root_fd, {"sha256": identifier, "size": size})
            os.close(existing)
        os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
        return {"type": "file", "sha256": identifier, "size": size,
                "content_type": str(getattr(upload, "content_type", "") or "")[:255], "modified_at": time.time()}
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def _occupied(state):
    records = list(state["entries"].values())
    for item in state["trash"].values():
        records.extend(item["entries"].values())
    return sum(entry["size"] for entry in records if entry["type"] == "file")


def save_uploads(path, uploads, *, single=False):
    store = _store()
    _path(path, root=True)
    if len(uploads) > store.MAX_UPLOAD_FILES:
        raise ValueError("일괄 업로드 파일 수 제한을 초과합니다.")
    sizes = []
    with _state(mutate=True) as state:
        _entry(state, path, directory=True)
        used = _occupied(state)
        with _blob_root() as (root_fd, root_path):
            for upload in uploads:
                name = store._safe_name(upload.filename or "")
                _name(name)
                store._validate_upload_name(name)
                target = "/".join(filter(None, [path, name]))
                if target in state["entries"]:
                    raise FileExistsError("이미 같은 이름의 파일이 있습니다.")
                entry = _publish(root_fd, root_path, upload, used, None if single else sum(sizes))
                state["entries"][target] = entry
                sizes.append(entry["size"])
                used += entry["size"]
        _audit(state, "file_uploaded", ["/".join(filter(None, [path, _store()._safe_name(upload.filename or "")])) for upload in uploads])
    # Commit and fresh replication protection completed before returning success.
    return sizes[0] if single else len(sizes)


def create_directory(relative_path, name):
    path = relative_path
    _path(path, root=True)
    _name(name)
    target = "/".join(filter(None, [path, name]))
    with _state(mutate=True) as state:
        _entry(state, path, directory=True)
        if target in state["entries"]:
            raise FileExistsError("이미 같은 이름의 폴더가 있습니다.")
        state["entries"][target] = {"type": "folder", "modified_at": time.time()}
        _audit(state, "folder_created", [target])


def get_directory(path=""):
    _path(path, root=True)
    with _state() as state:
        _entry(state, path, directory=True)
        items = []
        for key, entry in state["entries"].items():
            if key.rpartition("/")[0] == path:
                items.append({"name": key.rsplit("/", 1)[-1], "path": key, **entry})
        items.sort(key=lambda item: (item["type"] == "file", item["name"].lower()))
        parts = path.split("/") if path else []
        return {"current_path": path, "parent_path": path.rpartition("/")[0] if path else None,
                "breadcrumbs": [{"name": part, "path": "/".join(parts[:index+1])} for index, part in enumerate(parts)],
                "directories": [item for item in items if item["type"] == "folder"],
                "files": [item for item in items if item["type"] == "file"]}


class DownloadPath(type(Path())):
    def __fspath__(self):
        return self._descriptor_path()

    def __str__(self):
        return self._descriptor_path()

    def _descriptor_path(self):
        descriptor = getattr(self, "_descriptor", None)
        if descriptor is None:
            raise ValueError("다운로드 파일이 닫혔습니다.")
        # Darwin's /dev/fd open duplicates the file description and its offset.
        # Each path is request-owned; rewind before a fresh consumer opens it.
        os.lseek(descriptor, 0, os.SEEK_SET)
        return ("/proc/self/fd/" if sys.platform == "linux" else "/dev/fd/") + str(descriptor)

    @property
    def name(self):
        return self.download_name

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if mode not in {"r", "rb"}:
            raise _unavailable()
        return open(self._descriptor_path(), mode, buffering=buffering, encoding=encoding,
                    errors=errors, newline=newline)

    def close(self):
        descriptor = getattr(self, "_descriptor", None)
        if descriptor is not None:
            self._descriptor = None
            os.close(descriptor)

    def __del__(self):
        self.close()


def _download_snapshot(root_fd, entry):
    # The HTTP server reopens paths asynchronously. Snapshot validated bytes to
    # an anonymous local inode, retain a read-only FD, and serve that FD path.
    # Namespace changes, shared-path swaps and later shared-inode mutation
    # cannot change bytes after validation or resurrect a deleted blob path.
    if getattr(_STORAGE, 'pg', False):
        from ha_runtime import blobs
        payload = blobs.get(SERVICE, entry['sha256'], entry['size'])
        with tempfile.TemporaryFile() as snapshot:
            snapshot.write(payload); snapshot.flush(); snapshot.seek(0)
            os.fchmod(snapshot.fileno(), 0o400)
            fd_path = ('/proc/self/fd/' if sys.platform == 'linux' else '/dev/fd/') + str(snapshot.fileno())
            return os.open(fd_path, os.O_RDONLY)
    source = _verify_blob(root_fd, entry)
    try:
        with tempfile.TemporaryFile() as snapshot:
            hashed = hashlib.sha256()
            size = 0
            while chunk := os.read(source, _store().CHUNK_SIZE):
                hashed.update(chunk)
                size += len(chunk)
                snapshot.write(chunk)
            if size != entry["size"] or hashed.hexdigest() != entry["sha256"]:
                raise _unavailable()
            snapshot.flush()
            snapshot.seek(0)
            os.fchmod(snapshot.fileno(), 0o400)
            fd_path = ("/proc/self/fd/" if sys.platform == "linux" else "/dev/fd/") + str(snapshot.fileno())
            return os.open(fd_path, os.O_RDONLY)
    finally:
        os.close(source)


def get_download_path(path):
    _path(path)
    with _state() as state:
        entry = _entry(state, path)
        if entry["type"] != "file":
            raise FileNotFoundError("파일을 찾을 수 없습니다.")
        with _blob_root() as (root_fd, root_path):
            descriptor = _download_snapshot(root_fd, entry)
            result = DownloadPath((root_path or Path('/pg-blob-snapshot')) / entry["sha256"])
            result._descriptor = descriptor
            result.download_name = path.rsplit("/", 1)[-1]
            return result


class NamespacePath(type(Path())):
    def is_dir(self):
        return self.entry["type"] == "folder"

    def is_file(self):
        return self.entry["type"] == "file"

    def exists(self):
        return True

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if mode not in {"r", "rb"}:
            raise _unavailable()
        if self.is_dir():
            raise IsADirectoryError("폴더 경로입니다.")
        return get_download_path(self.logical_path).open(mode, buffering, encoding, errors, newline)

    def stat(self, *, follow_symlinks=True):
        mode = stat.S_IFDIR | 0o700 if self.is_dir() else stat.S_IFREG | 0o400
        return os.stat_result((mode, 0, 0, 1, os.geteuid(), os.getegid(),
                               self.entry.get("size", 0), 0, self.entry.get("modified_at", 0), 0))


def get_download_item_path(path):
    _path(path)
    with _state() as state:
        entry = _entry(state, path)
        result = NamespacePath(_store().STORAGE_PATH / path)
        result.logical_path, result.entry = path, copy.deepcopy(entry)
        return result


def iter_download_files(directory):
    if not isinstance(directory, NamespacePath):
        raise ValueError("공유 파일함 경로를 확인해주세요.")
    with _state() as state:
        selected = _selected(state, [directory.logical_path])
    for path, _ in selected:
        yield get_download_item_path(path)


def validate_download_limits(paths):
    store = _store()
    with _state() as state:
        selected = _selected(state, paths)
        if len(selected) > store.MAX_DOWNLOAD_FILES or sum(entry["size"] for _, entry in selected) > store.MAX_DOWNLOAD_TOTAL_BYTES:
            raise ValueError("일괄 다운로드 제한을 초과합니다.")


def write_download_archive(archive, paths):
    store = _store()
    with _state() as state:
        selected = _selected(state, paths)
        if len(selected) > store.MAX_DOWNLOAD_FILES or sum(entry["size"] for _, entry in selected) > store.MAX_DOWNLOAD_TOTAL_BYTES:
            raise ValueError("일괄 다운로드 제한을 초과합니다.")
        with _blob_root() as (root_fd, _):
            for path, entry in selected:
                descriptor = _download_snapshot(root_fd, entry)
                modified = time.localtime(entry.get("modified_at", 0))[:6]
                if not 1980 <= modified[0] <= 2107:
                    modified = (1980, 1, 1, 0, 0, 0)
                info = zipfile.ZipInfo(path, date_time=modified)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o400) << 16
                with os.fdopen(descriptor, "rb") as source, archive.open(info, "w") as destination:
                    shutil.copyfileobj(source, destination, store.CHUNK_SIZE)


def validate_delete_items(paths):
    with _state() as state:
        _selected(state, paths)
    return list(dict.fromkeys(paths))


def delete_item(relative_path):
    path = relative_path
    with _state(mutate=True) as state:
        _selected(state, [path])
        state["entries"] = {key: value for key, value in state["entries"].items()
                            if key != path and not key.startswith(path + "/")}
        _audit(state, "file_deleted", [path])


def move(path, destination="", new_name=None):
    _path(path)
    _path(destination, root=True)
    name = _name(new_name or path.rsplit("/", 1)[-1])
    result = "/".join(filter(None, [destination, name]))
    if destination == path or destination.startswith(path + "/"):
        raise ValueError("자신의 하위 폴더로 이동할 수 없습니다.")
    with _state(mutate=True) as state:
        entry = _entry(state, path)
        _entry(state, destination, directory=True)
        if entry["type"] == "file":
            _store()._validate_upload_name(name)
        if result == path:
            return result
        if result in state["entries"]:
            raise FileExistsError("이미 같은 이름의 항목이 있습니다.")
        selected = {key: value for key, value in state["entries"].items() if key == path or key.startswith(path + "/")}
        for key in selected:
            del state["entries"][key]
        state["entries"].update({result + key[len(path):]: value for key, value in selected.items()})
        _audit(state, "file_moved", [path, result])
    return result


def search(query):
    query = query.strip().casefold()
    if not query or len(query) > 200:
        raise ValueError("검색어는 1~200자로 입력해주세요.")
    with _state() as state:
        result = [{"name": key.rsplit("/", 1)[-1], "path": key, "type": entry["type"]}
                  for key, entry in sorted(state["entries"].items()) if query in key.rsplit("/", 1)[-1].casefold()]
        return {"items": result[:200], "truncated": len(result) > 200}


def trash(path):
    _path(path)
    token = secrets.token_hex(16)
    with _state(mutate=True) as state:
        _entry(state, path)
        entries = {key: value for key, value in state["entries"].items() if key == path or key.startswith(path + "/")}
        state["trash"][token] = {"path": path, "trashed_at": datetime.now(timezone.utc).isoformat(), "entries": entries}
        for key in entries:
            del state["entries"][key]
        _audit(state, "file_trashed", [path])
    return token


def _trash_items(state):
    now = datetime.now(timezone.utc)
    items = []
    for token, entry in state["trash"].items():
        when = datetime.fromisoformat(entry["trashed_at"])
        items.append({"id": token, "path": entry["path"], "trashed_at": entry["trashed_at"],
                      "trashed_at_display": when.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M"),
                      "size_bytes": sum(value["size"] for value in entry["entries"].values() if value["type"] == "file"),
                      "purge_allowed": True, "age_days": max(0, (now - when).days)})
    return sorted(items, key=lambda item: (item["trashed_at"], item["id"]), reverse=True)


def list_trash():
    with _state() as state:
        return _trash_items(state)


def trash_page(*, page=1, page_size=50):
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError("페이지 범위를 확인해주세요.")
    with _state() as state:
        items = _trash_items(state)
        pages = max(1, (len(items) + page_size - 1) // page_size)
        page = min(page, pages)
        return {"items": items[(page-1)*page_size:page*page_size], "total": len(items), "pages": pages, "page": page,
                "size_bytes": sum(item["size_bytes"] for item in items), "size_complete": True, "excluded": 0}


def restore(token, new_name=""):
    if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token):
        raise ValueError("휴지통 항목을 확인해주세요.")
    with _state(mutate=True) as state:
        record = state["trash"].get(token)
        if not record:
            raise FileNotFoundError("휴지통 항목을 찾을 수 없습니다.")
        path = record["path"]
        parent, _, original = path.rpartition("/")
        name = _name(new_name or original)
        result = "/".join(filter(None, [parent, name]))
        _entry(state, parent, directory=True)
        if record["entries"][path]["type"] == "file":
            _store()._validate_upload_name(name)
        if result in state["entries"]:
            raise FileExistsError("복원 위치에 같은 이름이 있습니다.")
        state["entries"].update({result + key[len(path):]: value for key, value in record["entries"].items()})
        del state["trash"][token]
        _audit(state, "file_restored", [result])
    return result


def _purge_selection(state, item_id, older_than_days, before):
    if older_than_days is not None and (type(older_than_days) is not int or not 0 <= older_than_days <= 36500):
        raise ValueError("보관 일수는 0~36500으로 입력해주세요.")
    items = _trash_items(state)
    if item_id is not None:
        if not isinstance(item_id, str) or not re.fullmatch("[0-9a-f]{32}", item_id) or item_id not in state["trash"]:
            raise ValueError("휴지통 항목을 확인해주세요.")
        items = [item for item in items if item["id"] == item_id]
    if older_than_days is not None:
        items = [item for item in items if datetime.fromisoformat(item["trashed_at"]).timestamp() <= before - older_than_days * 86400]
    return items


def _selection_digest(state, items):
    return hashlib.sha256(json.dumps({item["id"]: state["trash"][item["id"]] for item in items}, sort_keys=True).encode()).hexdigest()


def preview_purge(*, item_id=None, older_than_days=None):
    before, token = time.time(), secrets.token_urlsafe(32)
    with _state(mutate=True) as state:
        items = _purge_selection(state, item_id, older_than_days, before)
        state["confirmations"] = {key: value for key, value in state["confirmations"].items() if before - value["before"] <= 600}
        if len(state["confirmations"]) >= 100:
            raise ValueError("미리보기를 잠시 후 다시 요청해주세요.")
        state["confirmations"][hashlib.sha256(token.encode()).hexdigest()] = {"before": before, "item_id": item_id, "older_than_days": older_than_days,
                                         "digest": _selection_digest(state, items)}
        return {"items": items, "count": len(items), "size_bytes": sum(item["size_bytes"] for item in items),
                "excluded": 0, "confirmation": token}


def confirm_purge(confirmation):
    with _state(mutate=True) as state:
        token_hash = hashlib.sha256(confirmation.encode()).hexdigest() if isinstance(confirmation, str) else None
        record = state["confirmations"].get(token_hash)
        if not record or not 0 <= time.time() - record["before"] <= 600:
            raise ValueError("미리보기가 만료되었거나 올바르지 않습니다.")
        items = _purge_selection(state, record["item_id"], record["older_than_days"], record["before"])
        if _selection_digest(state, items) != record["digest"]:
            raise ValueError("휴지통 내용이 변경되었습니다. 미리보기를 다시 확인해주세요.")
        for item in items:
            del state["trash"][item["id"]]
        del state["confirmations"][token_hash]
        _audit(state, "file_trash_purged", [item["path"] for item in items])
    return {"ok": True, "deleted": [item["id"] for item in items], "failed": []}


def backup_manifest():
    """Export PG references for a later pinned backup; this is not a backup."""
    with _state() as state:
        snapshot = copy.deepcopy(state)
        snapshot["blob_gc"] = "disabled-until-backup-pins"
        return snapshot
