#!/usr/bin/env python3
"""Trusted native evidence adapter. Raw addresses/socket/logs stay in memory.

Only owned disposable nonwriters are created. Runtime fixed addresses are
intentionally configured elsewhere; no raw address is persisted in evidence.
This adapter grants no deployment authority; a reviewed controller owns policy
application, sequence locking and protected-resource gates.
"""
import concurrent.futures
import hashlib
import ipaddress
import json
import re
import secrets
import struct
import time
from datetime import datetime, timezone

PORTS = {'book-memo':8003,'youtube-memo':8002,'crawler-worker':8001,'portal-web':8000}
KEYS = {'book-memo':'BOOK_MEMO_UPSTREAM','youtube-memo':'YOUTUBE_MEMO_UPSTREAM','crawler-worker':'CRAWLER_WORKER_UPSTREAM','portal-web':'PORTAL_UPSTREAM'}
OWNER = 'len.pe.kr/fixed-ingress-evidence'
BUSYBOX = 'docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662'
KUBE = ['sudo','-n','k3s','kubectl','--request-timeout=15s']

class Blocked(Exception): pass

def require(ok, reason):
    if not ok: raise Blocked(reason)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def stamp(): return datetime.now(timezone.utc).isoformat()

def socket_record(raw, target, port, command='nc'):
    """Exact PID birth/inode/socket binding from one still-held client."""
    rows = {}
    for line in raw.splitlines():
        if '=' not in line: continue
        key,value=line.split('=',1)
        if key in {'P','S1','S2','I','R'}:
            require(key not in rows,'ambiguous_socket'); rows[key]=value
    require(set(rows)=={'P','S1','S2','I','R'},'missing_socket')
    require(rows['P'].isdigit() and rows['I'].isdigit(),'socket_identity')
    pid=int(rows['P']); inode=int(rows['I']); starts=[]
    for key in ('S1','S2'):
        value=rows[key]; require(value.startswith(str(pid)+' ('+command+') '),'client_process_identity')
        fields=value[value.rfind(')')+2:].split();require(len(fields)>=20 and fields[19].isdigit(),'client_starttime');starts.append(fields[19])
    require(starts[0]==starts[1] and pid>1 and inode>0,'client_process_reused')
    fields=rows['R'].split();require(len(fields)>=10 and fields[3]=='01' and fields[9]==str(inode),'client_inode_binding')
    def endpoint(value):
        a,p=value.split(':');require(len(a)==8,'ipv4_socket_required')
        return str(ipaddress.IPv4Address(struct.pack('<I',int(a,16)))),int(p,16)
    source,sp=endpoint(fields[1]);dest,dp=endpoint(fields[2]);require(dest==target and dp==port and sp>0,'socket_destination')
    statuses=re.findall(r'^HTTP/1\.[01] ([0-9]{3})(?:\s|$)',raw,re.M)
    require(statuses==['200'],'same_connection_http_status')
    return source,sp


def held_client_script(marker):
    require(re.fullmatch('[a-f0-9]{12}',marker) is not None,'invalid_marker')
    # Last pipeline member is the direct nc child, whose PID/inode are read.
    # Delayed final CRLF holds this one connection before the read-only GET.
    return r'''set -u
(printf 'GET /health?codex_fixed_probe='''+marker+r''' HTTP/1.1\r\nHost: codex.invalid\r\nConnection: close\r\n'; sleep 2; printf '\r\n') | nc -p "$3" -w 4 "$1" "$2" &
p=$!
i=0
while [ "$i" -lt 40 ]; do
 i=$((i+1))
 s1=$(cat "/proc/$p/stat" 2>/dev/null) || break
 for f in /proc/"$p"/fd/*; do
  link=$(readlink "$f" 2>/dev/null) || continue
  case "$link" in 'socket:['*']') inode=${link#socket:[}; inode=${inode%]}; row=$(awk -v inode="$inode" '$4=="01" && $10==inode {print}' "/proc/$p/net/tcp");;
   *) continue;; esac
  if [ -n "$row" ]; then
   s2=$(cat "/proc/$p/stat") || exit 72
   printf 'P=%s\nS1=%s\nI=%s\nR=%s\nS2=%s\n' "$p" "$s1" "$inode" "$row" "$s2"
   wait "$p"; exit $?
  fi
 done
 sleep .025
done
wait "$p"; exit 73
'''


def match_access(raw, marker, source_port, started):
    path='/health?codex_fixed_probe='+marker
    lines=[line for line in raw.splitlines() if path in line]
    require(len(lines)==1,'exact_marker_log_required')
    pattern=r'(?P<address>\S+):(?P<port>[0-9]+) - "GET '+re.escape(path)+r' HTTP/1\.[01]" 200(?:\s|$)'
    match=re.search(pattern,lines[0]); require(match is not None and int(match['port'])==source_port,'socket_marker_port_binding')
    observed=datetime.fromisoformat(lines[0].split()[0].replace('Z','+00:00'))
    require(observed.tzinfo is not None and started.timestamp()-1<=observed.timestamp()<=time.time()+1,'marker_log_time')
    return str(ipaddress.ip_address(match['address'].strip('[]')))


