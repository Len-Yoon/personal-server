#!/usr/bin/env python3
"""Summarize a bounded Portal observation grid, never exact uptime or SLO budget.

Input contains four Prometheus query_range API responses: available, desired,
available_timestamp and desired_timestamp. Timestamp queries must use
timestamp(the_same_metric_selector), so lookback-reused samples can be detected.
This offline tool does not contact Prometheus or change production state.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys


INPUT_LIMIT = 32 * 1024 * 1024
MAX_PERIOD = 7 * 24 * 60 * 60
FRESHNESS_SECONDS = 60
QUERIES = ('available', 'desired', 'available_timestamp', 'desired_timestamp')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError('invalid_number')
    try:
        return float(value)
    except (ValueError, OverflowError):
        raise ValueError('invalid_number') from None


def _matrix(response, start, end, step):
    if not isinstance(response, dict) or response.get('status') != 'success':
        raise ValueError('invalid_response')
    if response.get('warnings') or response.get('infos'):
        raise ValueError('partial_response')
    data = response.get('data')
    if not isinstance(data, dict) or data.get('resultType') != 'matrix':
        raise ValueError('invalid_matrix')
    rows = data.get('result')
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('ambiguous_series')
    if not rows:
        return {}
    if not isinstance(rows[0], dict) or not isinstance(rows[0].get('values'), list):
        raise ValueError('invalid_series')
    labels = rows[0].get('metric')
    if not isinstance(labels, dict) or labels.get('namespace') != 'personal-server' or labels.get('deployment') != 'portal-web':
        raise ValueError('invalid_selector')
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
        raise ValueError('invalid_labels')
    values = rows[0]['values']
    if len(values) > (end - start) // step + 1:
        raise ValueError('too_many_points')
    points = {}
    last = -1
    for pair in values:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('invalid_point')
        at = _number(pair[0])
        if not math.isfinite(at) or at < start or at > end:
            raise ValueError('invalid_timestamp')
        offset = (at - start) / step
        index = round(offset)
        if abs(offset - index) > 1e-7 or index <= last:
            raise ValueError('invalid_grid')
        last = index
        points[index] = _number(pair[1])
    return points


def summarize_samples(payload):
    if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('invalid_schema')
    start, end, step = (payload.get(key) for key in ('start', 'end', 'step_seconds'))
    if any(type(value) is not int for value in (start, end, step)):
        raise ValueError('invalid_period')
    if start < 0 or not 15 <= step <= 300 or not 0 < end - start <= MAX_PERIOD or (end - start) % step:
        raise ValueError('invalid_period')
    # datetime also rejects epochs outside the supported calendar.
    period = {'start': datetime.fromtimestamp(start, timezone.utc).isoformat(),
              'end': datetime.fromtimestamp(end, timezone.utc).isoformat(),
              'step_seconds': step, 'freshness_seconds': FRESHNESS_SECONDS}
    raw = payload.get('queries')
    if not isinstance(raw, dict) or set(raw) != set(QUERIES):
        raise ValueError('missing_queries')
    matrices = {key: _matrix(raw[key], start, end, step) for key in QUERIES}
    labels = [{key: value for key, value in raw[name]['data']['result'][0]['metric'].items()
               if key != '__name__'} for name in QUERIES if raw[name]['data']['result']]
    if labels and any(label != labels[0] for label in labels):
        raise ValueError('inconsistent_sources')
    count = (end - start) // step + 1
    good = bad = stop_candidates = 0
    unknown = {reason: 0 for reason in ('missing', 'nonfinite', 'stale', 'future_timestamp')}
    for index in range(count):
        if any(index not in matrices[key] for key in QUERIES):
            unknown['missing'] += 1
            continue
        available, desired, available_at, desired_at = (matrices[key][index] for key in QUERIES)
        if any(not math.isfinite(value) for value in (available, desired, available_at, desired_at)):
            unknown['nonfinite'] += 1
            continue
        if any(value < 0 or not value.is_integer() for value in (available, desired)):
            raise ValueError('invalid_replica_count')
        at = start + index * step
        ages = (at - available_at, at - desired_at)
        if min(ages) < 0:
            unknown['future_timestamp'] += 1
            continue
        if max(ages) > FRESHNESS_SECONDS:
            unknown['stale'] += 1
            continue
        good += available >= 1
        bad += available < 1
        stop_candidates += desired == 0 and available == 0
    observed = good + bad
    return {
        'schema_version': 1,
        'period': period,
        'samples': {'expected': count, 'observed': observed, 'good': good, 'bad': bad,
                    'unknown': count - observed, 'unknown_reasons': unknown,
                    'desired_zero_unavailable_candidates': stop_candidates},
        'coverage_percent': 100 * observed / count,
        'observed_sample_success_percent': 100 * good / observed if observed else None,
        'complete_grid_success_percent': 100 * good / count if observed == count else None,
        'semantics': 'evaluation_samples_not_exact_duration_or_user_requests',
        'planned_stop_policy': 'candidates_are_not_exempt_from_raw_failure',
        'slo_budget': {'status': 'not_calculable', 'reason': 'sample_grid_is_not_exact_duration'},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    args = parser.parse_args(argv)
    try:
        with Path(args.input).open('rb') as stream:
            raw = stream.read(INPUT_LIMIT + 1)
        if len(raw) > INPUT_LIMIT:
            raise ValueError('input_too_large')
        result = summarize_samples(json.loads(raw))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        # No exception text, labels, source URLs, paths or input values in errors.
        print('slo_sample_summary=FAIL reason=invalid_input', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
