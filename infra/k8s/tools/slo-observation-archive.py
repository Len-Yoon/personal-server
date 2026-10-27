#!/usr/bin/env python3
"""Preserve completed UTC-day Portal observation grids without filling gaps.

No Kubernetes writes, credentials, operational data, or external notifications.
Collection is opt-in; the suspended installation manifest requires deployment.
"""
import argparse
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
from http.client import HTTPException
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import zlib
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

LIMIT = 8 * 1024 * 1024
QUOTA = 384 * 1024 * 1024
RESERVE = 64 * 1024 * 1024
RETENTION_DAYS = 35
MARKER = b'personal-server-portal-observation-archive-v1\n'
NAME = re.compile(r'\d{4}-\d{2}-\d{2}\.json\.gz\Z')
DEFAULT_URL = 'http://prometheus-operated.monitoring.svc:9090'
_SPEC = importlib.util.spec_from_file_location('sample_summary', Path(__file__).with_name('slo-sample-summary.py'))
_ANALYZER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ANALYZER)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('redirect_not_allowed')


urlopen = build_opener(NoRedirect()).open


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def search_block(block, start, *, stored=False):
    if not isinstance(block, dict) or set(block) != {'step_seconds', 'values', 'timestamps'} or type(block['step_seconds']) is not int or block['step_seconds'] != 60:
        raise ValueError('invalid_search_block')
    normalized = {'step_seconds': 60}
    families = set()
    identities = {}
    points = 0
    for kind in ('values', 'timestamps'):
        response = block[kind]
        if not isinstance(response, dict) or response.get('status') != 'success' or response.get('warnings') or response.get('infos'):
            raise ValueError('invalid_search_response')
        data = response.get('data')
        if not isinstance(data, dict) or data.get('resultType') != 'matrix' or not isinstance(data.get('result'), list) or len(data['result']) > 128:
            raise ValueError('invalid_search_matrix')
        rows = []
        seen = set()
        for row in data['result']:
            if not isinstance(row, dict) or not isinstance(row.get('metric'), dict) or not isinstance(row.get('values'), list) or len(row['values']) > 1440:
                raise ValueError('invalid_search_series')
            labels = row['metric']
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
                raise ValueError('invalid_search_labels')
            if labels.get('namespace', 'personal-server') != 'personal-server':
                raise ValueError('invalid_search_scope')
            if 'source' in labels:
                name = 'portal_search_source_requests_total'
                semantic = {k: labels.get(k) for k in ('source', 'outcome')}
                if semantic['source'] not in ('news', 'youtube', 'books') or semantic['outcome'] not in ('ok', 'timeout', 'http_error', 'invalid_response', 'transport_error', 'internal_error', 'cancelled'):
                    raise ValueError('invalid_search_outcome')
            elif 'outcome' in labels:
                name = 'portal_search_requests_total'; semantic = {'outcome': labels['outcome']}
                if semantic['outcome'] not in ('complete', 'partial', 'failed', 'cancelled'):
                    raise ValueError('invalid_search_outcome')
            else:
                if labels.get('method') != 'GET' or labels.get('route') != '/search':
                    raise ValueError('invalid_search_scope')
                semantic = {'method': 'GET', 'route': '/search'}
                if 'le' in labels:
                    name = 'portal_http_request_duration_seconds_bucket'; semantic['le'] = labels['le']
                    number = _ANALYZER._number(labels['le'])
                    if number == math.inf:
                        semantic['le'] = '+Inf'
                    elif number in (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10):
                        semantic['le'] = f'{number:g}'
                    else:
                        raise ValueError('invalid_bucket')
                else:
                    name = 'portal_http_requests_total'; semantic['status_code'] = labels.get('status_code')
                    if not isinstance(semantic['status_code'], str) or not re.fullmatch('[1-5][0-9]{2}', semantic['status_code']):
                        raise ValueError('invalid_status_code')
            if labels.get('__name__', name) != name:
                raise ValueError('unexpected_metric')
            if stored:
                if set(labels) != set(semantic) | {'__name__', 'origin_sha256'} or not re.fullmatch('[0-9a-f]{64}', labels['origin_sha256']):
                    raise ValueError('unsanitized_search')
                origin = labels['origin_sha256']
            else:
                origin = digest(canonical({k: v for k, v in labels.items() if k != '__name__'}))
            safe = {'__name__': name, 'origin_sha256': origin, **semantic}
            identity = digest(canonical(safe))
            if identity in seen:
                raise ValueError('duplicate_search_series')
            seen.add(identity)
            previous = -1
            for pair in row['values']:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError('invalid_search_point')
                at = _ANALYZER._number(pair[0]); value = _ANALYZER._number(pair[1])
                offset = (at - start) / 60
                if not math.isfinite(at) or not 0 <= offset <= 1439 or abs(offset - round(offset)) > 1e-7 or round(offset) <= previous:
                    raise ValueError('invalid_search_grid')
                if kind == 'values' and math.isfinite(value) and value < 0:
                    raise ValueError('negative_counter')
                previous = round(offset)
            rows.append({'metric': safe, 'values': row['values']})
            if kind == 'values':
                families.add(name); points += len(row['values'])
        identities[kind] = seen
        normalized[kind] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': rows}}
    return normalized, {'status': 'preserved' if identities['values'] and identities['values'] == identities['timestamps'] else 'empty_or_missing',
                        'value_series': len(identities['values']), 'timestamp_series': len(identities['timestamps']),
                        'value_points': points, 'families': sorted(families),
                        'semantics': 'raw_counter_and_source_timestamp_samples_not_period_slo'}


