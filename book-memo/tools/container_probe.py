"""Fixed in-container probes. No user SQL, URL, path or Python input."""

PROBE_SCRIPT = r'''
import hashlib,json,os,sqlite3,sys
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,HTTPRedirectHandler,Request
from urllib.error import HTTPError,URLError

OPS={'seed','snapshot','write_v2','write_v1','health','ready','missing','read','identity'}
TABLES=('books','book_chapters','book_memos','memo_tags','memo_revisions','deleted_memos','write_requests','sqlite_sequence')
TITLE='Container fixture book'
STATES={1:('Original memo','Original fixture'),2:('V2 memo','Written by v2'),3:('V1 memo after rollback','Updated by v1 after rollback')}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def database(service):
    path=service.DB_PATH
    if path!=Path('/app/data/fixture.sqlite3') or path.is_symlink():
        raise ValueError('database_boundary')
    total=0
    for suffix in ('','-wal','-shm','-journal'):
        file=Path(str(path)+suffix)
        if file.is_symlink():
            raise ValueError('database_link')
        if file.exists():
            info=file.stat()
            if not file.is_file() or info.st_nlink!=1:
                raise ValueError('database_type')
            total+=info.st_size
    if total>4*1024*1024:
        raise ValueError('database_size')

def state(service):
    database(service)
    with service._connect() as db:
        tables={name:[list(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in TABLES}
        schema=[list(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        books=[{key:row[key] for key in row.keys() if key!='updated_at'} for row in db.execute('SELECT * FROM books ORDER BY id')]
        memo=db.execute('SELECT title,content,version FROM book_memos WHERE id=1').fetchone()
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        revisions=[json.loads(row['snapshot']) for row in db.execute('SELECT snapshot FROM memo_revisions ORDER BY id')]
    version=memo['version'] if memo else None
    expected=STATES.get(version)
    known=bool(memo and expected and (memo['title'],memo['content'])==expected)
    revision_valid=all((row['title'],row['content'],row['version'])==(*STATES[index+1],index+1)
                       for index,row in enumerate(revisions)) if len(revisions)<=2 else False
    return {'snapshot_hash':digest(tables),'schema_hash':digest(schema),'book_hash':digest(books),
            'supporting_hash':digest({name:tables[name] for name in ('book_chapters','memo_tags','deleted_memos')}),
            'integrity_ok':integrity,'book_count':len(tables['books']),'memo_count':len(tables['book_memos']),
            'memo_version':version,'content_valid':known,'revision_count':len(tables['memo_revisions']),
            'request_count':len(tables['write_requests']),'revisions_valid':revision_valid}

def http(path):
    opener=build_opener(ProxyHandler({}),NoRedirect())
    try:
        response=opener.open(Request('http://127.0.0.1:8003'+path,headers={'Accept':'application/json'}),timeout=2)
    except HTTPError as error:
        response=error
    except URLError:
        return None,b''
    with response:
        body=response.read(512*1024+1)
        if len(body)>512*1024:
            raise ValueError('http_limit')
        return response.code,body

def execute(op):
    if op not in OPS or os.environ.get('BOOK_MEMO_DB_PATH')!='/app/data/fixture.sqlite3':
        raise ValueError('operation_boundary')
    if op in {'health','ready','missing'}:
        paths={'health':'/health','ready':'/ready','missing':'/__container_lab_missing_readiness'}
        code,body=http(paths[op])
        valid=False
        if code==200 and op!='missing':
            valid=json.loads(body)=={'service':'book-memo','status':'ok' if op=='health' else 'ready'}
        return {'http_status':code,'valid':valid}
    if op=='identity':
        code,body=http('/openapi.json')
        paths=json.loads(body).get('paths',{}) if code==200 else {}
        return {'http_status':code,'main_sha':hashlib.sha256(Path('/app/app/main.py').read_bytes()).hexdigest(),
                'service_sha':hashlib.sha256(Path('/app/app/services/book_service.py').read_bytes()).hexdigest(),
                'trash_purge_route':'/books/{book_id}/trash/purge' in paths}
    from app.services import book_service as service
    database(service)
    if op=='seed':
        with service._connect() as db:
            if db.execute('SELECT count(*) FROM books').fetchone()[0]:
                raise ValueError('seed_exists')
        book=service.create_or_get_book({'isbn':'container-fixed-book','title':TITLE})
        service.create_memo(book['id'],None,*STATES[1],7,tags='fixture')
    elif op in {'write_v2','write_v1'}:
        before=state(service)
        expected=1 if op=='write_v2' else 2
        if before['memo_version']!=expected or not before['content_valid']:
            raise ValueError('unexpected_before_write')
        request_id='20000000-0000-4000-8000-000000000002' if op=='write_v2' else '30000000-0000-4000-8000-000000000003'
        for _ in range(2):
            service.update_memo(1,*STATES[expected+1],7,None,expected_version=expected,request_id=request_id)
        after=state(service)
        after['replay_verified']=(after['memo_version']==expected+1 and after['revision_count']==expected
                                  and after['request_count']==expected and after['revisions_valid'])
        return after
    if op=='read':
        current=state(service)
        title,content=STATES.get(current['memo_version'],('unknown','unknown'))
        code,body=http('/books/1')
        return {'http_status':code,'valid':code==200 and title.encode() in body and content.encode() in body}
    return state(service)

try:
    result=execute(sys.argv[1] if len(sys.argv)==2 else '')
    print(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False))
except Exception:
    print(json.dumps({'probe_error':'fixed_probe_failed'}))
    sys.exit(1)
'''

