"""Actual spawned SQLite failures and recovery against owned disposable fixtures."""
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'book-memo' / 'tools'
sys.path.insert(0, str(TOOLS))


def gated_snapshot_actor(connection, root, gate):
    """Delay only post-baseline snapshots, exposing the IPC phase boundary."""
    import incident_sqlite
    snapshot = incident_sqlite._snapshot
    calls = 0

    def gated(service):
        nonlocal calls
        calls += 1
        if calls > 1 and not gate.poll(8):
            raise RuntimeError('test snapshot gate timed out')
        return snapshot(service)

    incident_sqlite._snapshot = gated
    incident_sqlite.sqlite_actor(connection, root)


class SQLiteActorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='incident-sqlite-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.incident-owner').write_text('test-owner')
        self.ctx = multiprocessing.get_context('spawn')

    def start(self, locker=False, target=None, extra=()):
        from incident_sqlite import lock_actor, sqlite_actor
        parent, child = self.ctx.Pipe()
        process = self.ctx.Process(target=target or (lock_actor if locker else sqlite_actor),
                                   args=(child, str(self.root), *extra))
        process.start()
        child.close()
        self.addCleanup(self.cleanup_actor, parent, process)
        self.assertTrue(parent.poll(12), 'actor startup timed out')
        return parent, process, parent.recv()

    @staticmethod
    def cleanup_actor(pipe, process):
        if process.is_alive():
            try:
                pipe.send({'op': 'stop'})
            except (BrokenPipeError, OSError):
                pass
            process.join(1)
        if process.is_alive():
            process.terminate()
            process.join(5)
        pipe.close()

    def command(self, pipe, op, **extra):
        pipe.send({'op': op, **extra})
        self.assertTrue(pipe.poll(12), 'actor command timed out')
        return pipe.recv()

    def baseline(self):
        pipe, process, ready = self.start()
        self.assertEqual(ready, {'ready': True})
        baseline = self.command(pipe, 'baseline')
        self.assertTrue(baseline['ok'], baseline)
        self.assertEqual(len(baseline['data']['snapshot_hash']), 64)
        self.assertGreater(baseline['data']['page_count'], 0)
        return pipe, process, baseline

    def test_full_is_real_atomic_then_same_request_exactly_once(self):
        pipe, _, baseline = self.baseline()
        cap = self.command(pipe, 'constrain')
        self.assertTrue(cap['ok'], cap)
        self.assertEqual(cap['data']['max_page_count'], baseline['data']['page_count'])
        failed = self.command(pipe, 'probe', scenario='sqlite_full')
        self.assertEqual(failed, {'ok': True, 'data': {'error_code': sqlite3.SQLITE_FULL}})
        self.assertEqual(self.command(pipe, 'evidence'), {'ok': True, 'data': {'unchanged': True}})
        self.assertTrue(self.command(pipe, 'release')['ok'])
        verified = self.command(pipe, 'verify')
        self.assertTrue(verified['ok'], verified)
        for field in ('data_preserved', 'exactly_once', 'integrity_ok'):
            self.assertIs(verified['data'][field], True)
        with sqlite3.connect(self.root / 'fixture.sqlite3') as db:
            self.assertEqual(db.execute('SELECT version, length(content) FROM book_memos').fetchone(), (2, 65536))
            self.assertEqual(db.execute('SELECT count(*) FROM memo_revisions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM write_requests').fetchone()[0], 1)

    def test_busy_default_timeout_and_locker_release(self):
        pipe, _, _ = self.baseline()
        locker, process, ready = self.start(locker=True)
        self.assertEqual(ready, {'ready': True})
        started = time.monotonic()
        failed = self.command(pipe, 'probe', scenario='sqlite_busy')
        self.assertGreaterEqual(time.monotonic() - started, 4.5)
        self.assertEqual(failed, {'ok': True, 'data': {'error_code': sqlite3.SQLITE_BUSY}})
        self.assertEqual(self.command(pipe, 'evidence'), {'ok': True, 'data': {'unchanged': True}})
        self.assertTrue(self.command(locker, 'release')['ok'])
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertTrue(self.command(pipe, 'verify')['ok'])

    def test_wrong_scenario_or_absent_fault_cannot_report_failure(self):
        pipe, _, _ = self.baseline()
        self.assertFalse(self.command(pipe, 'probe', scenario='other')['ok'])
        self.assertFalse(self.command(pipe, 'probe', scenario='sqlite_full')['ok'])

    def test_evidence_requires_preceding_observed_failure(self):
        pipe, _, _ = self.baseline()
        self.assertEqual(self.command(pipe, 'evidence'), {'ok': False, 'reason': 'failure_required'})
        self.assertFalse(self.command(pipe, 'probe', scenario='sqlite_full')['ok'])
        self.assertEqual(self.command(pipe, 'evidence'), {'ok': False, 'reason': 'failure_required'})

    def test_snapshot_validation_runs_after_probe_response_in_evidence(self):
        gate, gate_writer = self.ctx.Pipe(duplex=False)
        self.addCleanup(gate.close)
        self.addCleanup(gate_writer.close)
        pipe, _, ready = self.start(target=gated_snapshot_actor, extra=(gate,))
        gate.close()
        self.assertEqual(ready, {'ready': True})
        self.assertTrue(self.command(pipe, 'baseline')['ok'])
        self.assertTrue(self.command(pipe, 'constrain')['ok'])
        pipe.send({'op': 'probe', 'scenario': 'sqlite_full'})
        self.assertTrue(pipe.poll(2), 'failure response waited for snapshot evidence')
        self.assertEqual(pipe.recv(), {'ok': True, 'data': {'error_code': sqlite3.SQLITE_FULL}})
        pipe.send({'op': 'evidence'})
        self.assertFalse(pipe.poll(0.2), 'evidence skipped the blocked snapshot validation')
        gate_writer.send(True)
        self.assertTrue(pipe.poll(2))
        self.assertEqual(pipe.recv(), {'ok': True, 'data': {'unchanged': True}})

    def test_preexisting_database_and_symlink_never_modified(self):
        fixture = self.root / 'fixture.sqlite3'
        fixture.write_bytes(b'unrelated database sentinel')
        pipe, _, _ = self.start()
        self.assertFalse(self.command(pipe, 'baseline')['ok'])
        self.assertEqual(fixture.read_bytes(), b'unrelated database sentinel')
        fixture.unlink()
        target = self.root / 'user.sqlite3'
        target.write_bytes(b'user sentinel')
        fixture.symlink_to(target)
        _, _, ready = self.start()
        self.assertEqual(ready, {'ok': False, 'reason': 'unsafe_fixture'})
        self.assertEqual(target.read_bytes(), b'user sentinel')

    def test_missing_or_symlink_owner_is_rejected(self):
        marker = self.root / '.incident-owner'
        marker.unlink()
        self.assertEqual(self.start()[2], {'ok': False, 'reason': 'unsafe_root'})
        target = self.root / 'owner'
        target.write_text('owner')
        marker.symlink_to(target)
        self.assertEqual(self.start()[2], {'ok': False, 'reason': 'unsafe_root'})

    def test_environment_db_path_does_not_touch_user_database(self):
        sentinel = self.root / 'user.sqlite3'
        sentinel.write_bytes(b'user sentinel')
        with patch.dict(os.environ, {'BOOK_MEMO_DB_PATH': str(sentinel), 'HTTP_PROXY': 'http://invalid'}):
            pipe, _, _ = self.baseline()
            self.assertTrue(self.command(pipe, 'constrain')['ok'])
        self.assertEqual(sentinel.read_bytes(), b'user sentinel')

    def test_terminated_locker_rolls_back_and_actor_recovers(self):
        pipe, _, _ = self.baseline()
        _, locker, ready = self.start(locker=True)
        self.assertEqual(ready, {'ready': True})
        locker.terminate()
        locker.join(5)
        self.assertFalse(locker.is_alive())
        self.assertTrue(self.command(pipe, 'verify')['ok'])

    def test_different_real_sqlite_code_is_rejected(self):
        pipe, _, _ = self.baseline()
        locker, _, ready = self.start(locker=True)
        self.assertEqual(ready, {'ready': True})
        failed = self.command(pipe, 'probe', scenario='sqlite_full')
        self.assertEqual(failed, {'ok': False, 'reason': 'unexpected_sqlite_code'})
        self.assertEqual(self.command(pipe, 'evidence'), {'ok': False, 'reason': 'failure_required'})
        self.assertTrue(self.command(locker, 'release')['ok'])
        self.assertTrue(self.command(pipe, 'verify')['ok'])

    def test_oversize_fixture_and_replaced_sidecar_are_rejected(self):
        fixture = self.root / 'fixture.sqlite3'
        with fixture.open('wb') as handle:
            handle.truncate(4 * 1024 * 1024 + 1)
        self.assertEqual(self.start()[2], {'ok': False, 'reason': 'fixture_too_large'})
        fixture.unlink()
        pipe, _, _ = self.baseline()
        sentinel = self.root / 'user-data'
        sentinel.write_bytes(b'preserve')
        (self.root / 'fixture.sqlite3-journal').symlink_to(sentinel)
        self.assertEqual(self.command(pipe, 'constrain'), {'ok': False, 'reason': 'unsafe_fixture'})
        self.assertEqual(sentinel.read_bytes(), b'preserve')

    def test_invalid_commands_and_peer_disconnect_exit(self):
        pipe, process, _ = self.start()
        pipe.send({'op': 'arbitrary', 'sql': 'DROP TABLE books'})
        self.assertTrue(pipe.poll(12))
        self.assertEqual(pipe.recv(), {'ok': False, 'reason': 'invalid_command'})
        pipe.close()
        process.join(5)
        self.assertEqual(process.exitcode, 0)


if __name__ == '__main__':
    unittest.main()
