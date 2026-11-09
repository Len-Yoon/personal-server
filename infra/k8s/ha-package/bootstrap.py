#!/usr/bin/env python3
"""Exact-input offline Ubuntu/Docker/closed PostgreSQL bootstrap bundle.

prepare/verify are inert. install requires a fresh, explicitly identified guest
and never creates a VM, reads production credentials, or opens writer authority.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
HEX = re.compile(r'[0-9a-f]{64}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
NAME = re.compile(r'[a-z][a-z0-9_.-]{0,63}\Z')
DEBS = {'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'}
ASSETS = ('install.sh', 'pg-entrypoint.sh', 'initialize.py')


class BootstrapError(RuntimeError):
    pass


def need(condition, reason='bootstrap_input_invalid'):
    if not condition:
        raise BootstrapError(reason)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def regular(path):
    path = Path(path)
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not path.is_symlink()
         and not any(p.is_symlink() for p in path.parents), 'regular_input_required')
    return path


def file_record(value, suffix):
    need(type(value) is dict and type(value.get('filename')) is str
         and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+%~-]{0,199}' + re.escape(suffix), value['filename'])
         and type(value.get('sha256')) is str and HEX.fullmatch(value['sha256']))


def source_url(value, hosts):
    need(type(value) is str)
    url = urlsplit(value)
    need(url.scheme == 'https' and url.hostname in hosts and not url.username and not url.password
         and not url.query and not url.fragment and url.port is None and '/latest/' not in url.path)
    return url


def direction(spec):
    node = spec.get('node')
    need(node in ('A','B'))
    if spec.get('schema_version',1) == 1:
        return 'A', None if node=='A' else 'A'
    need(spec.get('schema_version') == 2 and spec.get('primary_node') in ('A','B'))
    primary = spec['primary_node']; source = spec.get('source_node')
    need('source_node' in spec and source == (None if node==primary else primary),'invalid_clone_direction')
    return primary, source


def validate(lock):
    need(type(lock) is dict and type(lock.get('schema_version')) is int and lock['schema_version'] in (1,2))
    fields = {'schema_version','source_commit','installation_id','node','pg_a','pg_b','cluster','plan_sha256',
              'ubuntu_image','docker_packages','postgres','utility_config_digest'}
    need(set(lock) == fields | ({'primary_node','source_node','os_packages'} if lock['schema_version']==2 else set()))
    direction(lock)
    need(type(lock['source_commit']) is str and re.fullmatch(r'[0-9a-f]{40}',lock['source_commit']))
    need(all(type(lock[k]) is str and HEX.fullmatch(lock[k]) for k in ('installation_id','plan_sha256')))
    need(lock['node'] in ('A','B') and type(lock['cluster']) is str and NAME.fullmatch(lock['cluster']))
    for key in ('pg_a','pg_b'):
        need(type(lock[key]) is str)
        try:
            ip = ipaddress.ip_address(lock[key])
        except ValueError:
            raise BootstrapError('bootstrap_input_invalid') from None
        need(ip.version == 4 and ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved))
    need(lock['pg_a'] != lock['pg_b'])
    os_image = lock['ubuntu_image']
    file_record(os_image, '.img')
    need(set(os_image) == {'filename','sha256','source_url','format'} and os_image['format'] == 'qcow2')
    url = source_url(os_image['source_url'], {'cloud-images.ubuntu.com'})
    need(re.fullmatch(r'/releases/noble/release-[0-9]{8}/ubuntu-24\.04-server-cloudimg-amd64\.img',url.path))
    docker = lock['docker_packages']
    need(type(docker) is list and 5 <= len(docker) <= 128)
    operating_system = lock.get('os_packages',[])
    need(type(operating_system) is list and len(operating_system) <= 1024)
    names,files = {}, {os_image['filename']}
    for group,values in (('docker',docker),('os',operating_system)):
        seen=set()
        for item in values:
            file_record(item,'.deb')
            need(set(item) == {'filename','sha256','package','version','architecture','source_url'})
            need(type(item['package']) is str and re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,79}',item['package']))
            need(type(item['version']) is str and re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,120}',item['version']))
            need(item['architecture'] in ('amd64','all') and item['package'] not in seen)
            hosts={'archive.ubuntu.com','security.ubuntu.com'} | ({'download.docker.com'} if group=='docker' else set())
            url=source_url(item['source_url'],hosts)
            need(url.path.endswith('/'+item['filename']))
            if item['package'] in DEBS:
                need(group=='docker' and url.hostname=='download.docker.com' and '/ubuntu/' in url.path and '/noble/' in url.path)
            prior=names.get(item['package'])
            if prior is not None:
                need(all(item[k]==prior[k] for k in ('filename','sha256','version','architecture')),'package_closure_conflict')
            else:
                need(item['filename'] not in files,'artifact_filename_conflict')
                names[item['package']]=item;files.add(item['filename'])
            seen.add(item['package'])
    need(DEBS <= {item['package'] for item in docker})
    postgres = lock['postgres']
    file_record(postgres,'.tar')
    need(set(postgres) == {'filename','sha256','config_digest','uid','gid'} and postgres['filename'] not in files
         and type(postgres['config_digest']) is str and DIGEST.fullmatch(postgres['config_digest']))
    need(type(postgres['uid']) is int and type(postgres['gid']) is int and (postgres['uid'],postgres['gid']) in {(70,70),(999,999)})
    need(type(lock['utility_config_digest']) is str and DIGEST.fullmatch(lock['utility_config_digest']))
    return lock


def packages(lock):
    """Validated OS/Engine union: exact duplicates install once, conflicts reject."""
    result = {}
    for item in [*lock.get('os_packages',[]),*lock['docker_packages']]:
        prior=result.get(item['package'])
        need(prior is None or all(item[k]==prior[k] for k in ('filename','sha256','version','architecture')),
             'package_closure_conflict')
        result[item['package']]=item
    return list(result.values())


def archive(path, expected):
    """Validate an unextracted classic archive, including architecture and layers."""
    with tarfile.open(regular(path)) as reader:
        members = reader.getmembers()
        need(all((m.isfile() or m.isdir()) and not m.name.startswith('/') and '..' not in Path(m.name).parts
                 and '\\' not in m.name for m in members),'unsafe_archive_member')
        need(len({m.name for m in members}) == len(members), 'duplicate_archive_member')
        def read(name):
            member = reader.getmember(name)
            need(member.isfile() and member.size <= 16*1024*1024, 'invalid_archive_metadata')
            stream = reader.extractfile(member)
            need(stream is not None)
            return stream.read()
        manifests = json.loads(read('manifest.json'))
        need(type(manifests) is list and len(manifests) == 1, 'single_image_required')
        entry = manifests[0]
        raw = read(entry['Config'])
        need('sha256:'+hashlib.sha256(raw).hexdigest() == expected, 'image_config_mismatch')
        config = json.loads(raw)
        need(config.get('architecture') == 'amd64' and config.get('os') == 'linux', 'amd64_linux_required')
        env = config.get('config',{}).get('Env',[])
        need('PG_MAJOR=17' in env and any(v.startswith('PG_VERSION=17.') for v in env), 'postgres17_required')
        layers = entry['Layers']; differences = config.get('rootfs',{}).get('diff_ids',[])
        need(type(layers) is list and len(layers) == len(differences) and layers, 'image_layer_mismatch')
        for name, expected_layer in zip(layers,differences):
            member = reader.getmember(name); need(member.isfile(),'regular_image_layer_required')
            stream = reader.extractfile(member); h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024*1024),b''): h.update(block)
            need('sha256:'+h.hexdigest() == expected_layer,'image_layer_mismatch')
    return config


def command(argv):
    environment = {key:value for key,value in os.environ.items() if not key.startswith('DOCKER_')}
    if argv[0] == 'docker':
        argv = ['docker','--host','unix:///var/run/docker.sock',*argv[1:]]
    result = subprocess.run(argv,capture_output=True,check=False,env=environment)
    need(result.returncode == 0,'bootstrap_command_failed')
    return result.stdout


def pg_files(lock):
    primary,source = direction(lock)
    ip = lock['pg_a' if lock['node'] == 'A' else 'pg_b']
    peer = lock['pg_b' if lock['node'] == 'A' else 'pg_a']
    config = '\n'.join([
        "listen_addresses = '"+ip+"'", "port = 5432", "cluster_name = 'ha_"+lock['node']+"'",
        "ssl = on", "ssl_min_protocol_version = 'TLSv1.3'", "ssl_cert_file = '/run/ha-pg/server.crt'",
        "ssl_key_file = '/run/ha-pg/server.key'", "ssl_ca_file = '/run/ha-pg/ca.crt'",
        "hba_file = '/bootstrap/pg_hba.conf'", "password_encryption = 'scram-sha-256'",
        "unix_socket_directories = '/var/run/postgresql'", "wal_level = replica", "max_wal_senders = 4",
        "max_replication_slots = 0", "synchronous_commit = remote_apply", "synchronous_standby_names = 'FIRST 1 (ha_"+('b' if lock['node']=='A' else 'a')+")'",
        "fsync = on", "full_page_writes = on", "max_prepared_transactions = 0", "shared_buffers = '64MB'",
        "max_connections = 64", "wal_sender_timeout = 2000", "wal_receiver_timeout = 2000",
        "log_statement = 'none'", "log_min_error_statement = 'panic'", "log_parameter_max_length_on_error = 0",
        "log_connections = off", "log_disconnections = off", "log_hostname = off",
    ])+'\n'
    # No trust rule or unencrypted TCP. Local administrator is the explicit TCB.
    hba = 'local all postgres peer\nhostssl replication ha_repl '+peer+'/32 scram-sha-256\n'
    hba += 'hostssl ha all '+lock['pg_a']+'/32 scram-sha-256\nhostssl ha all '+lock['pg_b']+'/32 scram-sha-256\n'
    hba += 'host all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n'
    service = {'image':lock['postgres']['config_digest'],'pull_policy':'never','network_mode':'host',
        'restart':'no','read_only':True,'security_opt':['no-new-privileges:true'],
        'cap_drop':['ALL'],'user':str(lock['postgres']['uid'])+':'+str(lock['postgres']['gid']),
        'pids_limit':128,'mem_limit':'512m','cpus':1,
        'environment':{'HA_PG_NODE':lock['node'],'HA_PG_UID':str(lock['postgres']['uid']),'HA_PG_GID':str(lock['postgres']['gid']),'HA_PG_PRIMARY_NODE':primary,
                       'HA_PG_SOURCE_NODE':source or 'none','POSTGRES_PASSWORD_FILE':'/run/ha-pg/postgres-password',
                       'POSTGRES_INITDB_ARGS':'--data-checksums --auth-host=scram-sha-256 --auth-local=peer'},
        'entrypoint':['bash','/bootstrap/pg-entrypoint.sh'],
        'extra_hosts':{'ha-a':lock['pg_a'],'ha-b':lock['pg_b']},
        'volumes':[{'type':'bind','source':source,'target':target,'read_only':ro,'bind':{'create_host_path':False}}
                   for source,target,ro in [('/var/lib/n100-ha/pgdata','/var/lib/postgresql/data',False),
                    ('/etc/n100-ha/pg-private','/run/ha-pg',True),('/etc/n100-ha/pg-bootstrap','/bootstrap',True),
                    ('/run/n100-ha/pg','/var/run/postgresql',False)]],
        'tmpfs':['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777'],
        'logging':{'driver':'local','options':{'max-size':'5m','max-file':'2'}}}
    initializer = {'image':lock['utility_config_digest'],'pull_policy':'never','network_mode':'none',
        'user':str(lock['postgres']['uid'])+':'+str(lock['postgres']['gid']),'read_only':True,'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
        'restart':'no','pids_limit':64,'entrypoint':['python','-B','/bootstrap/initialize.py'],
        'command':['--config','/run/ha-init/config.json','--phase','catalog'],
        'volumes':[{'type':'bind','source':source,'target':target,'read_only':ro,'bind':{'create_host_path':False}}
            for source,target,ro in [('/etc/n100-ha/pg-init','/run/ha-init',True),
                ('/etc/n100-ha/pg-bootstrap','/bootstrap',True),('/run/n100-ha/pg','/var/run/postgresql',True)]],
        'tmpfs':['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777']}
    return {'postgresql.conf':config,'pg_hba.conf':hba,
            'pg-compose.json':encode({'name':'n100-ha-pg-'+lock['installation_id'][:12], 'services':{'postgres':service}}),
            'init-compose.json':encode({'name':'n100-ha-init-'+lock['installation_id'][:12], 'services':{'initialize':initializer}})}


def prepare(lock_path, inputs, output):
    lock = validate(json.loads(regular(lock_path).read_bytes()))
    inputs, output = Path(inputs).resolve(), Path(output).absolute()
    records = [lock['ubuntu_image'], *packages(lock), lock['postgres']]
    for record in records:
        need(digest(regular(inputs/record['filename'])) == record['sha256'],'artifact_hash_mismatch')
    with regular(inputs/lock['ubuntu_image']['filename']).open('rb') as stream:
        header = stream.read(8)
    need(header[:4] == b'QFI\xfb' and int.from_bytes(header[4:8],'big') in (2,3),'qcow2_image_required')
    archive(inputs/lock['postgres']['filename'],lock['postgres']['config_digest'])
    # Always bind executable installer/runtime bytes to the exact committed source.
    commit = lock['source_commit']
    need(command(['git','-C',str(ROOT),'rev-parse',commit+'^{commit}']).decode().strip() == commit)
    sources = ['infra/k8s/ha-package/bootstrap.py']+['infra/k8s/ha-package/bootstrap_assets/'+name for name in ASSETS]
    tracked = command(['git','-C',str(ROOT),'ls-tree','-rz','--name-only',commit]).decode().split('\0')
    sources += [p for p in tracked if re.fullmatch(r'infra/k8s/ha_runtime/[a-z_]+\.(?:py|sql)',p)]
    blobs = {name:command(['git','-C',str(ROOT),'show',commit+':'+name]) for name in sources}
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    files = {}
    def write(name,raw):
        path = output/name; path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_bytes(raw); path.chmod(0o444); files[name] = digest(path)
    write('lock.json',encode(lock))
    for name,raw in blobs.items():
        target = 'bootstrap.py' if name.endswith('/bootstrap.py') else ('assets/'+Path(name).name if '/bootstrap_assets/' in name else 'runtime/ha_runtime/'+Path(name).name)
        write(target,raw)
    for name,raw in pg_files(lock).items(): write('generated/'+name,raw.encode() if isinstance(raw,str) else raw)
    for record in records:
        target = output/'artifacts'/record['filename']; target.parent.mkdir(mode=0o700,exist_ok=True)
        shutil.copyfile(inputs/record['filename'],target); target.chmod(0o444)
        files['artifacts/'+record['filename']] = digest(target)
        need(files['artifacts/'+record['filename']] == record['sha256'],'artifact_changed_during_copy')
    manifest = {'schema_version':1,'source_commit':commit,'installation_id':lock['installation_id'],
        'node':lock['node'],'source_files':{k:hashlib.sha256(v).hexdigest() for k,v in blobs.items()},'files':files,
        'production_deployed':False,'writer_authority_issued':False,'full_seven_app_restore_verified':False,
        'os_conversion_verified':False,'provenance_signature_verified_by_builder':False}
    write('manifest.json',encode(manifest))
    return manifest


def verify(bundle):
    bundle = Path(bundle).absolute()
    manifest = json.loads(regular(bundle/'manifest.json').read_bytes())
    need(set(manifest) == {'schema_version','source_commit','installation_id','node','source_files','files',
         'production_deployed','writer_authority_issued','full_seven_app_restore_verified','os_conversion_verified',
         'provenance_signature_verified_by_builder'},'manifest_invalid')
    lock = validate(json.loads(regular(bundle/'lock.json').read_bytes()))
    need(all(manifest[k] == lock[k] for k in ('source_commit','installation_id','node')),'manifest_lock_mismatch')
    need(manifest['schema_version'] == 1 and all(manifest[k] is False for k in ('production_deployed','writer_authority_issued',
         'full_seven_app_restore_verified','os_conversion_verified','provenance_signature_verified_by_builder')))
    need(type(manifest['files']) is dict and manifest['files'],'bundle_files_required')
    required = {'lock.json','bootstrap.py',*('assets/'+n for n in ASSETS),
                *('generated/'+n for n in pg_files(lock)),
                *('artifacts/'+r['filename'] for r in [lock['ubuntu_image'],*packages(lock),lock['postgres']])}
    need(required <= set(manifest['files']),'bundle_files_missing')
    need(type(manifest['source_files']) is dict and manifest['source_files'],'source_manifest_required')
    for name,expected in manifest['source_files'].items():
        need(type(name) is str and type(expected) is str and HEX.fullmatch(expected),'source_manifest_invalid')
        target = 'bootstrap.py' if name == 'infra/k8s/ha-package/bootstrap.py' else ('assets/'+Path(name).name if name in {'infra/k8s/ha-package/bootstrap_assets/'+n for n in ASSETS} else 'runtime/ha_runtime/'+Path(name).name if re.fullmatch(r'infra/k8s/ha_runtime/[a-z_]+\.(?:py|sql)',name) else None)
        need(target is not None and manifest['files'].get(target) == expected,'source_manifest_binding_mismatch')
    for name,expected in manifest['files'].items():
        need(type(name) is str and not name.startswith('/') and '..' not in Path(name).parts
             and type(expected) is str and HEX.fullmatch(expected),'manifest_path_invalid')
        need(digest(regular(bundle/name)) == expected,'bundle_hash_mismatch')
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob('*') if not p.is_dir()}
    need(actual == set(manifest['files'])|{'manifest.json'},'bundle_inventory_mismatch')
    expected_generated = pg_files(lock)
    for name,raw in expected_generated.items():
        need((bundle/'generated'/name).read_bytes() == (raw.encode() if isinstance(raw,str) else raw),'generated_contract_mismatch')
    for record in [lock['ubuntu_image'],*packages(lock),lock['postgres']]:
        need(digest(regular(bundle/'artifacts'/record['filename'])) == record['sha256'],'artifact_hash_mismatch')
    archive(bundle/'artifacts'/lock['postgres']['filename'],lock['postgres']['config_digest'])
    return lock, manifest


def install(bundle, expected_manifest):
    """Root-only fresh guest action. Partial installation is never auto-retried."""
    bundle = Path(bundle).absolute()
    need(HEX.fullmatch(expected_manifest) and digest(regular(bundle/'manifest.json')) == expected_manifest,'trusted_manifest_hash_required')
    lock, _ = verify(bundle)
    need(os.geteuid() == 0 and os.uname().machine == 'x86_64','fresh_amd64_guest_root_required')
    os_release = Path('/etc/os-release').read_text()
    need('ID=ubuntu\n' in os_release and 'VERSION_ID="24.04"' in os_release,'ubuntu2404_required')
    intent = json.loads(regular('/etc/n100-ha-bootstrap-intent.json').read_bytes())
    need(intent == {k:lock[k] for k in ('source_commit','installation_id','node')}
         | {'ubuntu_image_sha256':lock['ubuntu_image']['sha256']},'guest_intent_mismatch')
    info = Path('/etc/n100-ha-bootstrap-intent.json').stat()
    need(info.st_uid == 0 and info.st_mode & 0o077 == 0,'private_guest_intent_required')
    for path in ('/etc/n100-ha','/var/lib/n100-ha','/var/lib/docker','/var/lib/containerd','/etc/docker','/usr/sbin/policy-rc.d',
                 '/run/systemd/system/docker.service','/run/systemd/system/docker.socket','/run/systemd/system/containerd.service'):
        need(not os.path.lexists(path),'fresh_guest_only')
    need(not shutil.which('docker') and not shutil.which('containerd'),'existing_engine_forbidden')
    for item in packages(lock):
        data = command(['dpkg-deb','--show','--showformat=${Package}\t${Version}\t${Architecture}',str(bundle/'artifacts'/item['filename'])]).decode()
        need(data == '\t'.join(item[k] for k in ('package','version','architecture')),'deb_metadata_mismatch')
    Path('/var/lib/n100-ha').mkdir(mode=0o700)
    journal = Path('/var/lib/n100-ha/bootstrap.json')
    journal.write_bytes(encode({'installation_id':lock['installation_id'],'status':'installing','manifest_sha256':expected_manifest}))
    journal.chmod(0o600)
    policy = Path('/usr/sbin/policy-rc.d')
    policy.write_text('#!/bin/sh\nexit 101\n'); policy.chmod(0o755)
    # Masks also protect against maintainer scripts invoking systemctl directly.
    command(['systemctl','mask','--runtime','docker.service','docker.socket','containerd.service'])
    try:
        command(['dpkg','--install',*[str(bundle/'artifacts'/item['filename']) for item in packages(lock)]])
        need(not command(['dpkg','--audit']).strip(),'offline_dependency_closure_incomplete')
        for item in packages(lock):
            installed = command(['dpkg-query','--show','--showformat=${db:Status-Status}\t${Version}\t${Architecture}',item['package']]).decode()
            need(installed == '\t'.join(('installed',item['version'],item['architecture'])),'installed_package_mismatch')
        Path('/etc/docker').mkdir(mode=0o755,exist_ok=True)
        Path('/etc/docker/daemon.json').write_bytes(encode({'storage-driver':'overlay2','features':{'containerd-snapshotter':False},'live-restore':True}))
        command(['systemctl','unmask','--runtime','docker.service','docker.socket','containerd.service'])
        command(['systemctl','disable','docker.service','docker.socket','containerd.service'])
        command(['systemctl','start','docker.service'])
        info = json.loads(command(['docker','info','--format','{{json .}}']))
        need(info.get('Driver') == 'overlay2' and 'io.containerd.snapshotter.v1' not in str(info.get('DriverStatus')),'classic_overlay2_required')
        command(['docker','load','--input',str(bundle/'artifacts'/lock['postgres']['filename'])])
        image = json.loads(command(['docker','image','inspect',lock['postgres']['config_digest']]))
        need(len(image) == 1 and image[0]['Id'] == lock['postgres']['config_digest'],'loaded_image_mismatch')
        for flag,key in (('-u','uid'),('-g','gid')):
            actual_id = command(['docker','run','--rm','--pull=never','--network=none','--read-only',
                '--cap-drop=ALL','--security-opt=no-new-privileges:true','--label','personal-server.ha-bootstrap='+lock['installation_id'],
                '--entrypoint','id',lock['postgres']['config_digest'],flag,'postgres']).decode().strip()
            need(actual_id == str(lock['postgres'][key]),'postgres_image_uid_mismatch')
        root = Path('/etc/n100-ha'); root.mkdir(mode=0o700)
        pg = root/'pg-bootstrap'; pg.mkdir(mode=0o750); os.chown(pg,0,lock['postgres']['gid'])
        for name in ('postgresql.conf','pg_hba.conf'):
            shutil.copyfile(bundle/'generated'/name,pg/name); (pg/name).chmod(0o440); os.chown(pg/name,0,lock['postgres']['gid'])
        for name in ('pg-entrypoint.sh','initialize.py'):
            shutil.copyfile(bundle/'assets'/name,pg/name); (pg/name).chmod(0o440); os.chown(pg/name,0,lock['postgres']['gid'])
        shutil.copyfile(bundle/'generated/pg-compose.json',root/'pg-compose.json')
        (root/'pg-compose.json').chmod(0o400)
        shutil.copyfile(bundle/'generated/init-compose.json',root/'init-compose.json'); (root/'init-compose.json').chmod(0o400)
        (root/'pg-init').mkdir(mode=0o700); os.chown(root/'pg-init',lock['postgres']['uid'],lock['postgres']['gid'])
        (root/'pg-private').mkdir(mode=0o700); os.chown(root/'pg-private',lock['postgres']['uid'],lock['postgres']['gid'])
        data = Path('/var/lib/n100-ha/pgdata'); data.mkdir(mode=0o700); os.chown(data,lock['postgres']['uid'],lock['postgres']['gid'])
        run = Path('/run/n100-ha/pg'); run.mkdir(mode=0o700,parents=True); os.chown(run,lock['postgres']['uid'],lock['postgres']['gid'])
        journal.write_bytes(encode({'installation_id':lock['installation_id'],'status':'engine_installed_pg_not_started',
             'manifest_sha256':expected_manifest,'writer_authority_issued':False,'production_deployed':False}))
    finally:
        # Only remove our own fixed inhibitor. Failure leaves masks/journal for inspection.
        if policy.is_file() and policy.read_text() == '#!/bin/sh\nexit 101\n': policy.unlink()
    return {'status':'PASS','postgres_started':False,'writer_authority_issued':False,'production_deployed':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action',required=True)
    p = subs.add_parser('prepare'); p.add_argument('--lock',required=True); p.add_argument('--inputs',required=True); p.add_argument('--output',required=True)
    p = subs.add_parser('verify'); p.add_argument('--bundle',required=True)
    p = subs.add_parser('install'); p.add_argument('--bundle',required=True); p.add_argument('--manifest-sha256',required=True)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            manifest = prepare(args.lock,args.inputs,args.output)
            result = {'status':'PASS','source_commit':manifest['source_commit'],'production_deployed':False}
        elif args.action == 'verify':
            lock,_ = verify(args.bundle); result = {'status':'PASS','source_commit':lock['source_commit'],'production_deployed':False}
        else: result = install(args.bundle,args.manifest_sha256)
    except Exception as error:
        # Never emit dpkg output, credentials, endpoint values, SQL or exception text.
        result = {'status':'FAIL','reason':str(error) if isinstance(error,BootstrapError) else 'bootstrap_failed'}
    print(json.dumps(result,sort_keys=True))
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
