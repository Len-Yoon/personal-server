"""Actual-app PostgreSQL checks with external providers mocked at the boundary."""
import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

SERVICES = ("crawler-worker", "car-care-worker", "homeops-executor", "system-agent")


def check(value, label):
    if not value:
        raise AssertionError(label)


def configure(service, node):
    root = Path("/tmp/ha-smoke") / (service + "-" + node)
    root.mkdir(parents=True, exist_ok=True)
    source = Path(os.getenv("HA_APP_SOURCE", "/source"))
    sys.path.insert(0, str(source / "infra/k8s"))
    sys.path.insert(0, str(source / service))
    os.chdir(source / service)
    os.environ.update(HA_LOCAL_RUNTIME="1", APP_ENV="test", DATA_ROOT=str(root))
    expected = Path("/fixture/config") / (service + "-" + node + ".json")
    configured = Path(os.environ.get("HA_PG_CONFIG", expected))
    check(configured == expected, "configuration_node_binding")
    os.environ["HA_PG_CONFIG"] = str(configured)
    return root


def car_care(node, root):
    import ha_runtime as backend
    from app.services.hyundai import HyundaiClient
    from app.services.store import CarCareStore
    from app import main
    with backend.worker_scope("car-care-worker"):
        store = CarCareStore(root / "must-not-exist.sqlite3")
        store.initialize()
        check(store.get_maintenance("engine_oil") is not None, "legacy_maintenance_preserved")
        check(store.load_last_snapshot().unknown_warnings == frozenset({"washer_fluid"}), "legacy_snapshot_preserved")
        store.complete_maintenance("transmission_oil", 14000, date(2026, 3, 2))
        check(store.get_maintenance("transmission_oil").odometer_km == 14000, "maintenance_pg_update")
        store.record_tire_change("all_season_tires", 14000, date(2026, 3, 2))
        with backend.connect("car-care-worker") as db:
            highwater = db.execute("SELECT MAX(id) FROM tire_changes").fetchone()[0]
        check(highwater >= 3, "deleted_id_highwater_preserved")
        calls = []
        def command(transaction):
            calls.append(True)
            transaction.set_alert_state("smoke:" + node, "active")
            return "synthetic pg reply"
        update_id = 8000 + (1 if node == "A" else 2)
        first = store.process_command(update_id, command)
        second = store.process_command(update_id, command)
        check(first == second and len(calls) == 1, "command_exact_replay")
        client = HyundaiClient(token_store_path=root / "must-not-exist-token.json")
        check(bool(client._load_refresh_token()), "oauth_migration_shared")
        client._save_authorization_state("synthetic-state-" + node)
        second_client = HyundaiClient(token_store_path=root / "also-must-not-exist-token.json")
        second_client._consume_authorization_state("synthetic-state-" + node)
        try:
            client._consume_authorization_state("synthetic-state-" + node)
        except ValueError:
            pass
        else:
            raise AssertionError("oauth_state_one_time")
        telegram = Mock()
        telegram.send.side_effect = OSError("synthetic ambiguous provider")
        try:
            main._send_command_reply(telegram, "synthetic reply", update_id)
        except OSError:
            pass
        else:
            raise AssertionError("provider_fault_injected")
        check(main._send_command_reply(telegram, "synthetic reply", update_id), "provider_retry_suppressed")
        check(telegram.send.call_count == 1, "provider_called_once")
        check(not (root / "must-not-exist.sqlite3").exists(), "no_sqlite_fallback")
    return 10


