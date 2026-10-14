#!/usr/bin/env python3
"""Publish sanitized car backup evidence, monitor misses, clean verified old Pods."""
import argparse
from datetime import date, datetime, timedelta, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re

SPEC = importlib.util.spec_from_file_location('car_monitor_schedule', Path(__file__).with_name('schedule.py'))
SCHEDULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCHEDULE)
POLICY = SCHEDULE.POLICY
STATE_NAME = 'car-care-backup-state'
COMPLETE = {'success', 'skipped_unchanged', 'retention_failed'}


def require(ok):
    SCHEDULE.require(ok)


def local_journals(state, now):
    result = []
    for path in state.glob('scheduled-*.json'):
        match = re.fullmatch(r'scheduled-(\d{4}-\d{2}-\d{2})\.json', path.name)
        require(match is not None)
        day = date.fromisoformat(match[1])
        record = SCHEDULE.read_journal(path)
        require(record.get('schema_version') == 1 and record.get('day') == day.isoformat()
                and day <= now.astimezone(SCHEDULE.KST).date())
        require(record.get('status') in COMPLETE | {'uncertain'})
        if record['status'] in COMPLETE:
            require(record.get('stage') == 'complete' and isinstance(record.get('finished_at'), str))
        if record.get('finished_at') is not None:
            require(POLICY.timestamp(record['finished_at']) <= now
                    and POLICY.timestamp(record['finished_at']).astimezone(SCHEDULE.KST).date() >= day)
        result.append((day, path, record))
    return sorted(result, key=lambda item: item[0], reverse=True)


def verified_reference(journal, records, now):
    proof = next((r for r in records if r.get('backup_id') == journal.get('backup_id')), None)
    require(proof is not None and proof.get('operation', 'backup') == 'backup')
    if proof.get('status') == 'skipped_unchanged':
        actual = next((r for r in records if r.get('backup_id') == proof.get('previous_backup_id')), None)
        require(actual is not None and POLICY.successful([actual], now))
        require(proof.get('content_sha256') == actual.get('content_sha256'))
        require(proof.get('restore_verified') is False and proof.get('previous_restore_verified_at') ==
                actual.get('restore_verified_at', actual.get('finished_at')))
    else:
        require(POLICY.successful([proof], now))
    return proof


def snapshot(state, now):
    SCHEDULE.private(state)
    SCHEDULE.private(state/'evidence')
    records = POLICY.read_records(state/'evidence')
    journals = local_journals(state, now)
    for _, _, journal in journals:
        if journal['status'] in COMPLETE:
            verified_reference(journal, records, now)
    proofs = POLICY.successful(records, now)
    proof = None
    if proofs:
        raw = proofs[0]
        require(isinstance(raw.get('content_sha256'), str) and POLICY.SHA.fullmatch(raw['content_sha256']))
        proof = {key: raw[key] for key in ('backup_id','artifact_sha256','content_sha256','snapshot_sha256',
                                          'finished_at','restore_verified_at','restore_verified','remote_upload_status','status')}
        require(POLICY.timestamp(proof['finished_at']) <= now)
    attempt = None
    if journals:
        raw = journals[0][2]
        attempt = dict(day=raw['day'], status=raw['status'], finished_at=raw.get('finished_at'))
    retention = None
    retention_records = [r for r in records if r.get('operation') == 'retention' and r.get('finished_at')]
    if retention_records:
        raw = max(retention_records, key=lambda r: POLICY.timestamp(r['finished_at']))
        require(raw.get('status') in ('success','failure','no_op') and POLICY.timestamp(raw['finished_at']) <= now)
        deleted = raw.get('deleted', [])
        require(isinstance(deleted, list) and type(raw.get('kept_count')) is int and raw['kept_count'] >= 1)
        # Validate identities even when an earlier failed retention has partial deletes.
        POLICY.successful(records, now)
        retention = dict(status=raw['status'], finished_at=raw['finished_at'],
                         deleted_count=len(deleted), kept_count=raw['kept_count'])
    value = dict(schema_version=1, scope=POLICY.SCOPE, observed_at=now.isoformat(),
                 last_attempt=attempt, proof=proof, retention=retention)
    require(len(POLICY.canonical(value)) <= 16384)
    return value


