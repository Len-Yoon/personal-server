from __future__ import annotations

from collections import Counter, defaultdict
from threading import RLock


_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _labels(**values: str) -> str:
    return ",".join(f'{key}="{value}"' for key, value in sorted(values.items()))


def _bucket_label(value: float) -> str:
    return f"{value:g}"


class HttpMetrics:
    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._durations: defaultdict[tuple[str, str], list[float]] = defaultdict(list)

    def record(self, *, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        with self._lock:
            self._requests[(method, route, str(status_code))] += 1
            self._durations[(method, route)].append(max(0.0, duration_seconds))

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            durations = {key: tuple(values) for key, values in self._durations.items()}
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
        for (method, route), values in sorted(durations.items()):
            base_labels = {"method": method, "route": route}
            for bucket in _DURATION_BUCKETS:
                labels = {**base_labels, "le": _bucket_label(bucket)}
                lines.append(
                    "portal_http_request_duration_seconds_bucket"
                    f"{{{_labels(**labels)}}} {sum(value <= bucket for value in values)}"
                )
            labels = {**base_labels, "le": "+Inf"}
            lines.append(
                "portal_http_request_duration_seconds_bucket"
                f"{{{_labels(**labels)}}} {len(values)}"
            )
            lines.append(
                "portal_http_request_duration_seconds_count"
                f"{{{_labels(**base_labels)}}} {len(values)}"
            )
            lines.append(
                "portal_http_request_duration_seconds_sum"
                f"{{{_labels(**base_labels)}}} {sum(values):.9g}"
            )
        return "\n".join(lines) + "\n"
