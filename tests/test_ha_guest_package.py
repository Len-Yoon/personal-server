"""Guest isolation and transition authority boundaries, without any deployment."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'infra/k8s'))
spec=importlib.util.spec_from_file_location('ha_guest_package',ROOT/'infra/k8s/ha-package/guest.py')
guest=importlib.util.module_from_spec(spec);spec.loader.exec_module(guest)


def fixture():
    return {'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'node':'A',
        'guest_ip':'192.0.2.10','pg_a':'192.0.2.10','pg_b':'192.0.2.11',
        'images':{name:'fixture.invalid/'+name+'@sha256:'+'c'*64 for name in (*guest.SERVICES,'ha-backup-utility','ha-ingress-utility')},
        'gateway_sha256':'d'*64,'broker_sha256':'e'*64,'docker_gid':999,
        'app_environment':{'car-care-worker':{'CAR_CARE_TELEGRAM_CHAT_ID':'fixture'},
            'homeops-executor':{'HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256':'f'*64,
                'HOMEOPS_DOCKER_MANAGED_SERVICES':'portal-web,book-memo'}}}


class GuestLayoutTest(unittest.TestCase):
    def test_seven_apps_have_only_fixed_capabilities_and_no_external_network(self):
        value=fixture();original=copy.deepcopy(value)
        compose,haproxy,review=guest.render(value)
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
            self.assertIn('unix@/run/ha-ingress/'+name+'/http.sock',haproxy)
        self.assertIn('size=640m',compose['services']['portal-web']['tmpfs'][0])
        self.assertFalse(review['ready_for_service_transition'])
        self.assertFalse(review['operating_authority_issued'])
        self.assertFalse(review['production_deployed'])
        self.assertIn('fresh DB lease',review['ingress_health'])

    def test_trusted_components_are_separate_and_private_keys_not_app_mounted(self):
        compose,haproxy,review=guest.render(fixture());services=compose['services']
        self.assertEqual(services['ha-broker']['user'],'10002:10002')
        self.assertEqual(services['ha-network']['user'],'10003:10001')
        self.assertEqual(services['ha-ingress']['user'],'10004:10004')
        self.assertEqual(services['ha-ingress']['entrypoint'],['/usr/sbin/haproxy'])
        self.assertEqual(services['ha-ingress']['command'],['-W','-db','-f','/etc/ha-ingress/haproxy.cfg'])
        self.assertIn('verify required',haproxy);self.assertIn('ssl-min-ver TLSv1.3',haproxy)
        self.assertIn('  no log',haproxy);self.assertNotIn('stats socket',haproxy);self.assertNotIn('daemon',haproxy)
        self.assertNotIn('0.0.0.0',haproxy)
        self.assertIn('bind 192.0.2.10:10443 ssl',haproxy)
        self.assertIn('http-request deny deny_status 403 unless',haproxy)
        self.assertIn('req.hdr_cnt(host) eq 1',haproxy)
        self.assertEqual(haproxy.count('  use_backend '),7)
        self.assertEqual(haproxy.count('  server app unix@'),7)
        self.assertEqual(review['ingress_key_requirements']['server.pem']['mode'],'0400')
        self.assertFalse(review['ingress_key_requirements']['application_mounts_contain_ingress_keys'])
        self.assertEqual(services['ha-broker']['extra_hosts']['ha-a'],'192.0.2.10')
        for service in guest.SERVICES:
            self.assertEqual(services['ha-broker']['extra_hosts'][service+'.ha.internal'],'192.0.2.10')
        self.assertTrue(all(s['restart']=='no' for s in services.values()))
        self.assertEqual(len(review['directories']),15)

    def test_standalone_ingress_matches_full_guest_layout(self):
        value = fixture()
        compose, expected, _ = guest.render(value)
        self.assertEqual(guest.render_ingress(value['guest_ip']), expected)
        image = value['images']['ha-ingress-utility']
        self.assertEqual(guest.render_ingress_container(image), compose['services']['ha-ingress'])
        with self.assertRaises(guest.LayoutError):
            guest.render_ingress('0.0.0.0')
        with self.assertRaises(guest.LayoutError):
            guest.render_ingress_container('haproxy:latest')

    def test_public_host_is_preserved_only_for_its_matching_tls_service(self):
        haproxy = guest.render_ingress(fixture()['guest_ip'])
        self.assertIn('acl sni_portal-web ssl_fc_sni,lower -m str portal-web.ha.internal', haproxy)
        self.assertIn('portal-web.ha.internal:10443 len.pe.kr portfolio.len.pe.kr file.len.pe.kr admin.len.pe.kr', haproxy)
        self.assertIn('use_backend app_portal-web if sni_portal-web host_portal-web', haproxy)
        self.assertIn('use_backend app_book-memo if sni_book-memo host_book-memo', haproxy)
        self.assertNotIn('portal.len.pe.kr', haproxy)

    def test_caddy_candidate_preserves_approved_native_crlf_and_http_host(self):
        import hashlib
        from ha_control.caddy_cutover import render
        from ha_control.controller import TransitionBlocked

        source = (ROOT / 'caddy/Caddyfile').read_bytes().replace(b'\n', b'\r\n')
        digest = hashlib.sha256(source).hexdigest()
        candidate = render(source, digest, fixture()['guest_ip'])
        self.assertEqual(candidate.count(b'header_up Host {hostport}'), 7)
        self.assertNotIn(b'\n', candidate.replace(b'\r\n', b''))
        self.assertIn(b'redir https://len.pe.kr{uri} permanent', candidate)
        with self.assertRaisesRegex(TransitionBlocked, 'caddy_cutover_source_unconfirmed'):
            render(source + b'\r\n', digest, fixture()['guest_ip'])

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

    def test_offline_config_ids_are_exact_and_mutable_or_malformed_references_rejected(self):
        value=fixture()
        value['images']={name:'sha256:'+format(index+1,'064x') for index,name in enumerate(value['images'])}
        compose,_,_=guest.render(value)
        for name in guest.SERVICES:
            self.assertEqual(compose['services'][name]['image'],value['images'][name])
            self.assertEqual(compose['services'][name]['pull_policy'],'never')
        self.assertEqual(compose['services']['ha-network']['image'],value['images']['ha-backup-utility'])
        self.assertEqual(compose['services']['ha-broker']['image'],value['images']['ha-backup-utility'])
        self.assertEqual(compose['services']['ha-ingress']['image'],value['images']['ha-ingress-utility'])
        for invalid in ('portal:latest','portal:fixed','a'*64,'sha256:abc','sha256:'+'g'*64,
                        'sha256:'+'A'*64,'sha512:'+'a'*64,'sha256:'+'a'*65,'sha256:'+'a'*64+'\n'):
            changed=fixture();changed['images']['portal-web']=invalid
            with self.subTest(image=invalid),self.assertRaises(guest.LayoutError):guest.render(changed)

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

    def test_signed_pinned_haproxy_has_fixed_foreground_command_and_inventory(self):
        recipe,base=self.ingress.recipe(self.files)
        self.assertIn('apk add --no-cache --upgrade',recipe)
        for package in ('haproxy=3.4.7-r0','musl=1.2.6-r3','musl-utils=1.2.6-r3','zlib=1.4-r0','libssl3=3.5.9-r0','libcrypto3=3.5.9-r0'):
            self.assertIn(package,recipe)
        self.assertIn('apk info -v | LC_ALL=C sort',recipe)
        self.assertIn('python:3.11-alpine3.24@sha256:',base)
        self.assertIn('USER 10004:10004',recipe)
        self.assertIn('ENTRYPOINT ["/usr/sbin/haproxy"]',recipe)
        self.assertIn('CMD ["-W", "-db", "-f", "/etc/ha-ingress/haproxy.cfg"]',recipe)
        self.assertNotIn('EXPOSE',recipe);self.assertNotIn('allow-untrusted',recipe)
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
