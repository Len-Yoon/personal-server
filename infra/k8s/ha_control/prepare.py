#!/usr/bin/env python3
"""Prepare/verify an inert exact-commit native controller handoff; never execute it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]
SOURCES = tuple("infra/k8s/ha_control/" + name for name in (
    "__init__.py", "controller.py", "windows.py", "pg.py", "cli.py", "prepare.py", "windows-fence.ps1")) + (
    "scripts/windows-bootstrap.ps1", "infra/k8s/ha_runtime/schema.sql")
TARGETS = {source: source.removeprefix("infra/k8s/") if source.startswith("infra/k8s/") else source for source in SOURCES}
REQUIREMENTS = b"psycopg[binary]==3.3.8\n"


class BundleError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def git(*args):
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if result.returncode:
        raise BundleError("committed_source_unavailable")
    return result.stdout


def snapshot(commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BundleError("full_source_commit_required")
    if git("rev-parse", commit + "^{commit}").decode().strip() != commit:
        raise BundleError("source_commit_mismatch")
    files = {}
    for source in SOURCES:
        entry = git("ls-tree", "-z", commit, "--", source).split(b"\0")[0]
        if not entry:
            raise BundleError("required_source_missing")
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if name.decode() != source or kind != "blob" or mode not in {"100644", "100755"}:
            raise BundleError("regular_committed_source_required")
        files[source] = git("cat-file", "blob", oid)
    return files


def templates(commit, bindings):
    endpoint = {"host": None, "port": None, "dbname": None, "user": None,
                "sslmode": "verify-full", "sslrootcert": None, "passfile": None}
    plan = {"schema_version": 1, "source_commit": commit,
            "controller_bundle_sha256": None,
            "bootstrap_sha256": bindings["source_bootstrap_sha256"],
            "native_windows_code_sha256": bindings["native_windows_code_sha256"],
            "controller_code_sha256": bindings["controller_code_sha256"], "cluster": None, "node": None,
            "generation": None, "timeline": None, "system_id": None, "legacy_root": None, "boot_ticks": None,
            "resource": {"kind": None, "distribution": None, "engine_id": None, "container_ids": None,
                         "image_ids": None, "vm_id": None, "new_standby": None},
            "pg_nodes": {"A": dict(endpoint), "B": dict(endpoint)}}
    status = {"ready": False, "production_deployed": False, "source_commit": commit,
              "requires": ["native_windows_acceptance", "real_owned_resource_ids", "explicit_user_approval",
                           "private_plan_and_authority_ACLs", "TLS_and_passfile_references"],
              "bindings": bindings,
              "approval_actions": {"fence": "owned-resource stop only; no PostgreSQL transition",
                                   "ha-transition": "explicit full HA transition scope; separately approved"},
              "execution_performed": False, "approval_issued": False}
    result = {"native-plan.template.json": encode(plan), "readiness.json": encode(status)}
    for action in ("fence", "ha-transition"):
        result[action + "-approval.template.json"] = encode({
            "action": action, "source_commit": commit, "plan_sha256": None, "expires_at": None})
    return result


def inventory(root):
    files = {}
    if root.is_symlink():
        raise BundleError("bundle_symlink_rejected")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BundleError("bundle_symlink_rejected")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return files


def prepare(commit, output):
    files = snapshot(commit)
    if output.exists() or output.is_symlink():
        raise BundleError("new_output_directory_required")
    manifest = {"format": 1, "source_commit": commit,
                "source_files": {key: sha(value) for key, value in sorted(files.items())},
                "generated_requirements_sha256": sha(REQUIREMENTS)}
    manifest["source_bootstrap_sha256"] = manifest["source_files"]["scripts/windows-bootstrap.ps1"]
    native = {key: value for key, value in manifest["source_files"].items()
              if key.endswith(("windows.py", "windows-fence.ps1", "windows-bootstrap.ps1"))}
    manifest["native_windows_code_sha256"] = sha(encode(native))
    manifest["controller_code_sha256"] = sha(encode(manifest["source_files"]))
    bindings = {key: manifest[key] for key in ("source_bootstrap_sha256", "native_windows_code_sha256", "controller_code_sha256")}
    rendered = {"files/" + TARGETS[key]: value for key, value in files.items()}
    rendered["files/requirements.txt"] = REQUIREMENTS
    rendered.update(templates(commit, bindings))
    manifest["bundle_files"] = {key: sha(value) for key, value in sorted(rendered.items())}
    manifest_bytes = encode(manifest)
    output.mkdir(parents=True, mode=0o700)
    for name, data in {**rendered, "source-manifest.json": manifest_bytes}.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
    return {"source_commit": commit, "manifest_sha256": sha(manifest_bytes), "ready": False,
            "source_bootstrap_sha256": manifest["source_bootstrap_sha256"],
            "native_windows_code_sha256": manifest["native_windows_code_sha256"]}


def verify(bundle, expected_manifest_sha256=None):
    if bundle.is_symlink():
        raise BundleError("bundle_symlink_rejected")
    path = bundle / "source-manifest.json"
    if path.is_symlink():
        raise BundleError("bundle_symlink_rejected")
    raw = path.read_bytes()
    if expected_manifest_sha256 is not None and (not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
                                                or sha(raw) != expected_manifest_sha256):
        raise BundleError("trusted_manifest_pin_mismatch")
    manifest = json.loads(raw)
    if (manifest.get("format") != 1 or not re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
            or set(manifest["source_files"]) != set(SOURCES)):
        raise BundleError("source_manifest_mismatch")
    actual = inventory(bundle)
    actual.pop("source-manifest.json", None)
    if actual != manifest["bundle_files"]:
        raise BundleError("bundle_files_modified")
    for source in SOURCES:
        if manifest["source_files"][source] != actual["files/" + TARGETS[source]]:
            raise BundleError("source_file_binding_mismatch")
    native = {key: value for key, value in manifest["source_files"].items()
              if key.endswith(("windows.py", "windows-fence.ps1", "windows-bootstrap.ps1"))}
    expected = {"source_bootstrap_sha256": manifest["source_files"]["scripts/windows-bootstrap.ps1"],
                "native_windows_code_sha256": sha(encode(native)),
                "controller_code_sha256": sha(encode(manifest["source_files"]))}
    if (any(manifest[key] != value for key, value in expected.items())
            or manifest["generated_requirements_sha256"] != sha(REQUIREMENTS)
            or actual["files/requirements.txt"] != sha(REQUIREMENTS)):
        raise BundleError("native_code_binding_mismatch")
    rendered = templates(manifest["source_commit"], expected)
    expected_paths = {"files/" + value for value in TARGETS.values()} | {"files/requirements.txt"} | set(rendered)
    if set(actual) != expected_paths or any(actual[name] != sha(data) for name, data in rendered.items()):
        raise BundleError("inert_template_binding_mismatch")
    return {"source_commit": manifest["source_commit"], "manifest_sha256": sha(raw), "verified": True,
            "ready": False, "production_deployed": False, "approval_issued": False, **expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--commit", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    verify_parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args()
    try:
        value = (prepare(args.commit, args.output) if args.command == "prepare" else
                 verify(args.bundle, args.expected_manifest_sha256))
        print(json.dumps(value, sort_keys=True))
    except BundleError as error:
        raise SystemExit("controller_bundle_failed:" + str(error)) from None
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("controller_bundle_failed:invalid_bundle") from None


if __name__ == "__main__":
    main()
