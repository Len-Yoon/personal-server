"""Prepare an inert, pinned service package. Never install or alter a host.

The trusted private input pins build evidence, not native acceptance. No CA,
passfile, key, Python installation or credentials are copied into the package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat

from . import prepare as controller_bundle

HOST_SOURCE = 'infra/k8s/ha_control/windows-service-host.cs'
ARGS_KEYS = ('format', 'service_name', 'service_sid', 'python_path', 'python_sha256',
             'launcher_path', 'launcher_sha256', 'plan_path', 'plan_sha256',
             'approval_path', 'approval_sha256', 'mode', 'host_sha256')
HEX = re.compile(r'[0-9a-f]{64}\Z')


class ProvisionError(RuntimeError):
    pass


def deny():
    raise ProvisionError('enforced_provision_unverified')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            deny()
        result[key] = value
    return result


def decode(raw):
    if len(raw) > 1048576:
        deny()
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: deny())


def private(path, expected):
    # Preparation is offline POSIX only. Windows ACL acceptance belongs to the
    # real service/driver and cannot be inferred from a mode bit emulation.
    if os.name == 'nt' or not HEX.fullmatch(str(expected)):
        deny()
    path = Path(path)
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_nlink != 1 or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        deny()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_size > 1048576:
            deny()
        raw = stream.read(1048577)
    after = path.lstat()
    fields = ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
    if sha(raw) != expected or any(getattr(after, key) != getattr(info, key) for key in fields):
        deny()
    return raw


def windows_path(value):
    if (type(value) is not str or not re.match(r'^[A-Za-z]:\\', value)
            or any(c in value for c in '\r\n\0"') or ':' in value[2:]
            or any(part in {'', '.', '..'} or part.endswith(('.', ' ')) for part in value[3:].split('\\'))):
        deny()


def render_args(values):
    if type(values) is not dict or set(values) != set(ARGS_KEYS):
        deny()
    if (values['format'] != 'enforced-service-v1'
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', str(values['service_name']))
            or not re.fullmatch(r'S-1-5-80-(?:[0-9]{1,10}-){4}[0-9]{1,10}', str(values['service_sid']))
            or values['mode'] not in {'initialize', 'advance', 'serve'}):
        deny()
    for key in ARGS_KEYS:
        value = values[key]
        if type(value) is not str or any(c in value for c in '\r\n\0'):
            deny()
        if key.endswith('_sha256') and not HEX.fullmatch(value):
            deny()
        if key.endswith('_path'):
            windows_path(value)
    return ''.join(f'{key}={values[key]}\n' for key in ARGS_KEYS).encode()


def source_at(commit):
    # A regular exact Git blob, never the mutable working tree.
    entry = controller_bundle.git('ls-tree', '-z', commit, '--', HOST_SOURCE).split(b'\0')[0]
    metadata, name = entry.split(b'\t', 1)
    mode, kind, oid = metadata.decode().split()
    if name.decode() != HOST_SOURCE or mode != '100644' or kind != 'blob':
        deny()
    return controller_bundle.git('cat-file', 'blob', oid)


def host_pe(raw):
    # Shape check only, not a compiler/source-equivalence proof.
    if len(raw) < 512 or len(raw) > 1048576 or raw[:2] != b'MZ':
        deny()
    offset = int.from_bytes(raw[60:64], 'little')
    if offset + 264 > len(raw) or raw[offset:offset+4] != b'PE\0\0':
        deny()
    if int.from_bytes(raw[offset+4:offset+6], 'little') != 0x14c:
        deny()
    optional = offset + 24
    if int.from_bytes(raw[optional:optional+2], 'little') != 0x10b:
        deny()
    if int.from_bytes(raw[offset+22:offset+24], 'little') & 0x2000:
        deny()  # Must be an executable, not a DLL renamed as an EXE.
    if not any(raw[optional+208:optional+216]):
        deny()  # PE32 CLR directory (entry 14).


def prepare(commit, bundle, host_exe, private_manifest, private_sha256, output):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        deny()
    trusted = decode(private(private_manifest, private_sha256))
    expected = {'format', 'approved_commits', 'bundle_manifest_sha256', 'host_source_sha256',
                'host_exe_sha256', 'build', 'service_args'}
    if (type(trusted) is not dict or set(trusted) != expected or type(trusted['format']) is not int
            or trusted['format'] != 1 or type(trusted['approved_commits']) is not list
            or not 1 <= len(trusted['approved_commits']) <= 8
            or len(set(trusted['approved_commits'])) != len(trusted['approved_commits'])
            or any(not re.fullmatch(r'[0-9a-f]{40}', c) for c in trusted['approved_commits'])
            or commit not in trusted['approved_commits']):
        deny()
    for key in ('bundle_manifest_sha256', 'host_source_sha256', 'host_exe_sha256'):
        if not HEX.fullmatch(str(trusted[key])):
            deny()
    binding = controller_bundle.verify(Path(bundle), trusted['bundle_manifest_sha256'])
    if binding['source_commit'] != commit:
        deny()
    source = source_at(commit)
    if sha(source) != trusted['host_source_sha256']:
        deny()
    build = trusted['build']
    expected_build = {'source_sha256': trusted['host_source_sha256'],
            'artifact_sha256': trusted['host_exe_sha256'], 'target': '.NETFramework,Version=v4.8',
            'platform': 'AnyCPU', 'output_kind': 'ConsoleApplication', 'native_accepted': False,
            'pins_sha256': build.get('pins_sha256') if type(build) is dict else None,
            'compiler': build.get('compiler') if type(build) is dict else None,
            'references': build.get('references') if type(build) is dict else None,
            'deterministic_outputs': 2}
    if (type(build) is not dict or build != expected_build
            or type(build.get('native_accepted')) is not bool
            or type(build.get('deterministic_outputs')) is not int
            or not HEX.fullmatch(str(build.get('pins_sha256')))):
        deny()
    for key, names in [('compiler', {'Microsoft.CodeAnalysis.dll', 'Microsoft.CodeAnalysis.CSharp.dll'}),
                       ('references', {'mscorlib.dll', 'System.dll', 'System.Core.dll', 'System.ServiceProcess.dll'})]:
        if (type(build[key]) is not dict or set(build[key]) != names
                or any(not HEX.fullmatch(str(value)) for value in build[key].values())):
            deny()
    # The externally reviewed pins digest binds the exact compiler/reference
    # mapping, not just a self-reported successful compilation.
    if sha(controller_bundle.encode({'compiler': build['compiler'], 'references': build['references']})) != build['pins_sha256']:
        deny()
    artifact = private(host_exe, trusted['host_exe_sha256'])
    host_pe(artifact)
    args = render_args(trusted['service_args'])
    if trusted['service_args']['host_sha256'] != trusted['host_exe_sha256']:
        deny()
    preflight_path = Path(bundle) / 'files/ha_control/windows-service-preflight.ps1'
    preflight_sha = sha(preflight_path.read_bytes())
    # Binding plan/approval bytes are prepared separately by their owner. No
    # executable install script or credentials are generated by this tool.
    output = Path(output)
    parent = output.parent.lstat()
    if (output.exists() or output.is_symlink() or output.parent.is_symlink()
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        deny()
    output.mkdir(mode=0o700)
    shutil.copytree(bundle, output / 'controller')
    for name, data in {'windows-service-host.cs': source, 'windows-service-host.exe': artifact,
                       'service.args': args}.items():
        target = output / name
        target.write_bytes(data)
        target.chmod(0o400)
    journal_root = PureWindowsPath(trusted['service_args']['plan_path']).parent / 'journal'
    plan = {'format': 1,
            'journal': {'root': str(journal_root), 'path': str(journal_root / 'controller-state.json'),
                        'authority_parent_immutable': True, 'root_precreated_by_TCB': True,
                        'writers': ['SYSTEM', 'Administrators', trusted['service_args']['service_sid']]}, 'source_commit': commit, 'profile': 'enforced-vm-v1',
            'preflight': {'bundle_path': 'controller/files/ha_control/windows-service-preflight.ps1',
                          'sha256': preflight_sha, 'host_files': {'windows-service-host.exe': sha(artifact),
                                                               'service.args': sha(args)},
                          'run_before': 'any_StartService', 'trusted_installer_required': True},
            'service_name': trusted['service_args']['service_name'], 'service_sid': trusted['service_args']['service_sid'],
            'execution_performed': False, 'ready': False,
            'install_sequence': ['review_target_and_frozen_artifact', 'require_new_private_code_and_config_roots',
                'copy_hash_verified_package_and_existing_python_references',
                'copy_only_EXE_and_service_args_to_new_dedicated_host_root', 'apply_reviewed_system_and_admin_owned_ACLs',
                'allow_service_read_only_and_separate_journal_modify',
                'TCB_precreate_plan_parent_journal_child_with_service_private_modify_ACL',
                'retain_immutable_authority_parent_and_validate_journal_files_lock_and_temps', 'register_exact_host_virtual_service_manual_start',
                'verify_real_service_sid_and_session_zero', 'verify_hyperv_group_and_namespace_DACL',
                'verify_exact_VM_VHD_boot_session_and_TLS_files',
                'trusted_installer_run_pinned_windows_service_preflight_before_any_StartService',
                'require_exact_two_leaf_host_inventory_no_CLR_config_or_sidecar_and_private_ACLs',
                'keep_host_root_TCB_write_only_between_preflight_and_StartService',
                'run_approved_native_acceptance_before_enable'],
            'rollback_sequence': ['stop_exact_new_service_and_confirm_owned_job_cleanup',
                'unregister_only_new_service_if_identity_and_image_hash_match',
                'remove_only_new_package_if_inventory_matches_and_no_live_children',
                'preserve_existing_ACLs_VM_VHD_Python_credentials_and_journal'],
            'requires': ['explicit_apply_approval', 'actual_service_sid_and_ACLs', 'actual_HyperV_boundary',
                         'actual_VM_VHD_and_boot_identity', 'actual_TLS_private_files', 'pre_StartService_trusted_inventory_and_ACLs',
                         'pinned_compiler_references_and_two_identical_outputs', 'whole_transition_two_seconds']}
    (output / 'install-plan.json').write_bytes(controller_bundle.encode(plan))
    inventory = controller_bundle.inventory(output)
    manifest = {'format': 1, 'source_commit': commit, 'private_input_sha256': private_sha256,
                'bundle_manifest_sha256': trusted['bundle_manifest_sha256'], 'build': build,
                'files': inventory, 'ready': False, 'execution_performed': False}
    raw = controller_bundle.encode(manifest)
    (output / 'provision-manifest.json').write_bytes(raw)
    return verify(output, sha(raw))


def verify(output, expected_sha256):
    output = Path(output)
    raw = (output / 'provision-manifest.json').read_bytes()
    if not HEX.fullmatch(str(expected_sha256)) or sha(raw) != expected_sha256:
        deny()
    value = decode(raw)
    if (set(value) != {'format','source_commit','private_input_sha256','bundle_manifest_sha256','build','files','ready','execution_performed'}
            or type(value['format']) is not int or value['format'] != 1
            or value['ready'] is not False or value['execution_performed'] is not False):
        deny()
    actual = controller_bundle.inventory(output)
    actual.pop('provision-manifest.json', None)
    if actual != value['files']:
        deny()
    result = controller_bundle.verify(output / 'controller', value['bundle_manifest_sha256'])
    if result['source_commit'] != value['source_commit']:
        deny()
    return {'format': 1, 'source_commit': value['source_commit'], 'manifest_sha256': sha(raw),
            'package_verified': True, 'ready': False, 'execution_performed': False,
            'native_accepted': False, 'installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('prepare')
    for name in ('commit','bundle','host-exe','private-manifest','private-sha256','output'):
        build.add_argument('--'+name, required=True)
    check = commands.add_parser('verify')
    check.add_argument('--output', required=True)
    check.add_argument('--expected-sha256', required=True)
    args = vars(parser.parse_args())
    command = args.pop('command')
    try:
        result = prepare(**args) if command == 'prepare' else verify(**args)
    except (ProvisionError, controller_bundle.BundleError, OSError, ValueError, TypeError, KeyError):
        raise SystemExit('enforced_provision_unverified') from None
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
