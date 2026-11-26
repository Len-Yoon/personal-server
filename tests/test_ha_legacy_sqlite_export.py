"""Exercise the reverse export's isolated source and real SQLite destination."""
from contextlib import contextmanager, closing
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

from psycopg.pq import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_runtime import legacy_sqlite_export as exporter
from ha_runtime.seven_migration import MigrationRejected


class Rows:
    def __init__(self, rows=()):
        self.rows = iter(rows)

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, size):
        result = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result


class RestoredSource:
    def __init__(self, expected_cluster):
        self.cluster = expected_cluster
        self.autocommit = True
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.read_only = False
        self.columns = {'parent': [('id', 'YES'), ('title', 'NO')],
                        'child': [('id', 'YES'), ('parent_id', 'NO'),
                                  ('payload', 'NO'), ('rating', 'NO')]}
        self.rows = {'parent': [(3, '한글'), (1, 'alpha')],
                     'child': [(12, 3, memoryview(b'\x01\x00\xff'), 3.25), (8, 1, None, None)]}
        self.bounds = {'parent': 99, 'child': 15}
        self.kind = 'r'
        self.extra = []
        self.sequence_reads = {}

    @contextmanager
    def transaction(self):
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield
        finally:
            self.info.transaction_status = TransactionStatus.IDLE
            self.read_only = False

    @contextmanager
    def cursor(self, *, name):
        assert name.startswith('legacy_export_')
        source = self

        class Cursor:
            def execute(self, query):
                self.result = source.execute(query)

            def fetchmany(self, size):
                return self.result.fetchmany(size)

        yield Cursor()

    def execute(self, query, parameters=()):
        if hasattr(query, 'as_string'):
            query = query.as_string(None)
        if query == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY':
            assert self.info.transaction_status == TransactionStatus.INTRANS
            self.read_only = True
            return Rows()
        assert self.read_only
        if query.startswith('SELECT current_database()'):
            return Rows([('ha', self.cluster, 'on')])
        if 'c.relkind IN' in query:
            return Rows([(name, self.kind) for name in self.columns] + [('ha_receipts', 'r'), ('ha_barriers', 'r')] + self.extra)
        if 'information_schema.columns' in query:
            return Rows(self.columns[parameters[1]])
        if 'pg_get_serial_sequence' in query:
            table = parameters[0].split('.')[-1].strip('"')
            return Rows([('s_book_memo_main', table + '_id_seq', 1, 1, 1, False, 1)])
        if query.startswith('SELECT last_value,is_called'):
            table = re.search(r'"([a-z]+)_id_seq"$', query)[1]
            self.sequence_reads[table] = self.sequence_reads.get(table, 0) + 1
            value = self.bounds[table]
            if isinstance(value, tuple):
                value = value[self.sequence_reads[table] - 1]
            return Rows([(value, True)])
        if query.startswith('SELECT "id",'):
            table = query.split('.')[-1].strip('"')
            return Rows(self.rows[table])
        raise AssertionError(query)


