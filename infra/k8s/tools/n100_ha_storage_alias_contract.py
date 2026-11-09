"""Pure storage/actor model; supplied metadata is never an OS attestation.

evaluate consumes a caller-owned attempt nonce even on rejection. The caller
must own that set for the entire session; this module creates no durable store.
No collection, filesystem reads, child processes, authority or writer receipt.
Windows handle semantics: learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_id_info
Linux coordinates retain legacy_linux's supported-FS boundary. 9p is unknown.
"""
import json
import math
import re
import time
from pathlib import PurePosixPath

LIMIT = 131072
ITEM_LIMIT = 512
EDGE_LIMIT = 2048
HEX = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
SUPPORTED = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'tmpfs'}
PROVIDERS = {'docker', 'kubernetes', 'systemd', 'cron', 'processes', 'mounts',
             'windows_services', 'windows_tasks', 'windows_processes', 'windows_startup', 'storage'}
REASONS = {'invalid_schema', 'inventory_limit', 'duplicate_identity', 'nonce_mismatch', 'source_mismatch',
           'deadline_exceeded', 'incomplete_provider', 'handle_lifetime_incomplete', 'handle_identity_changed',
           'unknown_actor_state', 'duplicate_state', 'owner_unavailable', 'multiple_declared_owners',
           'state_parent_required', 'seven_service_state_coverage', 'duplicate_edge', 'unknown_capability_edge',
           'storage_relation_unknown', 'credential_read_capability', 'competing_writer_or_recreator',
           'owner_write_coverage_incomplete', 'indirect_writer_capability', 'analysis_limit', 'invalid_clock',
           'nonce_reused', 'nonce_store_full', 'payload_limit'}
STATE_CATALOG = {
    'portal-web': {'database', 'security_state', 'file_data', 'temporary_parent'},
    'book-memo': {'database', 'sidecar_parent', 'auth_state'},
    'youtube-memo': {'database', 'sidecar_parent', 'auth_state'},
    'crawler-worker': {'archive_state', 'collection_state', 'notification_state', 'temporary_parent'},
    'car-care-worker': {'database', 'oauth_state', 'sidecar_parent', 'temporary_parent'},
    'homeops-executor': {'database', 'sidecar_parent'},
    'system-agent': {'node_state'},
}


class Rejected(ValueError):
    pass


def require(condition, reason='invalid_schema'):
    if not condition:
        raise Rejected(reason) from None


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys))


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def sha(value):
    return type(value) is str and HEX.fullmatch(value) is not None


def items(values, maximum=ITEM_LIMIT):
    require(type(values) is list and len(values) <= maximum, 'inventory_limit')
    return values


def index(values):
    result = {}
    for value in items(values):
        require(type(value) is dict and type(value.get('id')) is str and ID.fullmatch(value['id']))
        require(value['id'] not in result, 'duplicate_identity')
        result[value['id']] = value
    return result


def coordinate(value):
    require(type(value) is str and 0 < len(value) <= 4096 and value.startswith('/')
            and str(PurePosixPath(value)) == value and '..' not in PurePosixPath(value).parts
            and not any(ord(c) < 32 for c in value))


def identity(value, provider):
    common = {'kind', 'links', 'redirected'}
    exact(value, common | ({'backend', 'volume', 'file_id'} if provider == 'windows'
                          else {'filesystem', 'boot', 'namespace', 'device', 'root', 'inode'}))
    require(value['kind'] in {'file', 'directory'} and type(value['links']) is int
            and 1 <= value['links'] <= ITEM_LIMIT and type(value['redirected']) is bool)
    if provider == 'windows':
        require(value['backend'] in {'local_ntfs', 'local_refs', 'remote', 'unknown'}
                and sha(value['volume']) and type(value['file_id']) is str
                and re.fullmatch('[0-9a-f]{32}', value['file_id']) is not None)
    else:
        require(value['filesystem'] in SUPPORTED | {'9p', 'unknown'} and sha(value['boot'])
                and sha(value['namespace']) and type(value['device']) is str
                and re.fullmatch('[0-9]{1,10}:[0-9]{1,10}', value['device']) is not None
                and type(value['inode']) is int and value['inode'] > 0)
        coordinate(value['root'])


