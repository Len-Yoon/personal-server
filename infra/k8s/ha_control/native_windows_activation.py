"""N100 custody adapter for the first Windows write lease and A activation."""

import base64
import json
from contextlib import contextmanager
from datetime import datetime, timezone
import re
import secrets
from subprocess import TimeoutExpired
import time

from . import native_stdio_bridge as bridge
from .native_windows_ssh import fixed_script
from .provision_enforced import closure_service_args


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
        self.retirement_attempted = False

    @contextmanager
    def guest_authority(self, deadline):
        startup_deadline = min(deadline, time.monotonic() + 12)
        with self.custody.windows.live_authority_session(startup_deadline) as powershell:
            def check(boundary_deadline):
                return self._assert_current(powershell, boundary_deadline)

            yield check

    def assert_current(self, deadline, *, guest_boundary=False):
        bridge._need(not guest_boundary, 'native_windows_authority_session_required')
        return self._assert_current(self.custody.windows.powershell, min(deadline, time.monotonic() + 4))

    def _assert_current(self, powershell, deadline):
        custody = self.custody
        binding = custody.binding
        source = fixed_script('windows-ha-cutover-live-authority.ps1', custody.live_sha256, {
            'BindingPath': self.binding_path, 'LegacyPlanSha256': binding.plan_sha256,
            'AttemptId': binding.attempt_id, 'PriorSha256': secrets.token_hex(32),
            'ExpectedServiceInstanceSha256': self.service_instance_sha256,
        })
        result = powershell(source, deadline)
        keys = {'windows_generation', 'windows_generation_sha256', 'live_lease_sha256'}
        bridge._need(type(result) is dict and set(result) == keys and type(result['windows_generation']) is int
                     and result['windows_generation'] == 1
                     and all(bridge._hash(result[key]) for key in keys - {'windows_generation'}),
                     'native_activation_service_proof_unconfirmed')
        return bridge._digest(bridge._bytes(result))

    def retire_service(self, deadline, *, assert_guard):
        """Disable and stop the original SCM service instance under the cutover guard."""
        bridge._need(not self.retirement_attempted, 'initial_service_retirement_reconciliation_required')
        assert_guard()
        self.retirement_attempted = True
        result = self._service_retirement('Retire', deadline)
        assert_guard()
        return result

    def observe_retired(self, deadline, *, assert_guard):
        assert_guard()
        result = self._service_retirement('ObserveRetired', min(deadline, time.monotonic() + 4))
        assert_guard()
        return result

    def resume_retirement(self, deadline, *, assert_guard, assert_settled):
        """Finish disabling a still-running original service after a settled attempt."""
        assert_guard()
        assert_settled()
        assert_guard()
        result = self._service_retirement('ResumeRetire', deadline)
        assert_guard()
        return result

    def read_prior_service_args(self, deadline, *, assert_guard):
        """Read the prior stopped service's args digest under the cutover guard."""
        assert_guard()
        custody = self.custody
        source = fixed_script("windows-ha-cutover-live-authority.ps1", custody.live_sha256, {
            "BindingPath": self.binding_path, "LegacyPlanSha256": custody.binding.plan_sha256,
            "AttemptId": custody.binding.attempt_id, "PriorSha256": secrets.token_hex(32),
            "ExpectedServiceInstanceSha256": self.service_instance_sha256, "ReadPriorServiceArgs": True,
        })
        bridge._remaining(deadline)
        result = custody.windows.powershell(source, deadline)
        pin = result.get("prior_args_sha256") if type(result) is dict else None
        bridge._need(bridge._hash(pin) and bridge._matches(result, {
            "prior_args_sha256": pin, "service_instance_sha256": self.service_instance_sha256,
        }), "native_initial_prior_args_unconfirmed")
        assert_guard()
        return pin

    def _service_retirement(self, operation, deadline):
        custody = self.custody
        binding = custody.binding
        source = fixed_script('windows-ha-cutover-live-authority.ps1', custody.live_sha256, {
            'BindingPath': self.binding_path, 'LegacyPlanSha256': binding.plan_sha256,
            'AttemptId': binding.attempt_id, 'PriorSha256': secrets.token_hex(32),
            'ExpectedServiceInstanceSha256': self.service_instance_sha256, operation: True,
        })
        result = custody.windows.powershell(source, deadline)
        bridge._need(type(result) is dict and set(result) == {'initial_service_retired', 'retirement_sha256'}
                     and result['initial_service_retired'] is True and bridge._hash(result['retirement_sha256']),
                     'initial_service_retirement_unconfirmed')
        return result['retirement_sha256']


def publish_transition_observer_config(windows, root, stage_sha256, binding, manifest_sha256, deadline, *,
                                       observe_only=False):
    """Publish the exact callback config through the existing protected Windows stage."""
    raw = binding["callback_config"]
    pin = binding["callback_config_sha256"]
    runtime = binding["runtime_sha256"]
    bridge._need(type(raw) is bytes and 0 < len(raw) <= 4096 and bridge._digest(raw) == pin
                 and all(bridge._hash(value) for value in (stage_sha256, runtime, manifest_sha256))
                 and type(root) is str and re.fullmatch(
                     r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root),
                 "native_transition_observer_config_invalid")
    path = root + rf"\transition-observer-config-{pin}.json"
    parameters = {"InputPath": path, "InputSha256": pin, "ExpectedSourceManifestSha256": manifest_sha256,
                  "RuntimeSha256": runtime, "Go": True}
    script = "windows-ha-stage-initial-activation.ps1"
    if not observe_only:
        source = fixed_script(script, stage_sha256, parameters | {
            "IntentBase64": base64.b64encode(raw).decode("ascii"), "PublishTransitionObserverConfig": True,
        })
        try:
            result = windows.powershell(source, deadline)
        except (bridge.BridgeRejected, TimeoutExpired, OSError):
            pass  # The remote write may have completed. Only observe it.
        else:
            bridge._need(result == {"transition_observer_config_sha256": pin},
                         "native_transition_observer_config_unconfirmed")
            return path

    bridge._remaining(deadline)
    source = fixed_script(script, stage_sha256, parameters | {"ObserveTransitionObserverConfig": True})
    result = windows.powershell(source, deadline)
    bridge._need(result == {"observed_transition_observer_config_sha256": pin},
                 "native_transition_observer_config_unconfirmed")
    return path


