"""Start actual memo apps with disposable SQLite, fixtures and no outbound network."""
import argparse
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', choices=('book-memo', 'youtube-memo'), required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--fd', type=int, required=True)
    args = parser.parse_args()
    scratch = args.root.resolve()
    if (scratch.parent != Path(tempfile.gettempdir()).resolve()
            or not scratch.name.startswith('memo-browser-smoke-')
            or scratch.stat().st_mode & 0o077):
        raise ValueError('private disposable root required')
    inherited = socket.socket(fileno=os.dup(args.fd))
    try:
        if inherited.getsockname()[0] != '127.0.0.1' or inherited.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError('owned listening loopback descriptor required')
    finally:
        inherited.close()
    os.environ.update({
        'APP_ENV': 'test', 'DELETE_PASSWORD': 'memo-smoke-only',
        'BOOK_MEMO_DB_PATH': str(scratch / 'book.sqlite3'),
        'YOUTUBE_MEMO_DB_PATH': str(scratch / 'youtube.sqlite3'),
        'AUTH_RATE_LIMIT_STATE_PATH': str(scratch / 'auth-rate-limit.json'),
    })
    sys.path[:0] = [str(ROOT / args.service), str(ROOT)]
    if args.service == 'book-memo':
        from app.services import book_service as service
        from app.services.export_service import export_records
        parent = service.create_or_get_book({'isbn': 'smoke-seed', 'title': '격리 브라우저 책'})
        service.create_memo(parent['id'], None, '현재 메모', '격리 현재 내용', 1)
        payload = copy.deepcopy(export_records())
        payload['records'][0]['isbn'] = 'smoke-import'
        payload['records'][0]['title'] = '가져오기 시험 책'
        service.create_memo(parent['id'], None, '삭제된 시험 메모', '격리 삭제 내용', 1)
        detail_path = f'/books/{parent["id"]}'
    else:
        from app.services import memo_service as service
        parent = service.create_or_get_video('abcdefghijk', title_fetcher=lambda *_: '격리 브라우저 영상')
        service.create_memo(parent['id'], '현재 메모', '격리 현재 내용')
        from app.services.export_service import export_json
        payload = export_json(service.list_export_records())
        # export_json returns a serialized service-owned export envelope.
        payload = json.loads(payload)
        payload['records'][0]['youtube_id'] = 'lmnopqrstuv'
        payload['records'][0]['url'] = 'https://www.youtube.com/watch?v=lmnopqrstuv'
        payload['records'][0]['title'] = '가져오기 시험 영상'
        service.create_memo(parent['id'], '삭제된 시험 메모', '격리 삭제 내용')
        detail_path = f'/videos/{parent["id"]}'
    deleted = service.list_memos(parent['id'])[0]
    service.delete_memo(deleted['id'], expected_version=1)
    from app.services.import_service import validate_payload
    validate_payload(payload)
    (scratch / 'import-fixture.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    (scratch / 'detail-path.txt').write_text(detail_path, encoding='utf-8')
    # Any accidental metadata/search call fails locally instead of reaching an
    # external service. Listening and accepted sockets remain usable by uvicorn.
    def blocked(*_args, **_kwargs):
        raise OSError('outbound networking prohibited in memo smoke')
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    os.chdir(ROOT / args.service)
    from app.main import app
    import uvicorn
    uvicorn.run(app, fd=args.fd, log_level='error', access_log=False)


if __name__ == '__main__':
    main()
