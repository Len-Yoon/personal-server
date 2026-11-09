"""Mac transport behavior and policy doubles; not Linux UID/namespace acceptance."""
import copy
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/k8s'))
from ha_egress import socket_gateway as gateway


def echo_once(listener,prefix):
    def run():
        stream=None
        try:
            listener.settimeout(3);stream,_=listener.accept();stream.settimeout(3)
            content=[]
            while True:
                data=stream.recv(16384)
                if not data:break
                content.append(data)
            stream.sendall(prefix+b''.join(content))
        finally:
            if stream is not None:stream.close()
    thread=threading.Thread(target=run,daemon=True);thread.start();return thread


def receive(stream):
    chunks=[]
    while True:
        data=stream.recv(65536)
        if not data:return b''.join(chunks)
        chunks.append(data)


class GatewayPolicyTests(unittest.TestCase):
    def test_exact_immutable_destinations_and_broker_not_caller_selected(self):
        value={'schema_version':1,'pg_a':{'host':'10.20.30.1','port':5432},'pg_b':{'host':'10.20.30.2','port':5432}}
        with patch.object(gateway,'_read_policy',return_value=json.dumps(value).encode()):
            result=gateway.load_targets('/unused','a'*64)
        self.assertEqual(dict(result),{'pg-a':('10.20.30.1',5432),'pg-b':('10.20.30.2',5432),'broker':('127.0.0.1',8443)})
        with self.assertRaises(TypeError):result['broker']=('other',1)
        cases=[]
        for key,bad in [('host','database.example'),('host','127.0.0.1'),('host','169.254.169.254'),('port',2375),('port',True)]:
            item=copy.deepcopy(value);item['pg_a'][key]=bad;cases.append(item)
        item=copy.deepcopy(value);item['pg_b']=item['pg_a'];cases.append(item)
        item=copy.deepcopy(value);item['broker']={'host':'10.0.0.3','port':2375};cases.append(item)
        for item in cases:
            with patch.object(gateway,'_read_policy',return_value=json.dumps(item).encode()):
                with self.assertRaises(gateway.NetworkUnavailable):gateway.load_targets('/unused','a'*64)
        raw=b'{"schema_version":1,"schema_version":1,"pg_a":{},"pg_b":{}}'
        with patch.object(gateway,'_read_policy',return_value=raw):
            with self.assertRaises(gateway.NetworkUnavailable):gateway.load_targets('/unused','a'*64)

    def test_user_owned_or_unpinned_policy_never_becomes_trusted(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            path=Path(directory)/'policy';path.write_text('{}');path.chmod(0o444)
            with self.assertRaises(gateway.NetworkUnavailable):gateway.load_targets(path,'a'*64)
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(gateway.NetworkUnavailable):gateway.load_targets(link,'a'*64)
        with patch.object(gateway,'load_targets') as load:
            with self.assertRaises(gateway.NetworkUnavailable):gateway.SocketGateway('/unused','a'*64)
            load.assert_not_called()

    def test_peer_uid_is_mandatory_and_failed_verifier_never_connects_target(self):
        stream=MagicMock();stream.getsockopt.return_value=struct.pack('3i',123,10001,10001)
        with patch.object(gateway.sys,'platform','linux'),patch.object(gateway.socket,'SO_PEERCRED',1234,create=True):
            gateway.peer_uid(stream,10001)
            with self.assertRaises(gateway.NetworkUnavailable):gateway.peer_uid(stream,10004)
        with patch.object(gateway.sys,'platform','darwin'):
            with self.assertRaises(gateway.NetworkUnavailable):gateway.peer_uid(stream,10001)

    def test_existing_socket_objects_not_deleted_and_cleanup_requires_same_inode(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            path=Path(directory)/'channel.sock'
            server,captured=gateway.create_unix(path,os.geteuid(),os.getegid())
            try:
                with self.assertRaises(gateway.NetworkUnavailable):gateway.create_unix(path,os.geteuid(),os.getegid())
                self.assertEqual(gateway.socket_stamp(path,os.geteuid(),os.getegid()),captured)
                path.unlink();path.write_text('replacement')
                self.assertFalse(gateway.remove_unix(path,captured))
                self.assertEqual(path.read_text(),'replacement')
            finally:server.close()


class GatewayTransportTests(unittest.TestCase):
    def test_three_fixed_tcp_to_unix_routes_forward_binary_and_preserve_half_close(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            routes={};servers=[];threads=[];clients=[];forwarder=None
            try:
                for index in range(3):
                    path=Path(directory)/(str(index)+'.sock')
                    endpoint=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);endpoint.bind(str(path));endpoint.listen(1);servers.append(endpoint)
                    threads.append(echo_once(endpoint,bytes([index])))
                    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(8);listener.setblocking(False)
                    def connect(path=path):
                        stream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);stream.settimeout(2);stream.connect(str(path));return stream
                    routes[listener]=(connect,lambda _:None)
                forwarder=gateway.ByteForwarder(routes,connections=3);forwarder.start()
                payload=bytes(range(256))*4096 # Exceeds a relay buffer by sixteen times.
                for index,listener in enumerate(routes):
                    client=socket.create_connection(listener.getsockname(),timeout=3);clients.append(client)
                    client.sendall(payload);client.shutdown(socket.SHUT_WR)
                    self.assertEqual(receive(client),bytes([index])+payload)
            finally:
                for client in clients:client.close()
                if forwarder:forwarder.close()
                for endpoint in servers:endpoint.close()
                for thread in threads:thread.join(1)
                if forwarder:self.assertFalse(forwarder.streams)

    def test_unix_ingress_only_connects_fixed_loopback_and_denied_peer_has_zero_connects(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            target=socket.socket();target.bind(('127.0.0.1',0));target.listen(1)
            thread=echo_once(target,b'ingress:')
            path=Path(directory)/'http.sock';listener=socket.socket(socket.AF_UNIX);listener.bind(str(path));listener.listen(4);listener.setblocking(False)
            forwarder=gateway.ByteForwarder({listener:(lambda:gateway.tcp_connect(target.getsockname()),lambda _:None)})
            forwarder.start();client=socket.socket(socket.AF_UNIX);client.settimeout(2)
            try:
                client.connect(str(path));client.sendall(b'fixed');client.shutdown(socket.SHUT_WR)
                self.assertEqual(receive(client),b'ingress:fixed')
            finally:client.close();forwarder.close();target.close();thread.join(1)
            path.unlink();listener=socket.socket(socket.AF_UNIX);listener.bind(str(path));listener.listen(4);listener.setblocking(False)
            connector=MagicMock()
            def deny(_):raise gateway.NetworkUnavailable()
            forwarder=gateway.ByteForwarder({listener:(connector,deny)});forwarder.start()
            client=socket.socket(socket.AF_UNIX);client.settimeout(2)
            try:
                client.connect(str(path));self.assertEqual(client.recv(1),b'');connector.assert_not_called()
            finally:client.close();forwarder.close()

    def test_idle_stream_expires_and_shutdown_releases_live_streams(self):
        target=socket.socket();target.bind(('127.0.0.1',0));target.listen(2)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(2);listener.setblocking(False)
        forwarder=gateway.ByteForwarder({listener:(lambda:gateway.tcp_connect(target.getsockname()),lambda _:None)},connections=1,idle=.08)
        forwarder.start();client=socket.create_connection(listener.getsockname(),timeout=2)
        try:self.assertEqual(client.recv(1),b'')
        finally:client.close();forwarder.close();target.close()
        self.assertFalse(forwarder.streams)


if __name__=='__main__':unittest.main()
