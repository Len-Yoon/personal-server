"""Native Windows custody for private inputs and append-only coordinator files.

No ACL is relaxed or repaired. Existing paths must already be trusted. Reads
hold no-write/no-delete handles; new files receive a protected private DACL at
creation. Journal locks are per-file, separate from the authority service mutex.
"""

from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import hashlib
import sys

from .controller import TransitionBlocked
from . import initial_windows_gate as gate

ERROR = 'windows_protected_io_unverified'
TRUSTED = frozenset(gate.WRITERS)
SYSTEM_POWERSHELL = ('WindowsPowerShell', 'v1.0', 'powershell.exe')


def _need(value):
    if not value:
        raise TransitionBlocked(ERROR)


def _identity(info,directory):
    # Child creation changes a directory's timestamps, not its custody.
    return ((info.attributes,info.volume,info.links,info.index_high,info.index_low,
             info.created.dwHighDateTime,info.created.dwLowDateTime) if directory else info.identity())


def validate_acl(owner, entries, *, depth, private, allowed, drive_root=False, protected=True,
                 checked_os_child=False):
    """Conservative effective-access check; denies never mask unsafe allows."""
    _need(owner in allowed or (depth>1 and drive_root and owner==gate.TRUSTED_INSTALLER))
    _need(bool(entries) and (depth!=0 or protected))
    for kind,flags,mask,sid in entries:
        _need(kind in (0,1))
        if kind!=0:
            continue
        inherited_only=bool(flags&8)
        # Only the bounded OS-image reader may use the actual child's checked
        # ACL instead of a parent's non-effective inheritance template.
        if checked_os_child and depth>0 and inherited_only:
            continue
        if (depth<=1 or not inherited_only) and mask&(gate.LEAF_WRITE if depth<=1 else gate.ANCESTOR_REPLACE):
            _need(sid in allowed)
        if private and depth<=1 and not inherited_only and mask&gate.PRIVATE_READ:
            _need(sid in allowed)


@dataclass(frozen=True)
class WindowsFileStamp:
    path: str
    identity: tuple
    acl_sha256: str
    ancestors: tuple
    limit: int
    system_executable: bool = False


class _SecurityAttributes(ctypes.Structure):
    _fields_=[('length',wintypes.DWORD),('descriptor',ctypes.c_void_p),('inherit',wintypes.BOOL)]


