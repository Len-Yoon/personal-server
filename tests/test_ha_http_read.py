"""App GET facade protocol/cancellation without provider calls or raw fallback."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress import http_read
from ha_egress import client


def result(body=b'{"items":[]}',status=200):
    return {'status':status,'headers':{'Content-Type':'application/json'},'body_b64':base64.b64encode(body).decode()}


class HttpReadTests(unittest.TestCase):
    def test_exact_operation_payload_and_response_facade(self):
        with patch.object(client,'route',return_value=result()) as route:
            response=http_read.get('book-memo','https://openlibrary.org/search.json',params={'q':'한글','limit':12},headers={'Accept':'application/json'})
        self.assertEqual(route.call_args.args[0:2],('book-memo','book.http.get'))
        self.assertEqual(set(route.call_args.args[2]),{'url','headers'})
        self.assertNotIn('한글',route.call_args.args[2]['url'])
        self.assertEqual(response.json(),{'items':[]})
        self.assertEqual(b''.join(response.iter_content(2)),response.content)
        with response:self.assertEqual(json.loads(response.read()),{'items':[]})
        self.assertEqual(response.status_code,200)

    def test_no_mode_does_not_import_or_load_runtime_configuration(self):
        with patch.dict('os.environ',{},clear=True),patch.object(client,'configuration') as configuration:
            self.assertFalse(http_read.enabled('book-memo'))
            configuration.assert_not_called()

    def test_signed_route_error_or_none_cannot_return_a_raw_fallback_signal(self):
        for value in (None,{},dict(result(),token='private'),result(status=True),dict(result(),body_b64='%%%')):
            with patch.object(client,'route',return_value=value):
                with self.assertRaisesRegex(OSError,'^public_read_unavailable$'):http_read.get('book-memo','https://openlibrary.org/search.json')
        with patch.object(client,'route',side_effect=OSError('private body')):
            with self.assertRaisesRegex(OSError,'^public_read_unavailable$'):http_read.get('book-memo','https://openlibrary.org/search.json')

    def test_body_boundary_is_two_mebibytes_not_json_envelope_size(self):
        with patch.object(client,'route',return_value=result(b'x'*http_read.MAX_BODY)):
            self.assertEqual(len(http_read.get('crawler-worker','https://news.google.com/rss/search').content),http_read.MAX_BODY)
        with patch.object(client,'route',return_value=result(b'x'*(http_read.MAX_BODY+1))):
            with self.assertRaises(http_read.ReadUnavailable):http_read.get('crawler-worker','https://news.google.com/rss/search')

    def test_credentials_cookies_arbitrary_headers_and_unknown_service_never_dispatch(self):
        with patch.object(client,'route') as route:
            for headers in ({'Authorization':'private'},{'Cookie':'private'},{'X-Api-Key':'private'},{'Accept':'x\r\ny'}):
                with self.assertRaises(http_read.ReadUnavailable):http_read.get('book-memo','https://openlibrary.org/search.json',headers=headers)
            with self.assertRaises(http_read.ReadUnavailable):http_read.get('portal-web','https://openlibrary.org/search.json')
            route.assert_not_called()

    def test_cancelled_async_read_does_not_join_default_executor_or_retry(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def slow(*args,**kwargs):
            entered.set();release.wait(1);finished.set();return http_read.Response(200,{},b'{}')
        try:
            with patch.object(http_read,'get',side_effect=slow) as get:
                started=time.monotonic()
                with self.assertRaises(http_read.ReadUnavailable):asyncio.run(http_read.async_get('book-memo','fixture',timeout=.03))
                self.assertLess(time.monotonic()-started,.3)
                self.assertTrue(entered.is_set());self.assertFalse(finished.is_set())
                get.assert_called_once()
        finally:
            release.set();self.assertTrue(finished.wait(1))

    def test_async_concurrency_is_bounded_and_success_projects_response(self):
        with patch.object(http_read,'get',return_value=http_read.Response(200,{},b'{}')):
            self.assertEqual(asyncio.run(http_read.async_get('book-memo','fixture')).json(),{})
        for _ in range(3):http_read._ASYNC_READS.acquire()
        try:
            with self.assertRaises(http_read.ReadUnavailable):asyncio.run(http_read.async_get('book-memo','fixture'))
        finally:
            for _ in range(3):http_read._ASYNC_READS.release()


if __name__=='__main__':unittest.main()
