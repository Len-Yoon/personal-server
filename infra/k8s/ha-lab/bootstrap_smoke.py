#!/usr/bin/env python3
"""Explicit Mac/owned-Colima closed PostgreSQL bootstrap acceptance.

No N100/Windows/production input, provider calls, published ports, image pulls,
context changes or daemon configuration changes. Only UUID-labelled fixtures
are removed. An interrupted run leaves a private owner journal for inspection.
"""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
CONTEXT = re.compile(r'colima-codex-ha-[a-z0-9]{8}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
DAEMON = re.compile(r'[A-Za-z0-9:_.-]{1,128}\Z')
LABEL = 'personal-server.ha-bootstrap'
SOURCES = ('infra/k8s/ha-lab/bootstrap_smoke.py','infra/k8s/ha-package/bootstrap.py',
           'infra/k8s/ha-package/bootstrap_assets/pg-entrypoint.sh',
           'infra/k8s/ha-package/bootstrap_assets/initialize.py')
FLAGS = ('authentication failed','no pg_hba.conf entry','permission denied',
         'could not translate host','certificate verify failed','pg_basebackup: error',
         'all replication slots are in use','invalid permissions','FATAL','ERROR',
         'database system is ready to accept connections')


class SmokeError(RuntimeError):
    pass


def need(value, reason='closed_bootstrap_check_failed'):
    if not value:
        raise SmokeError(reason)


def canonical(value):
    return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()


def validate_inputs(context,daemon_id,postgres_image,utility_image):
    need(type(context) is str and CONTEXT.fullmatch(context),'owned_colima_context_required')
    need(type(daemon_id) is str and DAEMON.fullmatch(daemon_id),'explicit_daemon_identity_required')
    need(all(type(value) is str and DIGEST.fullmatch(value) for value in (postgres_image,utility_image)),
         'exact_image_config_digest_required')
    need(postgres_image != utility_image,'separate_runtime_images_required')


def source_binding():
    files = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}
    result = subprocess.run(['git','-C',str(ROOT),'rev-parse','HEAD'],capture_output=True,timeout=10)
    commit = result.stdout.decode().strip()
    need(result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}',commit),'source_commit_required')
    matches = True
    for name,expected in files.items():
        result = subprocess.run(['git','-C',str(ROOT),'show',commit+':'+name],capture_output=True,timeout=10)
        matches = matches and result.returncode == 0 and hashlib.sha256(result.stdout).hexdigest() == expected
    return {'source_commit':commit,'source_files':files,'worktree_source_matches_commit':matches}


def private_output(path):
    path = Path(path).absolute()
    need(not any(p.is_symlink() for p in (path,*path.parents)),'non_symlink_output_required')
    need(path.parent.is_dir(),'existing_output_parent_required')
    path.mkdir(mode=0o700,exist_ok=False)
    return path


