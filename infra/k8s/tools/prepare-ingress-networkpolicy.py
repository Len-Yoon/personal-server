#!/usr/bin/env python3
"""Prepare an app ingress boundary from fresh, reviewed live evidence; never apply.

This is deliberately separate from the full ingress/egress candidate. Egress is
unchanged. Kubernetes permits node traffic regardless of these policies: a
node-SNAT Caddy or host-backed alias is not isolated by a pod peer rule. The
explicit node-exception acknowledgement accepts that limit, not Caddy-only
isolation. Caller collects snapshots and socket/HTTP proof in memory, reviews
the returned policy, then performs separately authorized create/rollback.

CLI validation emits counts/hashes only, never policy addresses or input values.
No network, subprocess, credential, or cluster access is performed here.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('ingress_snapshot_contract', Path(__file__).with_name('prepare-app-hardening.py'))
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
PORTS = {'portal-web': 8000, 'book-memo': 8003, 'youtube-memo': 8002, 'crawler-worker': 8001}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _fresh(value, now, seconds=300):
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= seconds:
            raise ValueError('fresh_timezone_aware_observation_required')
    except (AttributeError, TypeError, ValueError):
        raise ValueError('fresh_timezone_aware_observation_required') from None


def _pod_peer(peer, proof, category):
    if not isinstance(peer, dict) or set(peer) != {'namespaceSelector', 'podSelector'}:
        raise ValueError('namespace_and_pod_peer_required')
    ns = peer['namespaceSelector']
    pod = peer['podSelector']
    if not isinstance(ns, dict) or set(ns) != {'matchLabels'} or not isinstance(pod, dict) or set(pod) != {'matchLabels'}:
        raise ValueError('exact_match_labels_required')
    namespace = 'monitoring' if category == 'metrics' else 'personal-server'
    if ns['matchLabels'] != {'kubernetes.io/metadata.name': namespace}:
        raise ValueError('unexpected_source_namespace')
    labels = pod['matchLabels']
    if not isinstance(labels, dict) or not labels or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in labels.items()):
        raise ValueError('nonempty_pod_labels_required')
    if category == 'fanout' and labels.get('app.kubernetes.io/name') != 'portal-web':
        raise ValueError('portal_fanout_source_required')
    if category == 'metrics' and labels.get('app.kubernetes.io/name') != 'prometheus':
        raise ValueError('prometheus_source_required')
    if proof.get('source_kind') != 'pod' or not proof.get('source_pod_uid') or proof.get('source_namespace') != namespace:
        raise ValueError('observed_source_pod_required')
    source_labels = proof.get('source_labels', {})
    if not isinstance(source_labels, dict) or any(source_labels.get(k) != v for k, v in labels.items()):
        raise ValueError('source_labels_do_not_match_peer')
    if proof.get('post_nat_source_matches_pod') is not True:
        raise ValueError('post_nat_source_pod_proof_required')


def prepare(snapshot, inventory, *, now=None):
    now = now or datetime.now(timezone.utc)
    meta, pod, _, container = _contract.validate_snapshot(snapshot)
    _contract.validate_observation(inventory, meta, now=now)
    _fresh(inventory.get('observed_at'), now)
    if inventory.get('image') != container['image'] or inventory.get('spec_sha256') != digest(snapshot['spec']):
        raise ValueError('image_and_spec_bound_inventory_required')
    if snapshot['spec'].get('replicas') != 1 or snapshot['spec'].get('strategy', {}).get('type') != 'Recreate':
        raise ValueError('existing_single_writer_required')
    if pod.get('containers') != [container]:
        raise ValueError('sidecar_requires_separate_port_review')
    labels = snapshot['spec']['template']['metadata'].get('labels', {})
    if labels.get('app.kubernetes.io/name') != meta['name']:
        raise ValueError('app_selector_mismatch')
    if inventory.get('existing_policies') != [] or inventory.get('flow_review_complete') is not True:
        raise ValueError('empty_policy_set_and_complete_review_required')
    smoke = inventory.get('enforcement', {})
    if not inventory.get('cluster_uid') or smoke.get('cluster_uid') != inventory['cluster_uid']:
        raise ValueError('cluster_bound_smoke_required')
    _fresh(smoke.get('observed_at'), now, 86400)
    if any(smoke.get(key) is not True for key in ('baseline', 'deny', 'selective_allow', 'unknown_pod_denied', 'cleanup', 'production_unchanged')):
        raise ValueError('actual_positive_negative_and_cleanup_smoke_required')
    caddy = inventory.get('caddy', {})
    _fresh(caddy.get('observed_at'), now)
    if caddy.get('status') != 200 or caddy.get('controlled_request') is not True or caddy.get('post_nat_observed') is not True:
        raise ValueError('controlled_caddy_post_nat_observation_required')
    if caddy.get('target_spec_sha256') != inventory['spec_sha256']:
        raise ValueError('target_bound_caddy_observation_required')
    ingress = []
    if caddy.get('source_kind') == 'node':
        if inventory.get('acknowledge_node_exception') is not True or caddy.get('source_matches_node_interface') is not True:
            raise ValueError('explicit_node_exception_acknowledgement_required')
    elif caddy.get('source_kind') == 'exact_ip':
        block = caddy.get('peer', {}).get('ipBlock', {})
        if set(caddy.get('peer', {})) != {'ipBlock'} or set(block) != {'cidr'}:
            raise ValueError('exact_caddy_ip_required')
        try:
            network = ipaddress.ip_network(block['cidr'], strict=True)
        except (ValueError, TypeError):
            raise ValueError('exact_caddy_ip_required') from None
        if network.prefixlen != network.max_prefixlen or network.is_multicast or network.is_unspecified or network.is_loopback:
            raise ValueError('single_unicast_caddy_address_required')
        if caddy.get('post_nat_peer_sha256') != digest(caddy['peer']) or caddy.get('source_is_node') is not False:
            raise ValueError('exact_non_node_post_nat_peer_proof_required')
        ingress.append({'from': [deepcopy(caddy['peer'])], 'ports': [{'protocol': 'TCP', 'port': PORTS[meta['name']]}]})
    else:
        raise ValueError('known_caddy_post_nat_source_required')
    flows = inventory.get('flows')
    exemptions = inventory.get('not_applicable', {})
    required = {'metrics', 'fanout'}
    if not isinstance(flows, list) or not isinstance(exemptions, dict) or set(exemptions) - required:
        raise ValueError('complete_ingress_review_required')
    if any(not isinstance(reason, str) or not reason.strip() for reason in exemptions.values()):
        raise ValueError('documented_non_applicable_flow_required')
    seen = set()
    for flow in flows:
        if not isinstance(flow, dict) or set(flow) != {'category', 'peer', 'port', 'proof'}:
            raise ValueError('explicit_ingress_flow_required')
        category = flow['category']
        if category not in required or category in seen or category in exemptions:
            raise ValueError('duplicate_or_unknown_ingress_category')
        proof = flow['proof']
        if not isinstance(proof, dict):
            raise ValueError('flow_source_proof_required')
        _fresh(proof.get('observed_at'), now)
        if type(flow['port']) is not int or flow['port'] != PORTS[meta['name']]:
            raise ValueError('only_observed_app_TCP_port_allowed')
        if proof.get('status') != 200 or proof.get('peer_sha256') != digest(flow['peer']) or proof.get('target_spec_sha256') != inventory['spec_sha256']:
            raise ValueError('successful_peer_and_target_bound_read_probe_required')
        _pod_peer(flow['peer'], proof, category)
        seen.add(category)
        ingress.append({'from': [deepcopy(flow['peer'])], 'ports': [{'protocol': 'TCP', 'port': flow['port']}]})
    if seen | set(exemptions) != required:
        raise ValueError('missing_ingress_category_review')
    # These runtime contracts always require Portal direct fanout and current
    # Portal/Crawler Prometheus scrapes. Host aliases cannot replace pod proof.
    if meta['name'] != 'portal-web' and 'fanout' not in seen:
        raise ValueError('downstream_portal_direct_fanout_proof_required')
    if meta['name'] in {'portal-web', 'crawler-worker'} and 'metrics' not in seen:
        raise ValueError('current_prometheus_scrape_proof_required')
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': meta['name'] + '-ingress-reviewed', 'namespace': meta['namespace']}, 'spec': {'podSelector': {'matchLabels': {'app.kubernetes.io/name': meta['name']}}, 'policyTypes': ['Ingress'], 'ingress': ingress}}
    return {'policy': policy, 'policy_sha256': digest(policy), 'deployment_uid': meta['uid'], 'resource_version': meta['resourceVersion'], 'spec_sha256': inventory['spec_sha256'], 'approval_required': True, 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'snapshot': 'fresh Deployment', 'inventory': 'UID/RV/image/spec SHA; cluster-bound positive/negative smoke; controlled Caddy post-NAT source; fresh actual pod source proofs for metrics/fanout; documented exemptions; node exception acknowledgement', 'validation_only': True, 'policy_output': 'in-memory prepare() only; separately reviewed authorized application required', 'max_flow_age_seconds': 300, 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False}, sort_keys=True))
        return
    if not args.snapshot or not args.inventory:
        parser.error('--snapshot and --inventory are required')
    try:
        result = prepare(json.loads(args.snapshot.read_text()), json.loads(args.inventory.read_text()))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(2, 'ingress_inventory_validation=FAIL\n')
    print(json.dumps({'validation': 'PASS', 'policy_sha256': result['policy_sha256'], 'ingress_rule_count': len(result['policy']['spec']['ingress']), 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False, 'approval_required': True}, sort_keys=True))


if __name__ == '__main__':
    main()
