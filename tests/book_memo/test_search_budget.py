import asyncio
import importlib
import hashlib
import os
import socket
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from tests._test_support import prepare_service_import


async def serve_slow_body(reader, writer, disconnected, handlers):
    task = asyncio.current_task()
    handlers.add(task)
    try:
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"items":[')
        await writer.drain()
        await reader.read()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        # A deadline can close an accepted connection before HTTP headers arrive.
        # EOF/reset still proves that the client released this TCP connection.
        disconnected.append(True)
    else:
        disconnected.append(True)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), .5)
        except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError):
            pass
        finally:
            handlers.discard(task)


class BookSearchBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        prepare_service_import('book-memo')
        from app.services import book_search
        self.search = importlib.reload(book_search)
        self.search._CACHE.clear()

    async def test_provider_preference_fallback_and_cache_are_bounded_and_copy_safe(self):
        search = self.search
        yes24 = AsyncMock(return_value=[{'title':'YES24', 'source':'yes24'}])
        google = AsyncMock(return_value=[{'title':'구글', 'source':'google_books'}])
        library = AsyncMock(return_value=[])
        with patch.dict(os.environ, {'YES24_API_KEY':'test'}), patch.object(search, '_search_yes24', yes24), patch.object(search, '_search_google_books', google), patch.object(search, '_search_open_library', library), patch.object(search, 'CACHE_MAX_ENTRIES', 2):
            first = await search.search_books_async(' 책 ')
            first[0]['title'] = '호출자 변경'
            self.assertEqual((await search.search_books_async('책'))[0]['title'], 'YES24')
            self.assertEqual(yes24.await_count, 1)
            google.assert_not_awaited()
            await search.search_books_async('두번째')
            await search.search_books_async('세번째')
            self.assertEqual(len(search._CACHE), 2)
            self.assertNotIn(('책',12,hashlib.sha256(b'test').hexdigest()), search._CACHE)
            key = ('세번째', 12, hashlib.sha256(b'test').hexdigest())
            search._CACHE[key] = (time.monotonic() - 1, search._CACHE[key][1])
            await search.search_books_async('세번째')
            self.assertEqual(yes24.await_count, 4)
            search._CACHE.clear()
            yes24.side_effect = aiohttp.ClientConnectionError('unavailable')
            self.assertEqual((await search.search_books_async('fallback'))[0]['source'], 'google_books')
            library.assert_not_awaited()
            google.assert_awaited_once()

    async def test_successful_empty_result_remains_distinct_from_all_failed_searches(self):
        search = self.search
        with patch.dict(os.environ, {'YES24_API_KEY':''}), patch.object(search, '_search_google_books', AsyncMock(return_value=[])), patch.object(search, '_search_open_library', AsyncMock(side_effect=aiohttp.ClientConnectionError())):
            self.assertEqual(await search.search_books_async('empty'), [])
        search._CACHE.clear()
        with patch.dict(os.environ, {'YES24_API_KEY':''}), patch.object(search, '_search_google_books', AsyncMock(side_effect=aiohttp.ClientConnectionError())), patch.object(search, '_search_open_library', AsyncMock(side_effect=aiohttp.ClientConnectionError())):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, '이용할 수 없습니다'):
                    await search.search_books_async('failed')
        self.assertNotIn(('failed',12,hashlib.sha256(b'').hexdigest()), search._CACHE)

    async def test_invalid_fallback_cannot_replace_a_successful_empty_result(self):
        search = self.search
        with patch.dict(os.environ, {'YES24_API_KEY':''}), patch.object(search, '_search_google_books', AsyncMock(return_value=[])), patch.object(search, '_search_open_library', AsyncMock(return_value={'invalid':'response'})):
            self.assertEqual(await search.search_books_async('malformed-fallback'), [])
            self.assertEqual(await search.search_books_async('malformed-fallback'), [])

    async def test_real_http_slow_body_is_cancelled_within_whole_budget_and_connections_close(self):
        search = self.search
        disconnected = []
        handlers = set()
        async def slow_body(reader, writer):
            await serve_slow_body(reader, writer, disconnected, handlers)
        server = await asyncio.start_server(slow_body, '127.0.0.1', 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/search"
        try:
            before = set(asyncio.all_tasks())
            with patch.dict(os.environ, {'YES24_API_KEY':''}), patch.object(search, 'GOOGLE_BOOKS_URL', url), patch.object(search, 'OPEN_LIBRARY_URL', url), patch.object(search, 'SEARCH_BUDGET_SECONDS', .12), patch.object(search, 'PROVIDER_BUDGET_SECONDS', .05):
                started = time.monotonic()
                with self.assertRaises(ValueError):
                    await search.search_books_async('slow-body')
                self.assertLess(time.monotonic()-started, .5)
            for _ in range(20):
                if len(disconnected)==2:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(disconnected), 2)
            self.assertFalse(set(asyncio.all_tasks()) - before)
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(handlers):
                task.cancel()
            await asyncio.gather(*tuple(handlers), return_exceptions=True)

    async def test_slow_body_fixture_counts_client_eof_before_request_headers(self):
        disconnected, errors = [], []
        handlers = set()
        accepted, finished = asyncio.Event(), asyncio.Event()
        async def handler(reader, writer):
            accepted.set()
            try:
                await serve_slow_body(reader, writer, disconnected, handlers)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        client = None
        try:
            before = set(asyncio.all_tasks())
            _, client = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            await asyncio.wait_for(accepted.wait(), .5)
            # Close an accepted TCP connection without sending HTTP headers.
            # CI can reach this exact state when the provider deadline expires.
            client.close()
            await asyncio.wait_for(client.wait_closed(), .5)
            await asyncio.wait_for(finished.wait(), .5)
            self.assertEqual(len(disconnected), 1)
            self.assertEqual(errors, [])
            self.assertFalse(handlers)
            self.assertFalse(set(asyncio.all_tasks()) - before)
        finally:
            if client is not None:
                client.close()
                await asyncio.wait_for(client.wait_closed(), .5)
            server.close()
            await server.wait_closed()
            remaining = tuple(handlers)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)

    async def test_dns_deadline_cancels_resolver_work_without_getaddrinfo_threads(self):
        search = self.search
        class SlowResolver:
            def __init__(self):
                self.cancelled = 0
                self.closed = False
            async def resolve(self, host, port=0, family=socket.AF_INET):
                try:
                    await asyncio.sleep(5)
                finally:
                    self.cancelled += 1
            async def close(self):
                self.closed = True
        resolver = SlowResolver()
        before = set(asyncio.all_tasks())
        with patch.dict(os.environ, {'YES24_API_KEY':''}), patch.object(search.aiohttp, 'AsyncResolver', return_value=resolver), patch.object(search, 'SEARCH_BUDGET_SECONDS', .08), patch.object(search, 'PROVIDER_BUDGET_SECONDS', .04), patch.object(socket, 'getaddrinfo', side_effect=AssertionError('threaded DNS is forbidden')):
            started = time.monotonic()
            with self.assertRaises(ValueError):
                await search.search_books_async('slow-dns')
            self.assertLess(time.monotonic()-started, .4)
        self.assertTrue(resolver.closed)
        self.assertGreater(resolver.cancelled, 0)
        self.assertFalse(set(asyncio.all_tasks()) - before)

    async def test_whole_budget_applies_across_providers_and_drains_cancelled_tasks(self):
        search = self.search
        cancelled = []
        async def slow(*_):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.append(True)
        with patch.dict(os.environ, {'YES24_API_KEY':'test'}), patch.object(search, '_search_yes24', slow), patch.object(search, '_search_google_books', slow), patch.object(search, '_search_open_library', slow), patch.object(search, 'SEARCH_BUDGET_SECONDS', .11), patch.object(search, 'PROVIDER_BUDGET_SECONDS', .06):
            started = time.monotonic()
            with self.assertRaises(ValueError):
                await search.search_books_async('all-slow')
            self.assertLess(time.monotonic()-started, .35)
        self.assertEqual(len(cancelled), 2)