def missed(state, value, now):
    activation = SCHEDULE.read_journal(state/'activation.json')
    activated = POLICY.timestamp(activation.get('activated_at'))
    require(activated <= now)
    local = now.astimezone(SCHEDULE.KST)
    first = activated.astimezone(SCHEDULE.KST)
    first_due = first.replace(hour=2, minute=30, second=0, microsecond=0)
    if first > first_due:
        first_due += timedelta(days=1)
    if local.hour < 9 or local < first_due.replace(hour=9, minute=0):
        return False
    attempt = value['last_attempt']
    return not (attempt and attempt['day'] == local.date().isoformat() and attempt['status'] in COMPLETE)


def missed_payload(is_missed, now):
    payload = SCHEDULE.alert_payload(is_missed, now)
    payload[0]['labels']['alertname'] = 'CarCareBackupMissed'
    payload[0]['annotations'] = dict(summary='차량 DB 정기 백업 실행 누락', description='09:00 KST 이후 당일 차량 백업 완료 증적 확인 필요')
    return payload


def publish(api, value):
    body = json.dumps(value, sort_keys=True, separators=(',', ':'))
    require(len(body.encode()) <= 16384)
    existing = api.call(['-n',SCHEDULE.NAMESPACE,'get','configmap',STATE_NAME,'-o','json'], missing=True)
    if existing is None:
        desired = dict(apiVersion='v1', kind='ConfigMap', metadata=dict(name=STATE_NAME, namespace=SCHEDULE.NAMESPACE),
                       data={'status_json': body})
        try:
            api.call(['-n',SCHEDULE.NAMESPACE,'create','-f','-','-o','json'], desired)
        except Exception:
            # Lost response can have created it; never issue another create.
            observed = api.call(['-n',SCHEDULE.NAMESPACE,'get','configmap',STATE_NAME,'-o','json'])
            require(observed.get('data', {}).get('status_json') == body)
        return
    meta = existing.get('metadata', {})
    require(existing.get('kind') == 'ConfigMap' and meta.get('name') == STATE_NAME
            and meta.get('namespace') == SCHEDULE.NAMESPACE and meta.get('resourceVersion') and meta.get('uid'))
    patch = [{'op':'test','path':'/metadata/uid','value':meta['uid']},
             {'op':'test','path':'/metadata/resourceVersion','value':meta['resourceVersion']}]
    if 'data' not in existing:
        patch.append({'op':'add','path':'/data','value':{'status_json':body}})
    else:
        require(isinstance(existing['data'], dict))
        patch.append({'op':'add','path':'/data/status_json','value':body})
    api.call(['-n',SCHEDULE.NAMESPACE,'patch','configmap',STATE_NAME,'--type=json','-p',json.dumps(patch),'-o','json'])


