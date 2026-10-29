#!/usr/bin/env python3
"""Restore synthetic Book data into a fresh owned local container and volume.

This is a same-daemon fixture exercise, not N100 DR, physical HA, or a service
RTO/RPO guarantee. Optional --cold-assets exports and reloads an owned image;
the same daemon, build/layer cache and physical host remain available.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import secrets
import shutil
import signal
import stat
import tempfile
import time

from container_release_lab import Stopped, budget, metadata, prepare_context, ready, unchanged, valid_state
from container_runtime import LocalDocker
from release_artifact import _canonical

LIMIT = 65536
CONFIG = {'schema_version': 1, 'service': 'book-memo', 'database': '/app/data/fixture.sqlite3',
          'user': '10001:10001', 'network': 'none', 'readonly_root': True,
          'published_ports': False, 'memory_bytes': 268435456, 'pids': 64, 'cpus': 1,
          'readiness_path': '/ready', 'health_path': '/health'}
TABLES = ('books', 'book_chapters', 'book_memos', 'memo_tags', 'memo_revisions',
          'deleted_memos', 'write_requests', 'sqlite_sequence')


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def verify_payload(payload):
    """Reject non-fixture and oversized transfers before a destination exists."""
    if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'tables', 'schema', 'state'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or not isinstance(payload['tables'], dict) or set(payload['tables']) != set(TABLES)
            or not isinstance(payload['schema'], list) or not 1 <= len(payload['schema']) <= 64
            or not isinstance(payload['state'], dict) or not valid_state(payload['state'], 1)
            or digest(payload['tables']) != payload['state']['snapshot_hash']
            or digest(payload['schema']) != payload['state']['schema_hash']
            or len(_canonical(payload)) > LIMIT):
        raise Stopped('invalid_snapshot_bundle')
    for rows in payload['tables'].values():
        if not isinstance(rows, list) or len(rows) > 50:
            raise Stopped('invalid_snapshot_bundle')
        for row in rows:
            if not isinstance(row, list) or not 1 <= len(row) <= 32 or any(
                    not (cell is None or type(cell) is int or isinstance(cell, str) and len(cell) <= 4096)
                    for cell in row):
                raise Stopped('invalid_snapshot_bundle')
    return payload


def read_bundle(path, expected_digest):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= LIMIT):
            raise Stopped('invalid_snapshot_bundle')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise Stopped('snapshot_bundle_hash_mismatch')
        payload = json.loads(raw)
        if raw != _canonical(payload):
            raise Stopped('invalid_snapshot_bundle')
        return verify_payload(payload)
    except (OSError, ValueError, TypeError):
        raise Stopped('invalid_snapshot_bundle') from None


def runtime_evidence(engine, container, volume, image):
    evidence = engine.restore_evidence(container)
    if (evidence.get('container_id') != container or evidence.get('image_id') != image['id']
            or evidence.get('volume_name') != volume or evidence.get('mount_volume_names') != [volume]
            or evidence.get('owner_verified') is not True or evidence.get('user') != CONFIG['user']
            or evidence.get('network') != 'none' or evidence.get('readonly_root') is not True
            or evidence.get('published_ports') is not False):
        raise Stopped('runtime_identity_or_mount_mismatch')
    identity = engine.probe(container, 'identity')
    if (identity.get('http_status') != 200 or identity.get('main_sha') != image['main_sha']
            or identity.get('service_sha') != image['service_sha']):
        raise Stopped('image_identity_mismatch')
    return evidence


def run_lab(deadline, interrupted, progress=None, engine_factory=None, cold_assets=False):
    report = {'schema_version': 1, 'valid': False, 'reason': 'initialization', 'stage': 'initialization',
              'scope': 'same_local_daemon_fresh_container_volume_synthetic_fixture_only',
              'started_at': utc(), 'events': [], 'fixture_removed': False,
              'reporting_failed': False,
              'image_supply': 'committed_current_release_build_same_daemon_cache',
              'image_archive_export_import': 'not_exercised',
              'source_loss': 'simulated_source_container_stopped_volume_retained_for_owned_cleanup',
              'limits': ['not_N100_DR', 'not_physical_host_loss', 'not_HA', 'not_production_RTO_RPO'],
              'cleanup': {'ok': False}}
    engine = None
    root = None
    started_tick = time.monotonic()

    def save(stage, *, required=True):
        report['stage'] = stage
        report['events'].append({'event': stage, 'at': utc()})
        if cold_assets:
            report['events'][-1]['elapsed_seconds'] = time.monotonic() - started_tick
        if progress is not None:
            try:
                progress(report)
            except Exception:
                # A full disk or broken event stream must never bypass owned
                # resource cleanup. Preserve the failure in the in-memory result.
                report['reporting_failed'] = True
                report['valid'] = False
                if required:
                    raise Stopped('report_output_failed') from None
                if report['reason'] in ('initialization', 'ok'):
                    report['reason'] = 'report_output_failed'

    try:
        budget(deadline, interrupted)
        report['environment'] = metadata(deadline, interrupted)
        report['environment']['source_hashes']['book-memo/tools/independent_restore_lab.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        root = Path(tempfile.mkdtemp(prefix='independent-restore-')).resolve()
        owner = secrets.token_hex(16)
        (root / '.container-lab-owner').write_text(owner)
        save('fixture_created')
        engine = (engine_factory or LocalDocker)(owner, deadline, interrupted)
        report['docker'] = engine.info()
        commit = report['environment']['git_sha']
        context = prepare_context(root / 'v2', commit, commit, deadline, interrupted)
        image = engine.build(root / 'v2', context, 'v2')
        if any(image.get(key) != context[key] for key in ('app_commit', 'build_commit', 'main_sha', 'service_sha')):
            raise Stopped('image_provenance_mismatch')
        report['release_bundle'] = {'image': image, 'release_payload_sha256': context['payload_digest'],
                                    'configuration': dict(CONFIG), 'configuration_sha256': digest(CONFIG)}
        if cold_assets:
            report.update(scope='same_local_daemon_exported_assets_fresh_resources_synthetic_fixture_only',
                          image_supply='verified_owned_image_archive',
                          image_archive_export_import='pending',
                          source_loss='owned_source_container_volume_image_removed_cache_and_host_retained')
            config_path = root / 'configuration.json'
            config_path.write_bytes(_canonical(CONFIG))
            archive_path = root / 'image.tar'
            report['image_archive'] = engine.export_image(image, archive_path)
            report['release_bundle']['image_archive'] = dict(report['image_archive'])
            save('image_and_configuration_assets_exported')
        save('release_built')
        source_volume = engine.volume()
        source = engine.start(image, source_volume, 'v2')
        ready(engine, source, deadline, interrupted)
        report['source'] = runtime_evidence(engine, source, source_volume, image)
        snapshot = engine.probe(source, 'seed')
        included_ack_at = utc()
        if not valid_state(snapshot, 1):
            raise Stopped('source_seed_invalid')
        payload = verify_payload(engine.export_snapshot(source))
        if not unchanged(snapshot, payload['state']):
            raise Stopped('snapshot_capture_changed_data')
        snapshot_at = utc()
        encoded = _canonical(payload)
        bundle_path = root / 'snapshot.json'
        with bundle_path.open('xb') as output:
            output.write(encoded)
        bundle_digest = hashlib.sha256(encoded).hexdigest()
        report['snapshot_bundle'] = {'data_sha256': bundle_digest, 'size_bytes': len(encoded),
                                     'snapshot_state': snapshot, 'captured_at': snapshot_at,
                                     'included_write_ack_at': included_ack_at,
                                     'timestamp_source': 'host_UTC_immediately_after_fixed_probe_ack'}
        report['release_bundle']['bundle_manifest_sha256'] = digest(report['release_bundle'] | {'data_sha256': bundle_digest})
        save('snapshot_bundle_verified')
        acknowledged = engine.probe(source, 'write_v2')
        additional_ack_at = utc()
        if not valid_state(acknowledged, 2) or acknowledged.get('replay_verified') is not True:
            raise Stopped('additional_write_not_acknowledged')
        report['additional_acknowledged_write'] = {'ack_at': additional_ack_at, 'state': acknowledged,
                                                  'unique_write_count': 1, 'request_replay_verified': True}
        if cold_assets:
            report['known_acknowledged_writes'] = [
                {'id': 'seed_memo_v1', 'version': snapshot['memo_version'], 'ack_at': included_ack_at},
                {'id': 'memo_v2', 'version': acknowledged['memo_version'], 'ack_at': additional_ack_at}]
            report['acknowledgement_scope'] = 'fixed_synthetic_logical_memo_mutations_seed_v1_and_idempotent_v2'
            report['preparation_seconds'] = time.monotonic() - started_tick
        save('additional_write_acknowledged')
        engine.stop(source)
        loss_tick = time.monotonic()
        loss_at = utc()
        report['loss_boundary_at'] = loss_at
        report['rto_definition'] = 'confirmed_source_stopped_to_fresh_destination_ready_read_and_exact_snapshot_verified'
        save('simulated_source_loss_confirmed')
        budget(deadline, interrupted)
        if cold_assets:
            report['source_removal'] = engine.discard_source(source, source_volume, image)
            if not all(report['source_removal'].get(key) is True for key in ('container_absent', 'volume_absent', 'image_absent')):
                raise Stopped('source_removal_not_verified')
            save('source_owned_resources_removed')
            # Recovery reads the serialized fixed configuration, never ambient
            # environment or a configuration from the source runtime.
            info = config_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT
                    or config_path.read_bytes() != _canonical(CONFIG)
                    or digest(json.loads(config_path.read_bytes())) != report['release_bundle']['configuration_sha256']):
                raise Stopped('configuration_bundle_mismatch')
            save('configuration_asset_verified')
            image = engine.import_image(image, archive_path, report['image_archive'])
            report['image_archive_export_import'] = 'verified_same_daemon_source_image_removed'
            save('image_archive_reimported')
            report['rto_definition'] = 'confirmed_source_stopped_through_owned_resource_removal_asset_verification_image_import_fresh_destination_ready_read_and_exact_snapshot_verified'
        # This is the only recovery input: pinned release/config metadata plus
        # a verified serialized snapshot. The old DB is never mounted or read.
        recovered_payload = read_bundle(bundle_path, bundle_digest)
        destination_volume = engine.volume()
        if destination_volume == source_volume:
            raise Stopped('destination_volume_not_fresh')
        destination = engine.start(image, destination_volume, 'v2')
        if destination == source:
            raise Stopped('destination_container_not_fresh')
        ready(engine, destination, deadline, interrupted)
        destination_evidence = runtime_evidence(engine, destination, destination_volume, image)
        if source_volume in destination_evidence['mount_volume_names']:
            raise Stopped('destination_mounted_source_volume')
        report['destination'] = destination_evidence
        save('fresh_destination_verified')
        restored = engine.restore_snapshot(destination, recovered_payload)
        if not valid_state(restored, 1) or not unchanged(snapshot, restored):
            raise Stopped('restored_data_mismatch')
        report['destination_health'] = ready(engine, destination, deadline, interrupted)
        read = engine.probe(destination, 'read')
        final = engine.probe(destination, 'snapshot')
        if (read.get('valid') is not True or read.get('http_status') != 200
                or not valid_state(final, 1) or not unchanged(snapshot, final)
                or final['snapshot_hash'] == acknowledged['snapshot_hash']):
            raise Stopped('restored_read_or_lost_write_accounting_failed')
        recovered_at = utc()
        report['restored_state'] = final
        report['functional_read'] = read
        report['recovery'] = {'rto_seconds': time.monotonic() - loss_tick, 'verified_at': recovered_at,
                              'snapshot_included_write_ack_at': included_ack_at,
                              'latest_source_write_ack_at': additional_ack_at,
                              'measured_acknowledged_write_gap_seconds': (datetime.fromisoformat(additional_ack_at) - datetime.fromisoformat(included_ack_at)).total_seconds(),
                              'backup_age_at_loss_seconds': (datetime.fromisoformat(loss_at) - datetime.fromisoformat(snapshot_at)).total_seconds(),
                              'lost_acknowledged_writes': 1, 'restored_memo_version': 1,
                              'latest_source_memo_version': 2,
                              'rpo_scope': 'observed_synthetic_ack_write_gap_not_production_guarantee'}
        if cold_assets:
            recovered = [item for item in report['known_acknowledged_writes'] if item['version'] <= final['memo_version']]
            lost = [item for item in report['known_acknowledged_writes'] if item['version'] > final['memo_version']]
            report['recovery'].update(recovered_acknowledged_write_ids=[item['id'] for item in recovered],
                                      lost_acknowledged_write_ids=[item['id'] for item in lost],
                                      lost_acknowledged_writes=len(lost),
                                      preparation_and_recovery_seconds=time.monotonic() - started_tick)
        save('restored_exact_snapshot_ready_and_read_verified')
        engine.stop(destination)
        budget(deadline, interrupted)
        report['valid'], report['reason'] = True, 'ok'
    except Stopped as error:
        report['reason'] = str(error)
    except Exception:
        report['reason'] = 'interrupted' if interrupted() else 'deadline' if time.monotonic() >= deadline else 'restore_lab_failed'
    finally:
        save('cleanup', required=False)
        if engine is not None:
            try:
                report['cleanup'] = engine.cleanup()
            except Exception:
                report['cleanup'] = {'ok': False, 'reason': 'cleanup_failed'}
        else:
            report['cleanup'] = {'ok': True, 'containers_removed': 0, 'volumes_removed': 0, 'images_removed': 0}
        if root is None:
            report['fixture_removed'] = True
        elif report['cleanup'].get('ok') is True:
            try:
                shutil.rmtree(root)
                report['fixture_removed'] = not root.exists()
            except OSError:
                pass
        if not report['cleanup'].get('ok') or not report['fixture_removed']:
            report['valid'], report['reason'] = False, 'cleanup_failed'
        elif report['reporting_failed']:
            report['valid'], report['reason'] = False, 'report_output_failed'
        if interrupted() and report['cleanup'].get('ok'):
            report['valid'], report['reason'] = False, 'interrupted'
        report['finished_at'] = utc()
        save('finished', required=False)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--cold-assets', action='store_true', help='reload verified owned image/config/snapshot after source removal; same daemon/cache/host remain')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--jsonl', required=True, type=Path)
    parser.add_argument('--deadline', type=float, default=600)
    args = parser.parse_args(argv)
    if not args.run or not math.isfinite(args.deadline) or not 0 < args.deadline <= 1200 or args.output.absolute() == args.jsonl.absolute():
        parser.error('explicit --run, distinct new output paths and finite deadline>0..1200 required')
    return args


def main(argv=None):
    args = arguments(argv)
    interrupted = False

    def stop(_signal, _frame):
        nonlocal interrupted
        interrupted = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with args.output.open('x', encoding='utf-8') as output, args.jsonl.open('x', encoding='utf-8') as events:
            def save(report):
                output.seek(0)
                json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.truncate()
                output.flush()
                events.write(json.dumps({'at': utc(), 'stage': report['stage'], 'valid': report['valid'],
                                         'reason': report['reason']}, sort_keys=True, allow_nan=False) + '\n')
                events.flush()
            report = run_lab(time.monotonic() + args.deadline, lambda: interrupted, save, cold_assets=args.cold_assets)
    except OSError:
        print('independent_restore_lab=FAIL reason=output_unavailable')
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"independent_restore_lab={'PASS' if report['valid'] else 'FAIL'} reason={report['reason']}")
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