def _validate_transition_observer_launch(observer, binding):
    pin_fields = {"launcher_sha256", "server_sha256", "callback_sha256", "windows_boot_sha256",
                  "wsl_boot_sha256", "collector_sha256", "reactivation_plan_sha256", "marker_sha256"}
    bridge._need(type(observer) is dict and set(observer) == pin_fields | {"service_name"}
                 and all(bridge._hash(observer[key]) for key in pin_fields)
                 and type(observer["service_name"]) is str
                 and re.fullmatch(r"N100HA_[a-f0-9]{24}_controller", observer["service_name"])
                 and all(bridge._hash(binding.get(key)) for key in ("plan", "attempt")),
                 "native_transition_observer_launch_invalid")


def publish_transition_observer_launch(windows, root, stage_sha256, binding, manifest_sha256, observer, deadline):
    """Publish a launch descriptor derived from the exact active exclusion binding."""
    _validate_transition_observer_launch(observer, binding)
    publish_transition_observer_config(windows, root, stage_sha256, binding, manifest_sha256, deadline, observe_only=True)
    pin = binding["callback_config_sha256"]
    raw = bridge._bytes(observer | {"callback_config_sha256": pin, "legacy_plan_sha256": binding["plan"],
                                   "legacy_attempt_id": binding["attempt"]})
    digest = bridge._digest(raw)
    source = fixed_script("windows-ha-stage-initial-activation.ps1", stage_sha256, {
        "InputPath": root + rf"\transition-observer-launch-{pin}.json", "InputSha256": digest,
        "ExpectedSourceManifestSha256": manifest_sha256, "RuntimeSha256": binding["runtime_sha256"],
        "IntentBase64": base64.b64encode(raw).decode("ascii"), "PublishTransitionObserverLaunch": True, "Go": True,
    })
    result = windows.powershell(source, deadline)
    bridge._need(result == {"transition_observer_launch_sha256": digest},
                 "native_transition_observer_launch_unconfirmed")
    return digest


def start_transition_observer(windows, root, stage_sha256, binding, manifest_sha256, launch_sha256, deadline):
    """Launch one prepared Windows observer after verifying its published config."""
    bridge._need(bridge._hash(launch_sha256), "native_transition_observer_launch_invalid")
    path = publish_transition_observer_config(
        windows, root, stage_sha256, binding, manifest_sha256, deadline, observe_only=True,
    )
    pin = binding["callback_config_sha256"]
    source = fixed_script("windows-ha-stage-initial-activation.ps1", stage_sha256, {
        "InputPath": root + rf"\transition-observer-launch-{pin}.json", "InputSha256": launch_sha256,
        "ExpectedSourceManifestSha256": manifest_sha256, "RuntimeSha256": binding["runtime_sha256"],
        "StartTransitionObserver": True, "Go": True,
    })
    result = windows.powershell(source, deadline)
    fields = {"key_sha256", "windows_boot_sha256", "wsl_boot_sha256", "collector_sha256"}
    bridge._need(type(result) is dict and set(result) == fields | {"callback_config_sha256", "observer_pid", "pipe_name"}
                 and result["callback_config_sha256"] == pin and all(bridge._hash(result[key]) for key in fields)
                 and type(result["observer_pid"]) is int and 0 < result["observer_pid"] <= 2147483647
                 and type(result["pipe_name"]) is str and re.fullmatch(r"n100ha-lease-proof-[a-f0-9]{32}", result["pipe_name"]),
                 "native_transition_observer_start_unconfirmed")
    return result | {"callback_config_path": path, "key_path": root + rf"\transition-observer-key-{pin}.key"}


def _transition_observer_plan_base(plan_raw, plan_sha256, root, binding, observer, client_sha256):
    error = "native_transition_observer_plan_invalid"
    bridge._need(type(plan_raw) is bytes and 0 < len(plan_raw) <= 1048576
                 and bridge._digest(plan_raw) == plan_sha256, error)
    try:
        plan = json.loads(plan_raw, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(error) from None
    bridge._need(type(plan) is dict and plan.get("profile") == "enforced-vm-v1"
                 and type(plan.get("generation")) is int and plan["generation"] >= 2
                 and not {"initial_wsl_cutover", "windows_transition_observer", "_approval_action",
                          "_installed_runtime"}.intersection(plan)
                 and type(plan.get("windows_enforced")) is dict
                 and type(observer) is dict and type(binding) is dict
                 and type(root) is str
                 and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root), error)
    callback_pin = binding.get("callback_config_sha256")
    sid = plan["windows_enforced"].get("service_sid")
    service = plan["windows_enforced"].get("service_name")
    bridge._need(type(service) is str and re.fullmatch(r"N100HA_[a-f0-9]{24}_controller", service)
                 and service == observer.get("service_name")
                 and type(sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}", sid)
                 and all(bridge._hash(value) for value in (
                     callback_pin, binding.get("plan"), binding.get("attempt"), client_sha256,
                 ))
                 and type(binding.get("callback_config")) is bytes
                 and bridge._digest(binding["callback_config"]) == callback_pin, error)
    return plan, callback_pin


def bind_transition_observer_plan(plan_raw, plan_sha256, *, root, binding, observer, started, client_sha256):
    """Prepare exact plan bytes for approval, binding one Windows observer receipt."""
    plan, callback_pin = _transition_observer_plan_base(plan_raw, plan_sha256, root, binding, observer, client_sha256)
    error = "native_transition_observer_plan_invalid"
    code_pins = ("windows_boot_sha256", "wsl_boot_sha256", "collector_sha256")
    bridge._need(type(started) is dict and bridge._hash(started.get("key_sha256"))
                 and started.get("callback_config_sha256") == callback_pin
                 and started.get("callback_config_path") == root + rf"\transition-observer-config-{callback_pin}.json"
                 and started.get("key_path") == root + rf"\transition-observer-key-{callback_pin}.key"
                 and all(bridge._hash(observer.get(field)) and started.get(field) == observer[field] for field in code_pins)
                 and type(started.get("observer_pid")) is int and 0 < started["observer_pid"] <= 2147483647
                 and type(started.get("pipe_name")) is str
                 and re.fullmatch(r"n100ha-lease-proof-[a-f0-9]{32}", started["pipe_name"]), error)
    plan["windows_transition_observer"] = {field: started[field] for field in code_pins} | {
        "pipe_name": started["pipe_name"], "server_pid": started["observer_pid"], "key_sha256": started["key_sha256"],
        "client_sha256": client_sha256, "legacy_plan_sha256": binding["plan"], "legacy_attempt_id": binding["attempt"],
        "callback_config_sha256": callback_pin,
    }
    raw = bridge._bytes(plan)
    return {"plan": raw, "plan_sha256": bridge._digest(raw)}


