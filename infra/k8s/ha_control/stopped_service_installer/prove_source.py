"""Emit a new equality receipt; never relabel the original compilation receipt."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


class SourceProofError(ValueError):
    pass


def require(condition):
    if not condition:
        raise SourceProofError('source_equivalence_rejected')


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def blob_hash(commit, path):
    entry = subprocess.check_output(['git', 'ls-tree', '-z', commit, '--', path])
    require(entry.endswith(b'\0') and entry.count(b'\0') == 1)
    fields = entry[:-1].split(b'\t')
    require(len(fields) == 2 and fields[1] == path.encode())
    metadata = fields[0].split()
    require(len(metadata) == 3 and metadata[:2] == [b'100644', b'blob'])
    raw = subprocess.check_output(['git', 'cat-file', 'blob', metadata[2]])
    return hashlib.sha256(raw).hexdigest()


def build_proof(selected, receipt_bytes):
    require(isinstance(receipt_bytes, bytes) and len(receipt_bytes) <= 65536)
    receipt = json.loads(receipt_bytes, object_pairs_hook=pairs,
                         parse_constant=lambda _: require(False))
    require(type(receipt) is dict)
    origin = receipt.get('source_commit')
    require(all(type(value) is str and re.fullmatch('[0-9a-f]{40}', value)
                for value in (selected, origin)))
    path = 'infra/k8s/ha_control/windows-service-host.cs'
    selected_hash, origin_hash = blob_hash(selected, path), blob_hash(origin, path)
    require(selected_hash == origin_hash == receipt.get('source_sha256'))
    pins = {'compiler': receipt['compiler'], 'references': receipt['references']}
    pins_hash = hashlib.sha256((json.dumps(pins, sort_keys=True, indent=2) + '\n').encode()).hexdigest()
    require(pins_hash == receipt.get('pins_sha256'))
    return {
        'format': 'source-equivalence-v1',
        'source_commit': selected,
        'build_origin_commit': origin,
        'host_source_sha256': selected_hash,
        'origin_host_source_sha256': origin_hash,
        'original_build_receipt_sha256': hashlib.sha256(receipt_bytes).hexdigest(),
        'original_receipt_relabelled': False,
        'compiler_reference_pins_sha256': pins_hash,
        'preflight_sha256': blob_hash(selected, 'infra/k8s/ha_control/windows-service-preflight.ps1'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--selected', required=True)
    parser.add_argument('--build-receipt', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    # Parse and bind the SAME single read. Never reopen the mutable receipt for hashing.
    receipt_bytes = Path(args.build_receipt).read_bytes()
    proof = build_proof(args.selected, receipt_bytes)
    with Path(args.output).open('x') as stream:
        json.dump(proof, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    try:
        main()
    except (SourceProofError, ValueError, KeyError, OSError, subprocess.CalledProcessError):
        raise SystemExit('source_equivalence_rejected') from None
