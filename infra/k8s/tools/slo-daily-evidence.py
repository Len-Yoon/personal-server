"""Collect, validate, and retain bounded daily SLO evidence records."""

from datetime import date, datetime, timezone
import json
import math
import os
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen as standard_urlopen
from zoneinfo import ZoneInfo


_FIELDS = {
    "date",
    "collected_at",
    "overall",
    "public_health",
    "portal_ready",
    "portal_http",
    "crawler_freshness",
    "missing",
}
_MISSING_SOURCES = {"public_health", "portal_ready", "portal_http", "crawler_freshness"}
_STATUS_VALUES = {"ok", "failed", "unobservable"}
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_HTTP_FIELDS = {"requests", "server_errors", "server_error_ratio", "p95_seconds"}

EVIDENCE_NAMESPACE = "monitoring"
EVIDENCE_CONFIGMAP = "slo-daily-evidence"
PROMETHEUS_URL = "http://prometheus-operated.monitoring.svc:9090/api/v1/query"
PUBLIC_HEALTH_URL = "https://len.pe.kr/health"
REQUEST_TIMEOUT_SECONDS = 10

_QUERY_PORTAL_HTTP_REQUESTS = "sum(increase(portal_http_requests_total[24h]))"
_QUERY_PORTAL_HTTP_ERRORS = (
    'sum(increase(portal_http_requests_total{status_code=~"5.."}[24h]))'
)
_QUERY_PORTAL_HTTP_P95 = (
    "histogram_quantile(0.95, sum by (le) "
    "(increase(portal_http_request_duration_seconds_bucket[24h])))"
)
_QUERY_PORTAL_READY = (
    'min(min_over_time(kube_deployment_status_replicas_available{namespace="personal-server",'
    'deployment="portal-web"}[24h]))'
)
_QUERY_CRAWLER_FRESHNESS = (
    "min_over_time(((crawler_news_collection_initialized == bool 1) * "
    "(time() - crawler_news_collection_last_success_timestamp_seconds <= bool 900) * "
    "(crawler_news_collection_consecutive_failures < bool 3))[24h:])"
)
_QUERY_SOURCES = {
    _QUERY_PORTAL_HTTP_REQUESTS: "portal_http",
    _QUERY_PORTAL_HTTP_ERRORS: "portal_http",
    _QUERY_PORTAL_HTTP_P95: "portal_http",
    _QUERY_PORTAL_READY: "portal_ready",
    _QUERY_CRAWLER_FRESHNESS: "crawler_freshness",
}