def checked_payload(payload, today):
    summary = _ANALYZER.summarize_samples(payload)
    start, end, step = (payload[k] for k in ('start', 'end', 'step_seconds'))
    if start % 86400 or 86400 % step or end != start + 86400 - step:
        raise ValueError('not_daily_grid')
    day = datetime.fromtimestamp(start, timezone.utc).date()
    if day >= today:
        raise ValueError('incomplete_day')
    # Validate all original labels before removing them. Retain only a hash of
    # the source identity, never addresses, query strings or arbitrary labels.
    clean = {k: payload[k] for k in ('schema_version', 'start', 'end', 'step_seconds')}
    clean['queries'] = {}
    for key in _ANALYZER.QUERIES:
        rows = []
        for row in payload['queries'][key]['data']['result']:
            labels = {k: v for k, v in row['metric'].items() if k != '__name__'}
            rows.append({'metric': {'namespace': 'personal-server', 'deployment': 'portal-web',
                                    'source_sha256': digest(canonical(labels))}, 'values': row['values']})
        clean['queries'][key] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': rows}}
    if 'search' in payload:
        clean['search'], summary['search_archive'] = search_block(payload['search'], start)
    else:
        summary['search_archive'] = {'status': 'not_collected'}
    if len(canonical(clean)) > LIMIT - 4096:
        raise ValueError('too_large')
    return day, clean, summary


def regular(fd):
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077:
        raise ValueError('unsafe_file')
    return s