def ancestor(left, right):
    return left == right or right.startswith(left.rstrip('/') + '/')


def same_domain(left, right):
    return (left['filesystem'] in SUPPORTED and left['filesystem'] == right['filesystem']
            and all(left[key] == right[key] for key in ('boot', 'namespace', 'device')))


def file_relation(left, right):
    """Validated session handles only; different Windows IDs never imply disjoint."""
    a, b = left['before'], right['before']
    if left['provider'] != right['provider']:
        return 'unknown'
    if left['provider'] == 'windows':
        return ('overlap' if a['backend'] in {'local_ntfs', 'local_refs'}
                and a['backend'] == b['backend'] and a['volume'] == b['volume']
                and a['file_id'] == b['file_id'] else 'unknown')
    if same_domain(a, b) and a['inode'] == b['inode']:
        return 'overlap'
    return 'unknown'


def closed_footprint(footprint, handles):
    """Derive finite tree closure from directory rows, not an attestation flag."""
    members = set(footprint['members'])
    root = footprint['root']
    require(root in members and members <= handles.keys())
    directories = {}
    incoming = {}
    for row in items(footprint['directories']):
        exact(row, {'directory', 'children'})
        parent = row['directory']
        require(parent in members and parent not in directories)
        children = items(row['children'])
        require(len(children) == len(set(children)) and set(children) <= members)
        directories[parent] = children
        for child in children:
            require(child != root and child not in incoming)
            incoming[child] = parent
    expected = {key for key in members if handles[key]['before']['kind'] == 'directory'}
    if set(directories) != expected or set(incoming) != members - {root}:
        return False
    reached, pending = set(), [root]
    while pending:
        current = pending.pop()
        if current in reached:
            return False
        reached.add(current)
        pending.extend(directories.get(current, []))
    if reached != members:
        return False
    for key in members:
        h = handles[key]; value = h['before']
        if (h['provider'] != 'linux' or value['filesystem'] not in SUPPORTED
                or value['redirected'] or value['kind'] == 'file' and value['links'] != 1):
            return False
        if key in incoming:
            parent = handles[incoming[key]]['before']
            # A different device/export/submount requires its own binding;
            # merely declaring an edge cannot establish that binding.
            if not same_domain(parent, value) or str(PurePosixPath(value['root']).parent) != parent['root']:
                return False
    return True


def footprint_relation(left, right, handles, closed, tick):
    for a in left['members']:
        for b in right['members']:
            tick()
            if file_relation(handles[a], handles[b]) == 'overlap':
                return 'overlap'
    if not closed[left['id']] or not closed[right['id']]:
        return 'unknown'
    a = handles[left['root']]['before']; b = handles[right['root']]['before']
    if not same_domain(a, b):
        return 'unknown'
    if ancestor(a['root'], b['root']) or ancestor(b['root'], a['root']):
        return 'overlap'
    return 'disjoint'


def covers_footprint(writer, state, handles, closed, tick):
    """Whole modeled write coverage, stronger than sharing any single inode.

    A directory ID cannot attest its descendants. Require closed Linux trees,
    canonical root containment and every state member in the writer's tree.
    Duplicate directory observations must also enumerate identical children.
    """
    if not closed[writer['id']] or not closed[state['id']]:
        return False
    a = handles[writer['root']]['before']; b = handles[state['root']]['before']
    if (a['kind'] != 'directory' or b['kind'] != 'directory'
            or not same_domain(a, b) or not ancestor(a['root'], b['root'])):
        return False
    def key(identifier):
        return tuple(sorted(handles[identifier]['before'].items()))
    def members(footprint):
        result = {}
        for identifier in footprint['members']:
            tick()
            identity_key = key(identifier)
            if identity_key in result:
                return None
            result[identity_key] = identifier
        return result
    available = members(writer); required = members(state)
    if available is None or required is None or not required.keys() <= available.keys():
        return False
    writer_children = {key(row['directory']): {key(child) for child in row['children']}
                       for row in writer['directories']}
    for row in state['directories']:
        tick()
        if writer_children.get(key(row['directory'])) != {key(child) for child in row['children']}:
            return False
    return True


