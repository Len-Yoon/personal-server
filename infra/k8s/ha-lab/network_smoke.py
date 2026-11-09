#!/usr/bin/env python3
"""Owned Linux namespace/socket acceptance. Echo bytes only; no providers or TLS claim."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[3]
SOURCES=('infra/k8s/ha-lab/network_smoke.py','infra/k8s/ha_runtime/__init__.py',
         'infra/k8s/ha_runtime/config.py','infra/k8s/ha_runtime/db.py','infra/k8s/ha_runtime/proof.py','infra/k8s/ha_runtime/enforcement.py',
         'infra/k8s/ha_runtime/enforcement.sql','infra/k8s/ha_control/__init__.py','infra/k8s/ha_control/privilege_closure.py','infra/k8s/tools/n100_ha_contract.py','infra/k8s/ha_egress/__init__.py',
         'infra/k8s/ha_egress/client.py','infra/k8s/ha_egress/socket_gateway.py',
         'infra/k8s/ha_egress/app_entrypoint.py')
BASE_CONTEXT='colima-codex-ha-01a1add8'
sys.path.insert(0,str(ROOT/'infra/k8s'))


def need(value):
    if not value:raise AssertionError('network_fixture_check_failed')


def install():
    """Image-local fake credentials/receipt: accepted metadata, never TLS evidence."""
    from ha_runtime.enforcement import definition_sha256
    authority=Path('/fixture');authority.mkdir(mode=0o755)
    release={'source_commit':'a'*40,'release_id':'b'*64,'source_files_sha256':'c'*64}
    def write(path,value,mode=0o444,uid=0,gid=0):
        Path(path).write_text(json.dumps(value) if isinstance(value,dict) else value)
        os.chown(path,uid,gid);os.chmod(path,mode)
    write('/source/infra/k8s/ha_runtime/release.json',release)
    for name in ('ca','cert'):write(authority/name,'fixture-placeholder-no-tls\n')
    for name in ('key','passfile'):write(authority/name,'fixture-not-a-credential\n',0o600,10001,10001)
    endpoint=lambda host:dict(host=host,port=5432,dbname='fixture',user='fixture',sslmode='verify-full',sslrootcert='/fixture/ca',passfile='/fixture/passfile')
    value=dict(cluster='isolated-network-fixture',system_id='123456789',node='A',service='book-memo',
               primary_node='A',primary=endpoint('ha-a'),partner=endpoint('ha-b'),scope='n100-approved',
               profile='enforced-vm-v1',enforcement_sha256=definition_sha256(),installation_id='d'*64,generation=1,
               effects=dict(url='https://ha-broker:8443',ca='/fixture/ca',certificate='/fixture/cert',key='/fixture/key',server_sha256='e'*64,plan_sha256='f'*64),**release)
    write(authority/'config.json',value,0o600,10001,10001)
    receipt={key:item for key,item in value.items() if key!='scope'}
    receipt.update(schema_version=3,action='activate-ha',authority_kind='installed-runtime',
                   installed_at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat().replace('+00:00','Z'),nodes={'A':'ha-a','B':'ha-b'})
    write(authority/'receipt.json',receipt)
    policy=dict(schema_version=1,pg_a=dict(host='172.29.241.10',port=5432),pg_b=dict(host='172.29.241.11',port=5432))
    write(authority/'targets.json',policy)


def echo(port):
    server=socket.socket();server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind(('0.0.0.0',port));server.listen()
    def serve(client):
        with client:
            while data:=client.recv(16384):client.sendall(data)
    while True:
        client,_=server.accept();threading.Thread(target=serve,args=(client,),daemon=True).start()


def roundtrip(address,unix=False):
    data=bytes(range(256))*513
    with socket.socket(socket.AF_UNIX if unix else socket.AF_INET,socket.SOCK_STREAM) as stream:
        stream.settimeout(4);stream.connect(address);stream.sendall(data);stream.shutdown(socket.SHUT_WR)
        received=bytearray()
        while chunk:=stream.recv(16384):received.extend(chunk)
        need(bytes(received)==data)


def linux(mode):
    need(sys.platform=='linux')
    from ha_egress import socket_gateway as gateway,app_entrypoint as entry
    if mode=='install':install()
    elif mode=='init':
        for path,uid,gid in (('/run/ha-network',10003,10001),('/run/ha-network/book-memo',10003,10001),('/run/ha-ingress',10001,10004)):
            Path(path).mkdir(exist_ok=True);os.chown(path,uid,gid);os.chmod(path,0o750)
    elif mode=='pg':echo(5432)
    elif mode=='gateway':
        for port in (8443,8444):threading.Thread(target=echo,args=(port,),daemon=True).start()
        policy=Path('/fixture/targets.json');instance=gateway.SocketGateway(str(policy),hashlib.sha256(policy.read_bytes()).hexdigest())
        instance.start();threading.Event().wait()
    elif mode=='app':
        command=[sys.executable,__file__,'--linux','child']
        raise SystemExit(entry.main(['--service','book-memo','--app-port','8003','--',*command]))
    elif mode=='child':
        Path('/run/ha-ingress/child-started').write_text('fixture')
        echo(8003)
    elif mode=='check':
        entry.validate_application('book-memo')
        for address in entry.LOOPS.values():roundtrip(address)
        for target in (('172.29.241.10',5432),('1.1.1.1',443)):
            try:
                with socket.create_connection(target,timeout=.3):pass
            except OSError:pass
            else:raise AssertionError('direct_tcp_admitted')
        try:Path('/run/ha-network/pg-a.sock').unlink()
        except OSError:pass
        else:raise AssertionError('capability_unlink_admitted')
        try:Path('/root-write-probe').write_text('fixture')
        except OSError:pass
        else:raise AssertionError('root_write_admitted')
        status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
        print(json.dumps(dict(kernel_status={key:status[key] for key in ('NoNewPrivs','CapInh','CapPrm','CapEff','CapBnd','CapAmb')},root_readonly=bool(os.statvfs('/').f_flag&os.ST_RDONLY),capability_readonly=bool(os.statvfs('/run/ha-network').f_flag&os.ST_RDONLY),gateway_peer_uid_verified=10003,uid=os.getuid(),gid=os.getgid(),groups=os.getgroups(),interfaces=[name for _,name in socket.if_nameindex()],three_binary_halfclose_paths=True,direct_tcp_denied=2,readonly_unlink_denied=True,production_config_loaded=True)))
    elif mode=='ingress':roundtrip('/run/ha-ingress/http.sock',True)
    elif mode=='wrong-ingress':
        try:roundtrip('/run/ha-ingress/http.sock',True)
        except (OSError,AssertionError):return
        raise AssertionError('wrong_ingress_uid_admitted')
    elif mode=='wrong-gateway':
        try:roundtrip('/run/ha-network/book-memo/pg-a.sock',True)
        except (OSError,AssertionError):return
        raise AssertionError('wrong_gateway_uid_admitted')


def host(directory,context):
    import shutil
    spec=importlib.util.spec_from_file_location('ha_fixture',ROOT/'infra/k8s/ha-lab/run.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    fixture=module.Fixture(directory,runtime_context=context)
    resources=[];report={'scope':'local_linux_representative_book_memo_echo_only','provider_calls':0,'production_access':False,'tls_verified':False,'all_seven_apps_verified':False,'windows_whole_transition_verified':False}
    def docker(*args,**kwargs):
        fixture.validate_runtime();return fixture.docker(*args,**kwargs)
    def create(kind,*args):
        identifier=docker(*args).splitlines()[-1];resources.append((kind,identifier));persist();return identifier
    def persist():
        (directory/'owner.json').write_text(json.dumps({'owner':fixture.owner,'runtime':fixture.runtime_identity,'resources':resources},sort_keys=True,indent=2))
    def snapshot():
        return {kind:sorted(set(docker(*args).split())) for kind,args in (('containers',('ps','-aq','--no-trunc')),('volumes',('volume','ls','-q')),('networks',('network','ls','-q','--no-trunc')),('images',('image','ls','-q','--no-trunc')))}
    def run(mode,*,network='none',user='10001:10001',mounts=(),detached=False,extra=()):
        args=['run','--label',fixture.label,'--network',network,'--user',user,'--cap-drop','ALL','--security-opt','no-new-privileges','--read-only','--group-add','10004']
        for mount in mounts:args+=['--mount',mount]
        args+=list(extra)
        if detached:args+=['-d']
        identifier=create('container',*args,image,'python','/source/infra/k8s/ha-lab/network_smoke.py','--linux',mode) if detached else None
        if identifier:return identifier
        # Create then start to retain exact ownership identity even when a check fails.
        args[0]='create';identifier=create('container',*args,image,'python','/source/infra/k8s/ha-lab/network_smoke.py','--linux',mode)
        return docker('start','-a',identifier)
    before=None;error=None;cleaned=True
    directory.mkdir(parents=True,exist_ok=False)
    try:
        fixture.validate_runtime();before=snapshot();report['runtime_identity']=fixture.runtime_identity
        persist()
        build=directory/'context';build.mkdir();hashes={}
        for name in SOURCES:
            source=ROOT/name;need(source.is_file() and not source.is_symlink());target=build/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target);hashes[name]=hashlib.sha256(source.read_bytes()).hexdigest()
        base=module.approved_base("311");docker('pull',base,timeout=300);before=snapshot()
        dockerfile=f'FROM {base}\nWORKDIR /source\nENV PYTHONPATH=/source/infra/k8s PYTHONDONTWRITEBYTECODE=1\nCOPY source/ /source/\nRUN python /source/infra/k8s/ha-lab/network_smoke.py --linux install\n'
        (build/'Dockerfile').write_text(dockerfile);report['source_sha256']=hashes;report['dockerfile_sha256']=hashlib.sha256(dockerfile.encode()).hexdigest();report['base_image']=base
        tag='ha-network-fixture:'+fixture.owner
        docker('build','--label',fixture.label,'-t',tag,str(build),timeout=300)
        image=docker('image','inspect',tag,'--format','{{.Id}}');resources.append(('image',image));persist()
        network=create('network','network','create','--label',fixture.label,'--internal','--subnet','172.29.241.0/24','ha-network-'+fixture.owner)
        volumes=[]
        for suffix in ('cap','ingress'):
            name='ha-network-'+suffix+'-'+fixture.owner;docker('volume','create','--label',fixture.label,name);resources.append(('volume',name));persist();volumes.append(name)
        cap,ingress=volumes
        whole=(f'type=volume,src={cap},dst=/run/ha-network',f'type=volume,src={ingress},dst=/run/ha-ingress')
        # Initialization requires only root DAC ownership, in an owned disconnected container.
        args=['create','--label',fixture.label,'--network','none','--read-only']
        for mount in whole:args+=['--mount',mount]
        initializer=create('container',*args,image,'python','/source/infra/k8s/ha-lab/network_smoke.py','--linux','init');docker('start','-a',initializer)
        for ip in ('172.29.241.10','172.29.241.11'):run('pg',network=network,detached=True,extra=('--ip',ip))
        gateway_id=run('gateway',network=network,user='10003:10001',mounts=whole[:1],detached=True)
        for _ in range(100):
            if docker('exec',gateway_id,'python','-c',"from pathlib import Path;print(Path('/run/ha-network/book-memo/broker.sock').is_socket())")=='True':break
            time.sleep(.05)
        else:raise AssertionError('gateway_not_ready')
        appmounts=(f'type=volume,src={cap},dst=/run/ha-network,volume-subpath=book-memo,readonly',whole[1])
        appflags=('--add-host','ha-a:127.0.0.2','--add-host','ha-b:127.0.0.3','--add-host','ha-broker:127.0.0.4','-e','APP_ENV=production','-e','HA_PG_CONFIG=/fixture/config.json','-e','HA_PG_ACTIVATION=/fixture/receipt.json')
        app=run('app',mounts=appmounts,detached=True,extra=appflags)
        for _ in range(100):
            if docker('exec',app,'python','-c',"from pathlib import Path;print(Path('/run/ha-ingress/child-started').exists())")=='True':break
            time.sleep(.05)
        else:raise AssertionError('application_not_ready')
        report['linux_checks']=json.loads(docker('exec',app,'python','/source/infra/k8s/ha-lab/network_smoke.py','--linux','check'))
        run('ingress',user='10004:10004',mounts=(whole[1],));report['caddy_uid_ingress_binary_halfclose']=True
        run('wrong-ingress',mounts=(whole[1],));run('wrong-gateway',user='10004:10001',mounts=whole[:1]);report['wrong_peer_uid_denied']=2
        child_stamp=docker('exec',app,'python','-c',"from pathlib import Path;print(Path('/run/ha-ingress/child-started').stat().st_mtime_ns)")
        negative=run('app',network=network,mounts=appmounts,detached=True,extra=appflags)
        code=docker('wait',negative);need(code=='78')
        # PID namespace has no child process after validation rejects eth0.
        inspection=json.loads(docker('container','inspect',negative))[0];need(inspection['State']['Running'] is False)
        need(docker('exec',app,'python','-c',"from pathlib import Path;print(Path('/run/ha-ingress/child-started').stat().st_mtime_ns)")==child_stamp)
        report['eth0_entrypoint_refused_before_child']=True
    except Exception as exc:
        error=type(exc).__name__+':'+(str(exc) if isinstance(exc,module.FixtureError) else 'network_fixture_failed')
    finally:
        for kind,identifier in reversed(resources):
            try:
                fixture.validate_runtime();value=json.loads(docker(kind,'inspect',identifier))[0]
                labels=value.get('Config',{}).get('Labels',{}) if kind in ('image','container') else value.get('Labels',{})
                need(labels.get('personal-server.ha-app-fixture')==fixture.owner)
                need((value.get('Name') if kind=='volume' else value.get('Id'))==identifier)
                docker(*(('rm','-f',identifier) if kind=='container' else (kind,'rm',identifier)))
            except Exception:cleaned=False
        if before is not None:
            after=snapshot();report['protected_resource_inventory_unchanged']=before==after
            if before!=after:error=error or 'protected_resource_inventory_changed'
        report.update(status='PASS' if not error and cleaned else 'FAIL',error=error,owned_fixture_removed=cleaned)
        (directory/'report.json').write_text(json.dumps(report,sort_keys=True,indent=2));print(json.dumps(report,sort_keys=True))
    return int(report['status']!='PASS')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--linux');parser.add_argument('--directory',type=Path);parser.add_argument('--context',default=BASE_CONTEXT);args=parser.parse_args()
    if args.linux:linux(args.linux);return 0
    need(args.context==BASE_CONTEXT and args.directory is not None)
    destination=args.directory.resolve();allowed=(ROOT/'build/ha-predeploy-evidence').resolve();need(allowed in destination.parents)
    return host(destination,args.context)


if __name__=='__main__':raise SystemExit(main())
