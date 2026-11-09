"""Private broker OAuth state acceptance with failure injection."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_ha_effects import E, Store, Connection, Cursor, BINDING, IDENTITY, REQUEST


class PrivateConnection(Connection):
    def execute(self,sql,args=None):
        s=self.store;args=args or ()
        if sql=='BEGIN':self.private_snapshot=deepcopy(s.private)
        if sql=='ROLLBACK' and self.snapshot is not None:s.private=self.private_snapshot
        if sql=='COMMIT' and not s.peer_lag:s.private_peer=deepcopy(s.private)
        if sql.startswith('SELECT payload,revision,payload_sha256 FROM ha_effects.private_state'):
            row=(s.private_peer if self.peer else s.private).get(tuple(args))
            return Cursor([deepcopy(row)] if row else [])
        if sql.startswith('SELECT revision,payload_sha256 FROM ha_effects.private_state'):
            row=(s.private_peer if self.peer else s.private).get(tuple(args))
            receipt=tuple(row[1:]) if row else None
            if receipt and self.peer and s.private_peer_corrupt:receipt=(receipt[0],'0'*64)
            return Cursor([receipt] if receipt else [])
        if sql.startswith('INSERT INTO ha_effects.private_state'):
            service,key,raw,digest=args
            if (service,key) in s.private:raise ValueError('duplicate')
            s.private[(service,key)]=(json.loads(raw),1,digest);return Cursor([])
        if sql.startswith('UPDATE ha_effects.private_state'):
            raw,digest,service,key,revision=args
            row=s.private.get((service,key))
            if not row or row[1]!=revision or s.private_cas_conflict:return Cursor([])
            s.private[(service,key)]=(json.loads(raw),revision+1,digest);return Cursor([(revision+1,)])
        return super().execute(sql,args)


class PrivateStore(Store):
    def __init__(self):
        super().__init__();self.private={};self.private_peer={}
        self.private_peer_corrupt=False;self.private_cas_conflict=False
    def connect(self,peer=False):
        connection=PrivateConnection(self,peer);self.connections.append(connection);return connection


class PrivateCredentialTests(unittest.TestCase):
    def fixture(self,callback):
        store=PrivateStore();backend=None
        def adapter(payload,*,timeout,deadline):return callback(store,backend)
        operation=E.Operation('car-care-worker','oauth-account',frozenset({'text'}),frozenset({'delivered'}),adapter,2)
        broker=E.Broker(store.connect,BINDING,{'send':operation},peer_factory=lambda:store.connect(True))
        backend=broker.credential_backend()
        return store,broker,backend

    @staticmethod
    def ack():return E.ProviderAck('public-ack',{'delivered':True})

    def test_private_state_is_unusable_outside_authenticated_adapter_or_from_another_thread(self):
        store,broker,backend=self.fixture(lambda *_:self.ack())
        for action in (lambda:backend.read_state('car-care-worker','hyundai_tokens',{}),lambda:backend.require_worker('car-care-worker')):
            with self.assertRaises(E.EffectsBlocked):action()
        self.assertEqual(store.connections,[])
        broker.dispatch(IDENTITY,REQUEST)
        with self.assertRaises(E.EffectsBlocked):backend.read_state('car-care-worker','hyundai_tokens',{})

    def test_refresh_fence_commits_and_peer_proves_before_provider_and_clears_only_with_new_token(self):
        provider=[]
        def adapter(store,backend):
            with backend.worker_scope('car-care-worker'):
                with backend.state_transaction('car-care-worker','hyundai_tokens',{}) as payload:
                    payload['refresh_token']='fixture-old-private'
                    payload['refresh_uncertain']=True
                    nested=backend.read_state('car-care-worker','hyundai_tokens',{})
                    self.assertEqual(nested,payload);nested.clear();self.assertTrue(payload)
                row=store.private_peer[('car-care-worker','hyundai_tokens')]
                self.assertTrue(row[0]['refresh_uncertain'])
                self.assertEqual(row[2],hashlib.sha256(E.canonical(row[0])).hexdigest())
                provider.append('refresh')
                with backend.state_transaction('car-care-worker','hyundai_tokens',{}) as payload:
                    payload['refresh_token']='fixture-new-private';payload.pop('refresh_uncertain')
            return self.ack()
        store,broker,backend=self.fixture(adapter)
        result=broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(provider,['refresh']);self.assertEqual(store.private,store.private_peer)
        self.assertEqual(store.private[('car-care-worker','hyundai_tokens')][1],2)
        self.assertNotIn('fixture-new-private',repr(result));self.assertNotIn('fixture-new-private',repr(store.ledger))
        self.assertTrue(broker.dispatch(IDENTITY,REQUEST)['replayed']);self.assertEqual(provider,['refresh'])

    def test_private_commit_lost_warning_peer_failure_never_reaches_provider(self):
        for mode in ('lost','warning','peer_corrupt','cas'):
            calls=[]
            def adapter(store,backend):
                if mode=='lost':store.fail_commit=store.commit_count+1
                if mode=='warning':store.warning_commit=store.commit_count+1
                if mode=='peer_corrupt':store.private_peer_corrupt=True
                if mode=='cas':store.private_cas_conflict=True
                with backend.state_transaction('car-care-worker','hyundai_tokens',{}) as payload:
                    payload['refresh_uncertain']=True
                calls.append('provider');return self.ack()
            store,broker,backend=self.fixture(adapter)
            if mode=='cas':
                payload={'refresh_token':'fixture-private'}
                row=(payload,1,hashlib.sha256(E.canonical(payload)).hexdigest())
                store.private[('car-care-worker','hyundai_tokens')]=row;store.private_peer=deepcopy(store.private)
            with self.subTest(mode=mode):
                with self.assertRaises(E.EffectsBlocked):broker.dispatch(IDENTITY,REQUEST)
                self.assertEqual(calls,[])
                self.assertEqual(store.ledger[('car-care-worker','event-1')]['state'],'uncertain')

    def test_read_only_adapter_never_receives_private_credential_context(self):
        attempted=[]
        def adapter(store,backend):
            with self.assertRaises(E.EffectsBlocked):backend.read_state('car-care-worker','hyundai_tokens',{})
            attempted.append('denied');return self.ack()
        store,broker,backend=self.fixture(adapter)
        broker.operations['send']=replace(broker.operations['send'],read_only=True)
        broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(attempted,['denied']);self.assertEqual(store.private,{})
        self.assertEqual(store.ledger,{});self.assertEqual(store.barriers,{})

    def test_rebind_rejects_reused_role_cluster_or_system_before_any_database_write(self):
        for new in (replace(BINDING,system_id='999'),replace(BINDING,broker_role='new_broker'),replace(BINDING,system_id='999',broker_role='new_broker',cluster='different')):
            connection=MagicMock()
            with self.assertRaises(E.EffectsBlocked):E.rebind_restored(connection,BINDING,new)
            connection.execute.assert_not_called()
            connection.transaction.assert_not_called()

    def test_private_state_scope_and_key_are_fixed_to_authenticated_service(self):
        def adapter(store,backend):
            for service,key in [('portal-web','hyundai_tokens'),('car-care-worker','other')]:
                with self.assertRaises(E.EffectsBlocked):backend.read_state(service,key,{})
            return self.ack()
        store,broker,backend=self.fixture(adapter);broker.dispatch(IDENTITY,REQUEST)
        self.assertEqual(store.private,{})


class EffectFixtureHostTests(unittest.TestCase):
    def exercise_host(self, fail_validation=False):
        import importlib.util
        import io
        import tempfile
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        from ha_control.prepare import EFFECTS_SOURCES  # Import before loader mocks.
        source=Path(__file__).resolve().parents[1]/'infra/k8s/ha-lab/effects_smoke.py'
        spec=importlib.util.spec_from_file_location('effect_host_marker_test',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        events=[];identity={'context':'colima-codex-ha-01a1add8','daemon_id':'fixture-daemon-id','local_socket_sha256':'a'*64}
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary).resolve()
            test=self
            class Fixture:
                def __init__(self,path,*,runtime_context):
                    test.assertEqual(path,directory)
                    test.assertEqual(runtime_context,identity['context'])
                    self.owner='fixture-owner';self.label='fixture-label';self.report={};self.phase='marker-test'
                def validate_runtime(self):
                    events.append('validate')
                    if fail_validation:raise TimeoutError('fixture-only')
                    return dict(identity)
                def prepare(self):
                    events.append('prepare')
                    marker=json.loads((directory/'owner.json').read_text())
                    test.assertEqual(marker['runtime_identity'],identity)
                    test.assertEqual(marker['runtime_context'],identity['context'])
                    test.assertFalse(any(marker['resources'].values()))
                    test.assertEqual((directory/'owner.json').stat().st_mode&0o777,0o600)
                    raise RuntimeError('fixture-stop-before-resources')
                def cleanup(self):events.append('cleanup');return True
            driver=SimpleNamespace(Fixture=Fixture)
            loader=SimpleNamespace(exec_module=lambda module:None)
            with patch.dict(sys.modules,{'effect_fixture_driver':driver}), \
                 patch.object(module.tempfile,'mkdtemp',return_value=str(directory)), \
                 patch.object(module.importlib.util,'spec_from_file_location',return_value=SimpleNamespace(name='effect_fixture_driver',loader=loader)), \
                 patch.object(module.importlib.util,'module_from_spec',return_value=driver),redirect_stdout(io.StringIO()):
                self.assertEqual(module.host_main(None,identity['context']),1)
            report=json.loads((directory/'summary.json').read_text())
            owner=json.loads((directory/'owner.json').read_text())
            self.assertEqual(report['owned_fixture_removed'],True)
            self.assertEqual(report['runtime_identity_verified'],not fail_validation)
            self.assertEqual(owner['runtime_identity'],None if fail_validation else identity)
        return events

    def test_context_and_daemon_identity_are_persisted_before_prepare_can_create_resources(self):
        self.assertEqual(self.exercise_host(),['validate','prepare','cleanup'])

    def test_failed_read_only_runtime_validation_never_prepares_resources(self):
        self.assertEqual(self.exercise_host(True),['validate','cleanup'])


if __name__=='__main__':unittest.main()