def model(request, nonce, source, now, tick):
    exact(request, {'schema_version', 'kind', 'nonce', 'source_sha256', 'session', 'observed_from',
                    'observed_until', 'deadline', 'providers', 'handles', 'footprints', 'actors', 'edges', 'states'})
    require(type(request['schema_version']) is int and request['schema_version'] == 1
            and request['kind'] == 'ha-storage-alias-model-request')
    require(sha(nonce) and request['nonce'] == nonce, 'nonce_mismatch')
    require(sha(source) and request['source_sha256'] == source, 'source_mismatch')
    require(sha(request['session']))
    start, end, deadline = (request[k] for k in ('observed_from', 'observed_until', 'deadline'))
    require(all(number(v) for v in (start, end, deadline, now))
            and start <= end <= now < deadline and deadline - start <= 2, 'deadline_exceeded')
    providers = request['providers']
    exact(providers, PROVIDERS)
    require(all(type(value) is str and value == 'complete' for value in providers.values()), 'incomplete_provider')
    handles = index(request['handles'])
    for h in handles.values():
        tick()
        exact(h, {'id', 'session', 'provider', 'opened_at', 'held_until', 'before', 'after'})
        require(h['session'] == request['session'] and h['provider'] in {'windows', 'linux'})
        require(number(h['opened_at']) and number(h['held_until'])
                and h['opened_at'] <= start and h['held_until'] >= end, 'handle_lifetime_incomplete')
        identity(h['before'], h['provider']); identity(h['after'], h['provider'])
        require(h['before'] == h['after'], 'handle_identity_changed')
    footprints = index(request['footprints']); closed = {}
    for footprint in footprints.values():
        tick()
        exact(footprint, {'id', 'root', 'members', 'directories', 'unresolved'})
        members = items(footprint['members'])
        require(all(type(v) is str for v in members) and len(members) == len(set(members)))
        unresolved = items(footprint['unresolved'])
        require(all(v in {'submount', 'hardlink', 'reparse', 'enumeration', 'missing_parent', 'cross_binding'} for v in unresolved))
        closed[footprint['id']] = closed_footprint(footprint, handles) and not unresolved
    actors = index(request['actors'])
    require(not (actors.keys() & footprints.keys()), 'duplicate_identity')
    for actor in actors.values():
        tick()
        exact(actor, {'id', 'service', 'state'})
        require(actor['service'] in set(STATE_CATALOG) | {''}
                and actor['state'] in {'active', 'enabled', 'disabled', 'unknown'})
        require(actor['state'] != 'unknown', 'unknown_actor_state')
    states = items(request['states']); covered = set(); owners = {}
    for state in states:
        tick()
        exact(state, {'service', 'category', 'footprint', 'owner', 'coverage'})
        service = state['service']; category = state['category']
        require(service in STATE_CATALOG and category in STATE_CATALOG[service])
        require((service, category) not in covered, 'duplicate_state')
        covered.add((service, category))
        require(state['footprint'] in footprints and state['owner'] in actors)
        owner = actors[state['owner']]
        require(owner['service'] == service and owner['state'] == 'active', 'owner_unavailable')
        require(owners.setdefault(service, owner['id']) == owner['id'], 'multiple_declared_owners')
        require(state['coverage'] in {'subtree', 'create_parent'})
        root = handles[footprints[state['footprint']]['root']]['before']
        require(root['kind'] == 'directory', 'state_parent_required')
        if category.endswith('_parent'):
            require(state['coverage'] == 'create_parent', 'state_parent_required')
    require(covered == {(s, c) for s, categories in STATE_CATALOG.items() for c in categories}, 'seven_service_state_coverage')
    edges = items(request['edges'], EDGE_LIMIT); outgoing = {key: [] for key in actors}
    seen = set()
    for edge in edges:
        tick()
        exact(edge, {'source', 'target', 'kind'})
        source_id, target, kind = (edge[k] for k in ('source', 'target', 'kind'))
        require(source_id in actors and kind in {'read', 'write', 'credential', 'management', 'recreate', 'unknown'})
        require(target in (footprints if kind in {'read', 'write'} else actors))
        key = source_id, target, kind
        require(key not in seen, 'duplicate_edge'); seen.add(key)
        outgoing[source_id].append(edge)
    # Availability is transitive: an active recreator can enable a disabled
    # writer. Disabled alone is insufficient if another actor can recreate it.
    potential = {key for key, a in actors.items() if a['state'] in {'active', 'enabled'}}
    pending = list(potential)
    while pending:
        tick()
        current = pending.pop()
        for edge in outgoing[current]:
            if edge['kind'] in {'management', 'credential', 'recreate', 'unknown'}:
                require(edge['kind'] != 'unknown', 'unknown_capability_edge')
                if edge['target'] not in potential:
                    potential.add(edge['target']); pending.append(edge['target'])
    relations = {'overlap': 0, 'disjoint': 0, 'unknown': 0}; owner_writes = set(); cache = {}
    for actor in potential:
        for edge in outgoing[actor]:
            if edge['kind'] not in {'read', 'write'}:
                continue
            for state in states:
                if edge['kind'] == 'read' and state['category'] not in {'oauth_state', 'security_state', 'auth_state'}:
                    continue
                tick()
                pair = tuple(sorted((edge['target'], state['footprint'])))
                if pair not in cache:
                    cache[pair] = footprint_relation(footprints[pair[0]], footprints[pair[1]], handles, closed, tick)
                relation = cache[pair]
                relations[relation] += 1
                require(relation != 'unknown', 'storage_relation_unknown')
                if relation == 'overlap':
                    require(actor == state['owner'], 'credential_read_capability' if edge['kind'] == 'read'
                            else 'competing_writer_or_recreator')
                    if (edge['kind'] == 'write' and covers_footprint(footprints[edge['target']],
                                                                   footprints[state['footprint']], handles, closed, tick)):
                        owner_writes.add((state['service'], state['category']))
    require(owner_writes == covered, 'owner_write_coverage_incomplete')
    # A non-owner with credentials/management of an owner has equivalent write
    # reachability even if that original actor has no direct mount edge.
    for origin in potential:
        reached, pending = set(), [origin]
        while pending:
            tick()
            actor = pending.pop()
            if actor in reached:
                continue
            reached.add(actor)
            pending.extend(e['target'] for e in outgoing[actor]
                           if e['kind'] in {'credential', 'management', 'recreate'})
        for owner in owners.values():
            require(owner not in reached or owner == origin, 'indirect_writer_capability')
    return relations


