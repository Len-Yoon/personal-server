from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('car_backup', ROOT / 'car-care-worker/tools/backup.py')
BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP)


class CarCareBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.database = self.base / 'car-care.sqlite3'
        self.credentials, self.scratch, self.evidence = [self.base / name for name in ('credentials', 'scratch', 'evidence')]
        for directory in (self.credentials, self.scratch, self.evidence):
            directory.mkdir(mode=0o700)
        for name in ('age-recipient', 'age-identity', 'rclone-config-passphrase'):
            (self.credentials / name).write_text('test-only')
        (self.credentials / 'rclone-config').write_text('RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        self.connection.execute('PRAGMA journal_mode=WAL')
        for name in BACKUP.TABLES:
            self.connection.execute(f'CREATE TABLE {name} (id INTEGER PRIMARY KEY, value TEXT)')
        self.connection.execute("INSERT INTO maintenance_records(value) VALUES ('committed-wal')")
        self.connection.commit()
        self.calls = []
        self.remote = {}

    def runner(self, args):
        self.calls.append(args)
        if args[0] == 'age':
            destination = Path(args[args.index('-o') + 1])
            raw = Path(args[-1]).read_bytes()
            destination.write_bytes(raw[4:] if '-d' in args else b'age:' + raw)
        elif 'copyto' in args:
            source, destination = args[-2:]
            if source.startswith('gdrive:'):
                Path(destination).write_bytes(self.remote[source])
            else:
                self.remote[destination] = Path(source).read_bytes()

    def execute(self, runner=None, **kwargs):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=True), patch.object(BACKUP.shutil, 'which', return_value='/fixture/bin'):
            return BACKUP.execute(self.database, self.credentials, self.scratch, self.evidence, runner or self.runner, **kwargs)

    def test_committed_wal_snapshot_and_remote_roundtrip_preserve_original(self):
        before = self.database.read_bytes()
        result = self.execute()
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['restore_verified'])
        self.assertEqual(self.database.read_bytes(), before)
        snapshot = self.base / 'download.sqlite3'
        snapshot.write_bytes(next(iter(self.remote.values()))[4:])
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('SELECT value FROM maintenance_records').fetchone(), ('committed-wal',))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual((self.credentials / 'rclone-config').read_text(), 'RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')
        for command in self.calls:
            self.assertNotIn('token', ' '.join(command))

    def test_download_corruption_retains_failed_evidence_and_cleans_plaintext(self):
        def corrupt(args):
            self.runner(args)
            if args[0] == 'rclone' and args[-2].startswith('gdrive:'):
                Path(args[-1]).write_bytes(b'broken')
        with self.assertRaises(ValueError):
            self.execute(corrupt)
        record = json.loads(next(self.evidence.glob("car-care-*.json")).read_text())
        self.assertEqual(record['status'], 'failure')
        self.assertEqual(record['stage'], 'download')
        self.assertTrue(record['remote_uploaded'])
        self.assertFalse(record['restore_verified'])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_provider_failure_does_not_leak_private_details_into_evidence(self):
        def fail(args):
            raise subprocess.CalledProcessError(1, args, stderr='private-token')
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(fail)
        evidence = next(self.evidence.glob("car-care-*.json")).read_text()
        self.assertNotIn('private-token', evidence)
        self.assertEqual(json.loads(evidence)['status'], 'failure')

    def test_unknown_oauth_table_is_rejected_without_external_call(self):
        self.connection.execute('CREATE TABLE oauth_tokens(token TEXT)')
        self.connection.commit()
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_source_symlink_rejected(self):
        link = self.base / 'linked'
        link.mkdir()
        self.database = link / 'car-care.sqlite3'
        self.database.symlink_to(self.base / 'car-care.sqlite3')
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_plaintext_credential_config_rejected(self):
        (self.credentials / 'rclone-config').write_text('[gdrive]\ntoken = private-token\n')
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_check_does_not_create_evidence_or_contact_remote(self):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=True), patch.object(BACKUP.shutil, 'which', return_value='/fixture/bin'):
            BACKUP.preflight(self.database, self.credentials, self.scratch, self.evidence)
        self.assertEqual(list(self.evidence.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_disk_scratch_is_rejected(self):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=False), self.assertRaises(ValueError):
            BACKUP.preflight(self.database, self.credentials, self.scratch, self.evidence)

    def test_public_evidence_directory_rejected(self):
        self.evidence.chmod(0o755)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_plaintext_refresh_is_rejected_and_records_uncertain_upload(self):
        def refresh(args):
            self.runner(args)
            if args[0] == 'rclone':
                Path(args[args.index('--config') + 1]).write_text('[gdrive]\ntoken = must-not-persist\n')
        with self.assertRaises(ValueError):
            self.execute(refresh)
        record = json.loads(next(self.evidence.glob("car-care-*.json")).read_text())
        self.assertEqual(record['status'], 'failure')
        self.assertEqual(record['remote_upload_status'], 'uncertain')
        self.assertFalse(record['restore_verified'])
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual((self.credentials / 'rclone-config').read_text(), 'RCLONE_ENCRYPT_V0:\nfixture-encrypted\n')

    def test_unchanged_skips_without_refreshing_restore_proof(self):
        first = self.execute(if_changed=True)
        calls = len(self.calls)
        second = self.execute(if_changed=True)
        self.assertEqual(second['status'], 'skipped_unchanged')
        self.assertEqual(len(self.calls), calls)
        self.assertFalse(second['restore_verified'])
        self.assertEqual(second['previous_restore_verified_at'], first['restore_verified_at'])
        self.assertNotIn('restore_verified_at', second)
        self.assertEqual(second['previous_backup_id'], first['backup_id'])

    def test_logical_fingerprint_ignores_vacuum_but_detects_committed_wal(self):
        first = self.execute(if_changed=True)
        self.connection.execute('VACUUM')
        self.assertEqual(self.execute(if_changed=True)['status'], 'skipped_unchanged')
        self.connection.execute("UPDATE maintenance_records SET value='changed'")
        self.connection.commit()
        result = self.execute(if_changed=True)
        self.assertEqual(result['status'], 'success')
        self.assertNotEqual(result['content_sha256'], first['content_sha256'])

    def test_go_without_change_detection_still_uploads_once(self):
        self.execute()
        self.execute()
        self.assertEqual(len(self.remote), 2)

    def test_uncertain_upload_blocks_future_attempt_without_retry(self):
        def uncertain(args):
            self.runner(args)
            if 'copyto' in args and not args[-2].startswith('gdrive:'):
                raise subprocess.TimeoutExpired(args, 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute(uncertain)
        calls = len(self.calls)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(len(self.calls), calls)
        records = [json.loads(p.read_text()) for p in self.evidence.glob('car-care-*.json')]
        self.assertEqual(len(records), 2)
        self.assertEqual({r['stage'] for r in records}, {'upload', 'unresolved_remote'})

    def test_lock_blocks_overlapping_execution(self):
        with BACKUP.POLICY.execution_lock(self.evidence):
            with self.assertRaises(BlockingIOError):
                self.execute()
        self.assertEqual(self.calls, [])

    def test_retention_plan_keeps_same_second_ties_and_latest_proof(self):
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        records = []
        for i in range(100):
            created = now - timedelta(days=i)
            backup_id = created.strftime('car-care-%Y%m%dT%H%M%SZ-') + f'{i:016x}'
            records.append(dict(scope='car-care-db-only', status='success', backup_id=backup_id,
                remote_upload_status='confirmed', restore_verified=True,
                restore_verified_at=created.isoformat(), finished_at=created.isoformat(),
                artifact_sha256='a'*64, snapshot_sha256='b'*64))
        tie = dict(records[35], backup_id=records[35]['backup_id'][:-16] + 'f'*16)
        records.append(tie)
        plan = BACKUP.POLICY.retention_plan(records, now=now)
        self.assertTrue(plan['delete'])
        self.assertIn(records[0]['backup_id'] + '.sqlite3.age', plan['keep'])
        self.assertEqual(plan['policy'], {'daily': 7, 'weekly': 4, 'monthly': 3})
        retained = set(plan['keep'])
        self.assertIn(tie['backup_id']+'.sqlite3.age', retained)
        self.assertEqual(tie['backup_id']+'.sqlite3.age' in retained,
                         records[35]['backup_id']+'.sqlite3.age' in retained)

    def test_retention_rejects_cross_scope_and_malformed_identity(self):
        first = self.execute()
        for key, value in [('scope', 'portal'), ('backup_id', '../car-care'), ('artifact_sha256', 'bad')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                BACKUP.POLICY.retention_plan([dict(first, **{key: value})])

    def test_upload_intent_is_durable_before_provider_called(self):
        def observe(args):
            if 'copyto' in args and not args[-2].startswith('gdrive:'):
                current = json.loads(next(self.evidence.glob('car-care-*.json')).read_text())
                self.assertEqual(current['remote_upload_status'], 'uncertain')
                self.assertEqual(current['stage'], 'upload')
            self.runner(args)
        self.execute(observe)

    def retain(self, **kwargs):
        with patch.object(BACKUP.HELPERS, 'memory_backed', return_value=True), patch.object(BACKUP.shutil, 'which', return_value='/fixture/bin'):
            return BACKUP.retain(self.database, self.credentials, self.scratch, self.evidence, **kwargs)

    def history(self):
        """A recent recoverable anchor and an expired ciphertext with real digests."""
        latest = self.execute()
        older = dict(latest)
        old_time = datetime.now(timezone.utc) - timedelta(days=200)
        older['backup_id'] = old_time.strftime('car-care-%Y%m%dT%H%M%SZ-') + '0'*16
        older['finished_at'] = older['restore_verified_at'] = old_time.isoformat()
        name = older['backup_id']+'.sqlite3.age'
        self.remote[BACKUP.REMOTE+'/'+name] = next(iter(self.remote.values()))
        BACKUP.POLICY.persist(self.evidence/(older['backup_id']+'.json'), older, initial=True)
        # Eight recent proofs ensure the old object is outside the seven-copy floor.
        for offset in range(1, 8):
            record = dict(latest)
            date = datetime.now(timezone.utc) - timedelta(days=offset)
            record['backup_id'] = date.strftime('car-care-%Y%m%dT%H%M%SZ-') + f'{offset:016x}'
            record['finished_at'] = record['restore_verified_at'] = date.isoformat()
            self.remote[BACKUP.REMOTE+'/'+record['backup_id']+'.sqlite3.age'] = next(iter(self.remote.values()))
            BACKUP.POLICY.persist(self.evidence/(record['backup_id']+'.json'), record, initial=True)
        return latest, older

    def retention_runner(self, args, **kwargs):
        if 'lsjson' in args:
            self.calls.append(args)
            items = [dict(Name=key.rsplit('/', 1)[1], Path=key.rsplit('/', 1)[1], IsDir=False,
                          Size=len(value), ID=key.rsplit('/', 1)[1], ModTime='2026-01-01T00:00:00+00:00')
                     for key, value in sorted(self.remote.items())]
            kwargs['stdout'].write(json.dumps(items).encode())
        elif 'deletefile' in args:
            self.calls.append(args)
            del self.remote[args[-1]]
        else:
            self.runner(args)

    def test_retention_plan_is_offline_and_execution_requires_exact_confirmation(self):
        self.history()
        count = len(self.calls)
        plan = self.retain()
        self.assertEqual(len(self.calls), count)
        self.assertTrue(plan['delete'])
        for options in [dict(), dict(expected_plan_sha256='bad', confirm_remote=BACKUP.REMOTE),
                        dict(expected_plan_sha256=plan['plan_sha256'], confirm_remote=BACKUP.REMOTE+'/../portal')]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.retain(delete=True, **options)
        self.assertEqual(len(self.calls), count)

    def test_retention_restores_anchor_verifies_candidate_and_preserves_last_copy(self):
        latest, older = self.history()
        plan = self.retain()
        result = self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                             confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertIn(older['backup_id']+'.sqlite3.age', result['deleted'])
        self.assertIn(BACKUP.REMOTE+'/'+latest['backup_id']+'.sqlite3.age', self.remote)
        self.assertGreaterEqual(len(self.remote), 7)
        self.assertEqual(self.retain()['delete'], [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_retention_unknown_object_and_digest_mismatch_block_delete(self):
        latest, older = self.history()
        plan = self.retain()
        remote = BACKUP.REMOTE+'/'+older['backup_id']+'.sqlite3.age'
        original = self.remote[remote]
        self.remote[remote] = b'corrupt'
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertFalse(any('deletefile' in args for args in self.calls))
        self.remote[remote] = original
        self.remote[BACKUP.REMOTE+'/other-service.sqlite3.age'] = original
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertFalse(any('deletefile' in args for args in self.calls))

    def test_retention_uncertain_delete_stops_later_upload_and_retry(self):
        self.history()
        plan = self.retain()
        def uncertain(args, **kwargs):
            self.retention_runner(args, **kwargs)
            if 'deletefile' in args:
                raise subprocess.TimeoutExpired(args, 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=uncertain)
        count = len(self.calls)
        with self.assertRaises(ValueError):
            self.execute()
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertEqual(len(self.calls), count)

    def test_stale_restore_proof_blocks_retention_without_remote_call(self):
        self.history()
        for path in self.evidence.glob('car-care-*.json'):
            record = json.loads(path.read_text())
            if record['status'] == 'success':
                date = datetime.now(timezone.utc)-timedelta(days=2)
                # The freshest archive is also aged so it remains valid evidence.
                if BACKUP.POLICY.created(record['backup_id']) > date:
                    new_id = date.strftime('car-care-%Y%m%dT%H%M%SZ-')+record['backup_id'][-16:]
                    record['backup_id'] = new_id
                    record['finished_at'] = record['restore_verified_at'] = date.isoformat()
                    path.unlink()
                    BACKUP.POLICY.persist(self.evidence/(new_id+'.json'), record, initial=True)
        plan = self.retain()
        count = len(self.calls)
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertEqual(len(self.calls), count)

    def test_inactive_templates_do_not_enable_or_delete(self):
        template = ROOT/'car-care-worker/tools/automation'
        service = (template/'car-care-backup.service.disabled').read_text()
        timer = (template/'car-care-backup.timer.disabled').read_text()
        self.assertIn('--go --if-changed', service)
        self.assertNotIn('--retention-go', service)
        self.assertNotIn('\n[Install]', service + timer)
        self.assertIn('Persistent=false', timer)
        self.assertIn('@APPROVED_ACTIVATION_MARKER@', service)

    def test_retention_duplicate_remote_identity_blocks_delete(self):
        self.history()
        plan = self.retain()
        def duplicate(args, **kwargs):
            if 'lsjson' in args:
                self.calls.append(args)
                key = next(iter(self.remote))
                name = key.rsplit('/', 1)[1]
                item = dict(Name=name, Path=name, IsDir=False, Size=len(self.remote[key]),
                            ID='fixture', ModTime='2026-01-01T00:00:00+00:00')
                kwargs['stdout'].write(json.dumps([item, item]).encode())
            else:
                self.retention_runner(args, **kwargs)
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=duplicate)
        self.assertFalse(any('deletefile' in args for args in self.calls))

    def test_remote_inventory_change_between_digest_and_delete_blocks_mutation(self):
        self.history()
        plan = self.retain()
        lists = []
        def changed(args, **kwargs):
            if 'lsjson' in args:
                lists.append(True)
                if len(lists) == 2:
                    key = next(iter(self.remote))
                    self.remote[key] += b'changed'
            self.retention_runner(args, **kwargs)
        with self.assertRaises(ValueError):
            self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                        confirm_remote=BACKUP.REMOTE, runner=changed)
        self.assertFalse(any('deletefile' in args for args in self.calls))

    def test_retention_single_last_copy_never_deleted(self):
        self.execute()
        plan = self.retain()
        self.assertEqual(plan['delete'], [])
        result = self.retain(delete=True, expected_plan_sha256=plan['plan_sha256'],
                             confirm_remote=BACKUP.REMOTE, runner=self.retention_runner)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(self.remote), 1)
        self.assertFalse(any('deletefile' in args for args in self.calls))

    def test_malformed_local_deletion_tombstone_is_rejected(self):
        first = self.execute()
        with self.assertRaises(ValueError):
            BACKUP.POLICY.retention_plan([dict(first, deleted=['../portal'])])


SCHEDULE_SPEC = importlib.util.spec_from_file_location('car_schedule', ROOT/'car-care-worker/tools/automation/schedule.py')
SCHEDULE = importlib.util.module_from_spec(SCHEDULE_SPEC)
SCHEDULE_SPEC.loader.exec_module(SCHEDULE)
POD_RUNNER_SPEC = importlib.util.spec_from_file_location('car_pod_runner', ROOT/'car-care-worker/tools/automation/pod_runner.py')
POD_RUNNER = importlib.util.module_from_spec(POD_RUNNER_SPEC)
POD_RUNNER_SPEC.loader.exec_module(POD_RUNNER)


class CarBackupScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.state.chmod(0o700)
        (self.state/'evidence').mkdir(mode=0o700)
        self.now = datetime.now(timezone.utc)
        self.image = 'docker.io/library/personal-server-portal-pvc-backup@sha256:'+'a'*64
        self.image_id = 'sha256:'+'b'*64
        self.desired = SCHEDULE.manifest(self.now.date(), self.image, self.image_id,
                                         self.state, self.state/'source', 'c'*64)
        self.created = []
        self.existing = None
        self.active = False
        test = self
        class FakeAPI:
            def get(self, name):
                return test.existing
            def active_backups(self):
                return test.active
            def create(self, pod):
                test.created.append(pod)
                test.existing = json.loads(json.dumps(pod))
                test.existing['metadata']['uid'] = 'fixture-pod'
                test.existing['status'] = dict(phase='Succeeded', containerStatuses=[dict(
                    restartCount=0, imageID='docker-pullable://fixture@'+test.image_id,
                    state=dict(terminated=dict(exitCode=0)))])
        self.api = FakeAPI()
        self.proof = dict(schema_version=1, scope='car-care-db-only', status='success',
            backup_id=self.now.strftime('car-care-%Y%m%dT%H%M%SZ-')+'d'*16,
            remote_upload_status='confirmed', restore_verified=True, finished_at=self.now.isoformat(),
            restore_verified_at=self.now.isoformat(), artifact_sha256='e'*64, snapshot_sha256='f'*64)
        BACKUP.POLICY.persist(self.state/'evidence'/(self.proof['backup_id']+'.json'), self.proof, initial=True)

    def run_schedule(self):
        return SCHEDULE.run(self.api, self.state, self.desired,
                            now=lambda: self.now-timedelta(seconds=1), sleep=lambda _: None, poll_seconds=0)

    def test_same_date_reentry_does_not_recreate_or_repeat_backup(self):
        first = self.run_schedule()
        second = self.run_schedule()
        self.assertEqual(first['status'], 'success')
        self.assertEqual(second['backup_id'], self.proof['backup_id'])
        self.assertEqual(len(self.created), 1)

    def test_lost_create_response_reconciles_once(self):
        create = self.api.create
        def lost(pod):
            create(pod)
            raise subprocess.TimeoutExpired('fixture', 30)
        self.api.create = lost
        self.assertEqual(self.run_schedule()['status'], 'success')
        self.assertEqual(len(self.created), 1)

    def test_missing_pod_after_intent_never_recreated(self):
        def absent(pod):
            self.created.append(pod)
            raise subprocess.TimeoutExpired('fixture', 30)
        self.api.create = absent
        with self.assertRaises(ValueError):
            self.run_schedule()
        with self.assertRaises(ValueError):
            self.run_schedule()
        self.assertEqual(len(self.created), 1)

    def test_other_backup_active_blocks_before_create(self):
        self.active = True
        with self.assertRaises(ValueError):
            self.run_schedule()
        self.assertEqual(self.created, [])
        self.assertEqual(list(self.state.glob('scheduled-*.json')), [])

    def test_unresolved_prior_day_blocks_new_date(self):
        BACKUP.POLICY.persist(self.state/'scheduled-2020-01-01.json', dict(status='uncertain'), initial=True)
        with self.assertRaises(ValueError):
            self.run_schedule()
        self.assertEqual(self.created, [])

    def test_failed_or_restarted_or_wrong_image_pod_blocks(self):
        for mutation in ('failed','restart','image','init','lifecycle','envFrom','args','volumeDevices',
                         'privileged','procMount','runAsUser','capabilities'):
            with self.subTest(mutation=mutation):
                self.created.clear()
                self.existing = None
                for path in self.state.glob('scheduled-*.json'):
                    path.unlink()
                create = type(self.api).create.__get__(self.api)
                def tamper(pod):
                    create(pod)
                    if mutation == 'failed': self.existing['status']['phase'] = 'Failed'
                    if mutation == 'restart': self.existing['status']['containerStatuses'][0]['restartCount'] = 1
                    if mutation == 'image': self.existing['status']['containerStatuses'][0]['imageID'] = 'sha256:bad'
                    if mutation == 'init': self.existing['spec']['initContainers'] = [dict(name='unknown')]
                    main = self.existing['spec']['containers'][0]
                    if mutation in ('lifecycle','envFrom','args','volumeDevices'): main[mutation] = ['unknown']
                    if mutation == 'privileged': main['securityContext']['privileged'] = True
                    if mutation == 'procMount': main['securityContext']['procMount'] = 'Unmasked'
                    if mutation == 'runAsUser': main['securityContext']['runAsUser'] = 0
                    if mutation == 'capabilities': main['securityContext']['capabilities']['add'] = ['SYS_ADMIN']
                self.api.create = tamper
                with self.assertRaises(ValueError):
                    self.run_schedule()
                self.assertEqual(len(self.created), 1)

    def test_remote_uncertain_evidence_is_not_a_new_success(self):
        path = next((self.state/'evidence').glob('*.json'))
        BACKUP.POLICY.persist(path, dict(self.proof, remote_upload_status='uncertain', status='failure'))
        with self.assertRaises(ValueError):
            self.run_schedule()
        result = json.loads(next(self.state.glob('scheduled-*.json')).read_text())
        self.assertEqual(result['status'], 'uncertain')

    def test_skip_preserves_old_restore_time_in_persistent_evidence(self):
        path = next((self.state/'evidence').glob('*.json'))
        proof = dict(self.proof, status='skipped_unchanged', restore_verified=False)
        proof.pop('restore_verified_at')
        BACKUP.POLICY.persist(path, proof)
        result = self.run_schedule()
        self.assertEqual(result['status'], 'skipped_unchanged')
        self.assertNotIn('restore_verified_at', result)

    def test_source_and_credentials_are_readonly_and_only_evidence_is_writable(self):
        pod = self.desired['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['restartPolicy'], 'Never')
        mounts = {v['name']: v for v in pod['containers'][0]['volumeMounts']}
        self.assertTrue(all(mounts[n]['readOnly'] for n in ('credentials','database','source')))
        self.assertEqual(mounts['state']['mountPath'], '/state/evidence')
        volumes = {v['name']:v for v in pod['volumes']}
        self.assertEqual(volumes['state']['hostPath']['path'], str(self.state/'evidence'))
        self.assertEqual(volumes['credentials']['secret']['secretName'], 'portal-pvc-backup-runtime')
        self.assertEqual(volumes['work']['emptyDir']['medium'], 'Memory')
        self.assertNotIn('--retention-go', json.dumps(pod))

    def test_weekly_restore_is_forced_on_sunday_or_after_seven_days(self):
        monday = datetime(2026,10,12, tzinfo=timezone.utc)
        record = dict(self.proof, backup_id='car-care-20261012T000000Z-'+'a'*16,
                      restore_verified_at=monday.isoformat(), finished_at=monday.isoformat())
        self.assertFalse(POD_RUNNER.force_restore([record], monday+timedelta(days=1)))
        self.assertTrue(POD_RUNNER.force_restore([record], monday+timedelta(days=6)))
        self.assertTrue(POD_RUNNER.force_restore([record], monday+timedelta(days=7)))
        self.assertTrue(POD_RUNNER.force_restore([], monday))

    def test_failure_alert_routes_through_fixed_existing_pipeline(self):
        payload = SCHEDULE.alert_payload(True, self.now)[0]
        self.assertEqual(payload['labels'], dict(alertname='CarCareBackupFailed', service='car-care-worker', severity='critical'))
        self.assertGreater(datetime.fromisoformat(payload['endsAt']), self.now)
        resolved = SCHEDULE.alert_payload(False, self.now)[0]
        self.assertEqual(resolved['endsAt'], self.now.isoformat())
        self.assertEqual(SCHEDULE.ALERT_PATH, '/api/v1/namespaces/monitoring/services/http:personal-server-monitoring-alertmanager:http-web/proxy/api/v2/alerts')

    def test_shared_lock_reused_and_no_service_restart_or_catchup(self):
        called = []
        class Lock:
            def __enter__(self): called.append('acquire')
            def __exit__(self, *args): called.append('release')
        with patch.object(SCHEDULE.SEQUENCE, 'local_lock', return_value=Lock()):
            with SCHEDULE.wait_shared_lock(): called.append('work')
        self.assertEqual(called, ['acquire','work','release'])
        folder = ROOT/'car-care-worker/tools/automation'
        service = (folder/'car-care-backup.service.tmpl').read_text()
        timer = (folder/'car-care-backup.timer.tmpl').read_text()
        self.assertIn('Restart=no', service)
        self.assertIn('05:30:00 Asia/Seoul', timer)
        self.assertIn('Persistent=false', timer)

    def test_notification_failure_does_not_rerun_successful_backup(self):
        from contextlib import nullcontext
        import sys
        class NotifyFailure:
            def notify(self, body):
                raise ValueError('fixture notification unavailable')
        argv = ['schedule.py','--go','--image',self.image,'--image-id',self.image_id]
        result = dict(day=self.now.astimezone(SCHEDULE.KST).date().isoformat(), status='success', backup_id=self.proof['backup_id'])
        with patch.object(sys,'argv',argv), patch.object(SCHEDULE,'Kubernetes',return_value=NotifyFailure()), \
             patch.object(SCHEDULE,'wait_shared_lock',return_value=nullcontext()), \
             patch.object(SCHEDULE,'finish_before_stop',return_value=nullcontext()), \
             patch.object(SCHEDULE,'state_directory',return_value=self.state), \
             patch.object(SCHEDULE,'canonical_source',return_value=('a'*40,'c'*64,self.state/'source')), \
             patch.object(SCHEDULE,'run',return_value=result) as run, patch('builtins.print'):
            self.assertEqual(SCHEDULE.main(), 1)
        run.assert_called_once()
        notification = json.loads(next(self.state.glob('notification-*.json')).read_text())
        self.assertEqual(notification['backup_status'], 'success')
        self.assertEqual(notification['alert_status'], 'failure')
        journal = json.loads(next(self.state.glob('scheduled-*.json')).read_text())
        self.assertEqual(journal['status'], 'success')
        self.assertEqual(journal['alert_status'], 'failure')


class CarBackupAlertTransportTests(unittest.TestCase):
    def process(self, banner=b'Starting to serve on 127.0.0.1:32123\n', *, stubborn=False):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, banner)
        os.close(write_fd)
        class Process:
            stdout = os.fdopen(read_fd, 'rb', buffering=0)
            returncode = None
            terminated = False
            killed = False
            def poll(self): return self.returncode
            def terminate(self): self.terminated = True
            def kill(self): self.killed = True
            def wait(self, timeout=None):
                if stubborn and not self.killed:
                    raise subprocess.TimeoutExpired('fixture', timeout)
                self.returncode = 0
                return 0
        process = Process()
        self.addCleanup(lambda: process.stdout.close())
        return process

    def response(self, status=200, data=b''):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return data[:limit]
        response = Response()
        response.status = status
        return response

    def test_json_post_is_fixed_local_scoped_and_process_is_reaped(self):
        process = self.process()
        requests = []
        def opener(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 10)
            return self.response()
        with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process) as start:
            SCHEDULE.send_alert([dict(labels=dict(service='car-care-worker'))], opener=opener)
        command = start.call_args.args[0]
        self.assertEqual(command[:5], ['sudo','-n','k3s','kubectl','proxy'])
        self.assertIn('--address=127.0.0.1', command)
        self.assertIn('--port=0', command)
        self.assertIn('--accept-paths=^'+SCHEDULE.re.escape(SCHEDULE.ALERT_PATH)+'$', command)
        self.assertEqual(requests[0].full_url, 'http://127.0.0.1:32123'+SCHEDULE.ALERT_PATH)
        self.assertEqual(requests[0].get_method(), 'POST')
        self.assertEqual(requests[0].get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(requests[0].data), [dict(labels=dict(service='car-care-worker'))])
        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(process.stdout.closed)

    def test_http_failure_is_not_retried_and_proxy_is_cleaned(self):
        process = self.process()
        calls = []
        def fail(request, timeout):
            calls.append(request)
            raise TimeoutError('fixture')
        with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process), self.assertRaises(TimeoutError):
            SCHEDULE.send_alert([], opener=fail)
        self.assertEqual(len(calls), 1)
        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, 0)

    def test_startup_invalid_host_or_oversized_banner_fails_before_http(self):
        for banner in (b'Starting to serve on 0.0.0.0:32123\n', b'x'*4097):
            with self.subTest(banner= banner[:40]):
                process = self.process(banner)
                with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process), \
                     patch.object(SCHEDULE,'build_opener') as opener, self.assertRaises(ValueError):
                    SCHEDULE.send_alert([], opener=opener)
                opener.assert_not_called()
                self.assertTrue(process.terminated)
                self.assertEqual(process.returncode, 0)

    def test_non_2xx_or_oversized_response_fails_and_reaps(self):
        for response in (self.response(status=503), self.response(data=b'x'*4097)):
            with self.subTest(status=response.status):
                process = self.process()
                with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process), self.assertRaises(ValueError):
                    SCHEDULE.send_alert([], opener=lambda *args, **kw: response)
                self.assertTrue(process.terminated)
                self.assertEqual(process.returncode, 0)

    def test_stubborn_child_is_killed_and_waited(self):
        process = self.process(stubborn=True)
        with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process):
            SCHEDULE.send_alert([], opener=lambda *args, **kw: self.response())
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, 0)

    def test_startup_timeout_cleans_child_without_post(self):
        process = self.process(b'')
        # Keep the writer open so startup waits instead of observing EOF.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        process.stdout.close()
        process.stdout = os.fdopen(read_fd, 'rb', buffering=0)
        class NotReady:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def register(self,*args): pass
            def select(self,timeout): return []
        with patch.object(SCHEDULE.subprocess,'Popen',return_value=process), \
             patch.object(SCHEDULE.selectors,'DefaultSelector',return_value=NotReady()), \
             patch.object(SCHEDULE,'build_opener') as opener, self.assertRaises(ValueError):
            SCHEDULE.send_alert([], opener=opener)
        opener.assert_not_called()
        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, 0)

    def test_actual_child_process_is_reaped_on_http_failure(self):
        import sys
        process = subprocess.Popen([sys.executable, '-u', '-c',
            "import time; print('Starting to serve on 127.0.0.1:32123'); time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
        self.addCleanup(cleanup)
        def failed(*args, **kwargs): raise TimeoutError('fixture')
        with patch.object(SCHEDULE.subprocess, 'Popen', return_value=process), self.assertRaises(TimeoutError):
            SCHEDULE.send_alert([], opener=failed)
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)

    def test_default_opener_disables_proxy_environment_and_redirects(self):
        process = self.process()
        with patch.object(SCHEDULE.subprocess,'Popen',return_value=process), \
             patch.object(SCHEDULE,'build_opener') as build:
            build.return_value.open.return_value = self.response()
            SCHEDULE.send_alert([])
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], SCHEDULE.NoAlertRedirect)
        with self.assertRaises(ValueError):
            handlers[1].redirect_request(None, None, None, None, None, None)
