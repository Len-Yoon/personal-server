"""Retire one archived guest observer, with separately gated, one-shot rollback.

This module never touches the Windows writer. Its caller holds the guest mutation
lock and supplies a fresh operation-specific proof at every boundary. Retirement
proves the exact old writer, stopped apps, protected inventory, and no successor;
stopping its provider lets the old writer perform its own revocation. The caller
must separately observe its native terminal and stopped/disabled service, then
prove closed A/B state. Restoration requires that closed pair and no successor.
A timeout
is reconciled by observation, never by dispatching the same mutation again.
"""

import argparse
import ast
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from . import native_guest_runtime as runtime, native_stdio_bridge as wire
from .legacy_writers import TransitionBlocked, _need

ERROR = "legacy_provider_unconfirmed"
UNSETTLED = "legacy_provider_reconciliation_required"
RESTORE_LISTENER_TIMEOUT = 5.0
UNIT = re.compile(r"n100ha-(recovery|postboot)-observer-[a-zA-Z0-9_-]{1,128}\.service\Z")
TRANSIENT = Path("/run/systemd/transient")
GUEST_ROOT = Path("/var/lib/n100-ha/cutover")
PROPERTIES = (
    "Id", "MainPID", "ActiveState", "SubState", "FragmentPath", "Transient",
    "Restart", "NoNewPrivileges", "PrivateTmp", "User", "DropInPaths",
    "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "OnFailure",
    "OnSuccess", "PropagatesStopTo", "BoundBy", "ConsistsOf", "Triggers",
)
EMPTY = ("DropInPaths", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost",
         "OnFailure", "OnSuccess", "PropagatesStopTo", "BoundBy", "ConsistsOf", "Triggers")
EXEC_HOOKS = frozenset({"ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"})


def _command(argv, *, timeout=8):
    completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                               env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    _need(completed.returncode == 0, ERROR)
    return completed.stdout


def _properties(unit):
    raw = _command(("/usr/bin/systemctl", "show", unit, "--no-pager", "--all",
                    "--property=" + ",".join(PROPERTIES)))
    lines = raw.decode("utf-8").splitlines()
    _need(all("=" in line for line in lines), ERROR)
    result = dict(line.split("=", 1) for line in lines)
    missing = set(PROPERTIES) - set(result)
    _need(len(result) == len(lines) and set(result) <= set(PROPERTIES) and missing <= EXEC_HOOKS, ERROR)
    # The target manager omits undefined exec-command arrays even with --all.
    # Normalize only those four undefined hooks. _live additionally requires
    # an empty explicit DropInPaths and a verified fragment whose strict parser
    # rejects every exec hook directive; configured hooks remain nonempty/deny.
    result.update({name: "" for name in missing})
    return result


def _listeners(*, timeout=8):
    raw = _command(("/usr/bin/ss", "-l", "-H", "-n", "-A", "vsock", "-p"), timeout=timeout)
    result = {}
    for line in raw.decode("utf-8").splitlines():
        fields = line.split()
        _need("LISTEN" in fields and len(fields) > fields.index("LISTEN") + 3, ERROR)
        port = fields[fields.index("LISTEN") + 3].rsplit(":", 1)[-1]
        _need(port.isdecimal(), ERROR)
        result.setdefault(int(port), []).append(line)
    return result


def _profile(raw):
    """Only the minimal original observer profile is eligible for reconstruction.

    Unknown directives are rejected instead of silently dropping resource,
    dependency, security, environment, or stop semantics during rollback.
    """
    section = None
    seen = {}
    allowed = {"Unit": {"Description", "CollectMode"}, "Service": {
        "ExecStart", "User", "Restart", "NoNewPrivileges", "PrivateTmp", "Type"}}
    for line in raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            _need(line in ("[Unit]", "[Service]"), ERROR)
            section = line[1:-1]
            continue
        _need(section in allowed and "=" in line and not line.endswith("\\"), ERROR)
        key, value = line.split("=", 1)
        _need(key in allowed[section], ERROR)
        # systemd-run can clear ExecStart before its one actual command.
        _need((section, key) not in seen or (key == "ExecStart" and seen[(section, key)] == ""), ERROR)
        seen[(section, key)] = value
    required = {("Service", "User"): "root", ("Service", "Restart"): "no",
                ("Service", "NoNewPrivileges"): "yes", ("Service", "PrivateTmp"): "yes"}
    _need(all(seen.get(key) == value for key, value in required.items())
          and bool(seen.get(("Service", "ExecStart")))
          and seen.get(("Service", "Type"), "simple") in ("simple", "exec")
          and seen.get(("Unit", "CollectMode"), "inactive") in ("inactive", "inactive-or-failed"), ERROR)
    description = seen.get(("Unit", "Description"))
    _need(description is None or (description and "\x00" not in description), ERROR)
    return seen


