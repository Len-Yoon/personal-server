"""Authenticate the installed SCM authority for the initial WSL cutover only."""

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PureWindowsPath
import re
import struct
import sys
from threading import Lock

from .controller import TransitionBlocked


ACTION = 'ha-initial-wsl-cutover'
RECOVERY_ACTION = 'ha-recover-initial-writer'
PROFILE = 'enforced-vm-v1'
SHA = re.compile(r'[a-f0-9]{64}\Z')
SID = re.compile(r'S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\Z')
INSTALL_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z')
APPROVAL_FIELDS = {'schema_version', 'action', 'authority_kind', 'installation_id', 'source_commit',
                   'installed_at', 'plan_sha256'}
RECOVERY_FIELDS = {'attempt_id', 'predecessor_terminal_sha256', 'predecessor_service_sha256'}
ARGS_FIELDS = ('format', 'service_name', 'service_sid', 'python_path', 'python_sha256', 'launcher_path',
               'launcher_sha256', 'plan_path', 'plan_sha256', 'approval_path', 'approval_sha256', 'mode', 'host_sha256')
WRITERS = {'S-1-5-18', 'S-1-5-32-544'}  # SYSTEM and BUILTIN\Administrators
TRUSTED_INSTALLER = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
LEAF_WRITE = 0x500D0156
ANCESTOR_REPLACE = 0x100D0040
PRIVATE_READ = 0x90000001
REPARSE_POINT = 0x0400
DIRECTORY = 0x0010
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _need(condition):
    if not condition:
        raise TransitionBlocked('initial_windows_gate_unverified') from None


def _path(value):
    value = str(value)
    _need(re.match(r'^[A-Za-z]:\\', value) and not re.search(r'[\x00-\x1f/:*?"<>|]', value[3:]))
    if value[3:]:
        _need(all(part and part not in ('.', '..') and not part.endswith(('.', ' '))
                  for part in value[3:].split('\\')))
    return PureWindowsPath(value)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(items):
    value = dict(items)
    _need(len(value) == len(items))
    return value


def _decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=lambda _: _need(False))


def _args(raw):
    rows = raw.decode('utf-8').split('\n')
    _need(len(rows) == len(ARGS_FIELDS) + 1 and rows[-1] == '' and b'\r' not in raw and b'\0' not in raw)
    values = {}
    for key, line in zip(ARGS_FIELDS, rows):
        field, delimiter, value = line.partition('=')
        _need(field == key and delimiter == '=' and bool(value))
        values[key] = value
    _need(values['format'] == 'enforced-service-v1' and values['mode'] == 'serve'
          and SID.fullmatch(values['service_sid'])
          and all(SHA.fullmatch(values[key]) for key in ARGS_FIELDS if key.endswith('_sha256')))
    return values


def initial_service_identity(scope_sha256):
    _need(type(scope_sha256) is str and SHA.fullmatch(scope_sha256))
    name = f'N100HA_{scope_sha256[:24]}_controller'
    digest = hashlib.sha1(name.upper().encode('utf-16-le'), usedforsecurity=False).digest()
    sid = 'S-1-5-80-' + '-'.join(str(number) for number in struct.unpack('<5I', digest))
    return name, sid


