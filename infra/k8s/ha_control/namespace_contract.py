"""Offline receipt verifier. Runtime namespace observations remain RAW-exact."""
import base64
import hashlib
import json
import struct

PROFILE = 'strict-canonical-observed-v1'
COMPARATOR_SHA256 = '56cc20aaf52bd7ea7b1b0f88d562a15d1f28ffb3aac7ad4b0e0df2ec3e1dac3e'
OBSERVED_VERSION = 'direct-children-observed-v1'
OBSERVED_FIELDS = {'descriptor_profile', 'comparator_sha256', 'apply_receipt_sha256', 'plan_sha256', 'service_identity_sha256'}


def need(value):
    if not value:
        raise ValueError('namespace_observed_contract_rejected')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(raw):
    """Same narrow grammar as pinned C#; preserve control/header/ordered ACEs."""
    need(type(raw) is bytes and 20 <= len(raw) <= 65536 and raw[0] == 1)
    u16 = lambda at: struct.unpack_from('<H', raw, at)[0]
    control = u16(2)
    need(control & 0x8004 == 0x8004)
    offsets = struct.unpack_from('<4I', raw, 4)
    need(offsets[0] and offsets[1] and offsets[3] and (control & 16 or not offsets[2]))
    used = set(range(20))
    parts = []

    def sid_length(at, limit):
        need(0 <= at <= limit - 8 and raw[at] == 1 and raw[at + 1] <= 15)
        size = 8 + 4 * raw[at + 1]
        need(at <= limit - size)
        return size

    for k, off in enumerate(offsets):
        if not off:
            parts.append(None)
            continue
        need(20 <= off < len(raw) and off % 4 == 0)
        if k < 2:
            size = sid_length(off, len(raw))
            compact = raw[off:off + size]
        else:
            need(off <= len(raw) - 8 and raw[off] in (2, 4))
            size = u16(off + 2)
            need(size >= 8 and size % 4 == 0 and off <= len(raw) - size)
            count = u16(off + 4)
            need(count <= 128 and (k != 3 or count > 0))
            at, limit = off + 8, off + size
            for _ in range(count):
                need(at <= limit - 8)
                n = u16(at + 2)
                need(n >= 16 and n % 4 == 0 and at <= limit - n)
                need(raw[at] in (0, 1) if k == 3 else raw[at] == 2)
                need(n == 8 + sid_length(at + 8, at + n))
                at += n
            need((limit - at) % 4 == 0 and not any(raw[at:limit]))
            compact = bytearray(raw[off:at])
            struct.pack_into('<H', compact, 2, len(compact))
        span = set(range(off, off + size))
        need(not used.intersection(span))
        used.update(span)
        parts.append(compact)
    need(all(raw[i] == 0 for i in range(20, len(raw)) if i not in used))
    result = bytearray(raw[:20])
    for k, part in enumerate(parts):
        if part is None:
            struct.pack_into('<I', result, 4 + k * 4, 0)
        else:
            result += b'\0' * (-len(result) % 4)
            struct.pack_into('<I', result, 4 + k * 4, len(result))
            result += part
    return bytes(result)


