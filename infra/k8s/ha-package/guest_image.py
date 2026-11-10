#!/usr/bin/env python3
"""Build fresh public guest image copies with matching early cloud-init output."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import uuid
import zlib

LOCAL=Path(__file__).resolve()
ROOT=LOCAL.parents[3]
_source=importlib.util.spec_from_file_location('ha_image_seed_output',LOCAL.with_name('guest_seed.py'))
SEED=importlib.util.module_from_spec(_source);_source.loader.exec_module(SEED)
PROFILE='cloud-init-output-preseed-v1'
FORMAT='ha-guest-node-image-derivation-v1'
PATCH_PATH='/etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg'
UPSTREAM_SHA='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
QEMU_SHA='9700138fb0464bdf494e3fa6a1a4368abdc44b5edc847fcd2ae3a7b94917a11e'
QEMU_VERSION='qemu-img version 11.1.3'
UTILITY_IMAGE='sha256:ded2c932b40c38c99f0b23b9edc29e160e23689db2d92cdbe251b693ca76be1b'
VIRTUAL_BYTES=24*1024**3
PATCH_SHA='659e515ee99507c6990b06c3a0560a794070514de74a9506e6ee70da20bc77c8'
SPEC_FIELDS={'schema_version','source_commit','upstream_qcow_path','qemu_img_path','qemu_img_sha256','qemu_img_version','docker_context','docker_daemon_identity_sha256','utility_image_config_digest'}
RECEIPT_FIELDS={'schema_version','format','profile','source_commit','node','upstream_qcow_sha256','derived_qcow_sha256','node_vhdx_sha256','node_vhdx_bytes','node_disk_identifier','patch_path','patch_sha256','patch_bytes','patch_uid','patch_gid','patch_mode','filesystem_check_exit','root_partition_offset','root_partition_bytes','upstream_unchanged','initial_and_userdata_output_match','qemu_img_version','qemu_img_sha256','utility_image_config_digest','docker_context','docker_daemon_identity_sha256','debugfs_version','e2fsck_version'}

class ImageError(Exception):pass

def need(value,code):
    if not value:raise ImageError(code)

def digest(raw):return hashlib.sha256(raw).hexdigest()
def file_hash(path):
    with path.open('rb') as stream:
        h=hashlib.sha256()
        for raw in iter(lambda:stream.read(1024*1024),b''):h.update(raw)
    return h.hexdigest()
def regular(path,private=False):
    need(path.is_absolute(),'absolute_image_path_required');entry=path.lstat()
    need(stat.S_ISREG(entry.st_mode) and entry.st_nlink==1 and not(entry.st_mode&0o022),'image_regular_input_required')
    if private:need(entry.st_uid==os.geteuid() and not(entry.st_mode&0o077),'private_image_spec_required')
    return path

def encode(value):return (json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
def patch_bytes():
    raw=SEED.early_config_bytes()
    need(len(raw)==61 and digest(raw)==PATCH_SHA and SEED.cloud_output_config()=={'all':'> /dev/null'},'image_output_profile_changed')
    return raw

def strict_json(raw):
    def pairs(values):
        result={}
        for key,value in values:
            need(key not in result,'image_duplicate_spec_key');result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs)
def write_new(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())

def validate_spec(value):
    need(type(value) is dict and set(value)==SPEC_FIELDS,'image_spec_fields')
    need(type(value['schema_version']) is int and value['schema_version']==1,'image_spec_version')
    need(all(type(value[k]) is str and 0<len(value[k])<=4096 for k in SPEC_FIELDS-{'schema_version'}),'image_spec_types')
    need(re.fullmatch('[0-9a-f]{40}',value['source_commit']) is not None,'image_source_commit')
    need(value['qemu_img_sha256']==QEMU_SHA and value['qemu_img_version']==QEMU_VERSION,'image_qemu_pin')
    need(value['utility_image_config_digest']==UTILITY_IMAGE,'image_utility_pin')
    need(re.fullmatch('colima-codex-ha-[0-9a-f]{8}',value['docker_context']) is not None,'owned_colima_context_required')
    need(re.fullmatch('[0-9a-f]{64}',value['docker_daemon_identity_sha256']) is not None and value['docker_daemon_identity_sha256']!='0'*64,'image_daemon_pin')
    for k in ('upstream_qcow_path','qemu_img_path'):
        need(Path(value[k]).is_absolute() and not any(x in value[k] for x in ('\n','\r','\x00',',')),'image_input_path')
    need(Path(value['upstream_qcow_path']).name=='ubuntu-24.04-server-cloudimg-amd64.img','original_public_qcow_only')
    return value

def run(argv,reason,timeout=180,allowed=(0,)):
    try:result=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,timeout=timeout)
    except (OSError,subprocess.SubprocessError):raise ImageError(reason) from None
    need(result.returncode in allowed and len(result.stdout)<=2*1024*1024 and len(result.stderr)<=2*1024*1024,reason)
    return result.stdout

def verify_source(commit):
    for path in (LOCAL,LOCAL.with_name('guest_seed.py')):
        relative=path.relative_to(ROOT).as_posix();raw=run(['git','-C',str(ROOT),'show',commit+':'+relative],'image_exact_source_required')
        need(raw==path.read_bytes(),'image_exact_source_required')

def docker_prefix(value):
    executable=shutil.which('docker');need(executable is not None,'docker_cli_required')
    return [executable,'--context',value['docker_context']]

def verify_runtime(value,prefix):
    identity=run(prefix+['info','--format','{{.ID}}'],'owned_daemon_query_failed').strip()
    need(0<len(identity)<=256 and digest(identity)==value['docker_daemon_identity_sha256'],'owned_daemon_changed')
    endpoint=json.loads(run(prefix+['context','inspect',value['docker_context'],'--format','{{json .Endpoints.docker.Host}}'],'owned_context_query_failed'))
    need(type(endpoint) is str and endpoint.startswith('unix://') and value['docker_context'][7:] in Path(endpoint[7:]).parts and '.colima' in Path(endpoint[7:]).parts,'local_owned_daemon_only')
    images=json.loads(run(prefix+['image','inspect',UTILITY_IMAGE],'image_utility_inspect_failed'))
    need(len(images)==1 and images[0]['Id']==UTILITY_IMAGE and images[0]['Os']=='linux' and images[0]['Architecture']=='amd64','image_utility_changed')

def qemu_info(tool,path):
    value=json.loads(run([str(tool),'info','--output=json',str(path)],'qemu_info_failed'))
    need(type(value.get('virtual-size')) is int and value['virtual-size']>0 and not value.get('backing-filename'),'standalone_image_required')
    return value

def root_partition(raw):
    with raw.open('rb') as stream:
        size=raw.stat().st_size;need(size%512==0,'public_raw_size')
        stream.seek(512);header=stream.read(512);need(header[:8]==b'EFI PART','public_GPT_signature')
        hsize,crc=struct.unpack_from('<II',header,12);need(92<=hsize<=512,'public_GPT_size');check=bytearray(header[:hsize]);check[16:20]=b'\0'*4
        need(zlib.crc32(check)&0xffffffff==crc,'public_GPT_CRC');current,other,first,last=struct.unpack_from('<QQQQ',header,24)
        need(current==1 and other==size//512-1 and 2<=first<=last<other,'public_GPT_bounds')
        lba,count,width,crc=struct.unpack_from('<QIII',header,72);need(1<=count<=128 and width==128 and lba*512+count*width<=first*512,'public_GPT_table_bounds')
        stream.seek(lba*512);entries=stream.read(count*width);need(len(entries)==count*width and zlib.crc32(entries)&0xffffffff==crc,'public_GPT_table_CRC')
        roots=[];partitions=[]
        for i in range(count):
            entry=entries[i*width:(i+1)*width]
            if entry[:16]==b'\0'*16:continue
            a,b=struct.unpack_from('<QQ',entry,32);need(first<=a<=b<=last,'public_partition_bounds');partitions.append((a,b))
            if str(uuid.UUID(bytes_le=entry[:16])) in ('0fc63daf-8483-4772-8e79-3d69d8477de4','4f68bce3-e8cd-4db1-96e7-fbcaf984b709'):roots.append((a,b))
        ordered=sorted(partitions);need(all(ordered[i][1]<ordered[i+1][0] for i in range(len(ordered)-1)),'public_partition_overlap')
        need(len(roots)==1 and roots[0][0]*512==1074790400,'public_root_partition_required')
        a,b=roots[0];return a*512,(b-a+1)*512

def sparse_partition_copy(source,target,offset,length):
    with source.open('rb') as src,target.open('xb') as dst:
        os.chmod(target,0o600);src.seek(offset);remaining=length
        while remaining:
            raw=src.read(min(1024*1024,remaining));need(bool(raw),'public_partition_short_read')
            if any(raw):dst.write(raw)
            else:dst.seek(len(raw),os.SEEK_CUR)
            remaining-=len(raw)
        dst.truncate(length);dst.flush();os.fsync(dst.fileno())

def insert_partition(raw,partition,offset,length):
    need(partition.stat().st_size==length,'patched_partition_length_changed')
    with raw.open('r+b') as dst,partition.open('rb') as src:
        dst.seek(offset);shutil.copyfileobj(src,dst,1024*1024);dst.flush();os.fsync(dst.fileno())

def patch_commands():
    return ('write /work/early-output.cfg '+PATCH_PATH+'\nset_inode_field '+PATCH_PATH+' uid 0\nset_inode_field '+PATCH_PATH+' gid 0\nset_inode_field '+PATCH_PATH+' mode 0100644\n').encode()

def patch_recipe():
    return '''set -eu
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
debugfs -V > /work/debugfs-version.txt 2>&1
e2fsck -V > /work/e2fsck-version.txt 2>&1
debugfs -R "stat /etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg" /work/root.ext4 > /work/before-stat.txt 2> /work/before-error.txt
grep -q 'File not found by ext2_lookup' /work/before-error.txt
if grep -q '^Inode:' /work/before-stat.txt; then exit 31; fi
debugfs -w -f /work/patch-commands.txt /work/root.ext4 > /work/debugfs-log.txt 2>&1
debugfs -R "dump /etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg /work/early-readback.cfg" /work/root.ext4 > /work/readback-log.txt 2>&1
cmp /work/early-output.cfg /work/early-readback.cfg
mkdir -m 700 /work/cloud
debugfs -R "dump /etc/cloud/cloud.cfg /work/cloud/cloud.cfg" /work/root.ext4 > /work/cloud-config-dump-log.txt 2>&1
debugfs -R "rdump /etc/cloud/cloud.cfg.d /work/cloud" /work/root.ext4 > /work/cloud-confd-dump-log.txt 2>&1
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -I - > /dev/null 2>&1 <<'CLOUD_OUTPUT_PROOF'
import json
import logging
import os
import stat
from pathlib import Path
logging.disable(logging.CRITICAL)
from cloudinit import util
root=Path('/work/cloud')
if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
    raise RuntimeError('extracted_config_root_invalid')
files=[]
total=0
count=0
for path in root.rglob('*'):
    count+=1
    relative=path.relative_to(root)
    parts=relative.parts
    inode=path.lstat()
    if count>128 or len(parts)>2 or path.is_symlink() or inode.st_uid!=os.getuid():
        raise RuntimeError('extracted_config_entry_invalid')
    if stat.S_ISDIR(inode.st_mode):
        if parts!=('cloud.cfg.d',):
            raise RuntimeError('extracted_config_directory_invalid')
        path.chmod(0o700)
    elif stat.S_ISREG(inode.st_mode):
        if inode.st_nlink!=1 or not(parts==('cloud.cfg',) or (len(parts)==2 and parts[0]=='cloud.cfg.d')):
            raise RuntimeError('extracted_config_file_invalid')
        if inode.st_size>262144:
            raise RuntimeError('extracted_config_size_invalid')
        total+=inode.st_size
        if total>1048576:
            raise RuntimeError('extracted_config_total_invalid')
        path.chmod(0o600)
        if parts==('cloud.cfg',) or path.suffix=='.cfg':
            files.append(path)
    else:
        raise RuntimeError('extracted_config_type_invalid')
config=root/'cloud.cfg'
confd=root/'cloud.cfg.d'
patch=confd/'99zz-n100-ha-initial-output.cfg'
if not config.is_file() or not confd.is_dir() or not patch.is_file():
    raise RuntimeError('extracted_required_config_missing')
if patch.read_bytes()!=Path('/work/early-readback.cfg').read_bytes():
    raise RuntimeError('extracted_patch_readback_mismatch')
for path in files:
    raw=path.read_bytes()
    if b'## template:' in raw:
        raise RuntimeError('extracted_config_template_not_supported')
    parsed=util.read_conf(str(path))
    if not isinstance(parsed,dict) or 'conf_d' in parsed:
        raise RuntimeError('extracted_config_external_confd_not_supported')
userdata=json.loads(Path('/work/early-output.cfg').read_text().split(chr(10),1)[1])
initial=util.read_conf_with_confd('/work/cloud/cloud.cfg')
expected=['> /dev/null','> /dev/null']
matched=(initial.get('output')==userdata['output']
         and list(util.get_output_cfg(initial,'init'))==expected
         and list(util.get_output_cfg(userdata,'init'))==expected)
with Path('/work/cloud-output-merge-proof.json').open('x') as proof:
    json.dump({'initial_and_userdata_output_match':matched},proof,sort_keys=True)
CLOUD_OUTPUT_PROOF
debugfs -R "stat /etc/cloud/cloud.cfg.d/99zz-n100-ha-initial-output.cfg" /work/root.ext4 > /work/after-stat.txt 2> /work/after-error.txt
e2fsck -fn /work/root.ext4 > /work/e2fsck-log.txt 2>&1
'''

def offline_patch(value,prefix,work,owner):
    need(not any(c in str(work) for c in (',','\n','\r','\0')),'image_work_path')
    name='n100-ha-image-'+owner;label='org.n100ha.guest-image.owner='+owner
    args=prefix+['create','--pull=never','--name',name,'--label',label,'--platform','linux/amd64','--user',str(os.getuid())+':'+str(os.getgid()),'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','64','--memory','512m','--cpus','1','--mount','type=bind,src='+str(work)+',dst=/work','--entrypoint','/bin/sh',UTILITY_IMAGE,'-ceu',patch_recipe()]
    container=run(args,'offline_patch_create_failed').strip().decode('ascii');need(re.fullmatch('[0-9a-f]{64}',container) is not None,'offline_patch_identity_unknown')
    try:run(prefix+['start','--attach',container],'offline_patch_or_fsck_failed',timeout=300)
    finally:
        facts=json.loads(run(prefix+['inspect',container],'offline_patch_cleanup_unknown'))
        need(len(facts)==1 and facts[0]['Id']==container and facts[0]['Image']==UTILITY_IMAGE and facts[0]['Name']=='/'+name and facts[0]['Config']['Labels'].get('org.n100ha.guest-image.owner')==owner,'offline_patch_cleanup_identity_changed')
        run(prefix+['rm','--force',container],'offline_patch_cleanup_failed')
        state=facts[0]['State']
        need(state['Status']=='exited' and state['Running'] is False and type(state['ExitCode']) is int and state['ExitCode']==0 and state['OOMKilled'] is False and state['Error']=='','offline_patch_or_fsck_failed')
    patch=patch_bytes();need((work/'early-readback.cfg').read_bytes()==patch,'early_output_readback_mismatch')
    proof_path=work/'cloud-output-merge-proof.json';need(proof_path.stat().st_size<=1024,'early_output_merge_proof_invalid')
    proof=strict_json(proof_path.read_bytes())
    need(type(proof) is dict and set(proof)=={'initial_and_userdata_output_match'} and proof['initial_and_userdata_output_match'] is True,'early_output_merge_not_verified')
    metadata=(work/'after-stat.txt').read_text();need(re.search(r'Type:\s+regular\s+Mode:\s+0?644\b',metadata) and re.search(r'User:\s+0\s+Group:\s+0\b',metadata) and re.search(r'Size:\s+'+str(len(patch))+r'\b',metadata),'early_output_inode_mismatch')
    versions=[]
    for name,pattern in (('debugfs',r'debugfs [0-9]+\.[0-9]+(?:\.[0-9]+)?(?: \([A-Za-z0-9 -]+\))?'),('e2fsck',r'e2fsck [0-9]+\.[0-9]+(?:\.[0-9]+)?(?: \([A-Za-z0-9 -]+\))?')):
        line=(work/(name+'-version.txt')).read_text().splitlines()[0];need(re.fullmatch(pattern,line) is not None,'offline_tool_version_unknown');versions.append(line)
    return versions

def vhdx_identifier(path):
    # Optional build-time libraries are absent from ordinary CI imports/tests.
    from dissect.hypervisor.disk.vhdx import VHDX
    from dissect.util.hash.crc32c import crc32c
    with path.open('rb') as stream:
        headers=[]
        for offset in (65536,131072):
            stream.seek(offset);raw=stream.read(4096);need(len(raw)==4096 and raw[:4]==b'head','derived_vhdx_header')
            check=bytearray(raw);check[4:8]=b'\0'*4;need(crc32c(bytes(check))==struct.unpack_from('<I',raw,4)[0],'derived_vhdx_header_CRC');headers.append(raw)
        active=max(headers,key=lambda raw:struct.unpack_from('<Q',raw,8)[0]);need(active[48:64]==b'\0'*16,'derived_vhdx_log_not_clean')
        stream.seek(0);disk=VHDX(stream);need(not disk.has_parent and disk.parent is None and disk.size==VIRTUAL_BYTES,'derived_vhdx_not_standalone')
        value=str(disk.id);need(uuid.UUID(value).int!=0,'derived_vhdx_empty_identifier');return value

def validate_receipt(value):
    need(type(value) is dict and set(value)==RECEIPT_FIELDS,'derivation_receipt_fields')
    need(type(value['schema_version']) is int and value['schema_version']==1 and value['format']==FORMAT and value['profile']==PROFILE,'derivation_receipt_version')
    need(value['node'] in ('A','B') and type(value['source_commit']) is str and re.fullmatch('[0-9a-f]{40}',value['source_commit']) is not None,'derivation_receipt_identity')
    for key in ('upstream_qcow_sha256','derived_qcow_sha256','node_vhdx_sha256','patch_sha256','qemu_img_sha256','docker_daemon_identity_sha256'):
        need(type(value[key]) is str and re.fullmatch('[0-9a-f]{64}',value[key]) is not None and value[key]!='0'*64,'derivation_receipt_hash')
    need(value['upstream_qcow_sha256']==UPSTREAM_SHA and value['derived_qcow_sha256']!=UPSTREAM_SHA,'derived_upstream_hash_collision')
    need(type(value['node_vhdx_bytes']) is int and 1048576<=value['node_vhdx_bytes']<=8*1024**3,'derivation_receipt_size')
    identifier=value['node_disk_identifier'];need(type(identifier) is str and re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',identifier) is not None and uuid.UUID(identifier).int!=0,'derivation_receipt_identifier')
    need(value['patch_path']==PATCH_PATH and value['patch_sha256']==PATCH_SHA and type(value['patch_bytes']) is int and value['patch_bytes']==61,'derivation_receipt_patch')
    need(all(type(value[k]) is int and value[k]==0 for k in ('patch_uid','patch_gid','filesystem_check_exit')) and value['patch_mode']=='0644','derivation_receipt_inode_or_fsck')
    need(type(value['root_partition_offset']) is int and value['root_partition_offset']==1074790400 and type(value['root_partition_bytes']) is int and 0<value['root_partition_bytes']<VIRTUAL_BYTES,'derivation_receipt_partition')
    need(value['upstream_unchanged'] is True and value['initial_and_userdata_output_match'] is True,'derivation_receipt_not_verified')
    need(value['qemu_img_version']==QEMU_VERSION and value['qemu_img_sha256']==QEMU_SHA and value['utility_image_config_digest']==UTILITY_IMAGE,'derivation_receipt_tool')
    need(type(value['docker_context']) is str and re.fullmatch('colima-codex-ha-[0-9a-f]{8}',value['docker_context']) is not None,'derivation_receipt_context')
    for key,tool in (('debugfs_version','debugfs'),('e2fsck_version','e2fsck')):
        need(type(value[key]) is str and re.fullmatch(tool+r' [0-9]+\.[0-9]+(?:\.[0-9]+)?(?: \([A-Za-z0-9 -]+\))?',value[key]) is not None,'derivation_receipt_tool_version')
    return value

def receipt(value,node,qcow_sha,path,identifier,partition,versions):
    patch=patch_bytes();need(qcow_sha!=UPSTREAM_SHA,'derived_upstream_hash_collision')
    result={'schema_version':1,'format':FORMAT,'profile':PROFILE,'source_commit':value['source_commit'],'node':node,'upstream_qcow_sha256':UPSTREAM_SHA,'derived_qcow_sha256':qcow_sha,'node_vhdx_sha256':file_hash(path),'node_vhdx_bytes':path.stat().st_size,'node_disk_identifier':identifier,'patch_path':PATCH_PATH,'patch_sha256':digest(patch),'patch_bytes':len(patch),'patch_uid':0,'patch_gid':0,'patch_mode':'0644','filesystem_check_exit':0,'root_partition_offset':partition[0],'root_partition_bytes':partition[1],'upstream_unchanged':True,'initial_and_userdata_output_match':True,'qemu_img_version':QEMU_VERSION,'qemu_img_sha256':QEMU_SHA,'utility_image_config_digest':UTILITY_IMAGE,'docker_context':value['docker_context'],'docker_daemon_identity_sha256':value['docker_daemon_identity_sha256'],'debugfs_version':versions[0],'e2fsck_version':versions[1]}
    return validate_receipt(result)

def derive(value,output):
    validate_spec(value);verify_source(value['source_commit']);upstream=regular(Path(value['upstream_qcow_path']));tool=regular(Path(value['qemu_img_path']))
    with upstream.open('rb') as stream:need(stream.read(4)==b'QFI\xfb','public_qcow_format_required')
    need(file_hash(upstream)==UPSTREAM_SHA,'original_public_qcow_hash_required');need(file_hash(tool)==QEMU_SHA,'image_qemu_changed')
    version=run([str(tool),'--version'],'image_qemu_query_failed').decode().splitlines()[0];need(version==QEMU_VERSION,'image_qemu_version_changed')
    info=qemu_info(tool,upstream);need(info['format']=='qcow2','public_qcow_format_required');prefix=docker_prefix(value);verify_runtime(value,prefix)
    need(output.is_absolute() and not output.exists() and output.parent.is_dir() and not any(c in str(output) for c in (',','\n','\r','\0')),'fresh_image_output_required');output.mkdir(mode=0o700);work=output/'work';work.mkdir(mode=0o700);owner=uuid.uuid4().hex
    patch=patch_bytes();need(json.loads(patch.split(b'\n',1)[1])=={'output':SEED.cloud_output_config()},'shared_output_policy_mismatch')
    write_new(output/'build-intent.json',encode({'schema_version':1,'profile':PROFILE,'source_commit':value['source_commit'],'upstream_sha256':UPSTREAM_SHA,'patch_sha256':digest(patch),'owner_uuid':owner,'automatic_retry_allowed':False,'production_transition':False}))
    raw=work/'public-copy.raw';run([str(tool),'convert','-f','qcow2','-O','raw',str(upstream),str(raw)],'public_raw_copy_failed',timeout=900);os.chmod(raw,0o600)
    need(raw.stat().st_size==info['virtual-size'],'public_raw_size_changed');partition=root_partition(raw);sparse_partition_copy(raw,work/'root.ext4',*partition)
    write_new(work/'early-output.cfg',patch);write_new(work/'patch-commands.txt',patch_commands());versions=offline_patch(value,prefix,work,owner)
    insert_partition(raw,work/'root.ext4',*partition);qcow=output/'derived-output-preseed.qcow2'
    run([str(tool),'convert','-f','raw','-O','qcow2',str(raw),str(qcow)],'derived_qcow_create_failed',timeout=900);os.chmod(qcow,0o600)
    run([str(tool),'resize','-f','qcow2',str(qcow),str(VIRTUAL_BYTES)],'derived_qcow_resize_failed');run([str(tool),'check','-f','qcow2',str(qcow)],'derived_qcow_check_failed',timeout=900)
    need(qemu_info(tool,qcow)['virtual-size']==VIRTUAL_BYTES,'derived_virtual_size_changed');qcow_sha=file_hash(qcow);need(qcow_sha!=UPSTREAM_SHA,'derived_upstream_hash_collision')
    nodes=[];ids=set()
    for node in ('A','B'):
        path=output/('node-'+node+'.vhdx');run([str(tool),'convert','-f','qcow2','-O','vhdx','-o','subformat=dynamic,block_size=2097152',str(qcow),str(path)],'node_vhdx_create_failed',timeout=900);os.chmod(path,0o600)
        run([str(tool),'compare','-q','-f','qcow2','-F','vhdx',str(qcow),str(path)],'node_vhdx_data_compare_failed',timeout=900);identifier=vhdx_identifier(path);need(identifier not in ids,'node_vhdx_identifiers_not_independent');ids.add(identifier);nodes.append((node,path,identifier))
    need(file_hash(upstream)==UPSTREAM_SHA and file_hash(tool)==QEMU_SHA,'upstream_or_tool_changed');verify_runtime(value,prefix)
    answers=[]
    for node,path,identifier in nodes:
        result=receipt(value,node,qcow_sha,path,identifier,partition,versions);derivation_name='node-'+node+'-image-derivation.json';derivation_raw=encode(result);write_new(output/derivation_name,derivation_raw)
        conversion={'schema_version':1,'node':node,'filename':path.name,'sha256':result['node_vhdx_sha256'],'bytes':result['node_vhdx_bytes'],'source_qcow_sha256':qcow_sha,'resized_qcow_sha256':qcow_sha,'tool_version':QEMU_VERSION,'tool_sha256':QEMU_SHA,'virtual_bytes':VIRTUAL_BYTES,'comparison_exit_code':0,'native_boot_verified':False,'native_disk_identifier':identifier,'native_input_verified':False}
        conversion_raw=encode(conversion);write_new(output/('node-'+node+'-conversion-receipt.json'),conversion_raw)
        answers.append({'node':node,'filename':path.name,'sha256':result['node_vhdx_sha256'],'bytes':result['node_vhdx_bytes'],'disk_identifier':identifier,'source_qcow_sha256':qcow_sha,'conversion_receipt_sha256':digest(conversion_raw),'tool_version':QEMU_VERSION,'tool_sha256':QEMU_SHA,'derivation_profile':PROFILE,'upstream_qcow_sha256':UPSTREAM_SHA,'derivation_receipt_sha256':digest(derivation_raw)})
    answer={'scope':'public_preboot_output_preseed_images_not_native_boot','source_commit':value['source_commit'],'nodes':answers,'production_deployed':False,'guest_boot_verified':False,'writer_authority_issued':False};write_new(output/'image-result.json',encode(answer));return answer

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--spec',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--go',action='store_true');args=parser.parse_args()
    need(args.go,'image_build_explicit_go_required');regular(args.spec,private=True);need(args.spec.stat().st_size<=65536,'image_spec_size');value=strict_json(args.spec.read_bytes());answer=derive(value,args.output);print(json.dumps(answer,sort_keys=True));return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'scope':'public_preboot_output_preseed_images','status':'FAIL','reason':str(error) if isinstance(error,ImageError) else 'image_build_failed','partial_artifacts_preserved':True,'automatic_retry_allowed':False,'production_deployed':False}));raise SystemExit(1)