def _validate(plan_raw, approval_raw, settings, sid, now):
    plan_pin, approval_pin = _digest(plan_raw), _digest(approval_raw)
    _need(plan_pin == settings['plan_sha256'] and approval_pin == settings['approval_sha256'])
    plan, approval = _decode(plan_raw), _decode(approval_raw)
    _need(type(plan) is dict and type(approval) is dict and approval.keys() == APPROVAL_FIELDS
          and type(approval['schema_version']) is int and approval['schema_version'] == 1
          and approval['authority_kind'] == 'installed-runtime'
          and approval['plan_sha256'] == plan_pin
          and type(plan.get('schema_version')) is int and plan['schema_version'] == 2
          and plan.get('profile') == PROFILE and plan.get('node') == 'A'
          and type(plan.get('generation')) is int and plan['generation'] == 1
          and type(plan.get('timeline')) is int and plan['timeline'] == 1
          and not {'_approval_action', '_installed_runtime'} & plan.keys())
    mode = plan.get('initial_wsl_cutover')
    _need(type(mode) is dict and mode == {'schema_version': 1, 'primary_node': 'A', 'partner_node': 'B'}
          and type(mode['schema_version']) is int)
    action = approval['action']
    from .initial_unpublished import ACTION as UNPUBLISHED_ACTION, scope as unpublished_scope
    unpublished='initial_unpublished_recovery' in plan
    if unpublished:
        unpublished_scope(plan)
        _need('initial_cutover_recovery' in plan and action==UNPUBLISHED_ACTION)
    from .initial_postboot import ACTION as POSTBOOT_ACTION, scope as postboot_scope
    postboot = 'initial_recovery_postboot' in plan
    if postboot:
        postboot_scope(plan)
        _need('initial_cutover_recovery' in plan and action == POSTBOOT_ACTION)
    rearming = 'initial_recovery_rearm' in plan
    if rearming:
        from .initial_pg_lease import REARM_ACTION, rearm_scope
        rearm_scope(plan)
        _need('initial_cutover_recovery' in plan and action == REARM_ACTION)
    original_name, original_sid = initial_service_identity(plan.get('installation_id'))
    if 'initial_cutover_recovery' in plan:
        recovery = plan['initial_cutover_recovery']
        _need(action == (UNPUBLISHED_ACTION if unpublished else (POSTBOOT_ACTION if postboot else (REARM_ACTION if rearming else RECOVERY_ACTION))) and type(recovery) is dict and recovery.keys() == RECOVERY_FIELDS
              and all(type(value) is str and SHA.fullmatch(value) for value in recovery.values()))
        name, expected_sid = initial_service_identity(recovery['attempt_id'])
        _need(name != original_name and expected_sid != original_sid)
    else:
        _need(action == ACTION)
        name, expected_sid = original_name, original_sid
    if 'initial_effect_quarantine' in plan:
        _need(action == (UNPUBLISHED_ACTION if unpublished else (POSTBOOT_ACTION if postboot else (REARM_ACTION if rearming else RECOVERY_ACTION))))
        from .initial_pg_lease import InitialPgLeaseRecovery
        InitialPgLeaseRecovery.quarantine_binding(plan)
    configuration = plan.get('windows_enforced')
    _need(type(configuration) is dict and configuration.get('service_name') == settings['service_name'] == name
          and configuration.get('service_sid') == settings['service_sid'] == sid == expected_sid
          and approval['installation_id'] == plan['installation_id']
          and type(approval['source_commit']) is str and re.fullmatch(r'[a-f0-9]{40}', approval['source_commit'])
          and approval['source_commit'] == plan.get('source_commit')
          and type(approval['installed_at']) is str and INSTALL_TIME.fullmatch(approval['installed_at']))
    installed = datetime.fromisoformat(approval['installed_at'].replace('Z', '+00:00'))
    _need(installed < now)
    return plan | {'_approval_action': action}, plan_pin, approval_pin



