"""Authenticate N100 successor publication and recover the exact app handoff."""

import base64
import json
from pathlib import PureWindowsPath
import time
from types import SimpleNamespace

from .controller import TransitionBlocked
from .initial_app_resume_wiring import PairedProofInputs, SuccessorAppStage
from .initial_postboot import FILES, scope as postboot_scope
from .native_saved_site_apps import SavedSiteApps
from .native_saved_site_material import SavedSitePlanAssembly
from .native_stdio_bridge import _bytes, _digest, _hash, _remaining
from .native_windows_activation import WindowsActivationLease


def _need(value):
    if not value:
        raise TransitionBlocked('saved_site_publication_unconfirmed')


_CAP = 262144
_SLICE = 2048
_READ_CARRYOVER = r'''
import base64,json,os
from ha_control import native_guest_worker as worker,native_stdio_bridge as wire

assert os.geteuid()==0 and wire._digest(worker.runtime.BOOT_ID.read_bytes())==CFG['boot_sha256']
assert all(wire._hash(CFG[k]) for k in ('binding_attempt','attempt','carryover_sha256'))
path=worker.ROOT/CFG['binding_attempt']/('postboot-'+CFG['attempt']+'-carryover-stopped.json')
raw=worker.runtime._read(path,0,{0o400},limit=262144)
assert wire._digest(raw)==CFG['carryover_sha256'] and 0<=CFG['offset']<len(raw)
part=raw[CFG['offset']:CFG['offset']+2048]
print(json.dumps({'offset':CFG['offset'],'size':len(raw),'part':base64.b64encode(part).decode()},
                 sort_keys=True,separators=(',',':')))
'''


def read_carryover(apps, postboot, deadline):
    """Read a pinned root-owned record without redoing attestation or app start."""
    _need(type(apps) is SavedSiteApps)
    pin, attempt = postboot['carryover_receipt_sha256'], postboot['attempt_id']
    _need(_hash(pin) and _hash(attempt))
    binding = apps.dispatcher.binding
    query = {'attempt': attempt, 'carryover_sha256': pin, 'binding_attempt': binding.attempt_id,
             'boot_sha256': binding.boot('A')}
    raw = bytearray()
    size = None
    while size is None or len(raw) < size:
        _remaining(deadline)
        result = apps._exchange_guest(query | {'offset': len(raw)}, _READ_CARRYOVER, b'', deadline)
        _need(type(result) is dict and set(result) == {'offset', 'size', 'part'}
              and type(result['offset']) is int and result['offset'] == len(raw)
              and type(result['size']) is int and 0 < result['size'] <= _CAP
              and (size is None or size == result['size']) and type(result['part']) is str)
        size = result['size']
        part = base64.b64decode(result['part'], validate=True)
        _need(len(part) == min(_SLICE, size - len(raw)))
        raw.extend(part)
    _need(_digest(raw) == pin)
    return bytes(raw)


class SavedSitePublication:
    """Wire the saved-site driver callbacks to native A/B proof and app startup."""

    def __init__(self, plan, get_apps, *, live_authority_sha256):
        _need(type(plan) is SavedSitePlanAssembly and callable(get_apps) and _hash(live_authority_sha256))
        self.plan, self.get_apps, self.live_pin = plan, get_apps, live_authority_sha256

    def _stage(self, operation, chain, published, terminal, deadline):
        _remaining(deadline)
        _need(operation['scope'] == self.plan.scope and operation['stage'] in {'publish', 'start_apps'}
              and _hash(published['first_write_proof_sha256']) and _hash(published['service_instance_sha256']))
        material = self.plan.read_arm_material(operation, chain, deadline)
        plan, pin = material['candidate'], material['plan_pin']
        _need(pin == chain['arm_host']['plan_sha256'] and terminal == material['files'][FILES[0]]
              and _digest(terminal) == chain['retire_provider']['terminal_sha256'])
        postboot = postboot_scope(plan)
        apps = self.get_apps(operation, chain, deadline)
        _need(type(apps) is SavedSiteApps and apps.pair is self.plan.pair and apps.terminal == terminal)
        windows = self.plan.pair.windows
        path = plan['initial_service_start']['binding_path']
        root = PureWindowsPath(path).parent
        runtime = PureWindowsPath(plan['windows_enforced']['python_path']).parent.parent
        proof = PairedProofInputs(runtime_path=str(runtime), plan_path=str(root / 'plan.json'), plan_sha256=pin,
            terminal_path=str(root / FILES[0]), terminal_sha256=postboot['predecessor_terminal_sha256'],
            python_sha256=plan['windows_enforced']['python_sha256'], source_commit=plan['source_commit'])
        custody = SimpleNamespace(binding=apps.dispatcher.binding, live_sha256=self.live_pin, windows=windows)
        lease = WindowsActivationLease(custody, path, published)
        stage = SuccessorAppStage(lease=lease, dispatcher=apps.dispatcher, powershell=windows.powershell,
            proof_inputs=proof, plan=plan, predecessor_terminal=terminal, launcher=apps.launcher,
            manifest_sha256=apps.manifest, prior_intent_sha256=apps.history['intent_sha256'],
            prior_progress_sha256=apps.history['progress_sha256'], plan_raw=_bytes(plan))
        return apps, stage

    def paired_publication(self, published, terminal, operation, chain, deadline):
        apps, stage = self._stage(operation, chain, published, terminal, deadline)
        with self.plan.pair.windows.live_authority_session(min(deadline, time.monotonic() + 12)) as powershell:
            stage.powershell = powershell
            return stage.observe_publication(deadline, assert_guard=apps.guard,
                                             predecessor_binding=self.plan.previous['binding'])

    def app_stage(self, operation, chain, published, deadline):
        terminal = self.plan.read_terminal(operation, chain, deadline)
        apps, stage = self._stage(operation, chain, published, terminal, deadline)
        return stage, read_carryover(apps, stage.postboot, deadline)
