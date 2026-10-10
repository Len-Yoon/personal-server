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

pre_nat_exact_ip is a separate Book ClusterIP:8003 offline candidate contract.
Its current request proof and disposable diagnostic have distinct target IDs.
Dynamic endpoint evidence never establishes production readiness or authority.
Input hashes check consistency; trusted collection/review establishes provenance.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import re
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


def _pre_nat_candidate(caddy, smoke, inventory, app, now):
    # The observed disposable route was ClusterIP:8003. It is a different
    # target from the fresh production Book request, never a Portal NodePort.
    if app != 'book-memo':
        raise ValueError('pre_nat_diagnostic_only_covers_book_clusterip_8003')
    if not isinstance(inventory.get('cluster_uid'), str) or not inventory['cluster_uid'].strip() or type(caddy.get('status')) is not int:
        raise ValueError('pre_nat_cluster_and_http_status_required')
    caddy_fields = {'observed_at', 'status', 'controlled_request', 'source_kind', 'target_spec_sha256', 'target_pod_uid', 'peer', 'pre_nat_peer_sha256', 'source_is_node', 'current_identity', 'pre_nat_proof', 'endpoint_stability'}
    if set(caddy) != caddy_fields or caddy.get('endpoint_stability') != 'dynamic' or caddy.get('source_is_node') is not False:
        raise ValueError('separate_dynamic_pre_nat_evidence_required')
    peer = caddy.get('peer')
    if not isinstance(peer, dict) or set(peer) != {'ipBlock'} or not isinstance(peer['ipBlock'], dict) or set(peer['ipBlock']) != {'cidr'}:
        raise ValueError('exact_pre_nat_peer_required')
    try:
        network = ipaddress.ip_network(peer['ipBlock']['cidr'], strict=True)
    except (ValueError, TypeError):
        raise ValueError('exact_pre_nat_peer_required') from None
    if (network.version != 4 or network.prefixlen != 32 or network.is_unspecified
            or network.is_multicast or network.is_loopback or network.is_link_local or network.is_reserved):
        raise ValueError('observed_single_ipv4_pre_nat_peer_required')
    identity = caddy.get('current_identity')
    identity_fields = {'container_id_sha256', 'container_image_sha256', 'started_at', 'network_settings_sha256'}
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise ValueError('current_caddy_identity_required')
    for key in ('container_id_sha256', 'network_settings_sha256'):
        if not isinstance(identity[key], str) or not re.fullmatch('[a-f0-9]{64}', identity[key]):
            raise ValueError('current_caddy_identity_required')
    if not isinstance(identity['container_image_sha256'], str) or not re.fullmatch('sha256:[a-f0-9]{64}', identity['container_image_sha256']):
        raise ValueError('current_caddy_image_identity_required')
    try:
        started = datetime.fromisoformat(identity['started_at'].replace('Z', '+00:00'))
        if started.tzinfo is None or started > now:
            raise ValueError('current_caddy_start_identity_required')
    except (AttributeError, TypeError, ValueError):
        raise ValueError('current_caddy_start_identity_required') from None
    # This proof belongs to the fresh production controlled request above.
    # The diagnostic below has its own isolated Pod/Service and route; neither
    # an input route claim nor its hash independently proves a live route.
    proof = caddy.get('pre_nat_proof')
    true_fields = {'same_connection_bound', 'pid_start_inode_bound', 'http_marker_bound', 'source_matches_current_endpoint'}
    proof_fields = true_fields | {'current_identity_sha256', 'source_sha256', 'peer_sha256', 'source_interface', 'node_uid', 'route', 'port'}
    if not isinstance(proof, dict) or set(proof) != proof_fields or any(proof[key] is not True for key in true_fields):
        raise ValueError('same_connection_current_endpoint_pre_nat_proof_required')
    node_uid = inventory.get('target_node_uid')
    if not isinstance(node_uid, str) or not node_uid.strip() or proof['node_uid'] != node_uid:
        raise ValueError('pre_nat_current_target_node_required')
    bindings = {'current_identity_sha256': digest(identity), 'source_sha256': digest(str(network.network_address)), 'peer_sha256': digest(peer)}
    if (proof['source_interface'] != 'caddy_container_endpoint' or proof['route'] != 'ClusterIP'
            or type(proof['port']) is not int or proof['port'] != 8003
            or any(proof[key] != value for key, value in bindings.items())
            or caddy['pre_nat_peer_sha256'] != bindings['peer_sha256']):
        raise ValueError('pre_nat_source_peer_identity_and_route_binding_required')
    if set(smoke) != {'cluster_uid', 'pre_nat_diagnostic'}:
        raise ValueError('separate_pre_nat_diagnostic_required')
    diagnostic = smoke.get('pre_nat_diagnostic')
    fields = {'cluster_uid', 'node_uid', 'run_id', 'tool_sha256', 'result', 'result_sha256', 'current_identity_sha256', 'source_sha256', 'peer_sha256', 'server_pod_uid', 'service_uid', 'route', 'port', 'phases'}
    if not isinstance(diagnostic, dict) or set(diagnostic) != fields:
        raise ValueError('bound_pre_nat_diagnostic_required')
    if (diagnostic['cluster_uid'] != inventory['cluster_uid'] or diagnostic['node_uid'] != node_uid
            or diagnostic['route'] != 'ClusterIP' or type(diagnostic['port']) is not int or diagnostic['port'] != 8003
            or any(diagnostic[key] != value for key, value in bindings.items())):
        raise ValueError('diagnostic_cluster_node_identity_peer_route_binding_required')
    for key in ('tool_sha256', 'result_sha256'):
        if not isinstance(diagnostic[key], str) or not re.fullmatch('[a-f0-9]{64}', diagnostic[key]):
            raise ValueError('diagnostic_tool_and_result_hash_required')
    if not isinstance(diagnostic['run_id'], str) or not re.fullmatch('[a-f0-9]{12}', diagnostic['run_id']):
        raise ValueError('diagnostic_run_identity_required')
    isolated_pod, service = diagnostic['server_pod_uid'], diagnostic['service_uid']
    if (not isinstance(isolated_pod, str) or not isolated_pod.strip() or not isinstance(service, str) or not service.strip()
            or isolated_pod == inventory['target_pod_uid'] or service in (isolated_pod, inventory['target_pod_uid'])):
        raise ValueError('distinct_isolated_server_and_service_required')
    phases = diagnostic['phases']
    phase_fields = {'baseline', 'caddy_denied', 'exact_ip_allow', 'unknown_pod_denied', 'unknown_service_denied'}
    if not isinstance(phases, dict) or set(phases) != phase_fields or any(phases[key] is not True for key in phase_fields):
        raise ValueError('actual_pre_nat_positive_negative_phases_required')
    result = diagnostic['result']
    true_result = {'owned_cleanup_complete', 'restored_flow_required', 'restored_flow_proven', 'production_postguard_pass', 'resource_create_attempted'}
    false_result = {'caddy_only_identity_isolation', 'long_term_stable_ip_proven', 'policy_hook_directly_observed', 'production_apply_authorized_by_result', 'production_identity_proven', 'caddy_static_ipv4_configured', 'raw_address_output', 'raw_address_persisted'}
    null_result = {'first_failure_sha256', 'cleanup_failure_reason', 'postguard_failure_reason'}
    result_fields = true_result | false_result | null_result | {'status', 'source_kind', 'run_id', 'target', 'completed_at', 'reason', 'production_app_count', 'production_policy_count'}
    if (not isinstance(result, dict) or set(result) not in (result_fields, result_fields | {'empty_endpoint_cleanup_used'})
            or digest(result) != diagnostic['result_sha256']
            or result['status'] != 'PASS' or result['source_kind'] != 'pre_nat_exact_ip'
            or result['run_id'] != diagnostic['run_id'] or result['reason'] != 'isolated_exact_ip_causal_admission_proven'
            or not isinstance(result['target'], str) or not re.fullmatch('[a-f0-9]{40}', result['target'])
            or any(result[key] is not True for key in true_result) or any(result[key] is not False for key in false_result)
            or any(result[key] is not None for key in null_result)
            or type(result['production_app_count']) is not int or result['production_app_count'] != 4
            or type(result['production_policy_count']) is not int or result['production_policy_count'] != 0
            or 'empty_endpoint_cleanup_used' in result and type(result['empty_endpoint_cleanup_used']) is not bool):
        raise ValueError('successful_hash_bound_cleaned_pre_nat_result_required')
    _fresh(result['completed_at'], now)
    completed = datetime.fromisoformat(result['completed_at'].replace('Z', '+00:00'))
    current_observed = datetime.fromisoformat(caddy['observed_at'].replace('Z', '+00:00'))
    if not started <= completed <= current_observed:
        raise ValueError('current_socket_observation_after_diagnostic_required')
    return {'from': [deepcopy(peer)], 'ports': [{'protocol': 'TCP', 'port': 8003}]}


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
    if not isinstance(smoke, dict):
        raise ValueError('enforcement_object_required')
    if not inventory.get('cluster_uid') or smoke.get('cluster_uid') != inventory['cluster_uid']:
        raise ValueError('cluster_bound_smoke_required')
    caddy = inventory.get('caddy', {})
    if not isinstance(caddy, dict):
        raise ValueError('caddy_object_required')
    pre_nat = caddy.get('source_kind') == 'pre_nat_exact_ip'
    if not pre_nat:
        _fresh(smoke.get('observed_at'), now, 86400)
        if any(smoke.get(key) is not True for key in ('baseline', 'deny', 'selective_allow', 'unknown_pod_denied', 'cleanup', 'production_unchanged')):
            raise ValueError('actual_positive_negative_and_cleanup_smoke_required')
    _fresh(caddy.get('observed_at'), now)
    if caddy.get('status') != 200 or caddy.get('controlled_request') is not True or not pre_nat and caddy.get('post_nat_observed') is not True:
        raise ValueError('controlled_caddy_pre_nat_observation_required' if pre_nat else 'controlled_caddy_post_nat_observation_required')
    if caddy.get('target_spec_sha256') != inventory['spec_sha256']:
        raise ValueError('target_bound_caddy_observation_required')
    target_pod_uid = inventory.get('target_pod_uid')
    if not isinstance(target_pod_uid, str) or not target_pod_uid.strip() or caddy.get('target_pod_uid') != target_pod_uid:
        raise ValueError('current_target_pod_bound_caddy_observation_required')
    ingress = []
    if pre_nat:
        ingress.append(_pre_nat_candidate(caddy, smoke, inventory, meta['name'], now))
    elif caddy.get('source_kind') == 'node':
        if inventory.get('acknowledge_node_exception') is not True or caddy.get('source_matches_node_interface') is not True:
            raise ValueError('explicit_node_exception_acknowledgement_required')
        target_node_uid = inventory.get('target_node_uid')
        proof = caddy.get('node_exception_proof', {})
        if (not isinstance(target_node_uid, str) or not target_node_uid.strip()
                or not isinstance(proof, dict)
                or proof.get('source_matches_target_node_address') is not True
                or proof.get('source_node_uid') != target_node_uid
                or proof.get('target_pod_uid') != target_pod_uid):
            raise ValueError('exact_target_node_exception_proof_required')
        _fresh(proof.get('observed_at'), now)
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
    result = {'policy': policy, 'policy_sha256': digest(policy), 'deployment_uid': meta['uid'], 'resource_version': meta['resourceVersion'], 'spec_sha256': inventory['spec_sha256'], 'approval_required': True, 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False}
    if pre_nat:
        result.update(caddy_source_kind='pre_nat_exact_ip', offline_candidate_only=True, operating_ready=False,
                      production_apply_authorized=False, stop_reasons=['dynamic_caddy_endpoint_not_durable_identity'],
                      policy_hook_directly_observed=False, long_term_stable_ip_proven=False,
                      production_identity_proven=False, production_apply_succeeded=False, production_route_proven=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'snapshot': 'fresh Deployment', 'inventory': 'UID/RV/image/spec SHA; cluster-bound positive/negative smoke; controlled Caddy post-NAT source; fresh actual pod source proofs for metrics/fanout; documented exemptions; node exception acknowledgement', 'validation_only': True, 'pre_nat_exact_ip': 'Book ClusterIP:8003 offline candidate only; caddy.pre_nat_proof belongs to the fresh production controlled request, enforcement.pre_nat_diagnostic to a distinct isolated server/Service; identity/socket/peer hashes; canonical JSON result SHA and tool SHA check consistency, not authenticity; trusted collector and review required; positive/negative/restore/cleanup proof; operating_ready=false; production route/apply, NodePort, policy hook and durable identity remain unproven', 'operating_ready': False, 'production_apply_authorized': False, 'policy_output': 'in-memory prepare() only; separately reviewed authorized application required', 'max_flow_age_seconds': 300, 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False}, sort_keys=True))
        return
    if not args.snapshot or not args.inventory:
        parser.error('--snapshot and --inventory are required')
    try:
        result = prepare(json.loads(args.snapshot.read_text()), json.loads(args.inventory.read_text()))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(2, 'ingress_inventory_validation=FAIL\n')
    print(json.dumps({'validation': 'PASS', 'offline_validation_only': True, **({key: result[key] for key in ('operating_ready', 'production_apply_authorized', 'stop_reasons')} if 'operating_ready' in result else {}), 'policy_sha256': result['policy_sha256'], 'ingress_rule_count': len(result['policy']['spec']['ingress']), 'egress_unchanged': True, 'node_traffic_unrestricted': True, 'caddy_only_isolation': False, 'approval_required': True}, sort_keys=True))


if __name__ == '__main__':
    main()
