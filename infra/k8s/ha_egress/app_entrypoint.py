"""Production-only loopback/Unix shim; original application command stays unchanged."""
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .socket_gateway import (APP_UID,APP_GID,GATEWAY_UID,CADDY_UID,CHANNELS,SERVICES,
    ByteForwarder,NetworkUnavailable,secure_directory,socket_stamp,stamp,peer_uid,
    create_unix,remove_unix,tcp_connect,CONNECT_SECONDS)

CAPABILITY_ROOT=Path('/run/ha-network')
INGRESS_ROOT=Path('/run/ha-ingress')
HOSTS=Path('/etc/hosts')
ALIASES={'ha-a':'127.0.0.2','ha-b':'127.0.0.3','ha-broker':'127.0.0.4'}
LOOPS={'pg-a':('127.0.0.2',5432),'pg-b':('127.0.0.3',5432),'broker':('127.0.0.4',8443)}


class UnixCapability:
    def __init__(self,path,directory_stamp):
        self.path=Path(path);self.directory_stamp=directory_stamp
        self.captured=socket_stamp(self.path,GATEWAY_UID,APP_GID)

    def connect(self):
        stream=None;descriptor=None
        try:
            descriptor,current=secure_directory(self.path.parent,GATEWAY_UID,APP_GID,readonly=True)
            if current!=self.directory_stamp or socket_stamp(self.path,GATEWAY_UID,APP_GID)!=self.captured:
                raise NetworkUnavailable()
            stream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);stream.settimeout(CONNECT_SECONDS)
            stream.connect(str(self.path))
            peer_uid(stream,GATEWAY_UID)
            if (stamp(self.path.parent.lstat())!=self.directory_stamp
                    or socket_stamp(self.path,GATEWAY_UID,APP_GID)!=self.captured):raise NetworkUnavailable()
            return stream
        except Exception:
            if stream is not None:stream.close()
            raise NetworkUnavailable() from None
        finally:
            if descriptor is not None:os.close(descriptor)


def parse_hosts(raw):
    found={name:[] for name in ALIASES}
    for line in raw.decode('ascii').splitlines():
        tokens=line.split('#',1)[0].split()
        if not tokens:continue
        for name in tokens[1:]:
            if name in found:found[name].append(tokens[0])
    if found!={name:[ip] for name,ip in ALIASES.items()}:raise NetworkUnavailable()
    return found


def validate_hosts():
    descriptor=None
    try:
        for parent in HOSTS.parents:
            info=parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise NetworkUnavailable()
        descriptor=os.open(HOSTS,os.O_RDONLY|os.O_NOFOLLOW)
        before=os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1 or before.st_mode&0o022:
            raise NetworkUnavailable()
        raw=os.read(descriptor,65537)
        if len(raw)>65536 or stamp(before)!=stamp(os.fstat(descriptor)) or stamp(before)!=stamp(HOSTS.lstat()):raise NetworkUnavailable()
        parse_hosts(raw)
        # Check effective NSS resolution, not just textual /etc/hosts entries.
        for name,expected in ALIASES.items():
            rows=socket.getaddrinfo(name,None,type=socket.SOCK_STREAM)
            if not rows or {row[4][0] for row in rows}!={expected}:raise NetworkUnavailable()
    except Exception:raise NetworkUnavailable() from None
    finally:
        if descriptor is not None:os.close(descriptor)


def validate_application(service):
    try:
        if (sys.platform!='linux' or os.geteuid()!=APP_UID or os.getegid()!=APP_GID
                or CADDY_UID not in os.getgroups() or os.environ.get('APP_ENV')!='production'
                or {name for _,name in socket.if_nameindex()}!={'lo'}
                or not os.statvfs('/').f_flag&os.ST_RDONLY):raise NetworkUnavailable()
        status={}
        for line in Path('/proc/self/status').read_text().splitlines():
            key,_,value=line.partition(':');status[key]=value.strip()
        if (status.get('NoNewPrivs')!='1' or any(int(status.get(key,'1'),16)!=0
                for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))):raise NetworkUnavailable()
        from ha_runtime.config import load
        config=load(service)
        if (config.get('profile')!='enforced-vm-v1' or config.get('scope')!='n100-approved'
                or {config['primary']['host'],config['partner']['host']}!={'ha-a','ha-b'}
                or any(config[key].get('port',5432)!=5432 for key in ('primary','partner'))):raise NetworkUnavailable()
        url=urlsplit(config['effects']['url'])
        if url.hostname!='ha-broker' or (url.port or 8443)!=8443:raise NetworkUnavailable()
        validate_hosts()
        descriptor,_=secure_directory(CAPABILITY_ROOT,GATEWAY_UID,APP_GID,readonly=True);os.close(descriptor)
        descriptor,_=secure_directory(INGRESS_ROOT,APP_UID,CADDY_UID);os.close(descriptor)
        return config
    except Exception:raise NetworkUnavailable() from None


