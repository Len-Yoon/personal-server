"""Direct authenticated PG observations, bounded by an unchanged role guard."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import threading
import uuid

from .config import DatabaseUnavailable


# The image copies the canonical reviewed contract here. Source-tree execution
# resolves that same canonical file; there is no second hand-maintained copy.
_path = Path(__file__).with_name("contract.py")
if not _path.exists():
    _path = Path(__file__).parents[1] / "tools/n100_ha_contract.py"
_spec = importlib.util.spec_from_file_location("ha_runtime.contract", _path)
contract = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = contract
_spec.loader.exec_module(contract)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def raw_connection(config, endpoint):
    import psycopg
    try:
        connection = psycopg.connect(**config[endpoint], autocommit=True,
                                     connect_timeout=2, application_name="ha_application")
        connection.execute("SET statement_timeout = '1500ms'")
        connection.execute("SET lock_timeout = '1500ms'")
        connection.execute("SET TIME ZONE 'UTC'")
        if connection.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone() != (True,):
            connection.close()
            raise DatabaseUnavailable("tls_connection_required")
        return connection
    except DatabaseUnavailable:
        raise
    except psycopg.Error:
        raise DatabaseUnavailable("authenticated_database_unavailable") from None


def observe(connection, config):
    try:
        value = connection.execute("SELECT ha.observe()").fetchone()[0]
    except Exception:
        raise DatabaseUnavailable("database_observation_unavailable") from None
    if (value["cluster"] != config["cluster"] or value["system_id"] != config["system_id"]
            or value["identity_service"] != config["service"]
            or value["identity_node"] != config["node"]):
        raise DatabaseUnavailable("observed_identity_mismatch")
    if not value["recovery"] and value["actual_timeline"] != value["timeline"]:
        raise DatabaseUnavailable("actual_timeline_mismatch")
    return value


def authority(value):
    now = time.monotonic()
    if not value["valid"] or not 0 < value["remaining"]:
        raise DatabaseUnavailable("role_authority_expired")
    return contract.Authority(
        cluster=value["cluster"], epoch=value["epoch"], revision=value["revision"],
        primary=value["primary"], partner=value["partner"], timeline=value["timeline"],
        serving=frozenset(value["serving"]), worker=value["worker"], mode=value["mode"],
        writes_open=value["writes_open"], issued_at=now,
        deadline=now + min(5.0, value["remaining"]),
    )


def _generation(value):
    return tuple(value[key] if key != "serving" else tuple(value[key]) for key in (
        "cluster", "epoch", "revision", "primary", "partner", "timeline", "serving", "worker", "mode", "writes_open",
    ))


def bounded_commit(connection):
    """Cancel synchronous-replication waits, which statement_timeout excludes."""
    def cancel():
        try:
            connection.cancel_safe(timeout=1)
        except Exception:
            # The caller still requires fresh durability proof; never acknowledge
            # based on cancellation success or a local committed transaction.
            pass
    timer = threading.Timer(1.5, cancel)
    timer.daemon = True
    timer.start()
    try:
        return connection.execute("COMMIT")
    finally:
        timer.cancel()
        timer.join()


def protect(connection, config, schema, binding, original):
    """Commit a fresh barrier after the original transaction/ledger read.

    The immutable receipt is in the same transaction as the application writes.
    A new barrier follows commit and covers its WAL even on a replay-only path.
    No local row or successful commit return is itself a successful proof.
    """
    import psycopg
    from psycopg import sql
    peer = None
    try:
        before = observe(connection, config)
        if _generation(before) != _generation(original) or before["node"] != before["primary"]:
            raise DatabaseUnavailable("role_changed_before_proof")
        auth = authority(before)
        contract.require_serving_owner(auth, service=config["service"], node=config["node"],
                                       target_node=config["node"] if config["service"] in {"system-agent", "homeops-executor"} else None,
                                       now=time.monotonic())
        attempt = str(uuid.uuid4())
        start = time.monotonic()
        notices = []
        notice_handler = lambda notice: notices.append(notice.severity_nonlocalized)
        connection.add_notice_handler(notice_handler)
        try:
            connection.execute("BEGIN")
            connection.execute("SET LOCAL synchronous_commit=on")
            connection.execute(sql.SQL("INSERT INTO {}.ha_barriers(attempt,receipt,epoch) VALUES(%s,%s,%s)").format(sql.Identifier(schema)),
                               (attempt, binding.request_id, before["epoch"]))
            bounded_commit(connection)
            # A post-commit insert LSN can include unrelated WAL not yet flushed
            # (e.g. a hint/full-page record). Force a following synchronous
            # commit to cover that exact captured watermark, without chasing a
            # moving current-WAL target or accepting a pre-commit flush LSN.
            watermark = connection.execute("SELECT pg_current_wal_insert_lsn()::text").fetchone()[0]
            connection.execute("BEGIN")
            connection.execute("SET LOCAL synchronous_commit=on")
            connection.execute(sql.SQL("INSERT INTO {}.ha_barriers(attempt,receipt,epoch) VALUES(%s,%s,%s)").format(sql.Identifier(schema)),
                               (attempt + ":flush", binding.request_id, before["epoch"]))
            bounded_commit(connection)
        finally:
            connection.remove_notice_handler(notice_handler)
        if "WARNING" in notices or "ERROR" in notices:
            raise DatabaseUnavailable("uncertain_barrier_commit")
        # pg_current_wal_insert_lsn is captured AFTER the barrier commit, never a
        # pre-commit/current flush value that could omit the request commit record.
        peer = raw_connection(config, "partner")
        rejection = "peer_receipt_not_replayed"
        receipt_query = sql.SQL("SELECT payload_hash,result_hash FROM {}.ha_receipts WHERE receipt=%s").format(sql.Identifier(schema))
        barrier_query = sql.SQL("SELECT receipt,epoch FROM {}.ha_barriers WHERE attempt=%s").format(sql.Identifier(schema))
        while time.monotonic() - start < 1.8:
            replica = observe(peer, config)
            primary = observe(connection, config)
            row_a = connection.execute(receipt_query, (binding.request_id,)).fetchone()
            row_b = peer.execute(receipt_query, (binding.request_id,)).fetchone()
            barrier_b = peer.execute(barrier_query, (attempt,)).fetchone()
            if (_generation(primary) != _generation(before) or _generation(replica) != _generation(before)
                    or not primary["valid"] or not replica["valid"]):
                raise DatabaseUnavailable("role_changed_during_proof")
            if row_a == row_b == (binding.payload_sha256, binding.result_sha256) and barrier_b == (binding.request_id, before["epoch"]):
                evidence = contract.DurabilityEvidence(
                    authority_before=auth, authority_after=auth,
                    attempt=attempt, primary_attempt=attempt, partner_attempt=attempt,
                    binding=binding, primary_binding=binding, partner_binding=binding,
                    watermark=watermark, primary_flush=primary["flush"], partner_flush=replica["flush"],
                    partner_replay=replica["replay"], primary=primary["node"], partner=replica["node"],
                    cluster=primary["cluster"], epoch=primary["epoch"], timeline=primary["timeline"],
                    partner_source_timeline=replica["source_timeline"], primary_in_recovery=primary["recovery"],
                    partner_in_recovery=replica["recovery"], sync_state=primary["sync_state"],
                    fsync=primary["fsync"], full_page_writes=primary["full_page_writes"],
                    partner_fsync=replica["fsync"], partner_full_page_writes=replica["full_page_writes"],
                    synchronous_commit="on", configured_partner=primary["configured_partner"],
                    protection_history_safe=primary["history_safe"] and replica["history_safe"],
                    observed_from=start, observed_until=time.monotonic(),
                )
                try:
                    contract.require_protected_result(auth, binding, evidence, attempt=attempt, now=time.monotonic())
                    return
                except contract.AdmissionDenied as error:
                    rejection = str(error)
            time.sleep(0.02)
        raise DatabaseUnavailable("fresh_replication_proof_unavailable:" + rejection)
    except DatabaseUnavailable:
        raise
    except contract.AdmissionDenied as error:
        # Contract reasons are static labels; never surface arbitrary exception text.
        known = {'invalid_clock','invalid_cluster','invalid_generation','invalid_db_pair',
                 'invalid_worker','invalid_serving','invalid_mode','invalid_as_owners',
                 'invalid_write_state','authority_expired_or_invalid','invalid_service_or_node',
                 'explicit_matching_target_required','unexpected_target','not_serving_owner','not_effect_owner'}
        reason = str(error) if str(error) in known else 'invalid_authority'
        raise DatabaseUnavailable('replication_admission_denied:' + reason) from None
    except (psycopg.Error, KeyError, TypeError, ValueError) as error:
        raise DatabaseUnavailable("replication_observation_failed:" + type(error).__name__) from None
    finally:
        if peer is not None:
            peer.close()
