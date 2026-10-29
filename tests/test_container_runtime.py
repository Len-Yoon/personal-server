"""Local Docker lab protocol and ownership boundaries; no engine required."""
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tarfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('container_runtime', ROOT / 'book-memo/tools/container_runtime.py')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)
        self.owner = 'a' * 32
        self.calls = []
        self.answers = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        (self.parent / '.container-lab-owner').write_text(self.owner)
        self.bin_patch = patch.object(self.module, '_docker_binary', return_value='/usr/local/bin/docker')
        self.bin_patch.start()
        self.addCleanup(self.bin_patch.stop)
        self.run_patch = patch.object(self.module.LocalDocker, '_run', autospec=True, side_effect=self.fake)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.env_patch = patch.dict('os.environ', {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def fake(self, runtime, args, **kwargs):
        self.calls.append(args)
        key = tuple(args)
        value = self.answers.get(key)
        if isinstance(value, Exception):
            raise value
        if value is not None:
            return value
        if args[:2] in (['volume', 'rm'], ['container', 'rm'], ['image', 'rm']):
            kind = args[0]
            name = args[-1]
            if kind == 'container':
                name = next((name_ for name_, record in runtime._containers.items() if record.get('id') == name), name)
            self.answers[tuple(runtime._list_args(kind, name))] = ''
            return name
        if args[:2] == ['context', 'inspect']:
            return json.dumps('unix:///private/test.sock')
        if args[:2] == ['container', 'ls'] or args[:2] == ['volume', 'ls'] or args[:2] == ['image', 'ls']:
            return ''
        if args == ['info', '--format', '{{json .}}']:
            return json.dumps({'ServerVersion': '29.8.2', 'OSType': 'linux', 'Architecture': 'aarch64'})
        return ''

    def runtime(self):
        return self.module.LocalDocker(self.owner, time.monotonic() + 60, lambda: False)

    def archive(self,runtime,*,tags=None,link=False,wrong_owner=False):
        tag=f'ps-book-version-{self.owner}:v2'
        labels={self.module.OWNER_LABEL:'foreign' if wrong_owner else self.owner,
                self.module.KIND_LABEL:self.module.KIND}
        config=self.module._canonical({'config':{'Labels':labels,'User':'10001:10001'}})
        image={'id':'sha256:'+hashlib.sha256(config).hexdigest(),'role':'v2'}
        runtime._images[tag]=image
        runtime._build_labels[tag]=labels
        path=self.parent/'image.tar'
        contents={'manifest.json':json.dumps([{'Config':'config.json','Layers':['layer.tar'],
                    'RepoTags':tags if tags is not None else [tag]}]).encode(),
                  'config.json':config,'layer.tar':b'layer-fixture'}
        with tarfile.open(path,'w') as archive:
            for name,data in contents.items():
                member=tarfile.TarInfo(name);member.size=len(data)
                archive.addfile(member,io.BytesIO(data))
            if link:
                member=tarfile.TarInfo('bad-link');member.type=tarfile.SYMTYPE;member.linkname='/tmp/foreign'
                archive.addfile(member)
        expected={'tag':tag,'image_id':image['id'],'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                  'size_bytes':path.stat().st_size}
        return image,path,expected

    def test_archive_validates_owned_single_image_without_extracting(self):
        runtime=self.runtime();image,path,expected=self.archive(runtime)
        self.assertEqual(runtime.verify_image_archive(path,image,expected),expected)
        self.assertEqual(set(item.name for item in self.parent.iterdir()),{'.container-lab-owner','image.tar'})
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaises(RuntimeError):runtime.verify_image_archive(path,image,expected)

    def test_archive_rejects_foreign_tags_links_and_owner_before_load(self):
        for options in ({'tags':['foreign:latest']},{'link':True},{'wrong_owner':True}):
            with self.subTest(options=options):
                runtime=self.runtime();image,path,expected=self.archive(runtime,**options)
                with self.assertRaises(RuntimeError):runtime.import_image(image,path,expected)
                self.assertFalse(any(call[:2]==['image','load'] for call in self.calls))

    def test_archive_existing_tag_prevents_import_and_uncertain_load_is_tracked(self):
        runtime=self.runtime();image,path,expected=self.archive(runtime)
        self.answers[tuple(runtime._list_args('image',expected['tag']))]=image['id']
        with self.assertRaises(RuntimeError):runtime.import_image(image,path,expected)
        self.assertFalse(any(call[:2]==['image','load'] for call in self.calls))
        self.answers[tuple(runtime._list_args('image',expected['tag']))]=''
        self.answers[('image','load','--input',str(path))]=RuntimeError('lost_response')
        with self.assertRaises(RuntimeError):runtime.import_image(image,path,expected)
        self.assertIn(expected['tag'],runtime._unknown_builds)

    def test_import_pinned_owned_image_clears_uncertain_load_marker(self):
        runtime=self.runtime();image,path,expected=self.archive(runtime)
        self.answers[('image','inspect',expected['tag'])]=json.dumps([{'Id':image['id'],
            'Config':{'Labels':runtime._build_labels[expected['tag']]},
            'RepoTags':[expected['tag']],'RepoDigests':[]}])
        self.assertEqual(runtime.import_image(image,path,expected),image)
        self.assertFalse(runtime._unknown_builds)
        self.assertIn(['image','load','--input',str(path)],self.calls)

    def test_archive_writer_file_limit_is_inherited_by_child_process(self):
        runtime=self.runtime();self.run_patch.stop()
        runtime._binary=sys.executable;runtime._endpoint=None
        path=self.parent/'image.tar'
        script=f'from pathlib import Path; Path({str(path)!r}).write_bytes(b"x"*8192)'
        with patch.object(self.module,'ARCHIVE_LIMIT',4096):
            with self.assertRaises(RuntimeError):runtime._run(['-c',script],cap=2,bounded_file=path)
        self.assertLessEqual(path.stat().st_size,4096)

    def test_export_checks_free_space_and_existing_archive_before_save(self):
        runtime=self.runtime();image,path,expected=self.archive(runtime)
        with self.assertRaises(RuntimeError):runtime.export_image(image,path)
        path.unlink()
        with patch.object(self.module.shutil,'disk_usage',return_value=type('Disk',(),{'free':1})()):
            with self.assertRaises(RuntimeError):runtime.export_image(image,path)
        self.assertFalse(any(call[:2]==['image','save'] for call in self.calls))

    def test_oci_index_graph_must_match_the_single_pinned_image(self):
        annotation_cases = [({},False), ({'io.containerd.image.name':'v2'},True),
                            ({'org.opencontainers.image.ref.name':'v2'},False),
                            ({'io.containerd.image.name':f'ps-book-version-{self.owner}:v2'},False),
                            ({'io.containerd.image.name':f'docker.io/library/ps-book-version-{self.owner}:v2'},False),
                            ({'io.containerd.image.name':'foreign:latest'},True),
                            ({'org.opencontainers.image.ref.name':'foreign:latest'},True)]
        for wrong_config,annotations,reject in [(False,annotations,reject) for annotations,reject in annotation_cases]+[(True,{},True)]:
            with self.subTest(wrong_config=wrong_config,annotations=annotations):
                runtime=self.runtime();image,path,expected=self.archive(runtime)
                with tarfile.open(path) as source:
                    contents={member.name:source.extractfile(member).read() for member in source}
                def blob(data):
                    identity=hashlib.sha256(data).hexdigest()
                    contents['blobs/sha256/'+identity]=data
                    return {'digest':'sha256:'+identity,'size':len(data)}
                config=blob(contents['config.json'] if not wrong_config else b'{"foreign":"image"}')
                layer=blob(contents['layer.tar'])
                manifest={'schemaVersion':2,'config':config,'layers':[layer]}
                reference=blob(json.dumps(manifest).encode())
                reference['mediaType']='application/vnd.oci.image.manifest.v1+json'
                reference['annotations']=annotations
                contents['index.json']=json.dumps({'schemaVersion':2,'manifests':[reference]}).encode()
                with tarfile.open(path,'w') as output:
                    for name,data in contents.items():
                        member=tarfile.TarInfo(name);member.size=len(data);output.addfile(member,io.BytesIO(data))
                expected.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),size_bytes=path.stat().st_size)
                if reject:
                    with self.assertRaises(RuntimeError):runtime.verify_image_archive(path,image,expected)
                else:self.assertEqual(runtime.verify_image_archive(path,image,expected),expected)

    def test_discard_source_removes_only_stopped_owned_unshared_resources(self):
        for foreign_tag in (False,True):
            with self.subTest(foreign_tag=foreign_tag):
                runtime=self.runtime();image,path,expected=self.archive(runtime)
                tag=expected['tag'];volume='owned-volume';identity='c'*64;name='owned-source'
                labels=runtime._labels()
                runtime._volumes[volume]=None
                runtime._containers[name]={'id':identity,'volume':volume,'image':image,'role':'v2','clean':True}
                data={'Id':identity,'State':{'Running':False,'Pid':0}}
                self.answers[('volume','inspect',volume)]=json.dumps([{'Name':volume,'Labels':labels}])
                self.answers[('image','inspect',tag)]=json.dumps([{'Id':image['id'],'Config':{'Labels':labels},
                    'RepoTags':[tag,'foreign:latest'] if foreign_tag else [tag],'RepoDigests':[]}])
                self.calls.clear()
                with patch.object(runtime,'_container',return_value=data):
                    if foreign_tag:
                        with self.assertRaises(RuntimeError):runtime.discard_source(identity,volume,image)
                        self.assertFalse(any(call[1]=='rm' for call in self.calls))
                    else:
                        result=runtime.discard_source(identity,volume,image)
                        self.assertTrue(result['container_absent'] and result['volume_absent'] and result['image_absent'])
                        self.assertFalse(runtime._containers or runtime._volumes)
                        self.assertIn(tag,runtime._images)
                        self.assertTrue(all('--force' not in call for call in self.calls))

    def test_local_context_and_sanitized_environment(self):
        runtime = self.runtime()
        self.assertEqual(runtime.info()['os'], 'linux')
        self.assertEqual(runtime._endpoint, 'unix:///private/test.sock')
        self.assertFalse(any(key.startswith('DOCKER_') for key in runtime._env))

    def test_reject_remote_endpoint_and_invalid_owner(self):
        with patch.dict('os.environ', {'DOCKER_HOST': 'tcp://private.example:2375'}):
            with self.assertRaisesRegex(RuntimeError, '^Local Docker lab failed$'):
                self.runtime()
        self.answers[('context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}')] = json.dumps('ssh://private.example')
        with self.assertRaises(RuntimeError):
            self.runtime()
        with self.assertRaises(RuntimeError):
            self.module.LocalDocker('bad', time.monotonic() + 60, lambda: False)

    def context(self):
        context = self.parent / 'v1'
        context.mkdir()
        payload = {'Dockerfile': self.module.DOCKERFILE_BYTES, 'requirements.txt': b'example==1\n', 'app/main.py': b'main', 'app/services/book_service.py': b'service'}
        files = {}
        for name, data in payload.items():
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[name] = hashlib.sha256(data).hexdigest()
        descriptor = {'schema_version': 1, 'app_commit': 'b' * 40, 'build_commit': 'c' * 40, 'files': files,
                      'payload_digest': hashlib.sha256(self.module._canonical(files)).hexdigest(),
                      'main_sha': files['app/main.py'], 'service_sha': files['app/services/book_service.py']}
        (context / 'release.json').write_bytes(self.module._canonical(descriptor))
        return context, descriptor

    def test_tampered_extra_and_symlink_context_rejected_before_build(self):
        runtime = self.runtime()
        context, descriptor = self.context()
        (context / 'extra').write_text('secret')
        with self.assertRaises(RuntimeError):
            runtime.build(context, descriptor, 'v1')
        self.assertFalse(any(call[0] == 'build' for call in self.calls))
        (context / 'extra').unlink()
        target = context / 'app/main.py'
        target.unlink()
        target.symlink_to(context / 'requirements.txt')
        with self.assertRaises(RuntimeError):
            runtime.build(context, descriptor, 'v1')

    def test_existing_image_tag_never_reused(self):
        runtime = self.runtime()
        context, descriptor = self.context()
        tag = f'ps-book-version-{self.owner}:v1'
        self.answers[tuple(runtime._list_args('image', tag))] = 'sha256:' + 'd' * 64
        with self.assertRaises(RuntimeError):
            runtime.build(context, descriptor, 'v1')
        self.assertFalse(any(call[0] == 'build' for call in self.calls))

    def test_cleanup_foreign_volume_is_not_removed(self):
        runtime = self.runtime()
        name = f'ps-book-version-{self.owner}-data-1'
        runtime._volumes[name] = None
        self.answers[tuple(runtime._list_args('volume', name))] = name
        self.answers[('volume', 'inspect', name)] = json.dumps([{'Name': name, 'Labels': {'org.personal-server.lab.owner': 'foreign'}}])
        result = runtime.cleanup()
        self.assertFalse(result['ok'])
        self.assertFalse(any(call[:2] == ['volume', 'rm'] for call in self.calls))

    def test_cleanup_uncertain_create_reconciles_only_owned_registered_volume(self):
        runtime = self.runtime()
        name = f'ps-book-version-{self.owner}-data-1'
        runtime._volumes[name] = None
        self.answers[tuple(runtime._list_args('volume', name))] = name
        self.answers[('volume', 'inspect', name)] = json.dumps([{'Name': name, 'Labels': runtime._labels()}])
        result = runtime.cleanup()
        self.assertTrue(result['ok'])
        self.assertEqual(result['volumes_removed'], 1)
        self.assertIn(['volume', 'rm', name], self.calls)

    def test_absence_requires_successful_list_and_cleanup_never_prunes(self):
        runtime = self.runtime()
        name = f'ps-book-version-{self.owner}-data-1'
        runtime._volumes[name] = None
        self.answers[tuple(runtime._list_args('volume', name))] = RuntimeError('private path')
        result = runtime.cleanup()
        self.assertFalse(result['ok'])
        self.assertGreater(result['residual_counts']['volumes'], 0)
        self.assertFalse(any('prune' in call or '--force' in call for call in self.calls))

    def test_unknown_image_and_untracked_probe_rejected(self):
        runtime = self.runtime()
        with self.assertRaises(RuntimeError):
            runtime.start({'id': 'sha256:' + 'a' * 64}, 'foreign-volume', 'v1')
        with self.assertRaises(RuntimeError):
            runtime.probe('foreign', 'seed')
        with self.assertRaises(RuntimeError):
            runtime.stop('foreign')


    def image(self, runtime):
        value = {'id': 'sha256:' + 'd' * 64, 'role': 'v1', 'app_commit': 'b' * 40,
                 'build_commit': 'c' * 40, 'main_sha': 'e' * 64, 'service_sha': 'f' * 64,
                 'os': 'linux', 'architecture': 'arm64'}
        runtime._images[f'ps-book-version-{self.owner}:v1'] = dict(value)
        return value

    def actor(self, runtime, *, running=True, exit_code=0):
        image = self.image(runtime)
        name = f'ps-book-version-{self.owner}-v1-1'
        identity = '1' * 64
        volume = f'ps-book-version-{self.owner}-data-1'
        record = {'id': identity, 'image': image, 'volume': volume, 'role': 'v1', 'clean': False}
        runtime._containers[name] = record
        runtime._volumes[volume] = None
        data = {'Id': identity, 'Name': '/' + name, 'Image': image['id'],
                'Config': {'Labels': runtime._labels() | {'org.personal-server.lab.role': 'v1'},
                           'User': '10001:10001',
                           'Cmd': ['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8003'],
                           'Env': ['BOOK_MEMO_DB_PATH=/app/data/fixture.sqlite3', 'DEMO_MODE=0',
                                   'PYTHONDONTWRITEBYTECODE=1', 'NO_PROXY=127.0.0.1,localhost']},
                'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                               'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 64,
                               'Memory': 268435456, 'NanoCpus': 1000000000,
                               'Tmpfs': {'/tmp': 'rw,noexec,nosuid,size=16m'}},
                'Mounts': [{'Type': 'volume', 'Name': volume, 'Destination': '/app/data', 'RW': True}],
                'State': {'Running': running, 'Status': 'running' if running else 'exited',
                          'Pid': 100 if running else 0, 'ExitCode': exit_code, 'OOMKilled': False, 'Error': ''}}
        return name, identity, data

    def test_previous_writer_uncertain_stop_blocks_next_even_if_engine_reports_none_running(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime)
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        self.answers[('container', 'stop', '--signal', 'SIGTERM', '--time', '5', identity)] = RuntimeError(self.module.ERROR)
        with self.assertRaisesRegex(RuntimeError, '^Local Docker lab failed$'):
            runtime.stop(identity)
        with self.assertRaises(RuntimeError):
            runtime.start(dict(runtime._containers[name]['image']), runtime._containers[name]['volume'], 'v1')
        self.assertFalse(any(call[0] == 'run' for call in self.calls))
        self.assertIn(['container', 'stop', '--signal', 'SIGTERM', '--time', '5', identity], self.calls)

    def test_nonzero_exit_never_proves_clean_writer_handoff(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False, exit_code=137)
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        with self.assertRaises(RuntimeError):
            runtime.stop(identity)
        self.assertFalse(runtime._containers[name]['clean'])

    def test_expected_sigterm_exit143_allows_handoff_but_oom_and_error_do_not(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False, exit_code=143)
        data['State'].update({'OOMKilled': False, 'Error': ''})
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        runtime.stop(identity)
        self.assertTrue(runtime._containers[name]['clean'])
        for key, value in [('OOMKilled', True), ('Error', 'runtime failure')]:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(data))
                changed['State'][key] = value
                self.answers[('container', 'inspect', name)] = json.dumps([changed])
                with self.assertRaises(RuntimeError):
                    runtime.stop(identity)
                self.assertFalse(runtime._containers[name]['clean'])

    def test_valid_clean_stop_records_handoff_and_foreign_identity_rejected(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False)
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        runtime.stop(identity)
        self.assertTrue(runtime._containers[name]['clean'])
        data['Id'] = '2' * 64
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        with self.assertRaises(RuntimeError):
            runtime.stop(identity)

    def test_runtime_rejects_host_mounts_ports_network_and_privilege(self):
        runtime = self.runtime()
        name, _, data = self.actor(runtime)
        record = runtime._containers[name]
        runtime._verify_runtime(data, record)
        for key, value in [('Binds', ['/private:/app/data']), ('NetworkMode', 'bridge'),
                           ('PortBindings', {'8003/tcp': [{'HostPort': '8003'}]}),
                           ('Privileged', True), ('ReadonlyRootfs', False), ('CapAdd', ['SYS_ADMIN'])]:
            with self.subTest(key=key):
                modified = json.loads(json.dumps(data))
                modified['HostConfig'][key] = value
                with self.assertRaises(RuntimeError):
                    runtime._verify_runtime(modified, record)

    def test_cleanup_container_before_volume_before_image(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False)
        volume = runtime._containers[name]['volume']
        tag = f'ps-book-version-{self.owner}:v1'
        self.answers[tuple(runtime._list_args('container', name))] = identity
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        self.answers[tuple(runtime._list_args('volume', volume))] = volume
        self.answers[('volume', 'inspect', volume)] = json.dumps([{'Name': volume, 'Labels': runtime._labels()}])
        self.answers[tuple(runtime._list_args('image', tag))] = data['Image']
        self.answers[('image', 'inspect', tag)] = json.dumps([{'Id': data['Image'], 'Config': {'Labels': runtime._labels()}}])
        result = runtime.cleanup()
        self.assertTrue(result['ok'])
        removals = [call[:2] for call in self.calls if len(call) > 1 and call[1] == 'rm']
        self.assertEqual(removals, [['container', 'rm'], ['volume', 'rm'], ['image', 'rm']])
        with self.assertRaises(RuntimeError):
            runtime.volume()

    def test_unremoved_container_preserves_volume_and_image(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False)
        self.answers[tuple(runtime._list_args('container', name))] = identity
        data['Config']['Labels']['org.personal-server.lab.owner'] = 'foreign'
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        result = runtime.cleanup()
        self.assertFalse(result['ok'])
        self.assertFalse(any(call[1:2] == ['rm'] for call in self.calls))


    def test_build_returns_inspected_immutable_identity_with_fixed_labels(self):
        runtime = self.runtime()
        context, descriptor = self.context()
        tag = f'ps-book-version-{self.owner}:v1'
        labels = runtime._labels() | {'org.personal-server.lab.role': 'v1'}
        labels.update({f'org.personal-server.lab.{key}': descriptor[key]
                       for key in ('app_commit', 'build_commit', 'main_sha', 'service_sha')})
        identity = 'sha256:' + 'd' * 64
        self.answers[('image', 'inspect', tag)] = json.dumps([{'Id': identity, 'Os': 'linux',
            'Architecture': 'arm64', 'Config': {'Labels': labels, 'User': '10001:10001'}}])
        result = runtime.build(context, descriptor, 'v1')
        self.assertEqual(result['id'], identity)
        result['id'] = 'sha256:' + 'e' * 64
        self.assertEqual(runtime._images[tag]['id'], identity)
        build = next(call for call in self.calls if call[0] == 'build')
        self.assertIn(f'org.personal-server.lab.owner={self.owner}', build)
        self.assertEqual(build[-1], str(context))

    def test_start_runs_only_immutable_image_with_complete_isolation_options(self):
        runtime = self.runtime()
        _, identity, data = self.actor(runtime)
        image = dict(next(iter(runtime._images.values())))
        volume = next(iter(runtime._volumes))
        runtime._containers.clear()
        self.answers[('volume', 'inspect', volume)] = json.dumps([{'Name': volume, 'Labels': runtime._labels()}])
        expected_name = f'ps-book-version-{self.owner}-v1-1'
        def run_side_effect(instance, args, **kwargs):
            if args[0] == 'run':
                self.calls.append(args)
                return identity
            if args[:2] == ['container', 'inspect']:
                self.calls.append(args)
                return json.dumps([data])
            return self.fake(instance, args, **kwargs)
        self.module.LocalDocker._run.side_effect = run_side_effect
        self.assertEqual(runtime.start(image, volume, 'v1'), identity)
        run = next(call for call in self.calls if call[0] == 'run')
        self.assertEqual(run[-1], image['id'])
        self.assertNotIn('--publish', run)
        self.assertNotIn('--privileged', run)
        self.assertIn('none', run)
        self.assertIn('--read-only', run)
        self.assertIn(f'type=volume,source={volume},target=/app/data', run)
        self.assertEqual(runtime._containers[expected_name]['id'], identity)

    def test_running_owner_actor_blocks_start_before_mutation(self):
        runtime = self.runtime()
        image = self.image(runtime)
        volume = f'ps-book-version-{self.owner}-data-1'
        runtime._volumes[volume] = None
        args = ['container', 'ls', '--no-trunc', '--filter',
                f'label={self.module.OWNER_LABEL}={self.owner}', '--format', '{{.ID}}']
        self.answers[tuple(args)] = 'f' * 64
        with self.assertRaises(RuntimeError):
            runtime.start(image, volume, 'v1')
        self.assertFalse(any(call[0] == 'run' for call in self.calls))

    def test_cleanup_created_container_is_safe_without_claiming_clean_stop(self):
        runtime = self.runtime()
        name, identity, data = self.actor(runtime, running=False)
        data['State']['Status'] = 'created'
        self.answers[tuple(runtime._list_args('container', name))] = identity
        self.answers[('container', 'inspect', name)] = json.dumps([data])
        result = runtime.cleanup()
        self.assertTrue(result['ok'])
        self.assertIn(['container', 'rm', identity], self.calls)


    def test_lost_volume_create_response_was_registered_before_mutation(self):
        runtime = self.runtime()
        seen = []
        def run_side_effect(instance, args, **kwargs):
            if args[:2] == ['volume', 'create']:
                seen.append(args[-1] in instance._volumes)
                raise RuntimeError(self.module.ERROR)
            return self.fake(instance, args, **kwargs)
        self.module.LocalDocker._run.side_effect = run_side_effect
        with self.assertRaises(RuntimeError):
            runtime.volume()
        self.assertEqual(seen, [True])
        self.assertEqual(len(runtime._volumes), 1)
        self.assertTrue(runtime.cleanup()['ok'])

    def test_image_tag_removal_does_not_claim_retained_immutable_id_removed(self):
        runtime = self.runtime()
        image = self.image(runtime)
        tag = f'ps-book-version-{self.owner}:v1'
        self.answers[tuple(runtime._list_args('image', tag))] = image['id']
        inspected = json.dumps([{'Id': image['id'], 'Config': {'Labels': runtime._labels()},
                                 'RepoTags': [], 'RepoDigests': []}])
        self.answers[('image', 'inspect', tag)] = inspected
        self.answers[('image', 'inspect', image['id'])] = inspected
        owner_list = ['image', 'ls', '--all', '--no-trunc', '--filter',
                      f'label={self.module.OWNER_LABEL}={self.owner}', '--format', '{{.ID}}']
        self.answers[tuple(owner_list)] = image['id']
        self.answers[('image', 'rm', '--no-prune', image['id'])] = RuntimeError(self.module.ERROR)
        result = runtime.cleanup()
        self.assertFalse(result['ok'])
        self.assertEqual(result['images_removed'], 0)
        self.assertEqual(result['residual_counts']['images'], 1)
        self.assertFalse(any('--force' in call for call in self.calls))
        image_removals = [call for call in self.calls if call[:2] == ['image', 'rm']]
        self.assertTrue(any(call[-1] == image['id'] for call in image_removals))
        self.assertTrue(all('--no-prune' in call for call in image_removals))

    def test_cleanup_preserves_foreign_sole_tag_and_digest_before_id_delete(self):
        for reference_kind in ('tag', 'digest'):
            with self.subTest(reference_kind=reference_kind):
                runtime = self.runtime()
                image = self.image(runtime)
                tag = f'ps-book-version-{self.owner}:v1'
                foreign_tags = ['foreign-project:keep'] if reference_kind == 'tag' else []
                foreign_digests = ['foreign-project@sha256:' + 'f' * 64] if reference_kind == 'digest' else []
                self.answers[tuple(runtime._list_args('image', tag))] = image['id']
                self.answers[('image', 'inspect', tag)] = json.dumps([{'Id': image['id'],
                    'Config': {'Labels': runtime._labels()}, 'RepoTags': [tag] + foreign_tags,
                    'RepoDigests': foreign_digests}])
                self.answers[('image', 'inspect', image['id'])] = json.dumps([{'Id': image['id'],
                    'Config': {'Labels': runtime._labels()}, 'RepoTags': foreign_tags,
                    'RepoDigests': foreign_digests}])
                owner_list = ['image', 'ls', '--all', '--no-trunc', '--filter',
                              f'label={self.module.OWNER_LABEL}={self.owner}', '--format', '{{.ID}}']
                self.answers[tuple(owner_list)] = image['id']
                self.calls.clear()
                result = runtime.cleanup()
                self.assertFalse(result['ok'])
                self.assertEqual(result['residual_counts']['images'], 1)
                image_removals = [call for call in self.calls if call[:2] == ['image', 'rm']]
                self.assertFalse(any(call[-1] == image['id'] for call in image_removals))
                self.assertTrue(image_removals)
                self.assertTrue(all('--no-prune' in call for call in image_removals))

    def test_failed_build_without_final_tag_keeps_unknown_residual(self):
        runtime = self.runtime()
        context, descriptor = self.context()
        def run_side_effect(instance, args, **kwargs):
            if args[0] == 'build':
                raise RuntimeError(self.module.ERROR)
            return self.fake(instance, args, **kwargs)
        self.module.LocalDocker._run.side_effect = run_side_effect
        with self.assertRaises(RuntimeError):
            runtime.build(context, descriptor, 'v1')
        result = runtime.cleanup()
        self.assertFalse(result['ok'])
        self.assertEqual(result['residual_counts']['unknown_builds'], 1)
        self.assertEqual(result['residual_counts']['images'], 1)

    def test_timeout_kills_own_descendants_and_preserves_unrelated_process(self):
        self.run_patch.stop()
        runtime = self.module.LocalDocker.__new__(self.module.LocalDocker)
        runtime._binary = sys.executable
        runtime._endpoint = None
        runtime._env = {'PATH': '/usr/bin:/bin'}
        runtime.deadline = time.monotonic() + 3
        runtime.interrupted = lambda: False
        runtime._cleanup_deadline = None
        pid_file = self.parent / 'child.pid'
        child_script = 'import time;time.sleep(30)'
        script = ('import subprocess,sys,time,pathlib;'
                  f'p=subprocess.Popen([sys.executable,"-c",{child_script!r}]);'
                  f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(30)')
        sentinel = subprocess.Popen([sys.executable, '-c', child_script], start_new_session=True)
        child_id = None
        try:
            with self.assertRaisesRegex(RuntimeError, '^Local Docker lab failed$'):
                runtime._run(['-c', script], cap=.2)
            child_id = int(pid_file.read_text())
            running = True
            until = time.monotonic() + 1
            while time.monotonic() < until:
                try:
                    os.kill(child_id, 0)
                    running = True
                except ProcessLookupError:
                    running = False
                if not running:
                    break
                time.sleep(.02)
            self.assertFalse(running, 'owned descendant remained alive after timeout')
            self.assertIsNone(sentinel.poll())
        finally:
            sentinel.kill()
            sentinel.wait()
            if child_id is not None:
                try:
                    os.kill(child_id, 9)
                except ProcessLookupError:
                    pass

    def test_completed_final_image_resolves_unknown_build_before_cleanup(self):
        runtime = self.runtime()
        context, descriptor = self.context()
        def run_side_effect(instance, args, **kwargs):
            if args[0] == 'build':
                raise RuntimeError(self.module.ERROR)
            return self.fake(instance, args, **kwargs)
        self.module.LocalDocker._run.side_effect = run_side_effect
        with self.assertRaises(RuntimeError):
            runtime.build(context, descriptor, 'v1')
        tag = f'ps-book-version-{self.owner}:v1'
        identity = 'sha256:' + 'd' * 64
        self.answers[tuple(runtime._list_args('image', tag))] = identity
        self.answers[('image', 'inspect', tag)] = json.dumps([{'Id': identity,
            'Config': {'Labels': runtime._build_labels[tag], 'User': '10001:10001'}}])
        result = runtime.cleanup()
        self.assertTrue(result['ok'])
        self.assertEqual(result['residual_counts']['unknown_builds'], 0)
        self.assertEqual(result['images_removed'], 1)

    def test_successful_parent_exit_also_kills_descendants_without_stdout_pipe(self):
        self.run_patch.stop()
        runtime = self.module.LocalDocker.__new__(self.module.LocalDocker)
        runtime._binary = sys.executable
        runtime._endpoint = None
        runtime._env = {'PATH': '/usr/bin:/bin'}
        runtime.deadline = time.monotonic() + 3
        runtime.interrupted = lambda: False
        runtime._cleanup_deadline = None
        pid_file = self.parent / 'child.pid'
        child_script = 'import time;time.sleep(30)'
        script = ('import subprocess,sys,pathlib;'
                  f'p=subprocess.Popen([sys.executable,"-c",{child_script!r}],stdout=subprocess.DEVNULL);'
                  f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))')
        child_id = None
        try:
            self.assertEqual(runtime._run(['-c', script], cap=1), '')
            child_id = int(pid_file.read_text())
            running = True
            until = time.monotonic() + 1
            while time.monotonic() < until:
                try:
                    os.kill(child_id, 0)
                except ProcessLookupError:
                    running = False
                    break
                time.sleep(.02)
            self.assertFalse(running, 'owned descendant remained after successful CLI exit')
        finally:
            if child_id is not None:
                try:
                    os.kill(child_id, 9)
                except ProcessLookupError:
                    pass

    def test_process_timeout_interrupt_output_cap_and_errors_are_sanitized(self):
        # Real, harmless child processes verify reaping without any Docker engine.
        self.run_patch.stop()
        for script, interrupted, cap, limit in [
            ('import time;time.sleep(30)', lambda: False, .04, 1024),
            ('import time;time.sleep(30)', lambda: time.monotonic() > self.interrupt_at, 2, 1024),
            ('print("private-endpoint" * 1000)', lambda: False, 2, 64),
            ('import sys;sys.stderr.write("private-endpoint");sys.exit(1)', lambda: False, 2, 1024),
        ]:
            with self.subTest(script=script):
                runtime = self.module.LocalDocker.__new__(self.module.LocalDocker)
                runtime._binary = sys.executable
                runtime._endpoint = None
                runtime._env = {'PATH': '/usr/bin:/bin'}
                runtime.deadline = time.monotonic() + 3
                self.interrupt_at = time.monotonic() + .04
                runtime.interrupted = interrupted
                runtime._cleanup_deadline = None
                children = []
                original = subprocess.Popen
                def spawn(*args, **kwargs):
                    process = original(*args, **kwargs)
                    children.append(process)
                    return process
                with patch.object(self.module.subprocess, 'Popen', side_effect=spawn):
                    with self.assertRaisesRegex(RuntimeError, '^Local Docker lab failed$'):
                        runtime._run(['-c', script], cap=cap, limit=limit)
                self.assertTrue(children)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdout.closed)


if __name__ == '__main__':
    unittest.main()