def read_at(directory, name, *, identity=False, retain=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    kept = False
    try:
        info = regular(fd)
        if info.st_size > LIMIT + 65536:
            raise ValueError('too_large')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(LIMIT + 65537)
        if len(data) > LIMIT + 65536:
            raise ValueError('too_large')
        if retain:
            kept = True
            return data, (info.st_dev, info.st_ino), fd
        return (data, (info.st_dev, info.st_ino)) if identity else data
    finally:
        if not kept:
            os.close(fd)


def initialize(path):
    path = Path(path)
    # Claim only a new directory, never an existing user's directory.
    path.mkdir(mode=0o700)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open('.owner', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(MARKER); stream.flush(); os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory)
    finally:
        os.close(directory)


def open_archive(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        s = os.fstat(directory)
        if s.st_uid != os.getuid() or s.st_mode & 0o077 or read_at(directory, '.owner') != MARKER:
            raise ValueError('unsafe_archive')
        lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        regular(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return directory, lock
    except BaseException:
        if lock is not None:
            os.close(lock)
        os.close(directory)
        raise


def entries(directory, today, held=None, enforce_quota=True):
    names = os.listdir(directory)
    if len(names) > 66 or any(n not in ('.owner', '.lock') and not NAME.fullmatch(n) for n in names):
        raise ValueError('unexpected_files')
    found = []
    for name in sorted(n for n in names if NAME.fullmatch(n)):
        if held is None:
            compressed, identity = read_at(directory, name, identity=True)
        else:
            compressed, identity, fd = read_at(directory, name, retain=True)
            holder = ExitStack()
            holder.callback(os.close, fd)
            held.callback(holder.close)
        # Gzip output is bounded as well as compressed input (zip-bomb guard).
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('too_large')
        capsule = json.loads(raw)
        if not isinstance(capsule, dict) or set(capsule) != {'schema_version', 'date', 'payload', 'payload_sha256'} or type(capsule['schema_version']) is not int or capsule['schema_version'] != 1:
            raise ValueError('invalid_capsule')
        payload = capsule['payload']
        # Archived labels are already sanitized; do not hash them repeatedly.
        summary = _ANALYZER.summarize_samples(payload)
        start, end, step = (payload[k] for k in ('start', 'end', 'step_seconds'))
        day = datetime.fromtimestamp(start, timezone.utc).date()
        if 'search' in payload:
            _, summary['search_archive'] = search_block(payload['search'], start, stored=True)
        else:
            summary['search_archive'] = {'status': 'not_collected'}
        if start % 86400 or 86400 % step or end != start + 86400 - step or day >= today:
            raise ValueError('not_daily_grid')
        if capsule['date'] != day.isoformat() or name != day.isoformat() + '.json.gz' or digest(canonical(payload)) != capsule['payload_sha256']:
            raise ValueError('corrupt_capsule')
        for response in payload['queries'].values():
            for row in response['data']['result']:
                labels = row['metric']
                if set(labels) != {'namespace', 'deployment', 'source_sha256'} or not re.fullmatch('[0-9a-f]{64}', labels['source_sha256']):
                    raise ValueError('unsanitized_archive')
        found.append({'name': name, 'date': day, 'bytes': len(compressed),
                      'identity': identity, 'holder': holder if held is not None else None,
                      'payload_sha256': capsule['payload_sha256'], 'summary': summary})
    if enforce_quota and sum(x['bytes'] for x in found) > QUOTA:
        raise ValueError('quota_exceeded')
    return found


def remove_expired(directory, rows, today):
    keep = []
    for item in rows:
        if item['date'] >= today - timedelta(days=RETENTION_DAYS):
            keep.append(item)
            continue
        current = os.stat(item['name'], dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != item['identity']:
            raise ValueError('archive_replaced')
        os.unlink(item['name'], dir_fd=directory)
        item['holder'].close()
    os.fsync(directory)
    return keep, len(rows) - len(keep)


def archive(payload, path, *, today=None, prune=False):
    today = today or datetime.now(timezone.utc).date()
    day, clean, summary = checked_payload(payload, today)
    if prune and day < today - timedelta(days=RETENTION_DAYS):
        raise ValueError('expired_input')
    capsule = {'schema_version': 1, 'date': day.isoformat(), 'payload': clean,
               'payload_sha256': digest(canonical(clean))}
    raw = canonical(capsule)
    if len(raw) > LIMIT:
        raise ValueError('too_large')
    compressed = gzip.compress(raw, mtime=0)
    directory, lock = open_archive(path)
    held = ExitStack()
    held.callback(os.close, directory)
    held.callback(os.close, lock)
    pending = None
    try:
        existing = entries(directory, today, held, enforce_quota=not prune)
        deleted = 0
        if prune:
            existing, deleted = remove_expired(directory, existing, today)
        same = [x for x in existing if x['date'] == day]
        if same and same[0]['payload_sha256'] != capsule['payload_sha256']:
            raise ValueError('immutable_day_conflict')
        if not same:
            if sum(x['bytes'] for x in existing) + len(compressed) > QUOTA or len(existing) >= 64:
                raise ValueError('quota_exceeded')
            space = os.fstatvfs(directory)
            if space.f_bavail * space.f_frsize < len(compressed) + RESERVE:
                raise ValueError('insufficient_space')
            pending = '.pending-' + secrets.token_hex(16)
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(compressed); stream.flush(); os.fsync(fd)
            finally:
                os.close(fd)
            # link is no-clobber even if an unrelated writer creates the name.
            os.link(pending, day.isoformat() + '.json.gz', src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(pending, dir_fd=directory); pending = None
            os.fsync(directory)
        incomplete_search = 'search' in clean and summary['search_archive']['status'] != 'preserved'
        return {'status': 'partial' if summary['samples']['unknown'] or incomplete_search else 'complete',
                'date': day.isoformat(), 'idempotent': bool(same), 'expired_removed': deleted,
                'payload_sha256': capsule['payload_sha256'], 'summary': summary}
    finally:
        try:
            if pending is not None:
                os.unlink(pending, dir_fd=directory)
        finally:
            held.close()


def prune_expired(path, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    directory, lock = open_archive(path)
    with ExitStack() as held:
        held.callback(os.close, directory); held.callback(os.close, lock)
        rows = entries(directory, today, held, enforce_quota=False)
        keep, deleted = remove_expired(directory, rows, today)
        return {'status': 'pruned', 'expired_removed': deleted, 'present_days': len(keep)}


def verify(path, *, today=None):
    today = today or datetime.now(timezone.utc).date()
    directory, lock = open_archive(path)
    try:
        rows = entries(directory, today)
        dates = {x['date'] for x in rows}
        wanted = [today - timedelta(days=i) for i in range(RETENTION_DAYS, 0, -1)]
        return {'status': 'verified', 'present_days': len(rows), 'retention_days': RETENTION_DAYS,
                'missing_dates': [x.isoformat() for x in wanted if x not in dates],
                'partial_dates': [x['date'].isoformat() for x in rows if x['summary']['samples']['unknown']],
                'search_unobserved_dates': [x['date'].isoformat() for x in rows if x['summary']['search_archive']['status'] != 'preserved'],
                'bytes': sum(x['bytes'] for x in rows), 'slo_budget': 'not_calculable'}
    finally:
        os.close(lock); os.close(directory)


def collect(day, base=DEFAULT_URL):
    # Cooperative 45s budget, checked between reads. An in-flight I/O may take
    # up to its 10s socket timeout; the Kubernetes Job also has an outer limit.
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or parsed.hostname not in ('prometheus-operated.monitoring.svc', 'localhost', '127.0.0.1', '::1'):
        raise ValueError('invalid_endpoint')
    start = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())
    payload = {'schema_version': 1, 'start': start, 'end': start + 86400 - 15,
               'step_seconds': 15, 'queries': {}}
    deadline = time.monotonic() + 45
    names = list(_ANALYZER.QUERIES) + ['search_values', 'search_timestamps']
    payload['search'] = {'step_seconds': 60}
    for name in names:
        if name.startswith('search_'):
            query = '({__name__=~"portal_search_requests_total|portal_search_source_requests_total",namespace="personal-server"} or {__name__=~"portal_http_requests_total|portal_http_request_duration_seconds_bucket",namespace="personal-server",method="GET",route="/search"})'
            step = 60
            if name == 'search_timestamps':
                query = 'timestamp(' + query + ')'
        else:
            metric = 'kube_deployment_spec_replicas' if name.startswith('desired') else 'kube_deployment_status_replicas_available'
            query = metric + '{namespace="personal-server",deployment="portal-web"}'; step = 15
            if name.endswith('_timestamp'):
                query = 'timestamp(' + query + ')'
        url = base.rstrip('/') + '/api/v1/query_range?' + urlencode({'query': query, 'start': start, 'end': start + 86400 - step, 'step': step})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('collection_deadline')
        with urlopen(url, timeout=min(10, remaining)) as response:
            raw = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('collection_deadline')
                chunk = response.read1(min(65536, LIMIT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > LIMIT:
                    raise ValueError('too_large')
        if name.startswith('search_'):
            payload['search'][name.removeprefix('search_')] = json.loads(raw)
        else:
            payload['queries'][name] = json.loads(raw)
    return payload


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid_arguments')
    try:
        parser = Parser(description=__doc__)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--input'); group.add_argument('--collect', action='store_true'); group.add_argument('--verify', action='store_true'); group.add_argument('--prune-expired', action='store_true')
        parser.add_argument('--directory', required=True)
        parser.add_argument('--initialize', action='store_true')
        parser.add_argument('--prune', action='store_true')
        parser.add_argument('--date')
        args = parser.parse_args(argv)
        today = datetime.now(timezone.utc).date()
        if args.verify or args.prune_expired:
            if args.initialize or args.prune or args.date:
                raise ValueError('invalid_arguments')
            result = verify(args.directory) if args.verify else prune_expired(args.directory)
        else:
            if args.collect:
                day = date.fromisoformat(args.date) if args.date else today - timedelta(days=1)
                if day >= today:
                    raise ValueError('incomplete_day')
                payload = collect(day)
            else:
                if args.date:
                    raise ValueError('invalid_arguments')
                with Path(args.input).open('rb') as stream:
                    raw = stream.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ValueError('too_large')
                payload = json.loads(raw)
            checked_payload(payload, today)
            if args.initialize and not Path(args.directory).exists() and not Path(args.directory).is_symlink():
                initialize(args.directory)
            result = archive(payload, args.directory, prune=args.prune)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 2 if result['status'] == 'partial' else 0
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, EOFError, zlib.error, HTTPException):
        print('slo_observation_archive=FAIL reason=invalid_input_or_storage', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
