import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SPEC=importlib.util.spec_from_file_location('backup_view_relay',Path(__file__).resolve().parents[1]/'sre-telegram-relay/app/main.py')
relay=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=relay;SPEC.loader.exec_module(relay)


class BackupStatusViewTests(unittest.TestCase):
    def evidence(self, scope='book-memo', **overrides):
        values={'schema_version':'1','scope':scope,'backup_status':'success','restore_status':'success','encrypted':'true','source_runtime':'k3s-pvc','restore_path_check':'success','restore_check':'sqlite_quick_check','backup_completed_at':'2026-10-13T00:00:00Z','restore_verified_at':'2026-10-13T00:01:00Z','evidence_expires_at':'2026-10-14T00:01:00Z','artifact_digest':'sha256:'+'a'*64,'source_digest':'sha256:'+'b'*64,'backup_id':'book-20261013T000000Z-42'}
        values.update(overrides);return '\n'.join(k+'='+v for k,v in values.items())+'\n'

    def view(self, raw, status='completed'):
        class API:
            def get_config_map(self,namespace,name):
                if name != 'book-pvc-backup-state':raise OSError('unavailable')
                return {'data':{'status':status,'stage':'completed','lock_run_id':'','evidence':raw,'run_id':'20261013T000000Z-42','completed_at':'2026-10-13T00:02:00Z'}}
        return relay.service_backup_summaries(API(),datetime(2026,10,13,1,tzinfo=timezone.utc))

    def test_valid_proof_exposes_only_display_fields(self):
        rows=self.view(self.evidence());self.assertTrue(rows['book']['verified']);self.assertFalse(rows['youtube']['verified'])
        self.assertNotIn('artifact_digest',rows['book']);self.assertNotIn('backup_id',rows['book'])

    def test_invalid_scopes_expiry_duplicates_and_partial_proof_fail_closed(self):
        for raw in [self.evidence(scope='youtube-memo'),self.evidence(evidence_expires_at='2026-10-13T00:30:00Z'),self.evidence()+'scope=book-memo\n',self.evidence(artifact_digest='bad'),self.evidence(restore_verified_at='2026-10-13T02:00:00Z'),self.evidence(encrypted='false'),self.evidence(backup_id='book-other-run')]:
            with self.subTest(raw=raw):self.assertFalse(self.view(raw)['book']['verified'])
        self.assertFalse(self.view(self.evidence(),'failed')['book']['verified'])
