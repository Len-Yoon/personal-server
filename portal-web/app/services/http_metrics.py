from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from threading import RLock


_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(**values: str) -> str:
    return ",".join(f'{key}="{value}"' for key, value in sorted(values.items()))


def _bucket_label(value: float) -> str:
    return f"{value:g}"


@dataclass
class _DurationStats:
    bucket_counts: list[int] = field(
        default_factory=lambda: [0] * len(_DURATION_BUCKETS)
    )
    count: int = 0
    total: float = 0.0


class HttpMetrics:
    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._durations: dict[tuple[str, str], _DurationStats] = {}

    def record(self, *, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        with self._lock:
            self._requests[(method, route, str(status_code))] += 1
            duration = max(0.0, duration_seconds)
            key = (method, route)
            stats = self._durations.get(key)
            if stats is None:
                stats = _DurationStats()
                self._durations[key] = stats
            for index, bucket in enumerate(_DURATION_BUCKETS):
                if duration <= bucket:
                    stats.bucket_counts[index] += 1
            stats.count += 1
            stats.total += duration

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            durations = {
                key: (tuple(stats.bucket_counts), stats.count, stats.total)
                for key, stats in self._durations.items()
            }
        lines = [
            "# HELP portal_http_requests_total Total completed HTTP requests.",
            "# TYPE portal_http_requests_total counter",
        ]
        for (method, route, status_code), count in sorted(requests.items()):
            lines.append(
                f"portal_http_requests_total{{{_labels(method=method, route=route, status_code=status_code)}}} {count}"
            )

        lines.extend([
            "# HELP portal_http_request_duration_seconds Completed HTTP request duration in seconds.",
            "# TYPE portal_http_request_duration_seconds histogram",
        ])
        for (method, route), (bucket_counts, count, total) in sorted(durations.items()):
            base_labels = {"method": method, "route": route}
            for bucket, bucket_count in zip(_DURATION_BUCKETS, bucket_counts):
                labels = {**base_labels, "le": _bucket_label(bucket)}
                lines.append(
                    "portal_http_request_duration_seconds_bucket"
                    f"{{{_labels(**labels)}}} {bucket_count}"
                )
            labels = {**base_labels, "le": "+Inf"}
            lines.append(
                "portal_http_request_duration_seconds_bucket"
                f"{{{_labels(**labels)}}} {count}"
            )
            lines.append(
                "portal_http_request_duration_seconds_count"
                f"{{{_labels(**base_labels)}}} {count}"
            )
            lines.append(
                "portal_http_request_duration_seconds_sum"
                f"{{{_labels(**base_labels)}}} {total:.9g}"
            )
        return "\n".join(lines) + "\n"