def crawler(node, root):
    import ha_runtime as backend
    from app.services import news_archive
    from app.services.news_archive_notifications import notification_event
    from app.services.news_collection_status import NewsCollectionStatusStore
    from app.services.readiness import readiness_checks
    owned = backend.worker_owned("crawler-worker")
    before = backend.read_state("crawler-worker", "archive", {})
    with patch.object(news_archive, "collect_korean_news_from_sources") as fetch, patch.object(news_archive, "notify_new_investing_articles", return_value=1), patch.object(news_archive, "notify_market_news_digest", return_value=True):
        if not owned:
            result = news_archive.collect_korean_news("KR_WORLD", force_refresh=True)
            check(result["cache"]["hit"], "serving_peer_cached_read")
            fetch.assert_not_called()
            check(backend.read_state("crawler-worker", "archive", {}) == before, "serving_peer_no_repair")
            return 2
        now = datetime.now(timezone.utc)
        article = {"url": "https://fixture.invalid/smoke-" + node, "title": "Synthetic PG article",
                   "published_at_sort": now.isoformat()}
        fetch.return_value = [article]
        result = news_archive.collect_korean_news("KR_WORLD", force_refresh=True)
        check(any(item["url"] == article["url"] for item in result["articles"]), "collector_pg_write")
        fetch.assert_called_once()
    check(all(readiness_checks().values()), "pg_readiness")
    status = NewsCollectionStatusStore(root / "must-not-exist-status.json")
    check(status.snapshot()["custom_metadata"] == {"retained": True}, "status_unknown_fields_preserved")
    check(backend.read_state("crawler-worker", "archive", {})["custom_metadata"] == {"retained": True}, "archive_unknown_fields_preserved")
    now = datetime.now(timezone.utc)
    pending_article = {"url": "https://fixture.invalid/uncertain-" + node, "title": "Synthetic uncertain article"}
    event = notification_event("alert", [pending_article], now.isoformat())
    with backend.worker_scope("crawler-worker"):
        with news_archive._archive_transaction():
            archive = news_archive._load_archive()
            archive.setdefault("telegram_outbox", []).append(event)
            news_archive._save_archive(archive)
        with patch.object(news_archive, "notify_new_investing_articles", side_effect=OSError("synthetic ambiguous provider")) as send:
            try:
                news_archive._send_pending_event(event["event_id"], now)
            except OSError:
                pass
            else:
                raise AssertionError("provider_fault_injected")
            check(news_archive._send_pending_event(event["event_id"], now), "outbox_uncertain_not_replayed")
            check(send.call_count == 1, "outbox_provider_called_once")
    check(not (root / "must-not-exist-status.json").exists(), "no_json_local_fallback")
    return 8


def homeops(node, root):
    from fastapi.testclient import TestClient
    from app import main
    from app.services.idempotency import ClaimError, RestartStore, payload_hash
    store = RestartStore(root / "must-not-exist.sqlite3")
    check(store.claim("legacy-completed", payload_hash({"fixture": "completed"}))["fixture"], "completed_legacy_claim_preserved")
    try:
        store.claim("legacy-uncertain", payload_hash({"fixture": "uncertain"}))
    except ClaimError as error:
        check(error.detail == "restart_outcome_unknown", "legacy_uncertainty_preserved")
    else:
        raise AssertionError("legacy_uncertainty_not_replayed")
    headers = {"X-HomeOps-Executor-Secret": "synthetic-lab-shared-secret"}
    payload = {"incident_id": "smoke-restart-" + node, "approval_token": "synthetic-lab-approval",
               "action": "restart_container", "service": "system-agent", "target_node": node}
    with patch.dict(os.environ, {"HOMEOPS_EXECUTOR_SHARED_SECRET": headers["X-HomeOps-Executor-Secret"]}), patch.object(main.docker_ops, "allowed_services", return_value={"system-agent"}), patch.object(main.docker_ops, "restart_service", return_value={"status": "running"}) as effect, TestClient(main.app) as client:
        for target in (None, "B" if node == "A" else "A"):
            check(client.post("/v1/restarts", headers=headers, json=dict(payload, target_node=target)).status_code == 403, "explicit_target_required")
        check(effect.call_count == 0, "target_rejection_no_effect")
        first = client.post("/v1/restarts", headers=headers, json=payload)
        second = client.post("/v1/restarts", headers=headers, json=payload)
        check(first.status_code == second.status_code == 200 and first.json() == second.json(), "restart_complete_replay")
        check(effect.call_count == 1, "restart_effect_once")
        uncertain = dict(payload, incident_id="smoke-uncertain-" + node)
        effect.reset_mock()
        effect.side_effect = OSError("synthetic ambiguous actuator")
        check(client.post("/v1/restarts", headers=headers, json=uncertain).status_code == 503, "actuator_unknown_response")
        check(client.post("/v1/restarts", headers=headers, json=uncertain).status_code == 409, "actuator_unknown_no_retry")
        check(effect.call_count == 1, "actuator_called_once")
    check(not (root / "must-not-exist.sqlite3").exists(), "no_sqlite_fallback")
    return 10


