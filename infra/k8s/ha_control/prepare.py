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
    "__init__.py", "controller.py", "windows.py", "pg.py", "initial_pg_lease.py", "initial_lease_service.py",
    "initial_windows_gate.py", "windows-ha-start-initial-service.ps1", "windows-ha-start-lease-observer.ps1",
    "windows-ha-stage-initial-activation.ps1",
    "windows_lease_pipe.py", "windows-ha-lease-proof-client.ps1", "windows-ha-lease-proof-server.ps1",
    "windows-ha-wsl-lease-observer.ps1",
    "cli.py", "prepare.py", "windows-fence.ps1",
    "windows-legacy-guard.ps1", "legacy_guard.py", "legacy_linux.py", "legacy_cutover.py", "legacy_writers.py", "launcher.py",
    "enforced_windows.py", "windows-enforced-vm.ps1", "fixed_vm_observer.py",
    "windows-fixed-vm-observer.ps1", "windows-fixed-vm-observer-client.ps1", "namespace_contract.py", "guest-owned-profile.ps1", "windows-guest-stage.ps1", "windows-guest-vm.ps1", "windows-guest-receipt.ps1", "windows-guest-network.ps1", "windows-guest-live-network.ps1", "windows-runtime-stage.ps1", "strict-canonical-sd.cs", "windows-namespace-policy.ps1", "windows-service-host.cs", "provision_enforced.py", "build-service-host.ps1", "windows-service-preflight.ps1", "privilege_closure.py")) + (
    "scripts/windows-bootstrap.ps1", "infra/k8s/ha_runtime/schema.sql", "infra/k8s/ha_runtime/enforcement.sql")
# Complete lazy effects import closure, with canonical paths retained in hashes.
EFFECTS_SOURCES = tuple("infra/k8s/ha_runtime/" + name for name in (
    "__init__.py", "config.py", "db.py", "proof.py", "effects.py", "enforcement.py",
    "schema.sql", "enforcement.sql")) + (
    "infra/k8s/ha_control/privilege_closure.py", "infra/k8s/tools/n100_ha_contract.py")
STOPPED_SERVICE_SOURCES = tuple('infra/k8s/ha_control/stopped_service_installer/' + name for name in (
    'reserve-identity.ps1', 'StrictJson.cs', 'StoppedService.cs', 'PrivateIo.cs',
    'ProductServiceSecurity.cs', 'prove_source.py'))
