"""Provider adapter contracts; no live provider, Docker or native acceptance."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from email.message import Message
import hashlib
import importlib
import io
import json
from pathlib import Path
import ssl
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
from ha_egress import providers as P
from ha_egress import hyundai_provider as H
from ha_egress import executor_provider as X


def budget():return {'timeout':1,'deadline':time.monotonic()+1}


class TelegramPollerTests(unittest.TestCase):
    def setUp(self):self.poller=P.TelegramPoller('12345:'+'x'*24,'-12345')

    def test_fixed_get_updates_with_bounded_poll_and_explicit_offset_only(self):
        for offset in (None,0,2**63-1):
            with self.subTest(offset=offset),patch.object(P,'post_json',return_value={'ok':True,'result':[]}) as post:
                ack=self.poller({'offset':offset},**budget())
                host,path,body=post.call_args.args
                self.assertEqual(host,'api.telegram.org');self.assertEqual(path,'/bot12345:'+'x'*24+'/getUpdates')
                self.assertEqual(body,{'timeout':0,'limit':3,'allowed_updates':['message']}|({} if offset is None else {'offset':offset}))
                self.assertEqual(ack.result,{'updates':[]});post.assert_called_once()
        self.assertNotIn('x'*24,repr(self.poller))

    def test_offset_type_shape_and_bounds_fail_before_transport(self):
        invalid=[{},None,{'offset':False},{'offset':-1},{'offset':2**63},{'offset':1.0},
                 {'offset':'1'},{'offset':0,'chat_id':'other'},{'offset':0,'url':'https://other.invalid'}]
        with patch.object(P,'post_json') as post:
            for payload in invalid:
                with self.subTest(payload=payload),self.assertRaises(P.ProviderUnavailable):self.poller(payload,**budget())
            post.assert_not_called()

    def test_unknown_chat_and_non_text_data_are_redacted_without_losing_update_ids(self):
        response={'ok':True,'result':[
            {'update_id':7,'message':{'chat':{'id':-12345},'text':'allowed','private':'hidden'}},
            {'update_id':8,'message':{'chat':{'id':999},'text':'unknown-private-text'}},
            {'update_id':9,'message':{'chat':{'id':True},'text':'boolean-chat'}}]}
        with patch.object(P,'post_json',return_value=response):ack=self.poller({'offset':0},**budget())
        self.assertEqual(ack.result,{'updates':[{'update_id':7,'chat_id':'-12345','text':'allowed'},
                                              {'update_id':8,'chat_id':'','text':None},
                                              {'update_id':9,'chat_id':'','text':None}]})
        self.assertNotIn('unknown-private-text',repr(ack));self.assertNotIn('private',repr(ack))
        with patch.object(P,'post_json',return_value={'ok':True,'result':[{'update_id':10,'message':{'chat':{'id':-12345},'text':'x'*4097}}]}):
            self.assertIsNone(self.poller({'offset':0},**budget()).result['updates'][0]['text'])

    def test_malformed_or_oversized_update_response_is_rejected_without_retry(self):
        invalid=[{'ok':False,'result':[]},{'ok':True,'result':{}},
                 {'ok':True,'result':[{'update_id':True}]},{'ok':True,'result':[{'update_id':-1}]},
                 {'ok':True,'result':[{'update_id':2**63}]},{'ok':True,'result':[{'update_id':n} for n in range(4)]}]
        for response in invalid:
            with self.subTest(response=response),patch.object(P,'post_json',return_value=response) as post:
                with self.assertRaises(P.ProviderUnavailable):self.poller({'offset':None},**budget())
                post.assert_called_once()
        with patch.object(P,'post_json',side_effect=P.ProviderUnavailable()) as post:
            with self.assertRaisesRegex(P.ProviderUnavailable,'^effect_provider_unavailable$'):self.poller({'offset':1},**budget())
            post.assert_called_once()


@contextmanager
def canonical_car_module():
    saved={key:value for key,value in sys.modules.items() if key=='app' or key.startswith('app.')}
    prior_path=list(sys.path)
    try:
        for key in saved:sys.modules.pop(key,None)
        sys.path.insert(0,str(ROOT/'car-care-worker'))
        yield importlib.import_module('app.services.hyundai')
    finally:
        for key in list(sys.modules):
            if key=='app' or key.startswith('app.'):sys.modules.pop(key,None)
        sys.modules.update(saved);sys.path[:]=prior_path


class PrivateBackend:
    def __init__(self):self.payload={};self.events=[]
    def require_worker(self,service):self.events.append(('worker',service))
    @contextmanager
    def worker_scope(self,service):self.events.append(('scope',service));yield
    @contextmanager
    def state_transaction(self,service,key,default):
        self.events.append(('transaction',service,key));yield self.payload;self.events.append(('commit',service,key))
    def read_state(self,service,key,default):return deepcopy(self.payload)


class HyundaiAdapterTests(unittest.TestCase):
    def test_actual_canonical_client_uses_injected_private_backend_and_transport_without_token_files(self):
        with canonical_car_module() as canonical:
            backend=PrivateBackend();broker=MagicMock();broker.credential_backend.return_value=backend
            requests=[]
            def send(request,*,timeout):
                requests.append(request)
                value={'access_token':'fixture-access-private','refresh_token':'fixture-refresh-private','expires_in':3600} if request.full_url.endswith('/token') else {}
                return io.BytesIO(json.dumps(value).encode())
            with patch.object(H,'canonical_client_type',return_value=canonical.HyundaiClient),patch.object(H,'BoundRequestTransport',return_value=send) as transport:
                provider=H.HyundaiProvider(broker,client_id='fixture-id',client_secret='fixture-secret',redirect_uri='https://callback.invalid/car')
                self.assertIs(provider._client._state_backend,backend)
                with patch.object(canonical,'runtime',side_effect=AssertionError('global backend selected')), \
                     patch.object(canonical,'urlopen',side_effect=AssertionError('legacy network used')), \
                     patch.object(Path,'mkdir',side_effect=AssertionError('token directory used')), \
                     patch.object(Path,'read_text',side_effect=AssertionError('token file read')), \
                     patch.object(Path,'write_text',side_effect=AssertionError('token file write')), \
                     patch.object(canonical.os,'open',side_effect=AssertionError('token lock file used')), \
                     patch.object(canonical.fcntl,'flock',side_effect=AssertionError('file lock used')):
                    begun=provider.begin({},**budget())
                    state=parse_qs(urlsplit(begun.result['url']).query)['state'][0]
                    self.assertEqual(backend.payload['authorization_state'],state)
                    connected=provider.complete({'code':'fixture-code','state':state},**budget())
                self.assertEqual(connected.result,{'connected':True})
                self.assertNotIn('fixture-code',connected.reference);self.assertNotIn('private',repr(connected))
                self.assertEqual(backend.payload['refresh_token'],'fixture-refresh-private')
                self.assertNotIn('authorization_state',backend.payload)
                self.assertEqual([request.get_method() for request in requests],['POST','POST'])
                self.assertEqual([urlsplit(request.full_url).hostname for request in requests],['prd.kr-ccapi.hyundai.com','dev.kr-ccapi.hyundai.com'])
                self.assertEqual(transport.call_count,2);broker.credential_backend.assert_called_once()
                self.assertFalse(hasattr(provider._local,'deadline'))
                with self.assertRaises(P.ProviderUnavailable):provider._open(Request('https://dev.kr-ccapi.hyundai.com/'),timeout=1)

    def test_refresh_unknown_keeps_private_uncertainty_and_never_retries_provider(self):
        with canonical_car_module() as canonical:
            backend=PrivateBackend();backend.payload={'refresh_token':'fixture-refresh-private'}
            broker=MagicMock();broker.credential_backend.return_value=backend
            attempts=[]
            def lost(request,*,timeout):attempts.append(request);raise P.ProviderUnavailable()
            with patch.object(H,'canonical_client_type',return_value=canonical.HyundaiClient),patch.object(H,'BoundRequestTransport',return_value=lost),patch.object(canonical,'urlopen',side_effect=AssertionError('legacy network used')):
                provider=H.HyundaiProvider(broker,client_id='fixture',client_secret='fixture-secret')
                for _ in range(2):
                    with self.assertRaisesRegex(P.ProviderUnavailable,'^effect_provider_unavailable$'):provider.snapshot({},**budget())
            self.assertTrue(backend.payload['refresh_uncertain']);self.assertEqual(len(attempts),1)
            self.assertIn(('commit','car-care-worker','hyundai_tokens'),backend.events)
            self.assertFalse(hasattr(provider._local,'deadline'))

    def test_typed_payload_deadline_and_snapshot_projection(self):
        client=MagicMock();broker=MagicMock()
        snapshot=SimpleNamespace(observed_at=datetime(2026,11,9,tzinfo=timezone.utc),odometer_km=123,dte_km=20,
                                 warnings={'washer_fluid','fuel'},unknown_warnings={'engine_oil'})
        client.fetch_snapshot.return_value=SimpleNamespace(status='success',snapshot=snapshot)
        with patch.object(H,'canonical_client_type',return_value=MagicMock(return_value=client)):
            provider=H.HyundaiProvider(broker,client_id='fixture',client_secret='fixture-secret')
        result=provider.snapshot({},**budget())
        self.assertEqual(result.result['snapshot']['warnings'],['fuel','washer_fluid'])
        self.assertEqual(set(result.result),{'status','snapshot'})
        for method,payload in ((provider.begin,{'url':'https://other.invalid'}),(provider.complete,{'code':'x'}),
                               (provider.complete,{'code':'x','state':False}),(provider.snapshot,{'token':'private'})):
            with self.assertRaises(P.ProviderUnavailable):method(payload,**budget())
        client.reset_mock()
        with self.assertRaises(P.ProviderUnavailable):provider.snapshot({},timeout=1,deadline=time.monotonic()-1)
        client.fetch_snapshot.assert_not_called();self.assertFalse(hasattr(provider._local,'deadline'))


class ExecutorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.secret='fixture-executor-private-'+'x'*32
        self.cert=b'fixture-certificate';self.pin=hashlib.sha256(self.cert).hexdigest()
        self.context=ssl.create_default_context()
        self.forward=X.ExecutorForwarder(url='https://executor.invalid:9443',node='B',secret=self.secret,
                                         context=self.context,server_sha256=self.pin,services={'book-memo','youtube-memo'})
        self.container={'status':'running','health':'healthy','started_at':'2026-11-09T00:00:00Z',
                        'exit_code':0,'cpu_percent':1.5,'memory_percent':2.0}

    @contextmanager
    def wire(self,value,headers=None):
        seen=[];response=io.BytesIO(json.dumps(value).encode());response.headers=headers or Message()
        def send(request,*,timeout):seen.append((request,timeout));return response
        with patch.object(X,'BoundRequestTransport',return_value=send) as factory:yield seen,factory

    def test_fixed_diagnostic_get_headers_peer_pin_and_typed_projection(self):
        value={'service':'book-memo','container':self.container,'logs':[]}
        with self.wire(value) as (seen,factory):ack=self.forward.diagnose({'service':'book-memo'},**budget())
        request,_=seen[0];headers={key.lower():value for key,value in request.header_items()}
        self.assertEqual(request.full_url,'https://executor.invalid:9443/v1/diagnostics/book-memo')
        self.assertEqual(request.get_method(),'GET');self.assertIsNone(request.data)
        self.assertEqual(headers['x-homeops-executor-secret'],self.secret);self.assertEqual(headers['x-homeops-target-node'],'B')
        self.assertNotIn('x-homeops-request-id',headers)
        self.assertEqual(factory.call_args.args[0],{('executor.invalid',9443)})
        self.assertIs(factory.call_args.kwargs['context'],self.context);self.assertEqual(factory.call_args.kwargs['server_sha256'],self.pin)
        self.assertEqual(ack.result['value'],value);self.assertNotIn(self.secret,repr(ack))

    def test_all_diagnostics_require_one_managed_header_and_exact_declared_rows(self):
        value=[{'service':'book-memo','container':self.container,'logs':[]}]
        headers=Message();headers['X-HomeOps-Managed-Services']='book-memo'
        with self.wire(value,headers):self.assertEqual(self.forward.diagnose({'service':None},**budget()).result['managed_services'],['book-memo'])
        for declarations in ([],['book-memo','book-memo'],['unknown'],['book-memo,book-memo'],['book-memo,youtube-memo']):
            headers=Message()
            for name in declarations:headers['X-HomeOps-Managed-Services']=name
            with self.subTest(header=declarations),self.wire(value,headers):
                with self.assertRaises(P.ProviderUnavailable):self.forward.diagnose({'service':None},**budget())

    def test_unavailable_service_is_projected_without_losing_healthy_diagnostics_or_private_error(self):
        healthy={'service':'book-memo','container':self.container,'logs':[]}
        unavailable={'service':'youtube-memo','container':{'status':'unknown','health':'unknown'},'logs':[],
                     'error':'fixture-private-unavailable-detail'}
        headers=Message();headers['X-HomeOps-Managed-Services']='book-memo,youtube-memo'
        with self.wire([healthy,unavailable],headers):ack=self.forward.diagnose({'service':None},**budget())
        self.assertEqual(ack.result['value'][0],healthy)
        self.assertEqual(ack.result['value'][1],{'service':'youtube-memo','container':{'status':'unknown','health':'unknown',
                         'started_at':'','exit_code':None,'cpu_percent':None,'memory_percent':None},'logs':[]})
        self.assertEqual(ack.result['managed_services'],['book-memo','youtube-memo'])
        self.assertNotIn('fixture-private-unavailable-detail',repr(ack))
        with self.wire(unavailable):self.assertEqual(self.forward.diagnose({'service':'youtube-memo'},**budget()).result['value']['container']['status'],'unknown')
        with patch.object(self.forward,'_request') as wire:
            for service in ([],{},False,1):
                for action,payload in ((self.forward.diagnose,{'service':service}),
                                       (self.forward.restart,{'service':service,'incident_id':'i','approval_token':'p'})):
                    with self.subTest(service=service),self.assertRaises(P.ProviderUnavailable):action(payload,**budget())
            wire.assert_not_called()

    def test_restart_retains_executor_incident_and_approval_body_and_fixed_target(self):
        payload={'incident_id':'fixture-incident','approval_token':'fixture-approval-private','service':'book-memo'}
        response={'service':'book-memo','status':'running','container':self.container}
        with self.wire(response) as (seen,_):ack=self.forward.restart(payload,**budget())
        request,_=seen[0];body=json.loads(request.data)
        self.assertEqual(request.full_url,'https://executor.invalid:9443/v1/restarts');self.assertEqual(request.get_method(),'POST')
        self.assertEqual(body,payload|{'action':'restart_container','target_node':'B'})
        self.assertNotIn('fixture-approval-private',repr(ack));self.assertNotIn('fixture-incident',ack.reference)
        with self.wire({'status':'accepted'}) as (seen,_):ack=self.forward.restart_all({'request_id':'fixture-all'},**budget())
        request,_=seen[0];headers={key.lower():value for key,value in request.header_items()}
        self.assertEqual(request.full_url,'https://executor.invalid:9443/v1/restarts/all');self.assertEqual(request.get_method(),'POST')
        self.assertEqual(headers['x-homeops-request-id'],'fixture-all');self.assertIsNone(request.data)
        self.assertEqual(ack.result['value'],{'status':'accepted'})

    def test_malformed_payload_and_remote_container_shape_never_bypass_checks(self):
        with patch.object(self.forward,'_request') as wire:
            for action,payload in ((self.forward.diagnose,{}),(self.forward.diagnose,{'service':'unknown'}),
                                   (self.forward.restart,{'incident_id':'i','approval_token':'a','service':'unknown'}),
                                   (self.forward.restart,{'incident_id':'','approval_token':'a','service':'book-memo'}),
                                   (self.forward.restart_all,{'request_id':False})):
                with self.assertRaises(P.ProviderUnavailable):action(payload,**budget())
            wire.assert_not_called()
        baseline={'service':'book-memo','container':self.container,'logs':[]}
        for value in (baseline|{'logs':['private-log']},baseline|{'extra':True},baseline|{'service':'youtube-memo'},
                      baseline|{'container':self.container|{'status':'unexpected'}},baseline|{'container':self.container|{'cpu_percent':True}}):
            with self.subTest(value=value),self.wire(value):
                with self.assertRaises(P.ProviderUnavailable):self.forward.diagnose({'service':'book-memo'},**budget())

    def test_transport_timeout_and_http_denial_are_single_attempt_static_errors(self):
        for failure in ('timeout','denied'):
            connection=MagicMock();connection.sock.getpeercert.return_value=self.cert
            if failure=='timeout':connection.getresponse.side_effect=TimeoutError(self.secret)
            else:connection.getresponse.return_value.status=403
            with self.subTest(failure=failure),patch.object(H.http.client,'HTTPSConnection',return_value=connection) as factory:
                with self.assertRaisesRegex(P.ProviderUnavailable,'^effect_provider_unavailable$') as error:
                    self.forward.restart({'incident_id':'fixture','approval_token':'fixture-private','service':'book-memo'},**budget())
                self.assertNotIn(self.secret,str(error.exception));self.assertIsNone(error.exception.__cause__)
                factory.assert_called_once();connection.request.assert_called_once();connection.close.assert_called_once()

    def test_transport_origin_pin_and_expired_deadline_prevent_first_send(self):
        connection=MagicMock();connection.sock.getpeercert.return_value=b'wrong-certificate'
        with patch.object(H.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(P.ProviderUnavailable):self.forward.diagnose({'service':'book-memo'},**budget())
            connection.request.assert_not_called();connection.close.assert_called_once()
        with patch.object(H.http.client,'HTTPSConnection') as factory:
            with self.assertRaises(P.ProviderUnavailable):H.BoundRequestTransport({('executor.invalid',9443)},time.monotonic()+1)(Request('https://other.invalid/'),timeout=1)
            factory.assert_not_called()
        connection=MagicMock()
        with patch.object(H.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaises(P.ProviderUnavailable):self.forward.diagnose({'service':'book-memo'},timeout=1,deadline=time.monotonic()-1)
            connection.request.assert_not_called();connection.close.assert_not_called()


if __name__=='__main__':unittest.main()