def validate_rearm_seal(plan, plan_pin, files):
    """Validate TCB-writable snapshots while the native gate holds their read handles."""
    from .initial_pg_lease import rearm_scope
    rearm = rearm_scope(plan)
    _need(set(files) == {'rearm-predecessor-state.json', 'rearm-predecessor-binding.json',
                         'rearm-predecessor-args.txt', 'rearm-predecessor-seal.json'})
    for name, key in (('state.json', 'predecessor_terminal_sha256'),
                      ('binding.json', 'predecessor_binding_sha256'), ('args.txt', 'predecessor_args_sha256')):
        _need(_digest(files['rearm-predecessor-' + name]) == rearm[key])
    previous = _decode(files['rearm-predecessor-state.json'])
    binding = _decode(files['rearm-predecessor-binding.json'])
    settings = _args(files['rearm-predecessor-args.txt'])
    seal = _decode(files['rearm-predecessor-seal.json'])
    identity = plan['windows_enforced']
    handoff = plan['initial_cutover_handoff']
    stable = {'schema_version', 'kind', 'generation', 'attempt_id', 'legacy_plan_sha256',
              'restore_receipt_sha256', 'activation_intent_sha256', 'controller_plan_sha256', 'recovery',
              'revision', 'phase', 'sequence'}
    binding_keys = {'attempt_id', 'controller_plan_sha256', 'first_write_proof_sha256', 'legacy_plan_sha256',
                    'service_created_ticks', 'service_image_path_sha256', 'service_name', 'service_pid', 'state_path'}
    _need(type(previous) is dict and set(previous) in (stable, stable | {'failure_code'})
          and previous['schema_version'] == 1 and type(previous['schema_version']) is int
          and previous['kind'] == 'initial-wsl-cutover-recovery'
          and previous['phase'] in ('lease_lost', 'local_revoke_observed')
          and type(previous['revision']) is int and previous['revision'] == 4
          and type(previous['generation']) is int and previous['generation'] == 1
          and type(previous['sequence']) is int and previous['sequence'] > 0
          and previous['recovery'] == plan['initial_cutover_recovery']
          and all(previous[key] == handoff[key] for key in ('attempt_id', 'legacy_plan_sha256',
                                                           'restore_receipt_sha256', 'activation_intent_sha256')))
    _need(type(binding) is dict and set(binding) == binding_keys
          and binding['controller_plan_sha256'] == previous['controller_plan_sha256'] != plan_pin
          and binding['legacy_plan_sha256'] == previous['legacy_plan_sha256']
          and binding['attempt_id'] == previous['attempt_id']
          and binding['service_name'] == settings['service_name'] == identity['service_name']
          and settings['service_sid'] == identity['service_sid']
          and settings['plan_sha256'] == binding['controller_plan_sha256']
          and settings['host_sha256'] == identity['service_image_sha256']
          and type(binding['service_pid']) is int and binding['service_pid'] > 0
          and type(binding['service_created_ticks']) is str and re.fullmatch(r'[1-9][0-9]{0,19}', binding['service_created_ticks'])
          and all(type(binding[key]) is str and SHA.fullmatch(binding[key]) for key in (
              'controller_plan_sha256', 'first_write_proof_sha256', 'service_image_path_sha256'))
          and _path(settings['plan_path']).parent / 'journal' / 'controller-state.json' == _path(binding['state_path']))
    expected = {'schema_version': 1, 'plan_sha256': plan_pin, 'rearm': rearm,
                'windows_boot_sha256': plan['windows_initial_observer']['windows_boot_sha256'],
                'service': {'service_name': identity['service_name'], 'service_sid': identity['service_sid'],
                            'state': 'Stopped', 'start_mode': 'Disabled', 'process_id': 0,
                            'service_image_path_sha256': binding['service_image_path_sha256']}}
    _need(type(seal) is dict and seal == expected and type(seal.get('schema_version')) is int
          and type(seal.get('service')) is dict and type(seal['service'].get('process_id')) is int)
    return rearm

def _check_acl(owner, entries, *, depth, os_drive_root, private_reader):
    _need(owner in WRITERS or depth > 1 and os_drive_root and owner == TRUSTED_INSTALLER)
    _need(bool(entries))
    for allowed, inherit_only, mask, sid in entries:
        if not allowed:
            continue
        if (depth <= 1 or not inherit_only) and mask & (LEAF_WRITE if depth <= 1 else ANCESTOR_REPLACE):
            _need(sid in WRITERS)
        if private_reader is not None and depth <= 1 and not inherit_only and mask & PRIVATE_READ:
            _need(sid in WRITERS or sid == private_reader)


