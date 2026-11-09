"""Explicit trusted broker startup. Templates never issue operating authority."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import threading
import time

from ha_runtime.config import SERVICES, _authority_file, _private, _release, validate_installed_authority
from ha_runtime.effects import Binding, Broker, Operation, TrustedIdentity
from .client import _pairs, LARGE_READ_OPERATIONS
from .providers import TelegramSender, TelegramPoller
from .docker_provider import DockerRestart, DockerInspect
from .executor_provider import ExecutorForwarder
from .hyundai_provider import HyundaiProvider
from .read_provider import PublicGet
from .internal_provider import InternalGet
from .server import BrokerServer


def deny():
    raise ValueError('broker_configuration_invalid')


def _file_stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,info.st_mode,
            info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def read_json(path, *, private=False):
    descriptor = None
    try:
        source = (_private if private else _authority_file)(path)
        verified = source.lstat()
        if verified.st_size > 1048576:
            deny()
        descriptor = os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        if _file_stamp(before) != _file_stamp(verified):
            deny()
        chunks=[];size=0
        while True:
            chunk=os.read(descriptor,min(65536,1048577-size))
            size+=len(chunk)
            if size>1048576:deny()
            if not chunk:break
            chunks.append(chunk)
        raw=b''.join(chunks)
        if (_file_stamp(before)!=_file_stamp(os.fstat(descriptor))
                or _file_stamp(before)!=_file_stamp(source.lstat())):
            deny()
        value=json.loads(raw,object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:deny()
        return raw,value
    except Exception:
        deny()
    finally:
        if descriptor is not None:os.close(descriptor)


class PinnedDispatch:
    """Recheck exact root-owned authority/config bytes around every dispatch.

    This blocks new dispatch/acknowledgement after a file change. It cannot
    recall an effect already sent by a previously admitted dispatch.
    """
    def __init__(self,broker,config_path,config_raw,authority_path,authority_raw):
        self.broker=broker
        self.config_path,self.authority_path=config_path,authority_path
        self.config_hash=hashlib.sha256(config_raw).hexdigest()
        self.authority_hash=hashlib.sha256(authority_raw).hexdigest()

    def _check(self):
        raw,config=read_json(self.config_path)
        authority_raw,authority=read_json(self.authority_path)
        if (hashlib.sha256(raw).hexdigest()!=self.config_hash
                or hashlib.sha256(authority_raw).hexdigest()!=self.authority_hash):deny()
        validate_authority(authority,config,self.config_hash)

    def __call__(self,identity,request):
        self._check()
        result=self.broker.dispatch(identity,request)
        self._check()
        return result


def validate_authority(value, config, config_hash):
    common = {'source_commit':config['release']['source_commit'], 'plan_sha256':config['binding']['plan_sha256'],
              'config_sha256':config_hash, 'action':'start-effect-broker'}
    if value.get('schema_version') == 3:
        if set(value) != set(common) | {'schema_version','authority_kind','installation_id','installed_at'}:
            deny()
        validate_installed_authority(value,config['installation_id'])
    else:
        if set(value) != set(common) | {'schema_version','expires_at'} or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
            deny()
        stamp = value['expires_at']
        if type(stamp) is not str or not stamp.endswith('Z'):
            deny()
        expires = datetime.fromisoformat(stamp.removesuffix('Z')+'+00:00')
        if not datetime.now(timezone.utc) < expires <= datetime.now(timezone.utc)+timedelta(hours=24):
            deny()
    if any(value.get(key) != expected for key,expected in common.items()):
        deny()


def tls_context(spec, *, server=False):
    if type(spec) is not dict or set(spec) != {'ca','certificate','key'}:
        deny()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if server else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(_authority_file(spec['ca'])))
    context.load_cert_chain(str(_authority_file(spec['certificate'])),str(_private(spec['key'])))
    return context


def pg_factory(endpoint, role):
    fields = {'host','port','dbname','user','sslmode','sslrootcert','passfile'}
    if (type(endpoint) is not dict or set(endpoint) != fields or endpoint['host'] not in {'ha-a','ha-b'}
            or endpoint['port'] != 5432 or endpoint['user'] != role or endpoint['sslmode'] != 'verify-full'
            or type(endpoint['dbname']) is not str or not endpoint['dbname']):
        deny()
    _authority_file(endpoint['sslrootcert']); _private(endpoint['passfile'])
    def connect():
        import psycopg
        return psycopg.connect(**endpoint,autocommit=True,connect_timeout=1,application_name='ha_effect_broker')
    return connect


def build(config):
    fields = {'schema_version','scope','profile','release','installation_id','node','generation','binding',
              'postgres','tls','clients','providers'}
    if (set(config) != fields or config['schema_version'] != 1 or type(config['schema_version']) is not int
            or config['scope'] != 'n100-approved' or config['profile'] != 'enforced-vm-v1'
            or config['release'] != _release() or config['node'] not in {'A','B'}
            or type(config['generation']) is not int or config['generation'] < 1
            or not re.fullmatch(r'[0-9a-f]{64}',str(config['installation_id']))
            or type(config['binding']) is not dict
            or set(config['binding']) != {'cluster','system_id','source_commit','plan_sha256','broker_role','broker_instance'}):
        deny()
    binding = Binding(**config['binding']); binding.validate()
    if binding.source_commit != config['release']['source_commit']:
        deny()
    postgres = config['postgres']
    if (type(postgres) is not dict or set(postgres) != {'primary','peer'}
            or any(type(postgres[key]) is not dict for key in ('primary','peer'))
            or postgres['primary'].get('host') == postgres['peer'].get('host')):
        deny()
    factory = pg_factory(postgres['primary'],binding.broker_role)
    peer_factory = pg_factory(postgres['peer'],binding.broker_role)
    if type(config['clients']) is not list or len(config['clients']) != 7:
        deny()
    identities = {}
    for item in config['clients']:
        if type(item) is not dict or set(item) != {'sha256','service','node','generation'}:
            deny()
        fingerprint = item['sha256']
        identity = TrustedIdentity(item['service'],item['node'],item['generation']); identity.validate()
        if (type(fingerprint) is not str or not re.fullmatch(r'[0-9a-f]{64}',fingerprint)
                or fingerprint in identities or identity.node != config['node']
                or identity.generation != config['generation']):
            deny()
        identities[fingerprint] = identity
    if {value.service for value in identities.values()} != SERVICES:
        deny()
    providers = config['providers']
    if type(providers) is not dict or set(providers) not in ({'telegram','hyundai','yes24','docker','executor'}, {'telegram','hyundai','yes24','docker','executor','internal'}):
        deny()
    operations,holder = {},{}
    telegram = providers['telegram']
    names = {'portal-web':'portal','crawler-worker':'crawler','car-care-worker':'car'}
    if telegram is not None:
        if type(telegram) is not dict or set(telegram) != set(names):
            deny()
        for service,prefix in names.items():
            if telegram[service] is None:
                continue
            _,secret = read_json(telegram[service],private=True)
            if set(secret) != {'token','chat_id'}:
                deny()
            token=secret['token']
            if type(token) is not str or not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}',token):deny()
            bot_id=str(int(token.split(':',1)[0]))
            resource = 'telegram:' + hashlib.sha256(bot_id.encode()).hexdigest()
            operations[prefix+'.telegram.send'] = Operation(service,resource,frozenset({'text'}),
                frozenset({'message_id'}),TelegramSender(**secret),timeout=8)
            if service == 'car-care-worker':
                operations['car.telegram.poll'] = Operation(service,resource,frozenset({'offset'}),
                    frozenset({'updates'}),TelegramPoller(**secret),timeout=3)
    hyundai = None
    if providers['hyundai'] is not None:
        _,hyundai = read_json(providers['hyundai'],private=True)
        required = {'client_id','client_secret','vehicle_id','redirect_uri'}
        if (set(hyundai) != required
                or any(type(hyundai[key]) is not str or not hyundai[key].strip() for key in required-{'vehicle_id'})
                or (hyundai['vehicle_id'] is not None and type(hyundai['vehicle_id']) is not str)):
            deny()
        hyundai['client_id']=hyundai['client_id'].strip()
        if not hyundai['client_id']:deny()
        hyundai['vehicle_id']=hyundai['vehicle_id'].strip() if hyundai['vehicle_id'] else None
        if not hyundai['vehicle_id']:hyundai['vehicle_id']=None
        # One configured car lane: discovery/secret rotation cannot change it.
        resource = 'hyundai:' + hashlib.sha256(('car-care-worker\0'+hyundai['client_id']).encode()).hexdigest()
        for name,payload_keys,result_keys in [('begin',set(),{'url'}),('complete',{'code','state'},{'connected'}),
                                              ('snapshot',set(),{'status','snapshot'})]:
            def invoke(payload,*,timeout,deadline,_name=name):
                return getattr(holder['hyundai'],_name)(payload,timeout=timeout,deadline=deadline)
            operations['car.hyundai.'+name] = Operation('car-care-worker',resource,frozenset(payload_keys),
                                                       frozenset(result_keys),invoke,timeout=10)
    yes24_key=None
    if providers['yes24'] is not None:
        _,yes24=read_json(providers['yes24'],private=True)
        if set(yes24)!={'api_key'} or type(yes24['api_key']) is not str or not yes24['api_key']:
            deny()
        yes24_key=yes24['api_key']
    for service,prefix in [('book-memo','book'),('youtube-memo','youtube'),('crawler-worker','crawler')]:
        operations[prefix+'.http.get'] = Operation(service,'public-get:'+prefix,frozenset({'url','headers'}),
            frozenset({'status','headers','body_b64'}),PublicGet(service,yes24_key=yes24_key if prefix=='book' else None),
            timeout=10,read_only=True)
    docker = providers['docker']
    if docker is not None:
        if (type(docker) is not dict or set(docker) != {'socket_path','engine_id','project','targets'}
                or type(docker['targets']) is not dict or not docker['targets'] or not set(docker['targets']) <= SERVICES
                or type(docker['engine_id']) is not str or not 1 <= len(docker['engine_id']) <= 128):
            deny()
        resource = 'docker:' + hashlib.sha256(docker['engine_id'].encode()).hexdigest()
        for service,target in docker['targets'].items():
            if type(target) is not dict or set(target) != {'container_id','image_id'}:
                deny()
            for action,adapter in [('restart',DockerRestart),('inspect',DockerInspect)]:
                operations['executor.docker.'+action+'.'+service] = Operation('homeops-executor',resource,frozenset(),
                    frozenset({'service','status','health','started_at'}),
                    adapter(docker['socket_path'],target['container_id'],target['image_id'],docker['project'],service),
                    timeout=10,read_only=action=='inspect')
    executor = providers['executor']
    if executor is not None:
        if type(executor) is not dict or set(executor) != {'url','secret_file','tls','server_sha256','services'}:
            deny()
        _,secret = read_json(executor['secret_file'],private=True)
        if set(secret) != {'secret'}:
            deny()
        forward = ExecutorForwarder(url=executor['url'],node=config['node'],secret=secret['secret'],
            context=tls_context(executor['tls']),server_sha256=executor['server_sha256'],services=executor['services'])
        resource = 'executor-route:' + hashlib.sha256(executor['url'].encode()).hexdigest()
        for name,payload_keys in [('diagnose',{'service'}),('restart',{'incident_id','approval_token','service'}),
                                  ('restart_all',{'request_id'})]:
            operations['portal.executor.'+name] = Operation('portal-web',resource,frozenset(payload_keys),
                frozenset({'value','managed_services'}),getattr(forward,name),timeout=10,read_only=name=='diagnose')
    internal = providers.get('internal')
    if internal is not None:
        if type(internal) is not dict or set(internal) != {'endpoints','tls'}:
            deny()
        operations['portal.internal.get'] = Operation('portal-web','internal-read:'+config['node'],
            frozenset({'target','path','query'}),frozenset({'value'}),
            InternalGet(internal['endpoints'],context=tls_context(internal['tls']),node=config['node']),
            timeout=2,read_only=True)
    broker = Broker(factory,binding,operations,peer_factory=peer_factory)
    if hyundai is not None:
        holder['hyundai'] = HyundaiProvider(broker,**hyundai)
    return broker,identities,tls_context(config['tls'],server=True)


def internal_dispatch(dispatch):
    def invoke(identity,request):
        if (type(identity) is not TrustedIdentity or identity.service!='homeops-executor'
                or type(request) is not dict or type(request.get('operation')) is not str
                or not request['operation'].startswith(('executor.docker.inspect.','executor.docker.restart.'))):
            deny()
        return dispatch(identity,request)
    return invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--authority',required=True)
    args = parser.parse_args()
    servers=[];started=[];previous={};stopped=threading.Event();failures=[]
    try:
        if os.geteuid() != 10002 or os.getenv('APP_ENV') != 'production' or 'HA_PG_CONFIG' in os.environ:
            deny()
        raw,config = read_json(args.config)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != args.expected_sha256:
            deny()
        authority_raw,authority = read_json(args.authority)
        validate_authority(authority,config,digest)
        broker,identities,context = build(config)
        dispatch=PinnedDispatch(broker,args.config,raw,args.authority,authority_raw)
        dispatch._check()
        public={key:value for key,value in identities.items() if value.service!='homeops-executor'}
        internal={key:value for key,value in identities.items() if value.service=='homeops-executor'}
        if len(public)!=6 or len(internal)!=1:deny()
        servers.append(BrokerServer(('127.0.0.1',8443),context,public,dispatch,
            plan_sha256=broker.binding.plan_sha256,
            large_read_operations={name for name in LARGE_READ_OPERATIONS if name in broker.operations and broker.operations[name].read_only}))
        servers.append(BrokerServer(('127.0.0.1',8444),context,internal,internal_dispatch(dispatch),
            plan_sha256=broker.binding.plan_sha256,workers=2))
        def stop(*_):stopped.set()
        for kind in (signal.SIGTERM,signal.SIGINT):previous[kind]=signal.signal(kind,stop)
        def serve(server):
            try:
                server.serve_forever(poll_interval=.1)
                if not stopped.is_set():failures.append(True)
            except Exception:failures.append(True)
            finally:stopped.set()
        for server in servers:
            thread=threading.Thread(target=serve,args=(server,),daemon=True)
            thread.start();started.append((server,thread))
        stopped.wait()
        if failures:deny()
    except Exception:
        print('effect_broker_start_blocked',file=sys.stderr)
        return 1
    finally:
        stopped.set();cleanup_failed=False
        for server,thread in started:
            try:
                if thread.is_alive():server.shutdown()
            except Exception:cleanup_failed=True
        for _,thread in started:
            try:thread.join(timeout=2)
            except Exception:cleanup_failed=True
        for server in servers:
            try:server.server_close()
            except Exception:cleanup_failed=True
        # Existing HTTP handlers have their own four-second cancellation timer.
        # Wait within one total budget; never retry an unresolved provider call.
        deadline=time.monotonic()+5
        for server in servers:
            try:
                with server.active_lock:active=tuple(server.active)
                for worker in active:worker.join(max(0,deadline-time.monotonic()))
            except Exception:cleanup_failed=True
        for kind,handler in previous.items():
            try:signal.signal(kind,handler)
            except Exception:cleanup_failed=True
        if cleanup_failed:
            print('effect_broker_start_blocked',file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
