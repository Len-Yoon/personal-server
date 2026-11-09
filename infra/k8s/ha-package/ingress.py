#!/usr/bin/env python3
"""Exact-commit ingress image builder. No host installation or route activation."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import uuid

_spec=importlib.util.spec_from_file_location('ha_app_package',Path(__file__).with_name('build.py'))
P=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(P)
SOURCES=('caddy/Dockerfile','infra/k8s/ha-package/Ingress.Dockerfile.template')
OWNER='personal-server.ha-ingress-package'


def recipe(files):
    bases=re.findall(r'^FROM (caddy:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64})$',files[SOURCES[0]].decode(),re.M)
    expected=('FROM @CADDY_BASE_IMAGE@\n'
        'RUN setcap -r /usr/bin/caddy && test -z "$(getcap /usr/bin/caddy)"\n'
        'USER 10004:10004\nENTRYPOINT ["/usr/bin/caddy"]\n'
        'CMD ["run", "--config", "/etc/ha-ingress/Caddyfile", "--adapter", "caddyfile"]\n')
    if len(bases)!=1 or files[SOURCES[1]].decode()!=expected:
        raise P.PackageError('invalid_ingress_template')
    return expected.replace('@CADDY_BASE_IMAGE@',bases[0]),bases[0]


def snapshot(commit):
    if not re.fullmatch('[0-9a-f]{40}',commit) or P.git('rev-parse',commit+'^{commit}').decode().strip()!=commit:
        raise P.PackageError('full_source_commit_required')
    files={}
    for source in SOURCES:
        entry=P.git('ls-tree','-z',commit,'--',source).split(b'\0')[0]
        if not entry:raise P.PackageError('ingress_source_missing')
        meta,name=entry.split(b'\t');mode,kind,oid=meta.decode().split()
        if mode not in {'100644','100755'} or kind!='blob' or name.decode()!=source:
            raise P.PackageError('regular_committed_source_required')
        files[source]=P.git('cat-file','blob',oid)
    return files


def prepare(commit,output):
    files=snapshot(commit);rendered,base=recipe(files);output=Path(output)
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    manifest={'source_commit':commit,'base':base,'source_files':{name:P.sha(raw) for name,raw in files.items()},
              'dockerfile_sha256':P.sha(rendered.encode()),'production_deployed':False}
    manifest['release_id']=P.sha(P.encode(manifest))
    (output/'Dockerfile').write_text(rendered);(output/'source-manifest.json').write_bytes(P.encode(manifest))
    for path in output.iterdir():path.chmod(0o444)
    return manifest


def verify_source(output):
    output=Path(output);manifest=json.loads((output/'source-manifest.json').read_bytes())
    files=snapshot(manifest['source_commit']);rendered,base=recipe(files)
    expected={'source_commit':manifest['source_commit'],'base':base,
        'source_files':{name:P.sha(raw) for name,raw in files.items()},
        'dockerfile_sha256':P.sha(rendered.encode()),'production_deployed':False}
    expected['release_id']=P.sha(P.encode(expected))
    if manifest!=expected or (output/'Dockerfile').read_bytes()!=rendered.encode():
        raise P.PackageError('ingress_source_binding_mismatch')
    return manifest


def build(output,context):
    output=Path(output);manifest=verify_source(output)
    if (output/'image.tar').exists() or (output/'image-bindings.json').exists():raise P.PackageError('new_build_required')
    docker=P.LocalDocker(context);owner=str(uuid.uuid4());tag='n100-ha-ingress:'+manifest['release_id']
    docker.call('build','--platform','linux/amd64','--label',OWNER+'='+owner,'--label',
        'org.opencontainers.image.revision='+manifest['source_commit'],'--label',
        'personal-server.ha-release='+manifest['release_id'],'-t',tag,str(output),timeout=300)
    image=json.loads(docker.call('image','inspect',tag))[0];config=image['Config']
    if (image['Architecture']!='amd64' or image['Os']!='linux' or config['User']!='10004:10004'
            or config['Entrypoint']!=['/usr/bin/caddy'] or config['Labels'].get(OWNER)!=owner
            or config['Labels'].get('org.opencontainers.image.revision')!=manifest['source_commit']
            or config['Labels'].get('personal-server.ha-release')!=manifest['release_id']):
        raise P.PackageError('ingress_image_binding_mismatch')
    common=['run','--rm','--pull=never','--network=none','--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges:true','--label',OWNER+'='+owner]
    version=docker.call(*common,image['Id'],'version')
    caps=docker.call(*common,'--entrypoint','/usr/sbin/getcap',image['Id'],'/usr/bin/caddy')
    if not version.startswith('v'+manifest['base'].split(':')[1].split('@')[0]) or caps:
        raise P.PackageError('ingress_unprivileged_smoke_failed')
    docker.call('image','save','--output',str(output/'image.tar'),image['Id'],timeout=180)
    receipt=P.archive_binding(output/'image.tar',image['Id'])
    receipt.update(source_commit=manifest['source_commit'],release_id=manifest['release_id'],owner=owner,
        tag=tag,smoke='PASS',file_capabilities='',user='10004:10004',version=version,
        production_deployed=False,local_runtime_context=context)
    (output/'image-bindings.json').write_bytes(P.encode(receipt));(output/'image-bindings.json').chmod(0o444)
    (output/'image.tar').chmod(0o444)
    return receipt


def verify(output):
    output=Path(output);manifest=verify_source(output);receipt=json.loads((output/'image-bindings.json').read_bytes())
    binding=P.archive_binding(output/'image.tar',receipt['image_config_digest'])
    if (receipt['source_commit']!=manifest['source_commit'] or receipt['release_id']!=manifest['release_id']
            or any(receipt.get(key)!=value for key,value in binding.items())):
        raise P.PackageError('ingress_archive_binding_mismatch')
    import tarfile
    with tarfile.open(output/'image.tar') as archive:
        index=json.load(archive.extractfile('manifest.json'))[0]
        config=json.load(archive.extractfile(index['Config']))['config']
    labels=config.get('Labels',{})
    if (config.get('User')!='10004:10004' or config.get('Entrypoint')!=['/usr/bin/caddy']
            or labels.get(OWNER)!=receipt['owner']
            or labels.get('org.opencontainers.image.revision')!=manifest['source_commit']
            or labels.get('personal-server.ha-release')!=manifest['release_id']):
        raise P.PackageError('ingress_archive_config_mismatch')
    return {'source_commit':manifest['source_commit'],'verified':True,'production_deployed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','build','verify'])
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--commit');parser.add_argument('--docker-context',default='orbstack')
    args=parser.parse_args()
    result=prepare(args.commit,args.output) if args.command=='prepare' else build(args.output,args.docker_context) if args.command=='build' else verify(args.output)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
