"""Bounded WSL client and Mac server protocol for a dedicated authenticated stdio channel.

The bridge is independent of SSH: the Mac dispatcher owns existing, pinned
guest SSH connections and must verify native results before returning a receipt.
Raw frozen data goes directly from WSL to the A guest over the separate mTLS
channel. No command, shell text, arbitrary paths, credentials, or artifacts are
accepted as bridge payloads. The receiver nonce is a one-attempt transfer input.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import stat
import threading
import time
from types import MappingProxyType


VERSION = 1
FRAME_BYTES = 4096
ACTIVATION_BOUNDARY_SECONDS = 2
MAX_REQUESTS = 64
MAX_SESSION_SECONDS = 3600
TRANSFER_BYTES = 8 * 1024**3
HEX = re.compile(r"[a-f0-9]{64}\Z")
NODES = ("A", "B")
VERBS = {
    # allowed nodes, maximum seconds, mutation, additional typed request fields
    "preflight": ("AB", 45, False, ("target_system_id_sha256",)),
    "begin_receive": ("A", 45, True, ("manifest_sha256", "fence_sha256", "transfer_nonce", "max_bytes")),
    "finish_receive": ("A", 615, False, ("manifest_sha256", "receiver_sha256", "transfer_proof_sha256")),
    "restore": ("A", 1845, True, ("manifest_sha256", "transfer_receipt_sha256", "target_receipt_sha256")),
    "prepare_runtime": ("AB", 295, True, ("manifest_sha256", "restore_receipt_sha256", "transfer_receipt_sha256",
                                          "target_receipt_sha256")),
    "verify_closed": ("AB", 45, False, ("manifest_sha256", "restore_receipt_sha256", "prepared_receipt_sha256")),
    "assert_activation_closed": ("A", 45, False, ("manifest_sha256", "restore_receipt_sha256",
                                                  "closed_a_sha256", "closed_b_sha256")),
    "activate": ("A", 75, True, ("manifest_sha256", "activation_check_sha256", "settlement_sha256")),
    "assert_live": ("A", 30, False, ("authority_sha256", "windows_generation", "windows_generation_sha256",
                                    "live_lease_sha256", "live_authority_sha256")),
    "observe_restore": ("A", 60, False, ("manifest_sha256", "restore_intent_sha256")),
    "observe_rollback": ("AB", 60, False, ("baseline_sha256", "manifest_sha256", "restore_attempted")),
}
OBSERVE = {"observe_restore", "observe_rollback"}
OBSERVATIONS = {"CLEAN", "CLEAN_FAILED", "COMMITTED_CLOSED", "UNKNOWN"}
MUTATIONS = {verb for verb, (_, _, mutates, _) in VERBS.items() if mutates}


class BridgeRejected(RuntimeError):
    pass


class OutcomeUnknown(BridgeRejected):
    pass


class _End(BridgeRejected):
    pass


def _need(condition, code):
    if not condition:
        raise BridgeRejected(code)


def _hash(value):
    return type(value) is str and HEX.fullmatch(value) is not None


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
                      sort_keys=True).encode("ascii") + b"\n"


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _matches(actual, expected):
    return type(actual) is dict and actual.keys() == expected.keys() and all(
        type(actual[key]) is type(value) and actual[key] == value for key, value in expected.items()
    )


def _pairs(items):
    result = dict(items)
    _need(len(result) == len(items), "bridge_duplicate_fields")
    return result


def _reject_constant(_):
    raise BridgeRejected("bridge_invalid_constant")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _need(remaining > 0, "bridge_deadline_exceeded")
    return remaining


@dataclass(frozen=True)
class Binding:
    plan_sha256: str
    attempt_id: str
    source_sha256: str
    boot_a_sha256: str
    boot_b_sha256: str

    def fields(self):
        fields = dict(vars(self))
        _need(all(_hash(value) for value in fields.values()) and self.boot_a_sha256 != self.boot_b_sha256,
              "bridge_binding_rejected")
        return fields

    def boot(self, node):
        _need(node in NODES, "bridge_node_rejected")
        return self.boot_a_sha256 if node == "A" else self.boot_b_sha256


@dataclass(frozen=True)
class Request:
    binding: Binding
    node: str
    verb: str
    sequence: int
    nonce: str
    args: object


@dataclass(frozen=True)
class Receipt:
    status: str
    receipt_sha256: str | None = None
    service_instance_sha256: str | None = None

    def fields(self, verb):
        allowed = OBSERVATIONS if verb in OBSERVE else {"PASS", "UNKNOWN"}
        _need(type(self.status) is str and self.status in allowed
              and ((self.receipt_sha256 is None) if self.status == "UNKNOWN" else _hash(self.receipt_sha256)),
              "bridge_result_rejected")
        fields = {"status": self.status, "receipt_sha256": self.receipt_sha256}
        if self.service_instance_sha256 is not None:
            _need(verb == "activate" and self.status == "PASS" and _hash(self.service_instance_sha256),
                  "bridge_result_rejected")
            fields["service_instance_sha256"] = self.service_instance_sha256
        return fields


class Stdio:
    """Borrow dedicated pipe descriptors. The owner closes them after the session."""

    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.buffer = bytearray()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)

    def read(self, deadline):
        while True:
            _remaining(deadline)
            end = self.buffer.find(b"\n")
            if end >= 0:
                raw = bytes(self.buffer[:end + 1])
                del self.buffer[:end + 1]
                try:
                    value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
                    _need(type(value) is dict and _bytes(value) == raw, "bridge_noncanonical_frame")
                except (ValueError, UnicodeError, RecursionError):
                    raise BridgeRejected("bridge_noncanonical_frame") from None
                return value
            _need(len(self.buffer) < FRAME_BYTES, "bridge_frame_limit")
            readable, _, _ = select.select([self.read_fd], [], [], _remaining(deadline))
            _need(readable, "bridge_deadline_exceeded")
            try:
                data = os.read(self.read_fd, FRAME_BYTES - len(self.buffer))
            except BlockingIOError:
                continue
            if not data:
                if self.buffer:
                    raise BridgeRejected("bridge_truncated_frame")
                raise _End("bridge_channel_closed")
            self.buffer.extend(data)

    def write(self, value, deadline):
        raw = _bytes(value)
        _need(0 < len(raw) <= FRAME_BYTES, "bridge_frame_limit")
        remaining = memoryview(raw)
        while remaining:
            _, writable, _ = select.select([], [self.write_fd], [], _remaining(deadline))
            _need(writable, "bridge_deadline_exceeded")
            try:
                written = os.write(self.write_fd, remaining)
            except BlockingIOError:
                continue
            _need(written > 0, "bridge_channel_closed")
            remaining = remaining[written:]


def _args(verb, node, args):
    _need(type(verb) is str and verb in VERBS and type(node) is str and node in NODES,
          "bridge_verb_rejected")
    nodes, _, _, keys = VERBS[verb]
    _need(node in nodes and type(args) is dict and set(args) == set(keys), "bridge_args_rejected")
    for key, value in args.items():
        if key == "max_bytes":
            valid = type(value) is int and 0 < value <= TRANSFER_BYTES
        elif key == "windows_generation":
            valid = type(value) is int and 0 < value < 2**63
        elif key == "restore_attempted":
            valid = type(value) is bool
        elif key == "manifest_sha256" and verb == "observe_rollback" and args["restore_attempted"] is False:
            valid = value is None or _hash(value)
        else:
            valid = _hash(value)
        _need(valid, "bridge_args_rejected")


class _Chain:
    def __init__(self, mode):
        _need(mode in ("cutover", "reconcile"), "bridge_mode_rejected")
        self.mode = mode
        self.results = {}
        self.manifest = None
        self.target_system = None
        self.windows_generation = None
        self.live_authorities = set()
        self.recovery = False

    def check(self, verb, node, args):
        _args(verb, node, args)
        if self.mode == "reconcile":
            _need(verb in OBSERVE, "bridge_reconciliation_read_only")
            return
        if verb in OBSERVE:
            _need(("activate", "A") not in self.results, "bridge_forward_recovery_required")
            self.recovery = True
            return
        _need(not self.recovery and ((verb, node) not in self.results or verb == "assert_live"),
              "bridge_out_of_order")
        if "manifest_sha256" in args and verb != "begin_receive":
            _need(args["manifest_sha256"] == self.manifest, "bridge_manifest_unbound")

        def require(stage, target, key=None):
            receipt = self.results.get((stage, target))
            _need(receipt is not None and (key is None or args[key] == receipt), "bridge_prior_receipt_unbound")

        if verb == "preflight":
            _need(self.target_system in (None, args["target_system_id_sha256"]), "bridge_target_unbound")
        elif verb == "begin_receive":
            require("preflight", "A")
            require("preflight", "B")
        elif verb == "finish_receive":
            require("begin_receive", "A", "receiver_sha256")
        elif verb == "restore":
            require("finish_receive", "A", "transfer_receipt_sha256")
            require("preflight", "A", "target_receipt_sha256")
        elif verb == "prepare_runtime":
            require("restore", "A", "restore_receipt_sha256")
            require("finish_receive", "A", "transfer_receipt_sha256")
            require("preflight", node, "target_receipt_sha256")
        elif verb == "verify_closed":
            require("restore", "A", "restore_receipt_sha256")
            require("prepare_runtime", node, "prepared_receipt_sha256")
        elif verb == "assert_activation_closed":
            require("restore", "A", "restore_receipt_sha256")
            require("verify_closed", "A", "closed_a_sha256")
            require("verify_closed", "B", "closed_b_sha256")
        elif verb == "activate":
            require("assert_activation_closed", "A", "activation_check_sha256")
        elif verb == "assert_live":
            require("activate", "A", "authority_sha256")
            _need(self.windows_generation in (None, args["windows_generation"])
                  and args["live_authority_sha256"] not in self.live_authorities, "bridge_live_authority_unbound")

    def record(self, verb, node, args, result):
        if result.status == "PASS":
            if verb == "preflight":
                self.target_system = args["target_system_id_sha256"]
            elif verb == "begin_receive":
                self.manifest = args["manifest_sha256"]
            elif verb == "assert_live":
                self.windows_generation = args["windows_generation"]
                self.live_authorities.add(args["live_authority_sha256"])
            self.results[(verb, node)] = result.receipt_sha256


class MutationJournal:
    """Mac-local private, durable one-shot gate. Reconciliation never writes it."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _open(self):
        path = self.directory
        _need(path.is_absolute() and ".." not in path.parts, "bridge_journal_directory_rejected")
        for parent in path.parents:
            info = parent.lstat()
            _need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid())
                  and (not info.st_mode & 0o022 or info.st_uid == 0 and info.st_mode & stat.S_ISVTX),
                  "bridge_journal_directory_rejected")
        info = path.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
              "bridge_journal_directory_rejected")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        observed = os.fstat(fd)
        if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise BridgeRejected("bridge_journal_directory_changed")
        return fd

    @staticmethod
    def _name(binding, node, verb):
        return f"{binding.attempt_id}-{node}-{verb}.intent"

    def assert_unattempted(self, binding):
        fd = self._open()
        try:
            for verb in MUTATIONS:
                for node in VERBS[verb][0]:
                    try:
                        os.stat(self._name(binding, node, verb), dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    raise BridgeRejected("bridge_reconciliation_required")
        finally:
            os.close(fd)

    def reserve(self, request, wire):
        fd = self._open()
        try:
            name = self._name(request.binding, request.node, request.verb)
            try:
                out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            except FileExistsError:
                raise BridgeRejected("bridge_reconciliation_required") from None
            with os.fdopen(out, "wb") as stream:
                stream.write(_bytes({"status": "INTENT", "verb": request.verb, "node": request.node,
                                     **request.binding.fields(), "request_sha256": _digest(_bytes(wire))}))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
        finally:
            os.close(fd)


class _Session:
    def __init__(self, channel, binding, mode, seconds):
        _need(type(seconds) in (float, int) and 0 < seconds <= MAX_SESSION_SECONDS, "bridge_deadline_rejected")
        self.channel = channel
        self.binding = binding
        self.chain = _Chain(mode)
        self.context = {"version": VERSION, "mode": mode, **binding.fields()}
        self.deadline = time.monotonic() + seconds
        self.session = None
        self.sequence = 0
        self.poisoned = False

    def envelope(self, kind, sequence, nonce, node, verb):
        return {"kind": kind, "session": self.session, "sequence": sequence, "nonce": nonce,
                "node": node, "boot_sha256": self.binding.boot(node), "verb": verb, **self.context}

    def limit(self, seconds):
        return min(time.monotonic() + seconds, self.deadline)


class Client(_Session):
    """WSL side. A poisoned client cannot issue another request."""

    def __init__(self, channel, binding, *, mode="cutover", seconds=MAX_SESSION_SECONDS):
        super().__init__(channel, binding, mode, seconds)
        deadline = self.limit(12)
        client_nonce = secrets.token_hex(32)
        channel.write({"kind": "open", "client_nonce": client_nonce, **self.context}, deadline)
        answer = channel.read(deadline)
        server_nonce = answer.get("server_nonce")
        _need(_hash(server_nonce) and server_nonce != client_nonce
              and _matches(answer, {"kind": "opened", "client_nonce": client_nonce,
                                    "server_nonce": server_nonce, **self.context}), "bridge_handshake_rejected")
        self.session = _digest(_bytes({"client_nonce": client_nonce, "server_nonce": server_nonce, **self.context}))

    def call(self, verb, node, /, **args):
        _need(not self.poisoned and self.sequence < MAX_REQUESTS, "bridge_session_unavailable")
        self.chain.check(verb, node, args)
        self.sequence += 1
        nonce = secrets.token_hex(32)
        deadline = self.limit(VERBS[verb][1])
        envelope = self.envelope("request", self.sequence, nonce, node, verb)
        try:
            self.channel.write({**envelope, "args": args}, deadline)
            answer = self.channel.read(deadline)
            result = Receipt(answer.get("status"), answer.get("receipt_sha256"), answer.get("service_instance_sha256"))
            expected = self.envelope("response", self.sequence, nonce, node, verb) | result.fields(verb)
            _need(_matches(answer, expected), "bridge_response_unbound")
            if result.status == "UNKNOWN" and verb not in OBSERVE:
                raise OutcomeUnknown("bridge_outcome_unconfirmed")
            self.chain.record(verb, node, args, result)
            return result
        except (BridgeRejected, OSError, ValueError, TypeError):
            self.poisoned = True
            if verb in MUTATIONS:
                raise OutcomeUnknown("bridge_mutation_outcome_unconfirmed") from None
            raise BridgeRejected("bridge_observation_unconfirmed") from None


def _dispatch_bounded(dispatch, request, deadline):
    _remaining(deadline)
    results = queue.Queue(maxsize=1)

    def work():
        try:
            _remaining(deadline)
            result = dispatch(request, deadline)
        except Exception:
            result = Receipt("UNKNOWN")
        results.put(result)

    threading.Thread(target=work, daemon=True).start()
    try:
        result = results.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise OutcomeUnknown("bridge_dispatch_deadline_exceeded") from None
    _need(type(result) is Receipt, "bridge_result_rejected")
    result.fields(request.verb)
    return result


class Server(_Session):
    """Mac side. Dispatcher uses fixed native handlers and a bounded guest SSH timeout."""

    def __init__(self, channel, binding, journal, dispatch, *, mode="cutover", seconds=MAX_SESSION_SECONDS):
        super().__init__(channel, binding, mode, seconds)
        _need(callable(dispatch) and isinstance(journal, MutationJournal), "bridge_dispatch_unbound")
        self.journal, self.dispatch = journal, dispatch
        self.nonces = set()

    def serve(self):
        _need(self.session is None and not self.poisoned, "bridge_session_unavailable")
        try:
            self._serve()
        finally:
            self.poisoned = True

    def _serve(self):
        deadline = self.limit(12)
        opening = self.channel.read(deadline)
        client_nonce = opening.get("client_nonce")
        _need(_hash(client_nonce)
              and _matches(opening, {"kind": "open", "client_nonce": client_nonce, **self.context}),
              "bridge_handshake_rejected")
        if self.chain.mode == "cutover":
            self.journal.assert_unattempted(self.binding)
        server_nonce = secrets.token_hex(32)
        self.channel.write({"kind": "opened", "client_nonce": client_nonce,
                            "server_nonce": server_nonce, **self.context}, deadline)
        self.session = _digest(_bytes({"client_nonce": client_nonce, "server_nonce": server_nonce, **self.context}))
        while self.sequence < MAX_REQUESTS:
            try:
                wire = self.channel.read(self.deadline)
            except _End:
                return
            sequence, nonce, node, verb = self.sequence + 1, wire.get("nonce"), wire.get("node"), wire.get("verb")
            args = wire.get("args")
            self.chain.check(verb, node, args)
            expected = self.envelope("request", sequence, nonce, node, verb) | {"args": args}
            _need(_hash(nonce) and nonce not in self.nonces and _matches(wire, expected), "bridge_request_unbound")
            self.sequence = sequence
            self.nonces.add(nonce)
            request = Request(self.binding, node, verb, sequence, nonce, MappingProxyType(dict(args)))
            deadline = self.limit(VERBS[verb][1])
            _remaining(deadline)
            if verb in MUTATIONS:
                self.journal.reserve(request, wire)
            result = _dispatch_bounded(self.dispatch, request, deadline)
            self.channel.write(self.envelope("response", sequence, nonce, node, verb) | result.fields(verb), deadline)
            if result.status == "UNKNOWN" and verb not in OBSERVE:
                raise OutcomeUnknown("bridge_outcome_unconfirmed")
            self.chain.record(verb, node, args, result)