def prepare_transition_observer_plan(windows, *, plan_raw, plan_sha256, root, binding, observer,
                                     manifest_sha256, stage_sha256, client_sha256, deadline):
    """Run observer preparation in order and return the plan that still needs approval."""
    _transition_observer_plan_base(plan_raw, plan_sha256, root, binding, observer, client_sha256)
    _validate_transition_observer_launch(observer, binding)
    publish_transition_observer_config(windows, root, stage_sha256, binding, manifest_sha256, deadline)
    launch_pin = publish_transition_observer_launch(windows, root, stage_sha256, binding, manifest_sha256,
                                                    observer, deadline)
    started = start_transition_observer(windows, root, stage_sha256, binding, manifest_sha256, launch_pin, deadline)
    return bind_transition_observer_plan(plan_raw, plan_sha256, root=root, binding=binding, observer=observer,
                                         started=started, client_sha256=client_sha256)


class WindowsTransitionServiceStart:
    """Start the prepared controller once. SCM startup is not a write-lease proof."""

    def __init__(self, windows, start_sha256):
        bridge._need(bridge._hash(start_sha256), "native_transition_start_scope_invalid")
        self.windows = windows
        self.start_sha256 = start_sha256
        self.attempted = False

    def start(self, controller, parameters, deadline):
        return self._invoke(controller, parameters, deadline, observe=False)

    def observe(self, controller, parameters, deadline):
        """Check an existing start intent and running SCM instance without another start."""
        return self._invoke(controller, parameters, deadline, observe=True)

    def stop(self, controller, parameters, started, deadline):
        """Settle only the instance proven by the start or read-only receipt."""
        bridge._need(type(started) is dict and started.get("controller_plan_sha256") == controller["plan_sha256"]
                     and type(started.get("service_pid")) is int and 0 < started["service_pid"] <= 2147483647
                     and type(started.get("service_created_ticks")) is str
                     and re.fullmatch(r"[1-9][0-9]{1,19}", started["service_created_ticks"]),
                     "native_transition_stop_scope_invalid")
        return self._invoke(controller, parameters, deadline, observe=False, stop=started)

    def check_lease_closed(self, controller, parameters, deadline):
        return self._invoke(controller, parameters, deadline, observe=False, closure="CheckLeaseClosed")

    def observe_closure_check(self, controller, parameters, deadline):
        return self._invoke(controller, parameters, deadline, observe=False, closure="ObserveClosureCheck")

    def restore_closure_args(self, controller, parameters, deadline):
        """Reconcile an uncertain invocation without replaying SCM start."""
        return self._invoke(controller, parameters, deadline, observe=False, closure="RestoreClosureArgs")

    def verify_prepared(self, controller, parameters, deadline):
        return self._invoke(controller, parameters, deadline, observe=False, verify_prepared=True)

    def publish_args(self, controller, parameters, previous_args_sha256, deadline):
        bridge._need(bridge._hash(previous_args_sha256), "native_transition_prepared_service_unconfirmed")
        return self._invoke(controller, parameters, deadline, observe=False, verify_prepared=True,
                            previous_args_sha256=previous_args_sha256)

    def run_rejoin(self, controller, parameters, mode, request_id, *, live_sha256, root, deadline):
        bridge._need(type(root) is str and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root)
                     and type(parameters) is dict and parameters.get("PlanPath") == root + r"\plan.json",
                     "native_transition_rejoin_receipt_unconfirmed")
        fixed_script("windows-ha-cutover-live-authority.ps1", live_sha256, {})
        launch = self._invoke(controller, parameters, deadline, observe=False, rejoin=(mode, request_id, None))
        return read_transition_rejoin_receipt(
            self.windows, live_sha256, root, controller["plan_sha256"], mode, host_pid=launch["host_pid"],
            started_ms=launch["started_at_ms"], service_sid=launch["service_sid"], deadline=deadline,
        )

    def restore_rejoin_args(self, controller, parameters, mode, request_id, deadline):
        return self._invoke(controller, parameters, deadline, observe=False, rejoin=(mode, request_id, "RestoreRejoinArgs"))

    def rejoin_stage_intent_present(self, controller, parameters, request_id, deadline):
        result = self._invoke(controller, parameters, deadline, observe=False,
                              rejoin=("stage-rejoin", request_id, "InspectRejoinStage"))
        return result["stage_intent_present"]

    def _invoke(self, controller, parameters, deadline, *, observe, stop=None, closure=None, rejoin=None, verify_prepared=False, previous_args_sha256=None):
        fields = START_PARAMETERS - {"BindingPath"}
        invalid = "native_transition_start_scope_invalid"
        bridge._need(type(controller) is dict and set(controller) == {"plan", "plan_sha256"}
                     and type(controller["plan"]) is bytes and 0 < len(controller["plan"]) <= 1048576
                     and bridge._digest(controller["plan"]) == controller["plan_sha256"]
                     and type(parameters) is dict and set(parameters) == fields
                     and parameters["PlanSha256"] == controller["plan_sha256"]
                     and all(bridge._hash(parameters[key]) for key in fields if key.endswith("Sha256")), invalid)
        try:
            plan = json.loads(controller["plan"], object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise bridge.BridgeRejected(invalid) from None
        bridge._need(type(plan) is dict and type(plan.get("windows_enforced")) is dict
                     and type(plan.get("windows_transition_observer")) is dict
                     and type(plan.get("transition_service_start")) is dict
                     and bridge._hash(plan["transition_service_start"].get("service_image_path_sha256"))
                     and plan["windows_enforced"].get("service_name") == parameters["ServiceName"], invalid)
        if verify_prepared:
            options = {"VerifyPreparedTransition": True} if previous_args_sha256 is None else {
                "PublishTransitionArgs": True, "PreviousArgsSha256": previous_args_sha256,
            }
            source = fixed_script("windows-ha-start-initial-service.ps1", self.start_sha256, parameters | options | {"Go": True})
            bridge._remaining(deadline)
            result = self.windows.powershell(source, deadline)
            args_pin = result.get("serving_args_sha256") if type(result) is dict else None
            sid = plan["windows_enforced"].get("service_sid")
            bridge._need(type(sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}", sid)
                         and bridge._hash(args_pin) and bridge._matches(result, {
                             "transition_service_prepared": True, "controller_plan_sha256": controller["plan_sha256"],
                             "service_sid": sid, "serving_args_sha256": args_pin,
                         }), "native_transition_prepared_service_unconfirmed")
            return result
        if rejoin is not None:
            mode, request_id, action = rejoin
            bridge._need(plan["transition_service_start"].get("lease_closure_check") is True
                         and mode in ("initialize", "stage-rejoin", "observe-rejoin-stage", "check-rejoin")
                         and type(request_id) is str and re.fullmatch(r"[a-f0-9]{32}", request_id), invalid)
            options = {"RejoinMode": mode, "RejoinRequestId": request_id, "Go": True}
            if action is not None:
                options[action] = True
            source = fixed_script("windows-ha-start-initial-service.ps1", self.start_sha256, parameters | options)
            bridge._remaining(deadline)
            result = self.windows.powershell(source, deadline)
            if action == "InspectRejoinStage":
                bridge._need(type(result) is dict and set(result) == {"stage_intent_present", "controller_plan_sha256"}
                             and type(result["stage_intent_present"]) is bool
                             and result["controller_plan_sha256"] == controller["plan_sha256"],
                             "native_transition_rejoin_intent_unconfirmed")
            elif action == "RestoreRejoinArgs":
                bridge._need(bridge._matches(result, {
                    "serving_args_restored": True, "controller_plan_sha256": controller["plan_sha256"],
                }), "native_transition_rejoin_restore_unconfirmed")
            else:
                sid = plan["windows_enforced"].get("service_sid")
                bridge._need(type(sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}", sid)
                             and type(result) is dict and set(result) == {
                                 "rejoin_command_completed", "mode", "controller_plan_sha256", "service_sid",
                                 "host_pid", "started_at_ms",
                             } and result["rejoin_command_completed"] is True and result["mode"] == mode
                             and result["controller_plan_sha256"] == controller["plan_sha256"]
                             and result["service_sid"] == sid
                             and type(result["host_pid"]) is int and 0 < result["host_pid"] <= 2147483647
                             and type(result["started_at_ms"]) is int and 0 < result["started_at_ms"] < 10**18,
                             "native_transition_rejoin_run_unconfirmed")
            return result
        if closure is not None:
            bridge._need(plan["transition_service_start"].get("lease_closure_check") is True, invalid)
            source = fixed_script("windows-ha-start-initial-service.ps1", self.start_sha256, parameters | {
                closure: True, "Go": True,
            })
            bridge._remaining(deadline)
            result = self.windows.powershell(source, deadline)
            if closure == "RestoreClosureArgs":
                bridge._need(bridge._matches(result, {
                    "serving_args_restored": True, "controller_plan_sha256": controller["plan_sha256"],
                }), "native_transition_closure_restore_unconfirmed")
            else:
                sid = plan["windows_enforced"].get("service_sid")
                bridge._need(type(result) is dict and set(result) == {
                    "closure_check_completed", "controller_plan_sha256", "service_sid", "host_pid", "started_at_ms",
                } and result["closure_check_completed"] is True
                    and result["controller_plan_sha256"] == controller["plan_sha256"]
                    and type(sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]+-){4}[0-9]+", sid)
                    and result["service_sid"] == sid
                    and type(result["host_pid"]) is int and 0 < result["host_pid"] <= 2147483647
                    and type(result["started_at_ms"]) is int and result["started_at_ms"] > 0,
                    "native_transition_closure_check_unconfirmed")
            return result
        if stop is not None:
            source = fixed_script("windows-ha-start-initial-service.ps1", self.start_sha256, parameters | {
                "StopTransition": True, "ExpectedServicePid": str(stop["service_pid"]),
                "ExpectedServiceCreatedTicks": stop["service_created_ticks"], "Go": True,
            })
            bridge._remaining(deadline)
            result = self.windows.powershell(source, deadline)
            bridge._need(bridge._matches(result, {
                "transition_service_stopped": True, "controller_plan_sha256": controller["plan_sha256"],
            }), "native_transition_service_stop_unconfirmed")
            return result
        source = fixed_script("windows-ha-start-initial-service.ps1", self.start_sha256, parameters | {
            "ObserveTransition" if observe else "StartTransition": True, "Go": True,
        })
        bridge._remaining(deadline)
        if not observe:
            bridge._need(not self.attempted, "native_transition_start_reconciliation_required")
            self.attempted = True
        try:
            result = self.windows.powershell(source, deadline)
        except (bridge.BridgeRejected, TimeoutExpired, OSError):
            if observe:
                raise
            return self.observe(controller, parameters, deadline)
        receipt = "transition_service_observed" if observe else "transition_service_started"
        bridge._need(type(result) is dict and set(result) == {
            receipt, "service_pid", "controller_plan_sha256", "service_created_ticks", "service_image_path_sha256",
            "python_command_sha256",
        } and result[receipt] is True and type(result["service_pid"]) is int
            and 0 < result["service_pid"] <= 2147483647
            and bridge._hash(result["python_command_sha256"])
            and result["controller_plan_sha256"] == controller["plan_sha256"]
            and type(result["service_created_ticks"]) is str
            and re.fullmatch(r"[1-9][0-9]{1,19}", result["service_created_ticks"])
            and result["service_image_path_sha256"] == plan["transition_service_start"]["service_image_path_sha256"],
            "native_transition_service_start_unconfirmed")
        return result


