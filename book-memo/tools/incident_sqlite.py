"""Spawn-only Book SQLite incident actors; all writes stay in an owned fixture."""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

_REQUEST_ID = '00000000-0000-4000-8000-000000000017'
_CONTENT = 'L' * (64 * 1024)
_TABLES = ('books', 'book_chapters', 'book_memos', 'memo_tags',
           'memo_revisions', 'deleted_memos', 'write_requests', 'sqlite_sequence')
_NAMES = ('fixture.sqlite3', 'fixture.sqlite3-journal', 'fixture.sqlite3-wal', 'fixture.sqlite3-shm')


class _Rejected(Exception):
    """A fixed, public failure reason, never an exception message from SQLite."""


def _fixture(root):
    root = Path(root)
    marker = root / '.incident-owner'
    if (root.is_symlink() or not root.is_dir() or marker.is_symlink()
            or not marker.is_file() or not 0 < marker.stat().st_size <= 128):
        raise _Rejected('unsafe_root')
    total = 0
    for name in _NAMES:
        path = root / name
        if path.is_symlink():
            raise _Rejected('unsafe_fixture')
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Rejected('unsafe_fixture')
            total += info.st_size
    if total > 4 * 1024 * 1024:
        raise _Rejected('fixture_too_large')
    return root / 'fixture.sqlite3'


def _service(path):
    # Spawn targets import the product only AFTER replacing inherited settings.
    os.environ.clear()
    os.environ.update({'PATH': os.defpath, 'BOOK_MEMO_DB_PATH': str(path),
                       'BOOK_MEMO_DEMO_MODE': '0', 'DEMO_MODE': '0', 'NO_PROXY': '*'})
    service_root = Path(__file__).resolve().parents[1]
    # Drop ambient Python paths while retaining interpreter/venv dependency paths.
    sys.path[:] = [str(service_root)] + [entry for entry in sys.path if entry and (
        Path(entry).is_relative_to(sys.base_prefix)
        or Path(entry).is_relative_to(sys.prefix))]
    for name in tuple(sys.modules):
        if name == 'app' or name.startswith('app.'):
            del sys.modules[name]
    return importlib.import_module('app.services.book_service')


def _snapshot(service):
    with service._connect() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in _TABLES}


def _pages(service):
    with service._connect() as db:
        return (db.execute('PRAGMA page_count').fetchone()[0],
                db.execute('PRAGMA page_size').fetchone()[0],
                db.execute('PRAGMA max_page_count').fetchone()[0])


def _send(connection, data):
    connection.send({'ok': True, 'data': data})


