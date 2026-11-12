"""Read the stopped N100 Compose mounts without starting a container."""
from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

from .legacy_writers import CONTAINER, DOCKER, IMAGE, _match, _need
from .pvc_capture import _digest, _extract_selected, _private_root, _run_archive_command

PATHS = {
    "car-care-worker": ("/data/car-care", "/data/oauth/hyundai-token.json"),
    "homeops-executor": ("/data/homeops-executor",),
}
MOUNTS = {
    "car-care-worker": {"/data/car-care": "bind", "/data/oauth": "volume"},
    "homeops-executor": {"/data/homeops-executor": "volume"},
}
ENVIRONMENT = {
    "car-care-worker": {"CAR_CARE_DB_PATH": "/data/car-care/car-care.sqlite3",
                        "HYUNDAI_TOKEN_STORE_PATH": "/data/oauth/hyundai-token.json"},
    "homeops-executor": {"HOMEOPS_IDEMPOTENCY_DB_PATH": "/data/homeops-executor/restarts.sqlite3"},
}


def _command(args, *, output=None, timeout=20):
    return _run_archive_command((*DOCKER, *args), output=output, timeout=timeout)


def _observe(service, container, run, *, stopped):
    _need(_match(CONTAINER, container), "compose_capture_scope_invalid")
    rows = json.loads(run(("inspect", container)))
    _need(type(rows) is list and len(rows) == 1, "compose_capture_source_unconfirmed")
    row, = rows
    state = row["State"]
    restart = row["HostConfig"]["RestartPolicy"]
    _need(row["Id"] == container and _match(IMAGE, row["Image"])
          and row["Config"]["Labels"]["com.docker.compose.service"] == service
          and (state["Running"], state["Status"], restart["Name"]) ==
              ((False, "exited", "no") if stopped else (True, "running", "unless-stopped"))
          and not any(state.get(key) for key in ("Paused", "Restarting", "Dead", "OOMKilled"))
          and restart["MaximumRetryCount"] == 0, "compose_capture_source_unconfirmed")
    keys = ENVIRONMENT[service]
    for key, value in keys.items():
        configured = [item for item in row["Config"]["Env"] if item.startswith(key + "=")]
        _need(configured == [key + "=" + value], "compose_capture_path_changed")
    mounts = {}
    copy_roots = MOUNTS[service]
    _need(not any(mount["Destination"].startswith(root + "/")
                  for mount in row["Mounts"] for root in copy_roots),
          "compose_capture_nested_mount")
    for destination, kind in copy_roots.items():
        options = [mount for mount in row["Mounts"] if mount["Destination"] == destination]
        _need(len(options) == 1 and options[0]["Type"] == kind
              and options[0].get("Source", "").startswith("/"), "compose_capture_mount_changed")
        mount, = options
        volume_id = None
        if kind == "volume":
            volumes = json.loads(run(("volume", "inspect", mount["Name"])))
            _need(len(volumes) == 1 and volumes[0]["Name"] == mount["Name"]
                  and volumes[0]["Driver"] == "local" and volumes[0]["Mountpoint"] == mount["Source"],
                  "compose_capture_mount_changed")
            volume_id = _digest(volumes[0])
        mounts[destination] = _digest({"mount": mount, "volume": volume_id})
    return {"container": container, "image": row["Image"], "mounts": mounts}


def inspect_compose_capture(containers, *, run=_command):
    _need(set(containers) == set(PATHS), "compose_capture_scope_invalid")
    return {service: _observe(service, container, run, stopped=False) for service, container in containers.items()}


class ComposeCapture:
    def __init__(self, bindings, *, run=_command):
        _need(set(bindings) == set(PATHS), "compose_capture_scope_invalid")
        self.bindings = deepcopy(bindings)
        self.run = run

    def collect(self, root, assert_writers_closed):
        root = Path(root)
        _need(_private_root(root), "compose_capture_private_destination_required")
        final = root / "compose-snapshot"
        _need(not os.path.lexists(final), "compose_capture_destination_exists")
        with tempfile.TemporaryDirectory(prefix="compose-staging-", dir=root) as directory:
            staging = Path(directory)
            for service, binding in self.bindings.items():
                assert_writers_closed()
                container = binding["container"]
                _need(_observe(service, container, self.run, stopped=True) == binding, "compose_capture_source_changed")
                destination = staging / service
                destination.mkdir(mode=0o700)
                for source in PATHS[service]:
                    with tempfile.TemporaryFile(dir=root) as archive:
                        self.run(("cp", f"{container}:{source}", "-"), output=archive, timeout=120)
                        archive.seek(0)
                        _extract_selected(archive, destination, {PurePosixPath(source).name})
                assert_writers_closed()
                _need(_observe(service, container, self.run, stopped=True) == binding, "compose_capture_source_changed")
            _need(not os.path.lexists(final), "compose_capture_destination_exists")
            staging.rename(final)
        return {service: final / service for service in self.bindings}
