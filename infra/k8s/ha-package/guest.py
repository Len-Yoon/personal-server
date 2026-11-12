#!/usr/bin/env python3
"""Render an inert, exact-image guest layout. Never installs or starts anything."""
import argparse
import ipaddress
import json
from pathlib import Path
import re

SERVICES = ('portal-web','book-memo','youtube-memo','crawler-worker','car-care-worker','homeops-executor','system-agent')
PORTS = dict(zip(SERVICES,(8000,8003,8002,8001,8015,8011,8010)))
IMAGE = re.compile(r'(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64})')
HEX = re.compile(r'[0-9a-f]{64}')
ROOT = '/etc/n100-ha'
RUN = '/run/n100-ha'


class LayoutError(ValueError):
    pass


def require(value):
    if not value:
        raise LayoutError('guest_layout_invalid')


def mount(source,target,*,readonly=True):
    return {'type':'bind','source':source,'target':target,'read_only':readonly,
            'bind':{'create_host_path':False}}


def base(image,user):
    return {'image':image,'pull_policy':'never','user':user,'read_only':True,
            'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],
            'restart':'no','stop_grace_period':'10s','pids_limit':128,
            'logging':{'driver':'local','options':{'max-size':'5m','max-file':'2'}},
            'tmpfs':['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777']}


def render_ingress(guest_ip):
    """Render only the static, mutually authenticated HAProxy frontend."""
    require(type(guest_ip) is str)
    ip = ipaddress.ip_address(guest_ip)
    require(ip.version == 4 and ip.is_private and not (
        ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved))
    config=['global','  maxconn 128','  nbthread 1','  hard-stop-after 5s',
        '  ssl-default-bind-options ssl-min-ver TLSv1.3',
        'defaults','  mode http','  no log','  option http-server-close',
        '  timeout connect 2s','  timeout client 15s','  timeout server 15s',
        '  timeout http-request 5s','  timeout http-keep-alive 2s','  timeout queue 2s',
        'frontend fixed_ingress',
        f'  bind {guest_ip}:10443 ssl crt /etc/ha-ingress/server.pem ca-file /etc/ha-ingress/clients-ca.crt verify required',
        '  http-request deny deny_status 403 unless { req.hdr_cnt(host) eq 1 }']
    for service in SERVICES:
        config += [f'  acl host_{service} hdr(host),lower -m str {service}.ha.internal {service}.ha.internal:10443']
    config += ['  http-request deny deny_status 403 unless '+' or '.join('host_'+service for service in SERVICES)]
    for service in SERVICES:
        config += [f'  use_backend app_{service} if host_{service}']
    for service in SERVICES:
        config += [f'backend app_{service}',f'  server app unix@/run/ha-ingress/{service}/http.sock maxconn 16 maxqueue 16']
    return '\n'.join(config) + '\n'


def render_ingress_container(image):
    require(type(image) is str and IMAGE.fullmatch(image) is not None)
    ingress=base(image,'10004:10004')
    ingress.update(network_mode='host',entrypoint=['/usr/sbin/haproxy'],
        command=['-W','-db','-f','/etc/ha-ingress/haproxy.cfg'],
        volumes=[mount(ROOT+'/ingress','/etc/ha-ingress'),mount(RUN+'/ingress','/run/ha-ingress')])
    return ingress



