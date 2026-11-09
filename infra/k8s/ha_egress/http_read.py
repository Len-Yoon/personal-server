"""Bounded response facade for signed public GETs; never a raw HTTP fallback."""
import asyncio
import base64
import io
import json
import math
import os
import threading
from urllib.parse import urlencode

MAX_BODY = 2 * 1024 * 1024
OPERATIONS = {'book-memo':'book.http.get', 'youtube-memo':'youtube.http.get',
              'crawler-worker':'crawler.http.get'}
_ASYNC_READS = threading.BoundedSemaphore(3)


class ReadUnavailable(OSError):
    def __init__(self):
        super().__init__('public_read_unavailable')


def enabled(service):
    if service not in OPERATIONS:
        raise ReadUnavailable()
    if 'HA_PG_CONFIG' not in os.environ:
        return False
    from .client import configuration
    return configuration(service) is not None


class Response:
    def __init__(self, status, headers, content):
        self.status = self.status_code = status
        self.headers = dict(headers)
        self.content = content
        self._stream = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stream.close()

    def read(self, size=-1):
        return self._stream.read(size)

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=16384):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ReadUnavailable()
        for offset in range(0,len(self.content),chunk_size):
            yield self.content[offset:offset+chunk_size]


def get(service, url, *, params=None, headers=None):
    """A caller selects the broker first; None/error can never select raw HTTP."""
    try:
        if service not in OPERATIONS or type(url) is not str:
            raise ReadUnavailable()
        if params is not None:
            if (type(params) is not dict or any(type(k) is not str or type(v) not in {str,int}
                                               for k,v in params.items())):
                raise ReadUnavailable()
            url += ('&' if '?' in url else '?') + urlencode(params)
        headers = {} if headers is None else headers
        if (type(headers) is not dict or len(headers) > 2
                or any(type(k) is not str or k.lower() not in {'accept','user-agent'}
                       or type(v) is not str or len(v) > 512 or any(ord(c)<32 or ord(c)==127 for c in v)
                       for k,v in headers.items()) or len({k.lower() for k in headers}) != len(headers)):
            raise ReadUnavailable()
        from .client import route
        result = route(service, OPERATIONS[service], {'url':url,'headers':headers})
        if (type(result) is not dict or set(result) != {'status','headers','body_b64'}
                or type(result['status']) is not int or not 100 <= result['status'] <= 599
                or type(result['headers']) is not dict or set(result['headers']) != {'Content-Type'}
                or type(result['headers']['Content-Type']) is not str or len(result['headers']['Content-Type']) > 512
                or any(ord(c)<32 or ord(c)==127 for c in result['headers']['Content-Type'])
                or type(result['body_b64']) is not str or len(result['body_b64']) > ((MAX_BODY+2)//3)*4):
            raise ReadUnavailable()
        content = base64.b64decode(result['body_b64'],validate=True)
        if len(content) > MAX_BODY:
            raise ReadUnavailable()
        return Response(result['status'],result['headers'],content)
    except Exception:
        raise ReadUnavailable() from None


async def async_get(service, url, *, params=None, headers=None, timeout=1):
    """No default executor shutdown wait; at most three bounded broker reads.

    Cancellation cannot recall a remote read. The broker/transport retain their
    own finite deadline; this future does not grant authority or retry a read.
    """
    if (type(timeout) not in {int,float} or not math.isfinite(timeout) or not 0 < timeout <= 10
            or not _ASYNC_READS.acquire(blocking=False)):
        raise ReadUnavailable()
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def finish(result, error):
        if not future.done():
            if error:
                future.set_exception(ReadUnavailable())
            else:
                future.set_result(result)

    def invoke():
        result = None
        error = False
        try:
            result = get(service,url,params=params,headers=headers)
        except Exception:
            error = True
        finally:
            _ASYNC_READS.release()
            try:
                loop.call_soon_threadsafe(finish,result,error)
            except RuntimeError:
                pass  # The cancelled caller's event loop has already closed.
    try:
        threading.Thread(target=invoke,daemon=True).start()
    except Exception:
        _ASYNC_READS.release()
        raise ReadUnavailable() from None
    try:
        return await asyncio.wait_for(future,timeout)
    except TimeoutError:
        raise ReadUnavailable() from None
