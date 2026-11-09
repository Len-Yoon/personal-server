"""Configuration/transaction entry boundaries; real SQL is exercised by the lab."""
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_runtime import db, enforcement
from ha_runtime.config import DatabaseUnavailable
from ha_control.pg import PgBackend,ENFORCED_SERVICES
from ha_control import privilege_closure
from ha_control.controller import TransitionBlocked
from unittest.mock import MagicMock


class EnforcementEntryTests(unittest.TestCase):
    def test_no_installer_sql_for_untrusted_namespace_or_role(self):
        class NoConnection:
            def transaction(self):
                raise AssertionError('SQL must not be reached')
        for schema,service,roles in [('public','book-memo',['book_a']),
                ('s_book_memo_main','portal-web',['book_a']),
                ('s_book_memo_main','book-memo',[]),
                ('s_book_memo_main','book-memo',['book_a','book_a']),
                ('s_book_memo_main','book-memo',['x;RESET ROLE'])]:
            with self.subTest(schema=schema,service=service,roles=roles):
                with self.assertRaises(DatabaseUnavailable):
                    enforcement.install_namespace(NoConnection(),schema,service,roles)

    def test_blob_cannot_open_or_borrow_another_service_transaction(self):
        connection=SimpleNamespace(closed=False)
        with patch.object(db,'_LOCAL',SimpleNamespace(states={'portal-web':{'connection':connection,'failed':False}})):
            self.assertIs(db.active_state_connection('portal-web'),connection)
            with self.assertRaises(DatabaseUnavailable):db.active_state_connection('book-memo')
            connection.closed=True
            with self.assertRaises(DatabaseUnavailable):db.active_state_connection('portal-web')

    def test_failed_nested_state_cannot_publish_more_blobs(self):
        with patch.object(db,'_LOCAL',SimpleNamespace(states={'portal-web':{'connection':SimpleNamespace(closed=False),'failed':True}})):
            with self.assertRaises(DatabaseUnavailable):db.active_state_connection('portal-web')
        with patch.object(db,'_LOCAL',SimpleNamespace()):
            with self.assertRaises(DatabaseUnavailable):db.active_state_connection('portal-web')


class EnforcedControlCatalogTests(unittest.TestCase):
    def fixture(self):
        backend=object.__new__(PgBackend)
        role_sets={node:{s:'role_'+str(index)+'_'+node.lower() for index,s in enumerate(sorted(ENFORCED_SERVICES))} for node in ('A','B')}
        namespace_tables={'s_'+s.replace('-','_')+'_state':{'service':s,'tables':['ha_barriers','ha_receipts','shared_state']} for s in ENFORCED_SERVICES}
        backend.plan={'system_id':'123','pg_enforcement':{'definition_sha256':'a'*64,'role_sets':role_sets,'namespace_tables':namespace_tables}}
        namespaces=[(schema,item['service'],'a'*64,'123',True,item['tables']) for schema,item in sorted(namespace_tables.items())]
        roles=[(role,s,node,2,False) for node,services in role_sets.items() for s,role in services.items()]
        return backend,namespaces,roles

    def test_live_catalog_requires_all_seven_services_and_exact_permits(self):
        backend,namespaces,roles=self.fixture()
        db_connection=MagicMock()
        db_connection.execute.return_value.fetchall.side_effect=[namespaces,roles]
        with patch.object(privilege_closure,'privilege_closures_unsafe',return_value=False):
            backend._enforced_observation(db_connection,{'generation':2})
        for invalid in [namespaces[:-1],[(*namespaces[0][:4],False,namespaces[0][5]),*namespaces[1:]],
                [(*namespaces[0][:5],[]),*namespaces[1:]],
                [(*namespaces[0][:5],['ha_receipts']),*namespaces[1:]],
                [(*namespaces[0][:5],sorted(namespaces[0][5]+['extra'])),*namespaces[1:]]]:
            db_connection=MagicMock();db_connection.execute.return_value.fetchall.return_value=invalid
            with self.assertRaises(TransitionBlocked):backend._enforced_observation(db_connection,{'generation':2})

    def test_old_generation_or_privileged_permit_cannot_be_relabelled_live(self):
        backend,namespaces,roles=self.fixture()
        for first in [(*roles[0][:3],1,False),(*roles[0][:4],True)]:
            db_connection=MagicMock();db_connection.execute.return_value.fetchall.side_effect=[namespaces,[first,*roles[1:]]]
            with self.assertRaises(TransitionBlocked):backend._enforced_observation(db_connection,{'generation':2})

    def test_live_effective_privilege_escape_is_refused(self):
        backend,namespaces,roles=self.fixture()
        connection=MagicMock();connection.execute.return_value.fetchall.side_effect=[namespaces,roles]
        with patch.object(privilege_closure,'privilege_closures_unsafe',return_value=True):
            with self.assertRaises(TransitionBlocked):backend._enforced_observation(connection,{'generation':2})

    def test_namespace_plan_requires_nonempty_exact_service_inventory(self):
        import copy
        backend,_,_=self.fixture()
        valid=backend.plan['pg_enforcement']['namespace_tables']
        PgBackend._namespace_contract(valid)
        first=next(iter(valid))
        variants=[{}, {key:value for key,value in valid.items() if key!=first}]
        for tables in ([],['x','x'],['z','a'],['bad;sql'],[None],['x']*513):
            value=copy.deepcopy(valid);value[first]['tables']=tables;variants.append(value)
        for service in ([],None,'not-a-service'):
            value=copy.deepcopy(valid);value[first]['service']=service;variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(TransitionBlocked):PgBackend._namespace_contract(value)


