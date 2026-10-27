"""Behavior contracts for read-only daily evidence aggregation."""

from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'infra/k8s/tools/slo-evidence-summary.py'


def record(day, **changes):
    value = {
        'date': day, 'collected_at': day + 'T00:00:00Z',
        'overall': 'ok', 'public_health': 'ok', 'portal_ready': 'ok',
        'crawler_freshness': 'ok', 'missing': [],
        'portal_http': {'requests': 100, 'server_errors': 2,
                        'server_error_ratio': .02, 'p95_seconds': .4},
    }
    value.update(changes)
    return value


class SloEvidenceSummaryTests(unittest.TestCase):
    def module(self):
        self.assertTrue(SCRIPT.is_file(), 'read-only summary tool missing')
        spec = importlib.util.spec_from_file_location('slo_summary', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_failures_and_unobserved_days_remain_separate(self):
        result = self.module().summarize_records([
            record('2026-10-02', public_health='failed'),
            record('2026-10-03', overall='unobservable', portal_ready='unobservable',
                   missing=['portal_ready']),
        ], date(2026, 10, 4), 4)
        self.assertEqual(result['missing_dates'], ['2026-10-01', '2026-10-04'])
        self.assertEqual(result['coverage_percent'], 50)
        self.assertEqual(result['collection'], {'ok_days': 1, 'unobservable_days': 1})
        self.assertEqual(result['sources']['public_health'], {
            'ok_days': 1, 'failed_days': 1, 'unobservable_days': 0,
            'missing_days': 2, 'observed_ok_percent': 50,
        })
        self.assertEqual(result['sources']['portal_ready']['unobservable_days'], 1)
        self.assertEqual(result['slo_budget']['status'], 'not_calculable')
        self.assertEqual(result['completeness'], 'incomplete')

    def test_complete_evidence_is_not_a_time_based_slo_claim(self):
        result = self.module().summarize_records([record('2026-10-04')], date(2026, 10, 4), 1)
        self.assertEqual(result['completeness'], 'complete')
        self.assertEqual(result['sources']['public_health']['observed_ok_percent'], 100)
        self.assertEqual(result['slo_budget']['reason'], 'daily_evidence_is_not_time_series')
        self.assertNotIn('availability_percent', result)

    def test_mixed_criteria_are_explicit_without_rewriting_old_records(self):
        old = record('2026-10-03')
        new = record('2026-10-04', criteria_version='freshness-1800-v1')
        summary = self.module().summarize_records([old, new], date(2026, 10, 4), 2)
        self.assertTrue(summary['criteria']['mixed'])
        self.assertEqual(summary['criteria']['record_counts'], {'legacy-freshness-900': 1, 'freshness-1800-v1': 1})
        self.assertNotIn('criteria_version', old)
        self.assertEqual(summary['slo_budget']['status'], 'not_calculable')

    def test_empty_records_have_no_success_ratio(self):
        result = self.module().summarize_records([], date(2026, 10, 4), 30)
        self.assertEqual(result['coverage_percent'], 0)
        self.assertEqual(len(result['missing_dates']), 30)
        self.assertIsNone(result['sources']['public_health']['observed_ok_percent'])
        self.assertIsNone(result['http']['window_weighted_error_ratio'])

    def test_http_uses_request_weights_not_average_of_ratios_or_p95(self):
        result = self.module().summarize_records([
            record('2026-10-03'),
            record('2026-10-04', portal_http={
                'requests': 900, 'server_errors': 0, 'server_error_ratio': 0, 'p95_seconds': 5,
            }),
        ], date(2026, 10, 4), 2)
        self.assertEqual(result['http']['window_weighted_error_ratio'], .002)
        self.assertEqual(result['http']['window_requests_sum'], 1000)
        self.assertEqual(result['http']['semantics'], 'sum_of_daily_rolling_windows_not_exact_period')
        self.assertNotIn('p95_seconds', result['http'])

    def test_zero_traffic_and_missing_http_are_not_success_observations(self):
        result = self.module().summarize_records([
            record('2026-10-03', portal_http={'requests': 0, 'server_errors': 0,
                   'server_error_ratio': None, 'p95_seconds': None}),
            record('2026-10-04', overall='unobservable', portal_http=None, missing=['portal_http']),
        ], date(2026, 10, 4), 2)
        self.assertEqual(result['http']['zero_traffic_days'], 1)
        self.assertEqual(result['http']['unobservable_days'], 1)
        self.assertIsNone(result['http']['window_weighted_error_ratio'])

    def test_duplicates_future_and_corrupt_records_rejected(self):
        for rows in (
            [record('2026-10-04'), record('2026-10-04')],
            [record('2026-10-05')],
            [record('2026-10-04', collected_at='2026-10-03T00:00:00Z')],
            [{'date': '2026-10-04'}],
            [record('2026-10-04', portal_http={'requests': 100, 'server_errors': 2,
                    'server_error_ratio': .5, 'p95_seconds': .4})],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.module().summarize_records(rows, date(2026, 10, 4), 30)

    def test_kst_date_uses_utc_collected_timestamp(self):
        result = self.module().summarize_records([
            record('2026-10-04', collected_at='2026-10-03T18:00:00Z'),
        ], date(2026, 10, 4), 1)
        self.assertEqual(result['present_days'], 1)

    def test_exact_period_excludes_old_rows_without_mutation(self):
        rows = [record('2026-09-30'), record('2026-10-01'), record('2026-10-04')]
        before = json.dumps(rows)
        result = self.module().summarize_records(rows, date(2026, 10, 4), 4)
        self.assertEqual(result['present_days'], 2)
        self.assertEqual(result['period']['start_date'], '2026-10-01')
        self.assertEqual(json.dumps(rows), before)

    def test_invalid_window_and_end_date_rejected(self):
        for days in (0, 31, True, 1.5):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.module().summarize_records([], date(2026, 10, 4), days)
        with self.assertRaises(ValueError):
            self.module().summarize_records([], '2026-10-04', 30)
        with self.assertRaises(ValueError):
            self.module().summarize_records([], date.min, 30)

    def test_overflowing_aggregate_is_rejected(self):
        rows = [record(day, portal_http={'requests': 1e308, 'server_errors': 0,
                'server_error_ratio': 0, 'p95_seconds': .1})
                for day in ('2026-10-03', '2026-10-04')]
        with self.assertRaises(ValueError):
            self.module().summarize_records(rows, date(2026, 10, 4), 2)

    def test_cli_configmap_and_array_match_and_do_not_modify_input(self):
        self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            for payload in ([record('2026-10-04')], {
                'data': {'records.json': json.dumps([record('2026-10-04')])},
            }):
                raw = json.dumps(payload)
                path.write_text(raw)
                result = subprocess.run([sys.executable, str(SCRIPT), '--input', str(path),
                    '--end-date', '2026-10-04', '--days', '1'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['present_days'], 1)
                self.assertEqual(path.read_text(), raw)

    def test_cli_invalid_input_does_not_echo_raw_data(self):
        self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            path.write_text('{"secret": "never-echo-this"}')
            result = subprocess.run([sys.executable, str(SCRIPT), '--input', str(path),
                '--end-date', '2026-10-04'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('never-echo-this', result.stdout + result.stderr)
            self.assertIn('invalid_input', result.stderr)


class SloSampleSummaryTests(unittest.TestCase):
    """Evidence-grid regression cases, separate from daily verdict aggregation."""

    def module(self):
        script = ROOT / 'infra/k8s/tools/slo-sample-summary.py'
        spec = importlib.util.spec_from_file_location('slo_samples', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def payload(self):
        start = 1792972800
        queries = {}
        for key in ('available', 'desired', 'available_timestamp', 'desired_timestamp'):
            values = [[start + i * 15, str(start + i * 15 if key.endswith('_timestamp') else 1)]
                      for i in range(3)]
            queries[key] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {'namespace': 'personal-server', 'deployment': 'portal-web'}, 'values': values}]}}
        return {'schema_version': 1, 'start': start, 'end': start + 30,
                'step_seconds': 15, 'queries': queries}

    def points(self, payload, name):
        return payload['queries'][name]['data']['result'][0]['values']

    def test_complete_success_grid_still_has_no_duration_or_budget_claim(self):
        p = self.payload()
        before = json.dumps(p, sort_keys=True)
        out = self.module().summarize_samples(p)
        self.assertEqual(out['samples']['good'], 3)
        self.assertEqual(out['complete_grid_success_percent'], 100)
        self.assertEqual(out['slo_budget']['status'], 'not_calculable')
        self.assertNotIn('downtime_seconds', out)
        self.assertEqual(json.dumps(p, sort_keys=True), before)

    def test_stop_and_rollout_both_remain_raw_bad_samples(self):
        p = self.payload()
        self.points(p, 'available')[0][1] = '0'
        self.points(p, 'available')[1][1] = '0'
        self.points(p, 'desired')[0][1] = '0'
        out = self.module().summarize_samples(p)
        self.assertEqual(out['samples']['bad'], 2)
        self.assertEqual(out['samples']['desired_zero_unavailable_candidates'], 1)
        self.assertAlmostEqual(out['complete_grid_success_percent'], 100 / 3)

    def test_missing_desired_or_source_timestamp_withholds_complete_ratio(self):
        for name in ('desired', 'available_timestamp', 'desired_timestamp'):
            with self.subTest(name=name):
                p = self.payload()
                del self.points(p, name)[1]
                out = self.module().summarize_samples(p)
                self.assertEqual(out['samples']['unknown_reasons']['missing'], 1)
                self.assertEqual(out['observed_sample_success_percent'], 100)
                self.assertAlmostEqual(out['coverage_percent'], 200 / 3)
                self.assertIsNone(out['complete_grid_success_percent'])

    def test_empty_series_is_all_unknown(self):
        p = self.payload()
        p['queries']['available']['data']['result'] = []
        out = self.module().summarize_samples(p)
        self.assertEqual(out['samples']['unknown'], 3)
        self.assertIsNone(out['observed_sample_success_percent'])

    def test_lookback_reused_samples_expire_at_source_age_boundary(self):
        p = self.payload()
        self.points(p, 'available_timestamp')[0][1] = str(p['start'] - 60)
        self.points(p, 'available_timestamp')[1][1] = str(p['start'] + 15 - 60.001)
        self.points(p, 'desired_timestamp')[2][1] = str(p['start'] + 30 - 61)
        out = self.module().summarize_samples(p)
        self.assertEqual(out['samples']['good'], 1)
        self.assertEqual(out['samples']['unknown_reasons']['stale'], 2)

    def test_future_source_time_and_nonfinite_values_are_unknown(self):
        for value, reason in (('NaN', 'nonfinite'), ('+Inf', 'nonfinite'),
                              (str(self.payload()['start'] + .001), 'future_timestamp'),
                              (str(self.payload()['start'] + 1), 'future_timestamp')):
            with self.subTest(value=value):
                p = self.payload()
                self.points(p, 'available_timestamp')[0][1] = value
                out = self.module().summarize_samples(p)
                self.assertEqual(out['samples']['unknown_reasons'][reason], 1)

    def test_duplicate_out_of_order_off_grid_and_out_of_window_points_rejected(self):
        for offsets in ((0, 0, 30), (0, 30, 15), (0, 16, 30), (-15, 15, 30), (0, 15, 45)):
            with self.subTest(offsets=offsets):
                p = self.payload()
                self.points(p, 'available')[:] = [[p['start'] + offset, '1'] for offset in offsets]
                with self.assertRaises(ValueError):
                    self.module().summarize_samples(p)

    def test_multiple_series_and_mismatched_selectors_never_aggregate_silently(self):
        for mutation in ('duplicate_series', 'wrong_app', 'different_source'):
            p = self.payload()
            row = p['queries']['available']['data']['result'][0]
            if mutation == 'duplicate_series':
                p['queries']['available']['data']['result'].append(row)
            elif mutation == 'wrong_app':
                row['metric']['deployment'] = 'another-app'
            else:
                row['metric']['instance'] = 'different-source'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.module().summarize_samples(p)

    def test_invalid_period_types_bounds_and_replica_counts_rejected(self):
        for field, value in (('schema_version', True), ('start', True), ('start', -1),
                             ('end', self.payload()['start'] + 604815), ('step_seconds', 0),
                             ('end', self.payload()['start'] + 31)):
            p = self.payload()
            p[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.module().summarize_samples(p)
        for value in ('-1', '0.5', True, {}, 'not-a-number'):
            p = self.payload()
            self.points(p, 'available')[0][1] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.module().summarize_samples(p)

    def test_partial_or_wrong_prometheus_response_rejected(self):
        for mutation in ('warning', 'error', 'instant'):
            p = self.payload()
            response = p['queries']['available']
            if mutation == 'warning':
                response['warnings'] = ['never echo internal detail']
            elif mutation == 'error':
                response['status'] = 'error'
            else:
                response['data']['resultType'] = 'vector'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.module().summarize_samples(p)

    def test_cli_does_not_echo_labels_paths_or_parse_errors(self):
        script = ROOT / 'infra/k8s/tools/slo-sample-summary.py'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private-input-label.json'
            p = self.payload()
            for response in p['queries'].values():
                response['data']['result'][0]['metric']['private_label'] = 'never-echo-this'
            path.write_text(json.dumps(p))
            result = subprocess.run([sys.executable, str(script), '--input', str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('never-echo-this', result.stdout + result.stderr)
            path.write_text('{"secret":"never-echo-this",')
            result = subprocess.run([sys.executable, str(script), '--input', str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(str(path), result.stderr)
            self.assertNotIn('never-echo-this', result.stdout + result.stderr)

    def test_cli_rejects_oversized_and_deep_json_without_raw_traceback(self):
        script = ROOT / 'infra/k8s/tools/slo-sample-summary.py'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.json'
            for raw in (b' ' * (32 * 1024 * 1024 + 1), b'[' * 2000 + b']' * 2000):
                path.write_bytes(raw)
                result = subprocess.run([sys.executable, str(script), '--input', str(path)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), 'slo_sample_summary=FAIL reason=invalid_input')


class SloObservationArchiveTests(unittest.TestCase):
    def module(self):
        path = ROOT / 'infra/k8s/tools/slo-observation-archive.py'
        spec = importlib.util.spec_from_file_location('slo_archive', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def payload(self, day='2026-10-26', complete=False):
        from datetime import datetime, timezone
        start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        queries = {}
        for name in ('available', 'desired', 'available_timestamp', 'desired_timestamp'):
            points = 5760 if complete else 3
            values = [[start + i * 15, str(start + i * 15 if name.endswith('_timestamp') else 1)]
                      for i in range(points)]
            queries[name] = {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {'namespace': 'personal-server', 'deployment': 'portal-web',
                            'private_label': 'never-retain-internal-source'}, 'values': values}]}}
        return {'schema_version': 1, 'start': start, 'end': start + 86400 - 15,
                'step_seconds': 15, 'queries': queries}

    def test_partial_grid_is_immutable_and_gaps_are_not_backfilled(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            original = self.payload()
            one = module.archive(original, folder, today=date(2026, 10, 28))
            self.assertEqual(one['status'], 'partial')
            self.assertEqual(one['summary']['samples']['unknown'], 5757)
            repeated = module.archive(original, folder, today=date(2026, 10, 28))
            self.assertTrue(repeated['idempotent'])
            changed = self.payload()
            changed['queries']['available']['data']['result'][0]['values'][0][1] = '0'
            before = (folder / '2026-10-26.json.gz').read_bytes()
            with self.assertRaises(ValueError):
                module.archive(changed, folder, today=date(2026, 10, 28))
            self.assertEqual((folder / '2026-10-26.json.gz').read_bytes(), before)
            out = module.verify(folder, today=date(2026, 10, 28))
            self.assertEqual(out['partial_dates'], ['2026-10-26'])
            self.assertIn('2026-10-27', out['missing_dates'])

    def test_complete_grid_retains_original_timestamp_and_drops_raw_labels(self):
        import gzip
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            value = self.payload(complete=True)
            out = module.archive(value, folder, today=date(2026, 10, 28))
            self.assertEqual(out['status'], 'complete')
            raw = gzip.decompress((folder / '2026-10-26.json.gz').read_bytes())
            self.assertNotIn(b'never-retain-internal-source', raw)
            saved = json.loads(raw)['payload']
            self.assertEqual(saved['queries']['available_timestamp']['data']['result'][0]['values'],
                             value['queries']['available_timestamp']['data']['result'][0]['values'])
            self.assertEqual(module.verify(folder, today=date(2026, 10, 28))['partial_dates'], [])

    def test_initialize_will_not_claim_existing_user_directory(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'user'; path.mkdir(); sentinel = path / 'keep'; sentinel.write_text('keep')
            with self.assertRaises(FileExistsError):
                module.initialize(path)
            self.assertEqual(sentinel.read_text(), 'keep')
            with self.assertRaises((OSError, ValueError)):
                module.archive(self.payload(), path, today=date(2026, 10, 28))

    def test_corruption_symlinks_hardlinks_and_unknown_files_block_writes(self):
        import os
        module = self.module()
        for kind in ('corrupt', 'symlink', 'hardlink', 'unknown'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp) / 'archive'; module.initialize(folder)
                module.archive(self.payload(), folder, today=date(2026, 10, 28))
                path = folder / '2026-10-26.json.gz'
                sentinel = Path(temp) / 'keep'; sentinel.write_bytes(b'keep')
                if kind == 'corrupt':
                    path.write_bytes(b'corrupt')
                elif kind == 'symlink':
                    path.unlink(); path.symlink_to(sentinel)
                elif kind == 'hardlink':
                    path.unlink(); os.link(sentinel, path)
                else:
                    (folder / 'user-file').write_text('user')
                with self.assertRaises((OSError, ValueError, EOFError)):
                    module.archive(self.payload('2026-10-27'), folder, today=date(2026, 10, 28))
                self.assertFalse((folder / '2026-10-27.json.gz').exists())
                self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_digest_tamper_and_gzip_bomb_are_rejected(self):
        import gzip
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            module.archive(self.payload(), folder, today=date(2026, 10, 28))
            path = folder / '2026-10-26.json.gz'
            capsule = json.loads(gzip.decompress(path.read_bytes()))
            capsule['payload_sha256'] = '0' * 64
            path.write_bytes(gzip.compress(json.dumps(capsule).encode()))
            with self.assertRaises(ValueError):
                module.verify(folder, today=date(2026, 10, 28))
            path.write_bytes(gzip.compress(b' ' * (module.LIMIT + 1)))
            with self.assertRaises(ValueError):
                module.verify(folder, today=date(2026, 10, 28))

    def test_expiry_prune_removes_only_verified_days_older_than_boundary(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            for day in ('2026-10-26', '2026-10-27'):
                module.archive(self.payload(day), folder, today=date(2026, 10, 28))
            out = module.archive(self.payload('2026-11-30'), folder, today=date(2026, 12, 1), prune=True)
            self.assertEqual(out['expired_removed'], 1)
            self.assertFalse((folder / '2026-10-26.json.gz').exists())
            self.assertTrue((folder / '2026-10-27.json.gz').exists())

    def test_new_write_failure_preserves_previous_archive_and_cleans_pending(self):
        from unittest.mock import patch
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            module.archive(self.payload(), folder, today=date(2026, 10, 28))
            before = (folder / '2026-10-26.json.gz').read_bytes()
            with patch.object(module.os, 'link', side_effect=OSError('never-echo-this')):
                with self.assertRaises(OSError):
                    module.archive(self.payload('2026-10-27'), folder, today=date(2026, 10, 28), prune=True)
            self.assertEqual((folder / '2026-10-26.json.gz').read_bytes(), before)
            self.assertEqual(sorted(x.name for x in folder.iterdir()), ['.lock', '.owner', '2026-10-26.json.gz'])

    def test_quota_and_free_space_fail_closed_without_new_file(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            with patch.object(module, 'QUOTA', 1):
                with self.assertRaises(ValueError):
                    module.archive(self.payload(), folder, today=date(2026, 10, 28))
            with patch.object(module.os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
                with self.assertRaises(ValueError):
                    module.archive(self.payload(), folder, today=date(2026, 10, 28))
            self.assertFalse((folder / '2026-10-26.json.gz').exists())

    def test_lock_blocks_duplicate_writer(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            fd, lock = module.open_archive(folder)
            try:
                with self.assertRaises(BlockingIOError):
                    module.archive(self.payload(), folder, today=date(2026, 10, 28))
            finally:
                module.os.close(lock); module.os.close(fd)

    def test_partial_days_and_today_are_not_silently_normalized(self):
        module = self.module()
        for mutation in ('today', 'start', 'end'):
            p = self.payload()
            if mutation == 'start':
                p['start'] += 15
            elif mutation == 'end':
                p['end'] -= 15
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                module.checked_payload(p, date(2026, 10, 26) if mutation == 'today' else date(2026, 10, 28))

    def test_collector_uses_completed_grid_and_bounded_read1_without_redirects(self):
        from unittest.mock import patch
        import io
        module = self.module()
        payload = self.payload()
        responses = []
        for name in module._ANALYZER.QUERIES:
            class Response(io.BytesIO):
                pass
            responses.append(Response(json.dumps(payload['queries'][name]).encode()))
        for _ in range(2):
            responses.append(Response(json.dumps({'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}).encode()))
        with patch.object(module, 'urlopen', side_effect=responses) as request:
            actual = module.collect(date(2026, 10, 26))
        self.assertEqual(actual['end'] - actual['start'], 86400 - 15)
        self.assertEqual(request.call_count, 6)
        for call in request.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 10)
        with self.assertRaises(ValueError):
            module.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://other.example')
        for url in ('https://user:secret@localhost', 'https://outside.example', 'file:///tmp/input',
                    'http://localhost/?secret=never-echo-this'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                module.collect(date(2026, 10, 26), url)

    def test_collector_deadline_and_large_response_abort(self):
        from unittest.mock import patch
        import io
        module = self.module()
        with patch.object(module.time, 'monotonic', side_effect=[0, 46]):
            with self.assertRaises(TimeoutError):
                module.collect(date(2026, 10, 26))
        with patch.object(module, 'urlopen', return_value=io.BytesIO(b' ' * (module.LIMIT + 1))):
            with self.assertRaises(ValueError):
                module.collect(date(2026, 10, 26))

    def test_cli_partial_returns_two_and_does_not_leak_raw_labels(self):
        script = ROOT / 'infra/k8s/tools/slo-observation-archive.py'
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'input.json'; source.write_text(json.dumps(self.payload()))
            folder = Path(temp) / 'archive'
            out = subprocess.run([sys.executable, str(script), '--input', str(source),
                                  '--directory', str(folder), '--initialize'], capture_output=True, text=True)
            self.assertEqual(out.returncode, 2, out.stderr)
            self.assertEqual(json.loads(out.stdout)['status'], 'partial')
            self.assertNotIn('never-retain-internal-source', out.stdout + out.stderr)
            source.write_text('{"private":"never-echo-this",')
            out = subprocess.run([sys.executable, str(script), '--input', str(source),
                                  '--directory', str(folder)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertNotIn('never-echo-this', out.stdout + out.stderr)
            self.assertNotIn(str(source), out.stderr)

    def search(self, payload):
        start = payload['start']
        labels = {'outcome': 'complete', 'namespace': 'personal-server',
                  'private_label': 'never-retain-internal-source'}
        payload['search'] = {'step_seconds': 60,
            'values': {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': {'__name__': 'portal_search_requests_total', **labels},
                 'values': [[start, '0'], [start + 60, '1']]}]}},
            'timestamps': {'status': 'success', 'data': {'resultType': 'matrix', 'result': [
                {'metric': dict(labels), 'values': [[start, str(start)], [start + 60, str(start + 59)]]}]}}}
        return payload

    def test_search_counter_reset_source_times_and_zero_values_survive_archive(self):
        import gzip
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            payload = self.search(self.payload())
            original = json.dumps(payload, sort_keys=True)
            out = module.archive(payload, folder, today=date(2026, 10, 28))
            self.assertEqual(out['summary']['search_archive']['status'], 'preserved')
            raw = gzip.decompress((folder / '2026-10-26.json.gz').read_bytes())
            self.assertNotIn(b'never-retain-internal-source', raw)
            stored = json.loads(raw)['payload']['search']
            self.assertEqual(stored['values']['data']['result'][0]['values'],
                             payload['search']['values']['data']['result'][0]['values'])
            self.assertEqual(stored['timestamps']['data']['result'][0]['values'],
                             payload['search']['timestamps']['data']['result'][0]['values'])
            self.assertEqual(json.dumps(payload, sort_keys=True), original)
            self.assertEqual(module.verify(folder, today=date(2026, 10, 28))['search_unobserved_dates'], [])

    def test_missing_search_timestamps_are_preserved_as_unobserved_not_filled(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            payload = self.search(self.payload())
            payload['search']['timestamps']['data']['result'] = []
            out = module.archive(payload, folder, today=date(2026, 10, 28))
            self.assertEqual(out['summary']['search_archive']['status'], 'empty_or_missing')
            self.assertEqual(module.verify(folder, today=date(2026, 10, 28))['search_unobserved_dates'], ['2026-10-26'])

    def test_search_scope_cardinality_grid_and_metric_labels_are_bounded(self):
        module = self.module()
        for kind in ('source', 'scope', 'family', 'duplicate', 'off_grid', 'too_many'):
            payload = self.search(self.payload())
            response = payload['search']['values']
            row = response['data']['result'][0]
            if kind == 'source':
                row['metric']['source'] = 'private-unbounded-source'
            elif kind == 'scope':
                row['metric']['namespace'] = 'other'
            elif kind == 'family':
                row['metric']['__name__'] = 'private_metric'
            elif kind == 'duplicate':
                response['data']['result'].append(row)
            elif kind == 'off_grid':
                row['values'][1][0] += 1
            else:
                response['data']['result'] *= 129
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                module.checked_payload(payload, date(2026, 10, 28))

    def test_prometheus_normalized_bucket_labels_remain_bounded(self):
        module = self.module()
        for label, wanted in (('1.0', '1'), ('5.0', '5'), ('10.0', '10'), ('+Inf', '+Inf')):
            payload = self.search(self.payload())
            for key in ('values', 'timestamps'):
                row = payload['search'][key]['data']['result'][0]
                row['metric'] = {'method': 'GET', 'route': '/search', 'le': label}
                if key == 'values':
                    row['metric']['__name__'] = 'portal_http_request_duration_seconds_bucket'
            _, clean, _ = module.checked_payload(payload, date(2026, 10, 28))
            self.assertEqual(clean['search']['values']['data']['result'][0]['metric']['le'], wanted)

    def test_prune_recovers_full_old_store_without_incoming_snapshot(self):
        from unittest.mock import patch
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            module.archive(self.payload(), folder, today=date(2026, 10, 28))
            with patch.object(module, 'QUOTA', 1):
                self.assertEqual(module.prune_expired(folder, today=date(2026, 12, 1))['expired_removed'], 1)
            self.assertEqual(module.verify(folder, today=date(2026, 12, 1))['present_days'], 0)
            with self.assertRaises(ValueError):
                module.archive(self.payload(), folder, today=date(2026, 12, 1), prune=True)
            self.assertFalse((folder / '2026-10-26.json.gz').exists())

    def test_expired_replacement_is_preserved_instead_of_deleted(self):
        from unittest.mock import patch
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            module.archive(self.payload(), folder, today=date(2026, 10, 28))
            real = module.entries
            def replace(directory, today, held=None, enforce_quota=True):
                result = real(directory, today, held, enforce_quota)
                target = folder / '2026-10-26.json.gz'
                target.unlink(); target.write_bytes(b'keep-new-file')
                return result
            with patch.object(module, 'entries', side_effect=replace):
                with self.assertRaises(ValueError):
                    module.prune_expired(folder, today=date(2026, 12, 1))
            self.assertEqual((folder / '2026-10-26.json.gz').read_bytes(), b'keep-new-file')

    def test_installation_stays_suspended_without_secret_or_api_token_mounts(self):
        import yaml
        path = ROOT / 'infra/k8s/slo-evidence/archive/cronjob.yaml'
        pvc, cron = list(yaml.safe_load_all(path.read_text()))
        self.assertEqual(pvc['kind'], 'PersistentVolumeClaim')
        self.assertTrue(cron['spec']['suspend'])
        self.assertEqual(cron['spec']['concurrencyPolicy'], 'Forbid')
        pod = cron['spec']['jobTemplate']['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['securityContext']['runAsUser'], 10001)
        self.assertEqual(len(pod['volumes']), 1)
        self.assertNotIn('secret', pod['volumes'][0])
        container = pod['containers'][0]
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['imagePullPolicy'], 'Never')
        self.assertNotIn('envFrom', container)

    def test_cli_deflate_and_http_protocol_errors_never_echo_source_details(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout, redirect_stderr
        from http.client import IncompleteRead, BadStatusLine
        import io
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'archive'; module.initialize(folder)
            (folder / '2026-10-26.json.gz').write_bytes(bytes.fromhex('1f8b0800000000000003') + b'\xff' * 20)
            error = io.StringIO()
            with redirect_stderr(error), redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(['--verify', '--directory', str(folder)]), 1)
            self.assertEqual(error.getvalue().strip(), 'slo_observation_archive=FAIL reason=invalid_input_or_storage')
            for exception in (IncompleteRead(b'never-echo-this', 100), BadStatusLine('never-echo-this')):
                error = io.StringIO()
                with patch.object(module, 'urlopen', side_effect=exception), redirect_stderr(error), redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(['--collect', '--date', '2026-10-26', '--directory', str(folder)]), 1)
                self.assertNotIn('never-echo-this', error.getvalue())
                self.assertNotIn(str(folder), error.getvalue())


class SloObservationHealthTests(unittest.TestCase):
    def module(self):
        path = ROOT / 'infra/k8s/tools/slo-observation-health.py'
        spec = importlib.util.spec_from_file_location('observation_health', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def prepared(self, root):
        module = self.module()
        folder = Path(root) / 'archive'
        module.ARCHIVE.initialize(folder)
        module.ARCHIVE.archive(SloObservationArchiveTests().payload(), folder, today=date(2026, 10, 28))
        return module, folder

    def test_verified_capsule_does_not_imply_search_or_replica_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
            out = module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 10, 28))
            self.assertEqual(out['present_days'], 1)
            self.assertEqual(out['partial_replica_days'], 1)
            self.assertEqual(out['search_series_unobserved_days'], 1)
            self.assertEqual(out['latest_preserved_day_timestamp_seconds'], 1792972800)
            self.assertGreater(out['stored_bytes'], 0)
            self.assertGreater(out['filesystem_available_bytes'], 0)
            after = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in folder.iterdir()}
            self.assertEqual(before, after)

    def test_readonly_monitor_never_creates_missing_lock_or_archive(self):
        with tempfile.TemporaryDirectory() as root:
            module = self.module()
            folder = Path(root) / 'archive'
            with self.assertRaises(FileNotFoundError):
                module.snapshot(folder, required_from=date(2026, 10, 26))
            self.assertFalse(folder.exists())
            module.ARCHIVE.initialize(folder)
            with self.assertRaises(FileNotFoundError):
                module.snapshot(folder, required_from=date(2026, 10, 26))
            self.assertEqual([p.name for p in folder.iterdir()], ['.owner'])

    def test_reader_never_takes_a_lock_or_blocks_existing_exclusive_writer(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            directory, lock = module.ARCHIVE.open_archive(folder)
            try:
                self.assertEqual(module.snapshot(folder, required_from=date(2026, 10, 26))['present_days'], 1)
                self.assertEqual(module.worker(folder, date(2026, 10, 26))[0], 'verified')
            finally:
                os.close(lock); os.close(directory)
            directory, lock = module.open_readonly(folder)
            try:
                self.assertEqual(module.snapshot(folder, required_from=date(2026, 10, 26))['present_days'], 1)
                writer_directory, writer_lock = module.ARCHIVE.open_archive(folder)
                os.close(writer_lock); os.close(writer_directory)
            finally:
                os.close(lock); os.close(directory)

    def test_overlapping_real_writer_succeeds_and_reader_reports_retry(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            original = module.ARCHIVE.entries
            written = []
            def entries(*args, **kwargs):
                if not written:
                    written.append(True)
                    module.ARCHIVE.archive(SloObservationArchiveTests().payload('2026-10-27'), folder,
                                          today=date(2026, 10, 28))
                return original(*args, **kwargs)
            with patch.object(module.ARCHIVE, 'entries', side_effect=entries):
                with self.assertRaises(module.ConcurrentChange):
                    module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 10, 28))
            self.assertTrue((folder / '2026-10-27.json.gz').is_file())
            self.assertEqual(module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 10, 28))['present_days'], 2)

    def test_recent_pending_is_retry_but_stale_pending_unknown_files_and_corruption_fail(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            pending = folder / ('.pending-' + 'a' * 32)
            pending.write_bytes(b'pending'); pending.chmod(0o600)
            with self.assertRaises(module.ConcurrentChange):
                module.snapshot(folder, required_from=date(2026, 10, 26))
            self.assertEqual(module.worker(folder, date(2026, 10, 26)), ('busy', None))
            os.utime(pending, (1, 1))
            self.assertEqual(module.worker(folder, date(2026, 10, 26)), ('invalid', None))
            pending.unlink()
            (folder / 'unknown').write_bytes(b'unknown')
            self.assertEqual(module.worker(folder, date(2026, 10, 26)), ('invalid', None))

    def test_current_directory_identity_replacement_is_retry(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            original = module.ARCHIVE.entries
            def entries(*args, **kwargs):
                rows = original(*args, **kwargs)
                folder.rename(Path(root) / 'old')
                folder.mkdir(mode=0o700)
                return rows
            with patch.object(module.ARCHIVE, 'entries', side_effect=entries):
                with self.assertRaises(module.ConcurrentChange):
                    module.snapshot(folder, required_from=date(2026, 10, 26))

    def test_deleted_historical_file_is_missing_even_with_latest_present(self):
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            module.ARCHIVE.archive(SloObservationArchiveTests().payload('2026-10-27'), folder,
                                  today=date(2026, 10, 28))
            out = module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 10, 28))
            self.assertEqual((out['required_days'], out['missing_required_days']), (2, 0))
            (folder / '2026-10-26.json.gz').unlink()
            after = module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 10, 28))
            self.assertEqual(after['latest_preserved_day_timestamp_seconds'], out['latest_preserved_day_timestamp_seconds'])
            self.assertEqual((after['required_days'], after['missing_required_days']), (2, 1))

    def test_required_window_has_no_invented_initial_history_and_honors_grace_and_retention(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            before = module.snapshot(folder, required_from=date(2026, 10, 26), now=datetime(2026, 10, 28, 0, 44, tzinfo=timezone.utc))
            after = module.snapshot(folder, required_from=date(2026, 10, 26), now=datetime(2026, 10, 28, 0, 45, tzinfo=timezone.utc))
            self.assertEqual((before['required_days'], before['missing_required_days']), (1, 0))
            self.assertEqual((after['required_days'], after['missing_required_days']), (2, 1))
            later = module.snapshot(folder, required_from=date(2026, 10, 26), today=date(2026, 12, 10))
            self.assertEqual((later['required_days'], later['missing_required_days']), (35, 35))
            with self.assertRaises(ValueError):
                module.snapshot(folder, required_from=date(2026, 10, 29), today=date(2026, 10, 28))

    def test_corrupt_capsule_is_fixed_failure_without_path_or_payload_disclosure(self):
        with tempfile.TemporaryDirectory() as root:
            module, folder = self.prepared(root)
            (folder / '2026-10-26.json.gz').write_bytes(b'private-bad-gzip-never-echo')
            self.assertEqual(module.worker(folder, date(2026, 10, 26)), ('invalid', None))
            result = subprocess.run([sys.executable, str(ROOT / 'infra/k8s/tools/slo-observation-health.py'),
                                     '--snapshot', '--directory', str(folder), '--required-from', '2026-10-26'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, '{"status":"invalid"}\n')
            self.assertEqual(result.stderr, '')

    def test_worker_timeout_and_malformed_output_fail_closed(self):
        from unittest.mock import patch
        module = self.module()
        with patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private', 25)):
            self.assertEqual(module.worker('unused', date(2026, 10, 26)), ('invalid', None))
        for result in (subprocess.CompletedProcess([], 0, b'{}', b''),
                       subprocess.CompletedProcess([], 0, b'{}', b'private-error'),
                       subprocess.CompletedProcess([], 3, b'{}', b'')):
            with patch.object(module.subprocess, 'run', return_value=result):
                self.assertEqual(module.worker('unused', date(2026, 10, 26)), ('invalid', None))

    def test_busy_does_not_refresh_integrity_success_time_and_error_clears_data(self):
        module = self.module()
        now = [1000]
        answers = [('verified', {'present_days': 1}), ('busy', None), ('invalid', None)]
        monitor = module.Collector('unused', date(2026, 10, 26), reader=lambda _path, _start: answers.pop(0), clock=lambda: now[0])
        initial = monitor.render().decode()
        self.assertIn('slo_observation_archive_verification_success 0\n', initial)
        self.assertNotIn('slo_observation_archive_present_days', initial)
        monitor.refresh(); now[0] = 2000; monitor.refresh()
        self.assertEqual(monitor.last_verified, 1000)
        self.assertEqual(monitor.busy, 1)
        self.assertEqual(monitor.success, 1)
        monitor.refresh()
        self.assertEqual(monitor.last_verified, 1000)
        self.assertEqual(monitor.success, 0)
        self.assertNotIn('slo_observation_archive_present_days', monitor.render().decode())

    def test_concurrent_refresh_cannot_start_second_whole_archive_read(self):
        module = self.module()
        calls = []
        monitor = module.Collector('unused', date(2026, 10, 26), reader=lambda _path, _start: (calls.append(1), None))
        monitor.running.acquire()
        try:
            monitor.refresh()
        finally:
            monitor.running.release()
        self.assertEqual(calls, [])

    def test_monitor_manifest_is_readonly_internal_and_never_mutates_collector(self):
        import yaml
        docs = list(yaml.safe_load_all((ROOT / 'infra/k8s/slo-evidence/archive/health-monitor.yaml').read_text()))
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertNotIn('serviceAccountName', pod)
        self.assertNotIn('fsGroup', pod['securityContext'])
        self.assertEqual(pod['securityContext']['runAsUser'], 10001)
        self.assertEqual(pod['volumes'], [{'name': 'archive', 'persistentVolumeClaim': {'claimName': 'slo-observation-archive', 'readOnly': True}}])
        container = pod['containers'][0]
        self.assertTrue(container['volumeMounts'][0]['readOnly'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertNotIn('env', container)
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertFalse(any(d['kind'] in ('CronJob', 'PersistentVolumeClaim', 'Secret', 'Service', 'Ingress') for d in docs))

    def test_archive_health_alerts_use_real_promtool(self):
        import shutil
        import yaml
        promtool = shutil.which('promtool')
        self.assertIsNotNone(promtool, 'promtool required; no silent skip')
        docs = list(yaml.safe_load_all((ROOT / 'infra/k8s/slo-evidence/archive/health-monitor.yaml').read_text()))
        groups = next(d['spec']['groups'] for d in docs if d['kind'] == 'PrometheusRule')
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            (base / 'health-rules.yaml').write_text(yaml.safe_dump({'groups': groups}))
            (base / 'health-test.yaml').write_text((ROOT / 'tests/fixtures/observation-health-alerts.test.yaml').read_text())
            result = subprocess.run([promtool, 'test', 'rules', 'health-test.yaml'], cwd=base, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rendered_code_is_exact_source_bound_immutable_and_ro_without_rebuilding_image(self):
        import hashlib
        module = self.module()
        image = 'docker.io/library/personal-server-slo-observation-archive@sha256:' + 'a' * 64
        docs = module.render(image, date(2026, 10, 26))['items']
        code = next(d for d in docs if d['kind'] == 'ConfigMap' and d.get('immutable'))
        source = (ROOT / 'infra/k8s/tools/slo-observation-health.py').read_bytes()
        sha = hashlib.sha256(source).hexdigest()
        self.assertEqual(code['data']['slo-observation-health.py'].encode(), source)
        self.assertEqual(code['metadata']['annotations']['personal-server/health-code-sha256'], sha)
        self.assertTrue(code['metadata']['name'].endswith(sha[:16]))
        pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']
        self.assertEqual(pod['metadata']['annotations']['personal-server/health-code-sha256'], sha)
        self.assertEqual(pod['spec']['containers'][0]['image'], image)
        self.assertTrue(all(m['readOnly'] for m in pod['spec']['containers'][0]['volumeMounts']))
        self.assertEqual(pod['spec']['volumes'][1]['configMap']['name'], code['metadata']['name'])
        self.assertEqual(pod['spec']['containers'][0]['args'][-2:], ['--required-from', '2026-10-26'])
        for invalid in ('image:latest', image[:-1], image.replace('archive', 'other')):
            with self.assertRaises(ValueError):
                module.render(invalid, date(2026, 10, 26))

    def test_archive_dashboard_does_not_convert_missing_data_to_success_or_claim_coverage(self):
        import yaml
        docs = list(yaml.safe_load_all((ROOT / 'infra/k8s/slo-evidence/archive/health-monitor.yaml').read_text()))
        dashboard = next(d for d in docs if d['kind'] == 'ConfigMap')
        config = json.loads(dashboard['data']['slo-observation-health.json'])
        self.assertEqual(config['timezone'], 'utc')
        self.assertEqual(len(config['panels']), 9)
        for panel in config['panels']:
            self.assertTrue(panel['targets'][0]['instant'])
            self.assertEqual(panel['options']['reduceOptions']['calcs'], ['last'])
            self.assertNotIn('or vector(0)', panel['targets'][0]['expr'])
            self.assertEqual(panel['fieldConfig']['defaults']['noValue'], '확인 필요')


if __name__ == '__main__':
    unittest.main()