class _TokenUser(ctypes.Structure):
    _fields_ = [('sid', ctypes.c_void_p), ('attributes', wintypes.DWORD)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                ('exe', wintypes.WCHAR * 260)]


class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
                ('written', wintypes.FILETIME), ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD), ('index_high', wintypes.DWORD),
                ('index_low', wintypes.DWORD)]

    def identity(self):
        return (self.attributes, self.volume, self.links, self.index_high, self.index_low, self.size,
                self.created.dwHighDateTime, self.created.dwLowDateTime,
                self.written.dwHighDateTime, self.written.dwLowDateTime)

    @property
    def size(self):
        return (self.size_high << 32) | self.size_low


class _AclInfo(ctypes.Structure):
    _fields_ = [('count', wintypes.DWORD), ('used', wintypes.DWORD), ('free', wintypes.DWORD)]


class _Ace(ctypes.Structure):
    _fields_ = [('kind', wintypes.BYTE), ('flags', wintypes.BYTE), ('size', wintypes.WORD), ('mask', wintypes.DWORD)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [('length', wintypes.USHORT), ('maximum', wintypes.USHORT), ('buffer', wintypes.LPWSTR)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.ULONG), ('root', wintypes.HANDLE), ('name', ctypes.POINTER(_UnicodeString)),
                ('attributes', wintypes.ULONG), ('security', ctypes.c_void_p), ('qos', ctypes.c_void_p)]


def _api(library, name, result, *parameters):
    function = getattr(library, name)
    function.argtypes = parameters
    function.restype = result
    return function


