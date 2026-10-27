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


if __name__ == '__main__':
    unittest.main()
