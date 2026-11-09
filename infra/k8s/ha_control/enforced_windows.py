"""Separate enforced-vm-v1 bridge; never accepts a legacy observation receipt.

Requires already provisioned service identity, ACLs and VM policy. This module
does not install anything. See docs/20261109_N100_강제소유권_구조전환.md.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

from .controller import TransitionBlocked

PROFILE = "enforced-vm-v1"
HEX = re.compile(r"[0-9a-f]{64}\Z")
SID = re.compile(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}\Z")
CONFIG = frozenset(("driver_sha256", "adapter_sha256", "policy_path", "policy_sha256", "service_name", "service_sid",
                    "boot_ticks", "session_id", "python_path", "python_sha256", "launcher_path",
                    "launcher_sha256", "service_image_sha256", "vm_id", "vm_notes_sha256",
                    "vm_config_root", "disks"))


def _deny():
    raise TransitionBlocked("enforced_windows_unverified")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _deny()
        result[key] = value
    return result


def _decode(raw):
    if len(raw) > 1048576:
        _deny()
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: _deny())


def _hex(value):
    if type(value) is not str or not HEX.fullmatch(value):
        _deny()


def _winpath(value):
    if (type(value) is not str or not re.match(r"^[A-Za-z]:\\", value)
            or "\x00" in value or ".." in PureWindowsPath(value).parts
            or value.startswith("\\") or ":" in value[2:]):
        _deny()


def _powershell_path():
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetSystemDirectoryW
    get_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    get_directory.restype = ctypes.c_uint
    size = get_directory(buffer, len(buffer))
    if not 0 < size < len(buffer):
        _deny()
    return Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"


def _validate(plan, approval, plan_hash, driver_hash):
    # PG-owned plan fields are validated by the PG adapter. Every byte of the
    # outer plan is still bound by approval/handshake/each response hashes.
    if (type(plan) is not dict or plan.get("schema_version") != 2
            or type(plan.get("schema_version")) is not int or plan.get("profile") != PROFILE
            or type(plan.get("cluster")) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plan["cluster"])
            or plan.get("node") not in {"A", "B"} or type(plan.get("generation")) is not int
            or plan["generation"] < 1 or not re.fullmatch(r"[0-9a-f]{40}", str(plan.get("source_commit", "")))):
        _deny()
    cfg = plan.get("windows_enforced")
    if type(cfg) is not dict or set(cfg) != CONFIG:
        _deny()
    for key in ("driver_sha256", "adapter_sha256", "policy_sha256", "session_id", "python_sha256", "launcher_sha256",
                "service_image_sha256", "vm_notes_sha256"):
        _hex(cfg[key])
    if (cfg["driver_sha256"] != driver_hash or not SID.fullmatch(str(cfg["service_sid"]))
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(cfg["service_name"]))
            or type(cfg["boot_ticks"]) is not str or not re.fullmatch(r"[0-9]{1,20}", cfg["boot_ticks"])):
        _deny()
    for key in ("policy_path", "python_path", "launcher_path", "vm_config_root"):
        _winpath(cfg[key])
    if str(uuid.UUID(cfg["vm_id"])) != cfg["vm_id"]:
        _deny()
    disks = cfg["disks"]
    if type(disks) is not list or not 1 <= len(disks) <= 8:
        _deny()
    paths = set()
    for disk in disks:
        if type(disk) is not dict or set(disk) != {"path", "disk_id", "file_identity_sha256"}:
            _deny()
        _winpath(disk["path"])
        _hex(disk["file_identity_sha256"])
        if str(uuid.UUID(disk["disk_id"])) != disk["disk_id"] or disk["path"].casefold() in paths:
            _deny()
        paths.add(disk["path"].casefold())
    # Installed runtime authority is a separate exact envelope, never an
    # expired bounded approval with its expiry silently removed. The fixed
    # plan still binds system/source/generation/host boot through native checks.
    if type(approval) is dict and type(approval.get('schema_version')) is int and approval['schema_version']==3:
        fields={'schema_version','profile','action','authority_kind','installation_id','installed_at',
                'plan_sha256','source_commit','policy_sha256'}
        if (set(approval)!=fields or approval['profile']!=PROFILE or approval['action']!='ha-transition'
                or approval['authority_kind']!='installed-runtime'
                or approval['plan_sha256']!=plan_hash or approval['source_commit']!=plan['source_commit']
                or approval['policy_sha256']!=cfg['policy_sha256']
                or type(plan.get('installation_id')) is not str or not HEX.fullmatch(plan['installation_id'])
                or type(approval['installation_id']) is not str or approval['installation_id']!=plan['installation_id']
                or type(approval['installed_at']) is not str
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",approval['installed_at'])):
            _deny()
        try:
            installed=datetime.fromisoformat(approval['installed_at'].replace('Z','+00:00'))
        except ValueError:
            _deny()
        if installed>=datetime.now(timezone.utc):
            _deny()
        return cfg,None  # Sentinel is issued only by this fully checked branch.
    if (type(approval) is not dict or set(approval) != {"schema_version", "profile", "plan_sha256",
            "source_commit", "policy_sha256", "action", "expires_at"}
            or type(approval["schema_version"]) is not int or approval["schema_version"] != 2
            or approval["profile"] != PROFILE or approval["plan_sha256"] != plan_hash
            or approval["source_commit"] != plan["source_commit"]
            or approval["policy_sha256"] != cfg["policy_sha256"]
            or approval["action"] != "ha-transition"
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", str(approval["expires_at"]))):
        _deny()
    expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    if not 0 < remaining <= 86400:
        _deny()
    return cfg, expires


class EnforcedWindowsFence:
    def __init__(self, plan, approval):
        if sys.platform != "win32" or not sys.flags.isolated or not sys.dont_write_bytecode:
            _deny()
        self.plan, self.approval = Path(plan), Path(approval)
        if not self.plan.is_absolute() or not self.approval.is_absolute():
            _deny()
        try:
            raw, approval_raw = self.plan.read_bytes(), self.approval.read_bytes()
            self.plan_data, self.approval_data = _decode(raw), _decode(approval_raw)
            self.plan_sha256 = hashlib.sha256(raw).hexdigest()
            self.approval_sha256 = hashlib.sha256(approval_raw).hexdigest()
            self.driver = Path(__file__).with_name("windows-enforced-vm.ps1")
            driver_hash = hashlib.sha256(self.driver.read_bytes()).hexdigest()
            self.cfg, self.expires = _validate(self.plan_data, self.approval_data, self.plan_sha256, driver_hash)
            self._installed_runtime = self.expires is None
            if self.cfg["adapter_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
                _deny()
        except TransitionBlocked:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            _deny()
        self.process = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._healthy = False

    def _base(self):
        return {"schema_version": 2, "profile": PROFILE, "scope": "vm-enforcement-only",
                "plan_sha256": self.plan_sha256, "approval_sha256": self.approval_sha256,
                "source_sha256": self.cfg["driver_sha256"], "policy_sha256": self.cfg["policy_sha256"],
                "session_id": self.cfg["session_id"], "principal_sha256": hashlib.sha256(self.cfg["service_sid"].encode()).hexdigest(),
                "boot_ticks": self.cfg["boot_ticks"]}

    @contextmanager
    def guard(self):
        try:
            executable = _powershell_path()
            self.process = subprocess.Popen([str(executable), "-NoProfile", "-NonInteractive", "-File", str(self.driver),
                                            "-Plan", str(self.plan), "-Approval", str(self.approval), "-Go"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           text=True, encoding="utf-8", errors="strict", bufsize=1)
            self._answers = queue.Queue(maxsize=8)
            process = self.process
            def read():
                try:
                    while True:
                        line = process.stdout.readline(16385)
                        if not line or len(line) > 16384 or not line.endswith("\n"):
                            self._answers.put_nowait(None)
                            return
                        self._answers.put_nowait(line)
                except (OSError, ValueError, queue.Full):
                    try:
                        self._answers.put_nowait(None)
                    except queue.Full:
                        pass
            self._reader = threading.Thread(target=read, daemon=True)
            self._reader.start()
            hello = _decode(self._answers.get(timeout=15))
            if type(hello) is not dict:
                _deny()
            expected = self._base() | {"guard": "ready", "broker_instance": hello.get("broker_instance"), "sequence": 1}
            _hex(hello.get("broker_instance"))
            if (hello != expected or type(hello.get("schema_version")) is not int
                    or type(hello.get("sequence")) is not int):
                _deny()
            self.instance = hello["broker_instance"]
            self._sequence = 1
            self._healthy = True
            self.plan_data["_approval_action"] = self.approval_data["action"]
            yield
            if self.process.poll() is not None:
                _deny()
        except (OSError, ValueError, TypeError, AttributeError, queue.Empty):
            _deny()
        finally:
            self._healthy = False
            if self.process:
                # Kill before touching a buffered stdin lock held by a stalled
                # writer; never block parent close behind that writer thread.
                if hasattr(self, "_writer") and self._writer.is_alive():
                    self.process.kill()
                    self.process.wait(timeout=2)
                    self._writer.join(timeout=1)
                try:
                    self.process.stdin.close()
                    self.process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=2)
                if hasattr(self, "_reader"):
                    self._reader.join(timeout=1)
                self.process = None

    def _request(self, action, state):
        with self._lock:
            if (action not in {"observe", "fence"} or not self._healthy or not self.process
                    or self.process.poll() is not None or state.get("old") != self.plan_data["node"]
                    or type(state.get("generation")) is not int
                    or state["generation"] != self.plan_data["generation"]
                    or (self.expires is None and getattr(self,'_installed_runtime',False) is not True)
                    or (self.expires is not None and datetime.now(timezone.utc) >= self.expires)):
                _deny()
            if type(self._sequence) is not int or not 1 <= self._sequence < 9007199254740991:
                _deny()
            self._sequence += 1
            sequence = self._sequence
            nonce = secrets.token_hex(24) + f'{sequence:016x}'
            started = time.monotonic()
            limit = 2 if action == "observe" else 45
            try:
                request = {"action": action, "node": state["old"], "generation": state["generation"],
                           "nonce": nonce, "broker_instance": self.instance, "sequence": sequence}
                sent = threading.Event()
                send_error = []
                process = self.process
                def send():
                    try:
                        process.stdin.write(json.dumps(request) + "\n")
                        process.stdin.flush()
                    except (OSError, ValueError):
                        send_error.append(True)
                    finally:
                        sent.set()
                self._writer = threading.Thread(target=send, daemon=True)
                self._writer.start()
                if not sent.wait(max(0, limit - (time.monotonic() - started))) or send_error:
                    self._healthy = False
                    _deny()
                while True:
                    remaining = limit - (time.monotonic() - started)
                    if remaining <= 0:
                        _deny()
                    response = _decode(self._answers.get(timeout=remaining))
                    if (type(response) is not dict or type(response.get("sequence")) is not int
                            or not 1 <= response["sequence"] <= sequence
                            or type(response.get("nonce")) is not str or not HEX.fullmatch(response["nonce"])
                            or response["nonce"][48:] != f'{response["sequence"]:016x}'
                            or response.get("broker_instance") != self.instance):
                        _deny()
                    # A late response to a previously issued fence is never a
                    # receipt for the new observe reconciliation request.
                    if response["sequence"] < sequence:
                        continue
                    expected = self._base() | {"status": "ok", "broker_instance": self.instance,
                        "nonce": nonce, "sequence": sequence, "node": state["old"], "generation": state["generation"],
                        "vm_id": self.cfg["vm_id"], "fenced": response.get("fenced")}
                    if (response != expected or type(response.get("fenced")) is not bool
                            or type(response.get("generation")) is not int or type(response.get("schema_version")) is not int
                            or time.monotonic() - started > limit or (self.expires is None and getattr(self,'_installed_runtime',False) is not True)
                    or (self.expires is not None and datetime.now(timezone.utc) >= self.expires)):
                        _deny()
                    return response["fenced"]
            except (OSError, ValueError, TypeError, queue.Empty):
                _deny()

    def fenced(self, state):
        return self._request("observe", state)

    def fence(self, state):
        try:
            if self._request("fence", state):
                return
        except TransitionBlocked:
            # Never repeat an ambiguous mutation. Reconcile only by a new live
            # observe nonce; process loss still blocks until a new guard session.
            if self.process and self.process.poll() is None and self.fenced(state):
                return
            raise
        _deny()