def _number(value, field, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    if integer:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        elif not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
    return value


def _validate_portal_http(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _HTTP_FIELDS:
        raise ValueError("portal_http must contain exactly the four defined metrics")

    requests = _number(value["requests"], "requests")
    server_errors = _number(value["server_errors"], "server_errors")
    if server_errors > requests:
        raise ValueError("server_errors cannot exceed requests")
    ratio = value["server_error_ratio"]
    p95 = value["p95_seconds"]
    if requests == 0:
        if ratio is not None or p95 is not None:
            raise ValueError("zero requests require null ratio and p95")
    else:
        ratio = _number(ratio, "server_error_ratio")
        p95 = _number(p95, "p95_seconds")
        if ratio > 1:
            raise ValueError("server_error_ratio must not exceed 1")
    return {
        "requests": requests,
        "server_errors": server_errors,
        "server_error_ratio": ratio,
        "p95_seconds": p95,
    }


def validate_record(record: dict) -> dict:
    """Validate a daily evidence record and return its fixed-field form."""
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    missing_fields = _FIELDS - record.keys()
    if missing_fields:
        raise ValueError("record is missing required fields")

    record_date = record["date"]
    if not isinstance(record_date, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(record_date)
    except ValueError as exc:
        raise ValueError("date must be a valid YYYY-MM-DD date") from exc
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record_date):
        raise ValueError("date must be YYYY-MM-DD")

    collected_at = record["collected_at"]
    if not isinstance(collected_at, str) or not _UTC_TIMESTAMP.fullmatch(collected_at):
        raise ValueError("collected_at must be an ISO 8601 UTC timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("collected_at must be a valid ISO 8601 UTC timestamp") from exc
    if parsed_timestamp.utcoffset() != timezone.utc.utcoffset(parsed_timestamp):
        raise ValueError("collected_at must use UTC")

    overall = record["overall"]
    if overall not in {"ok", "unobservable"}:
        raise ValueError("overall must be ok or unobservable")
    for field in ("public_health", "portal_ready", "crawler_freshness"):
        if record[field] not in _STATUS_VALUES:
            raise ValueError(f"{field} has an invalid status")

    missing = record["missing"]
    if not isinstance(missing, list) or any(
        not isinstance(source, str) or source not in _MISSING_SOURCES for source in missing
    ):
        raise ValueError("missing must be a list of known source names")
    if len(set(missing)) != len(missing):
        raise ValueError("missing sources must be unique")
    missing_set = set(missing)
    if (overall == "ok") != (not missing_set):
        raise ValueError("overall must reflect whether any sources are missing")

    portal_http = _validate_portal_http(record["portal_http"])
    if (portal_http is None) != ("portal_http" in missing_set):
        raise ValueError("portal_http nullability must reflect its missing status")
    statuses = {
        "public_health": record["public_health"],
        "portal_ready": record["portal_ready"],
        "crawler_freshness": record["crawler_freshness"],
    }
    if any((status == "unobservable") != (field in missing_set) for field, status in statuses.items()):
        raise ValueError("unobservable statuses must reflect missing sources")

    return {
        "date": record_date,
        "collected_at": collected_at,
        "overall": overall,
        "public_health": record["public_health"],
        "portal_ready": record["portal_ready"],
        "portal_http": portal_http,
        "crawler_freshness": record["crawler_freshness"],
        "missing": list(missing),
    }


def merge_records(existing: list, record: dict) -> list:
    """Replace the record for this date and retain the newest 30 validated rows."""
    if not isinstance(existing, list):
        raise ValueError("existing records must be a list")
    validated = {}
    for item in existing:
        checked = validate_record(item)
        validated[checked["date"]] = checked
    replacement = validate_record(record)
    validated[replacement["date"]] = replacement
    merged = list(validated.values())
    merged.sort(key=lambda item: item["date"], reverse=True)
    return merged[:30]


def metric_source(query: str) -> str:
    """Return the fixed evidence source represented by a collector query."""
    return _QUERY_SOURCES[query]


def _prometheus_scalar(prometheus_url, query, *, urlopen):
    request = Request(f"{prometheus_url}?{urlencode({'query': query})}")
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        if getattr(response, "status", 200) != 200:
            raise ValueError("Prometheus query did not return HTTP 200")
        body = response.read()
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Prometheus response has an invalid shape")
    result = payload["data"].get("result")
    # A labeled counter series does not exist until that status is first observed.
    # This fallback is restricted to errors; the caller validates totals first.
    if payload.get("status") == "success" and result == [] and query == _QUERY_PORTAL_HTTP_ERRORS:
        return 0
    if payload.get("status") != "success" or not isinstance(result, list) or len(result) != 1:
        raise ValueError("Prometheus query returned no single scalar")
    value = result[0].get("value") if isinstance(result[0], dict) else None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Prometheus scalar response is invalid")
    return _number(float(value[1]), "Prometheus value")


def _public_health_status(public_health_url, *, urlopen):
    request = Request(public_health_url, method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return "ok" if getattr(response, "status", 200) == 200 else "failed"
    except HTTPError:
        return "failed"
    except (URLError, OSError, ValueError):
        return "unobservable"


def _collector_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def collect_record(
    prometheus_url=PROMETHEUS_URL,
    public_health_url=PUBLIC_HEALTH_URL,
    *,
    now=None,
    prometheus_query=None,
    public_health_probe=None,
    urlopen=standard_urlopen,
):
    """Collect one KST-day record, failing closed for unavailable sources."""
    collected_at = _collector_now(now)
    query = prometheus_query or (
        lambda expression: _prometheus_scalar(prometheus_url, expression, urlopen=urlopen)
    )
    public_probe = public_health_probe or (
        lambda: _public_health_status(public_health_url, urlopen=urlopen)
    )
    missing = []

    try:
        public_result = public_probe()
        if isinstance(public_result, int):
            public_health = "ok" if public_result == 200 else "failed"
        else:
            public_health = public_result
        if public_health not in _STATUS_VALUES:
            public_health = "unobservable"
    except (URLError, OSError, ValueError):
        public_health = "unobservable"
    if public_health == "unobservable":
        missing.append("public_health")

    try:
        requests = _number(query(_QUERY_PORTAL_HTTP_REQUESTS), "requests")
        server_errors = _number(query(_QUERY_PORTAL_HTTP_ERRORS), "server_errors")
        if server_errors > requests:
            raise ValueError("server_errors cannot exceed requests")
        # An empty observation window has no latency quantile (Prometheus NaN).
        p95 = None if requests == 0 else _number(query(_QUERY_PORTAL_HTTP_P95), "p95_seconds")
        portal_http = {
            "requests": requests,
            "server_errors": server_errors,
            "server_error_ratio": None if requests == 0 else server_errors / requests,
            "p95_seconds": None if requests == 0 else p95,
        }
    except (AttributeError, URLError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        portal_http = None
        missing.append("portal_http")

    def status_for(query_text, source):
        try:
            return "ok" if _number(query(query_text), source) >= 1 else "failed"
        except (AttributeError, URLError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            missing.append(source)
            return "unobservable"

    portal_ready = status_for(_QUERY_PORTAL_READY, "portal_ready")
    crawler_freshness = status_for(_QUERY_CRAWLER_FRESHNESS, "crawler_freshness")
    record = {
        "date": collected_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
        "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
        "overall": "unobservable" if missing else "ok",
        "public_health": public_health,
        "portal_ready": portal_ready,
        "portal_http": portal_http,
        "crawler_freshness": crawler_freshness,
        "missing": missing,
    }
    return validate_record(record)


def update_configmap(record, *, configmap=EVIDENCE_CONFIGMAP, namespace=EVIDENCE_NAMESPACE, subprocess_run=subprocess.run):
    """Merge a validated record and patch only the ConfigMap records.json key."""
    checked = validate_record(record)
    get_command = ["kubectl", "get", "configmap", configmap, "--namespace", namespace, "-o", "json"]
    result = subprocess_run(get_command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Unable to read daily SLO evidence ConfigMap")
    try:
        configmap_data = json.loads(result.stdout)
        raw_records = configmap_data.get("data", {}).get("records.json", "[]")
        existing = json.loads(raw_records)
        merged = merge_records(existing, checked)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Daily SLO evidence ConfigMap has invalid records") from exc
    patch = json.dumps({"data": {"records.json": json.dumps(merged, separators=(",", ":"))}}, separators=(",", ":"))
    patch_command = ["kubectl", "patch", "configmap", configmap, "--namespace", namespace, "--type=merge", "-p", patch]
    result = subprocess_run(patch_command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Unable to patch daily SLO evidence ConfigMap")


def main():
    """Run collection and update once; unobservable Prometheus evidence fails the Job."""
    record = collect_record(prometheus_url=os.environ.get("PROMETHEUS_URL", PROMETHEUS_URL))
    update_configmap(record, configmap=os.environ.get("EVIDENCE_CONFIGMAP", EVIDENCE_CONFIGMAP))
    if record["overall"] == "unobservable":
        raise SystemExit("Daily SLO evidence contains unobservable sources")


if __name__ == "__main__":
    main()