# Reuse the fixed database/HTTP/state definitions without its CLI dispatcher.
# Only synthetic, bounded table rows cross the container boundary; never SQL.
RESTORE_SCRIPT = PROBE_SCRIPT.rsplit('\ntry:\n', 1)[0] + r'''
import base64

def transfer(op):
    if os.getuid()!=10001 or os.getgid()!=10001 or os.environ.get('BOOK_MEMO_DB_PATH')!='/app/data/fixture.sqlite3':
        raise ValueError('restore_identity')
    from app.services import book_service as service
    database(service)
    if op=='export' and len(sys.argv)==2:
        observed=state(service)
        if not observed['content_valid'] or observed['memo_version']!=1 or observed['book_count']!=1 or observed['memo_count']!=1:
            raise ValueError('synthetic_snapshot_required')
        with service._connect() as db:
            tables={name:[list(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in TABLES}
            schema=[list(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        payload={'schema_version':1,'tables':tables,'schema':schema,'state':observed}
        if len(json.dumps(payload,sort_keys=True,separators=(',',':')).encode())>65536:
            raise ValueError('snapshot_size')
        return payload
    if op!='restore' or len(sys.argv)!=3 or len(sys.argv[2])>87384:
        raise ValueError('restore_operation')
    raw=base64.b64decode(sys.argv[2],validate=True)
    if not 1<=len(raw)<=65536:
        raise ValueError('snapshot_size')
    payload=json.loads(raw)
    if (not isinstance(payload,dict) or set(payload)!={'schema_version','tables','schema','state'}
            or type(payload['schema_version']) is not int or payload['schema_version']!=1
            or not isinstance(payload['tables'],dict) or set(payload['tables'])!=set(TABLES)
            or digest(payload['tables'])!=payload['state'].get('snapshot_hash')
            or digest(payload['schema'])!=payload['state'].get('schema_hash')):
        raise ValueError('snapshot_structure')
    with service._connect() as db:
        schema=[list(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        if schema!=payload['schema'] or any(db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in TABLES):
            raise ValueError('destination_not_empty_or_schema_changed')
        columns={name:len(db.execute('PRAGMA table_info('+name+')').fetchall()) for name in TABLES}
        for name,rows in payload['tables'].items():
            if not isinstance(rows,list) or len(rows)>50:
                raise ValueError('snapshot_rows')
            for row in rows:
                if not isinstance(row,list) or len(row)!=columns[name] or any(
                        not (cell is None or type(cell) is int or isinstance(cell,str) and len(cell)<=4096) for cell in row):
                    raise ValueError('snapshot_cell')
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            for name in TABLES:
                if name=='sqlite_sequence':db.execute('DELETE FROM sqlite_sequence')
                rows=payload['tables'][name]
                if rows:db.executemany('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in range(columns[name]))+')',rows)
            actual={name:[list(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in TABLES}
            if (actual!=payload['tables'] or db.execute('PRAGMA integrity_check').fetchone()[0]!='ok'
                    or db.execute('PRAGMA foreign_key_check').fetchall()):
                raise ValueError('restored_snapshot_invalid')
            db.commit()
        except Exception:
            db.rollback()
            raise
    return state(service)

try:
    result=transfer(sys.argv[1] if len(sys.argv)>1 else '')
    print(json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False))
except Exception:
    print(json.dumps({'probe_error':'fixed_restore_probe_failed'}))
    sys.exit(1)
'''
