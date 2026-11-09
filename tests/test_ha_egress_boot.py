"""Broker startup policy and pinned-dispatch doubles; no authority is issued."""
from contextlib import ExitStack
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress import boot,read_provider
from ha_runtime.effects import EffectsBlocked,SERVICES,TrustedIdentity


def fixture():
    release={'source_commit':'a'*40,'release_id':'b'*64,'source_files_sha256':'c'*64}
    endpoint={'port':5432,'dbname':'ha','user':'ha_broker','sslmode':'verify-full',
              'sslrootcert':'/fixture/ca','passfile':'/fixture/pass'}
    return {'schema_version':1,'scope':'n100-approved','profile':'enforced-vm-v1','release':release,
            'installation_id':'f'*64,'node':'A','generation':3,
            'binding':{'cluster':'fixture','system_id':'123','source_commit':'a'*40,
                       'plan_sha256':'d'*64,'broker_role':'ha_broker','broker_instance':'e'*64},
            'postgres':{'primary':dict(endpoint,host='ha-a'),'peer':dict(endpoint,host='ha-b')},
            'tls':{'ca':'/fixture/ca','certificate':'/fixture/cert','key':'/fixture/key'},
            'clients':[{'sha256':f'{index:064x}','service':service,'node':'A','generation':3}
                       for index,service in enumerate(sorted(SERVICES))],
            'providers':dict.fromkeys(('telegram','hyundai','yes24','docker','executor'))}


class BuildTests(unittest.TestCase):
    def build(self,config,secrets=None):
        secrets={} if secrets is None else secrets
        with ExitStack() as stack:
            stack.enter_context(patch.object(boot,'_release',return_value=config['release']))
            stack.enter_context(patch.object(boot,'_authority_file',side_effect=Path))
            stack.enter_context(patch.object(boot,'_private',side_effect=Path))
            stack.enter_context(patch.object(boot,'tls_context',return_value=object()))
            stack.enter_context(patch.object(boot,'read_json',side_effect=lambda path,**_: (json.dumps(secrets[path]).encode(),copy.deepcopy(secrets[path]))))
            self.hyundai=stack.enter_context(patch.object(boot,'HyundaiProvider'))
            return boot.build(config)

    def test_explicit_null_providers_are_not_registered_or_simulated(self):
        config=fixture();broker,identities,_=self.build(config)
        self.assertEqual(set(broker.operations),{'book.http.get','youtube.http.get','crawler.http.get'})
        broker.factory=MagicMock();broker.peer_factory=MagicMock()
        identity=next(identity for identity in identities.values() if identity.service=='car-care-worker')
        for operation in ('car.telegram.send','car.telegram.poll','car.hyundai.snapshot','portal.executor.restart','executor.docker.restart.book-memo'):
            with self.assertRaises(EffectsBlocked):broker.dispatch(identity,{'operation':operation,'request_id':'fixture','payload':{}})
        broker.factory.assert_not_called();broker.peer_factory.assert_not_called();self.hyundai.assert_not_called()
        url='https://apis.yes24.com/v1/goods/itemList?query=fixture&category=BOOK&page=1&pageSize=12'
        with patch.object(read_provider,'_fetch') as fetch:
            with self.assertRaises(OSError):broker.operations['book.http.get'].adapter({'url':url,'headers':{}},timeout=1,deadline=time.monotonic()+1)
            fetch.assert_not_called()

    def test_telegram_resource_is_numeric_bot_identity_not_rotating_secret_or_chat(self):
        resources=[]
        for token,chat in [('12345:'+('a'*24),'123'),('0012345:'+('b'*24),'456'),('54321:'+('c'*24),'123')]:
            config=fixture();config['providers']['telegram']={'portal-web':'/fixture/telegram','crawler-worker':None,'car-care-worker':None}
            broker,_,_=self.build(config,{'/fixture/telegram':{'token':token,'chat_id':chat}})
            self.assertEqual(set(broker.operations),{'portal.telegram.send','book.http.get','youtube.http.get','crawler.http.get'})
            resources.append(broker.operations['portal.telegram.send'].resource)
            self.assertNotIn(token,resources[-1])
        self.assertEqual(resources[0],resources[1]);self.assertNotEqual(resources[0],resources[2])

    def test_hyundai_single_car_resource_survives_secret_rotation_and_null_empty_vehicle(self):
        resources=[]
        for vehicle,secret,client in [(None,'secret-one','client-id'),('','secret-two','client-id'),('specific-car','secret-three',' client-id ')]:
            config=fixture();config['providers']['hyundai']='/fixture/hyundai'
            data={'client_id':client,'client_secret':secret,'vehicle_id':vehicle,'redirect_uri':'https://callback.example.invalid/hyundai'}
            broker,_,_=self.build(config,{'/fixture/hyundai':data})
            resources.append(broker.operations['car.hyundai.snapshot'].resource)
            if not vehicle:self.assertIsNone(self.hyundai.call_args.kwargs['vehicle_id'])
            self.assertNotIn(secret,resources[-1])
        self.assertEqual(len(set(resources)),1)
        config=fixture();config['providers']['hyundai']='/fixture/hyundai'
        data={'client_id':'client','client_secret':'secret','vehicle_id':False,'redirect_uri':'https://callback.example.invalid'}
        with self.assertRaises(ValueError):self.build(config,{'/fixture/hyundai':data})

    def test_distinct_pg_hosts_required_even_when_other_endpoint_fields_differ(self):
        config=fixture();config['postgres']['peer']['host']='ha-a';config['postgres']['peer']['passfile']='/fixture/different'
        with patch.object(boot,'_release',return_value=config['release']),patch.object(boot,'pg_factory') as factory:
            with self.assertRaises(ValueError):boot.build(config)
            factory.assert_not_called()

    def test_null_is_explicit_while_missing_or_malformed_provider_settings_fail(self):
        config=fixture();config['providers'].pop('telegram')
        with self.assertRaises(ValueError):self.build(config)
        for value in ({},False,''):
            config=fixture();config['providers']['telegram']=value
            with self.assertRaises(ValueError):self.build(config)


class PinnedDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config_path=Path(self.temp.name)/'config.json';self.authority_path=Path(self.temp.name)/'authority.json'
        self.config=fixture();self.raw=json.dumps(self.config).encode()
        self.authority={'schema_version':1,'source_commit':'a'*40,'plan_sha256':'d'*64,
                        'config_sha256':hashlib.sha256(self.raw).hexdigest(),'action':'start-effect-broker',
                        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat().replace('+00:00','Z')}
        self.authority_raw=json.dumps(self.authority).encode()
        self.config_path.write_bytes(self.raw);self.authority_path.write_bytes(self.authority_raw)
        self.config_path.chmod(0o444);self.authority_path.chmod(0o444)
        # Ownership/parent-root ACL is an explicit fixture double, not a native
        # filesystem acceptance test. Re-run its mode/link checks on every read.
        def guard(value):
            path=Path(value);info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o444 or info.st_nlink!=1:raise ValueError('acl invalid')
            return path
        self.guard=patch.object(boot,'_authority_file',side_effect=guard);self.check=self.guard.start();self.addCleanup(self.guard.stop)
        self.broker=MagicMock();self.broker.dispatch.return_value={'status':'completed'}
        self.dispatch=boot.PinnedDispatch(self.broker,self.config_path,self.raw,self.authority_path,self.authority_raw)

    def test_each_dispatch_rechecks_acl_hash_authority_before_and_after(self):
        self.assertEqual(self.dispatch('identity',{'request':'fixture'}),{'status':'completed'})
        self.assertEqual(self.check.call_count,4)
        self.broker.dispatch.assert_called_once_with('identity',{'request':'fixture'})

    def test_deleted_changed_or_writable_authority_or_config_blocks_before_dispatch(self):
        for path in (self.config_path,self.authority_path):
            original=path.read_bytes()
            for mutation in ('delete','bytes','acl'):
                path.chmod(0o644)
                if mutation=='delete':path.unlink()
                elif mutation=='bytes':path.write_bytes(original+b' ');path.chmod(0o444)
                else:path.chmod(0o644)
                with self.assertRaisesRegex(ValueError,'^broker_configuration_invalid$'):self.dispatch('identity',{})
                self.broker.dispatch.assert_not_called()
                if path.exists():path.chmod(0o644)
                path.write_bytes(original);path.chmod(0o444)

    def test_change_during_dispatch_cannot_return_a_success_acknowledgement(self):
        def change(*_):
            self.authority_path.chmod(0o644);self.authority_path.write_bytes(self.authority_raw+b' ');self.authority_path.chmod(0o444)
            return {'status':'completed'}
        self.broker.dispatch.side_effect=change
        with self.assertRaises(ValueError):self.dispatch('identity',{})
        self.broker.dispatch.assert_called_once()

    def test_expired_original_authority_blocks_before_any_provider(self):
        self.authority['expires_at']='2000-01-01T00:00:00Z'
        raw=json.dumps(self.authority).encode();self.authority_path.chmod(0o644);self.authority_path.write_bytes(raw);self.authority_path.chmod(0o444)
        dispatch=boot.PinnedDispatch(self.broker,self.config_path,self.raw,self.authority_path,raw)
        with self.assertRaises(ValueError):dispatch('identity',{})
        self.broker.dispatch.assert_not_called()

    def test_symlink_hardlink_and_metadata_change_during_read_fail_closed(self):
        extra=Path(self.temp.name)/'extra';os.link(self.config_path,extra)
        with self.assertRaises(ValueError):self.dispatch('identity',{})
        extra.unlink()
        link=Path(self.temp.name)/'symlink';link.symlink_to(self.config_path)
        with self.assertRaises(ValueError):boot.read_json(link)
        original=os.read
        def alter(descriptor,size):
            data=original(descriptor,size)
            self.config_path.chmod(0o644)
            return data
        with patch.object(boot.os,'read',side_effect=alter):
            with self.assertRaises(ValueError):boot.read_json(self.config_path)
        self.broker.dispatch.assert_not_called()


    def boot_args(self):
        return ['boot','--config',str(self.config_path),'--expected-sha256',hashlib.sha256(self.raw).hexdigest(),'--authority',str(self.authority_path)]

    def identities(self):
        return {item['sha256']:TrustedIdentity(item['service'],item['node'],item['generation']) for item in self.config['clients']}

    def test_main_splits_reserved_identity_and_passes_guard_to_both_listeners(self):
        self.broker.binding.plan_sha256='d'*64;self.broker.operations={}
        public,internal=MagicMock(),MagicMock();handlers=[]
        def install(kind,handler):
            if callable(handler):handlers.append(handler)
            return 0
        # Intentional stop, without real signals/listeners or provider calls.
        public.serve_forever.side_effect=lambda **_:handlers[0]()
        internal.serve_forever.side_effect=lambda **_:handlers[0]()
        with patch.object(boot.sys,'argv',self.boot_args()),patch.object(boot.os,'geteuid',return_value=10002),patch.dict(os.environ,{'APP_ENV':'production'},clear=True),patch.object(boot,'build',return_value=(self.broker,self.identities(),object())),patch.object(boot,'BrokerServer',side_effect=[public,internal]) as create,patch.object(boot.signal,'signal',side_effect=install),patch('sys.stderr',io.StringIO()):
            self.assertEqual(boot.main(),0)
            self.assertEqual(create.call_args_list[0].args[0],('127.0.0.1',8443))
            self.assertEqual(create.call_args_list[1].args[0],('127.0.0.1',8444))
            public_ids=create.call_args_list[0].args[2];internal_ids=create.call_args_list[1].args[2]
            self.assertEqual(len(public_ids),6)
            self.assertNotIn('homeops-executor',{identity.service for identity in public_ids.values()})
            self.assertEqual([identity.service for identity in internal_ids.values()],['homeops-executor'])
            self.assertEqual(create.call_args_list[1].kwargs['workers'],2)
            dispatch=create.call_args_list[0].args[3]
            self.assertIsInstance(dispatch,boot.PinnedDispatch)
            self.assertEqual(dispatch('identity',{}),{'status':'completed'})
            public.server_close.assert_called_once();internal.server_close.assert_called_once()

    def test_internal_dispatch_allows_only_homeops_docker_prefix_and_uses_guard(self):
        guarded=MagicMock(return_value={'status':'completed'})
        dispatch=boot.internal_dispatch(guarded)
        executor=TrustedIdentity('homeops-executor','A',3)
        portal=TrustedIdentity('portal-web','A',3)
        for identity,operation in ((portal,'executor.docker.restart.book-memo'),(executor,'book.http.get'),
                                   (executor,'portal.executor.restart'),(executor,'executor.docker.restart'),
                                   (executor,'car.telegram.send')):
            with self.assertRaisesRegex(ValueError,'^broker_configuration_invalid$'):
                dispatch(identity,{'operation':operation})
        guarded.assert_not_called()
        for operation in ('executor.docker.inspect.book-memo','executor.docker.restart.book-memo'):
            self.assertEqual(dispatch(executor,{'operation':operation}),{'status':'completed'})
        self.assertEqual(guarded.call_count,2)

    def test_second_listener_initialization_failure_closes_first_without_shutdown_deadlock(self):
        self.broker.binding.plan_sha256='d'*64;self.broker.operations={}
        public=MagicMock();output=io.StringIO()
        with patch.object(boot.sys,'argv',self.boot_args()),patch.object(boot.os,'geteuid',return_value=10002),patch.dict(os.environ,{'APP_ENV':'production'},clear=True),patch.object(boot,'build',return_value=(self.broker,self.identities(),object())),patch.object(boot,'BrokerServer',side_effect=[public,OSError('private bootstrap path')]),patch('sys.stderr',output):
            self.assertEqual(boot.main(),1)
        public.server_close.assert_called_once();public.shutdown.assert_not_called()
        self.assertEqual(output.getvalue(),'effect_broker_start_blocked\n')

    def test_first_listener_failure_has_no_second_listener_or_thread(self):
        self.broker.binding.plan_sha256='d'*64;self.broker.operations={}
        with patch.object(boot.sys,'argv',self.boot_args()),patch.object(boot.os,'geteuid',return_value=10002),patch.dict(os.environ,{'APP_ENV':'production'},clear=True),patch.object(boot,'build',return_value=(self.broker,self.identities(),object())),patch.object(boot,'BrokerServer',side_effect=OSError('private')) as create,patch.object(boot.threading,'Thread') as threads,patch('sys.stderr',io.StringIO()):
            self.assertEqual(boot.main(),1)
        self.assertEqual(create.call_count,1);threads.assert_not_called()


if __name__=='__main__':unittest.main()