def _transition_authority_scope(controller, generation, root, attempt, old_vm_id):
    error = "native_transition_authority_record_invalid"
    generation.validate()
    bridge._need(generation.epoch > 1 and type(root) is str and re.fullmatch(
        r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root)
        and bridge._hash(attempt)
        and type(old_vm_id) is str and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", old_vm_id)
        and old_vm_id != "00000000-0000-0000-0000-000000000000"
        and type(controller) is dict and set(controller) == {"plan", "plan_sha256"}
        and type(controller["plan"]) is bytes and 0 < len(controller["plan"]) <= 1048576
        and bridge._digest(controller["plan"]) == controller["plan_sha256"], error)
    try:
        plan = json.loads(controller["plan"], object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(error) from None
    bridge._need(type(plan) is dict and plan.get("profile") == "enforced-vm-v1"
                 and type(plan.get("generation")) is int and plan["generation"] == generation.epoch
                 and plan.get("node") == generation.partner
                 and all(type(plan.get(field)) is dict for field in
                         ("windows_enforced", "windows_transition_observer", "transition_service_start")), error)
    scope = plan["windows_enforced"]
    observer = plan["windows_transition_observer"]
    image = plan["transition_service_start"].get("service_image_path_sha256")
    service = scope.get("service_name")
    python_path = scope.get("python_path")
    bridge._need(bridge._hash(image)
                 and type(service) is str and re.fullmatch(r"N100HA_[a-f0-9]{24}_controller", service)
                 and type(python_path) is str and python_path.isascii()
                 and bridge._hash(observer.get("legacy_plan_sha256"))
                 and scope.get("vm_id") == old_vm_id, error)
    disks = scope.get("disks")
    bridge._need(type(disks) is list and 1 <= len(disks) <= 8, error)
    paths = []
    for disk in disks:
        path = disk.get("path") if type(disk) is dict else None
        bridge._need(type(path) is str and re.fullmatch(r"[A-Za-z]:\\[\x20-\x7e]+", path), error)
        paths.append(path.lower())
    bridge._need(len(paths) == len(set(paths)), error)
    old_disk_paths_sha256 = bridge._digest("\n".join(sorted(paths)).encode("ascii"))
    return observer["legacy_plan_sha256"], service, image, python_path, old_disk_paths_sha256


def validate_transition_approval(controller, approval):
    error = "native_transition_plan_publication_unconfirmed"
    bridge._need(type(controller) is dict and set(controller) == {"plan", "plan_sha256"}
                 and type(controller["plan"]) is bytes and 0 < len(controller["plan"]) <= 49152
                 and bridge._digest(controller["plan"]) == controller["plan_sha256"]
                 and type(approval) is bytes and 0 < len(approval) <= 4096, error)
    try:
        plan, policy = (json.loads(raw, object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
                        for raw in (controller["plan"], approval))
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(error) from None
    expected = {"schema_version": 3, "profile": "enforced-vm-v1", "action": "ha-transition",
                "authority_kind": "installed-runtime", "plan_sha256": controller["plan_sha256"]}
    bridge._need(type(plan) is dict and plan.get("profile") == expected["profile"]
                 and type(plan.get("generation")) is int and plan["generation"] >= 2
                 and type(plan.get("windows_enforced")) is dict and type(policy) is dict, error)
    installed = policy.get("installed_at")
    expected |= {"installation_id": plan.get("installation_id"), "source_commit": plan.get("source_commit"),
                 "policy_sha256": plan["windows_enforced"].get("policy_sha256"), "installed_at": installed}
    bridge._need(bridge._hash(expected["installation_id"]) and bridge._hash(expected["policy_sha256"])
                 and type(expected["source_commit"]) is str and re.fullmatch(r"[a-f0-9]{40}", expected["source_commit"])
                 and type(installed) is str
                 and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", installed)
                 and bridge._matches(policy, expected), error)

    try:
        installed_at = datetime.fromisoformat(installed[:-1] + "+00:00")
    except ValueError:
        raise bridge.BridgeRejected(error) from None
    bridge._need(installed_at < datetime.now(timezone.utc), error)

    bundle = bridge._bytes({"plan": base64.b64encode(controller["plan"]).decode("ascii"),
                            "approval": base64.b64encode(approval).decode("ascii")})
    data = base64.b64encode(bundle).decode("ascii")
    bridge._need(len(data) <= 65536, error)
    return bundle, data


def publish_transition_plan(windows, controller, approval, *, root, stage_sha256, manifest_sha256, deadline):
    """Publish only the supplied approved plan bytes and exact installed approval."""
    error = "native_transition_plan_publication_unconfirmed"
    bridge._need(type(root) is str and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root)
                 and bridge._hash(manifest_sha256), error)
    bundle, data = validate_transition_approval(controller, approval)
    source = fixed_script("windows-ha-stage-initial-activation.ps1", stage_sha256, {
        "InputPath": root + r"\plan.json", "InputSha256": bridge._digest(bundle),
        "ExpectedSourceManifestSha256": manifest_sha256, "PublishTransitionPlan": True,
        "TransitionBundleBase64": data, "Go": True,
    })
    bridge._remaining(deadline)
    result = windows.powershell(source, deadline)
    bridge._need(bridge._matches(result, {
        "transition_plan_published": True, "plan_sha256": controller["plan_sha256"],
        "approval_sha256": bridge._digest(approval),
    }), error)
    return result


def prepare_staged_transition_service(starter, controller, generation, *, root, attempt, old_vm_id,
                                      approval_sha256, preflight_path, scm_verifier_path, deadline, previous_args_sha256=None):
    """Return launch parameters only after Windows verifies the approved staged inputs."""
    _transition_authority_scope(controller, generation, root, attempt, old_vm_id)
    plan = json.loads(controller["plan"])
    scope, pins = plan["windows_enforced"], plan["transition_service_start"]
    parameters = {
        "ServiceName": scope["service_name"], "ServiceHostSha256": scope["service_image_sha256"],
        "PlanPath": root + r"\plan.json", "PlanSha256": controller["plan_sha256"],
        "ApprovalPath": root + r"\approval.json", "ApprovalSha256": approval_sha256,
        "PreflightPath": preflight_path, "PreflightSha256": pins["preflight_sha256"],
        "ScmVerifierPath": scm_verifier_path,
    }
    expected_args, _ = closure_service_args({
        "format": "enforced-service-v1", "service_name": scope["service_name"], "service_sid": scope["service_sid"],
        "python_path": scope["python_path"], "python_sha256": scope["python_sha256"],
        "launcher_path": scope["launcher_path"], "launcher_sha256": scope["launcher_sha256"],
        "plan_path": parameters["PlanPath"], "plan_sha256": parameters["PlanSha256"],
        "approval_path": parameters["ApprovalPath"], "approval_sha256": parameters["ApprovalSha256"],
        "mode": pins["mode"], "host_sha256": scope["service_image_sha256"],
    }, controller["plan"])
    if previous_args_sha256 is None:
        verified = starter.verify_prepared(controller, parameters, deadline)
    else:
        verified = starter.publish_args(controller, parameters, previous_args_sha256, deadline)
    bridge._need(verified["serving_args_sha256"] == bridge._digest(expected_args),
                 "native_transition_prepared_service_unconfirmed")
    return parameters


def stage_approved_transition_service(starter, controller, generation, approval, *, root, attempt, old_vm_id,
                                      stage_sha256, manifest_sha256, previous_args_sha256,
                                      preflight_path, scm_verifier_path, deadline):
    """Install the exact approved files and arguments before any SCM start."""
    _transition_authority_scope(controller, generation, root, attempt, old_vm_id)
    bridge._need(bridge._hash(previous_args_sha256), "native_transition_prepared_service_unconfirmed")
    publication = publish_transition_plan(
        starter.windows, controller, approval, root=root, stage_sha256=stage_sha256,
        manifest_sha256=manifest_sha256, deadline=deadline,
    )
    return prepare_staged_transition_service(
        starter, controller, generation, root=root, attempt=attempt, old_vm_id=old_vm_id,
        approval_sha256=publication["approval_sha256"], preflight_path=preflight_path,
        scm_verifier_path=scm_verifier_path, deadline=deadline, previous_args_sha256=previous_args_sha256,
    )


def transition_authority_records(controller, started, generation, *, root, attempt, handoff_sha256, old_vm_id):
    """Render the two protected inputs consumed by the live Windows lease verifier."""
    legacy, service, image, python_path, old_disk_paths_sha256 = _transition_authority_scope(
        controller, generation, root, attempt, old_vm_id,
    )
    bridge._need(bridge._hash(handoff_sha256) and type(started) is dict
                 and started.get("controller_plan_sha256") == controller["plan_sha256"]
                 and started.get("service_image_path_sha256") == image and bridge._hash(started.get("python_command_sha256"))
                 and type(started.get("service_pid")) is int and 0 < started["service_pid"] <= 2147483647
                 and type(started.get("service_created_ticks")) is str
                 and re.fullmatch(r"[1-9][0-9]{1,19}", started["service_created_ticks"]),
                 "native_transition_authority_record_invalid")
    binding = bridge._bytes({
        "attempt_id": attempt, "controller_plan_sha256": controller["plan_sha256"],
        "generation": generation.epoch, "legacy_plan_sha256": legacy,
        "old_disk_paths_sha256": old_disk_paths_sha256, "old_vm_id": old_vm_id,
        "python_command_sha256": started["python_command_sha256"], "python_path_sha256": bridge._digest(python_path.encode()),
        "service_created_ticks": started["service_created_ticks"], "service_image_path_sha256": image,
        "service_name": service, "service_pid": started["service_pid"],
        "state_path": root + r"\journal\controller-state.json", "target": generation.primary, "timeline": generation.timeline,
    })
    pin = bridge._digest(binding)
    return {
        "binding_path": root + rf"\transition-service-{attempt}.json", "binding": binding,
        "service_instance_sha256": pin, "grant_path": root + rf"\transition-grant-{attempt}.json",
        "grant": bridge._bytes({"attempt_id": attempt, "handoff_sha256": handoff_sha256, "service_instance_sha256": pin}),
    }


def publish_transition_authority_records(windows, stage_sha256, records, controller_sha256, deadline):
    """Install the exact service binding first, then its generation grant."""
    instance = records["service_instance_sha256"]
    bridge._need(bridge._hash(controller_sha256) and bridge._hash(instance)
                 and bridge._digest(records["binding"]) == instance, "native_transition_authority_record_invalid")
    for name, parent_pin in (("binding", controller_sha256), ("grant", instance)):
        raw = records[name]
        pin = bridge._digest(raw)
        source = fixed_script("windows-ha-stage-initial-activation.ps1", stage_sha256, {
            "InputPath": records[f"{name}_path"], "InputSha256": pin, "ExpectedSourceManifestSha256": parent_pin,
            "IntentBase64": base64.b64encode(raw).decode("ascii"), "PublishTransitionAuthority": True, "Go": True,
        })
        bridge._remaining(deadline)
        result = windows.powershell(source, deadline)
        bridge._need(result == {"transition_authority_record_sha256": pin},
                     "native_transition_authority_record_unconfirmed")
    return instance


class WindowsTransitionLease:
    """Sample the running Windows controller and the remaining writer exclusions."""

    def __init__(self, windows, binding_path, *, service_instance_sha256, live_sha256, transition_attempt,
                 legacy_plan_sha256, generation_handoff_sha256, generation, assert_non_vm_exclusions):
        bridge._need(all(bridge._hash(value) for value in (
            service_instance_sha256, live_sha256, transition_attempt, legacy_plan_sha256, generation_handoff_sha256,
        )) and type(binding_path) is str and re.fullmatch(
            r'C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\transition-service-[a-f0-9]{64}\.json', binding_path,
        ) and binding_path.endswith(f'-{transition_attempt}.json')
            and callable(assert_non_vm_exclusions), 'native_transition_service_scope_invalid')
        generation.validate()
        bridge._need(generation.epoch > 1, 'native_transition_service_scope_invalid')
        self.windows = windows
        self.live_sha256 = live_sha256
        self.generation = generation
        self.assert_non_vm_exclusions = assert_non_vm_exclusions
        self.parameters = {'BindingPath': binding_path, 'LegacyPlanSha256': legacy_plan_sha256,
                           'AttemptId': transition_attempt, 'ExpectedServiceInstanceSha256': service_instance_sha256,
                           'TransitionHandoffSha256': generation_handoff_sha256}

    @contextmanager
    def guest_authority(self, deadline):
        with self.windows.live_authority_session(min(deadline, time.monotonic() + 12)) as powershell:
            def check(challenge, boundary, boundary_deadline):
                bridge._need(bridge._hash(challenge) and type(boundary) is str and 0 < len(boundary) <= 128,
                             'native_transition_boundary_invalid')
                limit = min(deadline, boundary_deadline)
                excluded = self.assert_non_vm_exclusions(limit)
                bridge._need(type(excluded) is dict and set(excluded) == {'legacy_sha256', 'standby_sha256'}
                             and all(bridge._hash(value) for value in excluded.values()),
                             'native_transition_exclusions_unconfirmed')
                boundary_sha256 = bridge._digest(boundary.encode())
                source = fixed_script('windows-ha-cutover-live-authority.ps1', self.live_sha256, self.parameters | {
                    'PriorSha256': challenge, 'TransitionBoundarySha256': boundary_sha256,
                })
                result = powershell(source, limit)
                if boundary == "controller-ready" and bridge._matches(result, {"transition_authority_pending": True}):
                    return None
                generation = self.generation
                bridge._need(type(result) is dict and bridge._hash(result.get('transition_proof_sha256')) and
                             bridge._matches(result, {'generation': generation.epoch, 'timeline': generation.timeline,
                                                      'revision': generation.revision,
                                                      'transition_proof_sha256': result['transition_proof_sha256']}),
                             'native_transition_service_proof_unconfirmed')
                bridge._remaining(limit)
                return bridge._digest(bridge._bytes(result | excluded | {
                    'challenge': challenge, 'boundary_sha256': boundary_sha256,
                }))
            yield check


def read_transition_closure_receipt(windows, live_sha256, root, plan_sha256, generation, *,
                                    host_pid, started_ms, service_sid, deadline):
    """Read the protected receipt belonging to the just-executed query."""
    bridge._need(type(root) is str and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root)
                 and bridge._hash(plan_sha256)
                 and all(type(value) is int and 0 < value < 10**18 for value in
                         (host_pid, started_ms, generation))
                 and type(service_sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}", service_sid),
                 "native_transition_db_closure_unconfirmed")
    source = fixed_script("windows-ha-cutover-live-authority.ps1", live_sha256, {
        "ReadClosureReceipt": True, "BindingPath": root + rf"\journal\lease-closure-{plan_sha256}.json",
        "ClosurePlanSha256": plan_sha256, "ClosureGeneration": str(generation),
        "ClosureHostPid": str(host_pid), "ClosureStartedMs": str(started_ms), "ClosureServiceSid": service_sid,
    })
    bridge._remaining(deadline)
    return windows.powershell(source, deadline)