def verify_apply_receipt(receipt_raw, journal_raw, plan_raw, closure, *, commit, installation, service_sid,
                         driver_policy_sha256):
    """Only completed, exact source/plan/service-bound apply receipts may seal runtime."""
    # Caller uses its duplicate-key rejecting decoder before invoking this API.
    def decode(raw):
        need(type(raw) is bytes and 1 <= len(raw) <= 3145728)
        def pairs(items):
            value = {}
            for k, v in items:
                need(k not in value)
                value[k] = v
            return value
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=lambda _: need(False))

    r = decode(receipt_raw)
    p = decode(plan_raw)
    fields = {'schema_version','format','source_commit','script_sha256','installation_id','plan_sha256',
              'descriptor_profile','comparator_sha256','service_identity_sha256','service_config_sha256',
              'service_sd_sha256','service_ownership_receipt_sha256','parent_sd_sha256','closure_names',
              'children_sha256','classification_sha256','original_sd_sha256','original_canonical_sha256',
              'requested_sd_sha256','requested_canonical_sha256','observed_sd_sha256','observed_canonical_sha256',
              'original_sd_base64','requested_sd_base64','observed_sd_base64','journal_sha256',
              'complete','atomic_tree_change','manual_reconcile_on_partial'}
    need(type(r) is dict and set(r) == fields and type(r['schema_version']) is int and r['schema_version'] == 3
         and r['format'] == 'ha-namespace-closure-apply-observed-v1' and r['complete'] is True
         and r['atomic_tree_change'] is False and r['manual_reconcile_on_partial'] is True)
    need(sha(receipt_raw) == closure['apply_receipt_sha256'] and sha(plan_raw) == closure['plan_sha256']
         and r['plan_sha256'] == sha(plan_raw) and sha(journal_raw) == r['journal_sha256'])
    need(r['source_commit'] == commit and r['installation_id'] == installation
         and r['script_sha256'] == driver_policy_sha256
         and r['service_identity_sha256'] == sha(service_sid.encode()) == closure['service_identity_sha256'])
    need(type(p) is dict and type(p.get('schema_version')) is int and p['schema_version'] == 3
         and p['format'] == 'ha-namespace-policy-observed-v1' and 'runtime_namespace_closure' not in p)
    for key in ('source_commit','script_sha256','installation_id','descriptor_profile','comparator_sha256',
                'service_identity_sha256','service_config_sha256','service_sd_sha256','service_ownership_receipt_sha256','parent_sd_sha256'):
        need(r[key] == p[key])
    need(r['descriptor_profile'] == PROFILE and r['comparator_sha256'] == COMPARATOR_SHA256)
    for target, source in (('closure_names','closure_names'),('children_sha256','closure_children_sha256'),
                           ('classification_sha256','closure_classification_sha256'),
                           ('original_sd_sha256','closure_original_sd_sha256'),
                           ('original_canonical_sha256','closure_original_canonical_sha256'),
                           ('requested_sd_sha256','closure_target_sd_sha256'),
                           ('requested_canonical_sha256','closure_target_canonical_sha256')):
        need(r[target] == p[source])
    need(r['closure_names'] == closure['names'] and r['children_sha256'] == closure['children_sha256']
         and r['classification_sha256'] == closure['classification_sha256'] and r['observed_sd_sha256'] == closure['sd_sha256'])
    n = len(closure['names'])
    need(2 <= n <= 9)
    for key in ('original_sd_sha256','original_canonical_sha256','requested_sd_sha256','requested_canonical_sha256',
                'observed_sd_sha256','observed_canonical_sha256','original_sd_base64','requested_sd_base64','observed_sd_base64'):
        need(type(r[key]) is list and len(r[key]) == n)
    for i in range(n):
        for kind in ('original','requested','observed'):
            encoded = r[kind + '_sd_base64'][i]
            need(type(encoded) is str and len(encoded) <= 87384)
            raw = base64.b64decode(encoded, validate=True)
            need(sha(raw) == r[kind + '_sd_sha256'][i]
                 and sha(canonical(raw)) == r[kind + '_canonical_sha256'][i])
        need(r['requested_canonical_sha256'][i] == r['observed_canonical_sha256'][i])
    events = [decode(line) for line in journal_raw.splitlines()]
    need(all(type(v) is dict and type(v.get('sequence')) is int
             and ('index' not in v or type(v['index']) is int) for v in events))
    need(len(events) == 1 + 3 * n and [v['sequence'] for v in events] == list(range(1, len(events) + 1)))
    intent = events[0]
    need(intent == dict(sequence=1,event='namespace_closure_intent',mode='apply',plan_sha256=r['plan_sha256'],
                        source_commit=commit,descriptor_profile=PROFILE,comparator_sha256=COMPARATOR_SHA256,
                        atomic_tree_change=False,manual_reconcile_on_partial=True))
    for step, i in enumerate(list(range(1, n)) + [0]):
        before, returned, readback = events[1 + step * 3:4 + step * 3]
        need(before == dict(sequence=2+step*3,event='namespace_closure_set_intent',index=i,
                            before_sd_sha256=r['original_sd_sha256'][i],requested_sd_sha256=r['requested_sd_sha256'][i],
                            requested_canonical_sha256=r['requested_canonical_sha256'][i]))
        need(returned == dict(sequence=3+step*3,event='namespace_closure_set_return',index=i,return_value=0)
             and type(returned['return_value']) is int)
        need(readback == dict(sequence=4+step*3,event='namespace_closure_observed_readback',index=i,
                              observed_sd_sha256=r['observed_sd_sha256'][i],observed_canonical_sha256=r['observed_canonical_sha256'][i],
                              full_sacl_verified=True) and readback['full_sacl_verified'] is True)
    return r