def evaluate(request, *, expected_nonce, expected_source_sha256, now, consumed_nonces, clock=time.monotonic):
    report = dict(schema_version=1, kind='ha-storage-alias-model-diagnostic', authority=False,
                  ready=False, supports=False, scope='supplied-model-only', status='blocked',
                  reason='invalid_schema', state_catalog='declared_minimum_only',
                  relations={'overlap': 0, 'disjoint': 0, 'unknown': 0})
    try:
        began = clock(); work = 0
        def tick():
            nonlocal work
            work += 1
            require(work <= 65536, 'analysis_limit')
            current = clock()
            require(number(began) and number(current) and current >= began, 'invalid_clock')
            require(number(now) and number(request.get('deadline'))
                    and now + current - began < request['deadline'], 'deadline_exceeded')
        require(type(consumed_nonces) is set and sha(expected_nonce))
        require(expected_nonce not in consumed_nonces, 'nonce_reused')
        require(len(consumed_nonces) < ITEM_LIMIT, 'nonce_store_full')
        consumed_nonces.add(expected_nonce)
        require(type(request) is dict)
        require(len(json.dumps(request, allow_nan=False).encode()) <= LIMIT, 'payload_limit')
        report['relations'] = model(request, expected_nonce, expected_source_sha256, now, tick)
        tick()
        report.update(status='modeled_safe', reason='none')
    except Rejected as error:
        reason = str(error)
        report['reason'] = reason if reason in REASONS else 'invalid_schema'
    except Exception:
        pass
    return report
