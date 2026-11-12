"""Native Windows and GitHub stages for the legacy cutover transaction."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import time

from .controller import TransitionBlocked, exclusive
from .legacy_cutover import _pin


class WindowsReactivationSession:
    """Keep one authenticated, source-pinned Windows command alive per attempt.

    The configured launcher must load the exact Windows driver and private plan,
    and invoke Invoke-HaReactivationSession with the appended plan and attempt IDs.
    A lost channel always requires separate reconciliation.
    """

    def __init__(self, launcher):
        self.launcher = launcher
        self.process = None

    @classmethod
    def for_n100_wsl(cls, entrypoint, entrypoint_sha256, driver, driver_sha256, plan_path):
        paths = (entrypoint, driver, plan_path)
        if any(type(path) is not str or not re.fullmatch(r"[A-Za-z]:\\[A-Za-z0-9_.\\-]+", path)
               or any(part in ("", ".", "..") for part in path[3:].split("\\"))
               for path in paths):
            raise TransitionBlocked("legacy_native_launcher_invalid")

        def launcher(plan, attempt):
            # Run the verified bytes already read from the entrypoint, not a
            # second path open after validating its digest.
            command = f"""
$ErrorActionPreference='Stop'
$entry=[IO.File]::ReadAllBytes('{entrypoint}')
$sha=[Security.Cryptography.SHA256]::Create()
try{{$digest=[BitConverter]::ToString($sha.ComputeHash($entry)).Replace('-','').ToLowerInvariant()}}
finally{{$sha.Dispose()}}
if($digest -cne '{_pin(entrypoint_sha256)}'){{throw 'legacy_native_entrypoint_digest'}}
$script=[ScriptBlock]::Create([Text.Encoding]::UTF8.GetString($entry).TrimStart([char]0xFEFF))
& $script -DriverPath '{driver}' -DriverSha256 '{_pin(driver_sha256)}' -PlanPath '{plan_path}' -PlanSha256 '{plan}' -AttemptId '{attempt}'
"""
            encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            return ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                    "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded)

        return cls(launcher)

    @contextmanager
    def guard(self, plan, attempt):
        if self.process is not None:
            raise TransitionBlocked("legacy_native_guard_already_active")
        self.plan, self.attempt = _pin(plan), _pin(attempt)
        self.sequence = 0
        self.buffer = bytearray()
        self.healthy = False
        try:
            argv = (self.launcher(plan, attempt) if callable(self.launcher)
                    else (*self.launcher, "-PlanSha256", plan, "-AttemptId", attempt))
            if not argv or not os.path.isabs(argv[0]):
                raise TransitionBlocked("legacy_native_launcher_invalid")
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            expected = self._base("ready")
            self._receive(expected, time.monotonic() + 20)
            self.healthy = True
            yield
        finally:
            self.healthy = False
            if self.process is not None:
                process, self.process = self.process, None
                process.stdin.close()
                process.stdout.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def _base(self, action):
        return dict(plan_sha256=self.plan, attempt_id=self.attempt, sequence=self.sequence, action=action)

    @staticmethod
    def _wait(fd, event, deadline):
        with selectors.DefaultSelector() as wait:
            wait.register(fd, event)
            if not wait.select(max(0, deadline - time.monotonic())):
                raise TransitionBlocked("legacy_native_command_unconfirmed")

    def _receive(self, expected, deadline):
        fd = self.process.stdout.fileno()
        while b"\n" not in self.buffer:
            self._wait(fd, selectors.EVENT_READ, deadline)
            part = os.read(fd, 4096)
            self.buffer.extend(part)
            if not part or len(self.buffer) > 4096:
                raise TransitionBlocked("legacy_native_response_unconfirmed")
        line, _, remaining = self.buffer.partition(b"\n")
        self.buffer = remaining
        response = json.loads(line)
        if (type(response) is not dict or type(response.get("sequence")) is not int
                or response != expected or time.monotonic() >= deadline):
            raise TransitionBlocked("legacy_native_response_unconfirmed")
        return hashlib.sha256(json.dumps(response, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def request(self, action, proof):
        if (not self.healthy or self.process.poll() is not None
                or action not in {"close", "verify", "restore", "finish"}):
            raise TransitionBlocked("legacy_native_guard_lost")
        self.sequence += 1
        request = self._base(action) | dict(nonce=secrets.token_hex(32), proof_sha256=_pin(proof))
        deadline = time.monotonic() + (90 if action in {"close", "restore"} else 20)
        try:
            fd = self.process.stdin.fileno()
            payload = memoryview(json.dumps(request).encode() + b"\n")
            while payload:
                self._wait(fd, selectors.EVENT_WRITE, deadline)
                payload = payload[os.write(fd, payload):]
            return self._receive(request, deadline)
        except (OSError, ValueError, TransitionBlocked):
            self.healthy = False
            raise TransitionBlocked("legacy_native_command_unconfirmed") from None


class WslBootstrapAdmission:
    """Hold the exact lock used by the installed WSL bootstrap entrypoint."""

    def __init__(self, source_sha256):
        self.root = Path("/mnt/c/personal-server")
        self.source_sha256 = _pin(source_sha256)

    @contextmanager
    def guard(self):
        source = self.root / "scripts/windows-bootstrap.sh"
        lock = self.root / "data/ha-legacy-bootstrap.lock"
        if (source.resolve() != source or lock.resolve() != lock
                or hashlib.sha256(source.read_bytes()).hexdigest() != self.source_sha256):
            raise TransitionBlocked("legacy_wsl_bootstrap_source_changed")
        with exclusive(lock):
            if hashlib.sha256(source.read_bytes()).hexdigest() != self.source_sha256:
                raise TransitionBlocked("legacy_wsl_bootstrap_source_changed")
            yield


class NativeCutover:
    """Bind Windows, GitHub and application-specific stages to LegacyCutover.

    The application stages own the live WSL/VM/capture/restore/Caddy operations
    and their private plan, including positive inspection after uncertain writes.
    """

    def __init__(self, windows, admission, applications, *, wsl_bootstrap=None):
        self.windows = windows
        self.admission = admission
        self.applications = applications
        self.wsl_bootstrap = wsl_bootstrap

    def __getattr__(self, name):
        return getattr(self.applications, name)

    @contextmanager
    def guard(self, plan, attempt):
        self.plan, self.attempt = plan, attempt
        self.admission_owned = self.windows_closed = self.linux_touched = False
        self.admission_unconfirmed = self.windows_unconfirmed = False
        with self.applications.guard(plan, attempt), self.windows.guard(plan, attempt):
            if self.wsl_bootstrap is None:
                yield
            else:
                with self.wsl_bootstrap.guard():
                    yield

    def _receipt(self, evidence, **facts):
        return dict(plan_sha256=self.plan, attempt_id=self.attempt, evidence_sha256=_pin(evidence), **facts)

    def close_admission(self, plan, baseline):
        self.admission_unconfirmed = True
        self.admission.close()
        self.admission_owned = True
        self.admission_unconfirmed = False
        evidence = hashlib.sha256(f"{self.attempt}:{baseline}:github-deploy-drained".encode()).hexdigest()
        return self._receipt(evidence, baseline_sha256=baseline,
                             new_deployments_blocked=True, admitted_deployments_settled=True)

    def block_reactivation(self, plan, baseline, admission):
        self.admission.assert_closed()
        self.applications.assert_direct_recovery_excluded(plan, baseline)
        if self.wsl_bootstrap is None:
            raise TransitionBlocked("legacy_wsl_bootstrap_guard_required")
        self.windows_unconfirmed = True
        windows = self.windows.request("close", admission)
        self.windows_closed = True
        self.windows_unconfirmed = False
        self.linux_touched = True
        evidence = self.applications.block_linux_reactivation(plan, baseline, windows)
        return self._receipt(evidence, baseline_sha256=baseline, admission_sha256=admission, blocked=True)

    def assert_reactivation_closed(self, plan, admission, blocked, boundary):
        self.admission.assert_closed()
        evidence = self.windows.request("verify", blocked)
        return self._receipt(evidence, admission_sha256=admission, reactivation_sha256=blocked, boundary=boundary,
                             new_deployments_blocked=True, admitted_deployments_settled=True,
                             windows_reactivation_blocked=True, recovery_commands_settled=True)

    def assert_activation_safe(self, plan, admission, blocked, fence, restore):
        self.applications.assert_activation_closed(plan, fence, restore)
        safety = self.assert_reactivation_closed(plan, admission, blocked, "before_activation")
        return safety | dict(fence_sha256=fence, restore_sha256=restore,
                             legacy_writers_closed=True, target_writes_closed=True, restore_ready=True)

    def _verify_guard(self, proof):
        self.admission.assert_closed()
        self.applications.assert_direct_recovery_excluded(self.plan, proof)
        self.windows.request("verify", proof)

    def fence_writers(self, plan, blocked, settled):
        self._verify_guard(settled)
        return self.applications.fence_writers(plan, blocked, settled)

    def capture(self, plan, fence):
        self._verify_guard(fence)
        return self.applications.capture(plan, fence)

    def activate_vm(self, plan, verified, settled):
        self._verify_guard(settled)
        return self.applications.activate_vm(plan, verified, settled)

    def switch_caddy(self, plan, authority):
        self._verify_guard(authority)
        return self.applications.switch_caddy(plan, authority)

    def assert_rollback_safe(self, plan, baseline):
        if self.admission_unconfirmed or self.windows_unconfirmed or not self.windows.healthy:
            raise TransitionBlocked("legacy_native_reconciliation_required")
        if self.windows_closed:
            self._verify_guard(baseline)
        elif self.admission_owned:
            self.admission.assert_closed()
        return self.applications.assert_rollback_safe(plan, baseline)

    def restore_legacy(self, plan, baseline, safety):
        if self.windows_closed:
            self._verify_guard(safety)
        runtime = _pin(self.applications.restore_legacy_runtime(plan, baseline, safety, self.linux_touched))
        windows = runtime
        if self.windows_closed:
            self.applications.release_owned_direct_recovery_exclusion(plan, safety)
            windows = self.windows.request("restore", runtime)
            self.applications.assert_legacy_runtime_healthy(plan, baseline)
        if self.admission_owned:
            self.admission.restore(owned_disable=True)
        evidence = hashlib.sha256(f"{runtime}:{windows}".encode()).hexdigest()
        return self._receipt(evidence, baseline_sha256=baseline, safety_sha256=safety,
                             legacy_state_restored=True, deployment_admission_restored=True, ready=True)

    def verify_live(self, plan, authority, ingress):
        receipt = self.applications.verify_live(plan, authority, ingress)
        self._verify_guard(ingress)
        self.applications.assert_durable_legacy_exclusion(plan, authority)
        self.windows.request("finish", ingress)
        return receipt
