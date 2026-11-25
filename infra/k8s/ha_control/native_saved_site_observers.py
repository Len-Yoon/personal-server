"""Read-only site binding for the saved-reboot observer adapter.

All site paths, original source pins and registry labels come from explicit
configuration. Raw descriptor/callback/starter bytes remain in memory. Each
pinned source is fetched in FRAME_BYTES-bounded slices under native ACL, boot
and stopped-writer checks. Only the returned WindowsSavedObservers.apply method
may change Windows; this factory/preflight never starts tasks or writes files.
"""
import base64
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from . import native_stdio_bridge as bridge
from .native_saved_observers import ROLES, WindowsSavedObservers, prepare_role

ERROR = 'saved_observer_site_unconfirmed'
SLICE_BYTES = 2200
SNAPSHOT_KEYS = {'mode', 'windows_boot_sha256', 'vm_id_sha256', 'states',
                 'plan_sha256', 'binding_sha256', 'terminal_sha256'}


def _need(value):
    bridge._need(value, ERROR)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(ERROR) from None
    _need(type(value) is dict)
    return value


def _path(value):
    _need(type(value) is str and re.fullmatch(
        r'C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\[A-Za-z0-9_.-]+', value)
        and not value.endswith('.'))
    return PureWindowsPath(value)


def observer_nonce(operation_nonce, role, boot, descriptor_pin, callback_pin):
    """A durable issued operation produces the same fresh instance on reconciliation."""
    _need(role in ROLES and all(bridge._hash(v) for v in
          (operation_nonce, boot, descriptor_pin, callback_pin)))
    return _sha(_encoded({'domain': 'n100ha-saved-observer-v1', 'operation_nonce': operation_nonce,
                          'role': role, 'boot_sha256': boot, 'descriptor_sha256': descriptor_pin,
                          'callback_sha256': callback_pin}))[:32]