def read_transition_rejoin_receipt(windows, live_sha256, root, plan_sha256, mode, *,
                                   host_pid, started_ms, service_sid, deadline):
    """Read the result for the exact Windows service invocation."""
    error = "native_transition_rejoin_receipt_unconfirmed"
    bridge._need(type(root) is str and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}", root)
                 and bridge._hash(plan_sha256)
                 and mode in ("initialize", "stage-rejoin", "observe-rejoin-stage", "check-rejoin")
                 and type(host_pid) is int and 0 < host_pid <= 2147483647
                 and type(started_ms) is int and 0 < started_ms < 10**18
                 and type(service_sid) is str and re.fullmatch(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}", service_sid),
                 error)
    source = fixed_script("windows-ha-cutover-live-authority.ps1", live_sha256, {
        "ReadRejoinReceipt": True, "BindingPath": root + r"\journal", "RejoinMode": mode,
        "ClosurePlanSha256": plan_sha256, "ClosureHostPid": str(host_pid),
        "ClosureStartedMs": str(started_ms), "ClosureServiceSid": service_sid,
    })
    bridge._remaining(deadline)
    result = windows.powershell(source, deadline)
    stage = result.get("rejoin_stage_sha256") if type(result) is dict else None
    phase = "fencing" if mode == "initialize" else "rejoining"
    expected = {"status": "PASS", "phase": phase, "authority_opened": False, "rejoin_stage_sha256": stage}
    if type(result) is dict:
        wal_keys = result.keys() & {"source_fence_wal", "replica_fence_wal"}
        if wal_keys:
            bridge._need(mode != "initialize" and len(wal_keys) == 1, error)
            key, = wal_keys
            bridge._need(bridge._wal_pin(result[key]), error)
            expected[key] = result[key]
    if mode == "check-rejoin":
        revision = result.get("revision") if type(result) is dict else None
        bridge._need(type(revision) is int and 0 < revision < 2**63, error)
        expected |= {"standby_rejoined": True, "revision": revision}
    bridge._need(bridge._hash(stage) and bridge._matches(result, expected), error)
    return result


