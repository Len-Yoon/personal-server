#!/usr/bin/env python3
"""Prepare one app-scoped NetworkPolicy; never contact or mutate a cluster.

Use --describe-input for the inventory contract. Each flow needs a reviewed peer,
ports and evidence reference. Required categories: DNS, Caddy ingress, metrics,
app fanout, external APIs/Drive, backups. Non-applicable categories need explicit
reasons; DNS cannot be exempted. IP peers must be observed after NodePort/SNAT,
not inferred from the Caddy host address. DNS rules do not permit arbitrary
external APIs: their maintained IP ranges must be supplied separately. Standard
NetworkPolicy cannot express FQDN allowlists; changing API ranges require review.
"""
import argparse
from copy import deepcopy
import importlib.util
import ipaddress
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('app_hardening_contract', Path(__file__).with_name('prepare-app-hardening.py'))
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
CATEGORIES = {'dns', 'caddy', 'metrics', 'fanout', 'external', 'backups'}


def _labels(selector):
    if not isinstance(selector, dict) or set(selector) != {'matchLabels'}:
        raise ValueError('nonempty_match_labels_required')
    labels = selector['matchLabels']
    if not isinstance(labels, dict) or not labels or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in labels.items()):
        raise ValueError('nonempty_match_labels_required')
    return labels


def _peer(peer):
    if not isinstance(peer, dict):
        raise ValueError('peer_required')
    if set(peer) == {'namespaceSelector', 'podSelector'}:
        namespace = _labels(peer['namespaceSelector'])
        _labels(peer['podSelector'])
        if set(namespace) != {'kubernetes.io/metadata.name'}:
            raise ValueError('exact_peer_namespace_required')
    elif set(peer) == {'ipBlock'}:
        block = peer['ipBlock']
        if not isinstance(block, dict) or set(block) != {'cidr'}:
            raise ValueError('explicit_cidr_required')
        network = ipaddress.ip_network(block['cidr'], strict=True)
        # Large, default-route-like networks defeat isolation. Wider provider
        # ranges require a separate reviewed policy; do not silently permit them.
        if network.prefixlen < (16 if network.version == 4 else 48) or network.is_multicast or network.is_unspecified:
            raise ValueError('bounded_unicast_cidr_required')
    else:
        raise ValueError('scoped_peer_required')


def _ports(ports):
    if not isinstance(ports, list) or not ports:
        raise ValueError('explicit_ports_required')
    for port in ports:
        if not isinstance(port, dict) or set(port) != {'protocol', 'port'} or port['protocol'] not in {'TCP', 'UDP'} or type(port['port']) is not int or not 1 <= port['port'] <= 65535:
            raise ValueError('numeric_TCP_UDP_port_required')


