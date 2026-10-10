"""Descriptor-relative file operations and reversible trash.

Linux/Darwin atomic exclusive rename prevents overwriting even with concurrent
requests. Unsupported kernels fail closed rather than use check-then-rename.
"""
import ctypes
import base64
import fcntl
import hashlib
import hmac
import secrets
import time
import errno
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services import file_store


MAX_SEARCH_ENTRIES = 10000
MAX_SEARCH_RESULTS = 200
_PURGE_KEY = secrets.token_bytes(32)
PURGE_PREVIEW_MAX_AGE = 600


class SnapshotLimitError(ValueError):
    """The item remains restorable, but cannot be fully measured safely."""


class TrashCompletedError(OSError):
    """The move succeeded; syncing its journal failed. Never retry this path."""
    def __init__(self, token):
        super().__init__('이동은 완료됐으나 저장 확인에 실패했습니다.')
        self.token = token


def _parts(path, *, root=False):
    if not isinstance(path, str) or '\\' in path or path.startswith('/'):
        raise ValueError('허용되지 않는 경로입니다.')
    parts = path.split('/') if path else []
    if any(not p or p.startswith('.') for p in parts) or (not parts and not root):
        raise ValueError('숨김 경로와 상위 경로는 사용할 수 없습니다.')
    return parts


def _name(name):
    _parts(name)
    if '/' in name or len(name.encode()) > 255:
        raise ValueError('파일 이름을 확인해주세요.')
    return name


@contextmanager
def _parent(path):
    parts = _parts(path)
    with file_store._open_storage_item('/'.join(parts[:-1])) as fd:
        yield fd, parts[-1]


def _exclusive_move(source_fd, name, destination_fd, new_name):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'linux' and hasattr(libc, 'renameat2'):
        call, flag = libc.renameat2, 1  # RENAME_NOREPLACE
    elif sys.platform == 'darwin' and hasattr(libc, 'renameatx_np'):
        call, flag = libc.renameatx_np, 4  # RENAME_EXCL
    else:
        raise OSError('이 환경에서는 안전한 이름 변경을 지원하지 않습니다.')
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(source_fd, os.fsencode(name), destination_fd, os.fsencode(new_name), flag):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError('이미 같은 이름의 항목이 있습니다.')
        raise OSError(code, '항목을 이동할 수 없습니다.')


def _check_source(fd, name, new_name=None):
    child = file_store._open_child_fd(fd, name)
    try:
        mode = os.fstat(child).st_mode
        if stat.S_ISREG(mode):
            if new_name:
                file_store._validate_upload_name(new_name)
        elif stat.S_ISDIR(mode):
            file_store._preflight_directory_fd(child)
        else:
            raise ValueError('일반 파일과 폴더만 처리할 수 있습니다.')
    finally:
        os.close(child)


def move(path, destination='', new_name=None):
    parts = _parts(path)
    target_parts = _parts(destination, root=True)
    name = _name(new_name or parts[-1])
    result = '/'.join(target_parts + [name])
    if result == path:
        return result
    if target_parts[:len(parts)] == parts:
        raise ValueError('폴더를 자신이나 하위 폴더로 이동할 수 없습니다.')
    with _parent(path) as (source_fd, source_name):
        _check_source(source_fd, source_name, name)
        with file_store._open_storage_item(destination) as destination_fd:
            if not stat.S_ISDIR(os.fstat(destination_fd).st_mode):
                raise NotADirectoryError('대상 폴더를 확인해주세요.')
            _exclusive_move(source_fd, source_name, destination_fd, name)
    return result


def search(query):
    query = query.strip().casefold()
    if not query or len(query) > 200:
        raise ValueError('검색어는 1~200자로 입력해주세요.')
    results, visited, truncated = [], 0, False
    def visit(fd, prefix, depth=0):
        nonlocal visited, truncated
        if depth > 64:
            truncated = True
            return
        with os.scandir(fd) as scan:
            for entry in scan:
                if entry.name.startswith('.'):
                    continue
                visited += 1
                if visited > MAX_SEARCH_ENTRIES or len(results) >= MAX_SEARCH_RESULTS:
                    truncated = True
                    return
                try:
                    child = file_store._open_child_fd(fd, entry.name)
                except (OSError, ValueError):
                    continue
                try:
                    mode = os.fstat(child).st_mode
                    directory = stat.S_ISDIR(mode)
                    if not directory and not stat.S_ISREG(mode):
                        continue
                    path = '/'.join(prefix + [entry.name])
                    if query in entry.name.casefold():
                        results.append({'name': entry.name, 'path': path, 'type': 'folder' if directory else 'file'})
                    if directory:
                        visit(child, prefix + [entry.name], depth + 1)
                finally:
                    os.close(child)
                if truncated:
                    return
    file_store.ensure_storage()
    with file_store._open_storage_item('') as fd:
        visit(fd, [])
    return {'items': results, 'truncated': truncated}