def cleanup(api, state, now):
    """Only old successful Pod/journal pairs. All backup evidence is preserved."""
    journals = local_journals(state, now)
    records = POLICY.read_records(state/'evidence')
    POLICY.require_resolved(records)
    if not journals:
        return 0
    latest = journals[0][0]
    count = 0
    for day, path, journal in reversed(journals):
        if day == latest or journal['status'] not in ('success','skipped_unchanged') or (now.astimezone(SCHEDULE.KST).date()-day).days <= 30:
            continue
        require(journal.get('stage') == 'complete' and POLICY.timestamp(journal['finished_at']) < now-timedelta(days=30))
        verified_reference(journal, records, now)
        source_hash = journal.get('source_sha256')
        require(isinstance(source_hash, str) and POLICY.SHA.fullmatch(source_hash))
        name = 'car-care-backup-'+day.strftime('%Y%m%d')
        original = POLICY.canonical(journal)
        journal_hash = hashlib.sha256(original).hexdigest()
        intent_path = state/('cleanup-'+day.isoformat()+'.json')
        intent = SCHEDULE.read_journal(intent_path) if intent_path.exists() else None
        uid = journal.get('pod_uid')
        require(isinstance(uid, str) and re.fullmatch(r'[a-zA-Z0-9-]{1,64}', uid))
        if intent is not None:
            require(intent.get('pod_uid') == uid and intent.get('name') == name and intent.get('journal_sha256') == journal_hash)
        pod = api.get(name)
        if pod is None:
            if intent is None:
                continue  # No historical verified identity: preserve journal.
            require(intent.get('status') in ('verified','deleting','uncertain','deleted'))
            require(POLICY.canonical(SCHEDULE.read_journal(path)) == original)
            path.unlink()
            intent.update(status='deleted', finished_at=now.isoformat())
            POLICY.persist(intent_path,intent)
            return count+1
        if intent is not None and intent.get('status') == 'uncertain':
            raise ValueError('car cleanup unresolved')
        meta = pod.get('metadata', {})
        require(isinstance(uid, str) and re.fullmatch(r'[a-zA-Z0-9-]{1,64}', uid) and meta.get('uid') == uid)
        image_id = meta.get('annotations', {}).get('personal-server/image-id')
        desired = SCHEDULE.manifest(day, journal['image'], image_id, state, state/('source-'+source_hash), source_hash)
        if journal.get('source_commit'):
            desired['metadata']['annotations']['personal-server/source-commit'] = journal['source_commit']
        require(pod.get('kind') == 'Pod' and SCHEDULE.valid_pod(desired, pod))
        status = pod.get('status', {})
        containers = status.get('containerStatuses', [])
        require(status.get('phase') == 'Succeeded' and len(containers) == 1 and containers[0].get('restartCount') == 0
                and containers[0].get('imageID','').endswith(image_id)
                and containers[0].get('state',{}).get('terminated',{}).get('exitCode') == 0)
        if intent is not None and intent.get('status') == 'deleting':
            continue
        if intent is None:
            intent = dict(name=name,pod_uid=uid,journal_sha256=journal_hash,status='verified')
            POLICY.persist(intent_path,intent,initial=True)
        if meta.get('deletionTimestamp'):
            continue
        # Durable mutation intent precedes DELETE: a crash after acceptance
        # must leave a reconcile-only record, never another submission.
        intent.update(status='deleting')
        POLICY.persist(intent_path,intent)
        # Same UID precondition and fixed name. A lost delete response is only reconciled.
        try:
            api.delete_pod(name, uid)
        except Exception:
            if api.get(name) is not None:
                intent.update(status='uncertain')
                POLICY.persist(intent_path,intent)
                raise ValueError('car cleanup unresolved')
        if api.get(name) is not None:
            return count  # One submitted DELETE is the per-run cap, even while terminating.
        require(POLICY.canonical(SCHEDULE.read_journal(path)) == original)
        path.unlink()
        intent.update(status='deleted',finished_at=now.isoformat())
        POLICY.persist(intent_path,intent)
        count += 1
        break  # At most one Pod/journal pair per five-minute run.
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go', action='store_true', required=True)
    parser.parse_args()
    os.umask(0o077)
    api = SCHEDULE.Kubernetes()
    state = SCHEDULE.state_directory()
    # A busy producer is deferred, not reported as missed or newly healthy.
    cleaned = True
    try:
        with SCHEDULE.SEQUENCE.local_lock(), POLICY.execution_lock(state/'evidence'):
            now = datetime.now(timezone.utc)
            value = snapshot(state, now)
            is_missed = missed(state, value, now)
            try:
                cleanup(api, state, now)
            except Exception:
                cleaned = False
    except BlockingIOError:
        POLICY.persist(state/'monitor-result.json',dict(observed_at=datetime.now(timezone.utc).isoformat(),
            publish_status='deferred',alert_status='not_attempted',cleanup_status='deferred',missed=None))
        print('car_care_monitor=DEFERRED producer_active=true')
        return 0
    # Publish the immutable observation after releasing locks; producers wait
    # only for local snapshot/cleanup and cannot race its timestamp capture.
    published = alerted = True
    try:
        publish(api, value)
    except Exception:
        published = False
    try:
        api.notify(missed_payload(is_missed, now))
    except Exception:
        alerted = False
    POLICY.persist(state/'monitor-result.json', dict(observed_at=now.isoformat(), publish_status='success' if published else 'failure',
        alert_status='success' if alerted else 'failure', cleanup_status='success' if cleaned else 'failure', missed=is_missed))
    print('car_care_monitor='+('PASS' if published and alerted and cleaned else 'FAIL'))
    return 0 if published and alerted and cleaned else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('car_care_monitor=FAIL')
        raise SystemExit(1)