def prepare(snapshot, inventory):
    meta, _, _, _ = _contract.validate_snapshot(snapshot)
    _contract.validate_observation(inventory, meta)
    if inventory.get('enforcement_smoke_passed') is not True or inventory.get('flow_review_complete') is not True:
        raise ValueError('enforcement_and_complete_flow_review_required')
    if inventory.get('existing_policies') != []:
        raise ValueError('existing_policy_union_requires_separate_review')
    labels = snapshot['spec']['template']['metadata']['labels']
    if labels.get('app.kubernetes.io/name') != meta['name']:
        raise ValueError('observed_app_selector_required')
    exemptions = inventory.get('not_applicable', {})
    if not isinstance(exemptions, dict) or set(exemptions) - (CATEGORIES - {'dns'}):
        raise ValueError('invalid_flow_exemption')
    if any(not isinstance(reason, str) or not reason.strip() for reason in exemptions.values()):
        raise ValueError('documented_exemption_required')
    flows = inventory.get('flows')
    if not isinstance(flows, list) or not flows:
        raise ValueError('reviewed_flows_required')
    spec = {'podSelector': {'matchLabels': {'app.kubernetes.io/name': meta['name']}}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}
    seen = set()
    dns_ports = set()
    for flow in flows:
        if not isinstance(flow, dict) or set(flow) != {'direction', 'category', 'peer', 'ports', 'evidence'}:
            raise ValueError('complete_flow_required')
        direction, category = flow['direction'], flow['category']
        if direction not in {'Ingress', 'Egress'} or category not in CATEGORIES:
            raise ValueError('invalid_flow_direction_or_category')
        if not isinstance(flow['evidence'], str) or not flow['evidence'].strip():
            raise ValueError('flow_evidence_reference_required')
        _peer(flow['peer'])
        _ports(flow['ports'])
        if category in {'caddy', 'metrics'} and direction != 'Ingress':
            raise ValueError('caddy_and_metrics_ingress_required')
        if category in {'dns', 'external'} and direction != 'Egress':
            raise ValueError('dns_and_external_egress_required')
        if category == 'dns':
            peer = flow['peer']
            if 'namespaceSelector' not in peer or peer['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] != 'kube-system' or peer['podSelector']['matchLabels'].get('k8s-app') != 'kube-dns':
                raise ValueError('observed_kube_dns_selector_required')
            if any(port['port'] != 53 for port in flow['ports']):
                raise ValueError('dns_port_53_required')
            dns_ports.update(port['protocol'] for port in flow['ports'])
        if category == 'external' and 'ipBlock' not in flow['peer']:
            raise ValueError('external_api_requires_maintained_IP_ranges')
        seen.add(category)
        key = 'from' if direction == 'Ingress' else 'to'
        spec[direction.lower()].append({key: [deepcopy(flow['peer'])], 'ports': deepcopy(flow['ports'])})
    if seen & set(exemptions) or seen | set(exemptions) != CATEGORIES or dns_ports != {'TCP', 'UDP'}:
        raise ValueError('complete_nonconflicting_flow_coverage_required')
    if not spec['ingress'] or not spec['egress']:
        raise ValueError('reviewed_ingress_and_egress_required')
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': meta['name'] + '-reviewed-allowlist', 'namespace': meta['namespace']}, 'spec': spec}
    return {'policy': policy, 'approval_required': True, 'deployment_uid': meta['uid'], 'resource_version': meta['resourceVersion'],
            'reviewed_flows': deepcopy(flows), 'not_applicable': deepcopy(exemptions),
            'preconditions': ['fresh namespace policy and peer inventory immediately before approved create', 'CNI enforcement smoke PASS and NodePort/SNAT source observation', 'verify DNS, Caddy, metrics, fanout, Drive/external APIs and backups before/after each app policy', 'maintain external provider CIDRs; policy supports IPs, not FQDNs', 'sequential app rollout with named-policy deletion rollback and external health checks']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot')
    parser.add_argument('--inventory')
    parser.add_argument('--describe-input', action='store_true')
    args = parser.parse_args()
    if args.describe_input:
        print(json.dumps({'deployment_uid': 'REQUIRED', 'resource_version': 'REQUIRED', 'observed_at': 'REQUIRED_UTC_ISO8601', 'enforcement_smoke_passed': False, 'existing_policies': None, 'flow_review_complete': False, 'not_applicable': {}, 'flows': [], 'flow_schema': {'direction': 'Ingress|Egress', 'category': '|'.join(sorted(CATEGORIES)), 'peer': {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'OBSERVED_NAMESPACE'}}, 'podSelector': {'matchLabels': {'OBSERVED_KEY': 'OBSERVED_VALUE'}}}, 'ports': [{'protocol': 'TCP|UDP', 'port': None}], 'evidence': 'REQUIRED_READ_ONLY_EVIDENCE_REFERENCE'}, 'ip_peer_alternative': {'ipBlock': {'cidr': 'OBSERVED_MAINTAINED_CIDR'}}}, indent=2))
        return
    if not args.snapshot or not args.inventory:
        parser.error('--snapshot and --inventory are required')
    try:
        with open(args.snapshot) as source:
            snapshot = json.load(source)
        with open(args.inventory) as source:
            inventory = json.load(source)
        result = prepare(snapshot, inventory)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.exit(1, 'networkpolicy_prepare=FAIL; verify snapshot and flow inventory contract\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