class _Native:
    def __init__(self):
        handle, dword, boolean, pointer = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        out = ctypes.POINTER
        filetime = out(wintypes.FILETIME)
        kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        security = ctypes.WinDLL('advapi32.dll', use_last_error=True)
        ntdll = ctypes.WinDLL('ntdll.dll')
        self.close = _api(kernel, 'CloseHandle', boolean, handle)
        self.free = _api(kernel, 'LocalFree', handle, handle)
        self.current = _api(kernel, 'GetCurrentProcess', handle)
        self.session = _api(kernel, 'ProcessIdToSessionId', boolean, dword, out(dword))
        self.snapshot = _api(kernel, 'CreateToolhelp32Snapshot', handle, dword, dword)
        self.first = _api(kernel, 'Process32FirstW', boolean, handle, out(_ProcessEntry))
        self.next = _api(kernel, 'Process32NextW', boolean, handle, out(_ProcessEntry))
        self.process = _api(kernel, 'OpenProcess', handle, dword, boolean, dword)
        self.times = _api(kernel, 'GetProcessTimes', boolean, handle, filetime, filetime, filetime, filetime)
        self.image = _api(kernel, 'QueryFullProcessImageNameW', boolean, handle, dword, wintypes.LPWSTR, out(dword))
        self.exitcode = _api(kernel, 'GetExitCodeProcess', boolean, handle, out(dword))
        self.windows = _api(kernel, 'GetWindowsDirectoryW', wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
        self.open = _api(kernel, 'CreateFileW', handle, wintypes.LPCWSTR, dword, dword, pointer, dword, dword, handle)
        self.nt_open = _api(ntdll, 'NtOpenFile', wintypes.LONG, out(handle), wintypes.ULONG, out(_ObjectAttributes),
                            pointer, wintypes.ULONG, wintypes.ULONG)
        self.file_info = _api(kernel, 'GetFileInformationByHandle', boolean, handle, out(_FileInfo))
        self.final_path = _api(kernel, 'GetFinalPathNameByHandleW', dword, handle, wintypes.LPWSTR, dword, dword)
        self.read = _api(kernel, 'ReadFile', boolean, handle, pointer, dword, out(dword), pointer)
        self.token = _api(security, 'OpenProcessToken', boolean, handle, dword, out(handle))
        self.token_info = _api(security, 'GetTokenInformation', boolean, handle, ctypes.c_int, pointer, dword, out(dword))
        self.sid_text = _api(security, 'ConvertSidToStringSidW', boolean, pointer, out(wintypes.LPWSTR))
        self.valid_sid = _api(security, 'IsValidSid', boolean, pointer)
        self.sid_size = _api(security, 'GetLengthSid', dword, pointer)
        self.security = _api(security, 'GetSecurityInfo', dword, handle, ctypes.c_int, dword,
                             out(pointer), out(pointer), out(pointer), out(pointer), out(pointer))
        self.acl_info = _api(security, 'GetAclInformation', boolean, pointer, pointer, dword, ctypes.c_int)
        self.ace = _api(security, 'GetAce', boolean, pointer, dword, out(pointer))
        directory = ctypes.create_unicode_buffer(32768)
        _need(0 < self.windows(directory, len(directory)) < len(directory))
        self.os_root = PureWindowsPath(directory.value).anchor.casefold()

    def _sid(self, sid):
        value = wintypes.LPWSTR()
        _need(sid and self.valid_sid(sid) and self.sid_text(sid, ctypes.byref(value)))
        try:
            return value.value
        finally:
            self.free(ctypes.cast(value, ctypes.c_void_p))

    def _process_sid(self, process, pid):
        session = wintypes.DWORD()
        _need(self.session(pid, ctypes.byref(session)) and session.value == 0)
        token = wintypes.HANDLE()
        _need(self.token(process, 0x0008, ctypes.byref(token)))  # TOKEN_QUERY
        try:
            size = wintypes.DWORD()
            _need(not self.token_info(token, 1, None, 0, ctypes.byref(size)) and 0 < size.value <= 4096)
            buffer = ctypes.create_string_buffer(size.value)
            _need(self.token_info(token, 1, buffer, len(buffer), ctypes.byref(size)))  # TokenUser
            user = _TokenUser.from_buffer(buffer)
            sid = self._sid(user.sid)
            _need(SID.fullmatch(sid) and self.token_info(token, 12, ctypes.byref(session), ctypes.sizeof(session),
                                                       ctypes.byref(size)) and session.value == 0)  # TokenSessionId
            return sid
        finally:
            self.close(token)

    def _parent_pid(self, pid):
        snapshot = self.snapshot(2, 0)  # TH32CS_SNAPPROCESS
        _need(snapshot not in (None, INVALID_HANDLE))
        try:
            entry = _ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            seen = self.first(snapshot, ctypes.byref(entry))
            while seen:
                if entry.pid == pid:
                    _need(0 < entry.parent != pid)
                    return entry.parent
                seen = self.next(snapshot, ctypes.byref(entry))
            _need(False)
        finally:
            self.close(snapshot)

    def _creation(self, handle):
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        _need(self.times(handle, *(ctypes.byref(item) for item in (created, exited, kernel, user))))
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def _process_image(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        _need(self.image(handle, 0, buffer, ctypes.byref(size)))
        return _path(buffer.value)

    @contextmanager
    def service(self):
        pid, current = os.getpid(), self.current()
        sid = self._process_sid(current, pid)
        parent_pid = self._parent_pid(pid)
        parent = self.process(0x1000, False, parent_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        _need(parent)
        try:
            _need(self._process_sid(parent, parent_pid) == sid and 0 < self._creation(parent) < self._creation(current))
            code = wintypes.DWORD()
            _need(self.exitcode(parent, ctypes.byref(code)) and code.value == 259)
            host = self._process_image(parent)
            _need(host.name.casefold() == 'windows-service-host.exe')
            yield sid, host, self._process_image(current)
        finally:
            self.close(parent)

    def _acl(self, handle, path, depth, private_reader):
        owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
        try:
            _need(self.security(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                ctypes.byref(descriptor)) == 0 and dacl.value)  # owner and DACL
            info = _AclInfo()
            _need(self.acl_info(dacl, ctypes.byref(info), ctypes.sizeof(info), 2) and 0 < info.count <= 1024)
            entries = []
            for index in range(info.count):
                pointer = ctypes.c_void_p()
                _need(self.ace(dacl, index, ctypes.byref(pointer)) and pointer.value)
                ace = _Ace.from_address(pointer.value)
                sid = pointer.value + ctypes.sizeof(_Ace)
                _need(ace.kind in (0, 1) and ace.size >= ctypes.sizeof(_Ace) + 8
                      and self.valid_sid(sid) and ctypes.sizeof(_Ace) + self.sid_size(sid) == ace.size)
                entries.append((ace.kind == 0, bool(ace.flags & 0x08), ace.mask, self._sid(sid)))
            _check_acl(self._sid(owner.value), entries, depth=depth, os_drive_root=str(path).casefold() == self.os_root,
                       private_reader=private_reader)
        finally:
            if descriptor.value:
                self.free(descriptor)

    def _info(self, handle):
        info = _FileInfo()
        _need(self.file_info(handle, ctypes.byref(info)))
        return info

    def _open_directory(self, path):
        # This metadata handle requires only READ_CONTROL and FILE_READ_ATTRIBUTES.
        # The installation root deliberately grants neither SYNCHRONIZE nor directory listing.
        value = '\\??\\' + str(path)
        size = len(value.encode('utf-16-le'))
        _need(size <= 65532)
        name = _UnicodeString(size, size + 2, value)
        attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), None, ctypes.pointer(name), 0x40, None, None)
        status_block = (ctypes.c_void_p * 2)()  # IO_STATUS_BLOCK
        handle = wintypes.HANDLE()
        status = self.nt_open(ctypes.byref(handle), 0x20080, ctypes.byref(attributes), status_block,
                              7, 0x200000)  # FILE_OPEN_REPARSE_POINT
        _need(status == 0 and handle.value not in (None, INVALID_HANDLE))
        return handle.value

    @contextmanager
    def _handle(self, path, *, directory, depth, private_reader):
        handle = (self._open_directory(path) if directory else
                  self.open(str(path), 0x80020000, 1, None, 3, 0x00200000, None))
        _need(handle not in (None, INVALID_HANDLE))
        try:
            info = self._info(handle)
            _need(not info.attributes & REPARSE_POINT and bool(info.attributes & DIRECTORY) == directory)
            buffer = ctypes.create_unicode_buffer(32768)
            _need(0 < self.final_path(handle, buffer, len(buffer), 0) < len(buffer)
                  and buffer.value.startswith('\\\\?\\') and _path(buffer.value[4:]) == path)
            self._acl(handle, path, depth, private_reader)
            yield handle, info
        finally:
            self.close(handle)

    @contextmanager
    def protected_file(self, path, limit, *, private_reader=None):
        _need(path.anchor.casefold() == self.os_root)
        with ExitStack() as stack:
            for depth, parent in reversed(tuple(enumerate(path.parents, start=1))):
                stack.enter_context(self._handle(parent, directory=True, depth=depth, private_reader=private_reader))
            handle, before = stack.enter_context(self._handle(path, directory=False, depth=0, private_reader=private_reader))
            _need(before.links == 1 and bool(before.index_high or before.index_low) and 0 < before.size <= limit)
            buffer, count = ctypes.create_string_buffer(before.size), wintypes.DWORD()
            _need(self.read(handle, buffer, before.size, ctypes.byref(count), None) and count.value == before.size
                  and self._info(handle).identity() == before.identity())
            yield buffer.raw


class InitialWindowsGate:
    """PgBackend guard for one verified installed initial-cutover SCM service."""

    def __init__(self, plan, approval):
        self.plan_path, self.approval_path = _path(plan), _path(approval)
        _need(self.plan_path.name == 'plan.json' and self.approval_path == self.plan_path.with_name('approval.json'))
        self.plan_data = self.plan_sha256 = self.approval_sha256 = None
        self._installed_runtime = False
        self._lock = Lock()

    @contextmanager
    def guard(self):
        _need(sys.platform == 'win32' and sys.flags.isolated and sys.dont_write_bytecode
              and ctypes.sizeof(ctypes.c_void_p) == 8 and self._lock.acquire(blocking=False))
        try:
            with ExitStack() as stack:
                try:
                    native = _Native()
                    sid, host, python = stack.enter_context(native.service())
                    args = stack.enter_context(native.protected_file(host.with_name('service.args'), 16384, private_reader=sid))
                    settings = _args(args)
                    launcher = _path(sys.argv[0])
                    _need(settings['service_sid'] == sid and _path(settings['plan_path']) == self.plan_path
                          and _path(settings['approval_path']) == self.approval_path
                          and _path(settings['python_path']) == python == _path(sys.executable)
                          and _path(settings['launcher_path']) == launcher)
                    for key, path in (('host', host), ('python', python), ('launcher', launcher)):
                        raw = stack.enter_context(native.protected_file(path, 64 * 1024 * 1024))
                        _need(_digest(raw) == settings[key + '_sha256'])
                    plan_raw = stack.enter_context(native.protected_file(self.plan_path, 1048576, private_reader=sid))
                    approval_raw = stack.enter_context(native.protected_file(self.approval_path, 16384, private_reader=sid))
                    verified = _validate(plan_raw, approval_raw, settings, sid, datetime.now(timezone.utc))
                except (OSError, ValueError, TypeError, KeyError, RecursionError, AttributeError):
                    _need(False)
                self._predecessor_terminal=None
                self._unpublished_verified=None
                if 'initial_unpublished_recovery' in verified[0]:
                    from .initial_unpublished import FILES as UNPUBLISHED_FILES, validate_seal as validate_unpublished
                    snapshots={name:stack.enter_context(native.protected_file(
                        self.plan_path.with_name(name),1048576,private_reader=sid)) for name in UNPUBLISHED_FILES}
                    self._unpublished_verified=validate_unpublished(verified[0],verified[1],snapshots)
                    self._predecessor_terminal=snapshots[UNPUBLISHED_FILES[0]]
                self._postboot_stopped_sha256 = None
                self._postboot_verified = None
                if 'initial_recovery_postboot' in verified[0]:
                    from .initial_postboot import FILES, files_for, validate_seal
                    snapshots = {name: stack.enter_context(native.protected_file(
                        self.plan_path.with_name(name), 65536, private_reader=sid)) for name in files_for(verified[0])}
                    self._postboot_verified = validate_seal(verified[0], verified[1], snapshots)
                    if 'successor_service_image_sha256' in self._postboot_verified:
                        image_path_sha256 = _digest(f'"{host}"'.encode('utf-8'))
                        _need(settings['host_sha256'] == self._postboot_verified['successor_service_image_sha256']
                              and image_path_sha256 == self._postboot_verified['successor_service_image_path_sha256'])
                    self._predecessor_terminal=snapshots[FILES[0]]
                    self._postboot_stopped_sha256 = _decode(snapshots['postboot-resume-evidence.json'])['postboot_stopped_receipt_sha256']
                self._rearm_verified = None
                if 'initial_recovery_rearm' in verified[0]:
                    snapshots = {name: stack.enter_context(native.protected_file(
                        self.plan_path.with_name(name), 65536, private_reader=sid))
                        for name in ('rearm-predecessor-state.json', 'rearm-predecessor-binding.json',
                                     'rearm-predecessor-args.txt', 'rearm-predecessor-seal.json')}
                    self._rearm_verified = validate_rearm_seal(verified[0], verified[1], snapshots)
                self.plan_data, self.plan_sha256, self.approval_sha256 = verified
                self._installed_runtime = True
                try:
                    yield
                finally:
                    self._predecessor_terminal=None
                    self._unpublished_verified=None
                    self._postboot_stopped_sha256 = None
                    self._postboot_verified = None
                    self._rearm_verified = None
                    self._installed_runtime = False
                    self.plan_data = self.plan_sha256 = self.approval_sha256 = None
        finally:
            self._lock.release()
