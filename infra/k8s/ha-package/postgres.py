#!/usr/bin/env python3
"""Exact-commit nonroot PostgreSQL package; never accesses an operational host."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import re
import tarfile
import uuid

_spec = importlib.util.spec_from_file_location('ha_app_package', Path(__file__).with_name('build.py'))
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
SOURCES = ('infra/k8s/ha-package/Postgres.Dockerfile.template',
           'infra/k8s/ha-package/postgres.py', 'infra/k8s/ha-package/build.py')
OWNER = 'personal-server.ha-postgres-package'
BASE = 'postgres:17.11-alpine3.24@sha256:4717f90960a225c2d1c5dac50508c401230f693ff626e06e84e6db0a68ded508'
RECIPE = ('FROM ' + BASE + "\nRUN apk add --no-cache --upgrade 'krb5-libs>=1.22.3-r0' 'libxslt>=1.1.43-r4' \\\n"
          '    && rm /usr/local/bin/gosu\nUSER 70:70\n')


def snapshot(commit):
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit) or P.git('rev-parse', commit+'^{commit}').decode().strip() != commit:
        raise P.PackageError('full_source_commit_required')
    files = {}
    for source in SOURCES:
        entry = P.git('ls-tree', '-z', commit, '--', source).split(b'\0')[0]
        if not entry:
            raise P.PackageError('postgres_source_missing')
        meta, name = entry.split(b'\t')
        mode, kind, oid = meta.decode().split()
        if mode not in {'100644', '100755'} or kind != 'blob' or name.decode() != source:
            raise P.PackageError('regular_committed_source_required')
        files[source] = P.git('cat-file', 'blob', oid)
    return files


def recipe(files):
    if files[SOURCES[0]] != RECIPE.encode():
        raise P.PackageError('invalid_postgres_template')
    return RECIPE


def source_manifest(commit, files):
    rendered = recipe(files)
    manifest = {'source_commit': commit, 'base': BASE,
                'source_files': {name: P.sha(raw) for name, raw in files.items()},
                'dockerfile_sha256': P.sha(rendered.encode()), 'production_deployed': False}
    manifest['release_id'] = P.sha(P.encode(manifest))
    return manifest


def prepare(commit, output):
    files = snapshot(commit)
    manifest = source_manifest(commit, files)
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output/'Dockerfile').write_text(recipe(files))
    (output/'source-manifest.json').write_bytes(P.encode(manifest))
    for path in output.iterdir():
        path.chmod(0o444)
    return manifest


def verify_source(output):
    output = Path(output)
    manifest = json.loads((output/'source-manifest.json').read_bytes())
    files = snapshot(manifest['source_commit'])
    if manifest != source_manifest(manifest['source_commit'], files) or (output/'Dockerfile').read_bytes() != recipe(files).encode():
        raise P.PackageError('postgres_source_binding_mismatch')
    return manifest


def check_config(config, owner, manifest):
    labels = config.get('Labels', {})
    env = set(config.get('Env', []))
    if (config.get('User') != '70:70' or config.get('Entrypoint') != ['docker-entrypoint.sh']
            or config.get('Cmd') != ['postgres'] or 'PG_MAJOR=17' not in env or 'PG_VERSION=17.11' not in env
            or labels.get(OWNER) != owner
            or labels.get('org.opencontainers.image.revision') != manifest['source_commit']
            or labels.get('personal-server.ha-release') != manifest['release_id']):
        raise P.PackageError('postgres_config_binding_mismatch')


def build(output, context):
    output = Path(output)
    manifest = verify_source(output)
    if (output/'image.tar').exists() or (output/'image-bindings.json').exists():
        raise P.PackageError('new_build_required')
    docker = P.LocalDocker(context)
    owner = str(uuid.uuid4())
    tag = 'n100-ha-postgres:'+manifest['release_id']
    docker.call('build', '--platform', 'linux/amd64', '--label', OWNER+'='+owner,
                '--label', 'org.opencontainers.image.revision='+manifest['source_commit'],
                '--label', 'personal-server.ha-release='+manifest['release_id'], '-t', tag, str(output), timeout=300)
    image = json.loads(docker.call('image', 'inspect', tag))[0]
    if image.get('Architecture') != 'amd64' or image.get('Os') != 'linux':
        raise P.PackageError('postgres_platform_mismatch')
    check_config(image['Config'], owner, manifest)
    common = ['run', '--rm', '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
              '--security-opt=no-new-privileges:true', '--label', OWNER+'='+owner]
    smoke = docker.call(*common, '--entrypoint', 'sh', image['Id'], '-ec',
        'test "$(id -u)" = 70; test "$(id -g)" = 70; test ! -e /usr/local/bin/gosu; '
        "apk info -e 'krb5-libs>=1.22.3-r0' 'libxslt>=1.1.43-r4' >/dev/null; "
        'test -f /usr/local/share/postgresql/extension/pgcrypto.control; postgres --version')
    if smoke != 'postgres (PostgreSQL) 17.11':
        raise P.PackageError('postgres_unprivileged_smoke_failed')
    # A private tmpfs cluster verifies pgcrypto can actually load; no host mount or exposed port.
    sql = docker.call(*common, '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
        '--entrypoint', 'sh', image['Id'], '-ec',
        'initdb -D /tmp/pg --auth-local=trust --auth-host=reject >/dev/null; '
        'pg_ctl -D /tmp/pg -l /tmp/pg.log -o "-k /tmp -c listen_addresses=\'\'" -w start >/dev/null; '
        'trap "pg_ctl -D /tmp/pg -m immediate stop >/dev/null" EXIT; '
        'psql -h /tmp -U postgres -d postgres -v ON_ERROR_STOP=1 -Atc '
        '"CREATE EXTENSION pgcrypto; SELECT encode(digest(\'ha-postgres-smoke\',\'sha256\'),\'hex\');"', timeout=180)
    expected = P.sha(b'ha-postgres-smoke')
    if sql.splitlines() != ['CREATE EXTENSION', expected]:
        raise P.PackageError('postgres_pgcrypto_smoke_failed')
    docker.call('image', 'save', '--output', str(output/'image.tar'), image['Id'], timeout=180)
    receipt = P.archive_binding(output/'image.tar', image['Id'])
    receipt.update(source_commit=manifest['source_commit'], release_id=manifest['release_id'], owner=owner,
                   tag=tag, user='70:70', version=smoke, pgcrypto_sha256=expected,
                   gosu_removed=True, smoke='PASS', production_deployed=False, local_runtime_context=context)
    (output/'image-bindings.json').write_bytes(P.encode(receipt))
    (output/'image-bindings.json').chmod(0o444)
    (output/'image.tar').chmod(0o444)
    verify(output)
    return receipt


def verify(output):
    output = Path(output)
    manifest = verify_source(output)
    receipt = json.loads((output/'image-bindings.json').read_bytes())
    binding = P.archive_binding(output/'image.tar', receipt['image_config_digest'])
    if (receipt.get('source_commit') != manifest['source_commit'] or receipt.get('release_id') != manifest['release_id']
            or any(receipt.get(key) != value for key, value in binding.items())
            or receipt.get('smoke') != 'PASS' or receipt.get('pgcrypto_sha256') != P.sha(b'ha-postgres-smoke')
            or receipt.get('user') != '70:70' or receipt.get('version') != 'postgres (PostgreSQL) 17.11'
            or receipt.get('gosu_removed') is not True):
        raise P.PackageError('postgres_archive_binding_mismatch')
    gosu = False
    removed = False
    with tarfile.open(output/'image.tar') as archive:
        index = json.load(archive.extractfile('manifest.json'))[0]
        config = json.load(archive.extractfile(index['Config']))
        if config.get('architecture') != 'amd64' or config.get('os') != 'linux':
            raise P.PackageError('postgres_archive_platform_mismatch')
        check_config(config['config'], receipt['owner'], manifest)
        for layer in index['Layers']:
            with tarfile.open(fileobj=io.BytesIO(archive.extractfile(layer).read())) as contents:
                for member in contents:
                    name = member.name.removeprefix('./').rstrip('/')
                    if name == 'usr/local/bin/gosu':
                        gosu = True
                    elif name == 'usr/local/bin/.wh.gosu':
                        gosu = False
                        removed = True
    if gosu or not removed:
        raise P.PackageError('postgres_gosu_removal_binding_mismatch')
    return {'source_commit': manifest['source_commit'], 'verified': True, 'production_deployed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'build', 'verify'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--commit')
    parser.add_argument('--docker-context', default='orbstack')
    args = parser.parse_args()
    result = prepare(args.commit, args.output) if args.command == 'prepare' else build(args.output, args.docker_context) if args.command == 'build' else verify(args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