def tcp_connections(raw):
    result=set()
    for line in raw:
        fields=line.split()
        if len(fields)<10 or fields[3]!='01':continue
        def endpoint(value):
            address,port=value.split(':')
            if len(address)!=8:return None,None
            return str(ipaddress.IPv4Address(struct.pack('<I',int(address,16)))),int(port,16)
        local,lp=endpoint(fields[1]);source,sp=endpoint(fields[2])
        if local:result.add((local,lp,source,sp))
    return result



SERVER = r'''
import http.server,json,threading,time,re
records={};lock=threading.Lock()
class H(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  if self.server.server_port==8081:
   if self.client_address[0]!='127.0.0.1':self.send_error(403);return
   with lock:body=json.dumps(records.get(self.path,{})).encode()
  else:
   with lock:records[self.path]={'count':records.get(self.path,{}).get('count',0)+1,'peer':list(self.client_address)}
   body=b'ok'
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
for port in (8000,8001,8002,8003,8081):
 threading.Thread(target=http.server.ThreadingHTTPServer(('127.0.0.1' if port==8081 else '0.0.0.0',port),H).serve_forever,daemon=True).start()
threading.Event().wait()
'''


class Collector:
    """Parent supplies a successful reviewed transition and append-only journal.

    Calls into backend are captured in memory, not forwarded to the user.
    The journal callback accepts hashes/UIDs/status only and fsyncs them.
    """
    def __init__(self, transition_backend, prepared, journal):
        self.transition=transition_backend;self.prepared=prepared;self.journal=journal
        self.run=secrets.token_hex(6);self.ns='fixed-ingress-'+self.run
        self.records={};self.pending={};self.diagnostics={};self.created=False;self.bound=None;self.children=[];self.inventories={}

    def run_command(self,b,args,timeout=30,input_value=None):
        value=b.run(args,timeout=timeout,data=None if input_value is None else json.loads(input_value))
        return value.decode() if isinstance(value,bytes) else value

    def api(self,b,*args,input_value=None):
        return json.loads(self.run_command(b,KUBE+list(args)+['-o','json'],input_value=input_value))

    def emit(self,event,**fields): self.journal(event,**fields)

    def caddy(self): return self.transition.caddy()

    def identity(self,b):
        c=self.caddy();p=self.prepared
        require(c['State']['Running'] is True and c['Image']==p['image_id'],'fixed_caddy_image')
        network=self.transition.owned_network()
        require(network is not None and network['Labels'].get('pe.kr.len.caddy-ingress-owner')==p['owner_id'],'fixed_network_owner')
        endpoint=c['NetworkSettings']['Networks'].get(p['network_name'])
        require(endpoint and endpoint['IPAddress']==p['address']['fixed_ip'] and endpoint.get('IPAMConfig',{}).get('IPv4Address')==p['address']['fixed_ip'],'fixed_endpoint_configuration')
        # Native transition verifies exact original shared network, other
        # containers, runtime files, image, security, mounts and route sources.
        self.transition.verify(p,rollback=False)
        identity={'container_id_sha256':digest(c['Id']),'container_image_sha256':c['Image'],'started_at':c['State']['StartedAt'],'network_settings_sha256':digest(c['NetworkSettings']['Networks'])}
        config={k:True for k in ('external_owned_bridge','explicit_ipam','configured_static_address','endpoint_matches_configuration','address_outside_dynamic_range','non_overlapping_ranges','shared_bridge_unchanged','same_image_preserved')}
        config.update(network_id_sha256=digest(network['Id']),network_spec_sha256=digest({k:network.get(k) for k in ('Driver','IPAM','Labels','Options','Internal','EnableIPv6')}),ipam_sha256=digest(network['IPAM']),runtime_config_sha256=digest({'bridge':p['target_bridge_sha256'],'env':p['target_env_sha256']}),owner_sha256=digest(p['owner_id']),observed_at=stamp(),source_sha256=digest(endpoint['IPAddress']),current_identity_sha256=digest(identity))
        stable={k:v for k,v in config.items() if k!='observed_at'}
        current=(identity,stable)
        require(self.bound is None or self.bound==current,'fixed_identity_drift');self.bound=current
        return {'current_identity':identity,'fixed_configuration':config,'peer':{'ipBlock':{'cidr':endpoint['IPAddress']+'/32'}}}

    def target(self,b,app):
        dep=self.api(b,'-n','personal-server','get','deployment',app)
        pods=self.api(b,'-n','personal-server','get','pods','-l','app.kubernetes.io/name='+app)['items']
        pods=[p for p in pods if p.get('status',{}).get('phase') not in ('Succeeded','Failed')]
        require(len(pods)==1,'single_target_pod');pod=pods[0]
        statuses=pod['status'].get('containerStatuses',[])
        require(not pod['metadata'].get('deletionTimestamp') and len(statuses)==1 and statuses[0]['ready'] is True and statuses[0]['restartCount']==0,'target_not_ready')
        svc=self.api(b,'-n','personal-server','get','service',app)
        slices=self.api(b,'-n','personal-server','get','endpointslices','-l','kubernetes.io/service-name='+app)['items']
        eps=[e for s in slices for e in s.get('endpoints',[]) if e.get('conditions',{}).get('ready')]
        require(len(eps)==1 and eps[0].get('targetRef',{}).get('uid')==pod['metadata']['uid'] and eps[0]['addresses']==[pod['status']['podIP']],'target_endpoint_binding')
        nodes=self.api(b,'get','nodes')['items'];node=next(n for n in nodes if n['metadata']['name']==pod['spec']['nodeName'])
        require(len(svc['spec']['ports'])==1 and svc['spec']['ports'][0]['port']==PORTS[app],'target_service_port')
        return dep,pod,svc,node

    def endpoint(self,b,app,pod,svc):
        slices=self.api(b,'-n','personal-server','get','endpointslices','-l','kubernetes.io/service-name='+app)['items']
        require(len(slices)==1,'single_endpoint_slice_required')
        ep=slices[0];items=ep.get('endpoints',[])
        require(len(items)==1 and items[0].get('targetRef',{}).get('uid')==pod['metadata']['uid']
                and items[0].get('conditions',{}).get('ready') is True
                and items[0].get('addresses')==[pod['status']['podIP']]
                and any(o.get('uid')==svc['metadata']['uid'] and o.get('controller') is True for o in ep['metadata'].get('ownerReferences',[])),'current_service_endpoint_binding')
        require(ep.get('ports')==[{'name':svc['spec']['ports'][0]['name'],'port':PORTS[app],'protocol':'TCP'}],'current_endpoint_port')
        return digest({'uid':ep['metadata']['uid'],'ports':ep['ports'],'endpoints':items,'pod_uid':pod['metadata']['uid'],'pod_spec_sha256':digest(pod['spec']),'service_uid':svc['metadata']['uid'],'service_spec_sha256':digest(svc['spec'])})

    def target_sockets(self,b,pod,seconds=3):
        code="import pathlib,time,json; rows=set(); end=time.monotonic()+"+str(seconds)+"\nwhile time.monotonic()<end:\n rows.update(pathlib.Path('/proc/net/tcp').read_text().splitlines());time.sleep(.01)\nprint(json.dumps(sorted(rows)))"
        rows=json.loads(self.run_command(b,KUBE+['-n','personal-server','exec',pod['metadata']['name'],'--','python','-c',code],timeout=seconds+8))
        return tcp_connections(rows)

    def request(self,b,app,pod,svc,identity):
        c=self.caddy();env=dict(x.split('=',1) for x in c['Config']['Env'] if '=' in x)
        actual=env[KEYS[app]].removeprefix('http://')
        require(actual==svc['spec']['clusterIP']+':'+str(PORTS[app]),'current_clusterip_upstream_required')
        endpoint=self.endpoint(b,app,pod,svc);marker=secrets.token_hex(6)
        before=self.target_sockets(b,pod,.1)
        client_rows=self.run_command(b,['docker','exec',c['Id'],'cat','/proc/net/tcp']).splitlines()
        used={row[1] for row in tcp_connections(client_rows)}|{row[3] for row in before if row[1]==PORTS[app]}
        free=[p for p in range(49152,65536) if p not in used];require(free,'no_probe_port_available');chosen=secrets.choice(free)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.target_sockets,b,pod,3)
            raw=self.run_command(b,['docker','exec',c['Id'],'sh','-c',held_client_script(marker),'probe',svc['spec']['clusterIP'],str(PORTS[app]),str(chosen)],timeout=10)
            source,source_port=socket_record(raw,svc['spec']['clusterIP'],PORTS[app])
            sockets=future.result()
        require(source==self.prepared['address']['fixed_ip'] and source_port==chosen,'current_route_fixed_source')
        matches={row for row in sockets if row[0]==pod['status']['podIP'] and row[1]==PORTS[app] and row[3]==source_port}
        require(len(matches)==1,'current_exact_server_socket_required')
        _,current,after,_=self.target(b,app)
        require(self.endpoint(b,app,current,after)==endpoint,'request_target_endpoint_changed')
        require(digest(self.identity(b)['current_identity'])==digest(identity),'request_caddy_identity_drift')
        return {'status':200,'port':PORTS[app],'same_connection_bound':True,'pid_start_inode_bound':True,
                'proof_method':'endpoint_socket_http','http_marker_bound':False,'server_endpoint_socket_bound':True,
                'source_matches_current_endpoint':True,'target_endpoint_unchanged':True,'target_endpoint_sha256':endpoint}

    def current(self,b,kind,name):
        args=[] if kind=='namespace' else ['-n',self.ns]
        items=self.api(b,*args,'get',kind,'--field-selector','metadata.name='+name)['items']
        require(len(items)<=1,'owned_resource_ambiguous');return items[0] if items else None

    def create(self,b,obj):
        kind=obj['kind'].lower();name=obj['metadata']['name'];require(self.current(b,kind,name) is None,'disposable_preexists')
        obj['metadata'].setdefault('labels',{})[OWNER]=self.run
        self.emit('disposable_create_intent',kind=kind,name=name,intent_sha256=digest(obj))
        self.pending[(kind,name)]=json.loads(json.dumps(obj))
        actual=None
        try:actual=self.api(b,'create','-f','-',input_value=json.dumps(obj))
        except Exception:actual=self.current(b,kind,name)
        self.freeze_created(obj,actual)
        def subset(a,e):
            if isinstance(e,dict):return isinstance(a,dict) and all(k in a and subset(a[k],v) for k,v in e.items())
            if isinstance(e,list):return isinstance(a,list) and len(a)==len(e) and all(subset(x,y) for x,y in zip(a,e))
            return a==e
        if kind=='networkpolicy' and obj['spec'].get('ingress')==[] and 'ingress' not in actual['spec']:
            obj=dict(obj,spec={k:v for k,v in obj['spec'].items() if k!='ingress'})
        require(subset(actual,obj),'disposable_intent_drift')
        if kind=='pod':self.validate_nonwriter(actual)
        return actual

    def freeze_created(self,intent,actual):
        kind=intent['kind'].lower();name=intent['metadata']['name']
        meta=(actual or {}).get('metadata',{})
        require(actual is not None and meta.get('labels',{}).get(OWNER)==self.run,
                'disposable_create_unknown')
        require(actual.get('kind')==intent['kind'] and meta.get('name')==name and
                meta.get('namespace')==intent['metadata'].get('namespace') and
                bool(meta.get('uid')) and not meta.get('deletionTimestamp'),'disposable_kind_drift')
        # Freeze the exact response before admission validation, so cleanup can
        # delete only this owned UID/spec. Absence never cancels a pending write.
        record={'kind':kind,'name':name,'uid':meta['uid'],'spec_sha256':digest(actual.get('spec',{}))}
        if kind=='pod':record['scheduling_spec_sha256']=digest({k:v for k,v in actual['spec'].items() if k!='nodeName'})
        previous=self.records.get((kind,name))
        require(previous is None or previous==record,'disposable_create_replaced')
        self.records[(kind,name)]=record
        if kind=='namespace':self.created=True
        self.emit('disposable_created',**record)
        self.pending.pop((kind,name),None)

    def reconcile_pending(self,b):
        for (kind,name),intent in list(self.pending.items()):
            self.freeze_created(intent,self.current(b,kind,name))

    def delete(self,b,kind,name):
        obj=self.current(b,kind,name)
        if obj is None:return
        record=self.records.get((kind,name));require(record is not None,'unowned_delete_forbidden')
        if kind=='pod' and digest(obj.get('spec',{}))!=record['spec_sha256']:
            self.validate_nonwriter(obj)
            require(digest({k:v for k,v in obj['spec'].items() if k!='nodeName'})==record.get('scheduling_spec_sha256'),'owned_pod_scheduling_drift')
            record['spec_sha256']=digest(obj['spec']);self.emit('disposable_scheduling_reconciled',**record)
        require(obj['metadata']['uid']==record['uid'] and obj['metadata'].get('labels',{}).get(OWNER)==self.run and digest(obj.get('spec',{}))==record['spec_sha256'],'owned_delete_drift')
        plural={'namespace':'namespaces','pod':'pods','service':'services','networkpolicy':'networkpolicies'}[kind]
        uri='/api/v1/'+plural+'/'+name if kind=='namespace' else ('/apis/networking.k8s.io/v1/' if kind=='networkpolicy' else '/api/v1/')+'namespaces/'+self.ns+'/'+plural+'/'+name
        body={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':record['uid'],'resourceVersion':obj['metadata']['resourceVersion']},'propagationPolicy':'Foreground'}
        self.emit('disposable_delete_intent',**record)
        try:self.run_command(b,KUBE+['delete','--raw',uri,'-f','-'],input_value=json.dumps(body))
        except Exception:
            observed=self.current(b,kind,name);require(observed is None or observed['metadata']['uid']==record['uid'] and bool(observed['metadata'].get('deletionTimestamp')),'disposable_delete_unknown')
        deadline=time.monotonic()+30
        while self.current(b,kind,name) is not None:
            require(time.monotonic()<deadline,'disposable_delete_timeout');time.sleep(.5)
        self.emit('disposable_deleted',**record)

    def validate_nonwriter(self,pod):
        spec=pod['spec']
        allowed={'containers','restartPolicy','automountServiceAccountToken','enableServiceLinks','securityContext','nodeName','schedulerName','dnsPolicy','terminationGracePeriodSeconds','preemptionPolicy','tolerations','serviceAccountName','serviceAccount'}
        require(set(spec)<=allowed and not spec.get('volumes') and spec.get('automountServiceAccountToken') is False and spec.get('enableServiceLinks') is False,'nonwriter_admission_boundary')
        require(len(spec['containers'])==1,'nonwriter_container_count')
        container=spec['containers'][0]
        require(not any(container.get(k) for k in ('env','envFrom','volumeMounts','volumeDevices','lifecycle','ports','livenessProbe','readinessProbe','startupProbe')),'nonwriter_container_admission')
        require(set(container)<= {'name','image','imagePullPolicy','command','securityContext','resources','terminationMessagePath','terminationMessagePolicy'},'nonwriter_container_fields')
        require(spec.get('restartPolicy')=='Never' and container.get('imagePullPolicy')=='Never','nonwriter_execution_contract')
        require(container['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'nonwriter_container_security')
        require(spec['securityContext']=={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},'nonwriter_pod_security')

    def ready(self,b,name):
        deadline=time.monotonic()+45
        while True:
            p=self.current(b,'pod',name);require(p and p['metadata']['uid']==self.records[('pod',name)]['uid'],'owned_pod_uid')
            self.validate_nonwriter(p)
            require(digest({k:v for k,v in p['spec'].items() if k!='nodeName'})==self.records[('pod',name)]['scheduling_spec_sha256'],'nonwriter_scheduling_drift')
            cs=p.get('status',{}).get('containerStatuses',[])
            if p.get('status',{}).get('phase')=='Running' and len(cs)==1 and cs[0].get('ready') and cs[0]['restartCount']==0:
                # Admission scheduling defaults become the frozen exact spec.
                self.records[('pod',name)]['spec_sha256']=digest(p['spec']);self.emit('disposable_ready',**self.records[('pod',name)]);return p
            require(time.monotonic()<deadline,'disposable_ready_timeout');time.sleep(.5)

    def namespace_inventory(self,b):
        kinds=self.run_command(b,KUBE+['api-resources','--namespaced=true','--verbs=list','-o','name']).splitlines()
        require(kinds and len(set(kinds))==len(kinds),'namespace_resource_inventory')
        defaults=[]
        for kind in kinds:
            raw=self.run_command(b,KUBE+['-n',self.ns,'get',kind,'-o=custom-columns=NAME:.metadata.name,UID:.metadata.uid,RV:.metadata.resourceVersion','--no-headers'])
            rows=[line.split() for line in raw.splitlines()]
            require(all(len(r)==3 and '<none>' not in r for r in rows),'namespace_metadata_inventory')
            base=kind.split('.')[0]
            for name,uid,rv in rows:
                if base=='events':continue
                if (base,name) in {('serviceaccounts','default'),('configmaps','kube-root-ca.crt')}:
                    defaults.append({'kind':kind,'name':name,'uid':uid,'rv':rv});continue
                record_kind={'pods':'pod','services':'service','networkpolicies':'networkpolicy'}.get(base)
                if record_kind and (record_kind,name) in self.records:
                    require(self.records[(record_kind,name)]['uid']==uid,'disposable_inventory_uid_drift');continue
                if base in ('endpointslices','endpoints'):
                    obj=self.api(b,'-n',self.ns,'get',kind,name)
                    service=self.records.get(('service','server'));server=self.records.get(('pod','server'))
                    require(service and server,'unowned_endpoint_inventory')
                    if base=='endpointslices':
                        require(any(o.get('uid')==service['uid'] and o.get('controller') is True for o in obj['metadata'].get('ownerReferences',[])) and all(e.get('targetRef',{}).get('uid')==server['uid'] for e in obj.get('endpoints',[])),'unowned_endpoint_inventory')
                    else:
                        require(name=='server' and all(a.get('targetRef',{}).get('uid')==server['uid'] for subset in obj.get('subsets',[]) for a in subset.get('addresses',[])+subset.get('notReadyAddresses',[])),'unowned_endpoint_inventory')
                    continue
                raise Blocked('unexpected_namespaced_resource')
        defaults=sorted(defaults,key=lambda x:x['kind'])
        if hasattr(self,'defaults'):require(defaults==self.defaults,'namespace_defaults_drift')
        return defaults

    def setup(self,b):
        if self.created:return
        dep,_,_,_=self.target(b,'book-memo');image=dep['spec']['template']['spec']['containers'][0]['image']
        aliases=self.run_command(b,['sudo','-n','k3s','ctr','-n','k8s.io','images','list','--quiet']).splitlines()
        require(image in aliases and BUSYBOX in aliases,'diagnostic_images_not_imported')
        self.create(b,{'apiVersion':'v1','kind':'Namespace','metadata':{'name':self.ns}});self.created=True
        deadline=time.monotonic()+20
        while True:
            defaults=self.namespace_inventory(b)
            if {x['name'] for x in defaults}=={'default','kube-root-ca.crt'}:break
            require(time.monotonic()<deadline,'namespace_defaults_timeout');time.sleep(.5)
        self.defaults=defaults;self.emit('namespace_defaults',defaults_sha256=digest(defaults),namespace_uid=self.records[('namespace',self.ns)]['uid'])
        for name,img,cmd in [('server',image,['python','-I','-B','-c',SERVER]),('unknown',BUSYBOX,['sh','-c','sleep 3600'])]:
            self.create(b,{'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':self.ns,'labels':{'app':'fixed-'+name}},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'enableServiceLinks':False,'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':name,'image':img,'imagePullPolicy':'Never','command':cmd,'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'200m','memory':'128Mi'}}}]}})
        self.create(b,{'apiVersion':'v1','kind':'Service','metadata':{'name':'server','namespace':self.ns},'spec':{'type':'ClusterIP','selector':{'app':'fixed-server'},'ports':[{'name':'http-'+str(p),'port':p,'targetPort':p,'protocol':'TCP'} for p in PORTS.values()]}})
        self.ready(b,'server');self.ready(b,'unknown')

    def probe(self,b,source,address,port,path='/health'):
        marker=secrets.token_hex(6);url='http://'+address+':'+str(port)+path+('?m='+marker if '?' not in path else '&m='+marker)
        prefix=['docker','exec',self.caddy()['Id']] if source=='caddy' else KUBE+['-n',self.ns,'exec','unknown','--']
        script='wget -S -T 3 -O /dev/null "$1" 2>&1; r=$?; printf "\\nPROBE_RC=%s\\n" "$r"'
        raw=self.run_command(b,prefix+['sh','-c',script,'probe',url],timeout=8)
        codes=re.findall(r'HTTP/1\.[01] ([0-9]{3})',raw);rc=re.findall(r'PROBE_RC=([0-9]+)',raw)
        require(len(rc)==1,'probe_transport_contract');code=int(rc[0]);status=int(codes[-1]) if codes else None
        reason='none' if code==0 and status==200 else 'timeout' if 'timed out' in raw.lower() else 'refused' if 'refused' in raw.lower() else 'unreachable' if 'unreachable' in raw.lower() else None
        require(reason is not None and (code==0 and status==200 or code==1 and status is None),'classified_probe_required')
        return {'transport_ok':True,'http_status':status,'exit_code':code,'reason':reason}

    def diagnostic(self,b,app,identity,config,peer,node):
        child=Collector(self.transition,self.prepared,self.journal);self.children.append(child)
        try:return child._diagnostic(b,app,identity,config,peer,node)
        finally:
            child.cleanup(b)
            self.children.remove(child)

    def _diagnostic(self,b,app,identity,config,peer,node):
        b.source_guard();baseline=b.guards()
        self.setup(b);server=self.ready(b,'server');svc=self.current(b,'service','server');port=PORTS[app]
        require(server['spec'].get('nodeName')==node['metadata']['name'],'diagnostic_target_node_mismatch')
        actual_node=self.api(b,'get','node',server['spec']['nodeName']);require(actual_node['metadata']['uid']==node['metadata']['uid'],'diagnostic_target_node_uid_mismatch')
        def phases(expected_caddy,expected_unknown):
            for _ in range(2):
                for address in (server['status']['podIP'],svc['spec']['clusterIP']):
                    c=self.probe(b,'caddy',address,port);u=self.probe(b,'unknown',address,port)
                    require((c['http_status']==200)==expected_caddy and (u['http_status']==200)==expected_unknown,'isolated_phase_failed')
        def policy(name,allow=False):
            ingress=[{'from':[peer],'ports':[{'protocol':'TCP','port':port}]}] if allow else []
            return {'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name,'namespace':self.ns},'spec':{'podSelector':{'matchLabels':{'app':'fixed-server'}},'policyTypes':['Ingress'],'ingress':ingress}}
        phases(True,True)
        self.create(b,policy('deny'));time.sleep(2);phases(False,False)
        self.create(b,policy('allow',True));time.sleep(2);phases(True,False)
        self.delete(b,'networkpolicy','allow');self.delete(b,'networkpolicy','deny');phases(True,True)
        bindings={'current_identity_sha256':digest(identity),'source_sha256':config['source_sha256'],'peer_sha256':digest(peer),'configuration_sha256':digest(config),'cluster_uid':self.api(b,'get','namespace','kube-system')['metadata']['uid'],'node_uid':node['metadata']['uid'],'route':'ClusterIP','port':port}
        # Full owned resource cleanup precedes the successful causal result.
        require(not self.api(b,'-n',self.ns,'get','networkpolicies')['items'],'isolated_policies_remain')
        diagnostic_run=self.run
        self.cleanup(b)
        b.source_guard();require(b.guards()==baseline,'diagnostic_protected_state_drift')
        result=dict(bindings,server_pod_uid=server['metadata']['uid'],service_uid=svc['metadata']['uid'],status='PASS',run_id=diagnostic_run,completed_at=stamp(),source_kind='fixed_pre_nat_ip',target_commit=b.target_commit)
        result.update({k:True for k in ('baseline','caddy_denied','fixed_address_allowed','unknown_pod_denied','unknown_service_denied','restored_flow_proven','owned_cleanup_complete','production_postguard_pass')})
        result.update({k:False for k in ('caddy_only_identity_isolation','policy_hook_directly_observed','production_identity_proven','production_apply_succeeded','raw_address_output','raw_address_persisted')})
        b.guards();self.emit('isolated_port_verified',app=app,result_sha256=digest(result),port=port)
        return dict(bindings,run_id=diagnostic_run,tool_sha256=hashlib.sha256(open(__file__,'rb').read()).hexdigest(),server_pod_uid=server['metadata']['uid'],service_uid=svc['metadata']['uid'],result=result,result_sha256=digest(result))

    def pod_http(self,b,namespace,pod,address,port,path):
        code="import urllib.request,json; r=urllib.request.urlopen("+repr('http://'+address+':'+str(port)+path)+",timeout=3);print(json.dumps({'status':r.status}))"
        value=json.loads(self.run_command(b,KUBE+['-n',namespace,'exec',pod['metadata']['name'],'--','python','-c',code],timeout=10));require(value.get('status')==200,'required_pod_flow_failed');return value

    def metrics(self,b,app,target,source):
        import urllib.request
        services=self.api(b,'-n','monitoring','get','services')['items']
        services=[s for s in services if s['spec'].get('clusterIP') not in (None,'None') and any(p['port']==9090 for p in s['spec'].get('ports',[]))]
        require(len(services)==1,'prometheus_service_ambiguous');deadline=time.monotonic()+50
        started=datetime.now(timezone.utc)
        while True:
            url='http://'+services[0]['spec']['clusterIP']+':9090/api/v1/targets?state=active'
            with urllib.request.urlopen(url,timeout=8) as r:targets=json.load(r)['data']['activeTargets']
            targets=[t for t in targets if any(t.get('labels',{}).get(k)==app for k in ('service','container','app'))]
            require(len(targets)==1,'prometheus_target_ambiguous')
            target_info=targets[0]
            observed=datetime.fromisoformat(target_info['lastScrape'].replace('Z','+00:00'))
            from urllib.parse import urlsplit
            route=urlsplit(target_info['scrapeUrl'])
            require(route.hostname in (target['status']['podIP'], self.api(b,'-n','personal-server','get','service',app)['spec']['clusterIP']) and route.port==PORTS[app] and route.path=='/metrics','prometheus_exact_target_route')
            sockets=self.target_sockets(b,target,2)
            matches={row for row in sockets if row[0]==target['status']['podIP'] and row[1]==PORTS[app] and row[2]==source['status']['podIP']}
            if target_info['health']=='up' and not target_info.get('lastError') and observed.tzinfo and observed>=started and matches:return
            require(time.monotonic()<deadline,'fresh_prometheus_scrape_not_proven')

    def flows(self,b,app,dep,svc):
        flows=[];receipts={};categories=[]
        if app!='portal-web':categories.append('fanout')
        if app in ('portal-web','crawler-worker'):categories.append('metrics')
        _,target,_,_=self.target(b,app);endpoint=self.endpoint(b,app,target,svc)
        for category in categories:
            namespace='monitoring' if category=='metrics' else 'personal-server';label='prometheus' if category=='metrics' else 'portal-web'
            pods=self.api(b,'-n',namespace,'get','pods','-l','app.kubernetes.io/name='+label)['items']
            pods=[p for p in pods if not p['metadata'].get('deletionTimestamp') and p.get('status',{}).get('phase')=='Running'];require(len(pods)==1,'source_pod_ambiguous');pod=pods[0]
            if category=='metrics':self.metrics(b,app,target,pod)
            else:
                marker=secrets.token_hex(6);started=datetime.now(timezone.utc)
                path='/health?codex_fixed_source='+marker
                self.pod_http(b,namespace,pod,svc['spec']['clusterIP'],PORTS[app],path)
                raw=self.run_command(b,KUBE+['-n','personal-server','logs',target['metadata']['name'],'--since-time='+started.isoformat().replace('+00:00','Z'),'--tail=100','--timestamps=true'])
                rows=[line for line in raw.splitlines() if path in line];require(len(rows)==1,'flow_exact_marker_required')
                match=re.search(r'(\S+):([0-9]+) - "GET '+re.escape(path)+r' HTTP/1\.[01]" 200(?:\s|$)',rows[0])
                require(match is not None and str(ipaddress.ip_address(match[1].strip('[]')))==pod['status']['podIP'],'flow_actual_pod_source_required')
            after=self.api(b,'-n',namespace,'get','pod',pod['metadata']['name'])
            require(after['metadata']['uid']==pod['metadata']['uid'] and after['spec']==pod['spec'],'source_pod_changed')
            _,current,after_svc,_=self.target(b,app);require(self.endpoint(b,app,current,after_svc)==endpoint,'flow_target_endpoint_changed')
            peer={'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}},'podSelector':{'matchLabels':{'app.kubernetes.io/name':label}}}
            proof={'observed_at':stamp(),'status':200,'peer_sha256':digest(peer),'target_spec_sha256':digest(dep['spec']),'source_kind':'pod','source_pod_uid':pod['metadata']['uid'],'source_namespace':namespace,'source_labels':pod['metadata']['labels'],'post_nat_source_matches_pod':True}
            flows.append({'category':category,'peer':peer,'port':PORTS[app],'proof':proof})
            receipts[category]={'status':200,'source_pod_uid':pod['metadata']['uid'],**({'direct_pod':True} if category=='fanout' else {'post_nat_source_matches_pod':True})}
        return flows,receipts

    def collect(self,app,b):
        facts=self.identity(b);identity=facts['current_identity'];config=facts['fixed_configuration'];peer=facts['peer']
        dep,pod,svc,node=self.target(b,app)
        diag=self.diagnostic(b,app,identity,config,peer,node)
        observed_request=self.request(b,app,pod,svc,identity);flows,_=self.flows(b,app,dep,svc)
        request={k:diag[k] for k in ('current_identity_sha256','source_sha256','peer_sha256','configuration_sha256','cluster_uid','node_uid','route','port')}
        request.update(target_pod_uid=pod['metadata']['uid'],service_uid=svc['metadata']['uid'],service_spec_sha256=digest(svc['spec']))
        request.update({k:v for k,v in observed_request.items() if k not in ('status','port')});request['route_chosen_source_matches_fixed_address']=True
        inv={'deployment_uid':dep['metadata']['uid'],'resource_version':dep['metadata']['resourceVersion'],'image':dep['spec']['template']['spec']['containers'][0]['image'],'spec_sha256':digest(dep['spec']),'observed_at':stamp(),'source_commit':b.target_commit,'cluster_uid':diag['cluster_uid'],'target_pod_uid':pod['metadata']['uid'],'target_node_uid':node['metadata']['uid'],'target_service_uid':svc['metadata']['uid'],'target_service_spec_sha256':digest(svc['spec']),'target_endpoint_sha256':observed_request['target_endpoint_sha256'],'existing_policies':b.policies(),'flow_review_complete':True,'enforcement':{'cluster_uid':diag['cluster_uid'],'fixed_pre_nat_diagnostic':diag},'caddy':{'observed_at':stamp(),'status':200,'controlled_request':True,'source_kind':'fixed_pre_nat_ip','target_spec_sha256':digest(dep['spec']),'target_pod_uid':pod['metadata']['uid'],'peer':peer,'source_is_node':False,'current_identity':identity,'fixed_configuration':config,'request_proof':request},'flows':flows,'not_applicable':{k:'No active reviewed flow for this service' for k in {'metrics','fanout'}-{f['category'] for f in flows}}}
        self.inventories[app]=inv
        return dep,inv

    def verify(self,app,b,policy_uid=None):
        identity=self.identity(b)['current_identity'];dep,pod,svc,_=self.target(b,app)
        caddy=self.request(b,app,pod,svc,identity)
        caddy.update({k:self.inventories[app]['caddy']['request_proof'][k] for k in ('route','current_identity_sha256','source_sha256','peer_sha256','configuration_sha256')});caddy['route_chosen_source_matches_fixed_address']=True
        _,flows=self.flows(b,app,dep,svc)
        self.setup(b);client=self.ready(b,'unknown')
        unknown={'client_pod_uid':client['metadata']['uid'],'client_spec_sha256':digest(client['spec']),'client_namespace_uid':self.records[('namespace',self.ns)]['uid'],'client_ready_before':True,'pod':self.probe(b,'unknown',pod['status']['podIP'],PORTS[app]),'service':self.probe(b,'unknown',svc['spec']['clusterIP'],PORTS[app]),'client_ready_after':True}
        after=self.ready(b,'unknown');require(after['metadata']['uid']==client['metadata']['uid'],'unknown_client_changed')
        return {'observed_at':stamp(),'policy_uid':policy_uid,'target_pod_uid':pod['metadata']['uid'],'target_service_uid':svc['metadata']['uid'],'target_service_spec_sha256':digest(svc['spec']),'current_identity_sha256':digest(identity),'caddy_clusterip':caddy,'unknown':unknown,'fanout':flows.get('fanout'),'metrics':flows.get('metrics')}

    def cleanup(self,b):
        for child in self.children[:]:
            child.cleanup(b);self.children.remove(child)
        self.reconcile_pending(b)
        if not self.created:return True
        self.namespace_inventory(b)
        for kind,name in [('networkpolicy','allow'),('networkpolicy','deny'),('service','server'),('pod','unknown'),('pod','server')]:self.delete(b,kind,name)
        self.namespace_inventory(b)
        deadline=time.monotonic()+30
        while self.api(b,'-n',self.ns,'get','endpointslices')['items']:
            require(time.monotonic()<deadline,'owned_endpoints_remain');time.sleep(.5)
        self.delete(b,'namespace',self.ns)
        self.created=False;self.records={};self.__dict__.pop('defaults',None);self.run=secrets.token_hex(6);self.ns='fixed-ingress-'+self.run
        return True
