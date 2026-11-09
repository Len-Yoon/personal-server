"""Offline guest bootstrap integrity and fresh-guest boundaries; no host install."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ha_bootstrap',ROOT/'infra/k8s/ha-package/bootstrap.py')
B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)


def fixture():
    return {'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'node':'A',
        'pg_a':'192.0.2.10','pg_b':'192.0.2.11','cluster':'synthetic','plan_sha256':'c'*64,
        'ubuntu_image':{'filename':'ubuntu.img','sha256':'d'*64,'format':'qcow2',
            'source_url':'https://cloud-images.ubuntu.com/releases/noble/release-20261108/ubuntu-24.04-server-cloudimg-amd64.img'},
        'docker_packages':[{'filename':name+'_1_amd64.deb','sha256':'e'*64,'package':name,'version':'1:1.0-1',
            'architecture':'amd64','source_url':'https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/'+name+'_1_amd64.deb'}
            for name in sorted(B.DEBS)],
        'postgres':{'filename':'postgres.tar','sha256':'f'*64,'config_digest':'sha256:'+'1'*64,'uid':999,'gid':999},
        'utility_config_digest':'sha256:'+'2'*64}


def pg_archive(path, *, arch='amd64', major='17', altered=False):
    layer = b'synthetic uncompressed layer'
    config = {'architecture':arch,'os':'linux','config':{'Env':['PG_MAJOR='+major,'PG_VERSION='+major+'.6']},
              'rootfs':{'diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]}}
    raw = json.dumps(config).encode(); config_id = hashlib.sha256(raw).hexdigest()
    members = {'manifest.json':json.dumps([{'Config':config_id+'.json','Layers':['layer.tar']}]).encode(),
               config_id+'.json':raw,'layer.tar':layer+(b'tampered' if altered else b'')}
    with tarfile.open(path,'w') as writer:
        for name,value in members.items():
            member = tarfile.TarInfo(name);member.size = len(value);writer.addfile(member,io.BytesIO(value))
    return 'sha256:'+config_id


class BootstrapTests(unittest.TestCase):
    def test_lock_is_exact_offline_and_rejects_mutable_or_foreign_inputs(self):
        self.assertEqual(B.validate(fixture()),fixture())
        changes = [lambda v:v.update(node='C'),lambda v:v.update(unknown=True),lambda v:v.update(pg_b=v['pg_a']),
            lambda v:v.update(pg_a='127.0.0.1'),lambda v:v.update(utility_config_digest='latest'),
            lambda v:v['ubuntu_image'].update(source_url='https://cloud-images.ubuntu.com/noble/current/image.img'),
            lambda v:v['ubuntu_image'].update(source_url='https://cloud-images.ubuntu.com/releases/noble/release-20261108/ubuntu-24.04-server-cloudimg-amd64.img?secret=value'),
            lambda v:v['docker_packages'][0].update(filename='../x.deb'),
            lambda v:v['docker_packages'][0].update(architecture='arm64'),
            lambda v:v['docker_packages'][0].update(source_url='https://untrusted.invalid/file.deb'),
            lambda v:v['docker_packages'].pop(),lambda v:v.update(schema_version=True),lambda v:v['postgres'].update(uid=0),lambda v:v['postgres'].update(uid=70,gid=999)]
        for change in changes:
            value = fixture();change(value)
            with self.assertRaises((B.BootstrapError,ValueError)): B.validate(value)

    def test_server_and_initializer_are_closed_with_no_app_network(self):
        lock = fixture();files = B.pg_files(lock)
        server = json.loads(files['pg-compose.json'])['services']['postgres']
        self.assertEqual(server['image'],lock['postgres']['config_digest'])
        self.assertEqual(server['pull_policy'],'never');self.assertEqual(server['restart'],'no')
        self.assertNotIn('ports',server);self.assertNotIn('privileged',server)
        self.assertTrue(all(not v['bind']['create_host_path'] for v in server['volumes']))
        self.assertIn("synchronous_commit = remote_apply",files['postgresql.conf'])
        self.assertIn("synchronous_standby_names = 'FIRST 1 (ha_b)'",files['postgresql.conf'])
        self.assertIn("ssl_min_protocol_version = 'TLSv1.3'",files['postgresql.conf'])
        self.assertNotIn(' trust',files['pg_hba.conf']);self.assertNotIn('password',str(server['command']) if 'command' in server else '')
        initializer = json.loads(files['init-compose.json'])['services']['initialize']
        self.assertEqual(initializer['network_mode'],'none');self.assertEqual(initializer['user'],'999:999')
        self.assertEqual(initializer['cap_drop'],['ALL']);self.assertTrue(initializer['read_only'])
        self.assertNotIn('environment',initializer)
        value = copy.deepcopy(lock);value['node']='B'
        self.assertIn("cluster_name = 'ha_B'",B.pg_files(value)['postgresql.conf'])
        value['postgres'].update(uid=70,gid=70)
        self.assertEqual(json.loads(B.pg_files(value)['pg-compose.json'])['services']['postgres']['user'],'70:70')
        self.assertEqual(json.loads(B.pg_files(value)['init-compose.json'])['services']['initialize']['user'],'70:70')
        self.assertIn("synchronous_standby_names = 'FIRST 1 (ha_a)'",B.pg_files(value)['postgresql.conf'])

    def test_schema2_direction_is_explicit_and_schema1_stays_strict(self):
        value=fixture();value.update(schema_version=2,primary_node='B',source_node='B',os_packages=[])
        self.assertEqual(B.validate(value),value)
        service=json.loads(B.pg_files(value)['pg-compose.json'])['services']['postgres']
        self.assertEqual(service['environment']['HA_PG_SOURCE_NODE'],'B')
        self.assertEqual(service['environment']['HA_PG_PRIMARY_NODE'],'B')
        value.update(node='B',source_node=None)
        self.assertEqual(B.validate(value),value)
        self.assertEqual(B.direction(value),('B',None))
        for change in (lambda v:v.update(source_node='B'),lambda v:v.update(primary_node='C'),
                       lambda v:v.pop('os_packages'),lambda v:v.update(schema_version=1)):
            wrong=copy.deepcopy(value);change(wrong)
            with self.assertRaises(B.BootstrapError):B.validate(wrong)
        self.assertEqual(B.direction(fixture()),('A',None))
        legacy=fixture();legacy['node']='B';self.assertEqual(B.direction(legacy),('A','A'))

    def test_schema2_os_dependency_union_deduplicates_only_identical_packages(self):
        value=fixture();value.update(schema_version=2,primary_node='A',source_node=None,os_packages=[])
        extra={'filename':'libsynthetic_1_amd64.deb','sha256':'4'*64,'package':'libsynthetic','version':'1:1.0',
               'architecture':'amd64','source_url':'https://archive.ubuntu.com/ubuntu/pool/main/l/libsynthetic/libsynthetic_1_amd64.deb'}
        value['docker_packages'].append(extra);value['os_packages'].append(copy.deepcopy(extra))
        B.validate(value);self.assertEqual(len(B.packages(value)),len(value['docker_packages']))
        for field,replacement in (('sha256','5'*64),('version','2:1.0'),('architecture','all'),('filename','different.deb')):
            wrong=copy.deepcopy(value);wrong['os_packages'][0][field]=replacement
            if field=='filename':wrong['os_packages'][0]['source_url']='https://archive.ubuntu.com/ubuntu/pool/main/l/libsynthetic/different.deb'
            with self.assertRaises(B.BootstrapError):B.validate(wrong)

    def test_image_architecture_version_and_layer_corruption_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name).resolve()/'pg.tar'
            good = pg_archive(path);B.archive(path,good)
            for kwargs in ({'arch':'arm64'},{'major':'16'},{'altered':True}):
                config = pg_archive(path,**kwargs)
                with self.assertRaises(B.BootstrapError): B.archive(path,config)
            good = pg_archive(path)
            with tarfile.open(path,'a') as writer:
                member=tarfile.TarInfo('escape');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';writer.addfile(member)
            with self.assertRaises(B.BootstrapError):B.archive(path,good)
            good = pg_archive(path)
            with self.assertRaises(B.BootstrapError):B.archive(path,'sha256:'+'0'*64)

    def test_exact_git_bundle_roundtrip_and_mutation_are_detected(self):
        with tempfile.TemporaryDirectory() as name:
            temp = Path(name).resolve();repo = temp/'repo';repo.mkdir();inputs = temp/'inputs';inputs.mkdir()
            for relative in ['infra/k8s/ha-package/bootstrap.py',*['infra/k8s/ha-package/bootstrap_assets/'+n for n in B.ASSETS],
                             'infra/k8s/ha_runtime/__init__.py','infra/k8s/ha_runtime/schema.sql']:
                path = repo/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/relative).read_bytes())
            def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
            git('init','-q');git('add','.');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            lock = fixture();lock['source_commit']=git('rev-parse','HEAD').decode().strip()
            for record in [lock['ubuntu_image'],*lock['docker_packages']]:
                path=inputs/record['filename'];path.write_bytes(b'QFI\xfb\x00\x00\x00\x03synthetic' if record is lock['ubuntu_image'] else record['filename'].encode());record['sha256']=B.digest(path)
            lock['postgres']['config_digest']=pg_archive(inputs/lock['postgres']['filename'])
            lock['postgres']['sha256']=B.digest(inputs/lock['postgres']['filename'])
            lock_path=temp/'lock.json';lock_path.write_text(json.dumps(lock));bundle=temp/'bundle'
            # Worktree changes must never enter a release prepared from the commit.
            source=repo/'infra/k8s/ha-package/bootstrap_assets/install.sh';source.write_text('worktree uncommitted')
            with patch.object(B,'ROOT',repo):manifest=B.prepare(lock_path,inputs,bundle)
            self.assertEqual(B.verify(bundle)[0],lock)
            self.assertFalse(manifest['writer_authority_issued']);self.assertFalse(manifest['full_seven_app_restore_verified'])
            self.assertNotIn(b'worktree uncommitted',(bundle/'assets/install.sh').read_bytes())
            target=bundle/'generated/postgresql.conf';target.chmod(0o600);target.write_text('changed')
            with self.assertRaises(B.BootstrapError):B.verify(bundle)

    def test_install_requires_independent_manifest_hash_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            bundle=Path(name).resolve();(bundle/'manifest.json').write_text('{}')
            with patch.object(B,'command') as command:
                with self.assertRaises(B.BootstrapError):B.install(bundle,'0'*64)
                command.assert_not_called()

    def test_install_docker_calls_pin_only_fresh_guest_local_daemon(self):
        with patch.dict(B.os.environ,{'DOCKER_HOST':'tcp://foreign.invalid:2375','DOCKER_CONTEXT':'foreign'}), patch.object(B.subprocess,'run') as run:
            run.return_value.returncode=0;run.return_value.stdout=b'local'
            self.assertEqual(B.command(['docker','info']),b'local')
            self.assertEqual(run.call_args.args[0],['docker','--host','unix:///var/run/docker.sock','info'])
            self.assertFalse(any(k.startswith('DOCKER_') for k in run.call_args.kwargs['env']))

    def test_entrypoint_and_initializer_no_dynamic_dsn_or_fixture_authority(self):
        entry=(ROOT/'infra/k8s/ha-package/bootstrap_assets/pg-entrypoint.sh').read_text()
        self.assertIn('sslmode=verify-full',entry);self.assertIn('application_name=$application_name',entry)
        self.assertIn('--no-slot --wal-method=stream',entry)
        self.assertIn('chmod 0700 "$PGDATA"',entry)
        self.assertNotIn('replication.dsn',entry);self.assertNotIn('set -x',entry)
        source=(ROOT/'infra/k8s/ha-package/bootstrap_assets/initialize.py').read_text()
        self.assertIn('DROP FUNCTION ha.local_roles(text[],text,boolean)',source)
        self.assertIn('false,false,clock_timestamp()-',source)
        self.assertIn('effects.validate_catalog(admin,binding)',source)
        self.assertIn("'full_seven_app_restore_verified':False",source)



class BootstrapSmokeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('closed_bootstrap_smoke',ROOT/'infra/k8s/ha-lab/bootstrap_smoke.py')
        cls.smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.smoke)

    def fixture(self,output):
        return self.smoke.Fixture('colima-codex-ha-0123abcd','owned-daemon','sha256:'+'1'*64,
                                  'sha256:'+'2'*64,output)

    def test_remote_mutable_and_implicit_runtime_inputs_are_rejected(self):
        good=('colima-codex-ha-0123abcd','owned-daemon','sha256:'+'1'*64,'sha256:'+'2'*64)
        self.smoke.validate_inputs(*good)
        for index,value in ((0,'orbstack'),(0,'remote'),(1,''),(1,'daemon\nother'),
                            (2,'postgres:17'),(3,'utility:latest'),(3,good[2])):
            arguments=list(good);arguments[index]=value
            with self.assertRaises(self.smoke.SmokeError):self.smoke.validate_inputs(*arguments)

    def test_replaced_daemon_is_rejected_before_mutation(self):
        fixture=self.fixture(Path('/unused'))
        fixture.connected=True
        with patch.object(fixture,'raw',return_value=(b'another-daemon',b'')) as raw:
            with self.assertRaises(self.smoke.SmokeError):fixture.docker('network','rm','a'*64)
            self.assertEqual(raw.call_args_list,[unittest.mock.call('info','--format','{{.ID}}',timeout=10)])

    def test_wrong_owner_or_resource_name_blocks_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            fixture=self.fixture(Path(name).resolve());fixture.connected=True
            identifier='a'*64;fixture.resources['container']=[identifier]
            def docker(*args,**kwargs):
                if args[:2]==('container','inspect'):
                    return json.dumps([{'Id':identifier,'Name':fixture.prefix+'-container-0',
                                        'Config':{'Labels':{self.smoke.LABEL:'somebody-else'}}}])
                if args[0] in ('ps','network','volume'):return ''
                self.fail('cleanup attempted an unowned mutation')
            with patch.object(fixture,'docker',side_effect=docker) as call:
                self.assertFalse(fixture.cleanup())
                self.assertFalse(any(c.args[0]=='rm' for c in call.call_args_list))

    def test_journal_failure_does_not_prevent_owned_resource_cleanup(self):
        fixture=self.fixture(Path('/unused'));fixture.connected=True
        identifier='a'*64;fixture.resources['container']=[identifier]
        with patch.object(fixture,'journal',side_effect=OSError('unavailable')), patch.object(fixture,'owned'), patch.object(fixture,'docker',return_value='') as docker:
            self.assertFalse(fixture.cleanup())
            self.assertIn(unittest.mock.call('rm','--force',identifier),docker.call_args_list)

    def test_reverse_never_promotes_while_old_owned_primary_is_still_running(self):
        fixture=self.fixture(Path('/unused'));fixture.a='old-a';fixture.b='owned-b'
        fixture.volumes={'data-a':'original-data'}
        original={'HostConfig':{'RestartPolicy':{'Name':'no'}}}
        still_running={'State':{'Running':True,'Pid':123}}
        with patch.object(fixture,'journal'), patch.object(fixture,'catalog_fingerprint',return_value='same'), patch.object(fixture,'psql',return_value='t') as psql, patch.object(fixture,'owned',side_effect=[original,still_running]), patch.object(fixture,'docker',return_value='') as docker:
            with self.assertRaises(self.smoke.SmokeError):fixture.reverse_acceptance()
            self.assertEqual(docker.call_args_list,[unittest.mock.call('stop','--time','5','old-a')])
            self.assertFalse(any('pg_promote' in call.args[1] for call in psql.call_args_list))

    def test_output_never_overwrites_a_previous_fixture_or_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();output=root/'new'
            self.smoke.private_output(output)
            self.assertEqual(output.stat().st_mode & 0o777,0o700)
            with self.assertRaises(FileExistsError):self.smoke.private_output(output)
            (root/'link').symlink_to(output,target_is_directory=True)
            with self.assertRaises(self.smoke.SmokeError):self.smoke.private_output(root/'link'/'child')


if __name__=='__main__':unittest.main()
