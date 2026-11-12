"""Concrete production host observations for the WSL cutover coordinator."""

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from ha_runtime.config import SERVICES

from .legacy_cutover import _pin
from .legacy_writers import _need
from .wsl_writer_proof import _recovery_settled


POWERSHELL = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
HEALTH = tuple('https://' + host + '/health' for host in ('len.pe.kr', 'books.len.pe.kr', 'memo.len.pe.kr', 'news.len.pe.kr'))
EXTERNAL_ACTORS = frozenset({'github:deploy-n100', 'windows:reactivation', 'wsl:manual-deploy'})


PROTECTED = r'''
$ErrorActionPreference='Stop'
$boot=(Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().Ticks
$trading=@(Get-Process -ErrorAction Stop | Where-Object ProcessName -Match '^(terminal64|terminal|metatrader)$' |
    Sort-Object Id | ForEach-Object { $_.ProcessName + ':' + $_.Id + ':' + $_.StartTime.ToUniversalTime().Ticks })
if($trading.Count -ne 1){throw 'protected_trading_process_missing'}
$guests=@(Get-Process -Name vmwp -ErrorAction Stop | Sort-Object Id | ForEach-Object { $_.Id.ToString() + ':' + $_.StartTime.ToUniversalTime().Ticks })
if($guests.Count -lt 2){throw 'protected_guests_missing'}
$raw=[Text.Encoding]::UTF8.GetBytes((@($boot.ToString())+$trading+$guests) -join "`n")
$sha=[Security.Cryptography.SHA256]::Create()
try{[Console]::WriteLine([BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-','').ToLowerInvariant())}
finally{$sha.Dispose()}
'''


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def protected_host_stamp():
    encoded = base64.b64encode(PROTECTED.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run((POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded),
                                stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=8)
        windows = _pin(result.stdout.decode('ascii').strip())
        return hashlib.sha256(windows.encode() + b':' + BOOT_ID.read_bytes()).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError):
        _need(False, 'native_host_protection_unverified')


class WindowsServiceLiveAuthority:
    def __init__(self, binding_path, source_sha256, service_instance):
        _need(type(binding_path) is str and re.fullmatch(
            r'C:\\ProgramData\\N100HA-Cutover-Inputs\\[a-f0-9]{40}\\initial-service-[a-f0-9]{64}\.json', binding_path),
            'native_host_live_authority_unverified')
        self.binding_path = binding_path
        _need(callable(service_instance), 'native_host_live_authority_unverified')
        self.service_instance = service_instance
        source = Path(__file__).with_name('windows-ha-cutover-live-authority.ps1').read_bytes()
        _need(hashlib.sha256(source).hexdigest() == _pin(source_sha256), 'native_host_live_authority_unverified')
        self.source = base64.b64encode(source).decode('ascii')

    def __call__(self, plan, attempt, prior):
        command = (
            "$source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + self.source + "')); "
            "&([scriptblock]::Create($source)) -BindingPath '" + self.binding_path + "' -LegacyPlanSha256 '"
            + _pin(plan) + "' -AttemptId '" + _pin(attempt) + "' -PriorSha256 '" + _pin(prior)
            + "' -ExpectedServiceInstanceSha256 '" + _pin(self.service_instance()) + "'"
        )
        encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
        try:
            result = subprocess.run((POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded),
                                    stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=8)
            _need(0 < len(result.stdout) < 1024, 'native_host_live_authority_unverified')
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            _need(False, 'native_host_live_authority_unverified')


def _public_health(rounds):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    for sequence in range(rounds):
        for url in HEALTH:
            try:
                with opener.open(url, timeout=4) as result:
                    _need(result.status == 200 and result.geturl() == url, 'native_host_external_health_unverified')
            except (OSError, URLError, ValueError):
                _need(False, 'native_host_external_health_unverified')
        if sequence + 1 < rounds:
            time.sleep(10)


