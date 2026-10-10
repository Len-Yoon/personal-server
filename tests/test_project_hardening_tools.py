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
