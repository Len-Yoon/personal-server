"""Windows custody policy, handle semantics and coordinator integration."""
from contextlib import contextmanager
import base64
import ctypes
from dataclasses import dataclass
import gzip
from pathlib import Path, PureWindowsPath
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/k8s'))
from ha_control import native_windows_local as local
from ha_control import sameboot_transition as transition
from ha_control import windows_protected_io as io


class AclTests(unittest.TestCase):
    def check(self,entries,**kw):
        io.validate_acl(kw.pop('owner','S-1-5-18'),entries,depth=kw.pop('depth',0),
                        private=kw.pop('private',True),allowed=io.TRUSTED,**kw)

    def test_private_tcb_only(self):
        self.check([(0,0,0x1f01ff,'S-1-5-18'),(0,0,0x1f01ff,'S-1-5-32-544')])

    def test_untrusted_read_or_write_and_null_acl_denied(self):
        for entries in ([],[(0,0,1,'S-1-1-0')],[(0,0,0x40000000,'S-1-1-0')],
                        [(1,0,0xffffffff,'S-1-1-0'),(0,0,1,'S-1-1-0')]):
            with self.assertRaises(io.TransitionBlocked):self.check(entries)

    def test_public_read_allowed_but_inheritable_parent_write_denied(self):
        self.check([(0,0,1,'S-1-1-0')],private=False)
        with self.assertRaises(io.TransitionBlocked):self.check([(0,8,0x40000000,'S-1-1-0')],depth=1)

    def test_only_checked_os_child_allows_noneffective_parent_genericall(self):
        entries=[(0,11,0x10000000,'S-1-1-0')]
        with self.assertRaises(io.TransitionBlocked):self.check(entries,depth=1,private=False)
        self.check(entries,depth=1,private=False,checked_os_child=True)
        for flags,depth in ((3,1),(11,0)):
            with self.assertRaises(io.TransitionBlocked):
                self.check([(0,flags,0x10000000,'S-1-1-0')],depth=depth,private=False,checked_os_child=True)
        # A harmless template never hides direct replacement/write authority.
        for rights in (0x10000000,0x40000000,0x10000,0x40):
            with self.assertRaises(io.TransitionBlocked):
                self.check(entries+[(0,0,rights,'S-1-1-0')],depth=1,private=False,checked_os_child=True)

    def test_ancestor_replacement_and_leaf_protection(self):
        with self.assertRaises(io.TransitionBlocked):self.check([(0,0,0x10000,'S-1-1-0')],depth=2)
        with self.assertRaises(io.TransitionBlocked):self.check([(0,0,1,'S-1-5-18')],protected=False)
        self.check([(0,0,1,'S-1-5-18')],depth=3,owner=io.gate.TRUSTED_INSTALLER,drive_root=True)

    def test_current_owner_is_explicit_read_policy(self):
        user='S-1-5-21-1-2-3-1001'
        io.validate_acl(user,[(0,0,0x1f01ff,user)],depth=0,private=True,allowed=io.TRUSTED|{user})
        with self.assertRaises(io.TransitionBlocked):self.check([(0,0,0x1f01ff,user)],owner=user)


