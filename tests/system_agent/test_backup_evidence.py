import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from tests._test_support import prepare_service_import


class BackupEvidenceTests(unittest.TestCase):
    def test_recent_directory_is_not_a_verified_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            prepare_service_import('system-agent')
            root=Path(temp)
            (root/'backups/recent').mkdir(parents=True)
            metrics=importlib.import_module('app.services.metrics').collect_metrics(data_root=root)
            self.assertFalse(metrics['backup']['verified'])
            self.assertEqual(metrics['backup']['status'],'warning')
            self.assertEqual(metrics['backup']['archive_status'],'backup_recent')

    def test_fresh_encrypted_restore_evidence_and_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            prepare_service_import('system-agent')
            root=Path(temp)
            path=root/'evidence'
            now=datetime.now(timezone.utc)
            stamp=lambda value:value.strftime('%Y-%m-%dT%H:%M:%SZ')
            values={'schema_version':'1','scope':'portal','backup_status':'success','encrypted':'true','backup_completed_at':stamp(now-timedelta(minutes=10)),'restore_status':'success','restore_verified_at':stamp(now-timedelta(minutes=5)),'evidence_expires_at':stamp(now+timedelta(hours=1)),'backup_id':'opaque-test','source_runtime':'k3s-pvc'}
            def write():path.write_text('\n'.join(f'{k}={v}' for k,v in values.items())+'\n')
            write()
            with patch.dict(os.environ,{'BACKUP_EVIDENCE_PATH':str(path)}):
                collect=importlib.import_module('app.services.metrics').collect_metrics
                self.assertTrue(collect(data_root=root)['backup']['verified'])
                values['encrypted']='false';write()
                self.assertFalse(collect(data_root=root)['backup']['verified'])
                values['encrypted']='true';values['evidence_expires_at']=stamp(now-timedelta(seconds=1));write()
                self.assertFalse(collect(data_root=root)['backup']['verified'])
