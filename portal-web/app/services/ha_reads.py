"""Typed internal reads for the network-isolated Portal; no HTTP fallback."""
import asyncio
import math
import os
import threading

_SLOTS=threading.BoundedSemaphore(3)


def enabled():
    if 'HA_PG_CONFIG' not in os.environ:return False
    from ha_egress.client import configuration
    return configuration('portal-web') is not None


def read(target,path,query=None):
    from ha_egress.client import route
    value=route('portal-web','portal.internal.get',{'target':target,'path':path,'query':{} if query is None else query})
    if type(value) is not dict or set(value)!={'value'} or type(value['value']) not in {dict,list}:
        raise OSError('internal_read_unavailable')
    return value['value']


async def async_read(target,path,query,*,timeout):
    if (type(timeout) not in {int,float} or not math.isfinite(timeout) or not 0<timeout<=2
            or not _SLOTS.acquire(blocking=False)):raise OSError('internal_read_unavailable')
    loop=asyncio.get_running_loop();future=loop.create_future()
    def finish(value,error):
        if not future.done():
            if error:future.set_exception(OSError('internal_read_unavailable'))
            else:future.set_result(value)
    def invoke():
        value=None;error=False
        try:value=read(target,path,query)
        except Exception:error=True
        finally:
            _SLOTS.release()
            try:loop.call_soon_threadsafe(finish,value,error)
            except RuntimeError:pass
    try:threading.Thread(target=invoke,daemon=True).start()
    except Exception:
        _SLOTS.release();raise OSError('internal_read_unavailable') from None
    return await asyncio.wait_for(future,timeout)


class SearchClient:
    async def __aenter__(self):return self
    async def __aexit__(self,*_):return False
    async def get_source(self,name,query,limit):
        return await async_read(name,'/api/search',{'q':query,'limit':limit},timeout=1.5)
