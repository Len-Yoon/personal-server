#!/usr/bin/env python3
"""Inert exact-commit release builder; never connects to N100 or deploys resources."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ("portal-web", "book-memo", "youtube-memo", "crawler-worker",
            "car-care-worker", "homeops-executor", "system-agent")
UTILITY = "ha-backup-utility"
OWNER = "personal-server.ha-package"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
BASE = re.compile(r"python:3\.(?:11|12)-alpine3\.24@sha256:[0-9a-f]{64}")


class PackageError(RuntimeError):
    pass


def sha(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def git(*args):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if result.returncode:
        raise PackageError("git_source_unavailable")
    return result.stdout


def allowed(path):
    """Positive allowlist plus defense against credentials/data hidden in assets."""
    p = PurePosixPath(path)
    if any(part.startswith(".") or part in {"data", "secrets", "__pycache__", "credentials", "node_modules"}
           for part in p.parts):
        return False
    if p.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".sqlite", ".db", ".env"}:
        return False
    if p.parts[0] in SERVICES:
        rest = p.parts[1:]
        return rest in [("Dockerfile",), ("requirements.txt",)] or (
            len(rest) >= 2 and rest[0] == "app" and (
                p.suffix == ".py" or (rest[1] in {"templates", "static"}
                                     and p.suffix.lower() in {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp", ".woff", ".woff2", ".ttf"})))
    return (len(p.parts) == 4 and p.parts[:3] == ("infra", "k8s", "ha_runtime")
            and p.suffix in {".py", ".sql"}) or path in {
                "infra/k8s/tools/n100_ha_contract.py", "infra/k8s/ha-package/Dockerfile.template",
                "infra/k8s/ha-package/Utility.Dockerfile.template"}


def snapshot(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PackageError("full_source_commit_required")
    if git("rev-parse", commit + "^{commit}").decode().strip() != commit:
        raise PackageError("source_commit_mismatch")
    result = {}
    for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode()
        if not allowed(path):
            continue
        mode, kind, oid = meta.decode().split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise PackageError("non_regular_source_rejected")
        result[path] = git("cat-file", "blob", oid)
    required = {s + "/" + f for s in SERVICES for f in ("Dockerfile", "requirements.txt", "app/main.py")}
    required |= {"infra/k8s/ha_runtime/__init__.py", "infra/k8s/ha_runtime/schema.sql",
                 "infra/k8s/ha_runtime/backup.py", "infra/k8s/ha_runtime/backup_cli.py",
                 "infra/k8s/tools/n100_ha_contract.py", "infra/k8s/ha-package/Dockerfile.template",
                 "infra/k8s/ha-package/Utility.Dockerfile.template"}
    if not required <= result.keys():
        raise PackageError("source_snapshot_incomplete")
    return result


def render(original, template):
    bases = re.findall(r"(?im)^\s*FROM\s+([^\r\n]+)", original)
    users = list(re.finditer(r"(?m)^USER ([^\r\n]+)$", original))
    if (len(bases) != 1 or not BASE.fullmatch(bases[0]) or len(users) != 1
            or users[0].group(1) != "10001:10001" or re.search(r"(?im)^\s*(ARG|ENTRYPOINT)\b", original)):
        raise PackageError("unsupported_original_dockerfile")
    if (template.count("@ORIGINAL_ROOT_SECTION@") != 1 or template.count("@ORIGINAL_USER_SECTION@") != 1
            or re.search(r"(?im)^\s*(FROM|ARG|USER|ENTRYPOINT|CMD)\b", template)
            or "psycopg[binary]==3.3.8" not in template):
        raise PackageError("invalid_package_template")
    # Only fixed additive instructions are permitted; drift fails closed.
    expected = ("@ORIGINAL_ROOT_SECTION@\n# Shared HA runtime is packaged but activation requires separately approved config.\n"
                "COPY _ha/ /opt/ha/\nRUN pip install --no-cache-dir 'psycopg[binary]==3.3.8'\n"
                "ENV PYTHONPATH=/opt/ha\n@ORIGINAL_USER_SECTION@\n")
    if template != expected:
        raise PackageError("invalid_package_template")
    index = users[0].start()
    rendered = template.replace("@ORIGINAL_ROOT_SECTION@", original[:index].rstrip()).replace(
        "@ORIGINAL_USER_SECTION@", original[index:].rstrip())
    return rendered, bases[0]


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise PackageError("bundle_symlink_rejected")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = sha(path.read_bytes())
    return result


def utility_recipe(files):
    template = files["infra/k8s/ha-package/Utility.Dockerfile.template"].decode()
    expected = ("FROM @APP_BASE_IMAGE@\nWORKDIR /opt/ha\n"
                "RUN apk add --no-cache postgresql17-client \\\n"
                "    && pip install --no-cache-dir 'cryptography==46.0.5' 'psycopg[binary]==3.3.8'\n"
                "RUN addgroup -S -g 10001 app && adduser -S -u 10001 -G app app\n"
                "COPY _ha/ /opt/ha/\nENV PYTHONPATH=/opt/ha PYTHONDONTWRITEBYTECODE=1\n"
                "USER 10001:10001\nCMD [\"python\", \"-m\", \"ha_runtime.backup_cli\", \"--help\"]\n")
    if template != expected:
        raise PackageError("invalid_utility_template")
    # Reuse the literal immutable native Python 3.11 base of the committed Portal.
    original = files["portal-web/Dockerfile"].decode()
    _, base = render(original, files["infra/k8s/ha-package/Dockerfile.template"].decode())
    if not base.startswith("python:3.11-"):
        raise PackageError("utility_native_python_base_required")
    return template.replace("@APP_BASE_IMAGE@", base), base


def prepare(commit, output):
    files = snapshot(commit)
    if output.exists():
        raise PackageError("new_output_directory_required")
    output.mkdir(parents=True, mode=0o700)
    template = files["infra/k8s/ha-package/Dockerfile.template"].decode()
    manifest = {"format": 1, "source_commit": commit,
                "source_files": {k: sha(v) for k, v in sorted(files.items())}, "services": {}, "utilities": {}}
    for service in SERVICES:
        context = output / "contexts" / service
        context.mkdir(parents=True)
        original = files[service + "/Dockerfile"].decode()
        rendered, base = render(original, template)
        for name, data in files.items():
            if name.startswith(service + "/") and name != service + "/Dockerfile":
                target = context / name.removeprefix(service + "/")
            elif name.startswith("infra/k8s/ha_runtime/") or name == "infra/k8s/tools/n100_ha_contract.py":
                target = context / "_ha" / name.removeprefix("infra/k8s/")
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (context / "Dockerfile").write_text(rendered)
        manifest["services"][service] = {"base": base, "user": "10001:10001",
            "original_dockerfile_sha256": sha(original.encode()), "dockerfile_sha256": sha(rendered.encode())}
    context = output / "contexts" / UTILITY
    context.mkdir(parents=True)
    rendered, base = utility_recipe(files)
    (context / "Dockerfile").write_text(rendered)
    for name, data in files.items():
        if name.startswith("infra/k8s/ha_runtime/") or name == "infra/k8s/tools/n100_ha_contract.py":
            target = context / "_ha" / name.removeprefix("infra/k8s/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    manifest["utilities"][UTILITY] = {"base": base, "user": "10001:10001", "dockerfile_sha256": sha(rendered.encode())}
    release = sha(encode(manifest))
    manifest["release_id"] = release
    generated = encode({"source_commit": commit, "release_id": release,
                        "source_files_sha256": sha(encode(manifest["source_files"]))})
    for service in (*SERVICES, UTILITY):
        (output / "contexts" / service / "_ha/ha_runtime/release.json").write_bytes(generated)
    manifest["bundle_files"] = inventory(output / "contexts")
    (output / "source-manifest.json").write_bytes(encode(manifest))
    plan = {"release_id": release, "source_commit": commit, "activation": "requires_separate_approved_controller_configuration",
            "production_deployed": False, "deploy_commands_executed": False,
            "images": {s: "n100-ha-package:" + release + "-" + s for s in SERVICES},
            "utility_images": {UTILITY: "n100-ha-package:" + release + "-" + UTILITY},
            "required_review": ["controller_config", "TLS_and_passfile_mounts", "fencing_and_writer_ownership",
                                "backup_restore", "HomeOps_Docker_socket_original_user_group_permissions"],
            "steps_after_approval": ["verify source and image bindings", "import exact saved images on approved targets",
                                     "validate approved config and credential ownership", "apply approved runtime mounts",
                                     "verify writer fencing, restore, and external health"]}
    (output / "deployment-plan.json").write_bytes(encode(plan))
    for path in output.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    return manifest


def verify_source(bundle):
    manifest = json.loads((bundle / "source-manifest.json").read_text())
    release = manifest["release_id"]
    if sha(encode({k: v for k, v in manifest.items() if k not in {"release_id", "bundle_files"}})) != release:
        raise PackageError("source_manifest_modified")
    if inventory(bundle / "contexts") != manifest["bundle_files"]:
        raise PackageError("bundle_files_modified")
    # Re-derive from exact Git objects: an attacker cannot simply rewrite hashes.
    files = snapshot(manifest["source_commit"])
    if manifest["source_files"] != {k: sha(v) for k, v in sorted(files.items())}:
        raise PackageError("source_commit_binding_mismatch")
    template = files["infra/k8s/ha-package/Dockerfile.template"].decode()
    expected = {}
    generated = encode({"source_commit": manifest["source_commit"], "release_id": release,
                        "source_files_sha256": sha(encode(manifest["source_files"]))})
    for service in SERVICES:
        dockerfile, base = render(files[service + "/Dockerfile"].decode(), template)
        if manifest["services"][service]["base"] != base:
            raise PackageError("base_binding_mismatch")
        expected[service + "/Dockerfile"] = sha(dockerfile.encode())
        expected[service + "/_ha/ha_runtime/release.json"] = sha(generated)
        for name, data in files.items():
            if name.startswith(service + "/") and name != service + "/Dockerfile":
                expected[service + "/" + name.removeprefix(service + "/")] = sha(data)
            elif name.startswith("infra/k8s/ha_runtime/") or name == "infra/k8s/tools/n100_ha_contract.py":
                expected[service + "/_ha/" + name.removeprefix("infra/k8s/")] = sha(data)
    utility_dockerfile, utility_base = utility_recipe(files)
    if manifest.get("utilities") != {UTILITY: {"base": utility_base, "user": "10001:10001", "dockerfile_sha256": sha(utility_dockerfile.encode())}}:
        raise PackageError("utility_source_binding_mismatch")
    expected[UTILITY + "/Dockerfile"] = sha(utility_dockerfile.encode())
    expected[UTILITY + "/_ha/ha_runtime/release.json"] = sha(generated)
    for name, data in files.items():
        if name.startswith("infra/k8s/ha_runtime/") or name == "infra/k8s/tools/n100_ha_contract.py":
            expected[UTILITY + "/_ha/" + name.removeprefix("infra/k8s/")] = sha(data)
    if expected != manifest["bundle_files"]:
        raise PackageError("rendered_source_binding_mismatch")
    return manifest


class LocalDocker:
    def __init__(self):
        if platform.system() != "Darwin":
            raise PackageError("mac_orbstack_only")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        context = json.loads(self.call("context", "inspect", "orbstack"))
        if context[0]["Endpoints"]["docker"]["Host"] != "unix://" + str(Path.home() / ".orbstack/run/docker.sock"):
            raise PackageError("isolated_local_socket_required")

    def call(self, *args, timeout=60):
        result = subprocess.run(["docker", "--context", "orbstack", *args], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise PackageError("local_docker_" + args[0] + "_failed")
        return result.stdout.strip()


def archive_binding(path, expected_id):
    with tarfile.open(path) as archive:
        def read(name):
            stream = archive.extractfile(name)
            if stream is None:
                raise PackageError("invalid_image_archive")
            return stream.read()
        raw = read("manifest.json")
        entries = json.loads(raw)
        if len(entries) != 1:
            raise PackageError("single_image_archive_required")
        config = read(entries[0]["Config"])
        if "sha256:" + sha(config) != expected_id:
            raise PackageError("image_config_binding_mismatch")
        parsed = json.loads(config)
        layers = entries[0]["Layers"]
        if ["sha256:" + sha(read(layer)) for layer in layers] != parsed["rootfs"]["diff_ids"]:
            raise PackageError("image_layer_binding_mismatch")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"archive_sha256": digest.hexdigest(), "docker_archive_manifest_sha256": sha(raw),
            "image_config_digest": expected_id}


def build(bundle):
    manifest = verify_source(bundle)
    if (bundle / "images").exists() or (bundle / "image-bindings.json").exists():
        raise PackageError("new_build_required")
    docker = LocalDocker()
    owner = str(uuid.uuid4())
    output = bundle / "images"
    output.mkdir()
    evidence = {"release_id": manifest["release_id"], "source_commit": manifest["source_commit"],
                "owner": owner, "images": {}, "utilities": {}, "production_deployed": False}
    smoke = ("import os,sys,pathlib,hashlib,json,psycopg,ha_runtime; "
             "assert os.getuid()==10001; assert not ha_runtime.enabled(); "
             "assert psycopg.__version__=='3.3.8'; "
             "[compile(p.read_bytes(),str(p),'exec') for p in pathlib.Path('/app/app').rglob('*.py')]; "
             "from ha_runtime import proof; ")
    for service in (*SERVICES, UTILITY):
        tag = "n100-ha-package:" + manifest["release_id"] + "-" + service
        docker.call("build", "--platform", "linux/amd64", "--label", OWNER + "=" + owner,
                    "--label", "org.opencontainers.image.revision=" + manifest["source_commit"],
                    "--label", "personal-server.ha-release=" + manifest["release_id"],
                    "--tag", tag, str(bundle / "contexts" / service), timeout=1800)
        image = json.loads(docker.call("image", "inspect", tag))[0]
        config = image["Config"]
        labels = config["Labels"]
        rendered = (bundle / "contexts" / service / "Dockerfile").read_text()
        expected_cmd = json.loads(re.findall(r"(?m)^CMD (.+)$", rendered)[0])
        if (not DIGEST.fullmatch(image["Id"]) or labels.get(OWNER) != owner
                or labels.get("org.opencontainers.image.revision") != manifest["source_commit"]
                or labels.get("personal-server.ha-release") != manifest["release_id"]
                or config["User"] != "10001:10001" or config["Cmd"] != expected_cmd
                or image.get("Architecture") != "amd64" or image.get("Os") != "linux"):
            raise PackageError("image_ownership_or_source_mismatch")
        expected_files = {}
        for relative, digest in manifest["bundle_files"].items():
            if not relative.startswith(service + "/"):
                continue
            relative = relative.removeprefix(service + "/")
            if relative == "Dockerfile":
                continue
            target = "/opt/ha/" + relative.removeprefix("_ha/") if relative.startswith("_ha/") else "/app/" + relative
            expected_files[target] = digest
        metadata = manifest["utilities"] if service == UTILITY else manifest["services"]
        native = "3.11" if "python:3.11-" in metadata[service]["base"] else "3.12"
        expected_release = {"source_commit": manifest["source_commit"], "release_id": manifest["release_id"],
                            "source_files_sha256": sha(encode(manifest["source_files"]))}
        service_smoke = (smoke + "assert str(sys.version_info.major)+'.'+str(sys.version_info.minor)==" + repr(native) + "; "
                         "assert ha_runtime.config._release()==" + repr(expected_release) + "; "
                         "expected=" + repr(expected_files) + "; "
                         "assert all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in expected.items()); "
                         "print('package_smoke=PASS')")
        if service == UTILITY:
            service_smoke = ("import cryptography,subprocess; assert cryptography.__version__=='46.0.5'; "
                             "assert all(subprocess.check_output([tool,'--version'],text=True).startswith(tool+' (PostgreSQL) 17.') "
                             "for tool in ['pg_dump','pg_restore']); " + service_smoke)
        result = docker.call("run", "--rm", "--pull=never", "--network=none", "--read-only",
                             "--cap-drop=ALL", "--security-opt=no-new-privileges", "--label", OWNER + "=" + owner,
                             "--entrypoint", "python", image["Id"], "-B", "-c", service_smoke, timeout=90)
        if result != "package_smoke=PASS":
            raise PackageError("image_smoke_failed")
        if service == UTILITY:
            docker.call("run", "--rm", "--pull=never", "--network=none", "--read-only",
                        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--label", OWNER + "=" + owner,
                        image["Id"], timeout=90)
        archive = output / (service + ".tar")
        docker.call("image", "save", "--output", str(archive), image["Id"], timeout=180)
        binding = archive_binding(archive, image["Id"])
        binding.update({"tag": tag, "smoke": "PASS", "user": config["User"], "cmd": config["Cmd"],
                        "env": config["Env"], "labels": labels})
        evidence["utilities" if service == UTILITY else "images"][service] = binding
        archive.chmod(0o444)
        print(json.dumps({"service": service, "build": "PASS", "smoke": "PASS"}), flush=True)
    (bundle / "image-bindings.json").write_bytes(encode(evidence))
    (bundle / "image-bindings.json").chmod(0o444)
    return evidence


def verify(bundle):
    manifest = verify_source(bundle)
    evidence = json.loads((bundle / "image-bindings.json").read_text())
    if (evidence["release_id"] != manifest["release_id"] or evidence["source_commit"] != manifest["source_commit"]
            or set(evidence["images"]) != set(SERVICES) or set(evidence.get("utilities", {})) != {UTILITY}):
        raise PackageError("image_release_binding_mismatch")
    for service, binding in {**evidence["images"], **evidence["utilities"]}.items():
        actual = archive_binding(bundle / "images" / (service + ".tar"), binding["image_config_digest"])
        if any(binding.get(k) != v for k, v in actual.items()):
            raise PackageError("image_archive_modified")
        with tarfile.open(bundle / "images" / (service + ".tar")) as archive:
            index = json.load(archive.extractfile("manifest.json"))[0]
            config = json.load(archive.extractfile(index["Config"]))["config"]
        labels = config.get("Labels", {})
        if (labels.get(OWNER) != evidence["owner"] or labels.get("org.opencontainers.image.revision") != manifest["source_commit"]
                or labels.get("personal-server.ha-release") != manifest["release_id"] or config.get("User") != "10001:10001"):
            raise PackageError("archived_image_release_mismatch")
    return {"release_id": manifest["release_id"], "services": len(SERVICES), "verified": True,
            "utilities": 1, "production_deployed": False, "registry_manifest_digest": "not_published"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--commit", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    for name in ("build", "verify", "verify-source"):
        sub.add_parser(name).add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.commit, args.output)
            result = {"release_id": result["release_id"], "source_commit": result["source_commit"], "prepared": True}
        else:
            result = {"build": build, "verify": verify, "verify-source": verify_source}[args.command](args.bundle)
            if args.command != "verify":
                result = {"release_id": result["release_id"], "verified": True, "production_deployed": False}
        print(json.dumps(result, sort_keys=True))
    except PackageError as error:
        raise SystemExit("package_failed:" + str(error)) from None
    except (KeyError, ValueError, OSError, tarfile.TarError, subprocess.TimeoutExpired):
        raise SystemExit("package_failed: see source/build preconditions; credential values suppressed") from None


if __name__ == "__main__":
    main()
