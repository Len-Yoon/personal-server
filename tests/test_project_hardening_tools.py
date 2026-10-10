import importlib.util
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'infra/k8s/tools'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class IngressOnlyPolicyTests(unittest.TestCase):
    def fixture(self, app='portal-web'):
        snapshot,_=HardeningToolsTests().hardening_fixture()
        snapshot['metadata']['name']=app
        snapshot['spec']['template']['metadata']['labels']['app.kubernetes.io/name']=app
        snapshot['spec']['template']['spec']['containers'][0]['name']=app
        module=load('prepare-ingress-networkpolicy');stamp=datetime.now(timezone.utc).isoformat()
        sha=module.digest(snapshot['spec'])
        inventory={'deployment_uid':'opaque','resource_version':'3','image':snapshot['spec']['template']['spec']['containers'][0]['image'],'spec_sha256':sha,'observed_at':stamp,'cluster_uid':'cluster-opaque','flow_review_complete':True,'existing_policies':[], 'acknowledge_node_exception':True,'enforcement':{'cluster_uid':'cluster-opaque','observed_at':stamp,**{key:True for key in ('baseline','deny','selective_allow','unknown_pod_denied','cleanup','production_unchanged')}},'caddy':{'observed_at':stamp,'status':200,'controlled_request':True,'post_nat_observed':True,'source_kind':'node','source_matches_node_interface':True},'flows':[],'not_applicable':{}}
        categories=['metrics'] if app=='portal-web' else ['fanout','metrics'] if app=='crawler-worker' else ['fanout']
        for category in categories:
            namespace='monitoring' if category=='metrics' else 'personal-server'
            labels={'app.kubernetes.io/name':'prometheus' if category=='metrics' else 'portal-web'}
            peer={'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}},'podSelector':{'matchLabels':labels}}
            inventory['flows'].append({'category':category,'peer':peer,'port':module.PORTS[app],'proof':{'observed_at':stamp,'status':200,'peer_sha256':module.digest(peer),'target_spec_sha256':sha,'source_kind':'pod','source_pod_uid':'source-opaque','source_namespace':namespace,'source_labels':deepcopy(labels),'post_nat_source_matches_pod':True}})
        inventory['not_applicable']={category:'No active flow in reviewed live inventory' for category in {'metrics','fanout'}-set(categories)}
        inventory['caddy']['target_spec_sha256']=sha
        inventory.update(target_pod_uid='target-pod-opaque',target_node_uid='target-node-opaque')
        inventory['caddy']['target_pod_uid']='target-pod-opaque'
        inventory['caddy']['node_exception_proof']={'observed_at':stamp,'target_pod_uid':'target-pod-opaque','source_node_uid':'target-node-opaque','source_matches_target_node_address':True}
        return module,snapshot,inventory

    def test_ingress_only_preserves_inputs_and_explicitly_reports_node_limit(self):
        for app in ('portal-web','book-memo','youtube-memo','crawler-worker'):
            with self.subTest(app=app):
                module,snapshot,inventory=self.fixture(app);before=deepcopy((snapshot,inventory))
                result=module.prepare(snapshot,inventory)
                self.assertEqual((snapshot,inventory),before)
                self.assertEqual(result['policy']['spec']['policyTypes'],['Ingress'])
                self.assertNotIn('egress',result['policy']['spec'])
                self.assertTrue(result['node_traffic_unrestricted'])
                self.assertFalse(result['caddy_only_isolation'])
                self.assertEqual(result['policy']['metadata']['namespace'],'personal-server')
                self.assertEqual(len(result['policy']['spec']['ingress']),len(inventory['flows']))

    def test_ingress_rejects_unbound_stale_unproven_and_union_evidence(self):
        module,snapshot,inventory=self.fixture()
        mutations=[lambda x:x.update(deployment_uid='different'),lambda x:x.update(resource_version='different'),lambda x:x.update(image='other@sha256:'+'b'*64),lambda x:x.update(spec_sha256='b'*64),lambda x:x.update(observed_at=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()),lambda x:x.update(observed_at=datetime.now().isoformat()),lambda x:x.update(existing_policies=[{}]),lambda x:x.update(flow_review_complete=False),lambda x:x.update(acknowledge_node_exception=False),lambda x:x['caddy'].update(post_nat_observed=False),lambda x:x['caddy'].update(status=503),lambda x:x['caddy'].update(source_matches_node_interface=False),lambda x:x['enforcement'].update(unknown_pod_denied=False),lambda x:x['enforcement'].update(cleanup=False),lambda x:x['enforcement'].update(cluster_uid='other'),lambda x:x['flows'][0]['proof'].update(source_pod_uid=''),lambda x:x['flows'][0]['proof'].update(post_nat_source_matches_pod=False),lambda x:x['flows'][0]['proof'].update(target_spec_sha256='b'*64),lambda x:x['flows'][0]['proof'].update(peer_sha256='b'*64),lambda x:x['flows'][0]['proof'].update(source_labels={}),lambda x:x['flows'][0]['proof'].update(status=401),lambda x:x['flows'][0].update(port=443),lambda x:x['flows'][0].update(category='external'),lambda x:x['flows'][0].update(peer={'podSelector':{}})]
        for mutate in mutations:
            invalid=deepcopy(inventory);mutate(invalid)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_ingress_requires_current_metrics_and_downstream_direct_portal(self):
        for app,category in [('portal-web','metrics'),('crawler-worker','metrics'),('book-memo','fanout'),('youtube-memo','fanout')]:
            module,snapshot,inventory=self.fixture(app)
            inventory['flows']=[f for f in inventory['flows'] if f['category']!=category]
            inventory['not_applicable'][category]='Host alias bypass is not pod proof'
            with self.assertRaises(ValueError):module.prepare(snapshot,inventory)

    def test_ingress_rejects_broad_monitoring_label_and_other_target_caddy_proof(self):
        module,snapshot,inventory=self.fixture()
        inventory['caddy']['target_spec_sha256']='b'*64
        with self.assertRaises(ValueError):module.prepare(snapshot,inventory)
        inventory['caddy']['target_spec_sha256']=inventory['spec_sha256']
        flow=inventory['flows'][0];labels={'app.kubernetes.io/part-of':'monitoring'}
        flow['peer']['podSelector']['matchLabels']=labels
        flow['proof'].update(source_labels=labels,peer_sha256=module.digest(flow['peer']))
        with self.assertRaises(ValueError):module.prepare(snapshot,inventory)

    def test_exact_caddy_peer_is_single_non_node_address_and_bound(self):
        module,snapshot,inventory=self.fixture();peer={'ipBlock':{'cidr':'192.0.2.7/32'}}
        inventory['caddy'].update(source_kind='exact_ip',peer=peer,post_nat_peer_sha256=module.digest(peer),source_is_node=False)
        result=module.prepare(snapshot,inventory)
        self.assertEqual(result['policy']['spec']['ingress'][0]['from'],[peer])
        for cidr in ('0.0.0.0/0','192.0.2.0/24','::/0','::1/128','ff02::1/128'):
            invalid=deepcopy(inventory);invalid['caddy']['peer']={'ipBlock':{'cidr':cidr}};invalid['caddy']['post_nat_peer_sha256']=module.digest(invalid['caddy']['peer'])
            with self.subTest(cidr=cidr):
                with self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        inventory['caddy']['source_is_node']=True
        with self.assertRaises(ValueError):module.prepare(snapshot,inventory)

    def test_node_exception_requires_fresh_exact_target_node_and_pod_proof(self):
        module,snapshot,inventory=self.fixture()
        mutations=[
            lambda x:x['caddy'].pop('node_exception_proof'),
            lambda x:x['caddy']['node_exception_proof'].update(source_matches_target_node_address=False),
            lambda x:x['caddy']['node_exception_proof'].update(source_node_uid='other-node'),
            lambda x:x['caddy']['node_exception_proof'].update(target_pod_uid='other-pod'),
            lambda x:x['caddy']['node_exception_proof'].update(observed_at=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()),
            lambda x:x['caddy']['node_exception_proof'].update(observed_at=datetime.now().isoformat()),
            lambda x:x.update(target_node_uid=''),
            lambda x:x.update(target_node_uid=None),
        ]
        for mutate in mutations:
            invalid=deepcopy(inventory);mutate(invalid)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_every_caddy_proof_is_bound_to_current_target_pod(self):
        for kind in ('node','exact_ip'):
            module,snapshot,inventory=self.fixture()
            if kind=='exact_ip':
                peer={'ipBlock':{'cidr':'192.0.2.7/32'}}
                inventory['caddy'].update(source_kind=kind,peer=peer,post_nat_peer_sha256=module.digest(peer),source_is_node=False)
            for value in ('other-pod','',None):
                invalid=deepcopy(inventory);invalid['caddy']['target_pod_uid']=value
                with self.subTest(kind=kind,value=value):
                    with self.assertRaises(ValueError):module.prepare(snapshot,invalid)
            invalid=deepcopy(inventory);invalid.pop('target_pod_uid')
            with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_ingress_cli_never_emits_policy_peer_or_snapshot_values(self):
        module,snapshot,inventory=self.fixture();peer={'ipBlock':{'cidr':'192.0.2.7/32'}}
        inventory['caddy'].update(source_kind='exact_ip',peer=peer,post_nat_peer_sha256=module.digest(peer),source_is_node=False)
        snapshot['spec']['template']['spec']['containers'][0]['env']=[{'name':'PRIVATE_TOKEN','value':'private-fixture-never-echo'}]
        inventory['spec_sha256']=module.digest(snapshot['spec'])
        inventory['caddy']['target_spec_sha256']=inventory['spec_sha256']
        inventory['flows'][0]['proof']['target_spec_sha256']=inventory['spec_sha256']
        with tempfile.TemporaryDirectory() as temp:
            s=Path(temp)/'snapshot.json';i=Path(temp)/'inventory.json';s.write_text(json.dumps(snapshot));i.write_text(json.dumps(inventory))
            command=['python3',str(ROOT/'infra/k8s/tools/prepare-ingress-networkpolicy.py'),'--snapshot',str(s),'--inventory',str(i)]
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['validation'],'PASS')
            for private in ('192.0.2.7','private-fixture-never-echo','opaque'):
                self.assertNotIn(private,result.stdout+result.stderr)
            inventory['caddy']['peer']['ipBlock']['cidr']='private-fixture-never-echo';i.write_text(json.dumps(inventory))
            result=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stderr,'ingress_inventory_validation=FAIL\n')

