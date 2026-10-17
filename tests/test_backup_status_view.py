import copy
import json
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

    def test_cleanup_summary_separate_from_proof_and_allowlisted(self):
        now = datetime(2026,10,13,1,tzinfo=timezone.utc)
        value = dict(schema_version=1, status="success", finished_at="2026-10-13T00:03:00Z",
                     kept_count=8, deleted_count=2, reclaimed_bytes=217)
        row = relay.retention_summary_for_display(json.dumps(value), now)
        self.assertEqual(row["retention_kept_count"], 8)
        self.assertEqual(row["retention_reclaimed_bytes"], 217)
        for key, invalid in [("deleted_count", True), ("reclaimed_bytes", -1),
                             ("finished_at", "2026-10-14T00:00:00Z"), ("path", "secret")]:
            bad = dict(value); bad[key] = invalid
            self.assertEqual(relay.retention_summary_for_display(json.dumps(bad), now), {})
        value.update(status="failure", kept_count=None, deleted_count=None, reclaimed_bytes=None)
        row = relay.retention_summary_for_display(json.dumps(value), now)
        self.assertIsNone(row["retention_deleted_count"])
        self.assertEqual(row["retention_status"], "failure")
        self.assertEqual(row["retention_reason"], "unknown")
        value["reason"] = "deletion_unconfirmed"
        self.assertEqual(relay.retention_summary_for_display(json.dumps(value), now)["retention_reason"], "deletion_unconfirmed")
        for invalid in ("private-path", "none", [], True):
            value["reason"] = invalid
            self.assertEqual(relay.retention_summary_for_display(json.dumps(value), now), {})

    def test_all_pvc_services_and_portal_publish_independent_cleanup_without_proof(self):
        now = datetime(2026,10,13,1,tzinfo=timezone.utc)
        value = dict(schema_version=1, status="failure", finished_at="2026-10-13T00:03:00Z",
                     kept_count=None, deleted_count=2, reclaimed_bytes=217)
        class API:
            def get_config_map(self, namespace, name):
                return {"data": {"retention_summary": json.dumps(value)}}
        rows = relay.service_backup_summaries(API(), now)
        for key in ("portal", "book", "youtube", "crawler"):
            with self.subTest(service=key):
                self.assertEqual(rows[key]["retention_status"], "failure")
                self.assertEqual(rows[key]["retention_deleted_count"], 2)
                self.assertEqual(rows[key]["retention_reclaimed_bytes"], 217)
                self.assertIsNone(rows[key]["retention_kept_count"])
                self.assertNotIn("backup_completed_at", rows[key])
                self.assertNotIn("evidence", rows[key])

    def test_valid_proof_exposes_only_display_fields(self):
        rows=self.view(self.evidence());self.assertTrue(rows['book']['verified']);self.assertFalse(rows['youtube']['verified'])
        self.assertNotIn('artifact_digest',rows['book']);self.assertNotIn('backup_id',rows['book'])

    def test_invalid_scopes_expiry_duplicates_and_partial_proof_fail_closed(self):
        for raw in [self.evidence(scope='youtube-memo'),self.evidence(evidence_expires_at='2026-10-13T00:30:00Z'),self.evidence()+'scope=book-memo\n',self.evidence(artifact_digest='bad'),self.evidence(restore_verified_at='2026-10-13T02:00:00Z'),self.evidence(encrypted='false'),self.evidence(backup_id='book-other-run')]:
            with self.subTest(raw=raw):self.assertFalse(self.view(raw)['book']['verified'])
        self.assertFalse(self.view(self.evidence(),'failed')['book']['verified'])

