#!/usr/bin/env python3
"""Host-side scheduled car backup; secrets are projected only into its fixed Pod."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import selectors
import stat
import subprocess
import sys
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
FILES = ('car-care-worker/tools/backup.py', 'car-care-worker/tools/backup_policy.py',
         'car-care-worker/tools/automation/pod_runner.py', 'infra/k8s/tools/portal-backup-restore-check.py')
SPEC = importlib.util.spec_from_file_location('car_schedule_policy', ROOT/'car-care-worker/tools/backup_policy.py')
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)
LOCK_SPEC = importlib.util.spec_from_file_location('car_shared_backup_lock', ROOT/'infra/k8s/tools/pvc-backup-sequence.py')
SEQUENCE = importlib.util.module_from_spec(LOCK_SPEC)
sys.modules[LOCK_SPEC.name] = SEQUENCE
LOCK_SPEC.loader.exec_module(SEQUENCE)
NAMESPACE = 'personal-server'
LABEL = 'personal-server/backup-stage'
KST = ZoneInfo('Asia/Seoul')
ALERT_PATH = '/api/v1/namespaces/monitoring/services/http:personal-server-monitoring-alertmanager:http-web/proxy/api/v2/alerts'


def require(ok):
    if not ok:
        raise ValueError('car schedule validation failed')


def private(path):
    info = path.stat()
    require(path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700)


def state_directory():
    base = Path.home()/'.local/state/personal-server'
    # The shared lock validates every parent, including symlinks and permissions.
    private(base)
    state = base/'car-care-backup'
    state.mkdir(mode=0o700, exist_ok=True)
    private(state)
    evidence = state/'evidence'
    evidence.mkdir(mode=0o700, exist_ok=True)
    private(evidence)
    return state


@contextmanager
def wait_shared_lock(*, timeout=10800, sleep=time.sleep, clock=time.monotonic):
    deadline = clock()+timeout
    while True:
        lock = SEQUENCE.local_lock()
        try:
            lock.__enter__()
        except BlockingIOError:
            require(clock() < deadline)
            sleep(30)
        else:
            try:
                yield
            finally:
                lock.__exit__(None, None, None)
            return


@contextmanager
def finish_before_stop():
    # Keep the shared host lock while the non-restarting Pod reaches terminal
    # state. A service stop must not release it while an upload still runs.
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: None)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def canonical_source(state, command=subprocess.run):
    def git(*args):
        result = command(['git', '-C', str(ROOT), *args], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, check=True, timeout=30)
        return result.stdout
    commit = git('rev-parse', 'HEAD').decode().strip()
    require(re.fullmatch('[a-f0-9]{40}', commit))
    digest = hashlib.sha256()
    blobs = {}
    for relative in FILES:
        data = git('show', commit+':'+relative)
        require(b'\r' not in data and (ROOT/relative).read_bytes().replace(b'\r\n', b'\n') == data)
        digest.update(relative.encode()+b'\0'+data)
        blobs[relative] = data
    source_hash = digest.hexdigest()
    folder = state/('source-'+source_hash)
    folder.mkdir(mode=0o700, exist_ok=True)
    private(folder)
    for relative, data in blobs.items():
        target = folder/relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            require(not target.is_symlink() and target.read_bytes() == data)
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
    return commit, source_hash, folder


def manifest(day, image, image_id, state, source, source_hash):
    require(re.fullmatch(r'docker.io/library/personal-server-portal-pvc-backup@sha256:[a-f0-9]{64}', image))
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image_id))
    return dict(apiVersion='v1', kind='Pod', metadata=dict(
        name='car-care-backup-'+day.strftime('%Y%m%d'), namespace=NAMESPACE,
        labels={LABEL: 'car-care', 'personal-server/backup-date': day.isoformat()},
        annotations={'personal-server/source-sha256': source_hash, 'personal-server/image-id': image_id}),
        spec=dict(restartPolicy='Never', activeDeadlineSeconds=1200, automountServiceAccountToken=False,
          terminationGracePeriodSeconds=30,
          securityContext=dict(runAsNonRoot=True, runAsUser=1000, runAsGroup=1000, fsGroup=1000,
                               seccompProfile=dict(type='RuntimeDefault')),
          containers=[dict(name='backup', image=image, imagePullPolicy='Never',
            command=['python3', '-I', '-B', '/repo/car-care-worker/tools/automation/pod_runner.py'],
            env=[dict(name='PYTHONDONTWRITEBYTECODE', value='1')],
            securityContext=dict(allowPrivilegeEscalation=False, readOnlyRootFilesystem=True,
                                 capabilities=dict(drop=['ALL'])),
            resources=dict(requests=dict(cpu='25m', memory='32Mi'), limits=dict(cpu='250m', memory='256Mi')),
            volumeMounts=[dict(name=n, mountPath=p, readOnly=True) for n,p in
                          [('database','/input'),('credentials','/credentials'),('source','/repo')]]+
                         [dict(name='work', mountPath='/work'),dict(name='state', mountPath='/state/evidence')])],
          volumes=[dict(name='database', hostPath=dict(path='/mnt/c/personal-server/data/car-care', type='Directory')),
                   dict(name='credentials', secret=dict(secretName='portal-pvc-backup-runtime', optional=False,
                      defaultMode=0o440, items=[dict(key=n,path=n) for n in
                      ('age-recipient','age-identity','rclone-config','rclone-config-passphrase')])),
                   dict(name='source', hostPath=dict(path=str(source), type='Directory')),
                   dict(name='state', hostPath=dict(path=str(state/'evidence'), type='Directory')),
                   dict(name='work', emptyDir=dict(medium='Memory', sizeLimit='64Mi'))]))


def subset(wanted, actual):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k,v in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and len(wanted) == len(actual) and all(subset(x,y) for x,y in zip(wanted,actual))
    return wanted == actual


class Kubernetes:
    def call(self, args, body=None, missing=False):
        result = subprocess.run(['sudo','-n','k3s','kubectl','--request-timeout=20s',*args],
            input=json.dumps(body) if body is not None else None, text=True, capture_output=True, timeout=30)
        if missing and result.returncode and 'NotFound' in result.stderr:
            return None
        require(result.returncode == 0)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def get(self, name):
        return self.call(['-n',NAMESPACE,'get','pod',name,'-o','json'], missing=True)

    def create(self, pod):
        return self.call(['-n',NAMESPACE,'create','-f','-','-o','json'], pod)

    def notify(self, payload):
        send_alert(payload)

    def active_backups(self):
        jobs = self.call(['-n',NAMESPACE,'get','jobs','-o','json'])['items']
        pods = self.call(['-n',NAMESPACE,'get','pods','-o','json'])['items']
        return any(j.get('status',{}).get('active',0) for j in jobs if 'backup' in j['metadata']['name']) or any(
            'backup' in p['metadata']['name'] and p.get('status',{}).get('phase') not in ('Succeeded','Failed') for p in pods)


class NoAlertRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('car alert redirect rejected')


def send_alert(payload, *, opener=None):
    """One JSON POST through a short-lived, loopback-only, path-limited proxy."""
    command = ['sudo','-n','k3s','kubectl','proxy','--port=0','--address=127.0.0.1',
               '--accept-hosts=^127\\.0\\.0\\.1$',
               '--accept-paths=^'+re.escape(ALERT_PATH)+'$',
               '--reject-methods=^(GET|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)$']
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic()+10
        banner = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b'\n' not in banner:
                remaining = deadline-time.monotonic()
                require(remaining > 0 and process.poll() is None)
                require(selector.select(remaining))
                chunk = os.read(process.stdout.fileno(), 256)
                require(chunk)
                banner.extend(chunk)
                require(len(banner) <= 4096)
        match = re.fullmatch(rb'Starting to serve on 127\.0\.0\.1:([0-9]{1,5})\r?\n', bytes(banner))
        require(match is not None)
        port = int(match[1])
        require(1 <= port <= 65535)
        request = Request('http://127.0.0.1:'+str(port)+ALERT_PATH,
                          data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type':'application/json'}, method='POST')
        # Ignore ambient HTTP_PROXY and refuse redirects outside this fixed URL.
        opener = opener or build_opener(ProxyHandler({}), NoAlertRedirect()).open
        with opener(request, timeout=10) as response:
            require(200 <= response.status < 300)
            require(len(response.read(4097)) <= 4096)
    finally:
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            process.stdout.close()



def alert_payload(failed, now):
    return [dict(labels=dict(alertname='CarCareBackupFailed', service='car-care-worker', severity='critical'),
        annotations=dict(summary='차량 DB 백업 또는 복원 검증 실패', description='차량 백업 실행 증적과 Pod 상태 확인 필요'),
        startsAt=(now-timedelta(minutes=1)).isoformat(),
        endsAt=(now+timedelta(hours=24) if failed else now).isoformat())]


def valid_pod(desired, actual):
    spec = actual.get('spec', {})
    main = spec.get('containers', [{}])[0]
    security = main.get('securityContext', {})
    allowed_security = set(desired['spec']['containers'][0]['securityContext']) | {
        'runAsNonRoot','runAsUser','runAsGroup','privileged','procMount'}
    return (subset(desired['metadata'], actual.get('metadata')) and subset(desired['spec'], spec)
            and not any(main.get(key) for key in ('envFrom','args','lifecycle','volumeDevices','stdin','tty'))
            and set(security) <= allowed_security
            and security.get('runAsUser') in (None,1000) and security.get('runAsGroup') in (None,1000)
            and security.get('runAsNonRoot') in (None,True)
            and security.get('procMount') in (None,'Default') and not security.get('privileged')
            and set(security.get('capabilities', {})) <= {'drop'}
            and not spec.get('initContainers') and not spec.get('ephemeralContainers')
            and not any(spec.get(key) for key in ('hostNetwork','hostPID','hostIPC','shareProcessNamespace')))


def read_journal(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 1024*1024)
        value = json.load(stream)
    require(isinstance(value, dict))
    return value


def run(api, state, desired, *, now=lambda: datetime.now(timezone.utc), sleep=time.sleep, poll_seconds=15):
    day = desired['metadata']['labels']['personal-server/backup-date']
    journal = state/('scheduled-'+day+'.json')
    # A prior unfinished intent can represent an upload even after Pod removal.
    for path in state.glob('scheduled-*.json'):
        require(not path.is_symlink())
        prior = read_journal(path)
        require(path == journal or prior.get('status') in ('success','skipped_unchanged'))
    existing = api.get(desired['metadata']['name'])
    if journal.exists():
        record = read_journal(journal)
        require(record.get('source_sha256') == desired['metadata']['annotations']['personal-server/source-sha256'])
        require(record.get('image') == desired['spec']['containers'][0]['image'])
        if record.get('status') in ('success','skipped_unchanged'):
            return record
        require(existing is not None)  # Never recreate a lost/failed operation.
    else:
        require(existing is None and not api.active_backups())
        record = dict(schema_version=1, day=day, status='uncertain', stage='create_intent',
                      source_sha256=desired['metadata']['annotations']['personal-server/source-sha256'],
                      image=desired['spec']['containers'][0]['image'],
                      source_commit=desired['metadata']['annotations'].get('personal-server/source-commit'),
                      started_at=now().isoformat())
        POLICY.persist(journal, record, initial=True)
        try:
            api.create(desired)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass  # Read back once; never issue a second create after a lost response.
        existing = api.get(desired['metadata']['name'])
    require(existing is not None and valid_pod(desired, existing))
    uid = existing['metadata'].get('uid')
    require(uid and (not record.get('pod_uid') or record['pod_uid'] == uid))
    record.update(pod_uid=uid, stage='pod_started')
    POLICY.persist(journal, record)
    deadline = time.monotonic()+1500
    for _ in range(102):
        require(existing['metadata']['uid'] == uid and valid_pod(desired, existing))
        status = existing.get('status',{})
        if status.get('phase') in ('Succeeded','Failed'):
            break
        require(time.monotonic() < deadline)
        sleep(poll_seconds)
        existing = api.get(desired['metadata']['name'])
        require(existing is not None)
    else:
        raise ValueError('car schedule validation failed')
    containers = status.get('containerStatuses',[])
    require(len(containers)==1 and containers[0].get('restartCount')==0
            and containers[0].get('imageID','').endswith(desired['metadata']['annotations']['personal-server/image-id']))
    require(status['phase']=='Succeeded' and containers[0].get('state',{}).get('terminated',{}).get('exitCode') == 0)
    records = POLICY.read_records(state/'evidence')
    POLICY.require_resolved(records)
    attempts = [r for r in records if r.get('finished_at') and POLICY.timestamp(r['finished_at']) >= POLICY.timestamp(record['started_at'])]
    require(len(attempts)==1)
    proof = attempts[0]
    require(proof.get('status') in ('success','skipped_unchanged'))
    require(proof['status']!='success' or proof.get('restore_verified') is True)
    record.update(status=proof['status'], stage='complete', backup_id=proof['backup_id'], finished_at=now().isoformat())
    POLICY.persist(journal, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go', action='store_true', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    api = Kubernetes()
    result = None
    state = None
    try:
        with wait_shared_lock(), finish_before_stop():
            state = state_directory()
            commit, source_hash, source = canonical_source(state)
            desired = manifest(datetime.now(KST).date(), args.image, args.image_id, state, source, source_hash)
            desired['metadata']['annotations']['personal-server/source-commit'] = commit
            result = run(api, state, desired)
    except Exception:
        print('car_care_schedule=BLOCKED')
    alert_ok = True
    try:
        api.notify(alert_payload(result is None, datetime.now(timezone.utc)))
    except Exception:
        alert_ok = False
        print('car_care_schedule_alert=FAIL')
    if state is not None:
        notification = dict(observed_at=datetime.now(timezone.utc).isoformat(),
                            alert_status='success' if alert_ok else 'failure',
                            backup_status=result['status'] if result is not None else 'blocked')
        POLICY.persist(state/('notification-'+datetime.now(KST).date().isoformat()+'.json'), notification)
    if result is not None:
        result['alert_status'] = 'success' if alert_ok else 'failure'
        POLICY.persist(state/('scheduled-'+result['day']+'.json'), result)
        print('car_care_schedule='+result['status'].upper())
    return 0 if result is not None and alert_ok else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('car_care_schedule=BLOCKED')
        raise SystemExit(1)