def render(spec):
    require(type(spec) is dict and set(spec)=={'schema_version','source_commit','installation_id','node',
        'guest_ip','pg_a','pg_b','images','gateway_sha256','broker_sha256','docker_gid','app_environment'})
    require(type(spec['schema_version']) is int and spec['schema_version']==1)
    require(type(spec['source_commit']) is str and re.fullmatch('[0-9a-f]{40}',spec['source_commit']))
    require(type(spec['installation_id']) is str and HEX.fullmatch(spec['installation_id']))
    require(spec['node'] in {'A','B'})
    for name in ('gateway_sha256','broker_sha256'):
        require(type(spec[name]) is str and HEX.fullmatch(spec[name]))
    for name in ('guest_ip','pg_a','pg_b'):
        require(type(spec[name]) is str)
        ip=ipaddress.ip_address(spec[name])
        require(ip.version==4 and ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved))
    require(spec['pg_a']!=spec['pg_b'])
    images=spec['images']
    require(type(images) is dict and set(images)==set(SERVICES)|{'ha-backup-utility','ha-ingress-utility'})
    require(all(type(value) is str and IMAGE.fullmatch(value) for value in images.values()))
    require(type(spec['docker_gid']) is int and 1<=spec['docker_gid']<=65535 and spec['docker_gid'] not in {10001,10002,10003,10004})
    env=spec['app_environment']
    # Provider tokens and arbitrary executable/environment injection never enter apps.
    allowed={'car-care-worker':{'CAR_CARE_TELEGRAM_CHAT_ID'},
             'homeops-executor':{'HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256','HOMEOPS_DOCKER_MANAGED_SERVICES'}}
    require(type(env) is dict and set(env)<=set(allowed))
    for service,values in env.items():
        require(type(values) is dict and set(values)<=allowed[service])
        require(all(type(value) is str and 0<len(value)<=256 and not any(c in value for c in '\r\n\0') for value in values.values()))
    require(set(env.get('car-care-worker',{}))=={'CAR_CARE_TELEGRAM_CHAT_ID'})
    require(HEX.fullmatch(env.get('homeops-executor',{}).get('HOMEOPS_EXECUTOR_SHARED_SECRET_SHA256','')))
    managed=env['homeops-executor'].get('HOMEOPS_DOCKER_MANAGED_SERVICES','').split(',')
    require(managed and len(managed)==len(set(managed)) and set(managed)<=set(SERVICES)-{'car-care-worker'})
    services={}
    directories=[{'path':RUN+'/network','uid':10003,'gid':10001,'mode':'0750'}]
    for service in SERVICES:
        command=(['python','-m','app.main'] if service=='car-care-worker' else
                 ['uvicorn','app.main:app','--host','0.0.0.0','--port',str(PORTS[service])])
        app=base(images[service],'10001:10001')
        if service=='portal-web':
            # One supported 500 MiB archive + one 32 MiB snapshot + upload/spool overhead.
            # This is an on-demand ceiling, not a resident-memory reservation or concurrency proof.
            app['tmpfs']=['/tmp:rw,noexec,nosuid,nodev,size=640m,mode=1777']
        app.update(network_mode='none',group_add=['10004'],
            command=['python','-m','ha_egress.app_entrypoint','--service',service,'--app-port',str(PORTS[service]),'--',*command],
            environment={'APP_ENV':'production','HA_PG_CONFIG':'/etc/ha/config.json',
                'HA_PG_ACTIVATION':'/etc/ha/activation.json',**env.get(service,{}),
                **({'HA_HOMEOPS_TARGET_NODE':spec['node']} if service=='portal-web' else {}),
                **({'HOMEOPS_RUNTIME_STATE_PATH':'/etc/ha/runtime-ownership.conf'} if service=='homeops-executor' else {})},
            extra_hosts={'ha-a':'127.0.0.2','ha-b':'127.0.0.3','ha-broker':'127.0.0.4'},
            volumes=[mount(ROOT+'/apps/'+service,'/etc/ha'),
                mount(RUN+'/network/'+service,'/run/ha-network'),
                mount(RUN+'/ingress/'+service,'/run/ha-ingress',readonly=False)])
        services[service]=app
        directories += [{'path':RUN+'/network/'+service,'uid':10003,'gid':10001,'mode':'0750'},
                        {'path':RUN+'/ingress/'+service,'uid':10001,'gid':10004,'mode':'0750'}]
    gateway=base(images['ha-backup-utility'],'10003:10001')
    gateway.update(network_mode='host',command=['python','-m','ha_egress.socket_gateway','--config',
        '/etc/ha-gateway/config.json','--sha256',spec['gateway_sha256']],
        volumes=[mount(ROOT+'/gateway','/etc/ha-gateway'),mount(RUN+'/network','/run/ha-network',readonly=False)])
    services['ha-network']=gateway
    broker=base(images['ha-backup-utility'],'10002:10002')
    broker.update(network_mode='host',group_add=[str(spec['docker_gid'])],environment={'APP_ENV':'production'},
        extra_hosts={'ha-a':spec['pg_a'],'ha-b':spec['pg_b'],
                     **{service+'.ha.internal':spec['guest_ip'] for service in SERVICES}},
        command=['python','-m','ha_egress.boot','--config','/etc/ha-broker/config.json',
            '--expected-sha256',spec['broker_sha256'],'--authority','/etc/ha-broker/authority.json'],
        volumes=[mount(ROOT+'/broker','/etc/ha-broker'),mount('/var/run/docker.sock','/var/run/docker.sock')])
    services['ha-broker']=broker
    services['ha-ingress']=render_ingress_container(images['ha-ingress-utility'])
    sequence=['verify source/image/plan and installation identities',
        'provision fresh guest directories, immutable config, separate ingress CA and private keys',
        'request explicit operational approval before legacy shutdown, fencing, route or writer transition and issuance of installed authority',
        'complete approved legacy writer decommission and provider credential revocation or durable actor fencing; capture the final consistent snapshot',
        'on the fresh empty ha database, run initialize.py --phase migration with the externally pinned frozen manifest and a new private receipt; this creates closed control, imports all seven datasets and PG blobs, and installs guards/effects in one transaction',
        'never run the empty catalog phase before migration; do not delete or replace an existing catalog to bypass the fresh-target check',
        'verify the migration receipt and independently reconcile provider credentials and unresolved effects; migration issues no installed or writer authority',
        'start gateway; wait for all 21 fixed Unix sockets; start broker; start seven apps; start ingress',
        'verify Linux uid/network/mount/credential isolation and both PostgreSQL peers',
        'verify Windows Hpre + PostgreSQL authority/peer + Hpost total <=2 seconds',
        'verify closed-state application health and unresolved external effects before the approved writer open',
        'after the approved writer open and route transition verify every application readiness and external health; use forward recovery after accepted writes']
    review={'schema_version':1,'source_commit':spec['source_commit'],'installation_id':spec['installation_id'],
        'node':spec['node'],'runtime_ownership_file':'crawler-worker=compose\nyoutube-memo=compose\nbook-memo=compose\n','production_deployed':False,'operating_authority_issued':False,
        'ready_for_service_transition':False,'directories':directories,'sequence':sequence,
        'legacy_enrollment_verified':False,
        'enrollment_boundary':'Legacy decommission and exclusive provider credential ownership are trusted enrollment prerequisites; this offline renderer does not verify production actors or revoke their credentials.',
        'frontend_upstream_contract':{service:{'address':spec['guest_ip']+':10443',
            'http_host':service+'.ha.internal','tls_server_name':service+'.ha.internal',
            'tls_verify_required':True,'client_certificate_required':True} for service in SERVICES},
        'portal_scratch':'640 MiB on-demand tmpfs supports one default 500 MiB archive plus a 32 MiB snapshot and upload overhead. Concurrent peak memory and free space require target-host acceptance.',
        'docker_engine_requirements':{'storage_driver':'overlay2','image_store':'classic','configure_before_import':True},
        'ingress_key_requirements':{'server.pem':{'uid':10004,'gid':10004,'mode':'0400','contents':'server certificate chain and private key combined PEM'},'clients-ca.crt':{'uid':0,'mode':'0444'},'application_mounts_contain_ingress_keys':False},
        'ingress_health':'Static ingress is not writer readiness. Route activation requires fresh DB lease and all application readiness.',
        'rollback':'Before new writes, preserve V1 rollback. After accepted writes/effects, use forward recovery and reconcile uncertain effects.',
        'retired_guest':'Fenced old VM stays off. A fresh standby supplies synchronous replay; never restart the fenced writer to satisfy peer checks.'}
    return {'name':'n100-ha-'+spec['installation_id'][:12],'services':services},render_ingress(spec['guest_ip']),review


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    spec=json.loads(Path(args.spec).read_text())
    compose,haproxy,review=render(spec)
    output=Path(args.output);output.mkdir(mode=0o700,parents=False,exist_ok=False)
    for name,value in [('compose.json',compose),('review.json',review),('haproxy.cfg',haproxy)]:
        path=output/name
        path.write_text(value if isinstance(value,str) else json.dumps(value,sort_keys=True,indent=2)+'\n')
        path.chmod(0o444)


if __name__=='__main__':main()
