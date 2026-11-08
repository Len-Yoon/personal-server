"""Synthetic private mount/Docker data over real pinned Budget; no live reads."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('filesystem_diagnostic', ROOT / 'infra/k8s/tools/n100_ha_filesystem_diagnostic.py')
DIAG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAG)
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import legacy_guard


class FilesystemDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.graph = DIAG._load(DIAG.APPROVED_SOURCE_SHA256)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.drvfs, self.generic, self.local = [self.root / name for name in ('drvfs', 'generic', 'local')]
        for path in (self.drvfs, self.generic, self.local):
            path.mkdir()
        self.device = str(os.major(self.root.stat().st_dev)) + ':' + str(os.minor(self.root.stat().st_dev))
        self.mountinfo = ('1 0 ' + self.device + ' / / rw - ext4 PRIVATE_ROOT rw\n'
            '2 1 ' + self.device + ' / ' + str(self.drvfs) + ' rw shared:7 - 9p PRIVATE_DRIVE rw,aname=drvfs;path=PRIVATE_PATH;uid=SECRET,cache=0x5,trans=fd\n'
            '3 1 ' + self.device + ' /remote ' + str(self.generic) + ' rw - 9p PRIVATE_SERVER rw,aname=PRIVATE_EXPORT,cache=loose,trans=tcp\n'
            '4 1 ' + self.device + ' /private ' + str(self.local) + ' ro - ext4 PRIVATE_LOCAL ro\n').encode()
        self.listing = [{'Id': letter * 64} for letter in ('a', 'b', 'c')]
        self.inspects = {item['Id']: dict(Id=item['Id'], Mounts=[dict(Source=str(path), Type='bind', RW=rw)],
                           Config={'Env': ['PRIVATE_TOKEN=SECRET_VALUE']})
                         for item, path, rw in zip(self.listing, (self.drvfs, self.generic, self.local), (True, False, False))}
        self.trace = []
        self.final_mountinfo = self.mountinfo
        self.final_listing = deepcopy(self.listing)
        self.error = None
        self.slow = False
        outer = self
        class FakeBudget(self.graph.Budget):
            def file(self, path):
                outer.trace.append(('file', str(path), id(self)))
                if path != '/proc/self/mountinfo':
                    raise AssertionError('unexpected source read')
                if outer.slow:
                    time.sleep(2)
                which = sum(call[0] == 'file' for call in outer.trace)
                return self.count(outer.mountinfo if which == 1 else outer.final_mountinfo)
            def docker(self, endpoint):
                outer.trace.append(('docker', endpoint, id(self)))
                if outer.error is not None and endpoint == 'plugins':
                    raise outer.error
                if endpoint == 'info':
                    value = {'Swarm': {'LocalNodeState': 'inactive'}}
                elif endpoint == 'plugins':
                    value = []
                elif endpoint == 'containers/json?all=1':
                    count = sum(call[0:2] == ('docker', endpoint) for call in outer.trace)
                    value = outer.listing if count == 1 else outer.final_listing
                elif endpoint.startswith('containers/') and endpoint.endswith('/json'):
                    value = outer.inspects[endpoint.split('/')[1]]
                else:
                    raise AssertionError('unexpected provider call')
                self.count(json.dumps(value).encode())
                return deepcopy(value)
        self.fake_budget = FakeBudget

    def scan(self):
        with patch.object(self.graph, 'Budget', self.fake_budget):
            return DIAG._scan(self.graph)

    def test_all_mounts_share_real_budget_get_order_and_private_output(self):
        value = self.scan()
        DIAG.validate(value, DIAG.APPROVED_SOURCE_SHA256)
        self.assertEqual(value['status'], 'measured')
        self.assertEqual(value['containers'], 3)
        self.assertEqual(value['mounts'], 3)
        self.assertEqual(value['docker_queries'], 7)
        self.assertEqual(len({call[2] for call in self.trace}), 1)
        self.assertEqual([(call[0], call[1]) for call in self.trace], [
            ('file', '/proc/self/mountinfo'), ('docker', 'info'), ('docker', 'plugins'),
            ('docker', 'containers/json?all=1'),
            *[('docker', 'containers/' + letter * 64 + '/json') for letter in ('a', 'b', 'c')],
            ('docker', 'containers/json?all=1'), ('file', '/proc/self/mountinfo')])
        self.assertEqual(value['first_9p']['backend'], 'drvfs_declared')
        self.assertEqual(value['first_9p']['cache'], 'mmap')
        self.assertTrue(value['first_9p']['rw_configured'])
        self.assertEqual(value['docker_stability'], 'unverified')
        self.assertEqual(value['alias_identity'], 'unverified')
        self.assertFalse(value['wsl_backend_verified'])
        for key in ('supports', 'authority', 'ready'):
            self.assertIs(value[key], False)
        encoded = json.dumps(value)
        for private in ('PRIVATE_', 'SECRET', str(self.root), 'a' * 64, 'b' * 64, 'c' * 64):
            self.assertNotIn(private, encoded)
        with self.assertRaises(legacy_guard.GuardBlocked):
            legacy_guard.verify(value)

    def test_declared_drvfs_never_authenticates_backend_and_cache_is_explicit(self):
        budget = self.graph.Budget()
        base = DIAG.parse_mountinfo(self.mountinfo.decode(), budget)[1]
        cases = [('drvfs;path=PRIVATE', 'none', 'fd', 'drvfs_declared', 'none', 'fd'),
                 ('drvfs', '0x5', 'virtio', 'drvfs_declared', 'mmap', 'virtio'),
                 ('drvfs_evil', '143', 'tcp', 'unknown', 'fscache', 'tcp'),
                 ('generic', '15', 'rdma', 'unknown', 'loose', 'rdma'),
                 ('generic', '0', 'unix', 'unknown', 'none', 'unix'),
                 ('generic', 'SECRET_MODE', 'SECRET_SERVER', 'unknown', 'unknown', 'unknown')]
        for aname, cache, transport, backend, expected_cache, expected_transport in cases:
            entry = deepcopy(base)
            entry['super_options'] = dict(aname=aname, cache=cache, trans=transport)
            row = DIAG.classify(entry, 'single_visible_mount', 'bind')
            self.assertEqual((row['backend'], row['cache'], row['transport']), (backend, expected_cache, expected_transport))
            self.assertNotIn('SECRET', json.dumps(row))
        entry = deepcopy(base);entry['super_options'] = {}
        self.assertEqual(DIAG.classify(entry, 'single_visible_mount', 'bind')['cache'], 'unspecified')

    def test_mountinfo_keeps_standard_fields_private_and_selects_top_stacked_mount(self):
        text = ('10 9 0:7 /root\\040with\\040space /fixture rw master:2 future:tag - 9p PRIVATE rw,aname=drvfs;path=PRIVATE,cache=none\n'
                '11 10 0:8 /top /fixture rw - ext4 PRIVATE rw\n')
        entries = DIAG.parse_mountinfo(text, self.graph.Budget())
        self.assertEqual(entries[0]['id'], 10)
        self.assertEqual(entries[0]['parent'], 9)
        self.assertEqual(entries[0]['root'], '/root with space')
        self.assertEqual(entries[0]['source'], 'PRIVATE')
        self.assertIn('aname', entries[0]['super_options'])
        selected, topology = DIAG._entry_for('/fixture/nested', entries)
        self.assertEqual(selected['id'], 11)
        self.assertEqual(topology, 'stacked_mounts')
        entries[1]['parent'] = 9
        with self.assertRaisesRegex(DIAG.DiagnosticError, 'ambiguous_mount_topology'):
            DIAG._entry_for('/fixture/nested', entries)

    def test_malformed_mountinfo_inventory_and_partial_provider_failures_block(self):
        for payload in ('', '1 0 0:1 / / rw - ext4 PRIVATE',
                        '1 0 0:1 / / rw,ro - ext4 PRIVATE rw',
                        '1 0 0:1 / / rw - ext4 PRIVATE rw\n1 0 0:1 / / rw - ext4 PRIVATE rw'):
            with self.subTest(payload=payload), self.assertRaises(DIAG.DiagnosticError):
                DIAG.parse_mountinfo(payload, self.graph.Budget())
        self.error = RuntimeError('PRIVATE_PATH SECRET_TOKEN')
        value = self.scan()
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['failure_phase'], 'docker_plugins')
        self.assertEqual(value['reason'], 'query_unavailable')
        self.assertEqual(value['counts'], [])
        self.assertNotIn('PRIVATE_', json.dumps(value))
        DIAG.validate(value, DIAG.APPROVED_SOURCE_SHA256)

    def test_mount_and_docker_changes_reject_every_partial_classification(self):
        self.final_mountinfo = self.mountinfo + b'5 1 0:9 / /new rw - tmpfs PRIVATE rw\n'
        value = self.scan()
        self.assertEqual(value['reason'], 'mountinfo_changed')
        self.assertEqual(value['counts'], [])
        self.trace.clear();self.final_mountinfo = self.mountinfo
        self.final_listing = self.listing[:-1]
        value = self.scan()
        self.assertEqual(value['reason'], 'docker_inventory_changed')
        self.assertEqual(value['mounts'], 0)

    def test_root_gate_precedes_source_read_without_elevation(self):
        with patch.object(DIAG, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(DIAG, 'os', SimpleNamespace(geteuid=lambda: 1000)), patch.object(DIAG, '_load') as loader:
            value = DIAG.collect(DIAG.APPROVED_SOURCE_SHA256)
        self.assertEqual(value['reason'], 'existing_root_required')
        self.assertEqual(value['uid'], 1000)
        loader.assert_not_called()

    def test_verified_source_bytes_only_and_no_main_or_path_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'collector.py'
            path.write_bytes(DIAG.SOURCE.read_bytes() + b'\nraise RuntimeError("PRIVATE")\n')
            drifted = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(DIAG, 'SOURCE', path):
                for pin in (DIAG.APPROVED_SOURCE_SHA256, drifted):
                    with self.assertRaisesRegex(DIAG.DiagnosticError, 'source_rejected'):
                        DIAG._load(pin)
                path.write_bytes(DIAG.SOURCE_LIMIT * b'x' + b'x')
                with self.assertRaisesRegex(DIAG.DiagnosticError, 'source_rejected'):
                    DIAG._load(DIAG.APPROVED_SOURCE_SHA256)
        self.assertNotEqual(self.graph.__name__, '__main__')
        self.assertEqual(self.graph.__collector_sha256__, DIAG.APPROVED_SOURCE_SHA256)

    def test_original_socket_trust_is_used_before_any_connection(self):
        fake = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_uid=0))
        with patch.object(self.graph, 'Path', return_value=fake), patch.object(self.graph.socket, 'socket') as connection:
            with self.assertRaisesRegex(self.graph.Blocked, 'untrusted_docker_socket'):
                self.graph.Budget().docker('info')
        connection.assert_not_called()

    def test_one_point_five_second_timer_and_handler_restoration(self):
        previous = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
        self.slow = True
        started = time.monotonic();value = self.scan()
        self.assertEqual(value['reason'], 'live_graph_deadline_exceeded')
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual((signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)), previous)
        DIAG.validate(value, DIAG.APPROVED_SOURCE_SHA256)

    def test_existing_timer_is_preserved_and_no_collection_is_started(self):
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            value = self.scan()
            self.assertEqual(value['reason'], 'collector_timer_already_owned')
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 4)
            self.assertEqual(self.trace, [])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_late_cleanup_or_finalization_cannot_extend_measured_budget(self):
        original_clock = time.monotonic
        original_signal = signal.signal
        termination = signal.getsignal(signal.SIGTERM)
        late = [False]
        def set_handler(number, handler):
            result = original_signal(number, handler)
            if number == signal.SIGTERM and handler is termination:
                late[0] = True
            return result
        def clock():
            return original_clock() + (2 if late[0] else 0)
        with patch.object(DIAG.signal, 'signal', side_effect=set_handler), patch.object(DIAG.time, 'monotonic', side_effect=clock):
            value = self.scan()
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['reason'], 'live_graph_deadline_exceeded')
        self.assertEqual(value['counts'], [])
        DIAG.validate(value, DIAG.APPROVED_SOURCE_SHA256)
        self.trace.clear()
        good = self.scan()
        bad = deepcopy(good);bad['elapsed_ms'] = 1500
        with self.assertRaises(DIAG.DiagnosticError):
            DIAG.validate(bad, DIAG.APPROVED_SOURCE_SHA256)
        with patch.object(DIAG.time, 'monotonic', return_value=101.5):
            value = DIAG._finalize(good, started=100)
        self.assertEqual(value['reason'], 'live_graph_deadline_exceeded')
        self.assertEqual(value['elapsed_ms'], 1500)

    def test_handler_and_timer_setup_failures_restore_every_existing_handler(self):
        previous_alarm, previous_term = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
        original_signal, original_timer = signal.signal, signal.setitimer
        for failure in ('alarm', 'term', 'timer', 'budget'):
            with self.subTest(failure=failure):
                def install(number, handler):
                    if ((failure == 'alarm' and number == signal.SIGALRM and handler is not previous_alarm)
                            or (failure == 'term' and number == signal.SIGTERM and handler is not previous_term)):
                        raise OSError('PRIVATE_SETUP_FAILURE')
                    return original_signal(number, handler)
                def timer(which, seconds, *args):
                    if failure == 'timer' and seconds:
                        raise OSError('PRIVATE_TIMER_FAILURE')
                    return original_timer(which, seconds, *args)
                class SetupBudget(self.fake_budget):
                    def __init__(self):
                        super().__init__()
                        if failure == 'budget':
                            self.deadline = 0
                with patch.object(self.graph, 'Budget', SetupBudget), \
                        patch.object(DIAG.signal, 'signal', side_effect=install), \
                        patch.object(DIAG.signal, 'setitimer', side_effect=timer):
                    value = DIAG._scan(self.graph)
                self.assertEqual(value['status'], 'blocked')
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous_alarm)
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                self.assertNotIn('PRIVATE_', json.dumps(value))

    def test_high_cardinality_collect_output_is_bounded_before_memory_bridge_return(self):
        good = self.scan()
        rows = []
        for mt, backend, cache, transport, topology in itertools.islice(itertools.product(
                ('bind', 'volume', 'tmpfs', 'other'), ('unknown', 'drvfs_declared'),
                ('none', 'mmap', 'loose', 'unknown'), ('fd', 'tcp'), tuple(DIAG.CLASSES['topology'])), DIAG.ROW_LIMIT):
            rows.append(dict(mount_type=mt, filesystem='9p', backend=backend, cache=cache,
                             transport=transport, topology=topology, mounts=1, rw_configured=1))
        good.update(containers=1, docker_queries=5, mounts=len(rows), counts=rows,
                    first_9p={name: rows[0][name] for name in DIAG.CLASSES} | {'rw_configured': True})
        DIAG.validate(good, DIAG.APPROVED_SOURCE_SHA256)
        size = len(json.dumps(good, allow_nan=False).encode())
        self.assertLess(size, DIAG.OUTPUT_LIMIT)
        # All live/source/provider seams are replaced; UID0 here is fixture
        # input only and cannot authorize any actual privileged operation.
        with patch.object(DIAG, 'sys', SimpleNamespace(platform='linux')), \
                patch.object(DIAG, 'os', SimpleNamespace(geteuid=lambda: 0)), \
                patch.object(DIAG, '_load', return_value=self.graph), \
                patch.object(DIAG, '_scan', return_value=good), patch.object(DIAG, 'OUTPUT_LIMIT', size - 1):
            value = DIAG.collect(DIAG.APPROVED_SOURCE_SHA256)
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(value['reason'], 'output_limit_exceeded')
        self.assertEqual(value['docker_mount_scan'], 'incomplete')
        self.assertEqual(value['counts'], [])
        self.assertLess(len(json.dumps(value).encode()), size - 1)
        DIAG.validate(value, DIAG.APPROVED_SOURCE_SHA256)

    def test_real_shared_payload_budget_and_invalid_inventory_never_finish(self):
        self.inspects['a' * 64]['Config']['Env'] = ['PRIVATE=' + 'x' * self.graph.LIMIT]
        value = self.scan()
        self.assertEqual(value['reason'], 'live_graph_payload_exceeded')
        self.assertEqual(value['counts'], [])
        self.trace.clear()
        self.listing.append(deepcopy(self.listing[0]))
        value = self.scan()
        self.assertEqual(value['reason'], 'invalid_docker_inventory')
        self.assertFalse(any(call[1].startswith('containers/a') for call in self.trace if call[0] == 'docker'))

    def test_json_request_and_strict_report_validator_never_accept_authority_or_private_rows(self):
        request = dict(schema_version=1, kind='ha-filesystem-diagnostic-request', source_sha256=DIAG.APPROVED_SOURCE_SHA256)
        good = self.scan()
        with patch.object(DIAG, 'collect', return_value=good) as collector:
            self.assertEqual(DIAG.collect_request(json.dumps(request).encode()), good)
        collector.assert_called_once_with(DIAG.APPROVED_SOURCE_SHA256)
        for raw in (b'x' * 1025, b'{"schema_version":1,"schema_version":1}', b'{"mode":"root"}'):
            self.assertEqual(DIAG.collect_request(raw)['reason'], 'request_rejected')
        bads = []
        for key, item in (('supports', True), ('ready', True), ('authority', True), ('uid', True),
                          ('elapsed_ms', float('nan')), ('docker_stability', 'stable'), ('verified', True)):
            bad = deepcopy(good);bad[key] = item;bads.append(bad)
        bad = deepcopy(good);bad['counts'][0]['backend'] = 'PRIVATE_VALUE';bads.append(bad)
        bad = deepcopy(good);bad['first_9p']['rw_configured'] = 1;bads.append(bad)
        for bad in bads:
            with self.assertRaises(DIAG.DiagnosticError):
                DIAG.validate(bad, DIAG.APPROVED_SOURCE_SHA256)

    def test_cli_explicit_local_only_no_stdout_exception_payload(self):
        with patch.object(DIAG, 'sys', SimpleNamespace(platform='darwin')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(DIAG.main(['--collect-local', '--source-sha256', 'PRIVATE_PIN']), 1)
        value = json.loads(output.getvalue())
        self.assertEqual(value['reason'], 'native_linux_required')
        self.assertNotIn('PRIVATE_PIN', output.getvalue())
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            DIAG.main(['--source-sha256', DIAG.APPROVED_SOURCE_SHA256])


if __name__ == '__main__':
    unittest.main()
