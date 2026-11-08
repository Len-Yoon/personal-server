#!/usr/bin/env python3
"""Non-authority Docker mount metadata; never accepts a filesystem for HA.

Original collector bytes supply the unchanged Budget and trusted Docker GETs.
collect(source_sha256) accepts SOURCE overridden to a pinned in-memory fd path.
collect_request accepts bounded JSON {schema_version:1,
kind:"ha-filesystem-diagnostic-request",source_sha256:"<reviewed SHA>"}.

Mount schema: man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html
9p options: kernel.org/doc/html/latest/filesystems/9p.html
DrvFs declaration syntax: github.com/microsoft/WSL/blob/main/src/linux/init/drvfs.cpp
An aname=drvfs declaration is NOT verified WSL/backend/alias identity.
Read-only Docker socket access still possesses management capability.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from types import ModuleType

SOURCE = Path(__file__).resolve().parents[1] / 'ha_control/legacy_linux.py'
APPROVED_SOURCE_SHA256 = '938a71aaf3b741f42dbe0c9c86c551e34242df9c6e196fd2bea62be3c3122f24'
SOURCE_LIMIT = 131072
OUTPUT_LIMIT = 16384
MOUNT_LIMIT = 16384
ROW_LIMIT = 48
CLASSES = {
    'mount_type': {'bind', 'volume', 'tmpfs', 'other'},
    'filesystem': {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'tmpfs', '9p', 'drvfs', 'virtiofs', 'other'},
    'backend': {'not_9p', 'drvfs_declared', 'unknown'},
    'cache': {'not_9p', 'unspecified', 'none', 'readahead', 'mmap', 'loose', 'fscache', 'other_numeric', 'unknown'},
    'transport': {'not_9p', 'unix', 'tcp', 'fd', 'virtio', 'rdma', 'usbg', 'unknown'},
    'topology': {'single_visible_mount', 'multiple_same_device', 'stacked_mounts'},
}
PHASES = {'root_gate', 'source', 'request', 'timer', 'output', 'mountinfo', 'docker_info', 'docker_plugins',
          'docker_list', 'docker_inspect', 'source_resolve', 'docker_recheck', 'mountinfo_recheck', 'none'}
REASONS = {'none', 'native_linux_required', 'existing_root_required', 'main_thread_required',
           'source_rejected', 'request_rejected', 'report_rejected', 'output_limit_exceeded', 'query_unavailable', 'invalid_mountinfo',
           'ambiguous_mount_topology', 'invalid_docker_inventory', 'invalid_mount_metadata',
           'mount_limit_exceeded', 'classification_limit_exceeded', 'filesystem_identity_changed',
           'unresolved_volume_source', 'unresolved_filesystem_alias', 'docker_inventory_changed',
           'mountinfo_changed', 'collector_timer_already_owned', 'collector_cancelled',
           'live_graph_deadline_exceeded', 'live_graph_payload_exceeded', 'untrusted_docker_socket',
           'live_docker_query_failed', 'unsupported_swarm_provider', 'unsupported_docker_plugin',
           'docker_identity_race'}


class DiagnosticError(RuntimeError):
    pass


def _deny(reason):
    raise DiagnosticError(reason) from None


def _load(source_sha256):
    if type(source_sha256) is not str or source_sha256 != APPROVED_SOURCE_SHA256:
        _deny('source_rejected')
    with SOURCE.open('rb') as stream:
        source = stream.read(SOURCE_LIMIT + 1)
    if len(source) > SOURCE_LIMIT or hashlib.sha256(source).hexdigest() != source_sha256:
        _deny('source_rejected')
    graph = ModuleType('_ha_filesystem_pinned_collector')
    graph.__collector_sha256__ = source_sha256
    # Execute only already verified immutable bytes under a non-main name.
    exec(compile(source, '<pinned-filesystem-collector>', 'exec'), graph.__dict__)
    return graph


def _reason(error, graph=None):
    if isinstance(error, DiagnosticError) or (graph is not None and isinstance(error, graph.Blocked)):
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in REASONS:
            return error.args[0]
    return 'query_unavailable'


def _blank(uid=None):
    return dict(schema_version=1, kind='ha-filesystem-diagnostic', scope='docker-mount-metadata-only',
                authority=False, ready=False, supports=False, uid=uid,
                source_sha256=APPROVED_SOURCE_SHA256, status='blocked', reason='query_unavailable',
                failure_phase='root_gate', elapsed_ms=0.0, docker_queries=0, containers=0, mounts=0,
                docker_mount_scan='incomplete', docker_stability='unverified', alias_identity='unverified',
                wsl_backend_verified=False, counts=[], first_9p=None)


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def _options(value):
    result = {}
    for item in value.split(','):
        key, separator, content = item.partition('=')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', key) or key in result:
            _deny('invalid_mountinfo')
        result[key] = _unescape(content) if separator else None
    return result


def parse_mountinfo(payload, budget):
    """Keep private topology/source/options in memory; never return them in a report."""
    if type(payload) is not str or '\x00' in payload:
        _deny('invalid_mountinfo')
    entries, ids = [], set()
    for line in payload.splitlines():
        budget.left()
        fields = line.split()
        if len(entries) >= 8192 or fields.count('-') != 1:
            _deny('invalid_mountinfo')
        separator = fields.index('-')
        if separator < 6 or len(fields) != separator + 4:
            _deny('invalid_mountinfo')
        if any(not re.fullmatch(r'[0-9]{1,12}', fields[k]) for k in (0, 1)) or not re.fullmatch(r'[0-9]{1,12}:[0-9]{1,12}', fields[2]):
            _deny('invalid_mountinfo')
        identifier, parent = int(fields[0]), int(fields[1])
        if identifier == 0 or identifier in ids:
            _deny('invalid_mountinfo')
        ids.add(identifier)
        root, point = _unescape(fields[3]), _unescape(fields[4])
        if not root.startswith('/') or not point.startswith('/') or any(len(v) > 8192 for v in fields):
            _deny('invalid_mountinfo')
        optional = fields[6:separator]
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?', v) for v in optional):
            _deny('invalid_mountinfo')
        options = _options(fields[5])
        if ('rw' in options) == ('ro' in options):
            _deny('invalid_mountinfo')
        entries.append(dict(id=identifier, parent=parent, device=fields[2], root=root, point=point,
                            filesystem=fields[separator + 1], source=_unescape(fields[separator + 2]),
                            options=options, optional=tuple(optional), super_options=_options(fields[separator + 3])))
    if not entries:
        _deny('invalid_mountinfo')
    return entries


def _entry_for(actual, entries):
    matches = [entry for entry in entries if actual == entry['point'] or actual.startswith(entry['point'].rstrip('/') + '/')]
    if not matches:
        _deny('unresolved_filesystem_alias')
    length = max(len(entry['point']) for entry in matches)
    same_point = [entry for entry in matches if len(entry['point']) == length]
    # A stacked mount's parent is the covered mount. Ambiguity is an error,
    # never permission to choose the first matching pathname.
    covered = {entry['parent'] for entry in same_point if entry['parent'] != entry['id']}
    visible = [entry for entry in same_point if entry['id'] not in covered]
    if len(visible) != 1:
        _deny('ambiguous_mount_topology')
    selected = visible[0]
    topology = ('stacked_mounts' if len(same_point) > 1 else 'multiple_same_device'
                if sum(entry['device'] == selected['device'] for entry in entries) > 1 else 'single_visible_mount')
    return selected, topology


def classify(entry, topology, mount_type):
    fs = entry['filesystem'] if entry['filesystem'] in CLASSES['filesystem'] else 'other'
    row = dict(mount_type=mount_type if mount_type in CLASSES['mount_type'] else 'other',
               filesystem=fs, backend='not_9p', cache='not_9p', transport='not_9p', topology=topology)
    if fs != '9p':
        return row
    options = entry['super_options']
    # This is an explicit declaration only, not exporter/WSL identity proof.
    aname = options.get('aname')
    row['backend'] = 'drvfs_declared' if type(aname) is str and aname.split(';', 1)[0] == 'drvfs' else 'unknown'
    transport = options.get('trans')
    row['transport'] = transport if transport in CLASSES['transport'] - {'not_9p', 'unknown'} else 'unknown'
    cache = options.get('cache')
    row['cache'] = 'unspecified' if 'cache' not in options else 'unknown'
    names = {'none': 0, 'readahead': 1, 'mmap': 5, 'loose': 15, 'fscache': 143}
    if type(cache) is str:
        if cache in names:
            row['cache'] = cache
        elif re.fullmatch(r'(?:0x[0-9a-fA-F]{1,2}|[0-9]{1,3})', cache):
            number = int(cache, 16 if cache.startswith('0x') else 10)
            row['cache'] = next((name for name, value in names.items() if value == number), 'other_numeric')
    return row


def _identifiers(value):
    if type(value) is not list or len(value) > 4096:
        _deny('invalid_docker_inventory')
    ids = []
    for item in value:
        if type(item) is not dict or type(item.get('Id')) is not str or not re.fullmatch('[0-9a-f]{64}', item['Id']):
            _deny('invalid_docker_inventory')
        ids.append(item['Id'])
    if len(set(ids)) != len(ids):
        _deny('invalid_docker_inventory')
    return sorted(ids)


def _scan(graph):
    """Private synthetic-provider seam; public collect has already checked UID0."""
    result = _blank(uid=0)
    started = time.monotonic()
    phase, queries, timed = 'timer', 0, False
    previous = termination = None
    try:
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal, 'SIGALRM'):
            _deny('main_thread_required')
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            _deny('collector_timer_already_owned')
        budget = graph.Budget()
        previous, termination = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
        def deadline(*unused):
            raise graph.Blocked('live_graph_deadline_exceeded')
        # Own restoration before the first installation attempt: a later
        # handler/timer setup or budget.left failure must still restore both.
        timed = True
        signal.signal(signal.SIGALRM, deadline)
        signal.signal(signal.SIGTERM, lambda *unused: (_ for _ in ()).throw(graph.Blocked('collector_cancelled')))
        signal.setitimer(signal.ITIMER_REAL, budget.left())
        phase = 'mountinfo'
        initial = budget.file('/proc/self/mountinfo')
        entries = parse_mountinfo(initial.decode('utf-8'), budget)
        def docker(endpoint):
            nonlocal queries
            queries += 1
            return budget.docker(endpoint)
        phase = 'docker_info'
        info = docker('info')
        if type(info) is not dict or type(info.get('Swarm')) is not dict:
            _deny('invalid_docker_inventory')
        if info['Swarm'].get('LocalNodeState') != 'inactive':
            _deny('unsupported_swarm_provider')
        phase = 'docker_plugins'
        plugins = docker('plugins')
        if type(plugins) is not list:
            _deny('invalid_docker_inventory')
        if plugins:
            _deny('unsupported_docker_plugin')
        phase = 'docker_list'
        ids = _identifiers(docker('containers/json?all=1'))
        counts, first, total = {}, None, 0
        for identifier in ids:
            budget.left()
            phase = 'docker_inspect'
            item = docker('containers/' + identifier + '/json')
            if type(item) is not dict or item.get('Id') != identifier:
                _deny('docker_identity_race')
            mounts = item.get('Mounts')
            if type(mounts) is not list or len(mounts) > 4096:
                _deny('invalid_mount_metadata')
            for mount in mounts:
                phase = 'source_resolve'
                budget.left()
                if (type(mount) is not dict or type(mount.get('Source')) is not str
                        or type(mount.get('Type')) is not str or not mount['Type'] or type(mount.get('RW')) is not bool):
                    _deny('invalid_mount_metadata')
                path = Path(mount['Source'])
                if not path.is_absolute():
                    _deny('unresolved_volume_source')
                actual = path.resolve(strict=True)
                entry, topology = _entry_for(str(actual), entries)
                info = actual.stat()
                if entry['device'] != str(os.major(info.st_dev)) + ':' + str(os.minor(info.st_dev)):
                    _deny('filesystem_identity_changed')
                row = classify(entry, topology, mount['Type'])
                if path.resolve(strict=True) != actual:
                    _deny('filesystem_identity_changed')
                key = tuple(row[name] for name in CLASSES)
                if key not in counts:
                    if len(counts) >= ROW_LIMIT:
                        _deny('classification_limit_exceeded')
                    counts[key] = dict(row, mounts=0, rw_configured=0)
                counts[key]['mounts'] += 1
                counts[key]['rw_configured'] += int(mount['RW'])
                if entry['filesystem'] == '9p' and first is None:
                    first = dict(row, rw_configured=mount['RW'])
                total += 1
                if total > MOUNT_LIMIT:
                    _deny('mount_limit_exceeded')
                budget.left()
        phase = 'docker_recheck'
        if _identifiers(docker('containers/json?all=1')) != ids:
            _deny('docker_inventory_changed')
        # List equality does NOT prove inspect/configuration stability.
        phase = 'mountinfo_recheck'
        if budget.file('/proc/self/mountinfo') != initial:
            _deny('mountinfo_changed')
        budget.left()
        result.update(status='measured', reason='none', failure_phase='none', containers=len(ids), mounts=total,
                      docker_mount_scan='finished', counts=[counts[key] for key in sorted(counts)], first_9p=first)
    except Exception as error:
        result.update(reason=_reason(error, graph), failure_phase=phase)
    finally:
        if timed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                try:
                    signal.signal(signal.SIGALRM, previous)
                finally:
                    signal.signal(signal.SIGTERM, termination)
        result['elapsed_ms'] = max(0.0, (time.monotonic() - started) * 1000)
        result['docker_queries'] = queries
    if result['status'] == 'measured' and result['elapsed_ms'] >= 1500:
        elapsed = result['elapsed_ms']
        result = _blank(uid=0)
        result.update(reason='live_graph_deadline_exceeded', failure_phase=phase, elapsed_ms=elapsed, docker_queries=queries)
    return result


def _finalize(value, started=None):
    """Every public collect result, including memory-fd callers, is bounded."""
    reason = 'report_rejected'
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > OUTPUT_LIMIT:
            reason = 'output_limit_exceeded'
            raise ValueError()
        value = validate(value, APPROVED_SOURCE_SHA256)
    except (DiagnosticError, ValueError, TypeError, OverflowError, RecursionError):
        uid = value.get('uid') if type(value) is dict else None
        value = _blank(uid=0 if type(uid) is int and uid == 0 else None)
        value.update(reason=reason, failure_phase='output')
    if started is not None:
        elapsed = max(value['elapsed_ms'], (time.monotonic() - started) * 1000)
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 6000:
            value = _blank(uid=0)
            value.update(reason='report_rejected', failure_phase='output')
        else:
            value['elapsed_ms'] = elapsed
            if value['status'] == 'measured' and elapsed >= 1500:
                queries = value['docker_queries']
                value = _blank(uid=0)
                value.update(reason='live_graph_deadline_exceeded', failure_phase='output', elapsed_ms=elapsed, docker_queries=queries)
    return value


def collect(source_sha256):
    result = _blank()
    phase = 'root_gate'
    try:
        if sys.platform != 'linux':
            _deny('native_linux_required')
        result['uid'] = os.geteuid()
        if result['uid'] != 0:
            _deny('existing_root_required')
        phase = 'source'
        graph = _load(source_sha256)
        started = time.monotonic()
        return _finalize(_scan(graph), started=started)
    except Exception as error:
        result.update(reason=_reason(error), failure_phase=phase)
        return _finalize(result)


def collect_request(payload):
    try:
        if type(payload) is not bytes or len(payload) > 1024:
            _deny('request_rejected')
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    _deny('request_rejected')
                result[key] = value
            return result
        value = json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda unused: _deny('request_rejected'))
        if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'source_sha256'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['kind'] != 'ha-filesystem-diagnostic-request' or value['source_sha256'] != APPROVED_SOURCE_SHA256):
            _deny('request_rejected')
        return collect(value['source_sha256'])
    except Exception:
        result = _blank();result.update(reason='request_rejected', failure_phase='request')
        return result


def validate(value, source_sha256):
    """Pure report validation for the remote bridge; performs no live reads."""
    try:
        if source_sha256 != APPROVED_SOURCE_SHA256 or type(value) is not dict or set(value) != set(_blank()):
            raise ValueError()
        fixed = dict(schema_version=1, kind='ha-filesystem-diagnostic', scope='docker-mount-metadata-only',
                     authority=False, ready=False, supports=False, source_sha256=source_sha256,
                     docker_stability='unverified', alias_identity='unverified', wsl_backend_verified=False)
        if any(type(value[key]) is not type(expected) or value[key] != expected for key, expected in fixed.items()):
            raise ValueError()
        if value['uid'] is not None and (type(value['uid']) is not int or not 0 <= value['uid'] <= 4294967295):
            raise ValueError()
        if (value['status'] not in {'measured', 'blocked'} or value['reason'] not in REASONS
                or value['failure_phase'] not in PHASES or type(value['elapsed_ms']) not in (int, float)
                or not math.isfinite(value['elapsed_ms']) or not 0 <= value['elapsed_ms'] <= 6000):
            raise ValueError()
        for key, maximum in (('containers', 4096), ('mounts', MOUNT_LIMIT), ('docker_queries', 4100)):
            if type(value[key]) is not int or not 0 <= value[key] <= maximum:
                raise ValueError()
        if type(value['counts']) is not list or len(value['counts']) > ROW_LIMIT:
            raise ValueError()
        seen = set()
        def category(row, extra):
            if type(row) is not dict or set(row) != set(CLASSES) | set(extra):
                raise ValueError()
            if any(type(row[name]) is not str or row[name] not in allowed for name, allowed in CLASSES.items()):
                raise ValueError()
            for name in ('backend', 'cache', 'transport'):
                if (row['filesystem'] == '9p') == (row[name] == 'not_9p'):
                    raise ValueError()
        for row in value['counts']:
            category(row, {'mounts', 'rw_configured'})
            key = tuple(row[name] for name in CLASSES)
            if key in seen or type(row['mounts']) is not int or not 1 <= row['mounts'] <= MOUNT_LIMIT or type(row['rw_configured']) is not int or not 0 <= row['rw_configured'] <= row['mounts']:
                raise ValueError()
            seen.add(key)
        first = value['first_9p']
        nine = [row for row in value['counts'] if row['filesystem'] == '9p']
        if (first is None) != (not nine):
            raise ValueError()
        if first is not None:
            category(first, {'rw_configured'})
            if first['filesystem'] != '9p' or type(first['rw_configured']) is not bool or not any(
                    all(first[name] == row[name] for name in CLASSES)
                    and (row['rw_configured'] > 0 if first['rw_configured'] else row['rw_configured'] < row['mounts']) for row in nine):
                raise ValueError()
        if value['status'] == 'measured':
            if (value['uid'] != 0 or value['reason'] != 'none' or value['failure_phase'] != 'none'
                    or value['elapsed_ms'] >= 1500
                    or value['docker_mount_scan'] != 'finished' or value['docker_queries'] != value['containers'] + 4
                    or sum(row['mounts'] for row in value['counts']) != value['mounts']):
                raise ValueError()
        elif (value['reason'] == 'none' or value['failure_phase'] == 'none' or value['docker_mount_scan'] != 'incomplete'
              or value['counts'] or value['containers'] or value['mounts'] or first is not None):
            raise ValueError()
        if len(json.dumps(value, allow_nan=False).encode()) > OUTPUT_LIMIT:
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, OverflowError):
        _deny('request_rejected')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect-local', action='store_true', required=True)
    parser.add_argument('--source-sha256', required=True)
    args = parser.parse_args(argv)
    value = collect(args.source_sha256)
    try:
        validate(value, APPROVED_SOURCE_SHA256)
    except DiagnosticError:
        value = _blank();value.update(reason='query_unavailable', failure_phase='source')
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0 if value['status'] == 'measured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
