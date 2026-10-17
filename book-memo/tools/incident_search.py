"""Stateful, loopback-only dependency fault with the real Portal aggregator."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('news','youtube','books')
PAYLOAD = {'results':[{'title':'incident fixture','url':'#'}]}


def search_actor(connection, sockets, nonce):
    os.environ.clear()
    os.environ.update({'PATH':os.defpath,'DEMO_MODE':'0','NO_PROXY':'127.0.0.1,localhost'})
    for name,sock in zip(SOURCES,sockets):
        os.environ[f'{name.upper()}_SEARCH_URL']=f'http://127.0.0.1:{sock.getsockname()[1]}/{nonce}/{name}'
    spec=importlib.util.spec_from_file_location('incident_actual_search',ROOT/'portal-web/app/services/global_search.py')
    module=importlib.util.module_from_spec(spec)

    async def run():
        mode='normal'
        handlers=set()

        async def handle(reader,writer,name):
            task=asyncio.current_task(); handlers.add(task)
            try:
                header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),3)
                correct=header.startswith(f'GET /{nonce}/{name}?'.encode())
                if correct and name=='books' and mode=='fault':
                    await asyncio.sleep(2)
                body=json.dumps(PAYLOAD if correct else {}).encode()
                writer.write(f'HTTP/1.1 {200 if correct else 404} Lab\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
                await writer.drain()
            except (OSError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(task)

        servers=[]
        try:
            for name,sock in zip(SOURCES,sockets):
                async def callback(reader,writer,name=name):
                    await handle(reader,writer,name)
                servers.append(await asyncio.start_server(callback,sock=sock,limit=8192))
            connection.send({'ready':True})
            while True:
                while not connection.poll():
                    await asyncio.sleep(.01)
                request=connection.recv(); op=request.get('op')
                if op in {'normal','fault'}:
                    mode=op
                    data={'mode':mode}
                elif op=='probe':
                    result=await module.search_all('incident-fixed-query')
                    statuses=[result[name]['status'] for name in SOURCES]
                    expected=['ok','ok','unavailable'] if mode=='fault' else ['ok']*3
                    valid=statuses==expected and all(result[name]['items']==(PAYLOAD['results'] if status=='ok' else [])
                                                     for name,status in zip(SOURCES,statuses))
                    data={'statuses':statuses,'valid':valid}
                elif op=='stop':
                    connection.send({'ok':True,'data':{}})
                    return
                else:
                    connection.send({'ok':False,'reason':'invalid_command'})
                    continue
                connection.send({'ok':True,'data':data})
        finally:
            for server in servers:
                server.close()
            for server in servers:
                await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*list(handlers),return_exceptions=True)

    try:
        spec.loader.exec_module(module)
        asyncio.run(run())
    except (Exception,KeyboardInterrupt):
        try:
            connection.send({'ok':False,'reason':'search_actor_failed'})
        except OSError:
            pass
    finally:
        connection.close()
