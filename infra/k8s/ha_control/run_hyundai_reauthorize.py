"""Launch the Hyundai recovery callback from the N100 WSL operator terminal."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile


DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
LOCK = Path("/mnt/c/personal-server/data/ha-legacy-bootstrap.lock")
OWNER = Path("/mnt/c/ProgramData/N100HA-Cutover/ha-controller-owner.json")
ROLE = "car-care-worker"
PORT = {"8015/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8015"}]}
ENVIRONMENT = (
    "HYUNDAI_CLIENT_ID", "HYUNDAI_CLIENT_SECRET",
    "HYUNDAI_REDIRECT_URI", "HYUNDAI_TOKEN_STORE_PATH",
)
LEGACY_MODULES = {
    "hyundai.py": "b6da9a827ccf92c341b07f66a72ac1756b86262269656257f0068ea86f53ac66",
    "oauth_callback.py": "ff157d8ab611481483385bacf4da91dc10e908425b7dbedee40f88fd43577be8",
}
HELPER_SHA256 = "8dc9888d91a3a0b7f38e25eb2f0f67af5a81439c04feda4137aa88bbd14ee5bd"
CONTAINER_HELPER = "/run/recovery/hyundai_reauthorize.py"
BOOTSTRAP = """
from hashlib import sha256
from pathlib import Path
import runpy
import sys
files = Path('/app/app/services')
if any(sha256((files / name).read_bytes()).hexdigest() != digest
       for name, digest in EXPECTED.items()):
    raise SystemExit('Legacy Hyundai callback source mismatch')
sys.argv = [HELPER, '--container-interface']
runpy.run_path(HELPER, run_name='__main__')
""".replace("EXPECTED", repr(LEGACY_MODULES)).replace("HELPER", repr(CONTAINER_HELPER))


def require(value, reason):
    if not value:
        raise ValueError(reason)


def inspect(*args):
    raw = subprocess.check_output((*DOCKER, *args), timeout=10, stderr=subprocess.DEVNULL)
    (item,) = json.loads(raw)
    return item


def get_owner():
    with os.fdopen(os.open(OWNER, os.O_RDONLY | os.O_NOFOLLOW), "rb") as marker:
        return json.loads(marker.read(65537))


def get_car(container_id):
    candidates = subprocess.check_output(
        (*DOCKER, "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.service=" + ROLE),
        timeout=10, stderr=subprocess.DEVNULL,
    ).decode("ascii").split()
    require(candidates == [container_id], "the legacy Compose service identity changed")
    car = inspect("inspect", "--type", "container", container_id)
    state = car["State"]
    config = car["Config"]
    require(car["Id"] == container_id and config["Labels"].get("com.docker.compose.service") == ROLE
            and config["User"] == "10001:10001", "the pinned Car Care identity changed")
    require(state["Status"] in {"created", "exited"} and not state["Running"]
            and car["HostConfig"]["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0},
            "the legacy worker is not fenced")
    require(car["HostConfig"]["PortBindings"] == PORT, "the original callback publication changed")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", car["Image"]), "the installed image ID is unpinned")
    (network,) = car["NetworkSettings"]["Networks"].values()
    network_id = network["NetworkID"]
    require(re.fullmatch(r"[a-f0-9]{64}", network_id), "the original callback network is unpinned")
    current_network = inspect("network", "inspect", network_id)
    require(current_network["Id"] == network_id and current_network["Driver"] == "bridge"
            and not current_network.get("Containers"), "the original callback bridge is occupied")
    (mount,) = (m for m in car["Mounts"] if m["Destination"] == "/data/oauth")
    require(mount["Type"] == "volume" and mount["RW"]
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", mount["Name"]),
            "the original OAuth volume changed")
    volume = inspect("volume", "inspect", mount["Name"])
    require(volume["Name"] == mount["Name"] and volume["Driver"] == "local",
            "the original OAuth volume is unavailable")
    active = subprocess.check_output(
        (*DOCKER, "ps", "-q", "--filter", "volume=" + volume["Name"]),
        timeout=10, stderr=subprocess.DEVNULL,
    )
    require(not active.strip(), "the original OAuth volume has an active container")
    env = dict(entry.split("=", 1) for entry in config["Env"])
    require(all(env.get(key) for key in ENVIRONMENT)
            and env["HYUNDAI_REDIRECT_URI"] == "https://car.len.pe.kr/oauth/hyundai/callback"
            and env["HYUNDAI_TOKEN_STORE_PATH"] == "/data/oauth/hyundai-token.json"
            and "HA_PG_CONFIG" not in env
            and not any(env.get(key) for key in ("HYUNDAI_OAUTH_BASE_URL", "HYUNDAI_API_BASE_URL")),
            "the existing OAuth configuration changed")
    return car["Image"], network_id, mount["Name"], {key: env[key] for key in ENVIRONMENT}


def run_callback(image, network, volume, env, helper):
    name = "n100-hyundai-reauth-" + secrets.token_hex(8)
    cidfile = helper.with_name("container-id")
    env_flags = [arg for name in ENVIRONMENT for arg in ("--env", name)]
    command = (*DOCKER, "run", "--rm", "-it", "--name", name, "--cidfile", str(cidfile), "--pull", "never",
               "--log-driver", "none", "--read-only", "--user", "10001:10001",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--network", network, "--publish", "127.0.0.1:8015:8015",
               "--mount", f"type=volume,source={volume},target=/data/oauth,volume-nocopy",
               "--mount", f"type=bind,source={helper},target={CONTAINER_HELPER},readonly",
               *env_flags, "--workdir", "/app", "--entrypoint", "python", image, "-B", "-c", BOOTSTRAP)
    try:
        return subprocess.run(command, env={"PATH": "/usr/bin:/bin", **env}, timeout=650).returncode
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired:
        return 124
    finally:
        if cidfile.is_file():
            container_id = cidfile.read_text(encoding="ascii").strip()
            require(re.fullmatch(r"[a-f0-9]{64}", container_id), "recovery container ID was invalid")
            subprocess.run((*DOCKER, "rm", "-f", container_id), check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[a-f0-9]{64}", args.container_id)
                and re.fullmatch(r"[a-f0-9]{64}", args.plan_sha256)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", args.attempt_id), "invalid recovery binding")
        require(args.check_only or (sys.stdin.isatty() and sys.stdout.isatty()),
                "an interactive terminal is required")
        with os.fdopen(os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner = get_owner()
            require(owner["attempt_id"] == args.attempt_id and owner["plan_sha256"] == args.plan_sha256,
                    "the retained Windows owner did not match")
            image, network, volume, env = get_car(args.container_id)
            helper = Path(__file__).with_name("hyundai_reauthorize.py").read_bytes()
            require(hashlib.sha256(helper).hexdigest() == HELPER_SHA256, "the reviewed callback source changed")
            print("기존 Car Care 정지, OAuth 볼륨 범위 및 로컬 콜백 포트를 확인했습니다.", flush=True)
            if args.check_only:
                return 0
            with tempfile.TemporaryDirectory(prefix="n100-hyundai-reauth-") as directory:
                file = Path(directory) / "callback.py"
                file.write_bytes(helper)
                file.chmod(0o444)
                require(get_owner() == owner, "the retained Windows owner changed")
                result = run_callback(image, network, volume, env, file)
                require(get_owner() == owner, "the retained Windows owner changed")
                get_car(args.container_id)
                return result
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Child errors can contain terminal commands. Keep the operator error
        # free of provider configuration or the callback connection URL.
        print(f"재인증 실행을 확인하지 못했습니다 ({type(error).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
