"""Actual nested local mTLS; Executor/provider and PostgreSQL ledger are doubles."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from tests import test_ha_egress_transport as transport_fixture
from ha_egress import client, providers
from ha_egress.boot import internal_dispatch
from ha_egress.executor_provider import ExecutorForwarder
from ha_egress.server import BrokerServer
from ha_runtime.effects import TrustedIdentity

ROOT=Path(__file__).resolve().parents[1]


class NestedBrokerTransportTest(unittest.TestCase):
    def setUp(self):
        # Reuse only certificate issuance/local authority-file bypass. Every TLS
        # socket, fingerprint map, HTTP parser, typed adapter and inner guard is real.
        self.tls=transport_fixture.TransportTest(methodName='runTest')
        self.tls.setUp();self.addCleanup(self.tls.doCleanups)
        self.tls.certificate('executor',False)
        self.portal=TrustedIdentity('portal-web','A',7)
        self.executor_identity=TrustedIdentity('homeops-executor','A',7)
        self.counts={'executor_http':0,'internal_inspect':0,'internal_restart':0,'outer':0}
        self.lock=threading.Lock();self.four=threading.Event();self.release=threading.Event()
        self.addCleanup(self.release.set)
        self.gates=[threading.Event() for _ in range(4)]
        self.done=[threading.Event() for _ in range(4)]
        self.addCleanup(lambda:[event.set() for event in self.gates])
        self.hold_internal=False;self.held=0
        self.two_internal=threading.Event();self.internal_release=threading.Event()
        self.addCleanup(self.internal_release.set)
        self.close_http=False;self.truncate=False;self.json_calls=0
        self.unknown=False
        def context():
            value=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            value.minimum_version=ssl.TLSVersion.TLSv1_3;value.verify_mode=ssl.CERT_REQUIRED
            value.load_verify_locations(cafile=self.tls.root/'ca.pem')
            value.load_cert_chain(self.tls.root/'server.pem',self.tls.root/'server.key')
            return value
        def internal(identity,request):
            self.assertEqual(identity,self.executor_identity)
            self.assertEqual(request['payload'],{})
            kind='inspect' if request['operation']=='executor.docker.inspect.book-memo' else 'restart'
            self.assertEqual(request['operation'],'executor.docker.'+kind+'.book-memo')
            with self.lock:
                self.counts['internal_'+kind]+=1
                if self.hold_internal:
                    self.held+=1
                    if self.held==2:self.two_internal.set()
            if self.hold_internal:self.assertTrue(self.internal_release.wait(1.5))
            return {'status':'completed','result':{'service':'book-memo','status':'running','health':'healthy','started_at':''}}
        fingerprint=self.tls.root/'executor.pem'
        from cryptography import x509
        cert=x509.load_pem_x509_certificate(fingerprint.read_bytes())
        self.inner=BrokerServer(('127.0.0.1',0),context(),
            {cert.fingerprint(hashes.SHA256()).hex():self.executor_identity},internal_dispatch(internal),plan_sha256='a'*64,workers=2)
        self.start(self.inner)
        self.inner_config=dict(self.tls.config,url='https://localhost:'+str(self.inner.server_port),
                               certificate=str(self.tls.root/'executor.pem'),key=str(self.tls.root/'executor.key'))
        fixture=self
        class ExecutorHandler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def reply(self,value):
                raw=json.dumps(value).encode();self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)+(7 if fixture.truncate else 0)))
                if fixture.close_http:
                    self.send_header('Connection','close');self.close_connection=True
                self.end_headers();self.wfile.write(raw)
            def perform(self,kind):
                try:
                    if self.headers.get('X-HomeOps-Executor-Secret')!='f'*64 or self.headers.get('X-HomeOps-Target-Node')!='A':raise ValueError()
                    with fixture.lock:
                        fixture.counts['executor_http']+=1
                        ticket=fixture.counts['executor_http']-1
                        if fixture.counts['executor_http']==4:fixture.four.set()
                    if kind=='inspect':
                        if self.path!='/v1/diagnostics/book-memo':raise ValueError()
                        if not fixture.gates[ticket].wait(1.5):raise TimeoutError()
                    else:
                        if self.path!='/v1/restarts':raise ValueError()
                        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        fixture.assertEqual(body,{'service':'book-memo','incident_id':'fixture-incident',
                            'approval_token':'fixture-approval','action':'restart_container','target_node':'A'})
                    result=client.dispatch(fixture.inner_config,'executor.docker.'+kind+'.book-memo','nested-'+kind,{})
                    fixture.assertEqual(result['result']['status'],'running')
                    container={'status':'running','health':'healthy','started_at':'','exit_code':None,'cpu_percent':None,'memory_percent':None}
                    value={'service':'book-memo','container':container}
                    if kind=='inspect':value['logs']=[]
                    else:value['status']='unknown' if fixture.unknown else 'running'
                    self.reply(value)
                    if kind=='inspect':fixture.done[ticket].set()
                except Exception:
                    self.send_error(503)
            def do_GET(self):self.perform('inspect')
            def do_POST(self):
                if self.path=='/fixture-json':
                    self.rfile.read(int(self.headers['Content-Length']))
                    fixture.json_calls+=1;self.reply({'accepted':True})
                else:self.perform('restart')
        self.http=ThreadingHTTPServer(('127.0.0.1',0),ExecutorHandler)
        self.http.daemon_threads=True
        self.http.socket=context().wrap_socket(self.http.socket,server_side=True)
        self.start(self.http)
        executor_context=ssl.create_default_context(cafile=self.tls.root/'ca.pem')
        executor_context.load_cert_chain(self.tls.root/'client.pem',self.tls.root/'client.key')
        self.executor_context=executor_context
        self.forwarder=ExecutorForwarder(url='https://localhost:'+str(self.http.server_port),node='A',secret='f'*64,
            context=executor_context,server_sha256=self.tls.config['server_sha256'],services={'book-memo'})
        self.public=self.tls.server
        self.public.identities=MappingProxyType({self.tls.client_cert.fingerprint(hashes.SHA256()).hex():self.portal})
        def outer(identity,request):
            self.assertEqual(identity,self.portal)
            with self.lock:self.counts['outer']+=1
            operation=request['operation'];payload=request['payload']
            if operation=='portal.executor.diagnose':ack=self.forwarder.diagnose(payload,timeout=1.8,deadline=time.monotonic()+1.8)
            elif operation=='portal.executor.restart':ack=self.forwarder.restart(payload,timeout=1.8,deadline=time.monotonic()+1.8)
            else:raise ValueError()
            return {'status':'completed','provider_ack':ack.reference,'result':ack.result,'replayed':False}
        self.public.dispatch=outer

    def start(self,server):
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():
            self.release.set();server.shutdown();server.server_close();thread.join(3)
        self.addCleanup(close)

    def call(self,operation='portal.executor.diagnose',payload=None):
        return client.dispatch(self.tls.config,operation,'fixture-request',{'service':'book-memo'} if payload is None else payload)

    def receipt(self):
        sources=('tests/test_ha_nested_broker.py','tests/test_ha_egress_transport.py','infra/k8s/ha_egress/hyundai_provider.py','infra/k8s/ha_egress/server.py','infra/k8s/ha_egress/client.py',
                 'infra/k8s/ha_egress/boot.py','infra/k8s/ha_egress/executor_provider.py','infra/k8s/ha_runtime/effects.py')
        value={'scope':'actual_local_nested_mtls_transport_only','status':'PASS','calls':dict(self.counts),
               'public_workers':4,'internal_workers':2,'outer_saturated':True,'nested_executor_release':'one_at_a_time_after_four_outer_slots_occupied','mutual_cross_certificate_denials':2,
               'internal_operation_guard_denied':True,'all_four_simultaneous_inner_success_guaranteed':False,'internal_saturated_handlers':2,'internal_overflow_denied_before_dispatch':1,'unknown_result_retries':0,'actual_provider_calls':0,
               'production_access':False,'actual_executor_application':False,'actual_pg_ledger':False,
               'production_authority_file_ownership':False,'windows_whole_transition_verified':False,
               'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}}
        target=ROOT/'build/ha-predeploy-evidence/nested-mtls-unit-receipt.json'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value,sort_keys=True,indent=2))

    def test_four_outer_requests_nest_through_reserved_internal_mtls_then_unknown_has_no_retry(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending=[pool.submit(self.call) for _ in range(4)]
            try:
                self.assertTrue(self.four.wait(1.5))
                self.assertFalse(self.public.slots.acquire(blocking=False))
                # The independent listener still has capacity while all four
                # public handlers are awaiting their real nested Executor hop.
                for operation in ('executor.docker.inspect.book-memo','executor.docker.restart.book-memo'):
                    result=client.dispatch(self.inner_config,operation,'reserved',{})
                    self.assertEqual(result['result']['status'],'running')
                with self.assertRaises(client.BrokerUnavailable):self.call()
                for gate,done in zip(self.gates,self.done):
                    gate.set();self.assertTrue(done.wait(.5))
            finally:
                self.release.set()
                for event in self.gates:event.set()
            results=[future.result(timeout=3) for future in pending]
        self.assertTrue(all(item['result']['value']['container']['status']=='running' for item in results))
        self.assertEqual(self.counts,{'outer':4,'executor_http':4,'internal_inspect':5,'internal_restart':1})
        # Both certificates are CA-valid; only the listener's fingerprint map
        # selects the allowed identity. Rejected calls never reach dispatch.
        for config in (dict(self.inner_config,certificate=self.tls.config['certificate'],key=self.tls.config['key']),
                       dict(self.inner_config,url=self.tls.config['url'])):
            with self.assertRaises(client.BrokerUnavailable):client.dispatch(config,'executor.docker.inspect.book-memo','wrong-cert',{})
        with self.assertRaises(client.BrokerUnavailable):client.dispatch(self.inner_config,'portal.executor.diagnose','wrong-operation',{})
        self.assertEqual(self.counts,{'outer':4,'executor_http':4,'internal_inspect':5,'internal_restart':1})
        self.hold_internal=True
        with ThreadPoolExecutor(max_workers=2) as pool:
            held=[pool.submit(client.dispatch,self.inner_config,'executor.docker.inspect.book-memo','bounded',{}) for _ in range(2)]
            try:
                self.assertTrue(self.two_internal.wait(1))
                before=dict(self.counts)
                with self.assertRaises(client.BrokerUnavailable):client.dispatch(self.inner_config,'executor.docker.inspect.book-memo','overflow',{})
                self.assertEqual(self.counts,before)
            finally:self.internal_release.set()
            self.assertTrue(all(future.result(timeout=2)['status']=='completed' for future in held))
        self.hold_internal=False
        payload={'service':'book-memo','incident_id':'fixture-incident','approval_token':'fixture-approval'}
        self.assertEqual(self.call('portal.executor.restart',payload)['result']['value']['status'],'running')
        self.unknown=True
        before=dict(self.counts)
        with self.assertRaises(client.BrokerUnavailable):self.call('portal.executor.restart',payload)
        self.assertEqual(self.counts,{**before,'outer':before['outer']+1,'executor_http':before['executor_http']+1,
                                     'internal_restart':before['internal_restart']+1})
        self.receipt()


    def test_http10_and_connection_close_accept_complete_json_reject_truncated_length_without_retry(self):
        payload={'service':'book-memo','incident_id':'fixture-incident','approval_token':'fixture-approval'}
        self.close_http=True
        for protocol in ('HTTP/1.0','HTTP/1.1'):
            with self.subTest(protocol=protocol):
                self.http.RequestHandlerClass.protocol_version=protocol
                self.truncate=False
                value=self.call('portal.executor.restart',payload)
                self.assertEqual(value['result']['value']['status'],'running')
                before=dict(self.counts)
                self.truncate=True
                # The JSON itself is complete: only the declared wire length is
                # short. Rejecting this proves EOF cannot be mistaken for success.
                with self.assertRaises(client.BrokerUnavailable):self.call('portal.executor.restart',payload)
                self.assertEqual(self.counts,{**before,'outer':before['outer']+1,
                    'executor_http':before['executor_http']+1,'internal_restart':before['internal_restart']+1})
                with patch.object(providers.ssl,'create_default_context',return_value=self.executor_context):
                    self.truncate=False
                    self.assertEqual(providers.post_json('localhost:'+str(self.http.server_port),'/fixture-json',{},
                        timeout=1,deadline=time.monotonic()+1),{'accepted':True})
                    before_json=self.json_calls;self.truncate=True
                    with self.assertRaises(providers.ProviderUnavailable):
                        providers.post_json('localhost:'+str(self.http.server_port),'/fixture-json',{},
                            timeout=1,deadline=time.monotonic()+1)
                    self.assertEqual(self.json_calls,before_json+1)
        sources=('tests/test_ha_nested_broker.py','infra/k8s/ha_egress/providers.py',
                 'infra/k8s/ha_egress/hyundai_provider.py','infra/k8s/ha_egress/executor_provider.py')
        value={'scope':'actual_local_mtls_http_connection_close','status':'PASS','protocols':['HTTP/1.0','HTTP/1.1'],
               'complete_json_success':4,'declared_length_truncation_denied':4,'truncated_requests_retried':0,
               'calls':dict(self.counts),'json_http_calls':self.json_calls,'actual_provider_calls':0,
               'production_access':False,'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}}
        target=ROOT/'build/ha-predeploy-evidence/nested-mtls-close-receipt.json'
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value,sort_keys=True,indent=2))


if __name__=='__main__':unittest.main()