@contextmanager
def _trash_fd():
    file_store.ensure_storage()
    with file_store._open_storage_item('') as root:
        try:
            os.mkdir('.trash', mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        fd = os.open('.trash', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            # Serialize trash, restore and purge across application workers.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)


def trash(path):
    _parts(path)
    token = uuid.uuid4().hex
    with _parent(path) as (source_fd, name):
        _check_source(source_fd, name)
        with _trash_fd() as trash_fd:
            os.mkdir(token, mode=0o700, dir_fd=trash_fd)
            entry = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            try:
                metadata = {'path': path, 'trashed_at': datetime.now(timezone.utc).isoformat()}
                meta_fd = os.open('metadata.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=entry)
                with os.fdopen(meta_fd, 'w') as output:
                    json.dump(metadata, output)
                    output.flush()
                    os.fsync(output.fileno())
                _exclusive_move(source_fd, name, entry, 'item')
                try:
                    os.fsync(entry)
                    os.fsync(source_fd)
                except OSError as exc:
                    raise TrashCompletedError(token) from exc
            finally:
                os.close(entry)
    return token


def _metadata(fd):
    meta_fd = os.open('metadata.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    if not stat.S_ISREG(os.fstat(meta_fd).st_mode):
        os.close(meta_fd)
        raise ValueError('휴지통 기록을 확인해주세요.')
    with os.fdopen(meta_fd, 'r') as source:
        value = json.loads(source.read(8193))
    if not isinstance(value, dict) or not isinstance(value.get('path'), str):
        raise ValueError('휴지통 기록을 확인해주세요.')
    _parts(value['path'])
    if not isinstance(value.get('trashed_at'), str):
        raise ValueError('휴지통 기록을 확인해주세요.')
    # Ignore untrusted extra keys: they must never replace the token or stats.
    return {'path': value['path'], 'trashed_at': value['trashed_at']}


def _token(token):
    if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('휴지통 항목을 확인해주세요.')
    return token


def _tree_snapshot(fd, *, depth=0, budget=None):
    # No paths are resolved, and every child is opened without following links.
    budget = [0] if budget is None else budget
    budget[0] += 1
    if depth > 64 or budget[0] > 10000:
        raise SnapshotLimitError('항목이 너무 큽니다. 휴지통을 별도로 점검해주세요.')
    info = os.fstat(fd)
    identity = [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    if stat.S_ISREG(info.st_mode):
        return info.st_size, identity
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('일반 파일과 폴더만 처리할 수 있습니다.')
    total, children = 0, []
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= 10000 - budget[0]:
                raise SnapshotLimitError('항목이 너무 큽니다. 휴지통을 별도로 점검해주세요.')
            names.append(entry.name)
    names.sort()
    for name in names:
        child = file_store._open_child_fd(fd, name)
        try:
            size, snapshot = _tree_snapshot(child, depth=depth+1, budget=budget)
            total += size
            children.append([name, snapshot])
        finally:
            os.close(child)
    return total, [identity, children]


def _snapshot_digest(fd, metadata, snapshot):
    entry = os.fstat(fd)
    return hashlib.sha256(json.dumps([entry.st_dev, entry.st_ino, metadata, snapshot], sort_keys=True).encode()).hexdigest()


def _trash_record_fd(fd, token, now, *, allow_incomplete=False):
    metadata = _metadata(fd)
    when = datetime.fromisoformat(metadata['trashed_at'])
    if when.tzinfo is None:
        raise ValueError('휴지통 시간대를 확인해주세요.')
    child = file_store._open_child_fd(fd, 'item')
    try:
        try:
            size, snapshot = _tree_snapshot(child)
            digest = _snapshot_digest(fd, metadata, snapshot)
        except SnapshotLimitError:
            if not allow_incomplete or not (stat.S_ISREG(os.fstat(child).st_mode) or stat.S_ISDIR(os.fstat(child).st_mode)):
                raise
            size, digest = None, None
    finally:
        os.close(child)
    return {'id': token, **metadata, 'trashed_at_display': when.astimezone(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M'), 'size_bytes': size, 'purge_allowed': digest is not None, 'age_days': max(0, (now-when).days), '_snapshot': digest}


def _trash_record(root, token, now, *, allow_incomplete=False):
    _token(token)
    fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
    try:
        return _trash_record_fd(fd, token, now, allow_incomplete=allow_incomplete)
    finally:
        os.close(fd)


def _trash_records(root, now):
    records, excluded = [], 0
    with os.scandir(root) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        try:
            records.append(_trash_record(root, name, now, allow_incomplete=True))
        except (OSError, ValueError, KeyError, OverflowError):
            excluded += 1
    return sorted(records, key=lambda item: (item['trashed_at'], item['id']), reverse=True), excluded


def list_trash():
    with _trash_fd() as root:
        records, _ = _trash_records(root, datetime.now(timezone.utc))
    return [{key: value for key, value in item.items() if key != '_snapshot'} for item in records]


def trash_page(*, page=1, page_size=50):
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError('페이지 범위를 확인해주세요.')
    with _trash_fd() as root:
        items, excluded = _trash_records(root, datetime.now(timezone.utc))
    total = len(items)
    pages = max(1, (total+page_size-1)//page_size)
    page = min(page, pages)
    return {'items': [{k: v for k, v in item.items() if k != '_snapshot'} for item in items[(page-1)*page_size:page*page_size]], 'total': total, 'pages': pages, 'page': page, 'size_bytes': sum(i['size_bytes'] or 0 for i in items), 'size_complete': excluded == 0 and all(i['purge_allowed'] for i in items), 'excluded': excluded}


def _purge_selection(root, item_id, older_than_days, before):
    if item_id is not None:
        _token(item_id)
        items = [_trash_record(root, item_id, datetime.now(timezone.utc))]
        excluded = 0
    else:
        items, excluded = _trash_records(root, datetime.now(timezone.utc))
        excluded += sum(not item['purge_allowed'] for item in items)
        items = [item for item in items if item['purge_allowed']]
    if older_than_days is not None:
        if type(older_than_days) is not int or not 0 <= older_than_days <= 36500:
            raise ValueError('보관 일수는 0~36500으로 입력해주세요.')
        items = [i for i in items if datetime.fromisoformat(i['trashed_at']).timestamp() <= before - older_than_days*86400]
    return items, excluded


def _selection_digest(items):
    return hashlib.sha256(json.dumps([(i['id'], i['_snapshot']) for i in items]).encode()).hexdigest()


def preview_purge(*, item_id=None, older_than_days=None):
    before = time.time()
    with _trash_fd() as root:
        items, excluded = _purge_selection(root, item_id, older_than_days, before)
    payload = {'item_id': item_id, 'older_than_days': older_than_days, 'before': before, 'digest': _selection_digest(items)}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    signature = hmac.new(_PURGE_KEY, encoded.encode(), hashlib.sha256).hexdigest()
    return {'items': [{k: v for k, v in i.items() if k != '_snapshot'} for i in items], 'count': len(items), 'size_bytes': sum(i['size_bytes'] for i in items), 'excluded': excluded, 'confirmation': encoded + '.' + signature}


def confirm_purge(confirmation):
    try:
        if len(confirmation) > 2048:
            raise ValueError
        encoded, signature = confirmation.split('.')
        expected = hmac.new(_PURGE_KEY, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        if not 0 <= time.time() - payload['before'] <= PURGE_PREVIEW_MAX_AGE:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError('미리보기가 만료되었거나 올바르지 않습니다. 다시 확인해주세요.') from None
    deleted = []
    with _trash_fd() as root:
        items, _ = _purge_selection(root, payload['item_id'], payload['older_than_days'], payload['before'])
        if not hmac.compare_digest(_selection_digest(items), payload['digest']):
            raise ValueError('휴지통 내용이 변경되었습니다. 미리보기를 다시 확인해주세요.')
        for item in items:
            token = item['id']
            fd = None
            try:
                fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                # Recheck the item before mutating; external changes fail closed.
                if _trash_record_fd(fd, token, datetime.now(timezone.utc))['_snapshot'] != item['_snapshot']:
                    raise ValueError('휴지통 내용이 변경되었습니다.')
                child = file_store._open_child_fd(fd, 'item')
                try:
                    _, snapshot = _tree_snapshot(child)
                    if _snapshot_digest(fd, _metadata(fd), snapshot) != item['_snapshot']:
                        raise ValueError('휴지통 내용이 변경되었습니다.')
                    if stat.S_ISDIR(os.fstat(child).st_mode):
                        file_store._preflight_directory_fd(child)
                        file_store._delete_directory_fd(child)
                        os.rmdir('item', dir_fd=fd)
                    else:
                        os.unlink('item', dir_fd=fd)
                finally:
                    os.close(child)
                deleted.append(token)
                # Remove only our journal. Unknown siblings are never deleted.
                os.unlink('metadata.json', dir_fd=fd)
                os.rmdir(token, dir_fd=root)
            except (OSError, ValueError):
                return {'ok': False, 'deleted': deleted, 'failed': [i['id'] for i in items if i['id'] not in deleted], 'detail': '일부 삭제 후 중지됐을 수 있습니다. 목록과 미리보기를 다시 확인해주세요.'}
            finally:
                if fd is not None:
                    os.close(fd)
    return {'ok': True, 'deleted': deleted, 'failed': []}


def restore(token, new_name=''):
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('휴지통 항목을 확인해주세요.')
    with _trash_fd() as root:
        fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            metadata = _metadata(fd)
            parts = _parts(metadata['path'])
            name = _name(new_name or parts[-1])
            _check_source(fd, 'item', name)
            destination = '/'.join(parts[:-1])
            with file_store._open_storage_item(destination) as target_fd:
                _exclusive_move(fd, 'item', target_fd, name)
            # Retain the small journal after restore; cleanup failure must never
            # imply that the data move failed or allow a second restore.
        finally:
            os.close(fd)
    return '/'.join(parts[:-1] + [name])