class Yes24ContractTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the replacement provider contract without live credentials."""

    def setUp(self):
        prepare_service_import('book-memo')
        from app.services import book_search
        self.search = importlib.reload(book_search)

    async def test_yes24_mapping_uses_isbn_and_canonical_link(self):
        payload = {'success': True, 'errorCode': None, 'data': {'items': [{
            'itemId': 123, 'title': '손자병법', 'author': '손무', 'isbn13': '9781234567890',
            'isbn10': '1234567890', 'publisher': '출판사', 'publishDate': '2026-10-29',
            'cover': 'https://image.yes24.com/goods/123/L', 'link': 'javascript:bad',
            'contentDetail': {'bookIntroduction': '책 소개 &amp; 설명'},
        }]}}
        get_json = AsyncMock(return_value=payload)
        with patch.dict(os.environ, {'YES24_API_KEY': 'fixture-key'}), patch.object(self.search, '_get_json', get_json):
            books = await self.search._search_yes24('손자병법', 12)
        self.assertEqual(books[0]['isbn'], '9781234567890')
        self.assertEqual(books[0]['source'], 'yes24')
        self.assertEqual(books[0]['preview_url'], 'https://www.yes24.com/product/goods/123')
        self.assertEqual(books[0]['description'], '책 소개 & 설명')
        self.assertEqual(get_json.call_args.kwargs['headers'], {'X-Api-Key': 'fixture-key'})
        self.assertEqual(get_json.call_args.kwargs['params']['category'], 'BOOK')

    async def test_primary_error_and_empty_fallback_are_not_silenced_or_cached(self):
        search = self.search
        with patch.dict(os.environ, {'YES24_API_KEY': 'fixture-key'}), patch.object(search, '_get_json', AsyncMock(return_value={'success': False, 'errorCode': 'AUTH_002', 'message': 'fixture-key'})), patch.object(search, '_search_google_books', AsyncMock(return_value=[])), patch.object(search, '_search_open_library', AsyncMock(return_value=[])):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'YES24') as error:
                    await search.search_books_async('손자병법')
                self.assertNotIn('fixture-key', str(error.exception))
            self.assertFalse(search._CACHE)

    async def test_rotated_key_does_not_reuse_old_cache(self):
        search = self.search
        provider = AsyncMock(return_value=[{'title': '책'}])
        with patch.object(search, '_search_yes24', provider):
            for key in ('old-fixture', 'new-fixture'):
                with patch.dict(os.environ, {'YES24_API_KEY': key}):
                    await search.search_books_async('책')
        self.assertEqual(provider.await_count, 2)

    async def test_real_http_404_codes_and_redirect_do_not_leak_auth(self):
        import json
        from aiohttp import web
        seen = []
        async def handler(request):
            seen.append((request.path, request.headers.get('X-Api-Key'), dict(request.query)))
            code = request.query['query']
            if code == 'redirect':
                raise web.HTTPFound('/leak')
            return web.json_response({'success': False, 'errorCode': code}, status=404)
        app = web.Application()
        app.router.add_get('/search', handler)
        async def leak(request):
            self.fail('credential request followed redirect')
        app.router.add_get('/leak', leak)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/search'
        try:
            async with aiohttp.ClientSession() as client:
                token = self.search._CLIENT.set(client)
                try:
                    with patch.object(self.search, 'YES24_SEARCH_URL', url), patch.dict(os.environ, {'YES24_API_KEY': 'fixture-key'}):
                        self.assertEqual(await self.search._search_yes24('SEARCH_001', 12), [])
                        for query in ('SEARCH_002', 'AUTH_002', 'redirect'):
                            with self.assertRaises(ValueError):
                                await self.search._search_yes24(query, 12)
                finally:
                    self.search._CLIENT.reset(token)
        finally:
            await runner.cleanup()
        self.assertTrue(all(key == 'fixture-key' for _, key, _ in seen))
        self.assertTrue(all('ttbkey' not in query and 'key' not in query for _, _, query in seen))
