#!/usr/bin/env python3
"""Read-only KST daily evidence summary, not a time-based SLO calculator."""

import argparse
from datetime import date, datetime, timedelta
import importlib.util
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

_SPEC = importlib.util.spec_from_file_location(
    'daily_evidence_contract', Path(__file__).with_name('slo-daily-evidence.py')
)
_CONTRACT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CONTRACT)
_SOURCES = ('public_health', 'portal_ready', 'crawler_freshness')
_INPUT_LIMIT = 1024 * 1024


def summarize_records(records, end_date, days=30):
    """Return fixed-field observation counts without modifying source evidence."""
    if type(end_date) is not date or type(days) is not int or not 1 <= days <= 30:
        raise ValueError('invalid_period')
    if not isinstance(records, list) or len(records) > 30:
        raise ValueError('invalid_records')
    try:
        start = end_date - timedelta(days=days - 1)
    except OverflowError as exc:
        raise ValueError('invalid_period') from exc
    selected = {}
    seen = set()
    for raw in records:
        try:
            item = _CONTRACT.validate_record(raw)
            day = date.fromisoformat(item['date'])
            timestamp = datetime.fromisoformat(item['collected_at'].replace('Z', '+00:00'))
            if timestamp.astimezone(ZoneInfo('Asia/Seoul')).date() != day:
                raise ValueError('inconsistent_record_date')
            http = item['portal_http']
            if http is not None and http['requests'] > 0 and not math.isclose(
                http['server_error_ratio'], http['server_errors'] / http['requests'],
                rel_tol=1e-9, abs_tol=1e-12,
            ):
                raise ValueError('inconsistent_error_ratio')
        except (TypeError, OverflowError, KeyError, AttributeError, ValueError) as exc:
            raise ValueError('invalid_record') from exc
        if day in seen or day > end_date:
            raise ValueError('duplicate_or_future_record')
        seen.add(day)
        if day >= start:
            selected[day] = item

    missing_dates = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range(days) if start + timedelta(days=offset) not in selected
    ]
    sources = {}
    for source in _SOURCES:
        counts = {
            status + '_days': sum(item[source] == status for item in selected.values())
            for status in ('ok', 'failed', 'unobservable')
        }
        observed = counts['ok_days'] + counts['failed_days']
        sources[source] = {
            **counts, 'missing_days': len(missing_dates),
            'observed_ok_percent': 100 * counts['ok_days'] / observed if observed else None,
        }
    http_records = [item['portal_http'] for item in selected.values() if item['portal_http'] is not None]
    try:
        requests = math.fsum(item['requests'] for item in http_records)
        errors = math.fsum(item['server_errors'] for item in http_records)
    except OverflowError as exc:
        raise ValueError('invalid_aggregate') from exc
    if not math.isfinite(requests) or not math.isfinite(errors):
        raise ValueError('invalid_aggregate')
    unobservable_days = sum(item['overall'] == 'unobservable' for item in selected.values())
    return {
        'schema_version': 1,
        'criteria': {
            'record_counts': {version: sum(item.get('criteria_version', 'legacy-freshness-900') == version for item in selected.values())
                              for version in ('legacy-freshness-900', 'freshness-1800-v1')},
            'mixed': len({item.get('criteria_version', 'legacy-freshness-900') for item in selected.values()}) > 1,
        },
        'period': {'start_date': start.isoformat(), 'end_date': end_date.isoformat(),
                   'days': days, 'timezone': 'Asia/Seoul'},
        'present_days': len(selected), 'missing_dates': missing_dates,
        'coverage_percent': 100 * len(selected) / days,
        'completeness': 'complete' if not missing_dates and not unobservable_days else 'incomplete',
        'collection': {'ok_days': len(selected) - unobservable_days,
                       'unobservable_days': unobservable_days},
        'sources': sources,
        'http': {
            'observed_days': len(http_records),
            'unobservable_days': len(selected) - len(http_records),
            'missing_days': len(missing_dates),
            'zero_traffic_days': sum(item['requests'] == 0 for item in http_records),
            'window_requests_sum': requests, 'window_server_errors_sum': errors,
            'window_weighted_error_ratio': errors / requests if requests else None,
            'semantics': 'sum_of_daily_rolling_windows_not_exact_period',
        },
        'slo_budget': {'status': 'not_calculable', 'reason': 'daily_evidence_is_not_time_series'},
    }


def render_daily_status(summary):
    """Describe the saved record for a single requested day."""
    if summary['period']['days'] != 1:
        raise ValueError('invalid_daily_period')
    lines = [f"일일 점검 · {summary['period']['end_date']} (한국시간)"]
    if not summary['present_days']:
        return '\n'.join([*lines, '수집 기록 — 없음', '점검 항목 — 확인 필요'])

    labels = {
        'public_health': '공개 Portal 접속',
        'portal_ready': 'Portal 준비 상태 (직전 24시간)',
        'crawler_freshness': '뉴스 수집 최신성 (직전 24시간)',
    }
    source_status = {
        name: '이상' if source['failed_days'] else '확인 필요' if source['unobservable_days'] else '정상'
        for name, source in summary['sources'].items()
    }
    partial = bool(summary['collection']['unobservable_days'])
    outcome = '이상 감지' if '이상' in source_status.values() else '정상'
    if partial:
        outcome = '이상 감지 · 일부 확인 필요' if outcome == '이상 감지' else '확인 필요'
    lines.extend([
        '수집 기록 — ' + ('부분 수집' if partial else '수집 완료'),
        '점검 항목 — ' + outcome,
        *(f"- {labels[name]} — {source_status[name]}" for name in _SOURCES),
    ])
    if summary['http']['unobservable_days']:
        lines.append('- Portal HTTP 지표 — 확인 필요')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--end-date', required=True)
    presentation = parser.add_mutually_exclusive_group()
    presentation.add_argument('--days', type=int, default=30)
    presentation.add_argument('--daily-status', action='store_true', help='요청한 날짜의 수집과 점검 항목 상태 표시')
    args = parser.parse_args(argv)
    try:
        end_date = date.fromisoformat(args.end_date)
        if args.end_date != end_date.isoformat():
            raise ValueError('invalid_date')
        with Path(args.input).open('rb') as stream:
            raw = stream.read(_INPUT_LIMIT + 1)
        if len(raw) > _INPUT_LIMIT:
            raise ValueError('input_too_large')
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = json.loads(payload['data']['records.json'])
        output = summarize_records(payload, end_date, 1 if args.daily_status else args.days)
        print(render_daily_status(output) if args.daily_status
              else json.dumps(output, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (OSError, TypeError, ValueError, KeyError, AttributeError, OverflowError):
        print('slo_evidence_summary=FAIL reason=invalid_input', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
