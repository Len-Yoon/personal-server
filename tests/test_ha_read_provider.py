"""TCB public GET boundary; all DNS, TLS and provider transports are doubles."""
import base64
import io
import json
from pathlib import Path
import socket
import os
import tempfile
import sys
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress import read_provider as provider

GOOGLE='https://www.googleapis.com/books/v1/volumes?q=fixture&maxResults=12&printType=books&langRestrict=ko'
YES24='https://apis.yes24.com/v1/goods/itemList?query=fixture&category=BOOK&page=1&pageSize=12'
NEWS='https://news.google.com/rss/search?q=fixture&hl=ko&gl=KR&ceid=KR%3Ako'
IP='93.184.216.34'


def rows(*addresses):
    return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(address,443)) for address in addresses]


class PublicGetTests(unittest.TestCase):
    def call(self,adapter,url=GOOGLE,headers=None):
        return adapter({'url':url,'headers':{} if headers is None else headers},timeout=2,deadline=12)

    def setUp(self):
        clock=patch.object(provider.time,'monotonic',return_value=10.)
        clock.start();self.addCleanup(clock.stop)

    def test_fixed_service_path_query_and_no_credentials_or_arbitrary_headers(self):
        adapter=provider.PublicGet('book-memo')
        urls=[GOOGLE.replace('https:','http:'),GOOGLE.replace('www.googleapis.com','127.0.0.1'),
              GOOGLE.replace('www.googleapis.com','www.googleapis.com.evil.invalid'),
              GOOGLE.replace('/books/v1/volumes','/delete'),GOOGLE+'&key=private',
              GOOGLE+'&q=duplicate',GOOGLE+'#fragment',GOOGLE.replace('https://','https://user:pass@'),
              GOOGLE.replace('.com/','.com:8443/'),GOOGLE.replace('q=fixture','q=x%0d%0aAuthorization'),
              GOOGLE.replace('q=fixture','token=private'), ' '+GOOGLE]
        with patch.object(provider,'_fetch') as fetch:
            for url in urls:
                with self.subTest(url=url),self.assertRaisesRegex(OSError,'^public_read_provider_unavailable$'):
                    self.call(adapter,url)
            for header in ({'Authorization':'private'},{'Cookie':'private'},{'X-Api-Key':'private'},
                           {'Host':'internal'},{'Accept':'x\r\nCookie:y'},{'Accept':'x','accept':'y'}):
                with self.assertRaises(provider.PublicReadUnavailable):self.call(adapter,headers=header)
            with self.assertRaises(provider.PublicReadUnavailable):adapter({'url':GOOGLE,'headers':{},'body':'x'},timeout=2,deadline=12)
            fetch.assert_not_called()

    def test_service_cannot_use_another_service_host_or_mutating_path(self):
        with patch.object(provider,'_fetch') as fetch:
            for service in ('youtube-memo','crawler-worker'):
                with self.assertRaises(provider.PublicReadUnavailable):self.call(provider.PublicGet(service))
            fetch.assert_not_called()
        for service in ('portal-web','book',[]):
            with self.assertRaises(provider.PublicReadUnavailable):provider.PublicGet(service)

    def test_yes24_key_only_tcb_constructor_and_two_endpoints_no_redirect(self):
        key='fixture-private-api-key'
        adapter=provider.PublicGet('book-memo',yes24_key=key)
        success=json.dumps({'success':True,'errorCode':None,'data':{'items':[]}}).encode()
        with patch.object(provider,'_fetch',return_value=(200,{'Content-Type':'application/json; reflected='+key},success,None)) as fetch:
            ack=self.call(adapter,YES24)
            self.assertEqual(fetch.call_args.args[2]['X-Api-Key'],key)
            self.assertEqual(fetch.call_args.args[2]['accept-encoding'],'identity')
            self.assertNotIn(key,repr(adapter));self.assertNotIn(key,str(ack))
            self.assertEqual(ack.result['headers'],{'Content-Type':'application/json'})
            self.call(adapter)
            self.assertNotIn('X-Api-Key',fetch.call_args.args[2])
        with patch.object(provider,'_fetch',return_value=(302,{},b'',YES24)) as fetch:
            with self.assertRaises(provider.PublicReadUnavailable):self.call(adapter,YES24)
            self.assertEqual(fetch.call_count,1)
        with patch.object(provider,'_fetch') as fetch:
            with self.assertRaises(provider.PublicReadUnavailable):self.call(provider.PublicGet('book-memo'),YES24)
            fetch.assert_not_called()

    def test_yes24_reflection_or_auth_error_never_returns_provider_body(self):
        key='fixture-private-api-key';adapter=provider.PublicGet('book-memo',yes24_key=key)
        for body in (json.dumps({'success':True,'errorCode':None,'data':key}).encode(),
                     b'{"success":false,"errorCode":"AUTH_002","message":"raw private response"}',
                     b'{"success":true,"errorCode":null,"data":"\\u0066ixture-private-api-key"}'):
            with patch.object(provider,'_fetch',return_value=(200,{'Content-Type':'application/json'},body,None)):
                with self.assertRaisesRegex(OSError,'^public_read_provider_unavailable$'):self.call(adapter,YES24)
        missing=b'{"success":false,"errorCode":"SEARCH_001","message":"discard this field"}'
        with patch.object(provider,'_fetch',return_value=(404,{'Content-Type':'application/json'},missing,None)):
            body=base64.b64decode(self.call(adapter,YES24).result['body_b64'])
            self.assertEqual(json.loads(body),{'success':False,'errorCode':'SEARCH_001','data':{}})
            self.assertNotIn(b'message',body)

    def test_redirect_each_hop_revalidated_and_limit_is_finite(self):
        adapter=provider.PublicGet('crawler-worker')
        for target in ('http://127.0.0.1/','https://metadata.google.internal/','https://news.google.com/delete'):
            with patch.object(provider,'_fetch',return_value=(302,{},b'',target)) as fetch:
                with self.assertRaises(provider.PublicReadUnavailable):self.call(adapter,NEWS)
                self.assertEqual(fetch.call_count,1)
        with patch.object(provider,'_fetch',return_value=(302,{},b'',NEWS)) as fetch:
            with self.assertRaises(provider.PublicReadUnavailable):self.call(adapter,NEWS)
            self.assertEqual(fetch.call_count,3)
        with patch.object(provider,'_fetch',side_effect=[(302,{},b'',NEWS),(200,{'Content-Type':'text/xml'},b'<rss/>',None)]) as fetch:
            ack=self.call(adapter,NEWS)
            self.assertEqual(base64.b64decode(ack.result['body_b64']),b'<rss/>')
            self.assertEqual(fetch.call_count,2)

    def test_dns_rejects_every_nonpublic_or_mixed_result_without_dial(self):
        bad=('127.0.0.1','10.0.0.1','169.254.169.254','100.64.0.1','224.0.0.1','0.0.0.0','192.0.2.1')
        for address in bad:
            with patch.object(provider.socket,'getaddrinfo',return_value=rows(IP,address)):
                with self.assertRaises(provider.PublicReadUnavailable):provider._resolve('www.googleapis.com')
        for address in ('::1','fe80::1','fc00::1','2001:db8::1','64:ff9b::a00:1','2002:0a00:0001::1','::ffff:127.0.0.1'):
            with self.assertRaises(provider.PublicReadUnavailable):provider._public(address)
        with patch.object(provider.socket,'getaddrinfo',return_value=rows(IP)) as resolve:
            self.assertEqual(provider._resolve('www.googleapis.com'),(socket.AF_INET,IP))
            resolve.assert_called_once_with('www.googleapis.com',443,type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP)

    def test_connect_uses_validated_numeric_ip_and_original_hostname_tls(self):
        context=MagicMock();context.verify_mode=provider.ssl.CERT_REQUIRED;context.check_hostname=True
        raw=MagicMock();raw.getpeername.return_value=(IP,443)
        with patch.object(provider.ssl,'SSLContext',return_value=context) as factory,patch.object(provider.socket,'socket',return_value=raw),patch.object(provider.socket,'getaddrinfo',side_effect=AssertionError('second DNS forbidden')):
            connection=provider._PinnedHTTPS('www.googleapis.com',(socket.AF_INET,IP),2,12)
            connection.connect()
        raw.connect.assert_called_once_with((IP,443))
        context.wrap_socket.assert_called_once_with(raw,server_hostname='www.googleapis.com')
        factory.assert_called_once_with(provider.ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs.assert_called_once()
        self.assertEqual(context.minimum_version,provider.ssl.TLSVersion.TLSv1_2)

    def fetch_response(self,*,body=b'{}',status=200,headers=None):
        connection=MagicMock();response=MagicMock(status=status)
        headers={'Content-Type':'application/json'} if headers is None else headers
        response.getheader.side_effect=lambda name,default=None:headers.get(name,default)
        response.read1.side_effect=io.BytesIO(body).read1
        connection.getresponse.return_value=response
        return connection,response

    def test_get_only_bounded_body_no_private_response_headers_and_absolute_timer(self):
        connection,response=self.fetch_response(body=b'x'*provider.MAX_BODY,
            headers={'Content-Type':'text/xml','Set-Cookie':'private','Content-Length':str(provider.MAX_BODY)})
        with patch.object(provider,'_resolve',return_value=(socket.AF_INET,IP)),patch.object(provider,'_PinnedHTTPS',return_value=connection),patch.object(provider.threading,'Timer') as timer:
            status,headers,body,location=provider._fetch('www.googleapis.com','/books/v1/volumes',{},timeout=2,deadline=12)
            self.assertEqual(len(body),provider.MAX_BODY);self.assertEqual(headers,{'Content-Type':'text/xml'})
            connection.request.assert_called_once_with('GET','/books/v1/volumes',headers={})
            timer.call_args.args[1]()
            connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            timer.return_value.cancel.assert_called_once();timer.return_value.join.assert_called_once()
            connection.close.assert_called_once()

    def test_compression_oversize_header_or_body_and_late_dns_fail_without_retry(self):
        for headers,body in (({'Content-Encoding':'gzip'},b'zipped'),({'Content-Length':str(provider.MAX_BODY+1)},b''),
                             ({},b'x'*(provider.MAX_BODY+1))):
            connection,_=self.fetch_response(body=body,headers=headers)
            with patch.object(provider,'_resolve',return_value=(socket.AF_INET,IP)),patch.object(provider,'_PinnedHTTPS',return_value=connection),patch.object(provider.threading,'Timer'):
                with self.assertRaises(provider.PublicReadUnavailable):provider._fetch('www.googleapis.com','/books/v1/volumes',{},timeout=2,deadline=12)
                self.assertEqual(connection.request.call_count,1);connection.close.assert_called_once()
        with patch.object(provider,'_resolve',return_value=(socket.AF_INET,IP)),patch.object(provider.time,'monotonic',return_value=13),patch.object(provider,'_PinnedHTTPS') as connection:
            with self.assertRaises(provider.PublicReadUnavailable):provider._fetch('www.googleapis.com','/books/v1/volumes',{},timeout=2,deadline=12)
            connection.assert_not_called()

    def test_expired_after_tls_cannot_send_and_transport_errors_are_static(self):
        connection,_=self.fetch_response()
        with patch.object(provider,'_resolve',return_value=(socket.AF_INET,IP)),patch.object(provider,'_PinnedHTTPS',return_value=connection),patch.object(provider.threading,'Timer'),patch.object(provider,'_remaining',side_effect=[2,2,2,PublicReadError()]):
            with self.assertRaises(OSError):self.call(provider.PublicGet('book-memo'))
            connection.connect.assert_called_once()
            connection.request.assert_not_called()
        with patch.object(provider,'_fetch',side_effect=OSError('private credentials and URL')):
            with self.assertRaisesRegex(OSError,'^public_read_provider_unavailable$'):self.call(provider.PublicGet('book-memo'))


    def test_actual_tls_context_requires_hostname_ca_and_does_not_enable_key_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'must-not-create-tls-key-log'
            with patch.dict(os.environ,{'SSLKEYLOGFILE':str(path)}):
                connection=provider._PinnedHTTPS('www.googleapis.com',(socket.AF_INET,IP),2,12)
            self.assertEqual(connection._context.verify_mode,provider.ssl.CERT_REQUIRED)
            self.assertTrue(connection._context.check_hostname)
            self.assertIsNone(connection._context.keylog_filename)
            self.assertFalse(path.exists())

    def test_unexpected_actual_peer_blocks_tls_and_first_get(self):
        raw=MagicMock();raw.getpeername.return_value=('127.0.0.1',443)
        context=MagicMock();context.verify_mode=provider.ssl.CERT_REQUIRED;context.check_hostname=True
        with patch.object(provider.ssl,'SSLContext',return_value=context),patch.object(provider.socket,'socket',return_value=raw):
            connection=provider._PinnedHTTPS('www.googleapis.com',(socket.AF_INET,IP),2,12)
            with self.assertRaises(provider.PublicReadUnavailable):connection.connect()
        context.wrap_socket.assert_not_called()

    def test_body_read_rechecks_absolute_deadline_before_returning_partial_data(self):
        connection,response=self.fetch_response()
        now=[10.]
        def late_read(size):now[0]=13.;return b'{}'
        response.read1.side_effect=late_read
        with patch.object(provider,'_resolve',return_value=(socket.AF_INET,IP)),patch.object(provider,'_PinnedHTTPS',return_value=connection),patch.object(provider.threading,'Timer'),patch.object(provider.time,'monotonic',side_effect=lambda:now[0]):
            with self.assertRaises(provider.PublicReadUnavailable):provider._fetch('www.googleapis.com','/books/v1/volumes',{},timeout=2,deadline=12)
        response.read1.assert_called_once();connection.close.assert_called_once()


class PublicReadError(OSError):
    pass


if __name__=='__main__':unittest.main()
