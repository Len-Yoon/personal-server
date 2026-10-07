import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('evidence_export', ROOT/'infra/k8s/tools/export-portal-backup-evidence.py')
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)
NOW = datetime(2026, 10, 7, 5, tzinfo=timezone.utc)
RAW = ('schema_version=1\nscope=portal\nbackup_status=success\nencrypted=true\n'
       'backup_completed_at=2026-10-07T00:00:00Z\nrestore_status=success\n'
       'restore_verified_at=2026-10-07T00:00:01Z\nevidence_expires_at=2026-10-08T00:00:00Z\n'
       'backup_id=portal-20261007T000000Z-123.tar.age\nsource_runtime=k3s-pvc\n'
       'artifact_digest=sha256:' + 'a'*64 + '\nsource_digest=sha256:' + 'b'*64 +
       '\nrestore_check=sqlite_quick_check\nrestore_path_check=success\n').encode()

class EvidenceExportTests(unittest.TestCase):
    def test_valid_snapshot_preserves_every_byte_and_atomic_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td).resolve()/'evidence'
            MOD.publish(directory, RAW, NOW)
            path=directory/'portal-backup.evidence'; self.assertEqual(path.read_bytes(),RAW)
            old=path.stat().st_ino
            changed=RAW.replace(b'backup_id=portal-',b'backup_id=other-')
            MOD.publish(directory, changed, NOW)
            self.assertEqual(path.read_bytes(),changed); self.assertNotEqual(path.stat().st_ino,old)
            self.assertEqual(path.stat().st_mode&0o777,0o444)
            self.assertFalse(list(directory.glob('.evidence-*')))

    def test_stale_future_expired_and_wrong_runtime_are_rejected(self):
        for before, after in ((b'2026-10-08T00:00:00Z',b'2026-10-07T04:59:59Z'),
                              (b'2026-10-07T00:00:00Z',b'2026-10-08T00:00:00Z'),
                              (b'2026-10-07T00:00:00Z',b'2026-10-05T00:00:00Z'),
                              (b'source_runtime=k3s-pvc',b'source_runtime=compose-local')):
            with self.subTest(after=after), self.assertRaises(ValueError):
                MOD.validate(RAW.replace(before,after),NOW)

    def test_all_explicit_restore_and_digest_proofs_are_required(self):
        for key in (b'artifact_digest',b'source_digest',b'restore_check',b'restore_path_check'):
            raw=b'\n'.join(line for line in RAW.split(b'\n') if not line.startswith(key+b'='))
            with self.subTest(key=key),self.assertRaises(ValueError):MOD.validate(raw,NOW)

    def test_invalid_export_does_not_change_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td).resolve()/'evidence';MOD.publish(directory,RAW,NOW)
            with self.assertRaises(ValueError):MOD.publish(directory,RAW+b'unknown=secret\n',NOW)
            self.assertEqual((directory/'portal-backup.evidence').read_bytes(),RAW)

    def test_untrusted_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td).resolve();directory=base/'evidence';directory.mkdir(mode=0o755)
            other=base/'other';other.write_bytes(b'keep')
            target=directory/'portal-backup.evidence';target.symlink_to(other)
            with self.assertRaises(ValueError):MOD.publish(directory,RAW,NOW)
            self.assertEqual(other.read_bytes(),b'keep');target.unlink()
            os.chmod(directory,0o777)
            with self.assertRaises(ValueError):MOD.publish(directory,RAW,NOW)

    def test_private_umask_and_existing_private_leaf_remain_app_readable(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td).resolve()/'evidence'
            old=os.umask(0o077)
            try:MOD.publish(directory,RAW,NOW)
            finally:os.umask(old)
            self.assertEqual(directory.stat().st_mode&0o777,0o755)
            os.chmod(directory,0o700);MOD.publish(directory,RAW,NOW)
            self.assertEqual(directory.stat().st_mode&0o777,0o755)

    def test_existing_non_evidence_directory_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td).resolve()/'evidence';directory.mkdir(mode=0o700)
            (directory/'private-file').write_bytes(b'keep')
            with self.assertRaises(ValueError):MOD.publish(directory,RAW,NOW)
            self.assertEqual(directory.stat().st_mode&0o777,0o700)

    def test_relative_and_symlink_ancestor_destinations_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td).resolve();real=base/'real';real.mkdir()
            link=base/'link';link.symlink_to(real,target_is_directory=True)
            with self.assertRaises(ValueError):MOD.publish(link/'evidence',RAW,NOW)
            self.assertFalse((real/'evidence').exists())
            with self.assertRaises(ValueError):MOD.publish(Path('relative/evidence'),RAW,NOW)

    def test_configmap_document_requires_exact_fixed_source_and_small_payload(self):
        doc={'metadata':{'name':'portal-pvc-backup-evidence','namespace':'personal-server'},'data':{'evidence':RAW.decode()}}
        self.assertEqual(MOD.decode_configmap(json.dumps(doc).encode()),RAW)
        for patch in ({'metadata':{'name':'other','namespace':'personal-server'}},{'data':{'evidence':'x'*16385}}):
            bad={**doc,**patch}
            with self.assertRaises(ValueError):MOD.decode_configmap(json.dumps(bad).encode())

    def test_provider_failure_has_fixed_output_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td).resolve()/'missing';output=io.StringIO()
            with patch('sys.argv',['export','--output-dir',str(directory)]), patch.object(MOD.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['private'],stderr=b'credential')), contextlib.redirect_stdout(output):
                self.assertEqual(MOD.main(),1)
            self.assertEqual(output.getvalue(),'portal_backup_evidence_export=FAIL\n')
            self.assertFalse(directory.exists())

    def test_restore_job_has_no_live_data_volume_or_api_identity(self):
        spec=importlib.util.spec_from_file_location('restore_job',ROOT/'infra/k8s/tools/portal-backup-restore-job.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        job=module.render('portal-restore-check-test','local/backup@sha256:'+'a'*64)
        pod=job['spec']['template']['spec']
        self.assertIs(pod['automountServiceAccountToken'],False)
        self.assertEqual(job['spec']['backoffLimit'],0)
        self.assertFalse(any('persistentVolumeClaim' in v or 'hostPath' in v for v in pod['volumes']))
        secrets=[v['secret'] for v in pod['volumes'] if 'secret' in v]
        self.assertEqual(len(secrets),1)
        self.assertEqual({x['key'] for x in secrets[0]['items']},{'rclone-config','rclone-config-passphrase','age-identity'})
        container=pod['containers'][0]
        self.assertIs(container['securityContext']['readOnlyRootFilesystem'],True)
        self.assertEqual(container['securityContext']['capabilities']['drop'],['ALL'])
        self.assertTrue(all(m['readOnly'] for m in container['volumeMounts'] if m['name'] in ('code','evidence','credentials')))
        with self.assertRaises(ValueError):module.render('portal-restore-check-test','local/backup:latest')

if __name__ == '__main__':unittest.main()
