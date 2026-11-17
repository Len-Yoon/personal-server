"""Witness exact ingress mutations without relaxing legacy writer exclusion."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re

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
        self.spec = spec
        self.directory = _scope(spec)

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