class LegacySqliteExportTests(unittest.TestCase):
    cluster = 'ha_legacy_export_' + 'a' * 32

    def setUp(self):
        self.stage = tempfile.TemporaryDirectory()
        self.addCleanup(self.stage.cleanup)
        self.folder = Path(self.stage.name).resolve()
        self.template, self.output = self.folder / 'frozen.sqlite3', self.folder / 'fresh.sqlite3'
        self.source = RestoredSource(self.cluster)
        self.make_template()

    def make_template(self, *, parent_autoincrement=True):
        self.template.unlink(missing_ok=True)
        self.template.touch(mode=0o600)
        with closing(sqlite3.connect(self.template)) as db:
            autocounter = ' AUTOINCREMENT' if parent_autoincrement else ''
            db.execute(f'CREATE TABLE parent(id INTEGER PRIMARY KEY{autocounter}, title TEXT NOT NULL)')
            db.execute('CREATE TABLE child(id INTEGER PRIMARY KEY AUTOINCREMENT,parent_id INTEGER REFERENCES parent(id),payload BLOB,rating REAL)')
            db.execute('CREATE INDEX child_parent ON child(parent_id)')
            db.execute("INSERT INTO parent(id,title) VALUES (75, 'old template')")
            db.execute('INSERT INTO child(id,parent_id) VALUES (27,75)')
            db.execute('DELETE FROM child')
            db.execute('DELETE FROM parent')
            db.commit()

    def export(self, **options):
        values = {'expected_cluster': self.cluster, 'service': 'book-memo', 'component': 'main',
                  'template': self.template, 'template_sha256': hashlib.sha256(self.template.read_bytes()).hexdigest(),
                  'output': self.output}
        return exporter.export_component(self.source, **(values | options))

    def test_rows_fingerprints_schema_and_high_water_marks(self):
        self.source.rows['child'] += [(position, 1, None, 0.5) for position in range(1000, 1300)]
        self.source.bounds['child'] = 1400
        original = self.template.read_bytes()
        receipt = self.export()
        self.assertEqual(self.template.read_bytes(), original)
        self.assertEqual(receipt['artifact_sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(receipt['tables']['parent']['identity_high_water'], 99)
        self.assertEqual(receipt['tables']['child']['identity_high_water'], 1400)
        self.assertEqual(receipt['tables']['child']['rows'], 302)
        self.assertEqual(set(self.folder.iterdir()), {self.output, self.template})
        with closing(sqlite3.connect(self.output)) as db:
            self.assertEqual(db.execute('SELECT * FROM parent ORDER BY id').fetchall(), [(1, 'alpha'), (3, '한글')])
            self.assertEqual(db.execute('SELECT * FROM child WHERE id<100 ORDER BY id').fetchall(),
                             [(8, 1, None, None), (12, 3, b'\x01\x00\xff', 3.25)])
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='child'").fetchone(), (1400,))
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='child_parent'").fetchone(), (1,))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute("INSERT INTO parent(title) VALUES('new')").lastrowid, 100)

    def test_digest_is_independent_of_source_row_order(self):
        first = self.export()
        self.source.rows['parent'].reverse()
        self.source.rows['child'].reverse()
        alternate = self.folder / 'alternate.sqlite3'
        second = self.export(output=alternate)
        self.assertEqual(first['tables'], second['tables'])

    def test_refuses_other_cluster_or_writable_connection(self):
        self.source.cluster = 'ha_A'
        with self.assertRaisesRegex(MigrationRejected, 'isolated_source_required'):
            self.export()
        self.assertFalse(self.output.exists())
        self.source.cluster = self.cluster
        self.source.autocommit = False
        with self.assertRaisesRegex(MigrationRejected, 'isolated_source_required'):
            self.export()

    def test_catalog_mismatches_fail_before_publication(self):
        for failure in ('missing_table', 'extra_table', 'wrong_kind', 'extra_column', 'missing_column', 'identity'):
            with self.subTest(failure=failure):
                self.source = RestoredSource(self.cluster)
                if failure == 'missing_table':
                    del self.source.columns['child']
                elif failure == 'extra_table':
                    self.source.extra.append(('not_exported', 'r'))
                elif failure == 'wrong_kind':
                    self.source.kind = 'v'
                elif failure == 'extra_column':
                    self.source.columns['child'].append(('extra', 'NO'))
                elif failure == 'missing_column':
                    self.source.columns['child'].pop()
                else:
                    self.source.columns['child'][0] = ('id', 'NO')
                with self.assertRaisesRegex(MigrationRejected, 'postgres_(tables|columns)_mismatch'):
                    self.export()
                self.assertFalse(self.output.exists())

    def test_preserves_existing_output_and_rejects_stale_template(self):
        self.output.write_bytes(b'keep')
        with self.assertRaisesRegex(MigrationRejected, 'frozen_inputs_and_new_output_required'):
            self.export()
        self.assertEqual(self.output.read_bytes(), b'keep')
        self.output.unlink()
        with self.assertRaisesRegex(MigrationRejected, 'frozen_artifact_changed'):
            self.export(template_sha256='0'*64)
        self.assertFalse(self.output.exists())
        Path(str(self.template) + '-wal').touch()
        with self.assertRaisesRegex(MigrationRejected, 'frozen_inputs_and_new_output_required'):
            self.export()

    def test_refuses_dtype_coercion_broken_reference_and_sequence_drift(self):
        self.source.rows['parent'][0] = (3, 123)
        with self.assertRaisesRegex(MigrationRejected, 'value_readback_mismatch'):
            self.export()
        self.source.rows['parent'][0] = (3, 'fixed')
        self.source.rows['child'][0] = (12, 404, b'', 1.0)
        with self.assertRaisesRegex(MigrationRejected, 'foreign_key_readback_mismatch'):
            self.export()
        self.source.rows['child'][0] = (12, 3, b'', 1.0)
        self.source.sequence_reads.clear()
        self.source.bounds['child'] = (15, 16)
        with self.assertRaisesRegex(MigrationRejected, 'source_sequence_changed'):
            self.export()
        self.assertFalse(self.output.exists())

    def test_plain_rowid_refuses_hidden_gap(self):
        self.make_template(parent_autoincrement=False)
        with self.assertRaisesRegex(MigrationRejected, 'rowid_high_water_unrepresentable'):
            self.export()
        self.assertFalse(self.output.exists())
        self.source.bounds['parent'] = 3
        self.assertEqual(self.export()['tables']['parent']['identity_high_water'], 3)


if __name__ == '__main__':
    unittest.main()