class NativeHostEvidence:
    def __init__(self, windows, wsl_bootstrap, admission, external_writers, live_authority):
        self.windows = windows
        self.bootstrap = wsl_bootstrap
        self.admission = admission
        _need(set(external_writers) == SERVICES and all(set(names) == EXTERNAL_ACTORS for names in external_writers.values()),
              'native_host_external_scope_unverified')
        self.live_authority = live_authority
        self.active = False
        self.sequence = 0

    @contextmanager
    def guard(self, plan, attempt):
        _need(not self.active and self.sequence == 0, 'native_host_scope_unbound')
        self.plan, self.attempt = _pin(plan), _pin(attempt)
        self.protected = protected_host_stamp()
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def _scope(self, plan, attempt):
        _need(self.active and (plan, attempt) == (self.plan, self.attempt)
              and self.windows.healthy and self.bootstrap.active, 'native_host_scope_unbound')

    def _receipt(self, boundary, prior, evidence, **facts):
        self.sequence += 1
        value = {'plan_sha256': self.plan, 'attempt_id': self.attempt, 'boundary': boundary, 'prior_sha256': _pin(prior)}
        raw = json.dumps(value | {'sequence': self.sequence, 'evidence': evidence}, separators=(',', ':'), sort_keys=True)
        return value | {'evidence_sha256': hashlib.sha256(raw.encode()).hexdigest()} | facts

    def _protected(self):
        current = protected_host_stamp()
        _need(current == self.protected, 'native_host_protection_changed')
        return current

    def _direct(self, prior):
        self.admission.assert_closed()
        return _recovery_settled(_pin(prior), time.monotonic() + 2)

    def observe_external_closed(self, plan, attempt, prior):
        self._scope(plan, attempt)
        self.bootstrap.assert_visible()
        direct = self._direct(prior)
        windows = self.windows.request('verify', prior)
        return {actor: self._receipt('external_writer_closed', prior, [actor, windows, direct])['evidence_sha256']
                for actor in sorted(EXTERNAL_ACTORS)}

    def observe(self, plan, attempt, boundary, prior):
        self._scope(plan, attempt)
        if boundary == 'preflight':
            _need(self.admission.state() == 'active', 'native_host_deployment_baseline_changed')
            protected = self._protected()
            _public_health(1)
            return self._receipt(boundary, prior, protected, legacy_live=True, protected_healthy=True,
                                 direct_recovery_ready=True, authority_storage_trusted=True, deployment_admission_ready=True)
        if boundary == 'authority_storage_trusted':
            return self._receipt(boundary, prior, self._protected(), authority_storage_trusted=True)
        if boundary == 'direct_recovery_excluded':
            return self._receipt(boundary, prior, self._direct(prior), direct_recovery_excluded=True,
                                 recovery_commands_settled=True)
        if boundary == 'vm_live_authority':
            self.bootstrap.assert_visible()
            proof = self.live_authority(plan, attempt, prior)
            required = {'windows_generation', 'windows_generation_sha256', 'live_lease_sha256'}
            _need(type(proof) is dict and set(proof) == required and type(proof['windows_generation']) is int
                  and proof['windows_generation'] > 0, 'native_host_live_authority_unverified')
            for key in required - {'windows_generation'}:
                _pin(proof[key])
            return self._receipt(boundary, prior, proof, active_node='A', **proof)
        if boundary in ('verify_live', 'legacy_healthy'):
            protected = self._protected()
            _public_health(3 if boundary == 'verify_live' else 1)
            self._protected()
            facts = {'external_healthy': True, 'services': sorted(SERVICES)} if boundary == 'verify_live' else {'legacy_healthy': True}
            return self._receipt(boundary, prior, protected, protected_healthy=True, **facts)
        if boundary == 'durable_legacy_exclusion':
            evidence = self.observe_external_closed(plan, attempt, prior)
            return self._receipt(boundary, prior, evidence, legacy_writers_closed=True, exclusion_durable=True)
        if boundary == 'rollback':
            return self._receipt(boundary, prior, self._direct(prior), readers_settled=True, prior_mutations_settled=True,
                                 deployment_admission_reconciled=True, legacy_route_unchanged=True)
        _need(False, 'native_host_observation_unrecognized')

    def assert_direct_recovery_release_safe(self, plan, attempt, safety):
        self._scope(plan, attempt)
        self.bootstrap.assert_visible()
        proof = self._receipt('direct_recovery_release_safe', safety, self._direct(safety))
        return {key: proof[key] for key in ('plan_sha256', 'attempt_id', 'evidence_sha256')} | {
            'safety_sha256': safety, 'release_safe': True,
        }
