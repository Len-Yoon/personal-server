"""Single-use challenge for an authenticated local Windows lease-proof pipe."""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import secrets
import stat
import subprocess
import sys
from threading import Lock, Thread
import time


SCOPE = "n100ha-legacy-closure-v1"
SHA = re.compile(r"[0-9a-f]{64}")
SERVICE_SID = re.compile(r"S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}")
PIPE = re.compile(r"n100ha-lease-proof-[0-9a-f]{32}")


def _require(condition):
    if not condition:
        raise ValueError("legacy_pipe_proof_rejected")


def _unique(items):
    fields = dict(items)
    _require(len(fields) == len(items))
    return fields


@dataclass(frozen=True)
class ProofPins:
    plan_sha256: str
    attempt_id: str
    generation: int
    windows_boot_sha256: str
    service_sid: str
    wsl_boot_sha256: str
    collector_sha256: str

    def challenge(self):
        values = {name: getattr(self, name) for name in (
            "plan_sha256", "attempt_id", "windows_boot_sha256", "wsl_boot_sha256", "collector_sha256"
        )}
        _require(all(type(value) is str and SHA.fullmatch(value) for value in values.values()))
        _require(type(self.generation) is int and 0 < self.generation < 2**63)
        _require(type(self.service_sid) is str and SERVICE_SID.fullmatch(self.service_sid))
        return dict(values, scope=SCOPE, nonce=secrets.token_hex(16), generation=self.generation,
                    service_sid=self.service_sid)


def verify_response(frame, challenge, deadline, *, key, monotonic=time.monotonic):
    """Authenticate the response before evaluating its bound closure facts."""
    _require(monotonic() < deadline and type(frame) is bytes and 0 < len(frame) <= 4096)
    _require(frame.endswith(b"\n") and frame.count(b"\n") == 1)
    _require(type(key) is bytearray and len(key) == 32)
    try:
        envelope = json.loads(frame.decode("ascii"), object_pairs_hook=_unique)
        _require(type(envelope) is dict and set(envelope) == {"body", "mac"})
        _require(type(envelope["body"]) is str and type(envelope["mac"]) is str
                 and SHA.fullmatch(envelope["mac"]))
        body = base64.b64decode(envelope["body"], validate=True)
        _require(0 < len(body) <= 2048 and hmac.compare_digest(
            envelope["mac"], hmac.digest(key, body, "sha256").hex()))
        response = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("legacy_pipe_proof_rejected") from None
    _require(type(response) is dict and set(response) == set(challenge) | {"closed", "observation_sha256"})
    _require(all(type(response[name]) is type(value) and response[name] == value
                 for name, value in challenge.items()))
    _require(response["closed"] is True and type(response["observation_sha256"]) is str
             and SHA.fullmatch(response["observation_sha256"]))
    _require(monotonic() < deadline)
    return response["observation_sha256"]


def _protected_path(path):
    _require(path.is_absolute())
    for entry in (path, *path.parents):
        info = entry.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 1024))
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return info


class PersistentLeaseProofClient:
    """One pinned native child, one ordered in-flight challenge, no automatic reconnect."""

    def __init__(self, *, pins, pipe_name, server_pid, key_path, key_sha256, powershell, client,
                 client_sha256, startup_timeout=15, monotonic=time.monotonic):
        _require(sys.platform == "win32")
        _require(type(pipe_name) is str and PIPE.fullmatch(pipe_name))
        _require(type(server_pid) is int and 0 < server_pid < 2**32)
        _require(type(startup_timeout) in (int, float) and 0 < startup_timeout <= 30)
        _require(type(client_sha256) is str and SHA.fullmatch(client_sha256))
        _require(type(key_sha256) is str and SHA.fullmatch(key_sha256))
        pins.challenge()  # Validate the fixed policy before launching native code.
        self.pins = pins
        self.monotonic = monotonic
        self._lock = Lock()
        self._incoming = Queue(maxsize=1)
        self._outgoing = Queue(maxsize=1)
        self._process = None
        self._key = bytearray(32)
        powershell, client, key_path = Path(powershell), Path(client), Path(key_path)
        try:
            _require(_protected_path(key_path).st_size == len(self._key))
            with key_path.open("rb", buffering=0) as stream:
                _require(stream.readinto(self._key) == len(self._key) and not stream.read(1))
            _require(hmac.compare_digest(hashlib.sha256(self._key).hexdigest(), key_sha256))
            _protected_path(powershell)
            _require(0 < _protected_path(client).st_size <= 16384)
            with client.open("rb") as stream:
                source = stream.read(16385)
            _require(len(source) <= 16384 and hashlib.sha256(source).hexdigest() == client_sha256)
            self._process = subprocess.Popen(
                [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                 "-File", str(client), "-PipeName", pipe_name, "-ServerPid", str(server_pid),
                 "-ServiceSid", pins.service_sid],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            Thread(target=self._read, args=(self._process.stdout,), daemon=True).start()
            Thread(target=self._write, args=(self._process.stdin,), daemon=True).start()
            _require(self._incoming.get(timeout=startup_timeout) == b"ready\r\n")
        except (OSError, ValueError, Empty):
            self.close()
            raise ValueError("legacy_pipe_proof_rejected") from None

    def _read(self, stream):
        try:
            while True:
                frame = stream.readline(4097)
                self._incoming.put_nowait(frame)
                if not frame or len(frame) > 4096:
                    break
        except (OSError, ValueError, Full):
            pass
        finally:
            stream.close()

    def _write(self, stream):
        try:
            while True:
                frame = self._outgoing.get()
                if frame is None:
                    break
                stream.write(frame)
                stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def request(self, deadline):
        """Return the fresh observation SHA using the original caller monotonic deadline."""
        _require(self._lock.acquire(blocking=False))
        try:
            _require(self._process is not None and self._process.poll() is None)
            deadline = min(deadline, self.monotonic() + 2)
            challenge = self.pins.challenge()
            raw = json.dumps(challenge, separators=(",", ":"), sort_keys=True).encode("ascii")
            budget = int((deadline - self.monotonic()) * 1000)
            _require(budget > 0)
            self._outgoing.put_nowait(str(budget).encode("ascii") + b" " + raw + b"\n")
            remaining = deadline - self.monotonic()
            _require(remaining > 0)
            frame = self._incoming.get(timeout=remaining)
            return verify_response(frame, challenge, deadline, key=self._key, monotonic=self.monotonic)
        except (OSError, ValueError, Empty, Full):
            self.close()
            raise ValueError("legacy_pipe_proof_rejected") from None
        finally:
            self._lock.release()

    def close(self):
        self._key[:] = bytes(len(self._key))
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            self._outgoing.put_nowait(None)
        except Full:
            pass
        # Reaping must not extend the caller's observation deadline.
        Thread(target=process.wait, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