def _argv(raw, *, role, port, hold_sha256, boot_sha256):
    _need(raw.endswith(b"\0") and b"\0\0" not in raw, ERROR)
    argv = raw[:-1].decode("utf-8").split("\0")
    _need(role in {"provider", "held"} and type(port) is int and 1 <= port < 1024
          and wire._hash(hold_sha256) and wire._hash(boot_sha256)
          and len(argv) == (11 if role == "provider" else 13)
          and argv[:5] == ["/usr/bin/python3", "-I", "-B", "-S", "-c"], ERROR)
    # Compilation validates the archived inline observer without executing it.
    try:
        compile(ast.parse(argv[5]), "archived_observer", "exec")
    except (SyntaxError, ValueError, TypeError):
        # Never include the protected inline observer source in an exception.
        raise TransitionBlocked(ERROR) from None
    tail = argv[6:]
    _need(tail[2:5] == [str(port), hold_sha256, boot_sha256]
          and wire._hash(tail[1]) and all(wire._hash(value) for value in tail[5:]), ERROR)
    source = Path(tail[0])
    _need(source.is_absolute() and ".." not in source.parts and source.is_relative_to(GUEST_ROOT)
          and source.name == "native-guest-recovery-observer.py", ERROR)
    # The original inline loader needs this exact source during rollback.
    # runtime._read verifies every parent, root ownership, mode, no links,
    # descriptor/path identity, and stable bytes before the hash is checked.
    _need(wire._digest(runtime._read(source, 0, {0o400}, limit=131072)) == tail[1], ERROR)
    return argv


