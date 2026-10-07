#!/usr/bin/env python3
"""Build an exact Git revision, scan the final image, and emit immutable evidence.

Only tracked Dockerfile/requirements/app assets enter the isolated context.
No push, import, deployment, or working-tree mutation is performed.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

APPS = {'portal-web', 'book-memo', 'youtube-memo', 'crawler-worker', 'homeops-executor', 'system-agent', 'car-care-worker'}


def build_context(repo, app, revision, destination):
    if app not in APPS or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('exact_revision_required')
    raw = subprocess.run(['git', '-C', str(repo), 'archive', revision, '--', app], check=True, capture_output=True).stdout
    count = 0
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            path = Path(member.name)
            relative = path.relative_to(app)
            if not relative.parts or relative.parts[0] not in {'Dockerfile', 'requirements.txt', 'app'}:
                continue
            if '..' in relative.parts or any(p.startswith('.') for p in relative.parts) or member.issym() or member.islnk():
                raise ValueError('unsafe_context_member')
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source:
                    target.write_bytes(source.read())
                target.chmod(member.mode & 0o777)
                count += 1
            else:
                raise ValueError('unsafe_context_member')
    if not (destination/'Dockerfile').is_file() or not (destination/'requirements.txt').is_file() or not count:
        raise ValueError('incomplete_build_context')


def publish_archive(archive, output, evidence):
    sidecar = output.with_suffix(output.suffix+'.json')
    if output.exists() or sidecar.exists():
        raise FileExistsError('artifact_exists')
    staged = []
    published = []
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as data:
            staged.append(Path(data.name))
            digest = hashlib.sha256()
            with archive.open('rb') as source:
                while chunk := source.read(1024*1024):
                    digest.update(chunk)
                    data.write(chunk)
            data.flush()
            os.fsync(data.fileno())
        evidence = {**evidence, 'archive_sha256': digest.hexdigest()}
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as meta:
            staged.append(Path(meta.name))
            meta.write((json.dumps(evidence, indent=2)+'\n').encode())
            meta.flush()
            os.fsync(meta.fileno())
        # Exclusive hardlinks publish complete files; concurrent existing files
        # are never replaced. On failure remove only files this call published.
        os.link(staged[0], output)
        published.append(output)
        os.link(staged[1], sidecar)
        published.append(sidecar)
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', choices=sorted(APPS), required=True)
    parser.add_argument('--revision', required=True, help='Full tested Git commit SHA')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(args.output.suffix+".json").exists():
        parser.exit(1, 'verified_image=FAIL: output_exists\n')
    repo = Path(__file__).resolve().parents[3]
    image = f'personal-server-{args.app}:{args.revision}'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Build/scan in temporary storage. Failed scans never leave an artifact
    # carrying a successful verification sidecar.
    with tempfile.TemporaryDirectory(prefix='verified-image-') as temp:
        context = Path(temp)/'context'
        context.mkdir()
        build_context(repo, args.app, args.revision, context)
        archive = Path(temp)/'image.tar'
        subprocess.run(['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--provenance=false', '--label', 'org.opencontainers.image.revision='+args.revision, '--tag', image, '--output', 'type=oci,dest='+str(archive)+',annotation-manifest-descriptor.io.personal-server.image-ref='+image, str(context)], check=True)
        subprocess.run(['trivy', 'image', '--input', str(archive), '--scanners', 'vuln', '--severity', 'HIGH,CRITICAL', '--exit-code', '1'], check=True)
        evidence = {'revision': args.revision, 'image': image, 'platform': 'linux/amd64', 'scan': 'passed', 'scope': 'final image OS and library vulnerabilities', 'deployment': 'not_performed'}
        publish_archive(archive, args.output, evidence)
        print('verified_image=PASS')


if __name__ == '__main__':
    main()