def observe_transition_db_closed(starter, controller, parameters, generation, *, live_sha256, root, deadline):
    """Recover a completed SCM check after a lost reply, then verify the separate DB receipt."""
    try:
        started = starter.check_lease_closed(controller, parameters, min(deadline, time.monotonic() + 22))
    except (bridge.BridgeRejected, TimeoutExpired, OSError):
        started = starter.observe_closure_check(controller, parameters, deadline)
    return read_transition_closure_receipt(
        starter.windows, live_sha256, root, controller["plan_sha256"], generation.epoch,
        host_pid=started["host_pid"], started_ms=started["started_at_ms"], service_sid=started["service_sid"],
        deadline=deadline,
    )


def settle_transition_controller(starter, controller, parameters, started, generation, observe_closed, deadline):
    """Stop the identified controller and require authenticated closure on both databases."""
    if started is None:
        started = starter.observe(controller, parameters, deadline)
    starter.stop(controller, parameters, started, deadline)
    proof = observe_closed(controller, generation, deadline)
    bridge._need(type(proof) is dict and set(proof) == {"plan_sha256", "generation", "node_proofs"}
                 and proof["plan_sha256"] == controller["plan_sha256"]
                 and type(proof["generation"]) is int and proof["generation"] == generation.epoch
                 and type(proof["node_proofs"]) is dict and set(proof["node_proofs"]) == {"A", "B"}
                 and all(bridge._hash(value) for value in proof["node_proofs"].values())
                 and proof["node_proofs"]["A"] != proof["node_proofs"]["B"],
                 "native_transition_db_closure_unconfirmed")
    bridge._remaining(deadline)
    return proof