def load_builder():
    spec = importlib.util.spec_from_file_location('closed_pg_bootstrap',ROOT/'infra/k8s/ha-package/bootstrap.py')
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class Fixture:
    def __init__(self,context,daemon_id,postgres_image,utility_image,output,*,reverse_closed_only=False):
        validate_inputs(context,daemon_id,postgres_image,utility_image)
        self.context,self.daemon_id,self.pg,self.utility = context,daemon_id,postgres_image,utility_image
        self.output = Path(output)
        need(type(reverse_closed_only) is bool,'invalid_fixture_mode')
        self.reverse_closed_only=reverse_closed_only
        self.owner = str(uuid.uuid4());self.prefix = 'ha-bootstrap-'+self.owner
        self.environment = {k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')}
        self.socket = Path.home()/'.colima'/context.removeprefix('colima-')/'docker.sock'
        self.connected = False;self.phase = 'runtime_identity'
        self.resources = {'container':[],'volume':[],'network':[]}
        self.report = {'scope':'mac_synthetic_candidate_closed_bootstrap_not_final_ci_or_n100',
            'owner':self.owner,'production_access':False,'providers_called':False,
            'postgres_image':postgres_image,'utility_image':utility_image,
            'full_seven_app_restore_verified':False,'writer_authority_issued':False,
            'windows_fence_verified':False,'operating_system_install_verified':False,
            'operating_authority_rebound':False,'effects_transition_performed':False,
            'reverse_closed_only_requested':reverse_closed_only}

    def _invoke(self,args,*,data=None,timeout=60,stdin=None):
        result = subprocess.run(args,input=data,stdin=stdin,capture_output=True,
                                env=self.environment,timeout=timeout)
        need(result.returncode == 0,'fixture_command_failed')
        return result.stdout,result.stderr

    def raw(self,*args,data=None,timeout=60,stdin=None):
        return self._invoke(['docker','--host','unix://'+str(self.socket),*args],data=data,timeout=timeout,stdin=stdin)

    def pin(self):
        current = self.raw('info','--format','{{.ID}}',timeout=10)[0].decode().strip()
        need(current == self.daemon_id,'fixture_daemon_identity_changed')

    def docker(self,*args,data=None,timeout=60,stdin=None):
        need(self.connected,'runtime_not_validated')
        # Recheck before every action, including cleanup. A changed/replaced
        # socket must never redirect a UUID cleanup to another daemon.
        self.pin()
        stdout,stderr = self.raw(*args,data=data,timeout=timeout,stdin=stdin)
        return (stdout+(stderr if args[0]=='logs' else b'')).decode().strip()

    def journal(self):
        value = {'schema_version':1,'owner':self.owner,'context':self.context,
            'daemon_id':self.daemon_id,'socket_path_sha256':hashlib.sha256(str(self.socket).encode()).hexdigest(),
            'phase':self.phase,'resources':self.resources,'production_access':False}
        temporary = self.output/'owner.json.tmp'
        temporary.write_bytes(canonical(value));temporary.chmod(0o600)
        temporary.replace(self.output/'owner.json')

    def validate_runtime(self):
        need(sys.platform == 'darwin','mac_local_runtime_required')
        context = json.loads(self._invoke(['docker','context','inspect',self.context],timeout=10)[0])
        need(type(context) is list and len(context) == 1 and context[0].get('Name') == self.context
             and context[0]['Endpoints']['docker']['Host'] == 'unix://'+str(self.socket),
             'owned_local_socket_required')
        need(stat.S_ISSOCK(self.socket.stat().st_mode),'owned_local_socket_required')
        self.pin()
        info = json.loads(self.raw('info','--format','{{json .}}',timeout=10)[0])
        need(info.get('Driver') == 'overlay2' and 'io.containerd.snapshotter.v1' not in str(info.get('DriverStatus')),
             'classic_overlay2_required')
        for image in (self.pg,self.utility):
            record = json.loads(self.raw('image','inspect',image)[0])
            need(len(record)==1 and record[0].get('Id')==image and record[0].get('Architecture')=='amd64'
                 and record[0].get('Os')=='linux','exact_amd64_linux_image_required')
            if image == self.pg:
                env = record[0].get('Config',{}).get('Env',[])
                need('PG_MAJOR=17' in env and any(v.startswith('PG_VERSION=17.') for v in env),'postgres17_required')
        self.connected = True
        self.report.update(daemon_id=self.daemon_id,runtime_context=self.context,**source_binding())
        self.journal()

    def owned(self,kind,identifier):
        values = json.loads(self.docker(kind,'inspect',identifier));need(len(values)==1,'fixture_ownership_mismatch')
        value=values[0]
        labels=value.get('Config',{}).get('Labels',{}) if kind=='container' else value.get('Labels',{})
        actual=value.get('Name') if kind=='volume' else value.get('Id')
        need(actual==identifier and labels.get(LABEL)==self.owner,'fixture_ownership_mismatch')
        name=value.get('Name','').lstrip('/')
        need(name.startswith(self.prefix),'fixture_name_mismatch')
        return value

    def create(self,kind,*args):
        name=self.prefix+'-'+kind+'-'+str(len(self.resources[kind]))
        if kind=='container':
            identifier=self.docker('create','--name',name,'--label',LABEL+'='+self.owner,*args)
        else:
            identifier=self.docker(kind,'create','--label',LABEL+'='+self.owner,*args,name)
        self.resources[kind].append(identifier)
        self.journal();self.owned(kind,identifier)
        return identifier

    def wait(self,predicate,seconds=50):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            try:
                if predicate(): return
            except SmokeError:
                for identifier in self.resources['container']:
                    record=self.owned('container',identifier)
                    if record['Config']['Entrypoint']==['bash'] and not record['State']['Running'] and record['State']['ExitCode']:
                        raise SmokeError('owned_postgres_exited')
            time.sleep(.2)
        raise SmokeError('fixture_deadline')

    def psql(self,container,statement,database='postgres'):
        self.owned('container',container)
        return self.docker('exec','-i',container,'psql','-h','/var/run/postgresql','-XAt','-v',
            'ON_ERROR_STOP=1','-U','postgres','-d',database,data=statement.encode(),timeout=20)

    def postgres_ids(self):
        result=[]
        for flag in ('-u','-g'):
            container=self.create('container','--network','none','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true','--entrypoint','id',self.pg,flag,'postgres')
            self.docker('start',container)
            need(self.docker('wait',container)=='0','postgres_identity_probe_failed')
            value=self.docker('logs',container)
            need(value in ('70','999'),'supported_postgres_uid_required');result.append(int(value))
        need(tuple(result) in ((70,70),(999,999)),'postgres_identity_pair_invalid')
        self.uid,self.gid=result;self.report['postgres_uid']=self.uid;self.report['postgres_gid']=self.gid

    def stage(self,helper,source,target):
        self.owned('container',helper)
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream,mode='w') as archive:
                for path in source.iterdir(): archive.add(path,arcname=path.name)
            stream.seek(0)
            self.docker('cp','-',helper+':/fixture/'+target,stdin=stream,timeout=30)

    def certificates(self,directory,private,node,address):
        def openssl(*args):
            result=subprocess.run(['openssl',*args],capture_output=True,timeout=30)
            need(result.returncode==0,'synthetic_certificate_failed')
        extensions=directory/'extensions'
        extensions.write_text('subjectAltName=DNS:ha-'+node+',IP:'+address+'\nextendedKeyUsage=serverAuth\n')
        openssl('req','-newkey','rsa:2048','-nodes','-keyout',str(private/'server.key'),'-out',str(directory/'request.csr'),'-subj','/CN=ha-'+node)
        openssl('x509','-req','-in',str(directory/'request.csr'),'-CA',str(directory/'ca.crt'),'-CAkey',str(directory/'ca.key'),'-CAcreateserial',
                '-out',str(private/'server.crt'),'-days','1','-extfile',str(extensions))
        (private/'ca.crt').write_bytes((directory/'ca.crt').read_bytes())

    def prepare(self):
        self.postgres_ids()
        self.network=self.create('network','--internal')
        subnet=ipaddress.ip_network(self.owned('network',self.network)['IPAM']['Config'][0]['Subnet'])
        need(subnet.version==4 and subnet.num_addresses>16,'private_fixture_subnet_required')
        self.addresses={'a':str(subnet.network_address+10),'b':str(subnet.network_address+11)}
        names=('bootstrap-a','private-a','socket-a','data-a','init-a','bootstrap-b','private-b','socket-b','data-b')
        self.volumes={name:self.create('volume') for name in names}
        helper=self.create('container','--network','none','--user','0:0','--entrypoint','sh',
            *[arg for name in names for arg in ('-v',self.volumes[name]+':/fixture/'+name)],self.pg,'-c','sleep 600')
        self.docker('start',helper)
        builder=load_builder()
        with tempfile.TemporaryDirectory(prefix='owned-closed-ha-') as name:
            temporary=Path(name).resolve()
            result=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(temporary/'ca.key'),
                '-out',str(temporary/'ca.crt'),'-days','1','-subj','/CN=Owned Synthetic Bootstrap CA'],capture_output=True,timeout=30)
            need(result.returncode==0,'synthetic_certificate_failed')
            services=builder.SERVICES if hasattr(builder,'SERVICES') else ('portal-web','book-memo','youtube-memo','crawler-worker','car-care-worker','homeops-executor','system-agent')
            passwords={role:secrets.token_urlsafe(32) for role in ['ha_repl','ha_effect_broker',
                *[service.replace('-','_')+'_'+node for service in services for node in ('a','b')]]}
            for node in ('a','b'):
                bootstrap=temporary/('bootstrap-'+node);private=temporary/('private-'+node)
                bootstrap.mkdir(mode=0o700);private.mkdir(mode=0o700)
                lock={'node':node.upper(),'pg_a':self.addresses['a'],'pg_b':self.addresses['b'],
                    'postgres':{'config_digest':self.pg,'uid':self.uid,'gid':self.gid},
                    'installation_id':hashlib.sha256(self.owner.encode()).hexdigest(),'utility_config_digest':self.utility}
                for filename,raw in builder.pg_files(lock).items():
                    if filename in ('postgresql.conf','pg_hba.conf'):(bootstrap/filename).write_text(raw)
                for filename in ('pg-entrypoint.sh','initialize.py'):
                    (bootstrap/filename).write_bytes((ROOT/'infra/k8s/ha-package/bootstrap_assets'/filename).read_bytes())
                self.certificates(temporary,private,node,self.addresses[node])
                if node=='a':self.original_a_certificate_sha256=hashlib.sha256((private/'server.crt').read_bytes()).hexdigest()
                (private/'postgres-password').write_text(secrets.token_urlsafe(32))
                (private/'repl.pgpass').write_text(''.join('ha-'+n+':5432:replication:ha_repl:'+passwords['ha_repl']+'\n' for n in ('a','b')))
                self.stage(helper,bootstrap,'bootstrap-'+node);self.stage(helper,private,'private-'+node)
            initialize=temporary/'initialize';initialize.mkdir(mode=0o700)
            (initialize/'config.json').write_bytes(canonical({'schema_version':1,'cluster':'synthetic_'+self.owner.replace('-',''),
                'source_commit':self.report['source_commit'],'plan_sha256':hashlib.sha256(self.owner.encode()).hexdigest(),
                'broker_instance':hashlib.sha256((self.owner+':broker').encode()).hexdigest(),'passwords':passwords}))
            self.stage(helper,initialize,'init-a')
            if self.reverse_closed_only:
                # Synthetic fixture CA/replication secret only, kept in memory
                # until a separate fresh standby certificate is issued.
                self.synthetic_ca=(temporary/'ca.crt').read_bytes(),(temporary/'ca.key').read_bytes()
                self.synthetic_repl_password=passwords['ha_repl']
            # Fixed numeric IDs, generated names and constant script; no secret
            # text or untrusted input becomes shell source.
            self.docker('exec',helper,'sh','-ceu','chown -R '+str(self.uid)+':'+str(self.gid)+
                ' /fixture; chmod 700 /fixture/*; chmod 600 /fixture/private-*/* /fixture/init-a/*; chmod 444 /fixture/bootstrap-*/*')

    def start(self,node,*,volumes=None,addresses=None,primary='A'):
        volumes=self.volumes if volumes is None else volumes
        addresses=self.addresses if addresses is None else addresses
        source='none' if node.upper()==primary else primary
        container=self.create('container','--network',self.network,'--ip',addresses[node],'--network-alias','ha-'+node,
            '--add-host','ha-a:'+addresses['a'],'--add-host','ha-b:'+addresses['b'],
            '--user',str(self.uid)+':'+str(self.gid),'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
            '--memory','512m','--pids-limit','128','-e','HA_PG_NODE='+node.upper(),
            '-e','HA_PG_UID='+str(self.uid),'-e','HA_PG_GID='+str(self.gid),
            '-e','HA_PG_PRIMARY_NODE='+primary,'-e','HA_PG_SOURCE_NODE='+source,
            '-v',volumes['bootstrap-'+node]+':/bootstrap:ro','-v',volumes['private-'+node]+':/run/ha-pg:ro',
            '-v',volumes['socket-'+node]+':/var/run/postgresql','-v',volumes['data-'+node]+':/var/lib/postgresql/data',
            '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m','--entrypoint','bash',self.pg,'/bootstrap/pg-entrypoint.sh')
        self.docker('start',container);return container

    def initialize(self,phase):
        container=self.create('container','--network','none','--user',str(self.uid)+':'+str(self.gid),'--read-only',
            '--cap-drop','ALL','--security-opt','no-new-privileges:true','-v',self.volumes['bootstrap-a']+':/bootstrap:ro',
            '-v',self.volumes['init-a']+':/run/ha-init:ro','-v',self.volumes['socket-a']+':/var/run/postgresql:ro',
            '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m','--entrypoint','python',self.utility,
            '-B','/bootstrap/initialize.py','--config','/run/ha-init/config.json','--phase',phase)
        self.docker('start',container);code=self.docker('wait',container)
        value=json.loads(self.docker('logs',container))
        need(code=='0' and value.get('status')=='PASS','closed_initializer_failed')
        return value

    def acceptance(self):
        self.phase='primary_initdb';self.journal();a=self.start('a');self.a=a
        self.wait(lambda:self.psql(a,'SELECT 1')=='1')
        self.phase='replication_bootstrap';self.journal();self.report['replication']=self.initialize('replication')
        self.phase='standby_basebackup';self.journal();b=self.start('b');self.b=b
        self.wait(lambda:self.psql(b,'SELECT pg_is_in_recovery()')=='t')
        self.wait(lambda:self.psql(a,"SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_b' AND state='streaming' AND sync_state='sync'")=='1')
        self.phase='closed_catalog';self.journal();self.report['catalog']=self.initialize('catalog')
        self.wait(lambda:self.psql(b,'SELECT count(*) FROM ha.enforced_roles','ha')=='14')
        for container in (a,b):
            need(self.psql(container,"SELECT NOT writes_open AND NOT history_safe AND expires_at<clock_timestamp() FROM ha.control",'ha')=='t')
            need(self.psql(container,"SELECT to_regprocedure('ha.local_roles(text[],text,boolean)') IS NULL",'ha')=='t')
            need(self.psql(container,'SELECT count(DISTINCT service),count(*) FROM ha.identities','ha')=='7|14')
            need(self.psql(container,'SELECT count(*) FROM ha.enforced_namespaces','ha')=='13')
            need(self.psql(container,'SELECT count(*) FROM ha_effects.ledger','ha')=='0')
            need(self.psql(container,'SELECT count(*) FROM ha_effects.private_state','ha')=='0')
        self.phase='direct_sql_denial';self.journal()
        # Catch the exact server-side guard error inside an app session. Any
        # other SQL/transport error fails the test instead of being counted as
        # the expected write rejection.
        self.psql(a,"""SET SESSION AUTHORIZATION book_memo_a;
DO $$ DECLARE message text; BEGIN
  INSERT INTO s_book_memo_main.ha_receipts VALUES('forbidden','x','x');
  RAISE EXCEPTION 'closed_write_was_accepted';
EXCEPTION WHEN SQLSTATE '42501' THEN
  GET STACKED DIAGNOSTICS message = MESSAGE_TEXT;
  IF message <> 'enforced_writer_denied' THEN RAISE; END IF;
END $$;""",'ha')
        need(self.psql(a,"SELECT count(*) FROM s_book_memo_main.ha_receipts WHERE receipt='forbidden'",'ha')=='0')
        self.report.update(closed_direct_sql_denied=True,closed_direct_sql_denial_sqlstate='42501',
                           closed_direct_sql_denial_message='enforced_writer_denied')
        self.phase='tls_sync_verify';self.journal()
        need(self.psql(a,"SELECT count(*) FROM pg_stat_ssl WHERE ssl AND version='TLSv1.3'")=='1')
        need(self.psql(a,"SELECT current_setting('synchronous_commit')")=='remote_apply')
        self.report.update(status='PASS',synchronous_remote_apply=True,tls13_replication=True,
            peer_catalog_replayed=True,both_authorities_closed=True,fixture_function_absent_both=True)

    def catalog_fingerprint(self,container):
        query="""SELECT jsonb_build_object(
'authority',(SELECT to_jsonb(c) FROM ha.control c),
'identities',(SELECT jsonb_agg(to_jsonb(i) ORDER BY role_name) FROM ha.identities i),
'namespaces',(SELECT jsonb_agg(to_jsonb(n) ORDER BY schema_name) FROM ha.enforced_namespaces n),
'effects',(SELECT count(*) FROM ha_effects.ledger),
'private',(SELECT count(*) FROM ha_effects.private_state))::text"""
        return hashlib.sha256(self.psql(container,query,'ha').encode()).hexdigest()

    def reverse_acceptance(self):
        self.phase='reverse_closed_precondition';self.journal()
        expected=self.catalog_fingerprint(self.a)
        need(self.catalog_fingerprint(self.b)==expected,'closed_peer_catalog_mismatch')
        need(self.psql(self.b,"SELECT NOT writes_open AND NOT history_safe AND expires_at<clock_timestamp() FROM ha.control",'ha')=='t')
        old_a=self.owned('container',self.a)
        need(old_a['HostConfig']['RestartPolicy']['Name']=='no','retired_node_restart_policy_unsafe')
        original_volume=self.volumes['data-a']
        self.docker('stop','--time','5',self.a)
        retired=self.owned('container',self.a)
        need(retired['State']['Running'] is False and retired['State']['Pid']==0,'old_owned_primary_not_stopped')
        self.docker('network','disconnect',self.network,self.a)
        self.phase='reverse_promote_owned_closed_b';self.journal()
        need(self.psql(self.b,'SELECT pg_promote(true,10)')=='t','closed_candidate_promotion_failed')
        need(self.psql(self.b,'SELECT pg_is_in_recovery()')=='f')
        need(self.catalog_fingerprint(self.b)==expected,'promotion_changed_logical_authority')
        timeline=int(self.psql(self.b,"SELECT ('x'||substring(pg_walfile_name(pg_current_wal_insert_lsn()),1,8))::bit(32)::int"))
        need(timeline>1,'new_physical_timeline_required')
        # New resource identities and a different IP/certificate. The retired A
        # container and its original PGDATA are preserved and never rewound.
        subnet=ipaddress.ip_network(self.owned('network',self.network)['IPAM']['Config'][0]['Subnet'])
        addresses={'a':str(subnet.network_address+12),'b':self.addresses['b']}
        fresh={name:self.create('volume') for name in ('bootstrap-a','private-a','socket-a','data-a')}
        need(fresh['data-a']!=original_volume,'fresh_standby_data_required')
        helper=self.create('container','--network','none','--user','0:0','--entrypoint','sh',
            *[arg for name,volume in fresh.items() for arg in ('-v',volume+':/fixture/'+name)],
            '-v',self.volumes['bootstrap-b']+':/fixture/bootstrap-b',self.pg,'-c','sleep 600')
        self.docker('start',helper)
        builder=load_builder()
        with tempfile.TemporaryDirectory(prefix='owned-reverse-closed-ha-') as name:
            temporary=Path(name).resolve()
            (temporary/'ca.crt').write_bytes(self.synthetic_ca[0]);(temporary/'ca.key').write_bytes(self.synthetic_ca[1])
            bootstrap=temporary/'bootstrap-a';private=temporary/'private-a';peer=temporary/'bootstrap-b'
            for directory in (bootstrap,private,peer):directory.mkdir(mode=0o700)
            for node,directory in (('A',bootstrap),('B',peer)):
                lock={'schema_version':2,'node':node,'primary_node':'B','source_node':'B' if node=='A' else None,
                    'pg_a':addresses['a'],'pg_b':addresses['b'],
                    'postgres':{'config_digest':self.pg,'uid':self.uid,'gid':self.gid},
                    'installation_id':hashlib.sha256((self.owner+':reverse').encode()).hexdigest(),'utility_config_digest':self.utility}
                for filename,raw in builder.pg_files(lock).items():
                    if filename in ('postgresql.conf','pg_hba.conf'):(directory/filename).write_text(raw)
            for filename in ('pg-entrypoint.sh','initialize.py'):
                (bootstrap/filename).write_bytes((ROOT/'infra/k8s/ha-package/bootstrap_assets'/filename).read_bytes())
            self.certificates(temporary,private,'a',addresses['a'])
            certificate=hashlib.sha256((private/'server.crt').read_bytes()).hexdigest()
            need(certificate!=self.original_a_certificate_sha256,'fresh_standby_certificate_required')
            (private/'postgres-password').write_text(secrets.token_urlsafe(32))
            (private/'repl.pgpass').write_text(''.join('ha-'+n+':5432:replication:ha_repl:'+self.synthetic_repl_password+'\n' for n in ('a','b')))
            self.stage(helper,bootstrap,'bootstrap-a');self.stage(helper,private,'private-a');self.stage(helper,peer,'bootstrap-b')
        self.docker('exec',helper,'sh','-ceu','chown -R '+str(self.uid)+':'+str(self.gid)+
            ' /fixture; chmod 700 /fixture/*; chmod 600 /fixture/private-a/*; chmod 444 /fixture/bootstrap-*/*')
        need(self.psql(self.b,'SELECT pg_reload_conf()')=='t')
        self.phase='reverse_fresh_a_basebackup';self.journal()
        new_a=self.start('a',volumes=fresh,addresses=addresses,primary='B')
        need(new_a!=self.a,'fresh_container_identity_required')
        self.wait(lambda:self.psql(new_a,'SELECT pg_is_in_recovery()')=='t')
        self.wait(lambda:self.psql(self.b,"SELECT count(*) FROM pg_stat_replication WHERE application_name='ha_a' AND state='streaming' AND sync_state='sync'")=='1')
        for container in (self.b,new_a):
            need(self.catalog_fingerprint(container)==expected,'reverse_catalog_hash_mismatch')
            need(self.psql(container,"SELECT NOT writes_open AND NOT history_safe AND expires_at<clock_timestamp() FROM ha.control",'ha')=='t')
            need(self.psql(container,"SELECT to_regprocedure('ha.local_roles(text[],text,boolean)') IS NULL",'ha')=='t')
        need(self.psql(self.b,"SELECT count(*) FROM pg_stat_ssl WHERE ssl AND version='TLSv1.3'")=='1')
        need(self.psql(self.b,"SELECT current_setting('synchronous_commit')")=='remote_apply')
        retired=self.owned('container',self.a)
        need(retired['State']['Running'] is False and retired['State']['Pid']==0,'retired_node_was_restarted')
        self.report['reverse_closed_pair']={'status':'PASS','old_owned_a_stopped':True,'old_a_data_reused':False,
            'old_a_rewound':False,'fresh_a_container':True,'fresh_a_data':True,'fresh_a_certificate':True,
            'primary_node':'B','standby_node':'A','physical_timeline':timeline,'tls13_replication':True,
            'synchronous_remote_apply':True,'logical_catalog_sha256':expected,'logical_authority_unchanged':True,
            'authority_primary_still_A':True,'new_roles_issued':False,'writer_authority_rebound':False,
            'windows_fence_verified':False,'effects_transition_performed':False,
            'operational_rejoin_verified':False}

    def diagnostics(self):
        result=[]
        for identifier in self.resources['container']:
            try:
                value=self.owned('container',identifier);logs=self.docker('logs',identifier)
                result.append({'exit_code':value['State']['ExitCode'],
                    'flags':{flag:flag.lower() in logs.lower() for flag in FLAGS}})
            except Exception:result.append({'unavailable':True})
        return result

    def cleanup(self):
        if not self.connected:return False
        self.phase='cleanup';clean=True
        try:self.journal()
        except Exception:clean=False
        # Recover successful create calls whose response/journal update was lost.
        for kind in ('container','volume','network'):
            try:
                args=('ps','--all','--quiet','--no-trunc') if kind=='container' else (kind,'ls','--quiet','--no-trunc') if kind=='network' else (kind,'ls','--quiet')
                identifiers=self.docker(*args,'--filter','label='+LABEL+'='+self.owner).splitlines()
                for identifier in identifiers:
                    self.owned(kind,identifier)
                    if identifier not in self.resources[kind]:self.resources[kind].append(identifier)
            except Exception:clean=False
        for kind in ('container','volume','network'):
            for identifier in reversed(self.resources[kind]):
                try:
                    self.owned(kind,identifier)
                    if kind=='container':self.docker('rm','--force',identifier)
                    else:self.docker(kind,'rm',identifier)
                except Exception:clean=False
        try:self.journal()
        except Exception:clean=False
        self.report['owned_fixture_removed']=clean
        return clean

    def run(self):
        try:
            self.validate_runtime();self.phase='private_fixture_setup';self.journal()
            self.prepare();self.acceptance()
            if self.reverse_closed_only:self.reverse_acceptance()
        except Exception as error:
            self.report.update(status='FAIL',phase=self.phase,
                reason=str(error) if isinstance(error,SmokeError) else 'fixture_failed')
        finally:
            self.report['diagnostics']=self.diagnostics() if self.connected else []
            if not self.cleanup():self.report.update(status='FAIL',owned_fixture_removed=False)
            (self.output/'result.json').write_bytes(canonical(self.report));(self.output/'result.json').chmod(0o600)
        return self.report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context',required=True);parser.add_argument('--daemon-id',required=True)
    parser.add_argument('--postgres-image',required=True);parser.add_argument('--utility-image',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--reverse-closed-only',action='store_true',
        help='Also stop owned A, promote owned closed B and clone an entirely fresh A; never rebind writer authority')
    args=parser.parse_args()
    try:
        validate_inputs(args.context,args.daemon_id,args.postgres_image,args.utility_image)
        output=private_output(args.output)
        report=Fixture(args.context,args.daemon_id,args.postgres_image,args.utility_image,output,reverse_closed_only=args.reverse_closed_only).run()
    except Exception as error:
        report={'status':'FAIL','reason':str(error) if isinstance(error,SmokeError) else 'fixture_setup_failed',
                'production_access':False,'providers_called':False}
    print(json.dumps(report,sort_keys=True))
    return int(report['status']!='PASS')


if __name__=='__main__':
    raise SystemExit(main())
