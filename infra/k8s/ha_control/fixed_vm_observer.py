"""Verify a fixed Windows VM observer response before consuming its snapshot."""
import base64
import hashlib
import hmac
import json
import re
from uuid import UUID


def _object(raw):
    def unique(items):
        values = dict(items)
        if len(values) != len(items):
            raise ValueError("fixed_observer_duplicate_field")
        return values

    return json.loads(raw, object_pairs_hook=unique)


def verify_snapshot(frame, key, nonce, expected_ids):
    """Bind a bounded response to the private session key, request and two VMs."""
    if (type(frame) is not bytes or not 0 < len(frame) <= 4096
            or not frame.endswith(b"\n") or type(key) is not bytes
            or re.fullmatch(rb"[a-f0-9]{64}", key) is None
            or type(nonce) is not str or re.fullmatch(r"[a-f0-9]{32}", nonce) is None):
        raise ValueError("fixed_observer_frame")

    envelope = _object(frame)
    if (type(envelope) is not dict or set(envelope) != {"body", "mac"}
            or type(envelope["body"]) is not str or type(envelope["mac"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", envelope["mac"]) is None):
        raise ValueError("fixed_observer_envelope")
    try:
        body = base64.b64decode(envelope["body"], validate=True)
    except ValueError as error:
        raise ValueError("fixed_observer_body") from error
    if not hmac.compare_digest(envelope["mac"], hmac.digest(key, body, hashlib.sha256).hex()):
        raise ValueError("fixed_observer_authentication")

    payload = _object(body)
    if (type(payload) is not dict or set(payload) != {"nonce", "vms"} or payload["nonce"] != nonce
            or type(payload["vms"]) is not list or len(payload["vms"]) != 2
            or type(expected_ids) is not dict or set(expected_ids) != {"A", "B"}
            or any(type(vm_id) is not str for vm_id in expected_ids.values())):
        raise ValueError("fixed_observer_challenge")
    ids = {node: UUID(vm_id) for node, vm_id in expected_ids.items()}
    if ids["A"] == ids["B"] or any(vm_id.int == 0 for vm_id in ids.values()):
        raise ValueError("fixed_observer_target_ids")
    for node, vm in zip(("A", "B"), payload["vms"]):
        if (type(vm) is not dict or set(vm) != {"node", "id", "state", "disk_paths_sha256"}
                or vm["node"] != node or type(vm["id"]) is not str
                or vm["id"] != str(ids[node])
                or vm["state"] not in ("Off", "Running")
                or type(vm["disk_paths_sha256"]) is not str
                or re.fullmatch(r"[a-f0-9]{64}", vm["disk_paths_sha256"]) is None):
            raise ValueError("fixed_observer_vm_binding")
    return payload["vms"]
