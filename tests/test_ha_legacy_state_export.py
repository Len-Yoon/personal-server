import copy
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))
from ha_runtime import legacy_state_export as export
from ha_runtime.seven_migration import MigrationRejected


BACKUP_PIN = '1' * 64
STAMP = '2026-11-26T01:00:00+00:00'
REQUEST = 'restart-nonlive'
PAYLOAD_PIN = '2' * 64


class LegacyStateExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / 'projection.json'
        self.output = self.root / 'out'
        self.projection = {
            'schema_version': 1,
            'scope': export.SCOPE,
            'backup_sha256': BACKUP_PIN,
            'services': {
                'crawler-worker': {
                    'archive': {
                        'schema_version': export.ARCHIVE_VERSION,
                        'articles': [{'title': '합성 기사', 'url': 'https://example.test/a'}],
                        'updated_at': STAMP,
                        'telegram_notifications_initialized': True,
                        'telegram_pending_articles': [],
                        'telegram_outbox': [{
                            'event_id': 'alert:sent', 'kind': 'alert', 'status': 'sent',
                            'articles': [{'url': 'https://example.test/a'}],
                            'article_urls': ['https://example.test/a'],
                            'removed_urls': ['https://example.test/a'],
                            'created_at': STAMP, 'sent_at': STAMP,
                        }],
                    },
                    'collection_status': {
                        'initialized': True, 'first_attempt_at': STAMP, 'last_attempt_at': STAMP,
                        'last_success_at': STAMP, 'last_failure_at': None, 'failures_total': 0,
                        'consecutive_failures': 0,
                    },
                },
                'book-memo': {'auth-rate-limit': {'synthetic-client': [STAMP]}, 'write-sessions': {}},
                'youtube-memo': {'auth-rate-limit': {}, 'write-sessions': {}},
                'homeops-executor': {'restart_claims': {
                    REQUEST: {
                        'payload_hash': PAYLOAD_PIN, 'created_at': 1795654800.25,
                        'state': 'completed', 'result': {'status': 'accepted'},
                    },
                    'expired-restart': {
                        'payload_hash': 'a' * 64, 'created_at': 1795654780,
                        'state': 'expired', 'result': None,
                    },
                }},
            },
        }

    def prepare(self, data=None, *, raw=None):
        raw = export.encoded(self.projection if data is None else data) if raw is None else raw
        self.source.write_bytes(raw)
        self.source.chmod(0o600)
        return export.sha(raw)

    def stage(self, pin=None, **kwargs):
        return export.stage_legacy_states(
            self.source, snapshot_sha256=pin if pin is not None else self.prepare(),
            backup_sha256=kwargs.pop('backup_sha256', BACKUP_PIN), output=self.output, **kwargs,
        )

    def test_legacy_formats_are_private_and_match_receipt(self):
        snapshot_pin = self.prepare()
        original = self.source.read_bytes()
        result = self.stage(snapshot_pin)
        receipt_raw = (self.output / 'legacy-state-receipt.json').read_bytes()
        receipt = json.loads(receipt_raw)
        self.assertEqual(export.sha(receipt_raw), result['receipt_sha256'])
        self.assertEqual(receipt['backup_sha256'], BACKUP_PIN)
        self.assertEqual(receipt['snapshot_sha256'], snapshot_pin)
        self.assertEqual(set(receipt['artifacts']), set(export.SERVICE_KEYS))
        for service, artifacts in receipt['artifacts'].items():
            for artifact in artifacts:
                path = self.output / service / artifact['file']
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(len(path.read_bytes()), artifact['bytes'])
                self.assertEqual(export.sha(path.read_bytes()), artifact['sha256'])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        crawler = self.projection['services']['crawler-worker']
        self.assertEqual(json.loads((self.output / 'crawler-worker/news_archive.json').read_bytes()),
                         crawler['archive'])
        status_path = self.output / 'crawler-worker/news_collection_status.json'
        auth_path = self.output / 'book-memo/auth-rate-limit-state.json'
        self.assertEqual(json.loads(auth_path.read_bytes()),
                         self.projection['services']['book-memo']['auth-rate-limit'])
        with patch.dict(sys.modules, {'app.services.ha_backend': SimpleNamespace(runtime=lambda: None)}):
            status_module = export._module('crawler-worker/app/services/news_collection_status.py')
            self.assertEqual(status_module.NewsCollectionStatusStore(status_path)._load(),
                             crawler['collection_status'])
            store_module = export._module('homeops-executor/app/services/idempotency.py')
        database = self.output / 'homeops-executor/restarts.sqlite3'
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            self.assertEqual(connection.execute(
                'SELECT request_id,state,result FROM restart_claims ORDER BY request_id'
            ).fetchall(), [('expired-restart', 'expired', None), (REQUEST, 'completed', '{"status":"accepted"}')])
        # A receipt with a current synthetic clock produces the legacy stored
        # result and never enters the new-operation branch of the reader.
        store = store_module.RestartStore(database, ttl=10**10)
        self.assertEqual(store.claim(REQUEST, PAYLOAD_PIN), {'status': 'accepted'})
        with self.assertRaisesRegex(store_module.ClaimError, 'restart_request_expired'):
            store.claim('expired-restart', 'a' * 64)
        self.assertEqual(self.source.read_bytes(), original)

    def test_refuses_mismatched_hash_and_nested_duplicate_keys(self):
        pin = self.prepare()
        with self.assertRaisesRegex(MigrationRejected, 'legacy_state_snapshot_binding_invalid'):
            self.stage(pin, backup_sha256='a' * 64)
        with self.assertRaises(MigrationRejected):
            self.stage('a' * 64)
        duplicate = self.source.read_bytes().replace(b'"initialized":true', b'"initialized":false,"initialized":true')
        with self.assertRaisesRegex(MigrationRejected, 'duplicate_source_json_key'):
            self.stage(self.prepare(raw=duplicate))
        self.assertFalse(self.output.exists())

    def test_refuses_unsettled_effects_and_homeops_claims(self):
        for change, reason in (
            (lambda data: data['crawler-worker']['archive']['telegram_outbox'][0].update(status='pending'),
             'legacy_crawler_effects_unsettled'),
            (lambda data: data['crawler-worker']['archive']['telegram_outbox'][0].update(status='uncertain'),
             'legacy_crawler_effects_unsettled'),
            (lambda data: data['crawler-worker']['archive']['telegram_pending_articles'].append({'url': 'x'}),
             'legacy_crawler_effects_unsettled'),
            (lambda data: data['homeops-executor']['restart_claims'][REQUEST].update(state='inflight', result=None),
             'legacy_restart_claim_unsettled'),
            (lambda data: data['homeops-executor']['restart_claims'][REQUEST].update(state='expired'),
             'legacy_restart_claim_result_invalid'),
        ):
            with self.subTest(reason=reason):
                specific = copy.deepcopy(self.projection)
                change(specific['services'])
                with self.assertRaisesRegex(MigrationRejected, reason):
                    self.stage(self.prepare(specific))
                self.assertFalse(self.output.exists())

    def test_refuses_unsupported_services_or_credentials(self):
        for service in ('portal-web', 'car-care-worker', 'effect-broker'):
            with self.subTest(service=service):
                specific = copy.deepcopy(self.projection)
                specific['services'][service] = {}
                with self.assertRaisesRegex(MigrationRejected, 'legacy_state_service_scope_invalid'):
                    self.stage(self.prepare(specific))
        self.projection['services']['homeops-executor']['restart_claims'][REQUEST]['result'] = {
            'refresh_token': 'synthetic',
        }
        with self.assertRaisesRegex(MigrationRejected, 'app_provider_credential_rejected'):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_disposition_required_for_noninvertible_session_hashes(self):
        session = 'e' * 64
        self.projection['services']['book-memo']['write-sessions'][session] = STAMP
        with self.assertRaisesRegex(MigrationRejected, 'legacy_memo_relogin_disposition_required'):
            self.stage()
        result = self.stage(allow_memo_relogin=True)
        self.assertEqual(result['memo_sessions_requiring_relogin'], {'book-memo': 1, 'youtube-memo': 0})
        for artifact in self.output.rglob('*.json'):
            self.assertNotIn(session.encode(), artifact.read_bytes())
        self.assertIn(session.encode(), self.source.read_bytes())

    def test_refuses_existing_destination_and_does_not_certify_partial_output(self):
        self.output.mkdir()
        marker = self.output / 'existing'
        marker.write_bytes(b'keep')
        with self.assertRaisesRegex(MigrationRejected, 'legacy_state_stage_exists'):
            self.stage()
        self.assertEqual(marker.read_bytes(), b'keep')
        self.output = self.root / 'failed-out'
        with patch.object(export, '_homeops_database', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.stage()
        self.assertFalse((self.output / 'legacy-state-receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