class PreNatIngressCandidateTests(unittest.TestCase):
    fixture = IngressOnlyPolicyTests.fixture
    def pre_fixture(self):
        module,snapshot,inventory=self.fixture('book-memo');stamp=inventory['observed_at'];peer={'ipBlock':{'cidr':'192.0.2.7/32'}}
        identity={'container_id_sha256':'a'*64,'container_image_sha256':'sha256:'+'b'*64,'started_at':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),'network_settings_sha256':'c'*64}
        proof={'current_identity_sha256':module.digest(identity),'source_sha256':module.digest('192.0.2.7'),'peer_sha256':module.digest(peer),'source_interface':'caddy_container_endpoint','node_uid':inventory['target_node_uid'],'route':'ClusterIP','port':8003,**{k:True for k in ('same_connection_bound','pid_start_inode_bound','http_marker_bound','source_matches_current_endpoint')}}
        inventory['caddy']={'observed_at':stamp,'status':200,'controlled_request':True,'source_kind':'pre_nat_exact_ip','target_spec_sha256':inventory['spec_sha256'],'target_pod_uid':inventory['target_pod_uid'],'peer':peer,'pre_nat_peer_sha256':module.digest(peer),'source_is_node':False,'current_identity':identity,'pre_nat_proof':proof,'endpoint_stability':'dynamic'}
        result={'status':'PASS','source_kind':'pre_nat_exact_ip','run_id':'abcdef123456','target':'d'*40,'completed_at':stamp,'reason':'isolated_exact_ip_causal_admission_proven','production_app_count':4,'production_policy_count':0,
                **{k:True for k in ('owned_cleanup_complete','restored_flow_required','restored_flow_proven','production_postguard_pass','resource_create_attempted')},
                **{k:False for k in ('caddy_only_identity_isolation','long_term_stable_ip_proven','policy_hook_directly_observed','production_apply_authorized_by_result','production_identity_proven','caddy_static_ipv4_configured','raw_address_output','raw_address_persisted')},
                **{k:None for k in ('first_failure_sha256','cleanup_failure_reason','postguard_failure_reason')}}
        diagnostic={'cluster_uid':inventory['cluster_uid'],'node_uid':inventory['target_node_uid'],'run_id':result['run_id'],'tool_sha256':'e'*64,'result':result,'result_sha256':module.digest(result),'current_identity_sha256':module.digest(identity),'peer_sha256':module.digest(peer),'source_sha256':proof['source_sha256'],'server_pod_uid':'isolated-server-pod','service_uid':'isolated-service','route':'ClusterIP','port':8003,'phases':{k:True for k in ('baseline','caddy_denied','exact_ip_allow','unknown_pod_denied','unknown_service_denied')}}
        inventory['enforcement']={'cluster_uid':inventory['cluster_uid'],'pre_nat_diagnostic':diagnostic}
        return module,snapshot,inventory

    def test_pre_nat_dynamic_endpoint_is_offline_candidate_with_no_production_authority(self):
        module,snapshot,inventory=self.pre_fixture();before=deepcopy((snapshot,inventory));result=module.prepare(snapshot,inventory)
        self.assertEqual(before,(snapshot,inventory));self.assertEqual(result['caddy_source_kind'],'pre_nat_exact_ip')
        self.assertEqual(result['policy']['spec']['ingress'][0]['from'],[inventory['caddy']['peer']])
        self.assertTrue(result['offline_candidate_only']);self.assertFalse(result['operating_ready']);self.assertFalse(result['production_apply_authorized'])
        self.assertEqual(result['stop_reasons'],['dynamic_caddy_endpoint_not_durable_identity'])
        for field in ('policy_hook_directly_observed','long_term_stable_ip_proven','production_identity_proven','production_apply_succeeded','production_route_proven','caddy_only_isolation'):self.assertFalse(result[field])

    def test_pre_nat_source_identity_peer_interface_target_route_and_unknown_fields_fail_closed(self):
        module,snapshot,inventory=self.pre_fixture()
        mutations=[lambda x:x['caddy'].update(post_nat_observed=True),lambda x:x['caddy'].update(endpoint_stability='static'),lambda x:x['caddy'].update(source_kind='unknown'),lambda x:x['caddy'].update(source_is_node=True),lambda x:x['caddy'].update(pre_nat_peer_sha256='0'*64),lambda x:x['caddy'].update(target_pod_uid='isolated-server-pod'),lambda x:x['caddy'].update(target_spec_sha256='0'*64),lambda x:x['caddy']['current_identity'].update(container_id_sha256='invalid'),lambda x:x['caddy']['current_identity'].update(container_image_sha256='other'),lambda x:x['caddy']['current_identity'].update(started_at=datetime.now().isoformat()),lambda x:x['caddy']['current_identity'].update(network_settings_sha256='0'*64),lambda x:x['caddy']['pre_nat_proof'].update(source_interface='node_interface'),lambda x:x['caddy']['pre_nat_proof'].update(source_sha256='0'*64),lambda x:x['caddy']['pre_nat_proof'].update(peer_sha256='0'*64),lambda x:x['caddy']['pre_nat_proof'].update(current_identity_sha256='0'*64),lambda x:x['caddy']['pre_nat_proof'].update(node_uid='wrong-node'),lambda x:x['caddy']['pre_nat_proof'].update(route='NodePort'),lambda x:x['caddy']['pre_nat_proof'].update(port=8000),lambda x:x['caddy']['pre_nat_proof'].update(same_connection_bound=False),lambda x:x['caddy']['pre_nat_proof'].update(http_marker_bound=False),lambda x:x['caddy']['pre_nat_proof'].update(pid_start_inode_bound=False),lambda x:x['caddy']['pre_nat_proof'].update(source_matches_current_endpoint=False),lambda x:x['caddy']['pre_nat_proof'].update(unknown=True)]
        for mutate in mutations:
            invalid=deepcopy(inventory);mutate(invalid)
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for cidr in ('192.0.2.0/24','::1/128','2001:db8::1/128','169.254.1.1/32','0.0.0.0/32'):
            invalid=deepcopy(inventory);invalid['caddy']['peer']={'ipBlock':{'cidr':cidr}}
            with self.subTest(cidr=cidr),self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_pre_nat_diagnostic_must_bind_success_cleanup_unknowns_and_distinct_isolated_target(self):
        module,snapshot,inventory=self.pre_fixture();diag=inventory['enforcement']['pre_nat_diagnostic']
        mutations=[lambda d:d.update(cluster_uid='other'),lambda d:d.update(node_uid='other'),lambda d:d.update(run_id='other'),lambda d:d.update(tool_sha256='bad'),lambda d:d.update(result_sha256='0'*64),lambda d:d.update(current_identity_sha256='0'*64),lambda d:d.update(peer_sha256='0'*64),lambda d:d.update(source_sha256='0'*64),lambda d:d.update(server_pod_uid=inventory['target_pod_uid']),lambda d:d.update(service_uid=''),lambda d:d.update(route='NodePort'),lambda d:d.update(port=8002),lambda d:d.update(unknown=True)]
        for key in diag['phases']:mutations.append(lambda d,k=key:d['phases'].update({k:False}))
        for mutate in mutations:
            invalid=deepcopy(inventory);mutate(invalid['enforcement']['pre_nat_diagnostic'])
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for key in ('owned_cleanup_complete','restored_flow_proven','production_postguard_pass'):
            invalid=deepcopy(inventory);d=invalid['enforcement']['pre_nat_diagnostic'];d['result'][key]=False;d['result_sha256']=module.digest(d['result'])
            with self.subTest(result_flag=key),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for key in ('policy_hook_directly_observed','long_term_stable_ip_proven','production_identity_proven','production_apply_authorized_by_result'):
            invalid=deepcopy(inventory);d=invalid['enforcement']['pre_nat_diagnostic'];d['result'][key]=True;d['result_sha256']=module.digest(d['result'])
            with self.subTest(result_flag=key),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        invalid=deepcopy(inventory);d=invalid['enforcement']['pre_nat_diagnostic'];d['result']['completed_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat();d['result_sha256']=module.digest(d['result'])
        with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_pre_nat_current_socket_must_follow_diagnostic_and_flags_are_booleans(self):
        module,snapshot,inventory=self.pre_fixture()
        invalid=deepcopy(inventory);invalid['caddy']['observed_at']=(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()
        with self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for key in inventory['caddy']['pre_nat_proof']:
            if type(inventory['caddy']['pre_nat_proof'][key]) is bool:
                for value in (1,None):
                    invalid=deepcopy(inventory);invalid['caddy']['pre_nat_proof'][key]=value
                    with self.subTest(key=key,value=value),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for field in ('caddy','enforcement'):
            invalid=deepcopy(inventory);invalid[field]=[]
            with self.subTest(field=field),self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        for field in ('current_identity','pre_nat_proof'):
            invalid=deepcopy(inventory);invalid['caddy'][field]=None
            with self.subTest(field=field),self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_pre_nat_isolation_cannot_be_relabelled_as_portal_or_another_port(self):
        for app in ('portal-web','youtube-memo','crawler-worker'):
            module,snapshot,inventory=self.pre_fixture();snapshot['metadata']['name']=app;snapshot['spec']['template']['metadata']['labels']['app.kubernetes.io/name']=app;snapshot['spec']['template']['spec']['containers'][0]['name']=app
            inventory['spec_sha256']=module.digest(snapshot['spec']);inventory['caddy']['target_spec_sha256']=inventory['spec_sha256']
            with self.subTest(app=app),self.assertRaises(ValueError):module.prepare(snapshot,inventory)

    def test_pre_nat_cli_is_offline_only_and_never_emits_peer_or_identity_values(self):
        module,snapshot,inventory=self.pre_fixture()
        with tempfile.TemporaryDirectory() as temp:
            s=Path(temp)/'snapshot.json';i=Path(temp)/'inventory.json';s.write_text(json.dumps(snapshot));i.write_text(json.dumps(inventory))
            result=subprocess.run(['python3',str(ROOT/'infra/k8s/tools/prepare-ingress-networkpolicy.py'),'--snapshot',str(s),'--inventory',str(i)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);out=json.loads(result.stdout);self.assertTrue(out['offline_validation_only']);self.assertFalse(out['operating_ready']);self.assertFalse(out['production_apply_authorized'])
            for value in ('192.0.2.7','isolated-server-pod','target-node-opaque','sha256:'+('b'*64)):self.assertNotIn(value,result.stdout+result.stderr)
            description=subprocess.run(['python3',str(ROOT/'infra/k8s/tools/prepare-ingress-networkpolicy.py'),'--describe-input'],capture_output=True,text=True)
            self.assertIn('pre_nat_exact_ip',description.stdout);self.assertIn('operating_ready',description.stdout)


class HardeningToolsTests(unittest.TestCase):
    def hardening_fixture(self):
        image='same@sha256:'+'a'*64
        snapshot={'kind':'Deployment','metadata':{'name':'portal-web','namespace':'personal-server','uid':'opaque','resourceVersion':'3'},'spec':{'strategy':{'type':'Recreate'},'replicas':1,'template':{'metadata':{'labels':{'app.kubernetes.io/name':'portal-web'}},'spec':{'securityContext':{'runAsNonRoot':True,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':'portal-web','image':image,'resources':{'requests':{'cpu':'75m'}},'readinessProbe':{'httpGet':{'path':'/health','port':8000}},'volumeMounts':[{'name':'data','mountPath':'/data'}, {'name':'tmp','mountPath':'/tmp'}]}], 'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'portal-data'}},{'name':'tmp','emptyDir':{}}]}}}}
        evidence={'deployment_uid':'opaque','resource_version':'3','image':image,'observed_at':datetime.now(timezone.utc).isoformat(),'ready':{'path':'/ready','status':200},'read_only_rootfs_verified':True,'baseline':{'duration_seconds':86400,'samples':288,'memory_p95_bytes':64*1024**2,'memory_peak_bytes':100*1024**2,'cpu_p95_millicores':40},'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'memory':'256Mi'}}}
        return snapshot,evidence

    def test_guarded_patch_preserves_images_volumes_and_existing_resources(self):
        snapshot,evidence=self.hardening_fixture()
        before=json.dumps(snapshot,sort_keys=True)
        result=load('prepare-app-hardening').prepare(snapshot,evidence)
        self.assertEqual(json.dumps(snapshot,sort_keys=True),before)
        self.assertEqual([p['op'] for p in result['patch'][:3]],['test']*3)
        self.assertFalse(any('/image' in p['path'] or '/volume' in p['path'] or p['path'].endswith('/requests/cpu') for p in result['patch'] if p['op']!='test'))
        self.assertTrue(result['approval_required'])
        snapshot['metadata']['namespace']='monitoring'
        with self.assertRaises(ValueError):load('prepare-app-hardening').prepare(snapshot,evidence)

    def test_hardening_requires_current_image_bound_readiness_and_baseline(self):
        module=load('prepare-app-hardening')
        snapshot,evidence=self.hardening_fixture()
        with self.assertRaises(ValueError):module.prepare(snapshot)
        for key,value in [('deployment_uid','other'),('resource_version','2'),('image','other@sha256:'+'b'*64),('read_only_rootfs_verified',False),('observed_at',(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()),('ready',{'path':'/health','status':200}),('baseline',{'duration_seconds':60,'samples':1})]:
            with self.subTest(key=key):
                invalid=deepcopy(evidence);invalid[key]=value
                with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_hardening_rejects_unsafe_resource_decisions_and_missing_probe(self):
        module=load('prepare-app-hardening')
        snapshot,evidence=self.hardening_fixture()
        for resources in [{'requests':{'cpu':'1m','memory':'128Mi'},'limits':{'memory':'256Mi'}},{'requests':{'cpu':'50m','memory':'32Mi'},'limits':{'memory':'256Mi'}},{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'memory':'128Mi'}},{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'memory':'0Mi'}},{'requests':{'cpu':'NaN','memory':'128Mi'},'limits':{'memory':'256Mi'}}]:
            invalid=deepcopy(evidence);invalid['resources']=resources
            with self.assertRaises(ValueError):module.prepare(snapshot,invalid)
        del snapshot['spec']['template']['spec']['containers'][0]['readinessProbe']
        with self.assertRaises(ValueError):module.prepare(snapshot,evidence)

    def test_hardening_uses_explicit_resources_preserves_security_and_does_not_echo_env(self):
        snapshot,evidence=self.hardening_fixture();container=snapshot['spec']['template']['spec']['containers'][0]
        container['securityContext']={'runAsUser':10001,'allowPrivilegeEscalation':False}
        container['env']=[{'name':'PRIVATE_TOKEN','value':'fixture-private-do-not-echo'}]
        result=load('prepare-app-hardening').prepare(snapshot,evidence)
        changes=[p for p in result['patch'] if p['op']!='test']
        self.assertIn({'op':'test','path':'/spec/template/spec/containers/0/image','value':container['image']},result['patch'])
        self.assertNotIn('fixture-private-do-not-echo',json.dumps(result))
        self.assertIn({'op':'add','path':'/spec/template/spec/containers/0/resources/limits','value':{'memory':'256Mi'}},changes)
        self.assertFalse(any(p['path'].endswith('/runAsUser') for p in changes))
        self.assertTrue(any(p['value']=='/ready' for p in changes))
        container['securityContext']['allowPrivilegeEscalation']=True
        with self.assertRaises(ValueError):load('prepare-app-hardening').prepare(snapshot,evidence)

    def test_hardening_rejects_writer_security_image_and_existing_resource_conflicts(self):
        module=load('prepare-app-hardening');snapshot,evidence=self.hardening_fixture()
        for mutate in [lambda x:x['spec'].update(strategy={'type':'RollingUpdate'}),lambda x:x['spec'].update(replicas=2),lambda x:x['spec']['template']['spec'].update(securityContext={}),lambda x:x['spec']['template']['spec']['containers'][0].update(securityContext={'privileged':True}),lambda x:x['spec']['template']['spec']['containers'][0].update(image='mutable:latest'),lambda x:x['spec']['template']['spec']['containers'][0].update(resources={'limits':{'memory':'100Mi'}}),lambda x:x['spec']['template']['spec']['containers'][0].update(volumeMounts=[]),lambda x:x['spec']['template']['spec'].update(hostNetwork=True)]:
            invalid=deepcopy(snapshot);mutate(invalid)
            with self.assertRaises(ValueError):module.prepare(invalid,evidence)

    def test_hardening_rejects_container_overrides_of_safe_pod_security(self):
        module=load('prepare-app-hardening');snapshot,evidence=self.hardening_fixture()
        for override in [{'runAsNonRoot':False}, {'runAsUser':0}, {'seccompProfile':{'type':'Unconfined'}}, {'procMount':'Unmasked'}, {'runAsNonRoot':False,'runAsUser':0,'seccompProfile':{'type':'Unconfined'}}]:
            with self.subTest(override=override):
                invalid=deepcopy(snapshot)
                invalid['spec']['template']['spec']['containers'][0]['securityContext']=override
                with self.assertRaises(ValueError):module.prepare(invalid,evidence)
        snapshot['spec']['template']['spec']['containers'][0]['securityContext']={'runAsNonRoot':True,'runAsUser':10001,'seccompProfile':{'type':'RuntimeDefault'}}
        module.prepare(snapshot,evidence)

    def test_hardening_generates_executable_json_patch_with_image_and_data_unchanged(self):
        snapshot,evidence=self.hardening_fixture()
        result=load('prepare-app-hardening').prepare(snapshot,evidence)
        patched=deepcopy(snapshot)
        for operation in result['patch']:
            keys=operation['path'].strip('/').split('/')
            parent=patched
            for key in keys[:-1]:parent=parent[int(key)] if isinstance(parent,list) else parent[key]
            key=int(keys[-1]) if isinstance(parent,list) else keys[-1]
            if operation['op']=='test':self.assertEqual(parent[key],operation['value'])
            else:parent[key]=deepcopy(operation['value'])
        pod=patched['spec']['template']['spec'];container=pod['containers'][0]
        self.assertEqual(container['readinessProbe']['httpGet'],{'path':'/ready','port':8000})
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(container['resources']['requests']['cpu'],'75m')
        self.assertEqual(container['image'],snapshot['spec']['template']['spec']['containers'][0]['image'])
        self.assertEqual(pod['volumes'],snapshot['spec']['template']['spec']['volumes'])

    def network_fixture(self):
        snapshot,_=self.hardening_fixture()
        peer={'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'monitoring'}},'podSelector':{'matchLabels':{'app.kubernetes.io/name':'prometheus'}}}
        rule=lambda direction,category,peer,port:{'direction':direction,'category':category,'peer':peer,'ports':[{'protocol':'TCP','port':port}],'evidence':'observed in reviewed read-only inventory'}
        inventory={'deployment_uid':'opaque','resource_version':'3','observed_at':datetime.now(timezone.utc).isoformat(),'enforcement_smoke_passed':True,'existing_policies':[], 'flow_review_complete':True,'not_applicable':{'fanout':'service has no backend fanout in fixture','backups':'PVC backups are separate pods and never connect to app'},'flows':[rule('Ingress','caddy',{'ipBlock':{'cidr':'192.0.2.4/32'}},8000),rule('Ingress','metrics',peer,8000),rule('Egress','external',{'ipBlock':{'cidr':'198.51.100.0/24'}},443), {'direction':'Egress','category':'dns','peer':{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}},'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}],'evidence':'reviewed resolver endpoints'}]}
        return snapshot,inventory

    def test_network_policy_is_app_scoped_and_includes_all_reviewed_flows(self):
        snapshot,inventory=self.network_fixture();before=deepcopy(inventory)
        result=load('prepare-networkpolicy').prepare(snapshot,inventory)
        self.assertEqual(inventory,before)
        policy=result['policy']
        self.assertEqual(policy['spec']['podSelector'],{'matchLabels':{'app.kubernetes.io/name':'portal-web'}})
        self.assertEqual(len(policy['spec']['ingress']),2)
        self.assertEqual(len(policy['spec']['egress']),2)
        self.assertTrue(result['approval_required'])
        self.assertNotIn('default-deny',json.dumps(policy))

    def test_network_policy_fails_closed_on_missing_flows_broad_peers_or_policy_overlap(self):
        module=load('prepare-networkpolicy');snapshot,inventory=self.network_fixture()
        for mutate in [lambda x:x.update(flow_review_complete=False),lambda x:x.update(enforcement_smoke_passed=False),lambda x:x.update(deployment_uid='other'),lambda x:x.update(existing_policies=[{'metadata':{'name':'unknown'}}]),lambda x:x.update(not_applicable={}),lambda x:x['flows'][0].update(peer={'ipBlock':{'cidr':'0.0.0.0/0'}}),lambda x:x['flows'][0].update(peer={'podSelector':{}}),lambda x:x['flows'][0].update(ports=[]),lambda x:x['flows'][0].update(evidence=''),lambda x:x['flows'][-1]['ports'].pop()]:
            invalid=deepcopy(inventory);mutate(invalid)
            with self.assertRaises(ValueError):module.prepare(snapshot,invalid)

    def test_exact_git_context_excludes_secrets_data_and_working_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            def git(*args):return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True).stdout.strip()
            git('init');git('config','user.email','test@example.invalid');git('config','user.name','Fixture')
            app=root/'book-memo';(app/'app').mkdir(parents=True)
            (app/'Dockerfile').write_text('FROM scratch\nCOPY app /app\n')
            (app/'requirements.txt').write_text('')
            (app/'app/main.py').write_text('original')
            (app/'app/logs').mkdir()
            (app/'app/logs/.gitkeep').write_bytes(b'\n')
            (app/'data').mkdir();(app/'data/private.sqlite3').write_text('private')
            (app/'.env').write_text('SECRET=private')
            git('add','.');git('commit','-m','fixture');revision=git('rev-parse','HEAD')
            (app/'app/main.py').write_text('working')
            target=Path(temp)/'context';target.mkdir()
            load('build-verified-image').build_context(root,'book-memo',revision,target)
            self.assertEqual((target/'app/main.py').read_text(),'original')
            self.assertFalse((target/'app/logs/.gitkeep').exists())
            self.assertFalse((target/'.env').exists());self.assertFalse((target/'data').exists())
            with self.assertRaises(ValueError):load('build-verified-image').build_context(root,'book-memo','main',target)
            (app/'app/logs/.gitkeep').write_text('private content')
            git('add','.');git('commit','-m','nonempty placeholder')
            with self.assertRaises(ValueError):load('build-verified-image').build_context(root,'book-memo',git('rev-parse','HEAD'),target)

    def test_publish_never_overwrites_and_removes_partial_pair(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);archive=root/'source.tar';archive.write_bytes(b'complete image')
            output=root/'image.tar';module=load('build-verified-image')
            sidecar=root/'image.tar.json';sidecar.write_text('existing')
            with self.assertRaises(FileExistsError):module.publish_archive(archive,output,{'scan':'passed'})
            self.assertFalse(output.exists());self.assertEqual(sidecar.read_text(),'existing')
            sidecar.unlink()
            original=module.os.link
            calls=[]
            def race(source,target):
                calls.append(target)
                if len(calls)==2:raise FileExistsError('race')
                return original(source,target)
            with patch.object(module.os,'link',side_effect=race):
                with self.assertRaises(FileExistsError):module.publish_archive(archive,output,{'scan':'passed'})
            self.assertFalse(output.exists());self.assertFalse(sidecar.exists())
            module.publish_archive(archive,output,{'scan':'passed'})
            self.assertEqual(output.read_bytes(),archive.read_bytes())
            self.assertEqual(json.loads(sidecar.read_text())['scan'],'passed')

    def test_runtime_backup_validation_matches_authoritative_contract(self):
        import ast
        authoritative=ast.parse((ROOT/'infra/k8s/tools/validate-backup-evidence.py').read_text())
        runtime=ast.parse((ROOT/'system-agent/app/services/backup_evidence.py').read_text())
        names={'parse_timestamp','parse_evidence','validate_evidence'}
        functions=lambda tree:{n.name:ast.dump(n,include_attributes=False) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names}
        self.assertEqual(functions(authoritative),functions(runtime))

    def test_build_exports_oci_annotation_and_requires_scan_before_publish(self):
        from argparse import Namespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            module=load('build-verified-image');output=Path(temp)/'app.oci.tar'
            args=Namespace(app='book-memo',revision='a'*40,output=output)
            calls=[]
            def runner(command,check):
                self.assertTrue(check);calls.append(command)
                if command[0]=='docker':
                    target=command[command.index('--output')+1]
                    self.assertTrue(target.startswith('type=oci,dest='))
                    self.assertIn('annotation-manifest-descriptor.io.personal-server.image-ref=personal-server-book-memo:',target)
                    import io, tarfile
                    with tarfile.open(target.split('dest=',1)[1].split(',',1)[0], 'w') as archive:
                        for name, raw in [('oci-layout', b'{"imageLayoutVersion":"1.0.0"}'), ('index.json', b'{"manifests":[]}')]:
                            member=tarfile.TarInfo(name);member.size=len(raw)
                            archive.addfile(member, io.BytesIO(raw))
                else:
                    self.assertEqual(command[:3],['trivy','image','--input'])
                    self.assertTrue(Path(command[3]).is_dir())
                    self.assertTrue((Path(command[3])/'oci-layout').is_file())
                    self.assertIn('--exit-code',command)
                    raise subprocess.CalledProcessError(1,command)
            with patch.object(module.argparse.ArgumentParser,'parse_args',return_value=args), patch.object(module,'build_context'), patch.object(module.subprocess,'run',side_effect=runner):
                with self.assertRaises(subprocess.CalledProcessError):module.main()
            self.assertEqual(len(calls),2)
            self.assertFalse(output.exists());self.assertFalse(output.with_suffix(output.suffix+'.json').exists())

    def test_oci_scan_rejects_traversal_and_links_before_scanner(self):
        import io, tarfile
        from unittest.mock import patch
        for name, link in [('safe/../../escape', False), ('blobs/link', True), ('duplicate', False)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);archive_path=root/'image.tar'
                with tarfile.open(archive_path, 'w') as archive:
                    member=tarfile.TarInfo(name)
                    if link:
                        member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';archive.addfile(member)
                    else:
                        member.size=1;archive.addfile(member, io.BytesIO(b'x'))
                        if name=='duplicate':archive.addfile(member, io.BytesIO(b'y'))
                module=load('build-verified-image')
                with patch.object(module.subprocess, 'run') as runner:
                    with self.assertRaises(ValueError):module.scan_archive(archive_path,root/'oci')
                    runner.assert_not_called()