class LocalSystemPowerShellTests(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        self.native.owner_sid = 'S-1-5-18'
        self.native.system_directory_path.return_value = PureWindowsPath(r'C:\Windows\System32')
        self.image = b'approved OS executable'
        self.native.system_executable_stamp.return_value = (self.image, 'stamp')
        self.native.same_file.return_value = True
        self.pin = local.wire._digest(self.image)
        factory = patch.object(local, 'WindowsProtectedIO', return_value=self.native)
        factory.start()
        self.addCleanup(factory.stop)

    def test_rejects_non_system_before_execution(self):
        self.native.owner_sid = 'S-1-5-32-544'
        with self.assertRaisesRegex(local.wire.BridgeRejected, 'local_recovery_system_required'):
            local.LocalSystemPowerShell(executable_sha256=self.pin)
        self.native.system_executable_stamp.assert_not_called()

    def test_pinned_direct_executable_has_no_profile_or_inherited_environment(self):
        shell = local.LocalSystemPowerShell(executable_sha256=self.pin)
        source = '@{okay=$true}|ConvertTo-Json -Compress'

        def execute(command, payload, deadline, *, maximum, start):
            self.assertEqual(gzip.decompress(payload).decode(), source)
            self.assertEqual(command[:-1], (str(shell.executable), '-NoLogo', '-NoProfile',
                                           '-NonInteractive', '-EncodedCommand'))
            loader = base64.b64decode(command[-1]).decode('utf-16-le')
            self.assertIn(local.wire._digest(payload), loader)
            self.assertEqual(maximum, 1048576)
            with patch.object(local, 'start_direct') as launch:
                start(command)
                launch.assert_called_once_with(command, cwd=str(shell.executable.parent),
                    env={'SystemRoot': str(shell.system.parent), 'WINDIR': str(shell.system.parent),
                         'PATH': str(shell.system)})
            return b'{"okay":true}\r\n'

        with patch.object(local, 'exchange_direct', side_effect=execute):
            self.assertEqual(shell.powershell(source, 1e12), {'okay': True})
        self.native.same_file.assert_called_once_with(shell.executable, 'stamp', private=False)

    def test_changed_image_and_invalid_response_are_rejected(self):
        with patch.object(local, 'exchange_direct') as exchange:
            shell = local.LocalSystemPowerShell(executable_sha256='a'*64)
            with self.assertRaisesRegex(local.wire.BridgeRejected, 'local_recovery_powershell_image_changed'):
                shell.powershell('{}', 1e12)
            exchange.assert_not_called()
            shell = local.LocalSystemPowerShell(executable_sha256=self.pin)
            exchange.return_value = b'{"a":1,"a":2}'
            with self.assertRaisesRegex(local.wire.BridgeRejected, 'bridge_duplicate_fields'):
                shell.powershell('{}', 1e12)
            self.native.same_file.return_value = False
            with self.assertRaisesRegex(local.wire.BridgeRejected, 'local_recovery_powershell_image_changed'):
                shell.powershell('{}', 1e12)

    def test_authority_callback_is_live_only_within_session(self):
        shell = local.LocalSystemPowerShell(executable_sha256=self.pin)
        with patch.object(shell, 'powershell', return_value={'proof': True}) as observe:
            with shell.live_authority_session(1e12) as query:
                self.assertEqual(query('sample', 1e11), {'proof': True})
            with self.assertRaisesRegex(local.wire.BridgeRejected, 'local_recovery_authority_session_closed'):
                query('sample', 1e11)
            observe.assert_called_once_with('sample', 1e11)


@dataclass
class Info:
    attributes:int
    index_low:int
    size:int=0
    links:int=1
    index_high:int=0
    volume:int=7
    written:int=0
    def __post_init__(self):self.created=SimpleNamespace(dwHighDateTime=0,dwLowDateTime=1)
    def identity(self):return (self.attributes,self.volume,self.links,self.index_low,self.size,self.written)


class FakeNative:
    os_root='c:\\'
    def __init__(self):
        self.files={};self.handles={};self.calls=[];self.number=10;self.last_error=0;self.drift=False
        self.files['c:\\']=(b'',Info(16,1))
        self.files['c:\\private']=(b'',Info(16,2))
    def open(self,path,access,share,sa,disposition,flags,template):
        key=path.casefold();self.calls.append((key,access,share,disposition,flags))
        if key in self.files and disposition==1:self.last_error=183;return io.gate.INVALID_HANDLE
        if any(k==key and (s==0 or share==0) for k,s in self.handles.values()):
            self.last_error=32;return io.gate.INVALID_HANDLE
        if key not in self.files:
            if disposition not in (1,4):self.last_error=2;return io.gate.INVALID_HANDLE
            self.files[key]=(b'',Info(0,len(self.files)+1))
            self.files['c:\\private'][1].written+=1
        self.number+=1;self.handles[self.number]=(key,share);return self.number
    def close(self,handle):del self.handles[handle]
    def free(self,descriptor):pass
    def _info(self,handle):
        i=self.files[self.handles[handle][0]][1]
        return Info(i.attributes,i.index_low,i.size,i.links,i.index_high,i.volume,i.written)
    def final_path(self,handle,buffer,length,flags):
        buffer.value='\\\\?\\'+self.handles[handle][0];return len(buffer.value)
    def read(self,handle,buffer,length,count,overlapped):
        key=self.handles[handle][0];raw,info=self.files[key]
        ctypes.memmove(buffer,raw,len(raw));count._obj.value=len(raw)
        if self.drift:info.written+=1
        return True


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.native=FakeNative();self.io=object.__new__(io.WindowsProtectedIO)
        self.io.native=self.native;self.io.admin_only=True;self.io.allowed=io.TRUSTED
        self.io.file_type=lambda h:1;self.io._acl=lambda *a:'a'*64
        self.io._descriptor=lambda:ctypes.c_void_p(123)
        self.io.flush=Mock(return_value=True);self.io.seek=Mock(return_value=True)
        def write(handle,buffer,length,count,overlapped):
            key=self.native.handles[handle][0];raw=ctypes.string_at(buffer,length)
            _,info=self.native.files[key];info.size=length;info.written+=1
            self.native.files[key]=(raw,info);count._obj.value=length;return True
        self.io.write=write
        self.error=patch.object(ctypes,'get_last_error',side_effect=lambda:self.native.last_error,create=True)
        self.error.start();self.addCleanup(self.error.stop)

    def test_new_file_create_new_write_through_flush_and_stable_read(self):
        path=PureWindowsPath(r'C:\private\entry.json');self.io.write_new(path,b'first')
        self.io.flush.assert_called_once()
        self.assertIn((str(path).casefold(),0xc0020000,1,1,0x80200000),self.native.calls)
        raw,stamp=self.io.read_stamp(path,100);self.assertEqual(raw,b'first')
        self.assertTrue(self.io.same_file(path,stamp))
        with self.assertRaises(FileExistsError):self.io.write_new(path,b'changed')
        self.assertEqual(self.io.read_stamp(path,100)[0],b'first');self.assertFalse(self.native.handles)

    def test_missing_file_maps_to_existing_coordinator_readback(self):
        with self.assertRaises(FileNotFoundError):self.io.read_stamp(r'C:\private\missing',100)
        self.assertFalse(self.native.handles)

    def test_read_rejects_drift_reparse_hardlink_and_limit(self):
        path=r'C:\private\key';self.io.write_new(path,b'key')
        self.native.drift=True
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,100)
        self.native.drift=False
        info=self.native.files[path.casefold()][1]
        for attributes,links,limit in ((0x400,1,100),(0,2,100),(0,1,2)):
            info.attributes,info.links=attributes,links
            with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,limit)
        self.assertFalse(self.native.handles)

    def test_per_file_lock_conflicts_only_with_same_journal(self):
        with self.io.exclusive(r'C:\private\one.lock'):
            with self.assertRaises(io.TransitionBlocked):
                with self.io.exclusive(r'C:\private\one.lock'):pass
            with self.io.exclusive(r'C:\private\two.lock'):pass
        with self.io.exclusive(r'C:\private\one.lock'):pass
        self.assertFalse(self.native.handles)

    def test_bad_device_or_final_path_is_denied(self):
        self.io.write_new(r'C:\private\key',b'key')
        self.io.file_type=lambda h:2
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(r'C:\private\key',100)

    def test_only_checked_os_public_leaf_allows_hardlinks_and_still_binds_count(self):
        path=PureWindowsPath(r'C:\Windows\System32\OpenSSH\ssh.exe')
        for number,parent in enumerate(reversed(path.parents),start=20):
            self.native.files[str(parent).casefold()]=(b'',Info(16,number))
        info=Info(0,24,size=5,links=2)
        self.native.files[str(path).casefold()]=(b'image',info)
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,100,private=False)
        self.io._checked_os_child=True
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,100,private=True)
        raw,stamp=self.io.read_stamp(path,100,private=False)
        self.assertEqual(raw,b'image');self.assertTrue(self.io.same_file(path,stamp,private=False))
        info.links=3
        self.assertFalse(self.io.same_file(path,stamp,private=False))
        info.links=0
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,100,private=False)
        info.links=2;info.attributes=io.gate.REPARSE_POINT
        with self.assertRaises(io.TransitionBlocked):self.io.read_stamp(path,100,private=False)
        self.assertFalse(self.native.handles)

    def test_os_exception_is_exact_system_directory_openssh_leaf_only(self):
        def directory(buffer,length):buffer.value=r'C:\Windows\System32';return len(buffer.value)
        self.io.system_directory=directory
        method=io.WindowsProtectedIO.system_executable_stamp
        reader=Mock();stamp=io.WindowsFileStamp(r'C:\Windows\System32\OpenSSH\ssh.exe',(),'',(),100)
        reader.read_stamp.return_value=(b'os-image',stamp)
        with patch.object(io,'WindowsProtectedIO',return_value=reader) as factory:
            raw,actual=method(self.io,stamp.path,100)
            self.assertEqual(raw,b'os-image');self.assertTrue(actual.system_executable)
            self.assertEqual(reader.allowed,io.TRUSTED|{io.gate.TRUSTED_INSTALLER})
            self.assertTrue(reader._checked_os_child)
            reader.read_stamp.assert_called_once_with(PureWindowsPath(stamp.path),100,private=False)
            factory.assert_called_once_with(admin_only=False)
        for path in (r'C:\private\ssh.exe',r'C:\Windows\System32\OpenSSH\config',
                     r'C:\Windows\System32\OpenSSH\child\ssh.exe'):
            with patch.object(io,'WindowsProtectedIO') as factory:
                with self.assertRaises(io.TransitionBlocked):method(self.io,path,100)
                factory.assert_not_called()

    def test_os_stamp_recheck_keeps_its_bounded_policy(self):
        stamp=io.WindowsFileStamp(r'C:\Windows\System32\OpenSSH\ssh.exe',(),'',(),100,True)
        self.io.system_executable_stamp=Mock(return_value=(b'image',stamp))
        self.assertTrue(self.io.same_file(stamp.path,stamp))
        self.io.system_executable_stamp.assert_called_once_with(stamp.path,100)

    def test_os_writer_exception_excludes_current_user(self):
        allowed=io.TRUSTED|{io.gate.TRUSTED_INSTALLER}
        io.validate_acl(io.gate.TRUSTED_INSTALLER,[(0,0,0x1f01ff,io.gate.TRUSTED_INSTALLER),
                        (0,0,1,'S-1-1-0')],depth=0,private=False,allowed=allowed)
        with self.assertRaises(io.TransitionBlocked):
            io.validate_acl(io.gate.TRUSTED_INSTALLER,[(0,0,0x40000000,'S-1-5-21-123')],
                            depth=0,private=False,allowed=allowed)


class IntegrationTests(unittest.TestCase):
    def test_windows_uses_native_io_without_posix_owner_or_authority_mutex(self):
        backend=Mock();backend.assert_directory=Mock();backend.read_stamp.return_value=(b'private',object())
        @contextmanager
        def lock(path):yield
        backend.exclusive.side_effect=lock
        with patch.object(transition.sys,'platform','win32'),patch.object(io,'WindowsProtectedIO',return_value=backend),\
             patch.object(transition,'_posix_exclusive',side_effect=AssertionError('authority mutex used')):
            self.assertEqual(transition._private_file('scope',100),b'private')
            coordinator=object.__new__(transition.SamebootTransition);coordinator.directory=PureWindowsPath(r'C:\private')
            coordinator._private();coordinator._write('phase.json',{'n':1})
            with transition.exclusive(coordinator.directory/'lock'):pass
        backend.assert_directory.assert_called_once_with(coordinator.directory,private=True)
        backend.write_new.assert_called_once_with(coordinator.directory/'phase.json',transition._encoded({'n':1}))


if __name__=='__main__':unittest.main()
