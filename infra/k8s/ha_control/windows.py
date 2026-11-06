"""Native Windows fence bridge; never instantiated by Linux/Mac lab adapters."""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import queue
import time

from .controller import TransitionBlocked
from .legacy_guard import GuardBlocked, hex64, pairs, validate_plan


class WindowsFence:
    def __init__(self, plan, approval):
        if sys.platform != "win32":
            raise TransitionBlocked("native_windows_required")
        self.plan, self.approval = Path(plan), Path(approval)
        if not self.plan.is_absolute() or not self.approval.is_absolute():
            raise TransitionBlocked("private_configuration_required")
        try:
            raw = self.plan.read_bytes()
            approval_raw = self.approval.read_bytes()
            if len(raw) > 1048576 or len(approval_raw) > 1048576:
                raise ValueError()
            self.plan_data = json.loads(raw, object_pairs_hook=pairs)
            self.plan_sha256 = hashlib.sha256(raw).hexdigest()
            approval_data = json.loads(approval_raw, object_pairs_hook=pairs)
            self.approval_sha256 = hashlib.sha256(approval_raw).hexdigest()
            self.approval_action = approval_data["action"]
            if not isinstance(self.plan_data, dict):
                raise ValueError()
            validate_plan(self.plan_data["legacy_guard"])
            hex64(self.plan_data["legacy_linux_sha256"])
        except (OSError, ValueError, KeyError, TypeError, GuardBlocked):
            raise TransitionBlocked("private_configuration_required") from None
        self.process = None

    @contextmanager
    def guard(self):
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).with_name("windows-fence.ps1")
        try:
            self.process = subprocess.Popen([str(executable), "-NoProfile", "-NonInteractive", "-File", str(script),
                                             "-Plan", str(self.plan), "-Approval", str(self.approval), "-Go"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, bufsize=1)
            self.answers = queue.Queue()
            process = self.process
            def read():
                for line in process.stdout:
                    self.answers.put(line)
                self.answers.put(None)
            threading.Thread(target=read, daemon=True).start()
            answer = self.answers.get(timeout=15)
            handshake = json.loads(answer, object_pairs_hook=pairs) if answer else {}
            if not isinstance(handshake, dict) or type(handshake.get("legacy_guard_schema")) is not int or handshake != {"guard": "ready", "plan_sha256": self.plan_sha256,
                                                   "approval_sha256": self.approval_sha256, "approval_action": self.approval_action,
                                                   "legacy_guard_schema": 1}:
                raise TransitionBlocked("windows_guard_unavailable")
            self.plan_data["_approval_action"] = self.approval_action
            yield
            if self.process.poll() is not None:
                raise TransitionBlocked("windows_guard_lost")
        except (OSError, ValueError, queue.Empty, GuardBlocked):
            raise TransitionBlocked("windows_guard_unavailable") from None
        finally:
            if self.process:
                try:
                    self.process.stdin.close()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=5)
                self.process = None

    def request(self, action, state):
        if action not in {"observe", "fence"} or not self.process or self.process.poll() is not None:
            raise TransitionBlocked("windows_guard_lost")
        nonce = secrets.token_hex(32)
        started = time.monotonic()
        try:
            self.process.stdin.write(json.dumps(dict(action=action, node=state["old"],
                                                    generation=state["generation"], nonce=nonce)) + "\n")
            self.process.stdin.flush()
            answer = self.answers.get(timeout=2 if action == "observe" else 45)
            value = json.loads(answer, object_pairs_hook=pairs) if answer else {}
            if not isinstance(value, dict):
                raise TransitionBlocked("windows_fence_unconfirmed")
            proof = value.get("legacy_guard")
            expected = {"schema_version": 1, "nonce": nonce, "plan_sha256": self.plan_sha256,
                        "cluster": self.plan_data["cluster"], "generation": state["generation"],
                        "collector_sha256": self.plan_data["legacy_linux_sha256"], "complete": True, "verified": True}
            if (value.get("nonce") != nonce or value.get("node") != state["old"]
                    or type(value.get("generation")) is not int or value.get("generation") != state["generation"]
                    or value.get("status") != "ok" or not isinstance(value.get("fenced"), bool)
                    or set(value) != {"nonce", "node", "generation", "status", "fenced", "legacy_guard"}
                    or proof != expected or not isinstance(proof, dict)
                    or any(type(proof[k]) is not type(expected[k]) for k in expected)
                    or action == "observe" and time.monotonic() - started > 2):
                raise TransitionBlocked("windows_fence_unconfirmed")
            return value.get("fenced") is True
        except (OSError, ValueError, TypeError, KeyError, queue.Empty, GuardBlocked):
            raise TransitionBlocked("windows_fence_unconfirmed") from None

    def fenced(self, state):
        return self.request("observe", state)

    def fence(self, state):
        if not self.request("fence", state):
            raise TransitionBlocked("positive_fence_required")
