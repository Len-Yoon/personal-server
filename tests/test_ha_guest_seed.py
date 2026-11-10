"""NoCloud integrity, credential boundaries and one-reboot guest state tests."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ha_guest_bootstrap import B,fixture,pg_archive

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ha_guest_seed',ROOT/'infra/k8s/ha-package/guest_seed.py')
S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)
image_spec=importlib.util.spec_from_file_location('ha_guest_image',ROOT/'infra/k8s/ha-package/guest_image.py')
I=importlib.util.module_from_spec(image_spec);image_spec.loader.exec_module(I)


def key_bytes():
    kind=b'ssh-ed25519';point=b'x'*32
    return b'\x00\x00\x00\x0b'+kind+b'\x00\x00\x00\x20'+point


def spec_value(lock,key):
    return {'schema_version':1,'source_commit':lock['source_commit'],'installation_id':lock['installation_id'],
        'node':'A','seed_id':'6'*64,'bootstrap_manifest_sha256':'7'*64,'expected_kernel_release':'6.8.0-149-generic',
        'mac_address':'00:15:5d:11:22:33','ipv4_cidr':'192.0.2.10/24','operator_public_key_path':str(key),
        'operator_public_key_sha256':S.sha(key)}


class SeedTests(unittest.TestCase):
    def test_only_exact_existing_public_key_is_copied_without_personal_comment(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name).resolve()/'existing.pub'
            key='ssh-ed25519 '+base64.b64encode(key_bytes()).decode()
            path.write_text(key+' private-comment@example.invalid\n');path.chmod(0o644)
            self.assertEqual(S.public_key(path,S.sha(path)),key)
            with self.assertRaises(S.SeedError):S.public_key(path,'0'*64)
            path.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n')
            with self.assertRaises(S.SeedError):S.public_key(path,S.sha(path))
            path.write_text('ssh-ed25519 '+base64.b64encode(key_bytes()[:-2]).decode())
            with self.assertRaises(S.SeedError):S.public_key(path,S.sha(path))

    def test_spec_rejects_unknown_network_kernel_or_bundle_identity(self):
        lock=fixture();lock.update(schema_version=2,primary_node='A',source_node=None,os_packages=[{'package':'linux-image-6.8.0-149-generic'}])
        with tempfile.TemporaryDirectory() as name:
            key=Path(name).resolve()/'operator.pub';key.write_text('public fixture')
            value=spec_value(lock,key);self.assertEqual(S.validate_spec(value,lock),value)
            mutations=[lambda v:v.update(node='B'),lambda v:v.update(schema_version=True),
                lambda v:v.update(expected_kernel_release='6.8.0-148-generic'),lambda v:v.update(mac_address='01:15:5d:11:22:33'),
                lambda v:v.update(ipv4_cidr='127.0.0.1/24'),lambda v:v.update(ipv4_cidr='192.0.2.11/24'),
                lambda v:v.update(password='forbidden')]
            for change in mutations:
                wrong=copy.deepcopy(value);change(wrong)
                with self.assertRaises(S.SeedError):S.validate_spec(wrong,lock)

    def test_cloud_init_has_no_new_sudo_user_or_password_and_does_not_wait_on_itself(self):
        with tempfile.TemporaryDirectory() as name:
            key=Path(name).resolve()/'operator.pub';key.write_text('public fixture')
            value=spec_value(fixture(),key)
            rendered=S.render(value,'ssh-ed25519 PUBLIC',b'fixed module', '8'*64)
            cloud=json.loads(rendered['user-data'].split(b'\n',1)[1])
            self.assertEqual(cloud['users'],[{'name':'root','lock_passwd':True,'ssh_authorized_keys':['ssh-ed25519 PUBLIC']}])
            self.assertFalse(cloud['ssh_pwauth']);self.assertFalse(cloud['package_update']);self.assertFalse(cloud['package_upgrade'])
            self.assertNotIn('sudo',str(cloud));self.assertNotIn('cloud-init status --wait',str(cloud))
            self.assertNotIn(str(key),str(cloud));self.assertNotIn(value['ipv4_cidr'],str(cloud))
            self.assertEqual(cloud['runcmd'][-1],['systemctl','start','--no-block','n100-ha-seed.service'])
            self.assertIn('After=cloud-final.service',S.unit())
            self.assertIn('WantedBy=cloud-init.target',S.unit())
            self.assertNotIn('WantedBy=multi-user.target',S.unit())
            self.assertNotIn('Restart=',S.unit())
            net=S.network(value)['ethernets']['ha0']
            self.assertFalse(net['dhcp4']);self.assertNotIn('routes',net);self.assertNotIn('nameservers',net)

    def test_schema2_optional_egress_is_separate_nonblocking_and_schema1_stays_static(self):
        lock=fixture();lock.update(schema_version=2,primary_node='A',source_node=None,
            os_packages=[{'package':'linux-image-6.8.0-149-generic'}])
        with tempfile.TemporaryDirectory() as name:
            key=Path(name).resolve()/'operator.pub';key.write_text('public fixture')
            value=spec_value(lock,key);original=S.network(value)
            value.update(schema_version=2,egress_mac_address='00:15:5d:44:55:66');S.validate_spec(value,lock)
            net=S.network(value)
            self.assertEqual(net['ethernets']['ha0'],original['ethernets']['ha0'])
            self.assertNotIn('routes',net['ethernets']['ha0'])
            egress=net['ethernets']['ha-egress'];self.assertTrue(egress['dhcp4']);self.assertTrue(egress['optional'])
            self.assertEqual(egress['dhcp4-overrides']['route-metric'],500)
            self.assertFalse(egress['dhcp6']);self.assertEqual(net['renderer'],'networkd')
            for mutation in (lambda v:v.update(schema_version=1),lambda v:v.update(egress_mac_address=v['mac_address']),
                             lambda v:v.update(egress_mac_address='01:15:5d:44:55:66')):
                wrong=copy.deepcopy(value);mutation(wrong)
                with self.assertRaises(S.SeedError):S.validate_spec(wrong,lock)
            del value['egress_mac_address'];S.validate_spec(value,lock)
            self.assertEqual(S.network(value),original)
            cloud=json.loads(S.render(value,'ssh-ed25519 PUBLIC',b'fixed module','8'*64)['user-data'].split(b'\n',1)[1])
            policy=next(row for row in cloud['write_files'] if row['path']=='/etc/apt/apt.conf.d/99n100-ha-seed-periodic')
            self.assertEqual(policy['content'],'APT::Periodic::Enable "0";\n')
            self.assertEqual(policy['permissions'],'0600')

    def test_one_reboot_state_machine_refuses_same_boot_wrong_kernel_and_failures(self):
        self.assertEqual(S.reboot_action({'phase':'new'},'a','old','target'),'install')
        state={'phase':'awaiting_reboot','reboot_requests':1,'boot_id_sha256':'a'}
        self.assertEqual(S.reboot_action(state,'b','target','target'),'verify_postboot')
        for old,boot,kernel in ((state,'a','target'),(state,'b','old'),({'phase':'failed'},'b','target'),
                                ({**state,'reboot_requests':2},'b','target'),({'phase':'installing'},'b','target')):
            with self.assertRaises(S.SeedError):S.reboot_action(old,boot,kernel,'target')
        self.assertEqual(S.reboot_action({'phase':'complete'},'b','target','target'),'already_complete')

    def test_cloud_init_recoverable_failure_is_not_accepted(self):
        with patch.object(S,'run',return_value=b'{"status":"done","extended_status":"degraded done"}'):
            with self.assertRaises(S.SeedError):S.cloud_status()
        with patch.object(S,'run',return_value=b'{"status":"done","extended_status":"done"}'):
            self.assertEqual(S.cloud_status(),0)
        with patch.object(S,'run',side_effect=S.SeedError('cloud_init_failed')):
            with self.assertRaises(S.SeedError):S.cloud_status()

    def identity(self):
        return {'source_commit':'a'*40,'installation_id':'b'*64,'node':'A','seed_id':'c'*64,
                'bootstrap_manifest_sha256':'d'*64,'expected_kernel_release':'6.8.0-149-generic'}

    def receipt(self):
        value=self.identity()
        key,fingerprint=S.server_key_record('ssh-ed25519 '+base64.b64encode(key_bytes()).decode())
        value.update(status='PASS',scope='new_guest_os_engine_and_pg_image_not_started',cloud_init_status_exit=0,
            kernel_boot_verified=True,guest_reboot_requests=1,new_boot_id_observed=True,boot_id_sha256='e'*64,
            automatic_os_updates_disabled=True,package_count=73,pg_image='sha256:'+'f'*64,pg_state='installed-not-started',pg_started=False,
            writer_authority_issued=False,provider_credentials_supplied_by_seed=False,effects_transition_performed=False,
            production_deployed=False,server_public_key=key,server_key_fingerprint=fingerprint,
            serial_delivery_is_not_host_receipt_verification=True)
        return value

    def test_reboot_marker_rejects_changed_identity_or_receipt_from_another_boot(self):
        config=self.identity();state={'phase':'awaiting_reboot',**config,'reboot_requests':1,'boot_id_sha256':'old'}
        self.assertEqual(S.state_action(state,config,'new',config['expected_kernel_release'],state_exists=True),'verify_postboot')
        changed=copy.deepcopy(config);changed['seed_id']='0'*64
        with self.assertRaises(S.SeedError):S.state_action(state,changed,'new',config['expected_kernel_release'],state_exists=True)
        with self.assertRaises(S.SeedError):S.state_action({'phase':'new'},config,'new','old',state_exists=True)
        with self.assertRaises(S.SeedError):S.state_action({**state,'reboot_requests':True},config,'new',config['expected_kernel_release'],state_exists=True)
        complete={**state,'phase':'complete','boot_id_sha256':'old'}
        with self.assertRaises(S.SeedError):S.state_action(complete,config,'new',config['expected_kernel_release'],state_exists=True)

    def test_serial_payload_has_only_public_identity_and_rejects_scope_or_fingerprint_forgery(self):
        receipt=self.receipt();payload=S.serial_payload(receipt)
        self.assertTrue(payload.startswith(S.SERIAL_PREFIX.encode()))
        self.assertLessEqual(len(payload),4096)
        mutations=[lambda v:v.update(ip_address='192.0.2.10'),lambda v:v.update(writer_authority_issued=True),
            lambda v:v.update(guest_reboot_requests=2),lambda v:v.update(server_key_fingerprint='SHA256:wrong'),
            lambda v:v.update(server_public_key=v['server_public_key']+' personal-comment'),
            lambda v:v.update(cloud_init_status_exit=False)]
        for change in mutations:
            value=copy.deepcopy(receipt);change(value)
            with self.assertRaises(S.SeedError):S.serial_payload(value)
        failure={**self.identity(),'status':'FAIL','reason':'cloud_init_failed','production_deployed':False,
                 'pg_started':False,'writer_authority_issued':False}
        S.serial_payload(failure)
        with self.assertRaises(S.SeedError):S.serial_payload({**failure,'reason':'failed at 192.0.2.10'})

    def test_guest_host_key_is_public_only_and_serial_short_write_cannot_succeed(self):
        public='ssh-ed25519 '+base64.b64encode(key_bytes()).decode()
        key,fingerprint=S.server_key_record(public+' root-private-comment')
        self.assertEqual(key,public);self.assertTrue(fingerprint.startswith('SHA256:'))
        with self.assertRaises(S.SeedError):S.server_key_record('-----BEGIN OPENSSH PRIVATE KEY-----')
        receipt=self.receipt()
        with patch.object(S.Path,'is_symlink',return_value=False), patch.object(S.Path,'lstat',return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), patch.object(S.os,'open',return_value=7), patch.object(S.os,'close') as close, patch.object(S.os,'write',return_value=0) as write:
            with self.assertRaises(S.SeedError):S.emit_serial(receipt)
            write.assert_called_once();close.assert_called_once_with(7)
            self.assertEqual(write.call_args.args[1],S.serial_payload(receipt))

    def test_exact_git_source_seed_tree_roundtrip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as name:
            temporary=Path(name).resolve();repo=temporary/'repo';repo.mkdir();inputs=temporary/'inputs';inputs.mkdir()
            paths=['infra/k8s/ha-package/bootstrap.py',S.SOURCE_PATH,
                *['infra/k8s/ha-package/bootstrap_assets/'+n for n in B.ASSETS],
                'infra/k8s/ha_runtime/__init__.py','infra/k8s/ha_runtime/schema.sql']
            for relative in paths:
                target=repo/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/relative).read_bytes())
            def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
            git('init','-q');git('add','.');git('-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
            lock=fixture();lock.update(schema_version=2,primary_node='A',source_node=None,os_packages=[
                {'package':'linux-image-6.8.0-149-generic','filename':'linux-image_1_amd64.deb','sha256':'0'*64,
                 'version':'6.8.0-149.149','architecture':'amd64','source_url':'https://archive.ubuntu.com/ubuntu/pool/main/l/linux/linux-image_1_amd64.deb'}])
            lock['source_commit']=git('rev-parse','HEAD').decode().strip()
            for row in lock['docker_packages']:
                if row['package']=='docker-compose-plugin':
                    row['filename']='docker-compose-plugin_5.7.0-1%7eubuntu.24.04%7enoble_amd64.deb'
                    row['source_url']='https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/'+row['filename']
            for row in [lock['ubuntu_image'],*B.packages(lock)]:
                file=inputs/row['filename'];file.write_bytes(b'QFI\xfb\x00\x00\x00\x03fixture' if row is lock['ubuntu_image'] else b'package fixture')
                row['sha256']=B.digest(file)
            lock['postgres']['config_digest']=pg_archive(inputs/lock['postgres']['filename'])
            lock['postgres']['sha256']=B.digest(inputs/lock['postgres']['filename'])
            lock_path=temporary/'lock.json';lock_path.write_bytes(B.encode(lock))
            bundle=temporary/'bundle'
            with patch.object(B,'ROOT',repo):B.prepare(lock_path,inputs,bundle)
            key=temporary/'existing.pub';key.write_text('ssh-ed25519 '+base64.b64encode(key_bytes()).decode()+' private-comment\n');key.chmod(0o644)
            value=spec_value(lock,key);value['bootstrap_manifest_sha256']=S.sha(bundle/'manifest.json')
            spec_path=temporary/'spec.json';spec_path.write_bytes(S.encode(value));spec_path.chmod(0o600)
            # A mutable worktree bootstrap is never executed; only Git bytes are trusted.
            (repo/'infra/k8s/ha-package/bootstrap.py').write_text("raise RuntimeError('mutable_bootstrap_must_not_execute')\n")
            bad=copy.deepcopy(value);bad['bootstrap_manifest_sha256']='0'*64
            spec_path.write_bytes(S.encode(bad))
            with patch.object(S,'ROOT',repo), patch.object(S,'source_builder') as execute:
                with self.assertRaises(S.SeedError):S.prepare(spec_path,bundle,temporary/'must-not-exist')
                execute.assert_not_called()
            self.assertFalse((temporary/'must-not-exist').exists())
            spec_path.write_bytes(S.encode(value))
            with patch.object(S,'ROOT',repo):tree,manifest=S.prepare(spec_path,bundle,temporary/'seed')
            trusted=S.source_builder(git('show',lock['source_commit']+':infra/k8s/ha-package/bootstrap.py'),repo/'infra/k8s/ha-package/bootstrap.py')
            self.assertEqual(S.verify_tree(tree,expected=manifest,verifier=trusted),manifest)
            self.assertNotIn(b'private-comment',(tree/'user-data').read_bytes())
            # Alter the cloud-init payload AND its self manifest while keeping
            # prepare's external manifest unchanged. Both host verification and
            # ISO generation must reject it before any ISO tool is invoked.
            payload=tree/'user-data';original_payload=payload.read_bytes()
            payload.chmod(0o600);payload.write_bytes(original_payload+b'\n# changed after prepare\n')
            original_seed=(tree/'seed-manifest.json').read_bytes()
            changed_seed=json.loads(original_seed);changed_seed['files']['user-data']=S.sha(payload)
            (tree/'seed-manifest.json').chmod(0o600);(tree/'seed-manifest.json').write_bytes(S.encode(changed_seed))
            with self.assertRaises(S.SeedError):S.verify_tree(tree,expected=manifest,verifier=trusted)
            real_run=S.run
            def no_iso_tool(argv,*args,**kwargs):
                self.assertNotEqual(argv[0],'/usr/bin/hdiutil')
                return real_run(argv,*args,**kwargs)
            with patch.object(S,'ROOT',repo), patch.object(S.sys,'platform','darwin'), patch.object(S.Path,'is_file',return_value=True), patch.object(S,'run',side_effect=no_iso_tool):
                with self.assertRaises(S.SeedError):S.create_iso(tree,temporary/'must-not-create.iso',manifest)
            self.assertFalse((temporary/'must-not-create.iso').exists())
            payload.write_bytes(original_payload);(tree/'seed-manifest.json').write_bytes(original_seed)
            # The in-guest config has no host files inventory by design; its
            # independent bootstrap/identity pins remain a separate contract.
            guest_expected={k:manifest[k] for k in ('source_commit','installation_id','node','seed_id',
                'bootstrap_manifest_sha256','guest_source_sha256','expected_kernel_release')}
            self.assertEqual(S.verify_tree(tree,expected=guest_expected,verifier=trusted),manifest)
            target=tree/'network-config';original=target.read_bytes();target.chmod(0o600);target.write_text('tampered')
            with self.assertRaises(S.SeedError):S.verify_tree(tree,expected=manifest,verifier=trusted)
            target.write_bytes(original)
            executable=tree/'bundle/bootstrap.py';executable.chmod(0o600)
            executable.write_text('raise RuntimeError(\'untrusted_code_must_not_execute\')\n')
            inner=json.loads((tree/'bundle/manifest.json').read_bytes())
            inner['files']['bootstrap.py']=S.sha(executable)
            (tree/'bundle/manifest.json').chmod(0o600);(tree/'bundle/manifest.json').write_bytes(S.encode(inner))
            changed=json.loads((tree/'seed-manifest.json').read_bytes())
            changed['bootstrap_manifest_sha256']=S.sha(tree/'bundle/manifest.json')
            changed['files']['bundle/manifest.json']=S.sha(tree/'bundle/manifest.json')
            changed['files']['bundle/bootstrap.py']=S.sha(executable)
            (tree/'seed-manifest.json').chmod(0o600);(tree/'seed-manifest.json').write_bytes(S.encode(changed))
            with patch.object(S,'builder') as load:
                with self.assertRaises(S.SeedError):S.verify_tree(tree,expected=manifest,verifier=trusted)
                load.assert_not_called()

    def test_unpinned_tree_verification_and_module_load_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();code=root/'bootstrap.py'
            code.write_text("raise RuntimeError('must_not_execute')\n")
            with self.assertRaises(S.SeedError):S.builder(code,None)
            with self.assertRaises(S.SeedError):S.verify_tree(root,expected=None,verifier=None)
            with patch.object(S,'git_source') as git, patch.object(S,'source_builder') as execute:
                with self.assertRaises(S.SeedError):S.create_iso(root,root/'out.iso',expected={})
                git.assert_not_called();execute.assert_not_called()

    def test_directory_symlink_and_fifo_are_rejected_before_copy_or_external_reads(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();source=root/'source';source.mkdir();outside=root/'outside';outside.mkdir()
            (outside/'private.txt').write_text('synthetic_private_value')
            (source/'unlisted').symlink_to(outside,target_is_directory=True)
            with patch.object(S.shutil,'copyfileobj') as copy:
                with self.assertRaises(S.SeedError):S.copy_manifest(source,root/'copy',{})
                copy.assert_not_called()
            self.assertFalse((root/'copy').exists())
            (source/'unlisted').unlink();S.os.mkfifo(source/'fifo')
            with self.assertRaises(S.SeedError):S.tree_files(source)

    def test_atomic_receipt_is_private_and_no_existing_temporary_file_is_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();path=root/'state.json'
            S.atomic(path,{'phase':'new'});self.assertEqual(path.stat().st_mode & 0o777,0o600)
            (root/'state.json.tmp').write_text('preserve')
            with self.assertRaises(S.SeedError):S.atomic(path,{'phase':'changed'})
            self.assertEqual(json.loads(path.read_bytes()),{'phase':'new'})


class ImageDerivationTests(unittest.TestCase):
    def value(self):
        return {'schema_version':1,'source_commit':'a'*40,
            'upstream_qcow_path':'/fixture/ubuntu-24.04-server-cloudimg-amd64.img',
            'qemu_img_path':'/fixture/qemu-img','qemu_img_sha256':I.QEMU_SHA,
            'qemu_img_version':I.QEMU_VERSION,'docker_context':'colima-codex-ha-01234567',
            'docker_daemon_identity_sha256':'b'*64,'utility_image_config_digest':I.UTILITY_IMAGE}

    def test_initial_system_config_matches_userdata_without_relaxing_status(self):
        with tempfile.TemporaryDirectory() as name:
            key=Path(name).resolve()/'existing.pub';key.write_text('public fixture')
            value=spec_value(fixture(),key)
            cloud=json.loads(S.render(value,'ssh-ed25519 PUBLIC',b'fixed module','8'*64)['user-data'].split(b'\n',1)[1])
            early=json.loads(S.early_config_bytes().split(b'\n',1)[1])
            self.assertEqual(early,{'output':cloud['output']})
            self.assertEqual(cloud['output'],{'all':'> /dev/null'})
            self.assertEqual(len(I.patch_bytes()),61)
            self.assertEqual(I.digest(I.patch_bytes()),'659e515ee99507c6990b06c3a0560a794070514de74a9506e6ee70da20bc77c8')
            with patch.object(I.SEED,'OUTPUT_SINK','> changed'):
                with self.assertRaises(I.ImageError):I.patch_bytes()
            # A recoverable failure still blocks acceptance; the fix is early config.
            with patch.object(S,'run',return_value=b'{"status":"done","extended_status":"degraded done"}'):
                with self.assertRaises(S.SeedError):S.cloud_status()

    def test_image_spec_is_closed_and_cannot_select_failed_disk_or_other_runtime(self):
        self.assertEqual(I.validate_spec(self.value()),self.value())
        for key,value in [('schema_version',True),('source_commit','g'*40),
                          ('upstream_qcow_path','/fixture/failed-A.vhdx'),
                          ('qemu_img_sha256','0'*64),('utility_image_config_digest','sha256:'+'0'*64),
                          ('docker_context','orbstack'),('docker_daemon_identity_sha256','0'*64)]:
            wrong=self.value();wrong[key]=value
            with self.assertRaises(I.ImageError):I.validate_spec(wrong)
        wrong=self.value();wrong['extra']='unlisted'
        with self.assertRaises(I.ImageError):I.validate_spec(wrong)
        with self.assertRaises(I.ImageError):I.strict_json('{"schema_version":1,"schema_version":1}')

    def test_new_partition_copy_only_changes_selected_owned_range(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();original=root/'original.raw';original.write_bytes(b'A'*16+b'B'*32+b'C'*16)
            original_hash=I.file_hash(original);part=root/'root.ext4'
            I.sparse_partition_copy(original,part,16,32);self.assertEqual(part.read_bytes(),b'B'*32)
            with self.assertRaises(FileExistsError):I.sparse_partition_copy(original,part,16,32)
            copy_path=root/'fresh.raw';copy_path.write_bytes(original.read_bytes());part.write_bytes(b'D'*32)
            I.insert_partition(copy_path,part,16,32)
            self.assertEqual(copy_path.read_bytes(),b'A'*16+b'D'*32+b'C'*16)
            self.assertEqual(I.file_hash(original),original_hash)
            part.write_bytes(b'wrong length')
            with self.assertRaises(I.ImageError):I.insert_partition(copy_path,part,16,32)

    def test_receipt_does_not_relabel_upstream_and_requires_real_success_fields(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name).resolve()/'node-A.vhdx'
            with path.open('wb') as handle:handle.truncate(1048576)
            value=I.receipt(self.value(),'A','c'*64,path,'12345678-1234-1234-1234-123456789abc',
                (1074790400,2683289088),('debugfs 1.47.0 (5-Feb-2023)','e2fsck 1.47.0 (5-Feb-2023)'))
            self.assertEqual(set(value),I.RECEIPT_FIELDS);self.assertEqual(len(value),28)
            self.assertEqual(value['upstream_qcow_sha256'],I.UPSTREAM_SHA)
            self.assertNotEqual(value['derived_qcow_sha256'],I.UPSTREAM_SHA)
            for key,changed in [('schema_version',True),('derived_qcow_sha256',I.UPSTREAM_SHA),
                                ('node_vhdx_bytes',True),('node_disk_identifier','0'*36),
                                ('patch_sha256','0'*64),('patch_bytes',True),('patch_uid',False),
                                ('filesystem_check_exit',1),('upstream_unchanged',False),
                                ('initial_and_userdata_output_match',False)]:
                wrong=copy.deepcopy(value);wrong[key]=changed
                with self.assertRaises(I.ImageError):I.validate_receipt(wrong)

    def test_patch_container_is_owned_offline_and_fsck_failure_does_not_accept(self):
        owner='f'*32;container='e'*64;calls=[]
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();(root/'early-readback.cfg').write_bytes(I.patch_bytes())
            (root/'early-output.cfg').write_bytes(I.patch_bytes())
            proof=root/'cloud-output-merge-proof.json';proof.write_text('{"initial_and_userdata_output_match":true}')
            (root/'after-stat.txt').write_text('Inode: 1 Type: regular Mode: 0644\nUser: 0 Group: 0 Size: 61\n')
            for tool in ('debugfs','e2fsck'):(root/(tool+'-version.txt')).write_text(tool+' 1.47.0 (5-Feb-2023)\n')
            facts=[{'Id':container,'Image':I.UTILITY_IMAGE,'Name':'/n100-ha-image-'+owner,
                    'Config':{'Labels':{'org.n100ha.guest-image.owner':owner}},
                    'State':{'Status':'exited','Running':False,'ExitCode':0,'OOMKilled':False,'Error':''}}]
            def mocked(argv,reason,*args,**kwargs):
                calls.append(argv)
                if 'create' in argv:return container.encode()
                if 'inspect' in argv:return json.dumps(facts).encode()
                return b''
            with patch.object(I,'run',side_effect=mocked):I.offline_patch(self.value(),['docker','--context',self.value()['docker_context']],root,owner)
            create=calls[0]
            self.assertIn('--pull=never',create)
            self.assertIn('--read-only',create);self.assertEqual(create[create.index('--network')+1],'none')
            self.assertEqual(create[create.index('--cap-drop')+1],'ALL')
            self.assertIn('no-new-privileges',create);self.assertEqual(create.count('--mount'),1)
            mounts=[create[i+1] for i,value in enumerate(create) if value=='--mount']
            self.assertEqual(sum(not value.endswith(',readonly') for value in mounts),1)
            self.assertIn('e2fsck -fn /work/root.ext4',I.patch_recipe())
            self.assertIn('exit 31',I.patch_recipe());self.assertNotIn('mount ',I.patch_recipe())
            import ast
            embedded=I.patch_recipe().split("<<'CLOUD_OUTPUT_PROOF'\n",1)[1].split('\nCLOUD_OUTPUT_PROOF',1)[0]
            ast.parse(embedded)
            self.assertIn("util.read_conf_with_confd('/work/cloud/cloud.cfg')",embedded)
            self.assertIn("util.get_output_cfg(initial,'init')",embedded)
            self.assertIn('rdump /etc/cloud/cloud.cfg.d /work/cloud',I.patch_recipe())
            self.assertIn("'conf_d' in parsed",embedded)
            self.assertIn('path.is_symlink()',embedded)
            self.assertIn('total>1048576',embedded)
            self.assertIn('inode.st_size>262144',embedded)
            for false_proof in (False,1):
                proof.write_text(json.dumps({'initial_and_userdata_output_match':false_proof}))
                with patch.object(I,'run',side_effect=mocked):
                    with self.assertRaises(I.ImageError):I.offline_patch(self.value(),['docker'],root,owner)
            proof.write_text('{"initial_and_userdata_output_match":true}')
            # The CLI return alone cannot prove that the filesystem checker passed.
            facts[0]['State']['ExitCode']=1
            with patch.object(I,'run',side_effect=mocked):
                with self.assertRaises(I.ImageError):I.offline_patch(self.value(),['docker'],root,owner)
            facts[0]['State']['ExitCode']=0
            calls.clear()
            def fail_fsck(argv,reason,*args,**kwargs):
                if 'start' in argv:
                    calls.append(argv);raise I.ImageError('offline_patch_or_fsck_failed')
                return mocked(argv,reason,*args,**kwargs)
            with patch.object(I,'run',side_effect=fail_fsck):
                with self.assertRaises(I.ImageError):I.offline_patch(self.value(),['docker'],root,owner)
            self.assertTrue(any('rm' in argv and container in argv for argv in calls))
            self.assertFalse(any(p.name.endswith('derivation.json') for p in root.iterdir()))


if __name__=='__main__':unittest.main()