class CarBackupStatusViewTests(unittest.TestCase):
    now = datetime(2026,10,15,1,tzinfo=timezone.utc)

    def payload(self):
        return dict(schema_version=1,scope='car-care-db-only',observed_at='2026-10-15T00:59:00Z',
            last_attempt=dict(day='2026-10-15',status='skipped_unchanged',finished_at='2026-10-14T17:40:00Z'),
            proof=dict(backup_id='car-care-20261014T043446Z-'+'a'*16,artifact_sha256='a'*64,
                content_sha256='b'*64,snapshot_sha256='c'*64,finished_at='2026-10-14T04:34:59Z',
                restore_verified_at='2026-10-14T04:34:58Z',restore_verified=True,
                remote_upload_status='confirmed',status='success'),
            retention=dict(status='no_op',finished_at='2026-10-14T04:35:00Z',deleted_count=0,kept_count=2))

    def view(self, payload):
        class API:
            def get_config_map(self, namespace, name):
                if name != 'car-care-backup-state':raise OSError('unavailable')
                return dict(metadata=dict(name=name,namespace=namespace),data=dict(status_json=json.dumps(payload)))
        return relay.service_backup_summaries(API(),self.now)['car-care']

    def test_car_cleanup_accepts_old_evidence_and_preserves_confirmed_partial_totals(self):
        old = self.view(self.payload())
        self.assertIsNone(old['retention_reclaimed_bytes'])
        self.assertEqual(old['retention_kept_count'], 2)
        value = self.payload()
        value['retention'].update(status='failure', kept_count=None, deleted_count=1, reclaimed_bytes=1024)
        row = self.view(value)
        self.assertTrue(row['verified'])
        self.assertEqual(row['retention_deleted_count'], 1)
        self.assertEqual(row['retention_reclaimed_bytes'], 1024)
        self.assertIsNone(row['retention_kept_count'])
        self.assertEqual(row['restore_verified_at'], old['restore_verified_at'])
        for invalid in (True, -1, 2**53):
            value['retention']['reclaimed_bytes'] = invalid
            self.assertFalse(self.view(value)['verified'])

    def test_unchanged_attempt_preserves_original_proof_times(self):
        row=self.view(self.payload())
        self.assertTrue(row['verified'])
        self.assertEqual(row['last_attempt_status'],'skipped_unchanged')
        self.assertEqual(row['restore_verified_at'],'2026-10-14T04:34:58+00:00')
        self.assertEqual(row['backup_completed_at'],'2026-10-14T04:34:59+00:00')
        self.assertEqual(row['evidence_expires_at'],'2026-10-22T04:34:58+00:00')
        self.assertNotIn('backup_id',row)
        self.assertNotIn('artifact_sha256',row)

    def test_invalid_or_stale_reports_never_verify(self):
        mutations=[
            ('scope','portal'),('schema_version',True),('observed_at','2026-10-15T01:01:00Z'),
            ('observed_at','2026-10-15T00:49:00Z'),('observed_at','2026-10-15T00:59:00'),
            ('proof.remote_upload_status','uncertain'),('proof.restore_verified',False),
            ('proof.artifact_sha256','secret-value'),('proof.finished_at','2026-10-14T04:34:57Z'),
            ('proof.restore_verified_at','2026-10-15T01:01:00Z'),
            ('retention.deleted_count',True),('retention.deleted_count',-1),
            ('last_attempt.day','2026-10-16'),('last_attempt.status','secret-value')]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                p=self.payload()
                if '.' in key:parent,child=key.split('.');p[parent][child]=value
                else:p[key]=value
                self.assertFalse(self.view(p)['verified'])

    def test_missing_attempt_and_uncertain_attempt_never_verify(self):
        for attempt in [None,dict(day='2026-10-15',status='uncertain',finished_at=None)]:
            p=self.payload();p['last_attempt']=attempt
            self.assertFalse(self.view(p)['verified'])

    def test_missing_day_after_deadline_does_not_hide_valid_old_proof(self):
        p=self.payload();p['last_attempt']['day']='2026-10-14'
        p['last_attempt']['finished_at']='2026-10-14T04:35:00Z'
        row=self.view(p)
        self.assertFalse(row['verified']);self.assertEqual(row['status_reason'],'backup_missed')
        self.assertIn('restore_verified_at',row)

    def test_null_retention_is_valid_and_failed_cleanup_is_separate(self):
        p=self.payload();p['retention']=None
        self.assertTrue(self.view(p)['verified'])
        p=self.payload();p['retention']['status']='failure';p['last_attempt']['status']='retention_failed'
        row=self.view(p);self.assertTrue(row['verified']);self.assertEqual(row['retention_status'],'failure')


    def test_vehicle_alert_has_fixed_user_facing_target(self):
        self.assertEqual(relay._format_alert_target({'service':'car-care-worker'},'CarCareBackupMissed'),'차량 DB 백업')
        self.assertEqual(relay._format_alert_target({'service':'car-care-worker'},'CarCareBackupFailed'),'차량 DB 백업')