SOURCES = tuple(dict.fromkeys((*SOURCES, *EFFECTS_SOURCES, *STOPPED_SERVICE_SOURCES)))
TARGETS = {source: source.removeprefix("infra/k8s/") if source.startswith("infra/k8s/") else source for source in SOURCES}
REQUIREMENTS = b"psycopg[binary]==3.3.8\n"
CONFIG_PROFILE_DEFINITION = {'name': 'windows-managed-config-v1', 'capability_name': 'vmWorkerProcess', 'capability_sid': 'S-1-15-3-1024-2268835264-3721307629-241982045-173645152-1490879176-104643441-2915960892-1612460704', 'config_scope': 'owned-config-subtree-only', 'config_capability_access_mask': 2032127, 'config_file_share_mode': 7, 'media_capability_allowed': False, 'media_file_share_mode': 1, 'host_tcb_role': 'HostHypervisorTCB/config-metadata-only', 'acceptance_scopes': ['isolated-predeployment', 'production']}
CONFIG_PROFILE_DEFINITION_SHA256 = hashlib.sha256(json.dumps(CONFIG_PROFILE_DEFINITION, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def managed_config_template():
    return dict(name=CONFIG_PROFILE_DEFINITION['name'], definition_sha256=CONFIG_PROFILE_DEFINITION_SHA256,
        capability_name=CONFIG_PROFILE_DEFINITION['capability_name'], capability_sid=CONFIG_PROFILE_DEFINITION['capability_sid'],
        host_hypervisor_tcb_acknowledged=True, acceptance_scope='isolated-predeployment',
        operational_cutover_accepted=False, io_acceptance_sha256='', external_capability_review_sha256='', isolation_receipt_sha256='')



class BundleError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def effects_runtime_sha256(source_files):
    return sha(encode({path: source_files[path] for path in EFFECTS_SOURCES}))


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
            "legacy_linux_sha256": bindings["legacy_linux_sha256"],
            "legacy_guard_code_sha256": bindings["legacy_guard_code_sha256"],
            "windows_guard_sha256": bindings["windows_guard_sha256"],
            "guard_python": {"path": None, "sha256": None},
            "legacy_guard": {"schema_version": 1, "windows": None,
                             "linux": {"distribution": None, "reader_user": None, "spec": None}},
            "generation": None, "timeline": None, "system_id": None, "legacy_root": None, "boot_ticks": None,
            "resource": {"kind": None, "distribution": None, "engine_id": None, "container_ids": None,
                         "image_ids": None, "vm_id": None, "new_standby": None},
            "pg_nodes": {"A": dict(endpoint), "B": dict(endpoint)}}
    status = {"ready": False, "production_deployed": False, "source_commit": commit,
              "requires": ["native_windows_acceptance", "real_owned_resource_ids", "explicit_user_approval",
                           "private_plan_and_authority_ACLs", "TLS_and_passfile_references",
                           "complete_frozen_legacy_graph", "source_bound_live_guard_under_two_seconds"],
              "bindings": bindings,
              "approval_actions": {"fence": "unsupported by this guard: standalone observer acceptance required; no PostgreSQL transition",
                                   "ha-transition": "explicit full HA transition scope; separately approved"},
              "standalone_fence_supported": False,
              "execution_performed": False, "approval_issued": False}
    status["enforced_install_privilege_scope"] = {"target":"pg_catalog.pg_settings","privilege":"UPDATE table and columns only","principals":"PUBLIC and registered application roles only","transaction":"atomic closed-authority install; rollback on failure","production_applied":False}
    status['windows_managed_config']={'default':'strict-capability-free',
        'explicit_profile':'windows-managed-config-v1','definition_sha256':CONFIG_PROFILE_DEFINITION_SHA256,
        'metadata_tcb':'HostHypervisorTCB/config-metadata-only; no foreign-writer-absence claim',
        'media':'capability-free; own VM RW or ISO Read',
        'isolated_authority_requires':['actual_guest_io_receipt','fresh_isolated_closed_providers_no_existing_ingress_receipt'],
        'production_cutover_requires':['separate_production_plan_policy_profile_hashes','actual_io','external_capability_actor_review','explicit_production_acceptance'],
        'native_accepted':False,'operational_cutover_accepted':False}
    result = {"native-plan.template.json": encode(plan), "readiness.json": encode(status)}
    for action in ("fence", "ha-transition"):
        result[action + "-approval.template.json"] = encode({
            "action": action, "source_commit": commit, "plan_sha256": None, "expires_at": None})
    enforced=dict(plan, schema_version=2,profile='enforced-vm-v1',installation_id=None,
        pg_enforcement={'definition_sha256':bindings['enforced_definition_sha256'],'namespace_tables':{},
            'effects':{'code_sha256':bindings['effects_runtime_sha256'],'broker_role':None},
            'role_sets':{node:{service:None for service in ('portal-web','book-memo','youtube-memo','crawler-worker','car-care-worker','homeops-executor','system-agent')} for node in ('A','B')}},
        windows_enforced={key:None for key in ('driver_sha256','adapter_sha256','policy_path','policy_sha256',
            'service_name','service_sid','boot_ticks','session_id','python_path','python_sha256','launcher_path',
            'launcher_sha256','service_image_sha256','vm_id','vm_notes_sha256','vm_config_root','disks')})
    enforced['windows_enforced'].update(driver_sha256=bindings['enforced_driver_sha256'],
                                      adapter_sha256=bindings['enforced_adapter_sha256'],disks=[])
    result['enforced-plan.template.json']=encode(enforced)
    managed=json.loads(json.dumps(enforced));managed['windows_enforced']['config_profile']=managed_config_template()
    result['enforced-windows-managed-config-plan.template.json']=encode(managed)
    result['windows-managed-config.definition.json']=encode(CONFIG_PROFILE_DEFINITION)

    result['enforced-approval.template.json']=encode({'schema_version':2,'profile':'enforced-vm-v1',
        'source_commit':commit,'plan_sha256':None,'policy_sha256':None,'action':'ha-transition','expires_at':None})
    # Template only: issuing installation identity/authority belongs to a
    # separately approved TCB administrator installation step.
    result['enforced-installed-authority.template.json']=encode({'schema_version':3,'profile':'enforced-vm-v1',
        'action':'ha-transition','authority_kind':'installed-runtime','installation_id':None,'installed_at':None,
        'plan_sha256':None,'source_commit':commit,'policy_sha256':None})
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
                "config_profile_definitions": {"windows-managed-config-v1": CONFIG_PROFILE_DEFINITION_SHA256},
                "source_files": {key: sha(value) for key, value in sorted(files.items())},
                "generated_requirements_sha256": sha(REQUIREMENTS)}
    manifest["source_bootstrap_sha256"] = manifest["source_files"]["scripts/windows-bootstrap.ps1"]
    native = {key: value for key, value in manifest["source_files"].items()
              if key.endswith(("windows.py", "windows-fence.ps1", "guest-owned-profile.ps1", "windows-guest-stage.ps1", "windows-guest-vm.ps1", "windows-guest-receipt.ps1", "windows-guest-network.ps1", "windows-guest-live-network.ps1", "windows-runtime-stage.ps1", "windows-namespace-policy.ps1", "strict-canonical-sd.cs", "reserve-identity.ps1", "StrictJson.cs", "StoppedService.cs", "PrivateIo.cs", "ProductServiceSecurity.cs", "prove_source.py", "windows-enforced-vm.ps1", "windows-fixed-vm-observer.ps1", "windows-fixed-vm-observer-client.ps1", "fixed_vm_observer.py", "windows-service-host.cs", "provision_enforced.py", "windows-service-preflight.ps1", "windows-bootstrap.ps1", "windows-legacy-guard.ps1", "legacy_guard.py", "legacy_linux.py"))}
    manifest["native_windows_code_sha256"] = sha(encode(native))
    manifest["controller_code_sha256"] = sha(encode(manifest["source_files"]))
    manifest["effects_runtime_sha256"] = effects_runtime_sha256(manifest["source_files"])
    for field, source in (("legacy_linux_sha256", "legacy_linux.py"), ("legacy_guard_code_sha256", "legacy_guard.py"),
                          ("windows_guard_sha256", "windows-legacy-guard.ps1")):
        manifest[field] = manifest["source_files"]["infra/k8s/ha_control/" + source]
    for field,source in (('enforced_adapter_sha256','infra/k8s/ha_control/enforced_windows.py'),
                         ('enforced_driver_sha256','infra/k8s/ha_control/windows-enforced-vm.ps1'),
                         ('enforced_definition_sha256','infra/k8s/ha_runtime/enforcement.sql'),
                         ('namespace_policy_sha256','infra/k8s/ha_control/windows-namespace-policy.ps1')):
        manifest[field]=manifest['source_files'][source]
    bindings = {key: manifest[key] for key in ("source_bootstrap_sha256", "native_windows_code_sha256", "controller_code_sha256",
                                             "legacy_linux_sha256", "legacy_guard_code_sha256", "windows_guard_sha256",
                                             "enforced_adapter_sha256","enforced_driver_sha256","enforced_definition_sha256","effects_runtime_sha256","namespace_policy_sha256")}
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
              if key.endswith(("windows.py", "windows-fence.ps1", "guest-owned-profile.ps1", "windows-guest-stage.ps1", "windows-guest-vm.ps1", "windows-guest-receipt.ps1", "windows-guest-network.ps1", "windows-guest-live-network.ps1", "windows-runtime-stage.ps1", "windows-namespace-policy.ps1", "strict-canonical-sd.cs", "reserve-identity.ps1", "StrictJson.cs", "StoppedService.cs", "PrivateIo.cs", "ProductServiceSecurity.cs", "prove_source.py", "windows-enforced-vm.ps1", "windows-fixed-vm-observer.ps1", "windows-fixed-vm-observer-client.ps1", "fixed_vm_observer.py", "windows-service-host.cs", "provision_enforced.py", "windows-service-preflight.ps1", "windows-bootstrap.ps1", "windows-legacy-guard.ps1", "legacy_guard.py", "legacy_linux.py"))}
    expected = {"config_profile_definitions": {"windows-managed-config-v1": CONFIG_PROFILE_DEFINITION_SHA256},
                "source_bootstrap_sha256": manifest["source_files"]["scripts/windows-bootstrap.ps1"],
                "native_windows_code_sha256": sha(encode(native)),
                "controller_code_sha256": sha(encode(manifest["source_files"])),
                "effects_runtime_sha256": effects_runtime_sha256(manifest["source_files"])}
    for field, source in (("legacy_linux_sha256", "legacy_linux.py"), ("legacy_guard_code_sha256", "legacy_guard.py"),
                          ("windows_guard_sha256", "windows-legacy-guard.ps1")):
        expected[field] = manifest["source_files"]["infra/k8s/ha_control/" + source]
    for field,source in (('enforced_adapter_sha256','infra/k8s/ha_control/enforced_windows.py'),
                         ('enforced_driver_sha256','infra/k8s/ha_control/windows-enforced-vm.ps1'),
                         ('enforced_definition_sha256','infra/k8s/ha_runtime/enforcement.sql'),
                         ('namespace_policy_sha256','infra/k8s/ha_control/windows-namespace-policy.ps1')):
        expected[field]=manifest['source_files'][source]
    if (any(manifest[key] != value for key, value in expected.items())
            or manifest["generated_requirements_sha256"] != sha(REQUIREMENTS)
            or actual["files/requirements.txt"] != sha(REQUIREMENTS)):
        raise BundleError("native_code_binding_mismatch")
    rendered = templates(manifest["source_commit"], {key: value for key, value in expected.items()
                                                   if key != "config_profile_definitions"})
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