def prepare_forwarder(app_port):
    listeners=[];owned=[];descriptors=[]
    try:
        if type(app_port) is not int or not 1024<=app_port<=65535 or app_port in {5432,8443}:raise NetworkUnavailable()
        descriptor,directory=secure_directory(CAPABILITY_ROOT,GATEWAY_UID,APP_GID,readonly=True);descriptors.append(descriptor)
        capabilities={channel:UnixCapability(CAPABILITY_ROOT/(channel+'.sock'),directory) for channel in CHANNELS}
        for capability in capabilities.values():
            probe=capability.connect();probe.close()
        descriptor,_=secure_directory(INGRESS_ROOT,APP_UID,CADDY_UID);descriptors.append(descriptor)
        routes={}
        for channel,address in LOOPS.items():
            listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM);listeners.append(listener)
            listener.bind(address);listener.listen(32);listener.setblocking(False)
            routes[listener]=(capabilities[channel].connect,lambda _:None)
        path=INGRESS_ROOT/'http.sock'
        listener,captured=create_unix(path,APP_UID,CADDY_UID)
        listeners.append(listener);owned.append((path,captured))
        routes[listener]=(lambda:tcp_connect(('127.0.0.1',app_port)),lambda stream:peer_uid(stream,CADDY_UID))
        forwarder=ByteForwarder(routes,connections=32)
        return forwarder,owned,descriptors
    except Exception:
        for listener in listeners:listener.close()
        for path,captured in owned:remove_unix(path,captured)
        for descriptor in descriptors:os.close(descriptor)
        raise NetworkUnavailable() from None


def _stop_child(child):
    if child is None:return
    # The production container has a private PID namespace. Capture the group
    # via start_new_session; normal parent exit must not leave helpers behind.
    deadline=time.monotonic()+3
    try:os.killpg(child.pid,signal.SIGTERM)
    except ProcessLookupError:
        child.wait();return
    try:child.wait(timeout=3)
    except subprocess.TimeoutExpired:pass
    while time.monotonic()<deadline:
        try:os.killpg(child.pid,0)
        except ProcessLookupError:return
        time.sleep(.02)
    try:os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    child.wait(timeout=1)


def run_application(service,app_port,command):
    forwarder=None;child=None;owned=[];descriptors=[];previous={};stopped=threading.Event()
    try:
        if (service not in SERVICES or not command or len(command)>64
                or any(type(value) is not str or not value or '\0' in value or len(value)>4096 for value in command)):
            raise NetworkUnavailable()
        validate_application(service)
        forwarder,owned,descriptors=prepare_forwarder(app_port)
        forwarder.start()
        for kind in (signal.SIGTERM,signal.SIGINT):previous[kind]=signal.signal(kind,lambda *_:stopped.set())
        if stopped.is_set():raise NetworkUnavailable()
        child=subprocess.Popen(list(command),shell=False,close_fds=True,start_new_session=True)
        while child.poll() is None and not stopped.wait(.1):pass
        code=child.poll()
        return (128+signal.SIGTERM) if code is None else (code if code>=0 else 128-code)
    finally:
        try:_stop_child(child)
        finally:
            if forwarder is not None:forwarder.close()
            for path,captured in owned:remove_unix(path,captured)
            for descriptor in descriptors:os.close(descriptor)
            for kind,handler in previous.items():signal.signal(kind,handler)


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    try:
        if len(argv)<6 or argv[0]!='--service' or argv[2]!='--app-port' or argv[4]!='--' or not argv[3].isdigit():
            raise NetworkUnavailable()
        return run_application(argv[1],int(argv[3]),argv[5:])
    except Exception:
        print('ha_network_entrypoint_unavailable',file=sys.stderr);return 78


if __name__=='__main__':raise SystemExit(main())