class SavedSiteObservers:
    """Callable implementation of platform.saved_observers(operation, receipts, deadline).

    config extends the existing site input-pins document with plan_path,
    binding_path, starter_path, starter_sha256 and saved_reboot_script_sha256.
    Every role retains descriptor_path/descriptor_sha256/callback_sha256 and an
    explicit mapping_label; legacy's label must be empty. No site values default.
    """
    def __init__(self, windows, config):
        keys = {'plan_path', 'binding_path', 'plan_sha256', 'roles', 'starter_path',
                'starter_sha256', 'saved_reboot_script_sha256'}
        _need(type(config) is dict and set(config) == keys and type(config['roles']) is dict
              and set(config['roles']) == set(ROLES))
        for field in ('plan_path', 'binding_path', 'starter_path'):
            _path(config[field])
        _need(PureWindowsPath(config['plan_path']).name == 'plan.json'
              and PureWindowsPath(config['binding_path']).parent == PureWindowsPath(config['plan_path']).parent
              and all(bridge._hash(config[k]) for k in
                      ('plan_sha256', 'starter_sha256', 'saved_reboot_script_sha256')))
        for role in ROLES:
            row = config['roles'][role]
            _need(type(row) is dict and set(row) == {'descriptor_path', 'descriptor_sha256',
                  'callback_sha256', 'mapping_label'} and bridge._hash(row['descriptor_sha256'])
                  and bridge._hash(row['callback_sha256']))
            _path(row['descriptor_path'])
            _need((role == 'legacy' and row['mapping_label'] == '') or
                  (role != 'legacy' and type(row['mapping_label']) is str
                   and re.fullmatch(r'[A-Za-z0-9_. :+-]{1,160}', row['mapping_label'])))
        # The audited installed starter uses the same canonical command builder.
        # A different reviewed version needs an explicit code change, never fallback.
        _need(_sha(Path(__file__).with_name('windows-ha-start-lease-observer.ps1').read_bytes())
              == config['starter_sha256'])
        self.windows = windows
        self.config = deepcopy(config)

    def _query(self, mode, writer, deadline, **values):
        bridge._remaining(deadline)
        cfg = {'mode': mode, 'writer': writer, **values}
        if mode == 'snapshot':
            source = Path(__file__).with_name('windows-saved-reboot.ps1').read_bytes()
            _need(_sha(source) == self.config['saved_reboot_script_sha256'])
            cfg.update(guard_base64=base64.b64encode(source).decode(),
                       guard_sha256=self.config['saved_reboot_script_sha256'])
        blob = base64.b64encode(gzip.compress(_encoded(cfg), mtime=0)).decode()
        script = NATIVE_SOURCE.replace('CONFIG_GZIP_BASE64', blob)
        _need(len(script.encode()) <= 131072)
        result = self.windows.powershell(script, deadline)
        _need(type(result) is dict and len(_encoded(result)) + 1 <= bridge.FRAME_BYTES)
        return result

    def _writer(self, value):
        keys = {'plan_path', 'plan_sha256', 'binding_path', 'binding_sha256', 'terminal_path',
                'terminal_sha256', 'previous_windows_boot_sha256', 'windows_boot_sha256', 'vm_id_sha256'}
        _need(type(value) is dict and set(value) == keys
              and value['plan_path'] == self.config['plan_path']
              and value['binding_path'] == self.config['binding_path']
              and value['plan_sha256'] == self.config['plan_sha256']
              and all(bridge._hash(value[k]) for k in keys if k.endswith('_sha256') and k != 'vm_id_sha256')
              and type(value['vm_id_sha256']) is dict and set(value['vm_id_sha256']) == {'A', 'B'}
              and all(bridge._hash(pin) for pin in value['vm_id_sha256'].values())
              and len(set(value['vm_id_sha256'].values())) == 2
              and value['terminal_path'] == str(PureWindowsPath(value['plan_path']).parent / 'journal/controller-state.json'))
        return deepcopy(value)

    def _snapshot(self, writer, deadline):
        value = self._query('snapshot', writer, deadline)
        _need(set(value) == {'snapshot', 'service_name', 'service_sid', 'operator_sid', 'image_path_sha256', 'source_endpoints'})
        snap = value['snapshot']
        _need(type(snap) is dict and set(snap) == SNAPSHOT_KEYS and snap['mode'] == 'Observe'
              and snap['windows_boot_sha256'] == writer['windows_boot_sha256']
              and snap['vm_id_sha256'] == writer['vm_id_sha256'] and snap['states'] == {'A': 'Running', 'B': 'Running'}
              and all(snap[key] == writer[key] for key in ('plan_sha256', 'binding_sha256', 'terminal_sha256'))
              and type(value['service_name']) is str and re.fullmatch(r'N100HA_[a-f0-9]{24}_controller', value['service_name'])
              and type(value['service_sid']) is str and re.fullmatch(r'S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}', value['service_sid'])
              and type(value['operator_sid']) is str and re.fullmatch(r'S-1-(?:[0-9]+-)+[0-9]+', value['operator_sid'])
              and bridge._hash(value['image_path_sha256'])
              and type(value['source_endpoints']) is dict and set(value['source_endpoints']) == set(ROLES))
        for role, endpoint in value['source_endpoints'].items():
            kind = 'lease' if role == 'legacy' else 'recovery'
            _need(type(endpoint) is dict and set(endpoint) == {'pipe_name', 'key_sha256'}
                  and type(endpoint['pipe_name']) is str and re.fullmatch('n100ha-' + kind + '-proof-[a-f0-9]{32}', endpoint['pipe_name'])
                  and bridge._hash(endpoint['key_sha256']))
        _need(all(len({row[field] for row in value['source_endpoints'].values()}) == 3
                  for field in ('pipe_name', 'key_sha256')))
        return value

    def _read(self, path, pin, maximum, writer, context, deadline):
        _path(path)
        _need(bridge._hash(pin) and maximum in (8192, 65536))
        data = bytearray()
        total = None
        try:
            while total is None or len(data) < total:
                offset = len(data)
                part = self._query('slice', writer, deadline, path=path, pin=pin, maximum=maximum,
                                   offset=offset, count=SLICE_BYTES, context=context)
                _need(set(part) == {'data', 'size', 'offset', 'sha256', 'fragment_sha256', 'windows_boot_sha256'}
                      and type(part['size']) is int and 0 < part['size'] <= maximum
                      and type(part['offset']) is int and part['offset'] == offset
                      and part['sha256'] == pin and part['windows_boot_sha256'] == writer['windows_boot_sha256']
                      and bridge._hash(part['fragment_sha256']) and type(part['data']) is str
                      and len(part['data']) <= 4 * ((SLICE_BYTES + 2) // 3)
                      and (total is None or total == part['size']))
                total = part['size']
                try:
                    fragment = base64.b64decode(part['data'], validate=True)
                except (ValueError, TypeError):
                    raise bridge.BridgeRejected(ERROR) from None
                _need(len(fragment) == min(SLICE_BYTES, total - offset)
                      and _sha(fragment) == part['fragment_sha256'])
                data.extend(fragment)
            _need(_sha(data) == pin)
            return bytes(data)
        finally:
            data[:] = b'\0' * len(data)

    def _collect(self, writer, deadline):
        writer = self._writer(writer)
        context = self._snapshot(writer, deadline)
        starter = self._read(self.config['starter_path'], self.config['starter_sha256'],
                             65536, writer, context, deadline)
        _need(starter == Path(__file__).with_name('windows-ha-start-lease-observer.ps1').read_bytes())
        specs = {}
        maps = {}
        for role in ROLES:
            row = self.config['roles'][role]
            raw = self._read(row['descriptor_path'], row['descriptor_sha256'], 8192, writer, context, deadline)
            descriptor = _decode(raw)
            _need(type(descriptor.get('arguments')) is dict)
            args = descriptor['arguments']
            _need(args.get('ServiceSid') == context['service_sid'] and args.get('OperatorSid') == context['operator_sid']
                  and args.get('WindowsBootSha256') == writer['previous_windows_boot_sha256']
                  and args.get('CallbackConfigSha256') == row['callback_sha256']
                  and args.get('PipeName') == context['source_endpoints'][role]['pipe_name']
                  and args.get('KeySha256') == context['source_endpoints'][role]['key_sha256'])
            callback_path = args.get('CallbackConfigPath')
            _need(_path(callback_path).parent == _path(row['descriptor_path']).parent)
            callback = self._read(callback_path, row['callback_sha256'], 8192, writer, context, deadline)
            doc = _decode(callback)
            if role != 'legacy':
                port = doc.get('guest_vsock_port')
                _need(type(port) is int and 0 < port < 1024 and doc.get('purpose') == role)
                maps[role] = {'id': f'{port:08x}-facb-11e6-bd58-64006a7986d3', 'label': row['mapping_label']}
            specs[role] = {key: row[key] for key in ('descriptor_path', 'descriptor_sha256', 'callback_sha256')}
            specs[role].update(descriptor_raw=raw, callback_raw=callback)
            if role != 'legacy':
                specs[role]['mapping_label'] = row['mapping_label']
        mapping = self._query('mappings', writer, deadline, mappings=maps, context=context)
        _need(set(mapping) == {'windows_boot_sha256', 'mappings'}
              and mapping['windows_boot_sha256'] == writer['windows_boot_sha256']
              and type(mapping['mappings']) is dict and set(mapping['mappings']) == set(maps)
              and all(type(value) is str and value in {'EXACT', 'ABSENT'} for value in mapping['mappings'].values()))
        final = self._snapshot(writer, deadline)
        _need(final == context)
        summary = {'plan_sha256': writer['plan_sha256'], 'windows_boot_sha256': writer['windows_boot_sha256'],
                   'protected_sources_verified': True, 'starter_sha256': self.config['starter_sha256'],
                   'mapping_status': mapping['mappings'], 'roles': {
                       role: {key: self.config['roles'][role][key] for key in ('descriptor_sha256', 'callback_sha256')}
                       for role in ROLES}}
        return specs, summary

    def preflight(self, writer_scope, deadline):
        """Prove real stopped-writer/source custody; ABSENT volatile maps are reported, never created."""
        _, summary = self._collect(writer_scope, deadline)
        return summary

    def __call__(self, operation, receipts, deadline):
        """Build the deterministic adapter from an issued rebind stage and real resumed proof."""
        from .sameboot_transition import _closed, _validate
        _need(type(operation) is dict and operation.get('stage') == 'rebind_observers'
              and bridge._hash(operation.get('nonce')) and type(operation.get('scope')) is dict
              and type(receipts) is dict)
        scope = operation['scope']
        _need(scope.get('saved_reboot') is True and scope.get('previous_plan_sha256') == self.config['plan_sha256'])
        _validate('resume_guests', receipts.get('resume_guests'), scope, receipts)
        resumed = receipts['resume_guests']
        _need(bridge._hash(scope.get('attempt_sha256')) and resumed['windows_boot_sha256'] != scope['previous_windows_boot_sha256']
              and _closed(resumed['closed_pair'], scope['previous_revision'] + 1, resumed['terminal_sha256']))
        writer = {'plan_path': self.config['plan_path'], 'plan_sha256': self.config['plan_sha256'],
                  'binding_path': self.config['binding_path'], 'binding_sha256': scope['previous_instance_sha256'],
                  'terminal_path': str(PureWindowsPath(self.config['plan_path']).parent / 'journal/controller-state.json'),
                  'terminal_sha256': resumed['terminal_sha256'],
                  'previous_windows_boot_sha256': scope['previous_windows_boot_sha256'],
                  'windows_boot_sha256': resumed['windows_boot_sha256'], 'vm_id_sha256': resumed['vm_id_sha256']}
        specs, _ = self._collect(writer, deadline)
        carryover = receipts.get('carryover')
        _need(type(carryover) is dict and bridge._hash(carryover.get('carryover_sha256')))
        _validate('carryover', carryover, scope, receipts)
        for role in ROLES:
            spec = specs[role]
            spec['nonce'] = observer_nonce(operation['nonce'], role, writer['windows_boot_sha256'],
                                            spec['descriptor_sha256'], spec['callback_sha256'])
            if role == 'held_admission':
                spec['callback_overrides'] = {'postboot_attempt_id': scope['attempt_sha256'],
                                             'postboot_carryover_receipt_sha256': carryover['carryover_sha256']}
            # Validate preparation against actual boot/source bytes; never synthesize
            # a future boot or a successful process/guest observation for compatibility.
            prepare_role(role, spec, writer['windows_boot_sha256'])
        return WindowsSavedObservers(self.windows, writer_scope=writer, roles=specs,
                                     starter_path=self.config['starter_path'],
                                     starter_sha256=self.config['starter_sha256'])


NATIVE_SOURCE = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$utf=[Text.UTF8Encoding]::new($false,$true)
$m=[IO.MemoryStream]::new([Convert]::FromBase64String('CONFIG_GZIP_BASE64'));$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);$r=[IO.StreamReader]::new($z,$utf)
try{$c=ConvertFrom-Json ($r.ReadToEnd())}finally{$r.Dispose()}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent();try{$operator=$identity.User.Value}finally{$identity.Dispose()}
$held=[Collections.Generic.List[IDisposable]]::new()
function Need([bool]$v){if(-not $v){throw 'saved_observer_site_scope'}}
function Pin([byte[]]$raw){$s=[Security.Cryptography.SHA256]::Create();try{[BitConverter]::ToString($s.ComputeHash($raw)).Replace('-','').ToLowerInvariant()}finally{$s.Dispose()}}
function Boot{$os=Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5;Pin ($utf.GetBytes($os.LastBootUpTime.ToUniversalTime().Ticks.ToString()))}
function Private([string]$Path,[string]$Reader=''){
 Need ($Path-cmatch '\AC:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}(?:\\[A-Za-z0-9_.-]+)*\z' -and $Path-cnotmatch '\\\.\.?(\\|$)')
 $leaf=Get-Item -LiteralPath $Path -Force;Need ($leaf-is [IO.FileInfo])
 for($node=$leaf;$null-ne $node;$node=if($node-is [IO.FileInfo]){$node.Directory}else{$node.Parent}){
  Need (-not ($node.Attributes-band [IO.FileAttributes]::ReparsePoint))
  $sd=[Security.AccessControl.RawSecurityDescriptor]::new((Get-Acl -LiteralPath $node.FullName).Sddl);$writers=@('S-1-5-18','S-1-5-32-544',$operator)
  $trusted=$node.FullName-ceq 'C:\' -and $sd.Owner.Value-ceq 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
  Need (($sd.Owner.Value-cin $writers -or $trusted) -and $null-ne $sd.DiscretionaryAcl -and $sd.DiscretionaryAcl.Count-gt 0)
  foreach($ace in $sd.DiscretionaryAcl){
   Need ($ace-is [Security.AccessControl.CommonAce] -and -not $ace.IsCallback)
   if($ace.AceQualifier-ne 'AccessAllowed' -or ([int]$ace.AceFlags-band 8)){continue}
   $mask=if($node-is [IO.DirectoryInfo]){269287488L}else{1343029590L}
   Need (($ace.AccessMask-band $mask)-eq 0 -or $ace.SecurityIdentifier.Value-cin $writers)
   if($Reader -and $node.FullName-ceq $leaf.FullName){Need ($ace.SecurityIdentifier.Value-cin ($writers+@($Reader)))}
  }
 }
}
function ReadPin([string]$Path,[string]$Expected,[int]$Maximum,[string]$Reader=''){
 Private $Path $Reader
 $f=[IO.File]::Open($Path,'Open','Read','Read');$held.Add($f);Need ($f.Length-gt 0 -and $f.Length-le $Maximum)
 $raw=[byte[]]::new([int]$f.Length);$at=0
 while($at-lt $raw.Length){$n=$f.Read($raw,$at,$raw.Length-$at);Need ($n-gt 0);$at+=$n}
 Need ((Pin $raw)-ceq $Expected);return ,$raw
}
function Context {
 Need ((Boot)-ceq $c.writer.windows_boot_sha256 -and $operator-ceq $c.context.operator_sid)
 $v=@(Get-CimInstance Win32_Service -Filter ("Name='"+$c.context.service_name+"'") -OperationTimeoutSec 5)
 Need ($v.Count-eq 1 -and $v[0].Name-ceq $c.context.service_name -and $v[0].State-ceq 'Stopped' -and $v[0].StartMode-ceq 'Disabled' -and $v[0].ProcessId-eq 0 -and $v[0].StartName-ieq ('NT SERVICE\'+$c.context.service_name) -and $v[0].ServiceType-ceq 'Own Process' -and -not $v[0].DesktopInteract -and (Pin ($utf.GetBytes($v[0].PathName)))-ceq $c.context.image_path_sha256)
}
try{
 Need ($PSVersionTable.PSEdition-ceq 'Desktop' -and [Environment]::Is64BitProcess)
 if($c.mode-ceq 'snapshot'){
  $g=[Convert]::FromBase64String($c.guard_base64);Need ((Pin $g)-ceq $c.guard_sha256);$w=$c.writer
  $rows=@(& ([scriptblock]::Create($utf.GetString($g))) -Mode Observe -VmAIdSha256 $w.vm_id_sha256.A -VmBIdSha256 $w.vm_id_sha256.B -WindowsBootSha256 $w.previous_windows_boot_sha256 -PlanPath $w.plan_path -PlanSha256 $w.plan_sha256 -BindingPath $w.binding_path -BindingSha256 $w.binding_sha256 -TerminalPath $w.terminal_path -TerminalSha256 $w.terminal_sha256)
  Need ($rows.Count-eq 1);$v=ConvertFrom-Json $rows[0]
  $plan=ConvertFrom-Json ($utf.GetString((ReadPin $w.plan_path $w.plan_sha256 1048576)))
  $name=$plan.windows_enforced.service_name;$sid=$plan.windows_enforced.service_sid
  Private $w.plan_path $sid
  Need ($name-cmatch '\AN100HA_[a-f0-9]{24}_controller\z' -and $sid-cmatch '\AS-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\z' -and (Boot)-ceq $w.windows_boot_sha256)
  $endpoints=[ordered]@{}
  foreach($role in @('legacy','held_admission','provider_isolation')){
   $old=if($role-ceq 'legacy'){$plan.windows_initial_observer}else{$plan.windows_recovery_observers.$role}
   $endpoints[$role]=@{pipe_name=$old.pipe_name;key_sha256=$old.key_sha256}
  }
  @{snapshot=$v;service_name=$name;service_sid=$sid;operator_sid=$operator;image_path_sha256=$plan.initial_service_start.service_image_path_sha256;source_endpoints=$endpoints}|ConvertTo-Json -Depth 5 -Compress
 }elseif($c.mode-ceq 'slice'){
  Context;Need ($c.count-eq 2200 -and $c.maximum-cin @(8192,65536) -and $c.offset-ge 0)
  $raw=ReadPin $c.path $c.pin $c.maximum $c.context.service_sid;Need ($c.offset-lt $raw.Length)
  $count=[Math]::Min($c.count,$raw.Length-$c.offset);$part=[byte[]]::new($count);[Array]::Copy($raw,$c.offset,$part,0,$count)
  Context
  @{data=[Convert]::ToBase64String($part);size=$raw.Length;offset=$c.offset;sha256=$c.pin;fragment_sha256=(Pin $part);windows_boot_sha256=$c.writer.windows_boot_sha256}|ConvertTo-Json -Compress
 }elseif($c.mode-ceq 'mappings'){
  Context;$facts=[ordered]@{}
  $root=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion\Virtualization\GuestCommunicationServices',$false);Need ($null-ne $root)
  try{foreach($role in $c.mappings.PSObject.Properties.Name){
   $expected=$c.mappings.$role;$key=$root.OpenSubKey($expected.id,$false)
   if($null-eq $key){$facts[$role]='ABSENT'}else{
    try{Need ($key.SubKeyCount-eq 0 -and $key.ValueCount-eq 1 -and @($key.GetValueNames())[0]-ceq 'ElementName' -and $key.GetValueKind('ElementName')-eq [Microsoft.Win32.RegistryValueKind]::String -and $key.GetValue('ElementName')-ceq $expected.label);$facts[$role]='EXACT'}finally{$key.Dispose()}
   }
  }}finally{$root.Dispose()}
  Context;@{windows_boot_sha256=$c.writer.windows_boot_sha256;mappings=$facts}|ConvertTo-Json -Depth 3 -Compress
 }else{throw 'saved_observer_site_mode'}
}catch{exit 1}finally{foreach($h in $held){$h.Dispose()}}
'''
