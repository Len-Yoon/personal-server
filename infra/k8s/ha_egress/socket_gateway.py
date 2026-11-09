"""Fixed raw-byte capabilities; no HTTP proxy, URL routing, credentials or TLS termination."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time
from types import MappingProxyType

APP_UID=10001
APP_GID=10001
GATEWAY_UID=10003
CADDY_UID=10004
SERVICES=frozenset({'portal-web','book-memo','youtube-memo','crawler-worker','car-care-worker','homeops-executor','system-agent'})
CHANNELS=('pg-a','pg-b','broker')
SOCKET_ROOT=Path('/run/ha-network')
BUFFER=65536
CONNECT_SECONDS=2
IDLE_SECONDS=300


class NetworkUnavailable(OSError):
    def __init__(self):super().__init__('ha_network_capability_unavailable')


def stamp(info):
    return (info.st_dev,info.st_ino,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode),info.st_ctime_ns)


def secure_directory(path,uid,gid,*,readonly=False):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise NetworkUnavailable()
    for parent in path.parents:
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0,uid} or info.st_mode&0o022:
            raise NetworkUnavailable()
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))!=(uid,gid,0o750)
                or stamp(info)!=stamp(path.lstat()) or (readonly and not os.fstatvfs(descriptor).f_flag&os.ST_RDONLY)):
            raise NetworkUnavailable()
        return descriptor,stamp(info)
    except Exception:
        os.close(descriptor);raise NetworkUnavailable() from None


def peer_uid(stream,uid):
    if sys.platform!='linux' or not hasattr(socket,'SO_PEERCRED'):raise NetworkUnavailable()
    _,actual,_=struct.unpack('3i',stream.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
    if actual!=uid:raise NetworkUnavailable()


def socket_stamp(path,uid,gid):
    info=Path(path).lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_nlink!=1
            or (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))!=(uid,gid,0o660)):
        raise NetworkUnavailable()
    return stamp(info)


def create_unix(path,uid,gid):
    """Never remove/reuse an existing object; unlink only this captured inode."""
    if Path(path).exists() or Path(path).is_symlink():raise NetworkUnavailable()
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    captured=None
    try:
        old=os.umask(0o077)
        try:server.bind(str(path))
        finally:os.umask(old)
        captured=stamp(Path(path).lstat())
        os.chown(path,uid,gid);os.chmod(path,0o660)
        captured=socket_stamp(path,uid,gid)
        server.listen(32);server.setblocking(False)
        return server,captured
    except Exception:
        server.close()
        if captured is not None:
            try:
                # chmod/chown change ctime; inode identity remains the ownership proof.
                current=Path(path).lstat()
                if (current.st_dev,current.st_ino)==captured[:2]:Path(path).unlink()
            except OSError:pass
        raise NetworkUnavailable() from None


def remove_unix(path,captured):
    try:
        if stamp(Path(path).lstat())!=captured:return False
        Path(path).unlink();return True
    except FileNotFoundError:return True
    except OSError:return False


def tcp_connect(target):
    address,port=target
    ip=ipaddress.ip_address(address)
    stream=socket.socket(socket.AF_INET if ip.version==4 else socket.AF_INET6,socket.SOCK_STREAM)
    try:
        stream.settimeout(CONNECT_SECONDS)
        stream.connect((str(ip),port))
        return stream
    except Exception:
        stream.close();raise NetworkUnavailable() from None


class ByteForwarder:
    """One accept thread and bounded stream threads, each with two capped buffers."""
    def __init__(self,routes,*,connections=64,idle=IDLE_SECONDS):
        if not routes or type(connections) is not int or not 1<=connections<=64 or not 0<idle<=IDLE_SECONDS:
            raise NetworkUnavailable()
        self.routes=dict(routes) # listener -> (fixed connector, peer verifier)
        self.idle=idle;self.slots=threading.BoundedSemaphore(connections)
        self.stop=threading.Event();self.lock=threading.Lock();self.streams=set();self.threads=set();self.acceptor=None

    def start(self):
        if self.acceptor is not None:raise NetworkUnavailable()
        self.acceptor=threading.Thread(target=self._accept,daemon=True);self.acceptor.start()

    def _accept(self):
        while not self.stop.is_set():
            try:ready,_,_=select.select(list(self.routes),[],[],.1)
            except (OSError,ValueError):return
            for server in ready:
                try:client,_=server.accept()
                except OSError:continue
                if not self.slots.acquire(blocking=False):client.close();continue
                with self.lock:self.streams.add(client)
                connector,verifier=self.routes[server]
                thread=threading.Thread(target=self._serve,args=(client,connector,verifier),daemon=True)
                with self.lock:self.threads.add(thread)
                try:thread.start()
                except Exception:
                    with self.lock:self.threads.discard(thread);self.streams.discard(client)
                    client.close();self.slots.release()

    def _serve(self,client,connector,verifier):
        upstream=None
        try:
            verifier(client)
            if self.stop.is_set():return
            upstream=connector()
            with self.lock:self.streams.add(upstream)
            client.setblocking(False);upstream.setblocking(False)
            peers={client:upstream,upstream:client}
            pending={client:bytearray(),upstream:bytearray()}
            readable={client,upstream};shutdown=set();last=time.monotonic()
            while not self.stop.is_set() and time.monotonic()-last<self.idle:
                for source in peers:
                    destination=peers[source]
                    if source not in readable and not pending[destination] and destination not in shutdown:
                        destination.shutdown(socket.SHUT_WR);shutdown.add(destination)
                if not readable and not any(pending.values()):return
                reads=[source for source in readable if len(pending[peers[source]])<BUFFER]
                writes=[destination for destination,buffer in pending.items() if buffer]
                ready,out,_=select.select(reads,writes,[],min(.1,self.idle))
                for source in ready:
                    destination=peers[source]
                    data=source.recv(min(16384,BUFFER-len(pending[destination])))
                    if data:pending[destination].extend(data);last=time.monotonic()
                    else:readable.discard(source)
                for destination in out:
                    count=destination.send(pending[destination])
                    if count<=0:return
                    del pending[destination][:count];last=time.monotonic()
        except Exception:
            pass # Disconnects never print payloads, destinations or credentials.
        finally:
            for stream in (client,upstream):
                if stream is not None:
                    try:stream.close()
                    except OSError:pass
                    with self.lock:self.streams.discard(stream)
            with self.lock:self.threads.discard(threading.current_thread())
            self.slots.release()

    def close(self):
        self.stop.set()
        for server in self.routes:
            try:server.close()
            except OSError:pass
        with self.lock:streams=list(self.streams);threads=list(self.threads)
        for stream in streams:
            try:stream.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:stream.close()
            except OSError:pass
        deadline=time.monotonic()+3
        for thread in ([self.acceptor] if self.acceptor else [])+threads:
            if thread is not threading.current_thread():thread.join(max(0,deadline-time.monotonic()))


def _pairs(items):
    result={}
    for key,value in items:
        if key in result:raise NetworkUnavailable()
        result[key]=value
    return result


def _read_policy(path,expected_sha256):
    descriptor=None
    try:
        path=Path(path)
        if not path.is_absolute() or '..' in path.parts or not isinstance(expected_sha256,str) or len(expected_sha256)!=64:
            raise NetworkUnavailable()
        for parent in path.parents:
            info=parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise NetworkUnavailable()
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        before=os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1
                or stat.S_IMODE(before.st_mode) not in {0o440,0o444}):raise NetworkUnavailable()
        raw=os.read(descriptor,65537)
        if (len(raw)>65536 or stamp(before)!=stamp(os.fstat(descriptor))
                or stamp(before)!=stamp(path.lstat()) or hashlib.sha256(raw).hexdigest()!=expected_sha256):raise NetworkUnavailable()
        return raw
    except Exception:raise NetworkUnavailable() from None
    finally:
        if descriptor is not None:os.close(descriptor)


def load_targets(path,expected_sha256):
    try:
        raw=_read_policy(path,expected_sha256)
        value=json.loads(raw,object_pairs_hook=_pairs)
        if type(value) is not dict or set(value)!={'schema_version','pg_a','pg_b'} or type(value['schema_version']) is not int or value['schema_version']!=1:
            raise NetworkUnavailable()
        targets={}
        for key,channel in (('pg_a','pg-a'),('pg_b','pg-b')):
            endpoint=value[key]
            if type(endpoint) is not dict or set(endpoint)!={'host','port'} or type(endpoint['host']) is not str or type(endpoint['port']) is not int or endpoint['port']!=5432:
                raise NetworkUnavailable()
            ip=ipaddress.ip_address(endpoint['host'])
            if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                raise NetworkUnavailable()
            targets[channel]=(str(ip),5432)
        if targets['pg-a']==targets['pg-b']:raise NetworkUnavailable()
        targets['broker']=('127.0.0.1',8443)
        return MappingProxyType(targets)
    except Exception:raise NetworkUnavailable() from None


class SocketGateway:
    def __init__(self,config,expected_sha256):
        if sys.platform!='linux' or os.geteuid()!=GATEWAY_UID or APP_GID not in {os.getegid(),*os.getgroups()}:
            raise NetworkUnavailable()
        self.targets=load_targets(config,expected_sha256)
        self.directories=[];self.owned=[];self.forwarder=None

    def start(self):
        if self.forwarder is not None:raise NetworkUnavailable()
        routes={}
        try:
            descriptor,_=secure_directory(SOCKET_ROOT,GATEWAY_UID,APP_GID)
            self.directories.append(descriptor)
            for service in sorted(SERVICES):
                path=SOCKET_ROOT/service
                try:
                    os.mkdir(path,mode=0o750)
                    os.chown(path,GATEWAY_UID,APP_GID);os.chmod(path,0o750)
                except FileExistsError:pass
                descriptor,_=secure_directory(path,GATEWAY_UID,APP_GID)
                self.directories.append(descriptor)
                for channel in CHANNELS:
                    name=path/(channel+'.sock')
                    listener,captured=create_unix(name,GATEWAY_UID,APP_GID)
                    self.owned.append((name,captured,listener))
                    target=(('127.0.0.1',8444) if service=='homeops-executor' and channel=='broker' else self.targets[channel])
                    routes[listener]=(lambda target=target:tcp_connect(target),lambda stream:peer_uid(stream,APP_UID))
            self.forwarder=ByteForwarder(routes,connections=64);self.forwarder.start()
        except Exception:
            self.close();raise NetworkUnavailable() from None

    def close(self):
        if self.forwarder is not None:self.forwarder.close()
        for path,captured,listener in self.owned:
            listener.close();remove_unix(path,captured)
        for descriptor in self.directories:
            try:os.close(descriptor)
            except OSError:pass
        self.directories=[];self.owned=[]


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    gateway=None;stopped=threading.Event();previous={}
    try:
        if len(argv)!=4 or argv[0]!='--config' or argv[2]!='--sha256':raise NetworkUnavailable()
        gateway=SocketGateway(argv[1],argv[3])
        for kind in (signal.SIGTERM,signal.SIGINT):
            previous[kind]=signal.signal(kind,lambda *_:stopped.set())
        gateway.start()
        stopped.wait()
        return 0
    except Exception:
        print('ha_network_capability_unavailable',file=sys.stderr);return 78
    finally:
        if gateway is not None:gateway.close()
        for kind,handler in previous.items():signal.signal(kind,handler)


if __name__=='__main__':raise SystemExit(main())
