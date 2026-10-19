#!/usr/bin/env python3
"""Prepare an expiring alert rule; never contacts Kubernetes or Telegram."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import re
import uuid


def plan(now=None, duration=240, test_id=None):
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None or type(duration) is not int or not 120 <= duration <= 300:
        raise ValueError("invalid_test_window")
    test_id = uuid.uuid4().hex[:12] if test_id is None else test_id
    if not isinstance(test_id, str) or not re.fullmatch(r"[0-9a-f]{12}", test_id):
        raise ValueError("invalid_test_id")
    expires = now + timedelta(seconds=duration)
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": "host-disk-notification-test-" + test_id, "namespace": "monitoring",
                     "labels": {"release": "personal-server-monitoring"},
                     "annotations": {"test-expires-at": expires.astimezone(timezone.utc).isoformat()}},
        "spec": {"groups": [{"name": "host-disk-notification-test", "interval": "15s", "rules": [{
            "alert": "HostDiskNotificationTest",
            "expr": f"vector(1) and on () (vector(time()) < {int(expires.timestamp())})",
            "for": "0s", "labels": {"severity": "warning", "sre_telegram": "true", "validation_id": test_id},
            "annotations": {"summary": "호스트 디스크 알림 전달 시험", "description": "실제 운영 장애가 아닌 자동 만료 시험"},
        }]}]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=240, choices=range(120, 301), metavar="120..300")
    args = parser.parse_args()
    print(json.dumps(plan(duration=args.duration_seconds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
