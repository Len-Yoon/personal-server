"""Run a verified Book artifact with a private database and inherited socket."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from release_artifact import verify_release

TABLES=('books','book_chapters','book_memos','memo_tags','memo_revisions',
        'deleted_memos','write_requests','sqlite_sequence')
TITLE='Release fixture book'
MEMO='Release fixture memo'
CONTENT='Release fixture content'


def _database(path,root):
    if path.parent!=root or path.name!='fixture.sqlite3':
        raise ValueError('database_boundary')
    total=0
    for suffix in ('','-wal','-shm','-journal'):
        candidate=Path(str(path)+suffix)
        if candidate.is_symlink():
            raise ValueError('database_link')
        if candidate.exists():
            info=candidate.stat()
            if not candidate.is_file() or info.st_nlink!=1:
                raise ValueError('database_type')
            total+=info.st_size
    if total>4*1024*1024:
        raise ValueError('database_size')


def _snapshot(service):
    with service._connect() as connection:
        snapshot={name:[list(row) for row in connection.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                  for name in TABLES}
        integrity=connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        book=connection.execute('SELECT title FROM books WHERE id=1').fetchone()
        memo=connection.execute('SELECT title,content,version FROM book_memos WHERE id=1').fetchone()
    return {'snapshot_hash':hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'integrity_ok':integrity,'fixture_valid':bool(book and book[0]==TITLE and memo and
                tuple(memo)==(MEMO,CONTENT,1)),
            'book_count':len(snapshot['books']),'memo_count':len(snapshot['book_memos'])}


def release_actor(connection,release_path,expected,db_path,sock):
    async def run():
        release=Path(release_path)
        root=release.parent
        db=Path(db_path)
        descriptor=verify_release(release,expected)
        _database(db,root)
        owner=(root/'.release-lab-owner').read_text()
        os.environ.clear()
        os.environ.update({'PATH':os.defpath,'BOOK_MEMO_DB_PATH':str(db),'DEMO_MODE':'0',
                           'NO_PROXY':'127.0.0.1,localhost'})
        sys.dont_write_bytecode=True
        os.chdir(release)
        # A spawn may inherit a service-specific PYTHONPATH. Keep only the
        # interpreter/venv library roots, then put the verified artifact first.
        prefixes={str(Path(sys.prefix).resolve()),str(Path(sys.base_prefix).resolve())}
        sys.path[:]=[str(release)]+[entry for entry in sys.path if entry and
                    any(Path(entry).resolve().is_relative_to(Path(prefix)) for prefix in prefixes)]
        for name in list(sys.modules):
            if name=='app' or name.startswith('app.'):
                del sys.modules[name]
        from app.main import app
        from app.services import book_service
        import uvicorn
        if not Path(book_service.__file__).resolve().is_relative_to(release):
            raise ValueError('unexpected_product_source')
        if book_service.DB_PATH!=db:
            raise ValueError('unexpected_database')
        server=uvicorn.Server(uvicorn.Config(app,log_config=None,log_level='critical',access_log=False,
                                           lifespan='on',proxy_headers=False,workers=1))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise ValueError('startup_failed')
                await asyncio.sleep(.01)
            connection.send({'ready':True,'release_digest':descriptor['release_digest'],
                             'pid':os.getpid(),'owner':owner})
            while not task.done():
                if not connection.poll():
                    await asyncio.sleep(.01)
                    continue
                request=connection.recv();op=request.get('op')
                _database(db,root)
                if op=='stop':
                    server.should_exit=True
                    await task
                    connection.send({'ok':True,'data':{}})
                    return
                if op=='seed':
                    with book_service._connect() as db_connection:
                        if db_connection.execute('SELECT count(*) FROM books').fetchone()[0]:
                            raise ValueError('seed_exists')
                    book=book_service.create_or_get_book({'isbn':'release-fixed-book','title':TITLE})
                    book_service.create_memo(book['id'],None,MEMO,CONTENT,7,tags='fixture')
                elif op!='snapshot':
                    raise ValueError('invalid_command')
                data=_snapshot(book_service)
                _database(db,root)
                connection.send({'ok':True,'data':data})
            await task
            raise ValueError('server_exited')
        finally:
            server.should_exit=True
            if not task.done():
                try:
                    await asyncio.wait_for(task,5)
                except asyncio.TimeoutError:
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
    try:
        asyncio.run(run())
    except (Exception,KeyboardInterrupt,SystemExit):
        try:
            connection.send({'ok':False,'reason':'release_actor_failed'})
        except OSError:
            pass
    finally:
        sock.close()
        connection.close()