class ProviderRetirement:
    def __init__(self, stage, binding, *, latest_intent_sha256, rollback_sha256,
                 hold_sha256, provider_port, held_port):
        self.stage = Path(stage)
        self.binding = binding
        self.scope = binding.fields()
        _need(wire._hash(latest_intent_sha256) and wire._hash(rollback_sha256) and wire._hash(hold_sha256)
              and type(provider_port) is int and type(held_port) is int
              and 1 <= provider_port < 1024 and 1 <= held_port < 1024 and provider_port != held_port, ERROR)
        self.intent = latest_intent_sha256
        self.rollback = rollback_sha256
        self.hold = hold_sha256
        self.ports = {"provider": provider_port, "held": held_port}

    def _archive(self):
        _need(os.geteuid() == 0 and wire._digest(runtime.BOOT_ID.read_bytes()) == self.binding.boot_a_sha256, ERROR)
        runtime._directory(self.stage, 0)
        raw = runtime._read(self.stage / ("provider-rollback-" + self.intent + ".json"), 0, {0o400}, limit=262144)
        _need(wire._digest(raw) == self.rollback, ERROR)
        document = json.loads(raw)
        _need(set(document) == {"scope", "latest_intent", "units"}
              and document["scope"] == self.scope and document["latest_intent"] == self.intent
              and type(document["units"]) is dict and set(document["units"]) == {"provider", "held"}, ERROR)
        for role, entry in document["units"].items():
            _need(type(entry) is dict and set(entry) == {
                "unit", "fragment", "argv", "fragment_sha256", "argv_sha256", "scope"}
                and type(entry["unit"]) is str and UNIT.fullmatch(entry["unit"])
                and entry["unit"].startswith("n100ha-" + ("recovery" if role == "provider" else "postboot") + "-observer-")
                and entry["scope"] == "exact-original-observer-profile", ERROR)
            for name in ("fragment", "argv"):
                _need(type(entry[name]) is str and len(entry[name]) <= 262144, ERROR)
                raw = bytes.fromhex(entry[name])
                _need(raw and wire._digest(raw) == entry[name + "_sha256"], ERROR)
            _profile(bytes.fromhex(entry["fragment"]))
            _argv(bytes.fromhex(entry["argv"]), role=role, port=self.ports[role],
                  hold_sha256=self.hold, boot_sha256=self.binding.boot_a_sha256)
        return document["units"]

    def _live(self, entry, role, *, original=True, wait_for_listener=False):
        if wait_for_listener:
            # A simple transient unit can be started before Python binds its
            # socket. Observe readiness without issuing another start command.
            deadline = time.monotonic() + RESTORE_LISTENER_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                _need(remaining > 0, UNSETTLED)
                try:
                    ready = self.ports[role] in _listeners(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise TransitionBlocked(UNSETTLED) from None
                remaining = deadline - time.monotonic()
                _need(remaining > 0, UNSETTLED)
                if ready:
                    break
                time.sleep(min(0.1, remaining))
        properties = _properties(entry["unit"])
        _need(properties["Id"] == entry["unit"] and properties["ActiveState"] == "active"
              and properties["SubState"] == "running" and properties["Transient"] == "yes"
              and properties["FragmentPath"] == str(TRANSIENT / entry["unit"])
              and all(properties[name] == "" for name in EMPTY)
              and all(properties[name] == value for name, value in {
                  "Restart": "no", "NoNewPrivileges": "yes", "PrivateTmp": "yes", "User": "root"}.items()), ERROR)
        pid = int(properties["MainPID"])
        _need(pid > 1, ERROR)
        process = Path("/proc") / str(pid)
        _need(process.stat().st_uid == 0 and (process / "cmdline").read_bytes() == bytes.fromhex(entry["argv"])
              and (process / "exe").resolve() == Path("/usr/bin/python3").resolve(), ERROR)
        fragment = runtime._read(TRANSIENT / entry["unit"], 0, {0o644, 0o640, 0o600}, limit=131072)
        if original:
            _need(wire._digest(fragment) == entry["fragment_sha256"], ERROR)
        _profile(fragment)
        sockets = _listeners()
        rows = sockets.get(self.ports[role], [])
        _need(len(rows) == 1 and re.findall(r"pid=([0-9]+),", rows[0]) == [str(pid)]
              and sum(bool(re.search(r"pid=" + str(pid) + r",", row))
                      for values in sockets.values() for row in values) == 1, ERROR)
        _need(_properties(entry["unit"]) == properties, ERROR)
        return {"pid": pid, "fragment_sha256": wire._digest(fragment), "argv_sha256": entry["argv_sha256"]}

    def _absent(self, entry):
        properties = _properties(entry["unit"])
        _need(properties["Id"] == entry["unit"] and properties["MainPID"] == "0"
              and properties["ActiveState"] == "inactive" and properties["SubState"] == "dead"
              and self.ports["provider"] not in _listeners(), UNSETTLED)

    def _paths(self, operation):
        prefix = "provider-" + self.rollback + "-" + operation
        return self.stage / (prefix + "-intent.json"), self.stage / (prefix + "-done.json")

    def _gate(self, operation, boundary, assert_safe):
        _need(callable(assert_safe), ERROR)
        self._archive()
        frame = {"scope": self.scope, "latest_intent": self.intent, "rollback_sha256": self.rollback,
                 "hold_sha256": self.hold,
                 "operation": operation, "boundary": boundary}
        challenge = wire._digest(wire._bytes(frame | {"nonce": secrets.token_hex(32)}))
        proof = assert_safe(challenge, boundary)
        _need(wire._hash(proof), ERROR)
        self._archive()
        return frame | {"safe_challenge_sha256": challenge, "safe_proof_sha256": proof}

    def retire(self, assert_safe):
        """Stop only the archived provider, leaving the held observer running.

        assert_safe freshly checks stopped applications, protected inventory,
        original writer lineage, and no successor writer. Before/dispatch
        accepts either the exact running old writer awaiting provider loss or
        an authenticated cooperative stop and fresh closed A/B proof. After
        dispatch the same lineage is verified separately from provider state.
        Caller holds the guest mutation lock for this entire call.
        """
        return self._operate("retire", assert_safe)

    def restore(self, assert_safe):
        """Restore only the same provider after a separate safe rollback proof.

        The callback additionally proves no replacement writer was started and
        no target writes reopened; retirement does not itself grant rollback.
        """
        return self._operate("restore", assert_safe)

    def observe(self):
        """Read-only prerequisite check; does not publish intent or receipt."""
        units = self._archive()
        return {"rollback_sha256": self.rollback,
                "provider": self._live(units["provider"], "provider"),
                "held": self._live(units["held"], "held")}

    def _operate(self, operation, assert_safe):
        units = self._archive()
        provider, held = units["provider"], units["held"]
        intent, done = self._paths(operation)
        frame = self._gate(operation, "before", assert_safe)
        held_before = self._live(held, "held")
        if operation == "restore":
            # Restoration requires the exact retirement receipt, never merely
            # a missing socket following an unrelated provider failure.
            self._receipt("retire")
        if os.path.lexists(done):
            receipt = self._receipt(operation)
            record = json.loads(runtime._read(done, 0, {0o400}, limit=131072))
            _need(record["held"] == held_before, ERROR)
            if operation == "retire":
                self._absent(provider)
            else:
                _need(self._live(provider, "provider", original=False) == record["provider"], ERROR)
            self._gate(operation, "after", assert_safe)
            return wire.Receipt("PASS", receipt)
        if not os.path.lexists(intent):
            if operation == "retire":
                initial = self._live(provider, "provider")
                # Do not stop providers after rollback: this is one retirement.
                _need(not any(os.path.lexists(path) for path in self._paths("restore")), ERROR)
            else:
                self._absent(provider)
                initial = None
            dispatch = self._gate(operation, "dispatch", assert_safe)
            runtime._write(intent, wire._bytes(frame | {"initial": initial, "held": held_before,
                "dispatch_proof_sha256": dispatch["safe_proof_sha256"], "automatic_retry_permitted": False}), mode=0o400)
            # Intent is durable before dispatch. An uncertain response can only
            # be resolved through the direct state checks below or a later call.
            try:
                if operation == "retire":
                    _command(("/usr/bin/systemctl", "stop", provider["unit"]))
                else:
                    profile = _profile(bytes.fromhex(provider["fragment"]))
                    arguments = ["/usr/bin/systemd-run", "--quiet", "--expand-environment=no", "--unit=" + provider["unit"],
                        "--property=User=root", "--property=Restart=no", "--property=NoNewPrivileges=yes",
                        "--property=PrivateTmp=yes", "--property=Type=" + profile.get(("Service", "Type"), "simple"),
                        "--property=CollectMode=" + profile.get(("Unit", "CollectMode"), "inactive")]
                    if ("Unit", "Description") in profile:
                        arguments.append("--description=" + profile[("Unit", "Description")])
                    # systemd-run passes this argv through its typed D-Bus
                    # property and escapes percent specifiers when serializing
                    # the transient file. Do not apply shell/unit-file escaping.
                    _command((*arguments, "--", *_argv(bytes.fromhex(provider["argv"]), role="provider",
                        port=self.ports["provider"], hold_sha256=self.hold, boot_sha256=self.binding.boot_a_sha256)))
            except (TransitionBlocked, OSError, subprocess.TimeoutExpired):
                pass
        original_intent = json.loads(runtime._read(intent, 0, {0o400}, limit=131072))
        expected_frame = {key: value for key, value in frame.items()
                          if key not in {"safe_proof_sha256", "safe_challenge_sha256"}}
        _need(all(original_intent.get(key) == value for key, value in expected_frame.items())
              and original_intent.get("automatic_retry_permitted") is False
              and original_intent.get("held") == held_before, ERROR)
        if operation == "retire":
            self._absent(provider)
            observed = None
        else:
            observed = self._live(provider, "provider", original=False, wait_for_listener=True)
        final = self._gate(operation, "after", assert_safe)
        _need(self._live(held, "held") == held_before, ERROR)
        document = final | {"intent_sha256": wire._digest(runtime._read(intent, 0, {0o400}, limit=131072)),
            "provider": observed, "held": held_before, "automatic_retry_permitted": False}
        runtime._write(done, wire._bytes(document), mode=0o400)
        return wire.Receipt("PASS", self._receipt(operation))

    def _receipt(self, operation):
        intent, done = self._paths(operation)
        raw = runtime._read(done, 0, {0o400}, limit=131072)
        record = json.loads(raw)
        _need(record.get("scope") == self.scope and record.get("latest_intent") == self.intent
              and record.get("rollback_sha256") == self.rollback and record.get("hold_sha256") == self.hold
              and record.get("operation") == operation
              and record.get("boundary") == "after" and wire._hash(record.get("safe_proof_sha256"))
              and wire._hash(record.get("safe_challenge_sha256"))
              and record.get("automatic_retry_permitted") is False
              and record.get("intent_sha256") == wire._digest(runtime._read(intent, 0, {0o400}, limit=131072)), ERROR)
        return wire._digest(raw)


def main(argv=None):
    """Protected read-only entry point; mutation gates stay in the coordinator.

    Invoke this module only from the reviewed root-owned source package. Config
    is a root-owned 0400 file with an independently pinned hash. There is no
    command-line flag that substitutes a digest or Boolean for the fresh
    operation-specific proof callback needed by retire()/restore().
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("observe",))
    parser.add_argument("--config", required=True)
    parser.add_argument("--config-sha256", required=True)
    args = parser.parse_args(argv)
    _need(os.geteuid() == 0 and wire._hash(args.config_sha256), ERROR)
    raw = runtime._read(Path(args.config), 0, {0o400}, limit=131072)
    _need(wire._digest(raw) == args.config_sha256, ERROR)
    document = json.loads(raw)
    _need(type(document) is dict and set(document) == {
        "binding", "latest_intent_sha256", "rollback_sha256", "hold_sha256", "provider_port", "held_port"}, ERROR)
    binding = wire.Binding(**document.pop("binding"))
    from . import native_guest_worker as worker
    adapter = ProviderRetirement(worker.ROOT / binding.attempt_id, binding, **document)
    with worker._guest_mutation_descriptor():
        observed = adapter.observe()
    print(json.dumps({"status": "OBSERVED", "rollback_sha256": adapter.rollback,
                      "observation_sha256": wire._digest(wire._bytes(observed))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