def system_agent(node, root):
    from fastapi.testclient import TestClient
    source = root / "host.json"
    payload = {"node_id": node, "captured_at": datetime.now(timezone.utc).isoformat(),
               "cpu_percent": 12.5, "memory_percent": 25, "disk_percent": 30, "uptime_seconds": 100,
               "host_disk": {"source": "windows-cim", "volume": "C:", "total_bytes": 1000, "available_bytes": 700}}
    source.write_text(json.dumps(payload))
    os.environ["HOST_METRICS_PATH"] = str(source)
    from app import main
    with TestClient(main.app) as client:
        check(client.get("/health").json()["node_id"] == node, "agent_node_identity")
        check(client.get("/metrics").status_code == 403, "agent_target_required")
        headers = {"X-HA-Target-Node": node}
        response = client.get("/metrics", headers=headers)
        check(response.status_code == 200 and response.json()["node_id"] == node, "agent_scoped_observation")
        check(response.headers["Cache-Control"] == "no-store", "agent_cache_disposable")
        payload["node_id"] = "B" if node == "A" else "A"
        source.write_text(json.dumps(payload))
        response = client.get("/metrics", headers=headers)
        check(response.json()["host"]["cpu_percent"] is None, "copied_host_not_impersonated")
        output = client.get("/internal/metrics", headers=headers).text
        check("capacity_bytes" not in output and "collection_ok" in output, "copied_prometheus_not_impersonated")
    return 6


def blocked(service, node, root):
    import ha_runtime as backend
    if service == "car-care-worker":
        from app.services.store import CarCareStore
        try:
            CarCareStore(root / "must-not-exist.sqlite3").initialize()
        except backend.DatabaseUnavailable as error:
            check(str(error) == "not_worker_owner", "nonowner_store_denied")
        else:
            raise AssertionError("nonowner_store_denied")
    else:
        from app import main
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"HOMEOPS_EXECUTOR_SHARED_SECRET": "synthetic-lab-shared-secret"}), patch.object(main.docker_ops, "allowed_services", return_value={"system-agent"}), patch.object(main.docker_ops, "restart_service") as effect, TestClient(main.app) as client:
            response = client.post("/v1/restarts", headers={"X-HomeOps-Executor-Secret": "synthetic-lab-shared-secret"}, json={"incident_id": "nonowner-" + node, "approval_token": "synthetic-lab-approval", "action": "restart_container", "service": "system-agent", "target_node": node})
            check(response.status_code == 503, "nonowner_executor_denied")
            check(effect.call_count == 0, "nonowner_no_effect")
    return 2


def run(service, node):
    root = configure(service, node)
    import ha_runtime as backend
    if service in {"car-care-worker", "homeops-executor"}:
        try:
            backend.require_worker(service)
        except backend.DatabaseUnavailable as error:
            if str(error) != "not_worker_owner":
                raise
            return {"pass": True, "service": service, "node": node, "owner_blocked": True,
                    "checks": blocked(service, node, root)}
    checks = {"car-care-worker": car_care, "crawler-worker": crawler,
              "homeops-executor": homeops, "system-agent": system_agent}[service](node, root)
    return {"pass": True, "service": service, "node": node, "checks": checks}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--node", choices=("A", "B"), required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.service, args.node), sort_keys=True))
    except Exception as error:
        result = {"pass": False, "service": args.service, "node": args.node, "error_class": type(error).__name__}
        if isinstance(error, AssertionError):
            result["check"] = str(error)
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
