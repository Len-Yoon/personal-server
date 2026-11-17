"""Witness exact ingress mutations without relaxing legacy writer exclusion."""

from contextlib import contextmanager
import copy
import fcntl
import os
from pathlib import Path
import re
import uuid

from ha_runtime import seven_capture
from ha_runtime.seven_migration import MigrationRejected

from . import caddy_runtime as caddy
from .controller import TransitionBlocked, exclusive


def _read(reader, path):
    descriptor = seven_capture.descriptor(path)
    return seven_capture.parse(reader.read(descriptor, limit=8192)), descriptor["sha256"]


def _scope(spec):
    caddy._need(type(spec) is dict and set(spec) == {"directory", "plan_sha256", "attempt_id", "coordinator", "project"}
                and all(type(spec[key]) is str and caddy.ID.fullmatch(spec[key]) for key in ("plan_sha256", "attempt_id"))
                and re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", spec["project"]), "ingress_execution_unbound")
    return caddy._private_dir(Path(spec["directory"]))


def config_override(spec, project):
    try:
        directory = _scope(spec)
        if os.fsencode(spec["project"]) == project:
            return os.fsencode(directory / "main" / "caddy-ha-compose.json")
    except (OSError, ValueError, TypeError, KeyError, TransitionBlocked):
        return None
    return None


def _observer_paths(runtime_path, legacy_attempt, spec):
    directory = _scope(spec)
    caddy._need(type(legacy_attempt) is str and caddy.ID.fullmatch(legacy_attempt)
                and spec["attempt_id"] != legacy_attempt
                and runtime_path == directory.parent / "runtime.json"
                and directory.name == "ingress" and directory.parent.name == spec["attempt_id"],
                "ingress_observer_runtime_unbound")
    return directory.parent.parent / legacy_attempt / "runtime.json"


def _observer_runtime(reader, previous_path, previous_sha256, legacy_plan, legacy_attempt, spec):
    caddy._need(type(previous_sha256) is str and caddy.ID.fullmatch(previous_sha256)
                and type(legacy_plan) is str and caddy.ID.fullmatch(legacy_plan), "ingress_observer_runtime_unbound")
    descriptor = {"path": str(previous_path), "sha256": previous_sha256,
                  "bytes": reader.watch(previous_path).stat().st_size}
    previous = seven_capture.parse(reader.read(descriptor))
    caddy._need(type(previous) is dict and type(previous["schema_version"]) is int and previous["schema_version"] == 1
                and (previous["plan_sha256"], previous["attempt_id"]) == (legacy_plan, legacy_attempt)
                and isinstance(previous["writer_baseline"], dict) and "ingress_execution" not in previous
                and "legacy_runtime_sha256" not in previous, "ingress_observer_runtime_unbound")
    # Copy the closed-writer baseline. The old coordinator's command exception
    # must not follow the new process, although the legacy fence stays bound.
    return {key: value for key, value in previous.items() if key != "coordinator"} | {
        "ingress_execution": spec, "legacy_runtime_sha256": previous_sha256,
    }


def assert_published_runtime(reader, path, runtime, legacy_plan, legacy_attempt):
    spec = runtime["ingress_execution"]
    previous_path = _observer_paths(path, legacy_attempt, spec)
    expected = _observer_runtime(reader, previous_path, runtime["legacy_runtime_sha256"], legacy_plan, legacy_attempt, spec)
    caddy._need(seven_capture.encoded(expected) == seven_capture.encoded(runtime), "ingress_observer_runtime_unbound")


def observer_callback_config(previous_raw, previous_sha256, runtime, primary, replacement, observer_release):
    """Rotate the fenced peer and observer code while retaining legacy exclusion identity."""
    error = "ingress_observer_callback_unbound"
    caddy._need(type(previous_raw) is bytes and 0 < len(previous_raw) <= 8192
                and caddy._sha(previous_raw) == previous_sha256 and primary in ("A", "B"), error)
    previous = seven_capture.parse(previous_raw)
    required = {"a_disk_paths_sha256", "b_disk_paths_sha256", "driver_path", "driver_sha256",
                "installation_id", "reactivation_plan_path", "runtime_path", "runtime_sha256",
                "source_loader_sha256", "source_manifest_path", "source_manifest_sha256", "source_root",
                "vm_a", "vm_b", "vm_observer_path", "vm_observer_sha256", "wsl_uid"}
    caddy._need(type(previous) is dict and required <= set(previous)
                and set(previous) <= required | {"kubernetes_reader", "reactivation_plan_sha256", "vm_b_installation_id"}
                and type(replacement) is dict and set(replacement) == {"node", "vm_id", "installation_id", "disk_paths_sha256"},
                error)
    release_pins = {"vm_observer_sha256", "source_loader_sha256", "source_manifest_sha256"}
    release_paths = {"vm_observer_path", "source_root", "source_manifest_path"}
    caddy._need(type(observer_release) is dict and set(observer_release) == release_pins | release_paths
                and all(type(observer_release[key]) is str and caddy.ID.fullmatch(observer_release[key])
                        for key in release_pins)
                and all(type(observer_release[key]) is str for key in release_paths), error)
    source_root = observer_release["source_root"]
    windows_path = observer_release["vm_observer_path"]
    caddy._need(re.fullmatch(r"/home/[A-Za-z0-9_.-]+/\.local/state/n100-ha/cutover-source-[a-f0-9]{40}/infra/k8s",
                            source_root)
                and not any(part in (".", "..") for part in source_root.split("/"))
                and observer_release["source_manifest_path"] == source_root.removesuffix("/infra/k8s") + ".manifest.json"
                and re.fullmatch(r"C:\\ProgramData\\N100HA-InitialCutover\\[a-f0-9]{40}\\[A-Za-z0-9_.-]+",
                                 windows_path)
                and not windows_path.endswith("."), error)
    peer = "B" if primary == "A" else "A"
    pins = (previous["installation_id"], previous.get("vm_b_installation_id", previous["installation_id"]),
            previous["a_disk_paths_sha256"], previous["b_disk_paths_sha256"],
            replacement["installation_id"], replacement["disk_paths_sha256"], runtime["runtime_sha256"])
    ids = (previous["vm_a"], previous["vm_b"], replacement["vm_id"])
    try:
        valid_ids = all(type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int for value in ids)
    except ValueError:
        valid_ids = False
    caddy._need(valid_ids and len(set(ids)) == 3 and replacement["node"] == peer
                and all(type(pin) is str and caddy.ID.fullmatch(pin) for pin in pins), error)
    peer_owner = pins[0] if peer == "A" else pins[1]
    caddy._need(replacement["installation_id"] != peer_owner, error)
    result = previous | observer_release | {
        "runtime_path": runtime["runtime_path"], "runtime_sha256": runtime["runtime_sha256"],
        "vm_" + peer.lower(): replacement["vm_id"],
        peer.lower() + "_disk_paths_sha256": replacement["disk_paths_sha256"],
        "vm_b_installation_id": pins[1],
    }
    owner_field = "installation_id" if peer == "A" else "vm_b_installation_id"
    result[owner_field] = replacement["installation_id"]
    raw = seven_capture.encoded(result)
    return {"callback_config": raw, "callback_config_sha256": caddy._sha(raw)}


def _inputs(reader, directory, spec, kind, argv):
    outer, outer_sha = _read(reader, directory / "ingress-switch-intent.json")
    caddy._need((outer["plan_sha256"], outer["attempt_id"]) == (spec["plan_sha256"], spec["attempt_id"]),
                "ingress_execution_unbound")
    if kind == "main":
        component, sha = _read(reader, directory / "main" / "caddy-apply-intent.json")
        caddy._need(argv == caddy._execution_command(spec["project"])
                    and component["original_container_sha256"] == outer["old_caddy_sha256"]
                    and component["candidate_sha256"] == outer["caddy_candidate_sha256"]
                    and component["overlay_sha256"] == outer["overlay_sha256"]
                    and caddy.ID.fullmatch(component["isolated_sha256"]), "ingress_execution_unbound")
    else:
        component, sha = _read(reader, directory / "relay" / "car-relay-replacement-intent.json")
        caddy._need(len(argv) == len(caddy.DOCKER) + 4 and argv[:-1] == (*caddy.DOCKER, "stop", "--time", "5")
                    and caddy.ID.fullmatch(argv[-1])
                    and component["previous_container_sha256"] == outer["old_relay_sha256"] == caddy._sha(argv[-1].encode())
                    and component["route_pin"] == outer["car_route_sha256"]
                    and caddy._sha(component["guest"].encode()) == outer["target_sha256"], "ingress_execution_unbound")
    return {"outer_sha256": outer_sha, "intent_sha256": sha, "argv_sha256": caddy._sha(b"\0".join(map(os.fsencode, argv)))}


class TransitionExecution:
    def __init__(self, spec):
        self.spec = copy.deepcopy(spec)
        self.directory = _scope(spec)

    def publish_observer_runtime(self, *, previous_sha256, legacy_plan, legacy_attempt, assert_legacy_closed):
        """Publish a fresh immutable observer input while the same coordinator holds its lock."""
        runtime_path = self.directory.parent / "runtime.json"
        previous_path = _observer_paths(runtime_path, legacy_attempt, self.spec)
        caddy._private_dir(runtime_path.parent)
        identity = caddy.CaddyExecution.identity()
        caddy._need(identity == self.spec["coordinator"]
                    and caddy._lock_held_by(self.directory / "ingress-switch.lock", identity["pid"]),
                    "ingress_execution_unlocked")
        reader = seven_capture.Reader()
        runtime = _observer_runtime(reader, previous_path, previous_sha256, legacy_plan, legacy_attempt, self.spec)
        assert_legacy_closed()
        reader.unchanged()
        payload = seven_capture.encoded(runtime)
        caddy._write(runtime_path, payload, 0o400)
        caddy._sync_dir(runtime_path.parent)
        reader.unchanged()
        assert_legacy_closed()
        return {"runtime_path": str(runtime_path), "runtime_sha256": caddy._sha(payload),
                "plan": legacy_plan, "attempt": legacy_attempt}

    @contextmanager
    def guard(self, main, car):
        """Bind both mutations to one held coordinator lock."""
        caddy._need(main.project == self.spec["project"] and main.execution is None and car.execution is None,
                    "ingress_execution_unbound")
        with exclusive(self.directory / "ingress-switch.lock"):
            main.execution = car.execution = self
            try:
                yield
            finally:
                main.execution = car.execution = None

    @contextmanager
    def _witness(self, kind, argv, *, raw=None, settled=False):
        reader = seven_capture.Reader()
        proof = _inputs(reader, self.directory, self.spec, kind, argv)
        if kind == "main":
            current, _ = _read(reader, self.directory / "main" / "caddy-apply-intent.json")
            caddy._need(caddy._sha(raw) == current["isolated_sha256"], "ingress_execution_unbound")
        identity = caddy.CaddyExecution.identity()
        caddy._need(identity == self.spec["coordinator"]
                    and caddy._lock_held_by(self.directory / "ingress-switch.lock", identity["pid"]),
                    "ingress_execution_unlocked")
        path = self.directory / kind / "transition-execution.json"
        if kind == "relay" and settled and os.path.lexists(path):
            actual, _ = _read(reader, path)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                caddy._need(actual == proof and seven_capture.stamp(os.fstat(stream.fileno())) ==
                            seven_capture.stamp(path.lstat()), "ingress_execution_unbound")
                reader.unchanged()
                yield
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "wb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.write(seven_capture.encoded(proof))
            stream.flush()
            os.fsync(stream.fileno())
            caddy._sync_dir(path.parent)
            reader.unchanged()
            yield

    def open(self, directory, project, raw):
        caddy._need(directory == self.directory / "main" and project == self.spec["project"], "ingress_execution_unbound")
        return self._witness("main", caddy._execution_command(project), raw=raw)

    def stop_relay(self, directory, container, *, settled=False):
        caddy._need(directory == self.directory / "relay", "ingress_execution_unbound")
        return self._witness("relay", (*caddy.DOCKER, "stop", "--time", "5", container), settled=settled)


def authorized(entry, observed_argv, spec):
    if spec is None:
        return False
    try:
        directory = _scope(spec)
        pid = int(entry.name)
        parent, started, argv, exe = caddy._process(pid)
        if observed_argv != argv or not argv or argv[0] != exe:
            return False
        expected = list(map(os.fsencode, caddy._execution_command(spec["project"])))
        if argv[1:] == expected[1:]:
            kind = "main"
            if exe != expected[0]:
                known = (str(caddy.DOCKER_CONFIG / "cli-plugins"), *caddy.PLUGIN_DIRS)
                if exe not in {os.fsencode(path + "/docker-compose") for path in known}:
                    return False
                parent, cli_start, cli, cli_exe = caddy._process(parent)
                if cli != expected or cli_exe != expected[0] or not 0 < cli_start <= started:
                    return False
                started = cli_start
            argv = expected
        else:
            kind = "relay"
        if not all(arg.isascii() for arg in argv):
            return False
        owner = spec["coordinator"]
        if parent != owner["pid"] or not 0 < owner["start"] <= started:
            return False
        _, actual_start, actual_argv, actual_exe = caddy._process(parent)
        if actual_start != owner["start"] or caddy._sha(b"\0".join([actual_exe, *actual_argv])) != owner["process"]:
            return False
        reader = seven_capture.Reader()
        witness = directory / kind / "transition-execution.json"
        proof, _ = _read(reader, witness)
        expected_proof = _inputs(reader, directory, spec, kind, tuple(os.fsdecode(arg) for arg in argv))
        result = (proof == expected_proof and caddy._lock_held_by(directory / "ingress-switch.lock", parent)
                  and caddy._lock_held_by(witness, parent))
        reader.unchanged()
        return result
    except (OSError, ValueError, TypeError, KeyError, IndexError, MigrationRejected, TransitionBlocked):
        return False
