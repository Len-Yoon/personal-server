"""N100 custody adapter for the first Windows write lease and A activation."""

import base64
from contextlib import contextmanager
import re
import secrets
import time

from . import native_stdio_bridge as bridge
from .native_windows_ssh import fixed_script


START_PARAMETERS = {'ServiceName', 'ServiceHostSha256', 'PlanPath', 'PlanSha256', 'ApprovalPath', 'ApprovalSha256',
                    'BindingPath', 'PreflightPath', 'PreflightSha256', 'ScmVerifierPath'}

WSL_READER = '''import hashlib, json, os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
runtime = len(sys.argv) == 4
limit = 1048576 if runtime else 4096
owner = os.geteuid()
for parent in path.parents:
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner) or info.st_mode & 0o022:
        raise SystemExit(1)
with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_nlink != 1 or info.st_mode & 0o077:
        raise SystemExit(1)
    raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit or len(raw) != info.st_size:
        raise SystemExit(1)
if runtime:
    record = json.loads(raw)
    if (record.get('schema_version'), record.get('plan_sha256'), record.get('attempt_id')) != (1, *sys.argv[2:]):
        raise SystemExit(1)
    print(json.dumps({'runtime_sha256': hashlib.sha256(raw).hexdigest()}))
else:
    sys.stdout.buffer.write(raw)
'''


class WindowsActivationCustody:
    def __init__(self, windows, handoff_io, binding, installation_id, journal_path, runtime_path, stage_parameters,
                 stage_sha256, start_sha256, live_sha256):
        bridge._need(bridge._hash(installation_id) and type(stage_parameters) is dict
                     and set(stage_parameters) == {'InputPath', 'InputSha256'}
                     and bridge._hash(stage_parameters['InputSha256']) and bridge._hash(stage_sha256)
                     and bridge._hash(start_sha256)
                     and bridge._hash(live_sha256), 'native_activation_custody_input_invalid')
        bridge._need(all(type(path) is str and re.fullmatch(r'/home/[A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.json', path)
                         and all(part not in {'', '.', '..'} for part in path[1:].split('/'))
                         for path in (journal_path, runtime_path))
                     and runtime_path.endswith(f'/{binding.attempt_id}/runtime.json'),
                     'native_activation_custody_input_invalid')
        self.windows = windows
        self.handoff_io = handoff_io
        self.binding = binding
        self.installation_id = installation_id
        self.journal_path = journal_path
        self.runtime_path = runtime_path
        self.stage_parameters = dict(stage_parameters)
        self.stage_sha256 = stage_sha256
        self.start_sha256 = start_sha256
        self.live_sha256 = live_sha256
        self.start_attempted = False

    def _bound(self, request):
        bridge._need(request.binding == self.binding and request.node == 'A' and request.verb == 'activate',
                     'native_activation_custody_unbound')

    def read_guest_check(self, request, deadline):
        self._bound(request)
        return self.handoff_io.read_guest_check(request, deadline)

    def publish_guest_handoff(self, request, payload, deadline):
        self._bound(request)
        self.handoff_io.publish_guest_handoff(request, payload, deadline)

    def read_wsl_intent(self, request, deadline):
        self._bound(request)
        return self._read_wsl(self.journal_path, deadline)

    def _read_wsl(self, path, deadline, *pins):
        encoded = base64.b64encode(WSL_READER.encode('ascii')).decode('ascii')
        python = f"import base64;exec(base64.b64decode('{encoded}'))"
        source = (
            "$ErrorActionPreference='Stop';$w=Join-Path ([Environment]::SystemDirectory) 'wsl.exe';"
            "$r=@(& $w --list --running --quiet|ForEach-Object{($_ -replace ([char]0),'').Trim()});"
            "if($LASTEXITCODE -ne 0 -or 'Ubuntu-24.04' -cnotin $r){exit 1};"
            f'& $w --distribution Ubuntu-24.04 --exec /usr/bin/python3 -I -B -S -c "{python}" '
            + ' '.join(f"'{arg}'" for arg in (path, *pins)) + ';exit $LASTEXITCODE'
        )
        return self.windows.powershell(source, deadline)

    def _stage(self, parameters, deadline):
        source = fixed_script('windows-ha-stage-initial-activation.ps1', self.stage_sha256,
                              self.stage_parameters | {'ExpectedSourceManifestSha256': self.binding.source_sha256}
                              | parameters)
        return self._result(source, deadline, 'initial_stage_')

    def _result(self, source, deadline, failure_prefix):
        result = self.windows.powershell(source, deadline)
        if set(result) == {'blocked'} and type(result['blocked']) is str and re.fullmatch(
                failure_prefix + r'[a-z0-9_]{1,80}', result['blocked']):
            raise bridge.BridgeRejected(result['blocked'])
        return result

    def preflight(self, deadline):
        bridge._need(bridge._matches(self._stage({'Preflight': True}, deadline), {'activation_stage_preflight': True}),
                     'native_activation_stage_preflight_unconfirmed')

    @contextmanager
    def open_first_write(self, request, intent, deadline):
        self._bound(request)
        bridge._need(not self.start_attempted, 'native_activation_service_start_uncertain')
        runtime = self._read_wsl(self.runtime_path, deadline, self.binding.plan_sha256, self.binding.attempt_id)
        bridge._need(set(runtime) == {'runtime_sha256'} and bridge._hash(runtime['runtime_sha256']),
                     'native_activation_runtime_unconfirmed')
        self.start_attempted = True
        parameters = self._stage({'IntentBase64': base64.b64encode(intent).decode('ascii'),
                                  'RuntimeSha256': runtime['runtime_sha256'], 'Go': True}, deadline)
        bridge._need(set(parameters) == START_PARAMETERS and
                     all(type(value) is str for value in parameters.values()), 'native_activation_stage_unconfirmed')
        source = fixed_script('windows-ha-start-initial-service.ps1', self.start_sha256,
                              parameters | {'ExpectedActivationIntentSha256': bridge._digest(intent), 'Go': True})
        result = self._result(source, deadline, 'initial_service_')
        keys = {'first_write_proof_sha256', 'service_instance_sha256'}
        bridge._need(type(result) is dict and set(result) == keys and all(map(bridge._hash, result.values())),
                     'native_activation_service_start_uncertain')
        yield WindowsActivationLease(self, parameters['BindingPath'], result)


class WindowsActivationLease:
    def __init__(self, custody, binding_path, result):
        self.custody = custody
        self.binding_path = binding_path
        self.first_write_proof_sha256 = result['first_write_proof_sha256']
        self.service_instance_sha256 = result['service_instance_sha256']

    def assert_current(self, deadline):
        custody = self.custody
        binding = custody.binding
        source = fixed_script('windows-ha-cutover-live-authority.ps1', custody.live_sha256, {
            'BindingPath': self.binding_path, 'LegacyPlanSha256': binding.plan_sha256,
            'AttemptId': binding.attempt_id, 'PriorSha256': secrets.token_hex(32),
            'ExpectedServiceInstanceSha256': self.service_instance_sha256,
        })
        result = custody.windows.powershell(source, min(deadline, time.monotonic() + bridge.ACTIVATION_BOUNDARY_SECONDS))
        keys = {'windows_generation', 'windows_generation_sha256', 'live_lease_sha256'}
        bridge._need(type(result) is dict and set(result) == keys and type(result['windows_generation']) is int
                     and result['windows_generation'] == 1
                     and all(bridge._hash(result[key]) for key in keys - {'windows_generation'}),
                     'native_activation_service_proof_unconfirmed')
        return bridge._digest(bridge._bytes(result))