class WindowsProtectedIO:
    def __init__(self, *, admin_only=False, _native=None):
        _need(type(admin_only) is bool and (sys.platform=='win32' or _native is not None))
        self.native=gate._Native() if _native is None else _native
        self.admin_only=admin_only
        if _native is not None:
            self.allowed=TRUSTED|{_native.owner_sid}
            self.owner_sid=_native.owner_sid
        else:
            token=wintypes.HANDLE();size=wintypes.DWORD()
            _need(self.native.token(self.native.current(),8,ctypes.byref(token)))
            try:
                _need(not self.native.token_info(token,1,None,0,ctypes.byref(size)) and 0<size.value<=4096)
                buf=ctypes.create_string_buffer(size.value)
                _need(self.native.token_info(token,1,buf,len(buf),ctypes.byref(size)))
                self.owner_sid=self.native._sid(gate._TokenUser.from_buffer(buf).sid)
            finally:self.native.close(token)
            self.allowed=TRUSTED|{self.owner_sid}
        if admin_only:self.allowed=TRUSTED
        kernel=ctypes.WinDLL('kernel32.dll',use_last_error=True) if _native is None else _native.kernel
        security=ctypes.WinDLL('advapi32.dll',use_last_error=True) if _native is None else _native.security_api
        api=gate._api
        self.file_type=api(kernel,'GetFileType',wintypes.DWORD,wintypes.HANDLE)
        self.system_directory=api(kernel,'GetSystemDirectoryW',wintypes.UINT,wintypes.LPWSTR,wintypes.UINT)
        self.flush=api(kernel,'FlushFileBuffers',wintypes.BOOL,wintypes.HANDLE)
        self.write=api(kernel,'WriteFile',wintypes.BOOL,wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,
                       ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
        self.seek=api(kernel,'SetFilePointerEx',wintypes.BOOL,wintypes.HANDLE,ctypes.c_longlong,
                      ctypes.POINTER(ctypes.c_longlong),wintypes.DWORD)
        self.sd_length=api(security,'GetSecurityDescriptorLength',wintypes.DWORD,ctypes.c_void_p)
        self.sd_control=api(security,'GetSecurityDescriptorControl',wintypes.BOOL,ctypes.c_void_p,
                            ctypes.POINTER(wintypes.WORD),ctypes.POINTER(wintypes.DWORD))
        self.sddl=api(security,'ConvertStringSecurityDescriptorToSecurityDescriptorW',wintypes.BOOL,
                      wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p)

    def _path(self,path):
        path=gate._path(path)
        _need(path.anchor.casefold()==self.native.os_root)
        return path

    def _acl(self,handle,path,depth,private):
        native=self.native
        owner,dacl,descriptor=(ctypes.c_void_p() for _ in range(3))
        try:
            _need(native.security(handle,1,5,ctypes.byref(owner),None,ctypes.byref(dacl),None,
                                  ctypes.byref(descriptor))==0 and dacl.value and descriptor.value)
            control=wintypes.WORD();revision=wintypes.DWORD();info=gate._AclInfo()
            _need(self.sd_control(descriptor,ctypes.byref(control),ctypes.byref(revision))
                  and control.value&4 and native.acl_info(dacl,ctypes.byref(info),ctypes.sizeof(info),2)
                  and 0<info.count<=1024)
            entries=[]
            for index in range(info.count):
                pointer=ctypes.c_void_p()
                _need(native.ace(dacl,index,ctypes.byref(pointer)) and pointer.value)
                ace=gate._Ace.from_address(pointer.value);sid=pointer.value+ctypes.sizeof(gate._Ace)
                _need(ace.kind in (0,1) and native.valid_sid(sid)
                      and ace.size==ctypes.sizeof(gate._Ace)+native.sid_size(sid))
                entries.append((ace.kind,ace.flags,ace.mask,native._sid(sid)))
            # Read-only SSH inputs may inherit an already safe parent DACL.
            # Coordinator leaves/directories always require protected DACLs.
            validate_acl(native._sid(owner.value),entries,depth=depth,private=private,allowed=self.allowed,
                         drive_root=str(path).casefold()==native.os_root,
                         protected=not self.admin_only or bool(control.value&0x1000),
                         checked_os_child=getattr(self,'_checked_os_child',False))
            length=self.sd_length(descriptor);_need(0<length<=65536)
            return hashlib.sha256(ctypes.string_at(descriptor,length)).hexdigest()
        finally:
            if descriptor.value:native.free(descriptor)

    def _verify(self,handle,path,*,directory,depth,private):
        info=self.native._info(handle)
        _need(self.file_type(handle)==1 and not info.attributes&gate.REPARSE_POINT
              and bool(info.attributes&gate.DIRECTORY)==directory
              and (directory or info.links==1 or
                   (getattr(self,'_checked_os_child',False) and depth==0 and not private and info.links>1))
              and bool(info.index_high or info.index_low))
        buffer=ctypes.create_unicode_buffer(32768)
        _need(0<self.native.final_path(handle,buffer,len(buffer),0)<len(buffer)
              and buffer.value.startswith('\\\\?\\') and gate._path(buffer.value[4:])==path)
        return info,self._acl(handle,path,depth,private)

    def _open(self,path,access,disposition,flags,security=None,share=1):
        handle=self.native.open(str(path),access,share,security,disposition,flags,None)
        if handle in (None,gate.INVALID_HANDLE):
            error=ctypes.get_last_error()
            if error in (2,3):raise FileNotFoundError(ERROR)
            if error in (80,183):raise FileExistsError(ERROR)
            raise TransitionBlocked(ERROR)
        return handle

    @contextmanager
    def _held(self,path,*,directory,depth,private):
        handle=self._open(path,0x20080 if directory else 0x80020000,3,
                          0x02200000 if directory else 0x00200000)
        try:
            info,acl=self._verify(handle,path,directory=directory,depth=depth,private=private)
            yield handle,info,acl
            current,current_acl=self._verify(handle,path,directory=directory,depth=depth,private=private)
            _need(_identity(current,directory)==_identity(info,directory) and current_acl==acl)
        finally:self.native.close(handle)

    @contextmanager
    def _parents(self,path,private):
        with ExitStack() as stack:
            stamps=[]
            for depth,parent in reversed(tuple(enumerate(path.parents,start=1))):
                _,info,acl=stack.enter_context(self._held(parent,directory=True,depth=depth,private=private))
                stamps.append((str(parent),_identity(info,True),acl))
            yield tuple(stamps)

    def _read_handle(self,handle,info,limit):
        _need(0<info.size<=limit)
        buffer=ctypes.create_string_buffer(info.size);count=wintypes.DWORD()
        _need(self.native.read(handle,buffer,info.size,ctypes.byref(count),None) and count.value==info.size)
        return buffer.raw

    def read_stamp(self,path,limit,*,private=True):
        _need(type(limit) is int and 0<limit<=67108864 and type(private) is bool)
        path=self._path(path)
        with self._parents(path,private) as ancestors,self._held(path,directory=False,depth=0,private=private) as held:
            handle,info,acl=held;raw=self._read_handle(handle,info,limit)
            stamp=WindowsFileStamp(str(path),info.identity(),acl,ancestors,limit)
        return raw,stamp

    def same_file(self,path,stamp,*,private=True):
        try:
            _need(type(stamp) is WindowsFileStamp)
            _,actual=(self.system_executable_stamp(path,stamp.limit) if stamp.system_executable else
                      self.read_stamp(path,stamp.limit,private=private))
            return actual==stamp
        except (OSError,ValueError,TransitionBlocked):return False

    def system_directory_path(self):
        buffer=ctypes.create_unicode_buffer(32768)
        _need(0<self.system_directory(buffer,len(buffer))<len(buffer))
        return self._path(buffer.value)

    def system_executable_stamp(self,path,limit=16777216):
        """Read only the canonical OS-owned OpenSSH and PowerShell images.

        TrustedInstaller is an OS authority here only. This exception never
        applies to key/config files or arbitrary caller-selected executables.
        """
        path=self._path(path);system=self.system_directory_path()
        _need(path in (system/'OpenSSH'/'ssh.exe',system/'OpenSSH'/'ssh-keygen.exe',
                       system.joinpath(*SYSTEM_POWERSHELL)))
        # Separate instance: no change to the private-reader ACL policy, even
        # while another thread is validating a key with this object.
        reader=WindowsProtectedIO(admin_only=False)
        reader.allowed=TRUSTED|{gate.TRUSTED_INSTALLER}
        reader._checked_os_child=True
        raw,stamp=reader.read_stamp(path,limit,private=False)
        return raw,replace(stamp,system_executable=True)

    def assert_directory(self,path,*,private=True):
        path=self._path(path)
        with self._parents(path,private),self._held(path,directory=True,depth=0,private=private):pass

    def _descriptor(self):
        owner='BA' if self.admin_only else self.owner_sid
        extra='' if self.admin_only or self.owner_sid in TRUSTED else '(A;;FA;;;'+self.owner_sid+')'
        text='O:'+owner+'D:P(A;;FA;;;SY)(A;;FA;;;BA)'+extra
        descriptor=ctypes.c_void_p()
        _need(self.sddl(text,1,ctypes.byref(descriptor),None) and descriptor.value)
        return descriptor

    def write_new(self,path,raw):
        _need(type(raw) is bytes and 0<len(raw)<=67108864)
        path=self._path(path)
        with self._parents(path,True):
            descriptor=self._descriptor()
            try:
                sa=_SecurityAttributes(ctypes.sizeof(_SecurityAttributes),descriptor,False)
                handle=self._open(path,0xc0020000,1,0x80200000,ctypes.byref(sa))
                try:
                    _,initial_acl=self._verify(handle,path,directory=False,depth=0,private=True)
                    count=wintypes.DWORD();buffer=ctypes.create_string_buffer(raw)
                    _need(self.write(handle,buffer,len(raw),ctypes.byref(count),None) and count.value==len(raw)
                          and self.flush(handle))
                    info,actual_acl=self._verify(handle,path,directory=False,depth=0,private=True)
                    _need(actual_acl==initial_acl and info.size==len(raw) and self.seek(handle,0,None,0)
                          and self._read_handle(handle,info,len(raw))==raw)
                finally:self.native.close(handle)
            finally:self.native.free(descriptor)

    @contextmanager
    def exclusive(self,path):
        path=self._path(path)
        with self._parents(path,True):
            descriptor=self._descriptor()
            try:
                sa=_SecurityAttributes(ctypes.sizeof(_SecurityAttributes),descriptor,False)
                handle=self._open(path,0xc0020000,4,0x80200000,ctypes.byref(sa),share=0)
                try:
                    info,initial_acl=self._verify(handle,path,directory=False,depth=0,private=True)
                    _need(info.size==0)
                    yield
                    after,actual_acl=self._verify(handle,path,directory=False,depth=0,private=True)
                    _need(after.identity()==info.identity() and actual_acl==initial_acl)
                finally:self.native.close(handle)
            finally:self.native.free(descriptor)