def sqlite_actor(connection, root: str):
    """Serve fixed commands over a parent-owned Pipe; never accept paths or SQL."""
    try:
        path = _fixture(root)
        service = _service(path)
        original_connect = service._connect
        cap = None
        baseline = None
        memo_id = None
        failure_pending = False

        @contextmanager
        def owned_connect():
            _fixture(root)
            with original_connect() as db:
                if cap is not None:
                    actual = db.execute(f'PRAGMA max_page_count = {cap}').fetchone()[0]
                    if actual != cap:
                        raise _Rejected('page_cap_mismatch')
                yield db
            _fixture(root)

        service._connect = owned_connect
        connection.send({'ready': True})
        while True:
            command = connection.recv()
            try:
                if not isinstance(command, dict) or set(command) - {'op', 'scenario'}:
                    raise _Rejected('invalid_command')
                op = command.get('op')
                if op == 'stop':
                    _send(connection, {'stopped': True})
                    return
                if op == 'baseline':
                    if baseline is not None or any((Path(root) / name).exists() for name in _NAMES):
                        raise _Rejected('fixture_exists')
                    # Exclusive creation prevents accidental overwrite of an existing DB.
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                    book = service.create_or_get_book({'isbn': 'incident-fixed-book', 'title': 'Incident fixture'})
                    service.create_memo(book['id'], None, 'Original memo', 'Original content', 7, tags='fixture')
                    memo_id = service.list_memos(book['id'])[0]['id']
                    baseline = _snapshot(service)
                    count, size, _ = _pages(service)
                    _send(connection, {'snapshot_hash': hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest(),
                                       'page_count': count, 'page_size': size})
                    continue
                if baseline is None:
                    raise _Rejected('baseline_required')
                if op in {'constrain', 'release'}:
                    count, size, _ = _pages(service)
                    desired = count if op == 'constrain' else count + 64
                    if size <= 0 or size > 65536 or desired * size > 1024 * 1024:
                        raise _Rejected('unsafe_page_limit')
                    cap = desired
                    count, size, actual = _pages(service)
                    _send(connection, {'page_count': count, 'page_size': size, 'max_page_count': actual})
                elif op == 'probe':
                    failure_pending = False
                    scenario = command.get('scenario')
                    if scenario not in {'sqlite_busy', 'sqlite_full'}:
                        raise _Rejected('invalid_scenario')
                    expected = sqlite3.SQLITE_BUSY if scenario == 'sqlite_busy' else sqlite3.SQLITE_FULL
                    try:
                        service.update_memo(memo_id, 'Recovered memo', _CONTENT, 7, None,
                                            expected_version=1, request_id=_REQUEST_ID)
                    except sqlite3.Error as error:
                        code = getattr(error, 'sqlite_errorcode', None)
                        if not isinstance(code, int) or code & 0xFF != expected:
                            raise _Rejected('unexpected_sqlite_code') from None
                        failure_pending = True
                        _send(connection, {'error_code': code & 0xFF})
                    else:
                        raise _Rejected('fault_not_observed')
                elif op == 'evidence':
                    if not failure_pending:
                        raise _Rejected('failure_required')
                    failure_pending = False
                    unchanged = _snapshot(service) == baseline
                    if not unchanged:
                        raise _Rejected('partial_write')
                    _send(connection, {'unchanged': unchanged})
                elif op == 'verify':
                    for _ in range(2):
                        result = service.update_memo(memo_id, 'Recovered memo', _CONTENT, 7, None,
                                                     expected_version=1, request_id=_REQUEST_ID)
                        if result != baseline['books'][0][0]:
                            raise _Rejected('unexpected_write_result')
                    snapshot = _snapshot(service)
                    before_memo = dict(zip(_column_names(service, 'book_memos'), baseline['book_memos'][0]))
                    with service._connect() as db:
                        memo = dict(db.execute('SELECT * FROM book_memos WHERE id = ?', (memo_id,)).fetchone())
                        revisions = list(db.execute('SELECT * FROM memo_revisions'))
                        requests = list(db.execute('SELECT * FROM write_requests'))
                        integrity = db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    preserved = (len(snapshot['books']) == 1 and
                                 all(a == b for key in ('book_chapters', 'memo_tags', 'deleted_memos')
                                     for a, b in [(snapshot[key], baseline[key])]) and
                                 len(revisions) == 1 and json.loads(revisions[0]['snapshot']) == before_memo)
                    # The update intentionally touches the book timestamp; all its other data must survive.
                    columns = _column_names(service, 'books')
                    preserved = preserved and all(a == b for col, a, b in zip(columns, baseline['books'][0], snapshot['books'][0])
                                                  if col != 'updated_at')
                    exactly_once = (len(snapshot['book_memos']) == 1 and memo['version'] == before_memo['version'] + 1
                                    and memo['content'] == _CONTENT and memo['title'] == 'Recovered memo'
                                    and memo['book_id'] == before_memo['book_id'] and memo['page'] == before_memo['page']
                                    and memo['chapter_id'] == before_memo['chapter_id'] and len(revisions) == 1
                                    and len(requests) == 1 and requests[0]['request_id'] == _REQUEST_ID.replace('-', '')
                                    and requests[0]['operation'] == 'memo-edit')
                    if not (preserved and exactly_once and integrity):
                        raise _Rejected('recovery_verification_failed')
                    _send(connection, {'data_preserved': preserved, 'exactly_once': exactly_once,
                                       'integrity_ok': integrity, 'version': memo['version']})
                else:
                    raise _Rejected('invalid_command')
            except _Rejected as error:
                connection.send({'ok': False, 'reason': error.args[0]})
            except Exception:
                connection.send({'ok': False, 'reason': 'actor_operation_failed'})
    except _Rejected as error:
        connection.send({'ok': False, 'reason': error.args[0]})
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        try:
            connection.send({'ok': False, 'reason': 'actor_start_failed'})
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


def _column_names(service, table):
    with service._connect() as db:
        return [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]


def lock_actor(connection, root: str):
    """Hold one real SQLite writer lock until the owning parent releases it."""
    db = None
    try:
        path = _fixture(root)
        if not path.is_file():
            raise _Rejected('fixture_missing')
        _service(path)
        # mode=rw forbids this actor from creating a missing fixture.
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
        db.execute('BEGIN IMMEDIATE')
        connection.send({'ready': True})
        while True:
            command = connection.recv()
            if isinstance(command, dict) and command in ({'op': 'release'}, {'op': 'stop'}):
                db.rollback()
                db.close()
                db = None
                _send(connection, {'released': True})
                return
            connection.send({'ok': False, 'reason': 'invalid_command'})
    except _Rejected as error:
        connection.send({'ok': False, 'reason': error.args[0]})
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        try:
            connection.send({'ok': False, 'reason': 'lock_failed'})
        except (BrokenPipeError, OSError):
            pass
    finally:
        if db is not None:
            db.rollback()
            db.close()
        connection.close()