class AtomicInstallTests(unittest.TestCase):
    def test_batch_validates_every_entry_before_database_access(self):
        admin=MagicMock()
        valid=('s_book_memo_main','book-memo',['book_a'])
        for entries in ([],[valid,valid],[valid,('public','book-memo',['book_a'])],[valid,('s_portal_web_state','portal-web',['book_a'])]):
            with self.assertRaises(DatabaseUnavailable):enforcement.install_namespaces(admin,entries)
        admin.transaction.assert_not_called()

    def test_closure_failure_leaves_outer_transaction_exceptional(self):
        admin=MagicMock()
        admin.execute.return_value.fetchone.return_value=(True,)
        admin.execute.return_value.fetchall.return_value=[('book_a','book-memo')]
        entries=[('s_book_memo_main','book-memo',['book_a']),('s_book_memo_state','book-memo',['book_a'])]
        with patch.object(enforcement,'_install_namespace',return_value={'installed':True}) as install, patch.object(enforcement,'_revoke_settings_updates') as revoke_settings, patch.object(enforcement,'privilege_closures_unsafe',return_value=True):
            with self.assertRaises(DatabaseUnavailable):enforcement.install_namespaces(admin,entries)
        self.assertEqual(install.call_count,2)
        revoke_settings.assert_called_once_with(admin,['book_a'])
        self.assertIs(admin.transaction.return_value.__exit__.call_args.args[0],DatabaseUnavailable)

    def test_effective_closure_batches_roles_and_rejects_missing_duplicate_or_unsafe_rows(self):
        admin=MagicMock()
        identities=[('book_a','book-memo'),('book_b','book-memo')]
        for rows,unsafe in [([('book_a',False),('book_b',False)],False),
                            ([('book_a',False)],True),
                            ([('book_a',False),('book_a',False)],True),
                            ([('book_a',False),('book_b',True)],True),
                            ([('book_a',False),('book_b',None)],True)]:
            admin.reset_mock();admin.execute.return_value.fetchall.return_value=rows
            self.assertIs(enforcement.privilege_closures_unsafe(admin,identities),unsafe)
            admin.execute.assert_called_once_with(enforcement.PRIVILEGE_CLOSURE_SQL,
                (['book_a','book_b'],['book-memo','book-memo']))

    def test_settings_revoke_is_exact_update_scope_and_validates_roles(self):
        class SQLText(str):
            def format(self,*values):return SQLText(str(self).format(*values))
            def join(self,values):return SQLText(str(self).join(values))
        fake_sql=SimpleNamespace(SQL=SQLText,Identifier=lambda value:'"'+value+'"')
        admin=MagicMock();admin.execute.return_value.fetchall.return_value=[('setting',)]
        with patch.dict(sys.modules,{'psycopg':SimpleNamespace(sql=fake_sql)}):
            enforcement._revoke_settings_updates(admin,['book_a'])
        statements=[str(call.args[0]) for call in admin.execute.call_args_list][1:]
        self.assertEqual(statements,[
            'REVOKE UPDATE ON pg_catalog.pg_settings FROM PUBLIC',
            'REVOKE UPDATE ("setting") ON pg_catalog.pg_settings FROM PUBLIC',
            'REVOKE UPDATE ON pg_catalog.pg_settings FROM "book_a"',
            'REVOKE UPDATE ("setting") ON pg_catalog.pg_settings FROM "book_a"'])
        with self.assertRaises(DatabaseUnavailable):
            enforcement._revoke_settings_updates(admin,['bad;role'])

    def test_privilege_catalog_failure_cannot_be_accepted_as_proof(self):
        admin=MagicMock()
        # A failed catalog/effective-privilege query must never turn into proof.
        admin.execute.side_effect=RuntimeError('parameter_catalog_unavailable')
        with self.assertRaises(RuntimeError):
            privilege_closure.privilege_closures_unsafe(admin,[('book_a','book-memo')])

    def test_native_bundle_pure_closure_imports_without_runtime_package(self):
        import shutil
        import subprocess
        import tempfile
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'ha_control';target.mkdir()
            for source in (root/'infra/k8s/ha_control').glob('*.py'):
                shutil.copyfile(source,target/source.name)
            code="import sys;sys.path.insert(0,sys.argv[1]);from ha_control.pg import PgBackend;from ha_control.privilege_closure import privilege_closures_unsafe;assert 'ha_runtime' not in sys.modules"
            subprocess.run([sys.executable,'-I','-c',code,directory],check=True,capture_output=True)

    def test_single_namespace_uses_same_global_batch_boundary(self):
        admin=object()
        with patch.object(enforcement,'install_namespaces',return_value=[{'pass':True}]) as batch:
            self.assertEqual(enforcement.install_namespace(admin,'s_book_memo_main','book-memo',['book_a'],generation=2),{'pass':True})
        batch.assert_called_once_with(admin,[('s_book_memo_main','book-memo',['book_a'])],generation=2)


if __name__=='__main__':unittest.main()
