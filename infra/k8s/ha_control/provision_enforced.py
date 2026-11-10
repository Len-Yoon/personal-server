"""Prepare an inert, pinned service package. Never install or alter a host.

The trusted private input pins build evidence, not native acceptance. No CA,
passfile, key, Python installation or credentials are copied into the package.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import uuid

from . import prepare as controller_bundle
from . import enforced_windows as enforced_profile

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


def profile_acceptance_receipts(trusted, selected, plan):
    receipts=trusted['acceptance_receipts']
    keys={key for key in ('io_acceptance_sha256','external_capability_review_sha256','isolation_receipt_sha256') if selected[key]}
    if type(receipts) is not dict or set(receipts)!=keys:
        deny()
    kinds={'io_acceptance_sha256':'guest-media-io','external_capability_review_sha256':'external-capability-review','isolation_receipt_sha256':'isolated-cluster-boundary'}
    vm_bindings=None
    for key in keys:
        encoded=receipts[key]
        if type(encoded) is not str or len(encoded)>1398104:
            deny()
        raw=base64.b64decode(encoded,validate=True)
        if sha(raw)!=selected[key]:
            deny()
        value=decode(raw)
        fields={'schema_version','kind','acceptance_scope','source_commit','installation_id','cluster','config_profile_definition_sha256','passed'}
        if key=='isolation_receipt_sha256':
            fields|={'vm_ids','providers_closed','existing_ingress_unchanged','production_authority'}
        elif key=='io_acceptance_sha256':
            fields|={'vm_ids','guest_boot_observed','vhd_write_observed','iso_read_observed'}
        else:
            fields|={'primary_token_survey_sha256','acquisition_model_reviewed','scope_limitations_acknowledged'}
        if (type(value) is not dict or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['kind']!=kinds[key] or value['acceptance_scope']!=selected['acceptance_scope'] or value['passed'] is not True
                or value['source_commit']!=plan['source_commit'] or value['installation_id']!=plan['installation_id'] or value['cluster']!=plan['cluster']
                or value['config_profile_definition_sha256']!=selected['definition_sha256']):
            deny()
        if key=='isolation_receipt_sha256':
            if value['providers_closed'] is not True or value['existing_ingress_unchanged'] is not True or value['production_authority'] is not False:
                deny()
        elif key=='io_acceptance_sha256':
            if any(value[k] is not True for k in ('guest_boot_observed','vhd_write_observed','iso_read_observed')):
                deny()
        else:
            if not HEX.fullmatch(str(value['primary_token_survey_sha256'])) or value['acquisition_model_reviewed'] is not True or value['scope_limitations_acknowledged'] is not True:
                deny()
        if key in {'io_acceptance_sha256','isolation_receipt_sha256'}:
            ids=value['vm_ids']
            if type(ids) is not dict or set(ids)!={'A','B'} or ids['A']==ids['B'] or any(type(v) is not str or str(uuid.UUID(v))!=v for v in ids.values()) or ids[plan['node']]!=plan['windows_enforced']['vm_id']:
                deny()
            if vm_bindings is not None and ids!=vm_bindings:
                deny()
            vm_bindings=ids


def managed_profile_binding(trusted, binding, commit):
    try:
        selected=enforced_profile.config_profile(trusted['config_profile'])
        digest=enforced_profile.config_profile_digest(selected)
        if digest!=trusted['config_profile_sha256'] or not HEX.fullmatch(str(trusted['policy_sha256'])):
            deny()
        payloads={}
        for label in ('plan','policy'):
            encoded=trusted[label+'_bytes_base64']
            if type(encoded) is not str or len(encoded)>1398104:
                deny()
            raw=base64.b64decode(encoded,validate=True)
            expected_hash=trusted['service_args']['plan_sha256'] if label=='plan' else trusted['policy_sha256']
            if sha(raw)!=expected_hash:
                deny()
            payloads[label]=decode(raw)
        bound=payloads['plan'];policy=payloads['policy'];cfg=bound['windows_enforced']
        policy_keys={'schema_version','profile','cluster','generation','session_id','service_sid','boot_ticks','vm_id','vm_notes_sha256','vm_config_root','disks','trusted_admin_sids','hyperv_member_sids','host_admins_tcb_acknowledged','config_profile'}
        if type(policy) is not dict or set(policy)!=policy_keys or type(policy['schema_version']) is not int or policy['schema_version']!=1:
            deny()
        if (bound['source_commit']!=commit or bound['profile']!='enforced-vm-v1' or type(bound['schema_version']) is not int or bound['schema_version']!=2
                or enforced_profile.config_profile_digest(cfg['config_profile'])!=digest
                or enforced_profile.config_profile_digest(policy['config_profile'])!=digest
                or cfg['policy_sha256']!=trusted['policy_sha256'] or policy['profile']!='enforced-vm-v1'
                or cfg['driver_sha256']!=binding['enforced_driver_sha256'] or cfg['adapter_sha256']!=binding['enforced_adapter_sha256']
                or any(cfg[key]!=trusted['service_args'][key] for key in ('service_name','service_sid'))
                or any(policy[key]!=cfg[key] for key in ('session_id','service_sid','boot_ticks','vm_id','vm_notes_sha256','vm_config_root','disks'))
                or policy['cluster']!=bound['cluster'] or policy['generation']!=bound['generation']):
            deny()
        profile_acceptance_receipts(trusted,selected,bound)
        return dict(config_profile=selected,config_profile_sha256=digest,
            definition_sha256=selected['definition_sha256'],plan_sha256=trusted['service_args']['plan_sha256'],
            policy_sha256=trusted['policy_sha256'],acceptance_scope=selected['acceptance_scope'],
            metadata_trust_scope='HostHypervisorTCB/config-metadata-only',
            operational_cutover_accepted=False,native_accepted=False,
            acceptance_claim='inert_package_only; actual I/O and scope-bound authority remain separate')
    except (enforced_profile.TransitionBlocked,ValueError,TypeError,KeyError,binascii.Error):
        deny()


def prepare(commit, bundle, host_exe, private_manifest, private_sha256, output):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        deny()
    trusted = decode(private(private_manifest, private_sha256))
    expected = {'format', 'approved_commits', 'bundle_manifest_sha256', 'host_source_sha256',
                'host_exe_sha256', 'build', 'service_args'}
    managed=type(trusted) is dict and 'config_profile' in trusted
    if managed:
        expected|={'config_profile','config_profile_sha256','policy_sha256','plan_bytes_base64','policy_bytes_base64','acceptance_receipts'}
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
    profile_binding=managed_profile_binding(trusted,binding,commit) if managed else None
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
    if profile_binding is not None:
        plan['config_profile_binding']=profile_binding
        plan['requires']+=['actual_guest_io_and_scope_bound_isolation_or_production_evidence','no_production_authority_from_isolated_profile']
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
