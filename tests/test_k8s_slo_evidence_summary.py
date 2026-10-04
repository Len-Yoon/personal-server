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


if __name__ == '__main__':
    unittest.main()
