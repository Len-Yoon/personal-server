#!/usr/bin/env python3
"""Exact-source NoCloud seed and fresh-guest two-boot offline installation.

Host prepare/ISO work never creates a VM or contacts N100. Guest work is limited
to the private intent, a single new-guest reboot, OS/Engine/image installation,
and PG-not-started acceptance. No provider/DB credentials or writer authority.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import types
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

SOURCE_PATH = 'infra/k8s/ha-package/guest_seed.py'
LOCAL = Path(__file__).resolve()
ROOT = LOCAL.parents[3] if len(LOCAL.parents)>3 else Path('/')
HEX = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
SERIAL_PREFIX = 'N100_HA_SEED_RECEIPT '
OUTPUT_SINK = '> /dev/null'
GUEST_ROOT = Path('/var/lib/n100-ha-seed')
GUEST_CONFIG = Path('/etc/n100-ha-seed/config.json')
GUEST_BUNDLE = Path('/opt/n100-ha-seed/bundle')
MOUNT = Path('/run/n100-ha-seed/iso')


class SeedError(RuntimeError):
    pass


def need(value,reason='guest_seed_invalid'):
    if not value:raise SeedError(reason)


def encode(value):
    return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()


def cloud_output_config():
    return {'all': OUTPUT_SINK}


def early_config_bytes():
    """System config and NoCloud user-data must select the same private sink."""
    return b'#cloud-config\n'+encode({'output': cloud_output_config()})


def sha(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def regular(path,*,private=False):
    path=Path(path);info=path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and not any(p.is_symlink() for p in (path,*path.parents)),
         'single_regular_file_required')
    if private:need(info.st_uid==os.geteuid() and not info.st_mode & 0o077,'private_input_required')
    return path


def run(argv,reason='guest_command_failed',timeout=180):
    environment={k:v for k,v in os.environ.items() if not k.startswith('DOCKER_')}
    if argv[0]=='docker':argv=['docker','--host','unix:///var/run/docker.sock',*argv[1:]]
    result=subprocess.run(argv,capture_output=True,env=environment,timeout=timeout)
    need(result.returncode==0,reason)
    return result.stdout


def builder(path,expected_sha256):
    need(type(expected_sha256) is str and HEX.fullmatch(expected_sha256),'trusted_bootstrap_code_pin_required')
    path=regular(path)
    need(sha(path)==expected_sha256,'bootstrap_executable_hash_mismatch')
    module=types.ModuleType('seed_offline_bootstrap');module.__file__=str(path)
    raw=path.read_bytes();need(hashlib.sha256(raw).hexdigest()==expected_sha256,'bootstrap_executable_hash_mismatch')
    exec(compile(raw,str(path),'exec'),module.__dict__)
    module.__source_sha256__=expected_sha256
    return module


def tree_files(root):
    """Inspect every entry through no-follow directory descriptors, before reads."""
    root=Path(root).absolute()
    need(not any(p.is_symlink() for p in (root,*root.parents)) and root.is_dir(),'plain_seed_directory_required')
    result=set();count=0
    pending=[(os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW),Path())]
    try:
        while pending:
            descriptor,relative=pending.pop()
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        count+=1;need(count<=4096,'seed_entry_limit')
                        info=entry.stat(follow_symlinks=False)
                        path=relative/entry.name
                        if stat.S_ISDIR(info.st_mode):
                            child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
                            pending.append((child,path))
                        else:
                            need(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'seed_nonregular_entry')
                            result.add(str(path))
            finally:os.close(descriptor)
    finally:
        for descriptor,_ in pending:os.close(descriptor)
    return result


def copy_manifest(source,destination,files):
    source=Path(source).absolute();destination=Path(destination).absolute()
    need(tree_files(source)==set(files),'copy_inventory_mismatch')
    destination.mkdir(mode=0o700,parents=False,exist_ok=False)
    root_fd=os.open(source,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name,expected in files.items():
            need(type(name) is str,'unsafe_copy_path')
            parts=Path(name).parts
            need(parts and not name.startswith('/') and '..' not in parts and '\\' not in name,'unsafe_copy_path')
            descriptors=[];parent=root_fd
            try:
                for part in parts[:-1]:
                    parent=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);descriptors.append(parent)
                descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                with os.fdopen(descriptor,'rb') as reader:
                    info=os.fstat(reader.fileno());need(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'copy_nonregular_file')
                    target=destination/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    with target.open('xb') as writer:shutil.copyfileobj(reader,writer,1024*1024)
                    target.chmod(0o400);need(sha(target)==expected,'copied_file_hash_mismatch')
            finally:
                for descriptor in reversed(descriptors):os.close(descriptor)
    finally:os.close(root_fd)


def git_source(commit,path):
    need(type(commit) is str and COMMIT.fullmatch(commit),'exact_source_commit_required')
    return run(['git','-C',str(ROOT),'show',commit+':'+path],'exact_seed_source_unavailable',30)


def source_builder(raw,path):
    module=types.ModuleType('seed_exact_git_bootstrap');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    module.__source_sha256__=hashlib.sha256(raw).hexdigest()
    return module


def public_key(path,expected):
    need(type(expected) is str and HEX.fullmatch(expected),'public_key_hash_required')
    path=regular(path)
    need(path.suffix=='.pub' and not path.stat().st_mode & 0o022,'public_key_file_required')
    need(sha(path)==expected,'public_key_hash_mismatch')
    text=path.read_text().strip();parts=text.split()
    need(len(parts)>=2 and len(text)<16384 and not any(c in text for c in '\r\n\0'),'single_public_key_required')
    kind=parts[0];need(kind in ('ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256'),'supported_public_key_required')
    try:raw=base64.b64decode(parts[1],validate=True)
    except ValueError:raise SeedError('invalid_public_key') from None
    length=int.from_bytes(raw[:4],'big')
    need(length==len(kind) and raw[4:4+length]==kind.encode() and len(raw)>4+length,'invalid_public_key')
    offset=4+length
    def field():
        nonlocal offset
        need(offset+4<=len(raw),'invalid_public_key')
        size=int.from_bytes(raw[offset:offset+4],'big');offset+=4
        need(size>0 and offset+size<=len(raw),'invalid_public_key')
        value=raw[offset:offset+size];offset+=size
        return value
    if kind=='ssh-ed25519':need(len(field())==32,'invalid_public_key')
    elif kind=='ssh-rsa':need(len(field())<=8 and len(field())>=256,'invalid_public_key')
    else:need(field()==b'nistp256' and len(field())==65,'invalid_public_key')
    need(offset==len(raw),'invalid_public_key')
    # Personal comments/addresses are never copied into the ISO or receipts.
    return kind+' '+parts[1]


def validate_spec(value,lock):
    need(type(value) is dict and type(value.get('schema_version')) is int and value['schema_version'] in (1,2),'seed_spec_fields_invalid')
    fields={'schema_version','source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256',
            'expected_kernel_release','mac_address','ipv4_cidr','operator_public_key_path','operator_public_key_sha256'}
    need(set(value)==fields or (value['schema_version']==2 and set(value)==fields|{'egress_mac_address'}),'seed_spec_fields_invalid')
    need(all(type(value[k]) is str and HEX.fullmatch(value[k]) for k in
             ('installation_id','seed_id','bootstrap_manifest_sha256','operator_public_key_sha256')))
    need(type(value['source_commit']) is str and COMMIT.fullmatch(value['source_commit']))
    need(all(value[k]==lock[k] for k in ('source_commit','installation_id','node')),'seed_bundle_identity_mismatch')
    need(value['node'] in ('A','B'))
    need(type(value['expected_kernel_release']) is str and re.fullmatch(r'6\.8\.0-[0-9]+-generic',value['expected_kernel_release']))
    need(any(row['package']=='linux-image-'+value['expected_kernel_release'] for row in lock.get('os_packages',[])),
         'expected_kernel_not_in_signed_package_lock')
    need(type(value['mac_address']) is str and re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',value['mac_address']))
    need(int(value['mac_address'][:2],16)&1==0 and value['mac_address']!='00:00:00:00:00:00')
    egress=value.get('egress_mac_address')
    if egress is not None:
        need(type(egress) is str and re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',egress)
             and int(egress[:2],16)&1==0 and egress!='00:00:00:00:00:00' and egress!=value['mac_address'],
             'invalid_distinct_egress_mac')
    try:address=ipaddress.ip_interface(value['ipv4_cidr'])
    except (TypeError,ValueError):raise SeedError('invalid_private_guest_network') from None
    need(address.version==4 and 8<=address.network.prefixlen<=30 and address.ip.is_private
         and not (address.ip.is_loopback or address.ip.is_link_local or address.ip.is_multicast or address.ip.is_reserved)
         and address.ip not in (address.network.network_address,address.network.broadcast_address),'invalid_private_guest_network')
    need(str(address.ip)==lock['pg_a' if value['node']=='A' else 'pg_b'],'seed_network_bundle_mismatch')
    need(type(value['operator_public_key_path']) is str and Path(value['operator_public_key_path']).is_absolute())
    return value


def network(value):
    result={'version':2,'ethernets':{'ha0':{'match':{'macaddress':value['mac_address']},'set-name':'ha0',
        'dhcp4':False,'dhcp6':False,'accept-ra':False,'link-local':[],
        'addresses':[value['ipv4_cidr']]}}}
    if value.get('schema_version')==2 and value.get('egress_mac_address') is not None:
        result['renderer']='networkd'
        result['ethernets']['ha-egress']={'match':{'macaddress':value['egress_mac_address']},'set-name':'ha-egress',
            'dhcp4':True,'dhcp6':False,'accept-ra':False,'link-local':[],'optional':True,
            'dhcp4-overrides':{'route-metric':500,'use-hostname':False,'send-hostname':False}}
    return result


def unit():
    return '''[Unit]
Description=Fresh HA guest offline seed acceptance
After=cloud-final.service
Wants=cloud-final.service
ConditionPathExists=/etc/n100-ha-seed/config.json
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/n100-ha-seed/guest_seed.py guest --config /etc/n100-ha-seed/config.json
TimeoutStartSec=1800
StandardOutput=null
StandardError=null
[Install]
WantedBy=cloud-init.target
'''


def render(value,key,source,os_hash):
    config={k:value[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256',
                                 'expected_kernel_release','operator_public_key_sha256')}
    config.update(schema_version=1,guest_source_sha256=hashlib.sha256(source).hexdigest(),ubuntu_image_sha256=os_hash)
    intent={k:value[k] for k in ('source_commit','installation_id','node')}
    intent['ubuntu_image_sha256']=os_hash
    files=[{'path':'/etc/apt/apt.conf.d/99n100-ha-seed-periodic','owner':'root:root','permissions':'0600',
            'content':'APT::Periodic::Enable "0";\n'},
        {'path':'/etc/n100-ha-seed/config.json','owner':'root:root','permissions':'0600','content':encode(config).decode()},
        {'path':'/etc/n100-ha-bootstrap-intent.json','owner':'root:root','permissions':'0600','content':encode(intent).decode()},
        {'path':'/opt/n100-ha-seed/guest_seed.py','owner':'root:root','permissions':'0400','encoding':'b64',
         'content':base64.b64encode(source).decode()},
        {'path':'/etc/systemd/system/n100-ha-seed.service','owner':'root:root','permissions':'0444','content':unit()},
        {'path':'/etc/ssh/sshd_config.d/00-n100-ha-seed.conf','owner':'root:root','permissions':'0444',
         'content':'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nAllowUsers root\n'}]
    cloud={'users':[{'name':'root','lock_passwd':True,'ssh_authorized_keys':[key]}],
        'disable_root':False,'ssh_pwauth':False,'ssh_deletekeys':False,'ssh_genkeytypes':['ed25519'],
        'ssh_quiet_keygen':True,'no_ssh_fingerprints':True,'ssh_publish_hostkeys':{'enabled':False},
        'package_update':False,'package_upgrade':False,'package_reboot_if_required':False,
        'output':cloud_output_config(),'write_files':files,
        'runcmd':[['systemctl','daemon-reload'],['systemctl','enable','n100-ha-seed.service'],
                  ['systemctl','start','--no-block','n100-ha-seed.service']],
        'final_message':'HA seed cloud-init stage finished; guest acceptance is separate.'}
    return {'meta-data':encode({'instance-id':'n100-ha-seed-'+value['seed_id'],
                'local-hostname':'ha-'+value['node'].lower()+'-'+value['seed_id'][:12]}),
            'network-config':encode(network(value)),
            'user-data':b'#cloud-config\n'+encode(cloud),'guest_seed.py':source}


def verify_tree(tree,expected,verifier):
    need(type(expected) is dict and type(expected.get('bootstrap_manifest_sha256')) is str
         and HEX.fullmatch(expected['bootstrap_manifest_sha256']),'external_bootstrap_manifest_pin_required')
    tree=Path(tree);actual=tree_files(tree)
    need(sha(regular(tree/'bundle/manifest.json'))==expected['bootstrap_manifest_sha256'],'bootstrap_manifest_hash_mismatch')
    manifest=json.loads(regular(tree/'seed-manifest.json').read_bytes())
    need(set(manifest)=={'schema_version','source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256',
                         'guest_source_sha256','expected_kernel_release','files'},'seed_manifest_fields_invalid')
    need(type(manifest['schema_version']) is int and manifest['schema_version']==1)
    # Host callers retain prepare's complete manifest as an external pin.
    # Guest config intentionally pins identity and the bootstrap manifest only.
    if 'files' in expected:
        need(type(expected['files']) is dict and manifest==expected,'seed_manifest_differs_from_external_pin')
    need(all(manifest[k]==expected[k] for k in ('source_commit','installation_id','node','seed_id',
        'bootstrap_manifest_sha256','guest_source_sha256','expected_kernel_release')),'guest_seed_identity_mismatch')
    need(type(manifest['files']) is dict and manifest['files'],'seed_inventory_missing')
    required={'meta-data','user-data','network-config','guest_seed.py','bundle/manifest.json','bundle/bootstrap.py'}
    need(required<=set(manifest['files']),'seed_inventory_missing')
    for name,expected in manifest['files'].items():
        need(type(name) is str and not name.startswith('/') and '..' not in Path(name).parts and '\\' not in name
             and type(expected) is str and HEX.fullmatch(expected),'unsafe_seed_path')
        need(sha(regular(tree/name))==expected,'seed_file_hash_mismatch')
    need(actual==set(manifest['files'])|{'seed-manifest.json'},'seed_inventory_mismatch')
    need(sha(tree/'guest_seed.py')==manifest['guest_source_sha256'],'seed_source_hash_mismatch')
    need(sha(tree/'bundle/manifest.json')==manifest['bootstrap_manifest_sha256'],'bootstrap_manifest_hash_mismatch')
    # Authenticate executable bootstrap bytes through the independently pinned
    # bundle manifest before importing them. Imports must not create pycache.
    bundle_manifest=json.loads(regular(tree/'bundle/manifest.json').read_bytes())
    need(bundle_manifest.get('files',{}).get('bootstrap.py')==sha(regular(tree/'bundle/bootstrap.py')),
         'bootstrap_executable_hash_mismatch')
    need(getattr(verifier,'__source_sha256__',None)==sha(tree/'bundle/bootstrap.py'),'trusted_bootstrap_verifier_mismatch')
    locked,_=verifier.verify(tree/'bundle')
    need(all(manifest[k]==locked[k] for k in ('source_commit','installation_id','node')),'seed_bundle_identity_mismatch')
    return manifest


def prepare(spec_path,bundle,output):
    # Parse the private external pins before executing ANY bootstrap module.
    value=json.loads(regular(spec_path,private=True).read_bytes())
    need(type(value) is dict and type(value.get('source_commit')) is str and COMMIT.fullmatch(value['source_commit'])
         and type(value.get('bootstrap_manifest_sha256')) is str and HEX.fullmatch(value['bootstrap_manifest_sha256']),
         'external_source_and_manifest_pins_required')
    source=git_source(value['source_commit'],SOURCE_PATH)
    need(source and hashlib.sha256(source).hexdigest()==sha(regular(__file__))
         and hashlib.sha256(source).hexdigest()==sha(regular(ROOT/SOURCE_PATH)),'seed_source_worktree_differs_from_commit')
    bootstrap_source=git_source(value['source_commit'],'infra/k8s/ha-package/bootstrap.py')
    tree_files(bundle)
    need(sha(regular(Path(bundle)/'manifest.json'))==value['bootstrap_manifest_sha256'],'bootstrap_manifest_hash_mismatch')
    bundle_manifest=json.loads(regular(Path(bundle)/'manifest.json').read_bytes())
    need(bundle_manifest.get('files',{}).get('bootstrap.py')==hashlib.sha256(bootstrap_source).hexdigest(),
         'bundle_bootstrap_not_exact_git_source')
    module=source_builder(bootstrap_source,ROOT/'infra/k8s/ha-package/bootstrap.py')
    lock,bundle_manifest=module.verify(bundle)
    validate_spec(value,lock)
    key=public_key(value['operator_public_key_path'],value['operator_public_key_sha256'])
    output=Path(output).absolute()
    need(not any(p.is_symlink() for p in (output,*output.parents)),'non_symlink_output_required')
    output.mkdir(mode=0o700,exist_ok=False);tree=output/'tree';tree.mkdir(mode=0o700)
    copy_manifest(bundle,tree/'bundle',{**bundle_manifest['files'],'manifest.json':value['bootstrap_manifest_sha256']})
    for name,raw in render(value,key,source,lock['ubuntu_image']['sha256']).items():
        path=tree/name;path.write_bytes(raw);path.chmod(0o400)
    files={name:sha(regular(tree/name)) for name in tree_files(tree)}
    manifest={k:value[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256','expected_kernel_release')}
    manifest.update(schema_version=1,guest_source_sha256=hashlib.sha256(source).hexdigest(),files=files)
    (tree/'seed-manifest.json').write_bytes(encode(manifest));(tree/'seed-manifest.json').chmod(0o400)
    verify_tree(tree,expected=manifest,verifier=module)
    return tree,manifest


def create_iso(tree,output,expected):
    need(sys.platform=='darwin' and Path('/usr/bin/hdiutil').is_file(),'mac_hdiutil_required')
    need(type(expected) is dict,'external_source_and_manifest_pins_required')
    need(type(expected.get('files')) is dict and expected['files'],'external_complete_seed_manifest_required')
    need(type(expected.get('bootstrap_manifest_sha256')) is str and HEX.fullmatch(expected['bootstrap_manifest_sha256']),
         'external_bootstrap_manifest_pin_required')
    guest_source=git_source(expected.get('source_commit'),SOURCE_PATH)
    need(hashlib.sha256(guest_source).hexdigest()==expected.get('guest_source_sha256')
         and hashlib.sha256(guest_source).hexdigest()==sha(regular(__file__)),'seed_source_worktree_differs_from_commit')
    trusted=source_builder(git_source(expected['source_commit'],'infra/k8s/ha-package/bootstrap.py'),
                           ROOT/'infra/k8s/ha-package/bootstrap.py')
    tree=Path(tree).absolute();output=Path(output).absolute();manifest=verify_tree(tree,expected=expected,verifier=trusted)
    need(not output.exists() and not any(p.is_symlink() for p in (output,*output.parents)),'new_iso_output_required')
    run(['/usr/bin/hdiutil','makehybrid','-iso','-joliet','-iso-volume-name','CIDATA','-joliet-volume-name','CIDATA',
         '-quiet','-o',str(output),str(tree)],'seed_iso_build_failed',600)
    regular(output).chmod(0o600)
    # Read back the actual filesystem, not only the source staging directory.
    mount=output.parent/'iso-readback';mount.mkdir(mode=0o700,exist_ok=False)
    device=None
    try:
        response=plistlib.loads(run(['/usr/bin/hdiutil','attach',str(output),'-readonly','-noautoopen','-nobrowse',
                                   '-mountpoint',str(mount),'-plist'],'seed_iso_attach_failed',60))
        entities=response.get('system-entities',[])
        mounted=[entry for entry in entities if entry.get('mount-point')==str(mount)]
        need(len(mounted)==1,'seed_iso_mount_identity_invalid')
        candidates=[entry.get('dev-entry','') for entry in entities if re.fullmatch(r'/dev/disk[0-9]+',entry.get('dev-entry',''))]
        need(len(candidates)==1,'seed_iso_device_identity_invalid');device=candidates[0]
        info=plistlib.loads(run(['/usr/sbin/diskutil','info','-plist',str(mount)],'seed_iso_volume_query_failed',30))
        need(info.get('VolumeName')=='CIDATA','seed_iso_label_mismatch')
        readback=verify_tree(mount,expected=manifest,verifier=trusted);need(readback==manifest,'seed_iso_readback_mismatch')
        receipt={'status':'PASS','scope':'mac_seed_iso_creation_and_readback_not_vm_boot',
            'source_commit':manifest['source_commit'],'installation_id':manifest['installation_id'],
            'node':manifest['node'],'seed_id':manifest['seed_id'],'iso_sha256':sha(output),'volume_label':'CIDATA',
            'file_count':len(manifest['files'])+1,'seed_manifest_sha256':sha(tree/'seed-manifest.json'),
            'bootstrap_manifest_sha256':manifest['bootstrap_manifest_sha256'],'readback_hashes_verified':True,
            'production_access':False,'vm_boot_verified':False,'kernel_boot_verified':False,
            'pg_started':False,'writer_authority_issued':False}
    finally:
        if device is not None:run(['/usr/bin/hdiutil','detach',device,'-quiet'],'seed_iso_detach_failed',60)
    (output.parent/'iso-receipt.json').write_bytes(encode(receipt));(output.parent/'iso-receipt.json').chmod(0o600)
    return receipt


def atomic(path,value):
    path=Path(path);temporary=path.with_name(path.name+'.tmp')
    need(not temporary.exists() and not temporary.is_symlink(),'unexpected_state_temporary_file')
    temporary.write_bytes(encode(value));temporary.chmod(0o600);temporary.replace(path)
    with path.open('rb') as stream:os.fsync(stream.fileno())
    directory=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def reboot_action(state,boot_hash,kernel,expected):
    need(type(state) is dict,'invalid_guest_state')
    phase=state.get('phase')
    if phase=='new':return 'install'
    if phase=='awaiting_reboot':
        need(type(state.get('reboot_requests')) is int and state['reboot_requests']==1 and state.get('boot_id_sha256')!=boot_hash,'new_guest_reboot_not_observed')
        need(kernel==expected,'expected_kernel_not_booted')
        return 'verify_postboot'
    if phase=='complete':return 'already_complete'
    raise SeedError('terminal_guest_failure_no_retry')


def state_action(state,config,boot_hash,kernel,*,state_exists):
    identity={k:config[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256')}
    if state_exists:
        need(state.get('phase')!='new' and all(state.get(k)==value for k,value in identity.items()),
             'existing_guest_state_identity_mismatch')
    else:need(state=={'phase':'new'},'unexpected_initial_guest_state')
    if state.get('phase')=='complete':
        need(state.get('boot_id_sha256')==boot_hash and kernel==config['expected_kernel_release'],
             'completed_seed_receipt_is_from_another_boot')
    return reboot_action(state,boot_hash,kernel,config['expected_kernel_release'])


def cloud_status():
    # Called only after cloud-final.service, never from cloud-init runcmd.
    value=json.loads(run(['cloud-init','status','--format','json'],'cloud_init_failed',30))
    need(value.get('status')=='done' and value.get('extended_status','done')=='done','cloud_init_not_cleanly_done')
    return 0


def kernel_and_packages(module,lock,expected):
    need(not run(['dpkg','--audit'],'offline_package_audit_failed').strip(),'offline_package_audit_not_empty')
    for row in module.packages(lock):
        installed=run(['dpkg-query','--show','--showformat=${db:Status-Status}\t${Version}\t${Architecture}',row['package']],
                      'installed_package_query_failed').decode()
        need(installed=='\t'.join(('installed',row['version'],row['architecture'])),'offline_package_version_mismatch')
    need(regular('/boot/vmlinuz-'+expected).stat().st_size>0,'expected_kernel_file_missing')


def postboot_checks(module,lock,expected):
    need(os.uname().release==expected,'expected_kernel_not_booted')
    periodic=regular('/etc/apt/apt.conf.d/99n100-ha-seed-periodic',private=True)
    need(periodic.read_text()=='APT::Periodic::Enable "0";\n','automatic_os_update_policy_mismatch')
    kernel_and_packages(module,lock,expected)
    run(['systemctl','start','docker.service'],'fresh_guest_engine_start_failed')
    info=json.loads(run(['docker','info','--format','{{json .}}'],'fresh_guest_engine_query_failed'))
    need(info.get('Driver')=='overlay2' and 'io.containerd.snapshotter.v1' not in str(info.get('DriverStatus')),
         'classic_overlay2_required')
    images=json.loads(run(['docker','image','inspect',lock['postgres']['config_digest']],'pg_image_query_failed'))
    need(len(images)==1 and images[0].get('Id')==lock['postgres']['config_digest'],'pg_image_mismatch')
    need(not run(['docker','ps','--all','--quiet'],'guest_container_query_failed').strip(),'pg_must_remain_not_started')
    need(not run(['ss','-H','-ltn','sport = :5432'],'pg_listener_query_failed').strip(),'unexpected_pg_listener')
    data=Path('/var/lib/n100-ha/pgdata')
    need(data.is_dir() and not data.is_symlink() and not any(data.iterdir()),'pg_data_must_remain_empty')
    socket=Path('/run/n100-ha/pg')
    need(not any(parent.is_symlink() for parent in (socket,*socket.parents)),'unsafe_pg_socket_path')
    if not socket.exists():
        socket.mkdir(mode=0o700,parents=True);os.chown(socket,lock['postgres']['uid'],lock['postgres']['gid'])
    info=socket.stat()
    need(info.st_uid==lock['postgres']['uid'] and info.st_gid==lock['postgres']['gid'] and info.st_mode & 0o777==0o700
         and not any(socket.iterdir()),'pg_socket_identity_mismatch')


def server_key_record(text):
    parts=text.strip().split()
    need(len(parts)>=2 and parts[0]=='ssh-ed25519' and not any(c in text.strip() for c in '\r\n\0'),
         'guest_server_public_key_missing')
    try:raw=base64.b64decode(parts[1],validate=True)
    except ValueError:raise SeedError('guest_server_key_invalid') from None
    need(len(raw)==51 and raw[:4]==b'\x00\x00\x00\x0b' and raw[4:15]==b'ssh-ed25519'
         and raw[15:19]==b'\x00\x00\x00\x20','guest_server_key_invalid')
    return parts[0]+' '+parts[1],'SHA256:'+base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')


def server_key():
    # Read the public file only. The SSH server's private key never leaves guest.
    return server_key_record(regular('/etc/ssh/ssh_host_ed25519_key.pub').read_text())


def serial_payload(receipt):
    identity={'source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256','expected_kernel_release'}
    fail=identity|{'status','reason','production_deployed','pg_started','writer_authority_issued'}
    complete=identity|{'status','scope','cloud_init_status_exit','kernel_boot_verified','guest_reboot_requests',
        'new_boot_id_observed','boot_id_sha256','automatic_os_updates_disabled','package_count','pg_image','pg_state','pg_started','writer_authority_issued',
        'provider_credentials_supplied_by_seed','effects_transition_performed','production_deployed','server_public_key',
        'server_key_fingerprint','serial_delivery_is_not_host_receipt_verification'}
    need(type(receipt) is dict and receipt.get('status') in ('PASS','FAIL'),'serial_receipt_invalid')
    fields=set(receipt)
    need(fields==complete if receipt['status']=='PASS' else fields in (fail,fail|{'server_public_key','server_key_fingerprint'}),
         'serial_receipt_fields_invalid')
    need(type(receipt['source_commit']) is str and COMMIT.fullmatch(receipt['source_commit']) and receipt['node'] in ('A','B'),'serial_receipt_identity_invalid')
    need(all(type(receipt[k]) is str and HEX.fullmatch(receipt[k]) for k in
        ('installation_id','seed_id','bootstrap_manifest_sha256')),'serial_receipt_identity_invalid')
    need(type(receipt['expected_kernel_release']) is str and re.fullmatch(r'6\.8\.0-[0-9]+-generic',receipt['expected_kernel_release']),'serial_receipt_identity_invalid')
    need(all(receipt[k] is False for k in ('production_deployed','pg_started','writer_authority_issued')),'serial_receipt_unsafe')
    if receipt['status']=='FAIL':
        need(type(receipt['reason']) is str and re.fullmatch(r'[a-z0-9_]{1,80}',receipt['reason']),'serial_reason_not_static')
        if 'server_public_key' in receipt:
            key,fingerprint=server_key_record(receipt['server_public_key'])
            need(key==receipt['server_public_key'] and fingerprint==receipt['server_key_fingerprint'],'serial_server_key_mismatch')
    else:
        key,fingerprint=server_key_record(receipt['server_public_key'])
        need(key==receipt['server_public_key'] and fingerprint==receipt['server_key_fingerprint'],'serial_server_key_mismatch')
        need(type(receipt['guest_reboot_requests']) is int and receipt['guest_reboot_requests']==1
             and receipt['new_boot_id_observed'] is True and receipt['kernel_boot_verified'] is True
             and type(receipt['cloud_init_status_exit']) is int and receipt['cloud_init_status_exit']==0 and receipt['automatic_os_updates_disabled'] is True,'serial_boot_not_accepted')
        need(type(receipt['boot_id_sha256']) is str and HEX.fullmatch(receipt['boot_id_sha256'])
             and type(receipt['package_count']) is int and 0<receipt['package_count']<=1152
             and re.fullmatch(r'sha256:[0-9a-f]{64}',receipt['pg_image']),'serial_runtime_identity_invalid')
        need(receipt['scope']=='new_guest_os_engine_and_pg_image_not_started'
             and receipt['pg_state']=='installed-not-started'
             and receipt['provider_credentials_supplied_by_seed'] is False
             and receipt['effects_transition_performed'] is False
             and receipt['serial_delivery_is_not_host_receipt_verification'] is True,'serial_scope_invalid')
    payload=(SERIAL_PREFIX+json.dumps(receipt,sort_keys=True,separators=(',',':'))+'\n').encode()
    need(len(payload)<=4096,'serial_receipt_too_large')
    return payload


def emit_serial(receipt):
    # Host must bind COM1 ACL/VM identity and pin this public key before SSH.
    payload=serial_payload(receipt)
    path=Path('/dev/ttyS0')
    need(not path.is_symlink() and stat.S_ISCHR(path.lstat().st_mode),'trusted_guest_serial_required')
    descriptor=os.open(path,os.O_WRONLY|os.O_NOCTTY|os.O_NONBLOCK|os.O_NOFOLLOW)
    try:need(os.write(descriptor,payload)==len(payload),'serial_receipt_incomplete')
    finally:os.close(descriptor)


def guest(config_path):
    import fcntl
    need(os.geteuid()==0 and os.uname().machine=='x86_64','fresh_amd64_guest_root_required')
    config=json.loads(regular(config_path,private=True).read_bytes())
    fields={'schema_version','source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256',
            'expected_kernel_release','operator_public_key_sha256','guest_source_sha256','ubuntu_image_sha256'}
    need(set(config)==fields and type(config['schema_version']) is int and config['schema_version']==1,'guest_config_invalid')
    need(type(config['source_commit']) is str and COMMIT.fullmatch(config['source_commit']) and config['node'] in ('A','B'),'guest_config_invalid')
    need(all(type(config[k]) is str and HEX.fullmatch(config[k]) for k in ('installation_id','seed_id','bootstrap_manifest_sha256',
             'operator_public_key_sha256','guest_source_sha256','ubuntu_image_sha256')),'guest_config_invalid')
    need(type(config['expected_kernel_release']) is str and re.fullmatch(r'6\.8\.0-[0-9]+-generic',config['expected_kernel_release']),'guest_config_invalid')
    need(sha(regular(__file__))==config['guest_source_sha256'],'guest_source_hash_mismatch')
    GUEST_ROOT.mkdir(mode=0o700,exist_ok=True)
    need(not GUEST_ROOT.is_symlink() and GUEST_ROOT.stat().st_uid==0 and GUEST_ROOT.stat().st_mode & 0o077==0,'private_guest_state_required')
    lock_path=GUEST_ROOT/'lock'
    descriptor=os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    mounted=False
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state_path=GUEST_ROOT/'state.json'
        state=json.loads(regular(state_path,private=True).read_bytes()) if state_path.exists() else {'phase':'new'}
        identity={k:config[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256')}
        boot_hash=hashlib.sha256(regular('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()
        action=state_action(state,config,boot_hash,os.uname().release,state_exists=state_path.exists())
        if action=='already_complete':return json.loads(regular(GUEST_ROOT/'receipt.json',private=True).read_bytes())
        cloud_status()
        devices=run(['blkid','-t','LABEL=CIDATA','-o','device'],'seed_block_device_missing',30).decode().strip().splitlines()
        need(len(devices)==1 and re.fullmatch(r'/dev/sr[0-9]+',devices[0]),'single_seed_cdrom_required')
        MOUNT.parent.mkdir(mode=0o700,exist_ok=True)
        need(not MOUNT.parent.is_symlink() and MOUNT.parent.stat().st_uid==0
             and MOUNT.parent.stat().st_mode & 0o777==0o700,'private_seed_mount_parent_required')
        MOUNT.mkdir(mode=0o700,exist_ok=False)
        run(['mount','-t','iso9660','-o','ro,nosuid,nodev,noexec',devices[0],str(MOUNT)],'seed_iso_mount_failed',30);mounted=True
        tree_files(MOUNT)
        need(sha(regular(MOUNT/'bundle/manifest.json'))==config['bootstrap_manifest_sha256'],'bootstrap_manifest_hash_mismatch')
        authenticated=json.loads(regular(MOUNT/'bundle/manifest.json').read_bytes())
        code_pin=authenticated.get('files',{}).get('bootstrap.py')
        trusted=builder(MOUNT/'bundle/bootstrap.py',code_pin)
        manifest=verify_tree(MOUNT,expected=config,verifier=trusted)
        need(all(manifest[k]==config[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256',
                                                'guest_source_sha256','expected_kernel_release')),'guest_seed_identity_mismatch')
        if action=='install':
            need(not GUEST_BUNDLE.exists(),'fresh_guest_bundle_target_required')
            pinned=json.loads(regular(MOUNT/'bundle/manifest.json').read_bytes())
            copy_manifest(MOUNT/'bundle',GUEST_BUNDLE,{**pinned['files'],'manifest.json':config['bootstrap_manifest_sha256']})
            GUEST_BUNDLE.parent.chmod(0o700);GUEST_BUNDLE.chmod(0o700)
        need(sha(regular(GUEST_BUNDLE/'manifest.json'))==config['bootstrap_manifest_sha256'],'bootstrap_manifest_hash_mismatch')
        module=builder(GUEST_BUNDLE/'bootstrap.py',code_pin);locked,_=module.verify(GUEST_BUNDLE)
        if action=='install':
            atomic(state_path,{'phase':'installing',**identity,'reboot_requests':0})
            run(['systemctl','mask','--now','apt-daily.timer','apt-daily-upgrade.timer'],
                'fresh_guest_apt_timer_mask_failed',30)
            run(['systemctl','stop','apt-daily.service','apt-daily-upgrade.service'],
                'fresh_guest_apt_service_stop_failed',60)
            module.install(GUEST_BUNDLE,config['bootstrap_manifest_sha256'])
            kernel_and_packages(module,locked,config['expected_kernel_release'])
            pending={'phase':'awaiting_reboot',**identity,'reboot_requests':1,'boot_id_sha256':boot_hash}
            atomic(state_path,pending)
            receipt={'status':'PENDING_REBOOT','seed_id':config['seed_id'],'kernel_boot_verified':False,'pg_started':False,
                     'writer_authority_issued':False,'production_deployed':False}
            atomic(GUEST_ROOT/'receipt.json',receipt)
            run(['systemctl','reboot','--no-wall','--no-block'],'new_guest_reboot_request_failed',30)
            return receipt
        postboot_checks(module,locked,config['expected_kernel_release'])
        key,fingerprint=server_key()
        receipt={k:config[k] for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256','expected_kernel_release')}
        receipt.update(status='PASS',scope='new_guest_os_engine_and_pg_image_not_started',cloud_init_status_exit=0,
            kernel_boot_verified=True,guest_reboot_requests=1,new_boot_id_observed=True,boot_id_sha256=boot_hash,automatic_os_updates_disabled=True,
            package_count=len(module.packages(locked)),pg_image=locked['postgres']['config_digest'],
            pg_state='installed-not-started',pg_started=False,writer_authority_issued=False,
            provider_credentials_supplied_by_seed=False,effects_transition_performed=False,
            production_deployed=False,server_public_key=key,server_key_fingerprint=fingerprint,
            serial_delivery_is_not_host_receipt_verification=True)
        run(['systemctl','disable','n100-ha-seed.service'],'seed_service_disable_failed',30)
        atomic(GUEST_ROOT/'receipt.json',receipt)
        atomic(state_path,{'phase':'complete',**identity,'reboot_requests':1,'boot_id_sha256':boot_hash})
        emit_serial(receipt)
        return receipt
    except Exception as error:
        receipt={'status':'FAIL','reason':str(error) if isinstance(error,SeedError) else ('bootstrap_'+str(error) if 'module' in locals()
                     and isinstance(error,module.BootstrapError) and re.fullmatch(r'[a-z0-9_]{1,60}',str(error)) else 'guest_seed_failed'),
                 **{k:config.get(k) for k in ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256','expected_kernel_release')},
                 'production_deployed':False,'pg_started':False,'writer_authority_issued':False}
        try:
            key,fingerprint=server_key()
            receipt.update(server_public_key=key,server_key_fingerprint=fingerprint)
        except Exception:pass
        atomic(GUEST_ROOT/'receipt.json',receipt)
        atomic(GUEST_ROOT/'state.json',{'phase':'failed',**{k:config.get(k) for k in
            ('source_commit','installation_id','node','seed_id','bootstrap_manifest_sha256')}})
        # Static failure status may be collected from the same trusted VM serial
        # path; never fall back to raw cloud-init/subprocess output.
        try:emit_serial(receipt)
        except Exception:pass
        raise SeedError(receipt['reason']) from None
    finally:
        if mounted:run(['umount',str(MOUNT)],'seed_iso_unmount_failed',30)
        if MOUNT.exists() and not os.path.ismount(MOUNT):MOUNT.rmdir()
        os.close(descriptor)


def main():
    parser=argparse.ArgumentParser(description=__doc__);subs=parser.add_subparsers(dest='action',required=True)
    p=subs.add_parser('prepare');p.add_argument('--spec',required=True);p.add_argument('--bundle',required=True);p.add_argument('--output',required=True)
    p=subs.add_parser('guest');p.add_argument('--config',default=str(GUEST_CONFIG))
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            tree,expected=prepare(args.spec,args.bundle,args.output);result=create_iso(tree,Path(args.output)/'seed.iso',expected)
        else:result=guest(args.config)
    except Exception as error:
        result={'status':'FAIL','reason':str(error) if isinstance(error,SeedError) else 'guest_seed_failed',
                'production_deployed':False,'writer_authority_issued':False}
    print(json.dumps(result,sort_keys=True))
    return int(result['status'] not in ('PASS','PENDING_REBOOT'))


if __name__=='__main__':raise SystemExit(main())
