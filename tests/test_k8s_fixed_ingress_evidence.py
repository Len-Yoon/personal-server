import importlib.util
import struct
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('fixed_evidence',ROOT/'infra/k8s/tools/fixed-ingress-evidence.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class FixedEvidenceTests(unittest.TestCase):
    def raw(self):
        # Linux proc stat field22 is process birth time; proc tcp inode field10.
        stat='22 (nc) '+' '.join(['S']+['0']*18+['111'])
        endpoint=lambda ip,port:struct.pack('<I',int.from_bytes(bytes(map(int,ip.split('.'))),'big')).hex().upper()+':'+format(port,'04X')
        row='0: '+endpoint('192.0.2.7',42000)+' '+endpoint('192.0.2.8',8003)+' 01 0 0 0 0 0 123'
        return '\n'.join(['P=22','S1='+stat,'I=123','R='+row,'S2='+stat,'HTTP/1.1 200 OK',''])

    def test_socket_is_bound_to_same_live_pid_inode_target_and_http(self):
        raw=self.raw();self.assertEqual(m.socket_record(raw,'192.0.2.8',8003),('192.0.2.7',42000))
        for bad in (raw.replace('S2=22 (nc)','S2=23 (nc)'),raw.replace('I=123','I=999'),raw.replace(' 01 ',' 06 '),raw.replace('200 OK','503 Error'),raw+'\nP=22'):
            with self.subTest(raw=bad),self.assertRaises(m.Blocked):m.socket_record(bad,'192.0.2.8',8003)
        with self.assertRaises(m.Blocked):m.socket_record(raw,'192.0.2.9',8003)
        with self.assertRaises(m.Blocked):m.socket_record(raw,'192.0.2.8',8000)

    def test_exact_access_marker_time_and_port_are_required(self):
        now=datetime.now(timezone.utc);marker='a'*12
        line=now.isoformat()+' INFO 192.0.2.8:42000 - "GET /health?codex_fixed_probe='+marker+' HTTP/1.1" 200 OK'
        self.assertEqual(m.match_access(line,marker,42000,now),'192.0.2.8')
        for bad in (line+'\n'+line,line.replace(':42000',':42001'),line.replace(' 200 ',' 500 '),line.replace(marker,'b'*12)):
            with self.subTest(log=bad),self.assertRaises(m.Blocked):m.match_access(bad,marker,42000,now)

    def test_marker_shell_rejects_injection_and_bounds_child(self):
        shell=m.held_client_script('b'*12)
        self.assertIn('-w 4',shell);self.assertIn('nc -p "$3"',shell);self.assertIn('/proc/$p/stat',shell);self.assertIn('wait "$p"',shell)
        for bad in ('a;echo bad','$(id)','a'*13):
            with self.assertRaises(m.Blocked):m.held_client_script(bad)

    def test_delete_refuses_foreign_uid_or_changed_spec_before_mutation(self):
        collector=m.Collector(Mock(),{},Mock());backend=Mock()
        obj={'metadata':{'uid':'foreign','resourceVersion':'4','labels':{m.OWNER:collector.run}},'spec':{}}
        collector.records[('pod','unknown')]={'kind':'pod','name':'unknown','uid':'owned','spec_sha256':m.digest({})}
        collector.current=Mock(return_value=obj)
        with self.assertRaises(m.Blocked):collector.delete(backend,'pod','unknown')
        backend.run.assert_not_called()
        obj['metadata']['uid']='owned';obj['spec']={'foreign':True}
        with self.assertRaises(m.Blocked):collector.delete(backend,'pod','unknown')
        backend.run.assert_not_called()

    def test_probe_cannot_treat_transport_or_http_errors_as_policy_denial(self):
        collector=m.Collector(Mock(),{},Mock());backend=Mock()
        collector.run_command=Mock(return_value='HTTP/1.1 503 Error\nPROBE_RC=1\n')
        with self.assertRaises(m.Blocked):collector.probe(backend,'unknown','192.0.2.8',8003)
        collector.run_command=Mock(return_value='wget: download timed out\nPROBE_RC=1\n')
        result=collector.probe(backend,'unknown','192.0.2.8',8003)
        self.assertEqual(result,{'transport_ok':True,'http_status':None,'exit_code':1,'reason':'timeout'})
        collector.run_command=Mock(return_value='transport failed without reason')
        with self.assertRaises(m.Blocked):collector.probe(backend,'unknown','192.0.2.8',8003)

if __name__=='__main__': unittest.main()

class CollectorBoundaryTests(unittest.TestCase):
    def collector(self):return m.Collector(Mock(),{'address':{'fixed_ip':'192.0.2.7'}},Mock())

    def test_admission_drift_keeps_owned_response_in_journal_for_exact_cleanup(self):
        collector=self.collector();backend=Mock()
        collector.current=Mock(return_value=None)
        actual={'kind':'Pod','metadata':{'name':'server','namespace':collector.ns,'uid':'owned','labels':{m.OWNER:collector.run}},'spec':{'containers':[{'name':'foreign'}]}}
        collector.api=Mock(return_value=actual)
        intent={'apiVersion':'v1','kind':'Pod','metadata':{'name':'server','namespace':collector.ns},'spec':{'containers':[{'name':'server'}]}}
        with self.assertRaises(m.Blocked):collector.create(backend,intent)
        self.assertEqual(collector.records[('pod','server')]['uid'],'owned')
        self.assertTrue(any(call.args[0]=='disposable_created' for call in collector.journal.call_args_list))

    def test_per_port_diagnostic_cannot_delete_rollout_unknown_client(self):
        from unittest.mock import patch
        collector=self.collector();collector.created=True
        collector.records[('pod','unknown')]={'uid':'rollout-client'}
        child=Mock();child._diagnostic.return_value={'status':'PASS'}
        with patch.object(m,'Collector',return_value=child):
            result=collector.diagnostic(Mock(),'book-memo',{}, {}, {}, {})
        self.assertEqual(result,{'status':'PASS'})
        child.cleanup.assert_called_once()
        self.assertEqual(collector.records[('pod','unknown')]['uid'],'rollout-client')
        self.assertTrue(collector.created);self.assertEqual(collector.children,[])

    def test_namespace_foreign_workload_and_secret_are_metadata_only_and_block_delete(self):
        for kind in ('statefulsets.apps','secrets'):
            collector=self.collector();backend=Mock()
            collector.run_command=Mock(side_effect=[kind+'\n','foreign uid 3\n'])
            with self.subTest(kind=kind),self.assertRaises(m.Blocked):collector.namespace_inventory(backend)
            args=collector.run_command.call_args_list[1].args[1]
            self.assertTrue(any(x.startswith('-o=custom-columns=') for x in args))
            self.assertNotIn('json',args);backend.run.assert_not_called()

    def test_current_endpoint_requires_owned_single_ready_pod_and_exact_port(self):
        collector=self.collector();backend=Mock()
        pod={'metadata':{'uid':'pod'},'status':{'podIP':'192.0.2.8'},'spec':{}}
        svc={'metadata':{'uid':'service'},'spec':{'ports':[{'name':'http','port':8003}]}}
        ep={'metadata':{'uid':'endpoint','ownerReferences':[{'uid':'service','controller':True}]},'endpoints':[{'targetRef':{'uid':'pod'},'conditions':{'ready':True},'addresses':['192.0.2.8']}],'ports':[{'name':'http','port':8003,'protocol':'TCP'}]}
        collector.api=Mock(return_value={'items':[ep]})
        first=collector.endpoint(backend,'book-memo',pod,svc);self.assertEqual(len(first),64)
        from copy import deepcopy
        for mutation in (lambda e:e['endpoints'][0]['targetRef'].update(uid='other'),lambda e:e['ports'][0].update(port=8000),lambda e:e['metadata']['ownerReferences'][0].update(uid='other')):
            wrong=deepcopy(ep);mutation(wrong);collector.api.return_value={'items':[wrong]}
            with self.assertRaises(m.Blocked):collector.endpoint(backend,'book-memo',pod,svc)

    def test_diagnostic_cannot_report_pass_after_protected_state_drift(self):
        collector=self.collector();backend=Mock();backend.guards.side_effect=['before','after'];backend.target_commit='d'*40
        node={'metadata':{'uid':'node','name':'n100'}}
        server={'metadata':{'uid':'server'},'spec':{'nodeName':'n100'},'status':{'podIP':'192.0.2.8'}}
        svc={'metadata':{'uid':'svc'},'spec':{'clusterIP':'192.0.2.9'}}
        collector.setup=Mock();collector.ready=Mock(return_value=server);collector.current=Mock(return_value=svc);collector.cleanup=Mock(return_value=True)
        collector.api=Mock(side_effect=lambda b,*a,**k: node if a[:2]==('get','node') else {'metadata':{'uid':'cluster'}} if a[:2]==('get','namespace') else {'items':[]})
        state={'phase':'baseline'}
        def create(b,obj):state['phase']='deny' if obj['metadata']['name']=='deny' else 'allow'
        def delete(b,kind,name):
            if name=='deny':state['phase']='baseline'
        collector.create=Mock(side_effect=create);collector.delete=Mock(side_effect=delete)
        def probe(b,source,*a,**k):return {'http_status':200 if state['phase']=='baseline' or state['phase']=='allow' and source=='caddy' else None}
        collector.probe=Mock(side_effect=probe)
        from unittest.mock import patch
        with patch.object(m.time,'sleep'),self.assertRaisesRegex(m.Blocked,'diagnostic_protected_state_drift'):
            collector._diagnostic(backend,'book-memo',{}, {'source_sha256':'e'*64}, {},node)
        self.assertFalse(any(call.args[0]=='isolated_port_verified' for call in collector.journal.call_args_list))

    def test_socket_http_request_requires_same_port_and_unchanged_endpoint(self):
        collector=self.collector();backend=Mock();identity={}
        pod={'metadata':{'name':'book','uid':'pod'},'status':{'podIP':'192.0.2.8'}}
        svc={'spec':{'clusterIP':'192.0.2.8'}}
        collector.caddy=Mock(return_value={'Id':'caddy','Config':{'Env':['BOOK_MEMO_UPSTREAM=192.0.2.8:8003']}})
        raw=FixedEvidenceTests().raw().replace(':A410',':C000')
        collector.target_sockets=Mock(side_effect=[set(),{('192.0.2.8',8003,'192.0.2.7',49152)}])
        collector.run_command=Mock(side_effect=['',raw]);collector.endpoint=Mock(side_effect=['a'*64,'b'*64])
        collector.target=Mock(return_value=({},pod,svc,{}));collector.identity=Mock(return_value={'current_identity':identity})
        from unittest.mock import patch
        with patch.object(m.secrets,'choice',return_value=49152),self.assertRaisesRegex(m.Blocked,'request_target_endpoint_changed'):
            collector.request(backend,'book-memo',pod,svc,identity)

    def test_unknown_namespace_create_stays_pending_when_readback_is_absent(self):
        collector=self.collector();backend=Mock()
        intent={'apiVersion':'v1','kind':'Namespace','metadata':{'name':collector.ns}}
        collector.current=Mock(return_value=None)
        collector.api=Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(m.Blocked,'disposable_create_unknown'):
            collector.create(backend,intent)
        with self.assertRaisesRegex(m.Blocked,'disposable_create_unknown'):
            collector.cleanup(backend)
        self.assertIn(('namespace',collector.ns),collector.pending)
        self.assertFalse(collector.created)
        self.assertEqual(collector.api.call_count,1)
        backend.run.assert_not_called()

    def test_late_owned_namespace_response_reconciles_exact_uid_without_retry(self):
        collector=self.collector();backend=Mock();name=collector.ns
        intent={'apiVersion':'v1','kind':'Namespace','metadata':{'name':name}}
        collector.current=Mock(return_value=None);collector.api=Mock(side_effect=TimeoutError())
        with self.assertRaises(m.Blocked):collector.create(backend,intent)
        actual={'kind':'Namespace','metadata':{'name':name,'uid':'late-owned','labels':{m.OWNER:collector.run}},'spec':{}}
        collector.current.return_value=actual
        collector.reconcile_pending(backend)
        self.assertEqual(collector.pending,{})
        self.assertTrue(collector.created)
        self.assertEqual(collector.records[('namespace',name)]['uid'],'late-owned')
        self.assertEqual(collector.api.call_count,1)

    def test_pending_pod_and_child_cannot_be_lost_on_cleanup_failure(self):
        collector=self.collector();backend=Mock();child=self.collector()
        intent={'apiVersion':'v1','kind':'Pod','metadata':{'name':'server','namespace':child.ns},'spec':{}}
        child.current=Mock(return_value=None);child.api=Mock(side_effect=TimeoutError())
        with self.assertRaises(m.Blocked):child.create(backend,intent)
        collector.children.append(child)
        with self.assertRaisesRegex(m.Blocked,'disposable_create_unknown'):collector.cleanup(backend)
        self.assertEqual(collector.children,[child])
        self.assertIn(('pod','server'),child.pending)
        self.assertEqual(child.api.call_count,1)

    def test_preexisting_resource_fails_before_pending_write(self):
        collector=self.collector();collector.current=Mock(return_value={'metadata':{}})
        collector.api=Mock()
        with self.assertRaisesRegex(m.Blocked,'disposable_preexists'):
            collector.create(Mock(),{'kind':'Namespace','metadata':{'name':collector.ns}})
        self.assertEqual(collector.pending,{})
        collector.api.assert_not_called()