def close_previous_transition_service(
    starter, previous, previous_parameters, previous_generation, controller, generation, *,
    previous_root, previous_attempt, previous_old_vm_id, live_sha256, deadline,
):
    """Settle the exact prior controller and read the argument pin for its successor."""
    _, service, image, _, _ = _transition_authority_scope(
        previous, previous_generation, previous_root, previous_attempt, previous_old_vm_id,
    )
    generation.validate()
    error = "native_transition_predecessor_unconfirmed"
    bridge._need(type(controller) is dict and set(controller) == {"plan", "plan_sha256"}
                 and type(controller["plan"]) is bytes and 0 < len(controller["plan"]) <= 1048576
                 and bridge._digest(controller["plan"]) == controller["plan_sha256"], error)
    policy = json.loads(previous["plan"])
    try:
        successor = json.loads(controller["plan"], object_pairs_hook=bridge._pairs, parse_constant=bridge._reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise bridge.BridgeRejected(error) from None
    bridge._need(type(successor) is dict and successor.get("profile") == "enforced-vm-v1"
                 and type(successor.get("windows_enforced")) is dict and type(successor.get("transition_service_start")) is dict
                 and successor.get("cluster") == policy["cluster"] and successor.get("system_id") == policy["system_id"]
                 and successor.get("installation_id") == policy["installation_id"]
                 and generation.partner == previous_generation.primary
                 and generation.epoch == previous_generation.epoch + 1
                 and generation.timeline == previous_generation.timeline + 1
                 and successor.get("timeline") == previous_generation.timeline
                 and successor.get("generation") == generation.epoch and successor.get("node") == generation.partner
                 and successor.get("source_lease_close_required") is True
                 and successor["windows_enforced"].get("service_name") == service
                 and successor["windows_enforced"].get("service_sid") == policy["windows_enforced"]["service_sid"]
                 and successor["windows_enforced"].get("service_image_sha256") == policy["windows_enforced"]["service_image_sha256"]
                 and successor["transition_service_start"].get("service_image_path_sha256") == image, error)

    pins = policy["transition_service_start"]
    bridge._need(type(previous_parameters) is dict and previous_parameters.get("PlanPath") == previous_root + r"\plan.json"
                 and previous_parameters.get("PlanSha256") == previous["plan_sha256"]
                 and pins.get("lease_closure_check") is True
                 and type(pins.get("target_timeline")) is int and pins["target_timeline"] == previous_generation.timeline
                 and type(policy.get("timeline")) is int and policy["timeline"] < previous_generation.timeline, error)
    fixed_script("windows-ha-cutover-live-authority.ps1", live_sha256, {})

    def observe_closed(plan, current_generation, limit):
        return observe_transition_db_closed(
            starter, plan, previous_parameters, current_generation,
            live_sha256=live_sha256, root=previous_root, deadline=limit,
        )

    settle_transition_controller(starter, previous, previous_parameters, None, previous_generation, observe_closed, deadline)
    verified = starter.verify_prepared(previous, previous_parameters, deadline)
    return verified["serving_args_sha256"]


@contextmanager
def open_transition_controller(controller, generation, *, starter, prepare_service, stage_sha256, live_sha256,
                               root, attempt, old_vm_id, publish_handoff, assert_non_vm_exclusions, deadline, observe_closed=None):
    """Start the approved controller, bind its live lease, then expose fresh checks."""
    generation.validate()
    bridge._need(generation.epoch > 1 and all(bridge._hash(value) for value in (attempt, stage_sha256, live_sha256))
                 and callable(assert_non_vm_exclusions) and callable(publish_handoff)
                 and (observe_closed is None or callable(observe_closed)),
                 "native_transition_service_scope_invalid")
    _transition_authority_scope(controller, generation, root, attempt, old_vm_id)
    if observe_closed is None:
        plan = json.loads(controller["plan"])
        scope = plan["transition_service_start"]
        source_timeline = plan.get("timeline")
        target_timeline = scope.get("target_timeline")
        bridge._need(scope.get("lease_closure_check") is True
                     and type(source_timeline) is int and type(target_timeline) is int
                     and source_timeline < target_timeline == generation.timeline,
                     "native_transition_db_closure_unconfirmed")
    parameters = prepare_service(controller, generation, deadline)
    started = None
    ready = False
    try:
        started = starter.start(controller, parameters, deadline)
        options = {"root": root, "attempt": attempt, "old_vm_id": old_vm_id}
        records = transition_authority_records(controller, started, generation, handoff_sha256="0" * 64, **options)
        instance = records["service_instance_sha256"]
        handoff = publish_handoff(generation, instance, deadline)
        records = transition_authority_records(controller, started, generation, handoff_sha256=handoff, **options)
        bridge._need(records["service_instance_sha256"] == instance, "native_transition_service_scope_invalid")
        publish_transition_authority_records(starter.windows, stage_sha256, records, controller["plan_sha256"], deadline)
        legacy = json.loads(records["binding"])["legacy_plan_sha256"]
        authority = WindowsTransitionLease(
            starter.windows, records["binding_path"], service_instance_sha256=instance, live_sha256=live_sha256,
            transition_attempt=attempt, legacy_plan_sha256=legacy, generation_handoff_sha256=handoff,
            generation=generation, assert_non_vm_exclusions=assert_non_vm_exclusions,
        )
        with authority.guest_authority(deadline) as check:
            ready_deadline = min(deadline, time.monotonic() + 25)
            while check(secrets.token_hex(32), "controller-ready", min(ready_deadline, time.monotonic() + 7)) is None:
                time.sleep(min(0.2, bridge._remaining(ready_deadline)))
            ready = True

            def assert_guard():
                return check(secrets.token_hex(32), "ingress", min(deadline, time.monotonic() + 7))

            yield assert_guard, check
    finally:
        if not ready and starter.attempted:
            def default_observer(plan, current_generation, limit):
                return observe_transition_db_closed(
                    starter, plan, parameters, current_generation,
                    live_sha256=live_sha256, root=root, deadline=limit,
                )

            settle_transition_controller(
                starter, controller, parameters, started, generation, observe_closed or default_observer, time.monotonic() + 35,
            )
