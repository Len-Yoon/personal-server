#!/usr/bin/env python3
"""Read-only archive integrity monitoring; never creates or repairs archive files.

Verification runs in one bounded child, independently of scrapes. A preserved
search series is not evidence of full sample coverage or a calculated SLO.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import zlib

SPEC = importlib.util.spec_from_file_location('observation_archive', Path(__file__).with_name('slo-observation-archive.py'))
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)
PREFIX = 'slo_observation_archive_'
FIELDS = frozenset(('present_days', 'stored_bytes', 'filesystem_available_bytes',
                    'latest_preserved_day_timestamp_seconds', 'partial_replica_days',
                    'search_series_unobserved_days', 'required_days', 'missing_required_days'))


class ConcurrentChange(RuntimeError):
    """The immutable directory changed while being observed; retry next cycle."""


def open_readonly(path):
    # Never flock: the production writer takes LOCK_EX|LOCK_NB with no retry.
    # Its files are immutable, atomically published and cooperatively pruned.
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_archive')
        marker = os.open('.owner', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            ARCHIVE.regular(marker)
            if os.read(marker, len(ARCHIVE.MARKER) + 1) != ARCHIVE.MARKER:
                raise ValueError('unsafe_archive')
        finally:
            os.close(marker)
        lock = os.open('.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        ARCHIVE.regular(lock)
        return directory, lock
    except BaseException:
        if lock is not None:
            os.close(lock)
        os.close(directory)
        raise


def fingerprint(directory, path):
    info = os.fstat(directory)
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ConcurrentChange()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def pending_publication(directory):
    names = os.listdir(directory)
    pending = [n for n in names if re.fullmatch(r'\.pending-[a-f0-9]{32}', n)]
    if len(names) > 66 or any(n not in ('.owner', '.lock') and not ARCHIVE.NAME.fullmatch(n) and n not in pending for n in names):
        raise ValueError('unexpected_files')
    for name in pending:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        age = time.time() - info.st_mtime
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink not in (1, 2) or info.st_size > ARCHIVE.LIMIT + 65536 or not 0 <= age <= 200):
            raise ValueError('invalid_pending')
    if pending:
        raise ConcurrentChange()


def snapshot(path, *, required_from, today=None, now=None):
    now = now or (datetime.combine(today, datetime.min.time(), timezone.utc) + timedelta(hours=12)
                  if today else datetime.now(timezone.utc))
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError('UTC_clock_required')
    today = now.date()
    if type(required_from) is not date or required_from > today:
        raise ValueError('invalid_retention_start')
    directory, lock = open_readonly(path)
    try:
        before = fingerprint(directory, path)
        error = None
        try:
            pending_publication(directory)
            rows = ARCHIVE.entries(directory, today)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, EOFError, zlib.error, ConcurrentChange) as caught:
            error = caught
        try:
            after = fingerprint(directory, path)
        except OSError:
            raise ConcurrentChange() from None
        if after != before:
            raise ConcurrentChange() from None
        if error is not None:
            raise error
        fs = os.fstatvfs(directory)
        latest = max((datetime.combine(r['date'], datetime.min.time(), timezone.utc).timestamp()
                      for r in rows), default=0)
        first = max(required_from, today - timedelta(days=ARCHIVE.RETENTION_DAYS))
        last = (now - timedelta(seconds=2700)).date() - timedelta(days=1)
        wanted = {first + timedelta(days=i) for i in range(max(0, (last - first).days + 1))}
        observed = {r['date'] for r in rows}
        return {'present_days': len(rows), 'stored_bytes': sum(r['bytes'] for r in rows),
                'filesystem_available_bytes': fs.f_bavail * fs.f_frsize,
                'latest_preserved_day_timestamp_seconds': latest,
                'partial_replica_days': sum(bool(r['summary']['samples']['unknown']) for r in rows),
                'search_series_unobserved_days': sum(r['summary']['search_archive']['status'] != 'preserved' for r in rows),
                'required_days': len(wanted), 'missing_required_days': len(wanted - observed)}
    finally:
        os.close(lock)
        os.close(directory)


def worker(path, required_from):
    try:
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                 '--snapshot', '--directory', str(path), '--required-from', required_from.isoformat()],
                                capture_output=True, timeout=25, check=False)
        if len(result.stdout) > 4096 or result.stderr or result.returncode not in (0, 3):
            return 'invalid', None
        if result.returncode == 3:
            return ('busy', None) if result.stdout == b'{"status":"busy"}\n' else ('invalid', None)
        values = json.loads(result.stdout)
        if set(values) != FIELDS or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values.values()):
            return 'invalid', None
        return 'verified', values
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return 'invalid', None


class Collector:
    def __init__(self, path, required_from, *, reader=worker, clock=time.time):
        self.path, self.required_from, self.reader, self.clock = path, required_from, reader, clock
        self.lock = threading.Lock()
        self.running = threading.Lock()
        self.values = {}
        self.success = self.busy = self.last_attempt = self.last_verified = 0

    def refresh(self):
        # Concurrent scrapes never create parallel whole-archive verification.
        if not self.running.acquire(blocking=False):
            return
        try:
            status, values = self.reader(self.path, self.required_from)
            with self.lock:
                self.last_attempt = self.clock()
                self.busy = int(status == 'busy')
                if status == 'verified':
                    self.values = values
                    self.last_verified = self.last_attempt
                    self.success = 1
                elif status != 'busy':
                    self.success = 0
                    self.values = {}
        finally:
            self.running.release()

    def render(self):
        with self.lock:
            values = dict(self.values, verification_success=self.success, verification_busy=self.busy,
                          last_verification_attempt_timestamp_seconds=self.last_attempt,
                          last_verified_timestamp_seconds=self.last_verified,
                          quota_bytes=ARCHIVE.QUOTA, reserve_bytes=ARCHIVE.RESERVE)
        return ''.join(f'# TYPE {PREFIX}{key} gauge\n{PREFIX}{key} {value}\n'
                       for key, value in sorted(values.items())).encode('ascii')


def serve(path, required_from, port=9091):
    collector = Collector(path, required_from)
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        # Fixed numerical metrics only. Never echo URI, filesystem or errors.
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path not in ('/metrics', '/health', '/ready'):
                self.send_error(404)
                return
            data = collector.render() if self.path == '/metrics' else b'ok\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def refresh_loop():
        while not stopped.is_set():
            collector.refresh()
            stopped.wait(300)

    class BoundedServer(HTTPServer):
        def handle_error(self, *_args):
            pass

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    server = BoundedServer(('0.0.0.0', port), Handler)
    server.timeout = 5
    threading.Thread(target=refresh_loop, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()


def render(image, required_from):
    # Source-bound immutable code CM keeps the already scanned collector image
    # unchanged. No workflow/build allowlist or production CronJob is edited.
    import yaml
    if not re.fullmatch(r'docker\.io/library/personal-server-slo-observation-archive@sha256:[a-f0-9]{64}', image):
        raise ValueError('immutable_archive_image_required')
    if type(required_from) is not date or required_from > datetime.now(timezone.utc).date():
        raise ValueError('invalid_retention_start')
    source = Path(__file__).read_text(encoding='utf-8')
    sha = hashlib.sha256(source.encode('utf-8')).hexdigest()
    name = 'slo-observation-health-code-' + sha[:16]
    docs = list(yaml.safe_load_all((Path(__file__).resolve().parents[1] / 'slo-evidence/archive/health-monitor.yaml').read_text()))
    deployment = next(d for d in docs if d['kind'] == 'Deployment')
    template = deployment['spec']['template']
    template['metadata']['annotations'] = {'personal-server/health-code-sha256': sha}
    pod = template['spec']
    pod['containers'][0]['image'] = image
    pod['containers'][0]['args'] = ['--directory', '/archive/observations', '--required-from', required_from.isoformat()]
    pod['containers'][0]['volumeMounts'].append({'name': 'health-code', 'mountPath': '/opt/observations/slo-observation-health.py', 'subPath': 'slo-observation-health.py', 'readOnly': True})
    pod['volumes'].append({'name': 'health-code', 'configMap': {'name': name, 'defaultMode': 0o444}})
    code = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
            'metadata': {'name': name, 'namespace': 'monitoring', 'annotations': {'personal-server/health-code-sha256': sha}},
            'data': {'slo-observation-health.py': source}}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [code, *docs]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory')
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--image')
    parser.add_argument('--required-from', required=True)
    args = parser.parse_args(argv)
    try:
        required_from = date.fromisoformat(args.required_from)
        if required_from.isoformat() != args.required_from:
            raise ValueError('invalid_retention_start')
        if args.render:
            if args.directory or args.snapshot or not args.image:
                raise ValueError('invalid_arguments')
            print(json.dumps(render(args.image, required_from), sort_keys=True))
            return 0
        if args.directory is None or args.image:
            raise ValueError('invalid_arguments')
        if args.snapshot:
            print(json.dumps(snapshot(args.directory, required_from=required_from), separators=(',', ':'), allow_nan=False))
        else:
            serve(args.directory, required_from)
        return 0
    except ConcurrentChange:
        print('{"status":"busy"}')
        return 3
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, EOFError, zlib.error):
        print('{"status":"invalid"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
