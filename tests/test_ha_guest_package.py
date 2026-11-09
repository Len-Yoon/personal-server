"""Guest isolation and transition authority boundaries, without any deployment."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ha_guest_package',ROOT/'infra/k8s/ha-package/guest.py')
guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)


def fixture():
    return {'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'node':'A',
        'guest_ip':'192.0.2.10','pg_a':'192.0.2.10','pg_b':'192.0.2.11',
        'images':{name:'fixture.invalid/'+name+'@sha256:'+'c'*64 for name in (*guest.SERVICES,'ha-backup-utility','caddy')},
        'gateway_sha256':'d'*64,'broker_sha256':'e'*64,'docker_gid':999,
        'app_environment':{'car-care-worker':{'CAR_CARE_TELEGRAM_CHAT_ID':'fixture'},
            'homeops-executor':{'HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256':'f'*64,
                'HOMEOPS_DOCKER_MANAGED_SERVICES':'portal-web,book-memo'}}}


class GuestLayoutTest(unittest.TestCase):
    def test_seven_apps_have_only_fixed_capabilities_and_no_external_network(self):
        value=fixture();original=copy.deepcopy(value)
        compose,caddy,review=guest.render(value)
        self.assertEqual(value,original)
        self.assertEqual(set(compose['services']),set(guest.SERVICES)|{'ha-network','ha-broker','ha-ingress'})
        for name in guest.SERVICES:
            app=compose['services'][name]
            self.assertEqual(app['network_mode'],'none')
            self.assertTrue(app['read_only']);self.assertEqual(app['cap_drop'],['ALL'])
            self.assertEqual(app['user'],'10001:10001');self.assertEqual(app['group_add'],['10004'])
            self.assertNotIn('pid',app);self.assertNotIn('privileged',app);self.assertNotIn('ports',app)
            self.assertNotIn('docker.sock',str(app));self.assertNotIn('/broker',str(app))
            mounts=app['volumes'];self.assertEqual(len(mounts),3)
            self.assertEqual([m['read_only'] for m in mounts],[True,True,False])
            self.assertTrue(all(not m['bind']['create_host_path'] for m in mounts))
            self.assertEqual(app['extra_hosts']['ha-broker'],'127.0.0.4')
            self.assertIn('unix//run/ha-ingress/'+name+'/http.sock',caddy)
        self.assertIn('size=640m',compose['services']['portal-web']['tmpfs'][0])
        self.assertFalse(review['ready_for_service_transition'])
        self.assertFalse(review['operating_authority_issued'])
        self.assertFalse(review['production_deployed'])
        self.assertIn('fresh DB lease',review['ingress_health'])

    def test_trusted_components_are_separate_and_private_keys_not_app_mounted(self):
        compose,caddy,review=guest.render(fixture());services=compose['services']
        self.assertEqual(services['ha-broker']['user'],'10002:10002')
        self.assertEqual(services['ha-network']['user'],'10003:10001')
        self.assertEqual(services['ha-ingress']['user'],'10004:10004')
        self.assertEqual(services['ha-ingress']['entrypoint'],['/usr/bin/caddy'])
        self.assertEqual(services['ha-ingress']['command'][0],'run')
        self.assertIn('require_and_verify',caddy);self.assertIn('admin off',caddy)
        self.assertNotIn('0.0.0.0',caddy)
        self.assertEqual(services['ha-broker']['extra_hosts']['ha-a'],'192.0.2.10')
        for service in guest.SERVICES:
            self.assertEqual(services['ha-broker']['extra_hosts'][service+'.ha.internal'],'192.0.2.10')
        self.assertTrue(all(s['restart']=='no' for s in services.values()))
        self.assertEqual(len(review['directories']),15)

    def test_unpinned_images_secret_injection_and_namespace_bypass_are_rejected(self):
        changes=[lambda v:v['images'].update({'portal-web':'portal:latest'}),
            lambda v:v['app_environment']['car-care-worker'].update({'CAR_CARE_TELEGRAM_BOT_TOKEN':'secret'}),
            lambda v:v['app_environment']['homeops-executor'].update({'PYTHONPATH':'/tmp'}),
            lambda v:v.update({'network_mode':'host'}),lambda v:v.update({'docker_gid':10001}),
            lambda v:v.update({'pg_b':v['pg_a']}),lambda v:v.update({'guest_ip':'127.0.0.1'}),
            lambda v:v['app_environment']['homeops-executor'].update({'HOMEOPS_DOCKER_MANAGED_SERVICES':'car-care-worker'})]
        for change in changes:
            value=fixture();change(value)
            with self.subTest(value=value),self.assertRaises(guest.LayoutError):guest.render(value)

    def test_original_argv_comes_from_actual_seven_dockerfiles(self):
        compose,_,_=guest.render(fixture())
        import json
        for service in guest.SERVICES:
            lines=(ROOT/service/'Dockerfile').read_text().splitlines()
            original=json.loads(next(line[4:] for line in lines if line.startswith('CMD ')))
            command=compose['services'][service]['command']
            self.assertEqual(command[command.index('--')+1:],original)


class IngressPackageTest(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('ha_ingress_package',ROOT/'infra/k8s/ha-package/ingress.py')
        self.ingress=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.ingress)
        self.files={name:(ROOT/name).read_bytes() for name in self.ingress.SOURCES}

    def test_file_capabilities_removed_before_running_without_capabilities(self):
        recipe,base=self.ingress.recipe(self.files)
        self.assertIn('setcap -r /usr/bin/caddy',recipe)
        self.assertIn('test -z "$(getcap /usr/bin/caddy)"',recipe)
        self.assertLess(recipe.index('setcap -r'),recipe.index('USER 10004:10004'))
        self.assertIn('@sha256:',base)
        self.assertIn('ENTRYPOINT ["/usr/bin/caddy"]',recipe)
        self.assertNotIn('EXPOSE',recipe)
        altered=dict(self.files);altered[self.ingress.SOURCES[1]]+=b'RUN arbitrary\n'
        with self.assertRaises(self.ingress.P.PackageError):self.ingress.recipe(altered)

    def test_prepare_binds_exact_git_and_detects_rendered_or_manifest_tampering(self):
        import tempfile,subprocess,json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'repo';root.mkdir()
            for name,raw in self.files.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            def git(*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
            git('init','-q');git('add','.');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            commit=git('rev-parse','HEAD').decode().strip();output=Path(temp)/'bundle'
            with patch.object(self.ingress.P,'ROOT',root):
                manifest=self.ingress.prepare(commit,output)
                self.assertEqual(self.ingress.verify_source(output),manifest)
                path=output/'Dockerfile';path.chmod(0o600);original=path.read_bytes();path.write_bytes(original+b'RUN arbitrary\n')
                with self.assertRaises(self.ingress.P.PackageError):self.ingress.verify_source(output)
                path.write_bytes(original)
                path=output/'source-manifest.json';path.chmod(0o600);value=json.loads(path.read_bytes());value['source_commit']='f'*40;path.write_text(json.dumps(value))
                with self.assertRaises(self.ingress.P.PackageError):self.ingress.verify_source(output)
