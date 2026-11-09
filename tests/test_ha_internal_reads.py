"""Fixed internal routes and network-isolated Portal behavior; no live service calls."""
import asyncio
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from tests._test_support import prepare_service_import
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress.internal_provider import InternalGet,TARGETS
from ha_egress.providers import ProviderUnavailable


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.reader=InternalGet({k:{'url':'https://'+v+'.ha.internal:10443','server_sha256':'a'*64}
            for k,v in TARGETS.items()},context=object(),node='A')

    def test_only_fixed_get_and_bound_target_node_are_sent(self):
        response=io.BytesIO(b'{"results":[]}')
        with patch('ha_egress.internal_provider.BoundRequestTransport') as transport:
            transport.return_value.return_value=response
            result=self.reader({'target':'books','path':'/api/search','query':{'q':'a b','limit':5}},timeout=1,deadline=time.monotonic()+1)
        request=transport.return_value.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).hostname,'book-memo.ha.internal')
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),{'q':['a b'],'limit':['5']})
        self.assertEqual(request.get_method(),'GET');self.assertEqual(request.get_header('X-ha-target-node'),'A')
        self.assertEqual(result.result,{'value':{'results':[]}})

    def test_arbitrary_url_path_mutation_and_oversized_query_are_never_sent(self):
        for payload in ({'target':'https://other','path':'/health','query':{}},
            {'target':'books','path':'/api/delete','query':{}},
            {'target':'system','path':'/metrics','query':{'secret':'x'}},
            {'target':'books','path':'/api/search','query':{'q':'x','limit':True}},
            {'target':'books','path':'/api/search','query':{'q':'x'*513,'limit':5}}):
            with patch('ha_egress.internal_provider.BoundRequestTransport') as transport:
                with self.assertRaises(ProviderUnavailable):self.reader(payload,timeout=1,deadline=time.monotonic()+1)
                transport.assert_not_called()


class PortalTest(unittest.TestCase):
    def setUp(self):
        prepare_service_import('portal-web')
        from app.services import global_search,system_status,ha_reads
        self.search,self.status,self.reads=global_search,system_status,ha_reads

    def test_search_uses_broker_with_existing_parallel_result_contract(self):
        with patch.dict('os.environ',{'HA_PG_CONFIG':'fixture'}),patch.object(self.reads,'enabled',return_value=True), \
             patch.object(self.reads,'read',return_value={'results':[{'title':'fixture','url':'/memos/1'}]}) as read, \
             patch.object(self.search.httpx,'AsyncClient',side_effect=AssertionError('raw network forbidden')):
            result=asyncio.run(self.search.search_all('fixture'))
        self.assertEqual(set(result),{'news','youtube','books'})
        self.assertTrue(all(row['status']=='ok' for row in result.values()));self.assertEqual(read.call_count,3)

    def test_status_and_events_preserve_sanitization_without_raw_network(self):
        with patch.object(self.reads,'enabled',return_value=True),patch.object(self.reads,'read',return_value={'overall_status':'ok'}), \
             patch.object(self.status,'urlopen',side_effect=AssertionError('raw network forbidden')):
            self.assertEqual(self.status.get_dashboard_status(),{'overall_status':'ok'})
        events=[{key:'fixture' for key in self.status.RECOVERY_EVENT_FIELDS} | {'private':'discard'}]
        with patch.object(self.reads,'enabled',return_value=True),patch.object(self.reads,'read',return_value=events), \
             patch.object(self.status,'urlopen',side_effect=AssertionError('raw network forbidden')):
            self.assertNotIn('private',self.status.get_recovery_events()[0])
