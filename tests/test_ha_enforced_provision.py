"""Offline package boundaries and managed host grammar; never SCM/VM proof."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra/k8s'))
from ha_control import provision_enforced as P

HOST = ROOT / P.HOST_SOURCE
COMMIT = 'a' * 40


def settings():
    return dict(format='enforced-service-v1', service_name='FixtureBroker',
        service_sid='S-1-5-80-1-2-3-4-5', python_path=r'C:\Trusted Python\python.exe',
        python_sha256='b'*64, launcher_path=r'C:\Trusted Code\ha_control\launcher.py',
        launcher_sha256='c'*64, plan_path=r'C:\Private\plan.json', plan_sha256='d'*64,
        approval_path=r'C:\Private\approval.json', approval_sha256='e'*64,
        mode='serve', host_sha256='f'*64)


def pe():
    data=bytearray(1024);data[:2]=b'MZ';data[60:64]=(128).to_bytes(4,'little')
    data[128:132]=b'PE\0\0';data[132:134]=(0x14c).to_bytes(2,'little')
    data[152:154]=(0x10b).to_bytes(2,'little');data[360:364]=(8192).to_bytes(4,'little')
    return bytes(data)


class EnforcedProvisionTests(unittest.TestCase):
    def test_preflight_owner_exception_is_exact_os_root_ancestor_only(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        env=dict(os.environ, PREFLIGHT=str(ROOT/'infra/k8s/ha_control/windows-service-preflight.ps1'))
        script=r'''
$ErrorActionPreference='Stop'
. $env:PREFLIGHT
function Get-PreflightOsVolumeRoot{return 'C:\'}
$ti='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
if(-not (Test-PreflightOsRootOwner $ti 'c:\' $true)){throw 'os_root_rejected'}
foreach($case in @(@($ti,'C:\',$false),@($ti,'C:\Private',$true),@($ti,'D:\',$true),@($ti,'C:\..\',$true),@('S-1-1-0','C:\',$true))){
 if(Test-PreflightOsRootOwner $case[0] $case[1] ([bool]$case[2])){throw 'unsafe_owner_exception'}
}
$blocked=$false
try{Assert-PreflightMask 'S-1-1-0' 262144 @('S-1-5-18') @('S-1-5-18') $false $true}catch{$blocked=$true}
if(-not $blocked){throw 'unsafe_ace_bypassed'}
# Parse the real call site without claiming native ACL execution on macOS.
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:PREFLIGHT,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse_failed'}
$entry=$ast.Find({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Assert-PreflightEntry'},$true)
$calls=@($entry.FindAll({param($a) $a -is [Management.Automation.Language.CommandAst] -and $a.GetCommandName() -ceq 'Test-PreflightOsRootOwner'},$true))
if($calls.Count -ne 1 -or $calls[0].Extent.Text -cne 'Test-PreflightOsRootOwner $descriptor.Owner.Value $Entry.FullName $Ancestor'){throw 'owner_context_not_connected'}
'owner-context-only'
'''
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(),'owner-context-only')

    def test_exact_args_and_no_command_input(self):
        good=settings()
        raw=P.render_args(good)
        self.assertEqual(raw.decode().splitlines()[0], 'format=enforced-service-v1')
        for mode in ('initialize','advance','serve'):
            self.assertIn(('mode='+mode+'\n').encode(),P.render_args(good|{'mode':mode}))
        for key, value in [('mode','shell'),('python_path',r'\\host\python.exe'),
                ('plan_path',r'C:\Private\..\other'),('approval_path',r'C:\Private\file:stream'),
                ('service_name','wrong\nmode=serve'),('host_sha256','A'*64),
                ('launcher_path',r'C:\Private\trailing.\launcher.py')]:
            with self.subTest(key=key, value=value), self.assertRaises(P.ProvisionError):
                bad=deepcopy(good);bad[key]=value;P.render_args(bad)
        with self.assertRaises(P.ProvisionError):
            P.render_args(good | {'command':'whoami'})

    def test_real_managed_host_parser_and_quoted_fixed_command(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        self.assertTrue(Path(pwsh).is_file(), 'real PowerShell required')
        env=dict(os.environ, HOST_SOURCE=str(HOST), HOST_ARGS=P.render_args(settings()).decode())
        script=r'''
$ErrorActionPreference='Stop'
Add-Type -Path $env:HOST_SOURCE -CompilerOptions '/define:PORTABLE_FIXTURE'
$value=[EnforcedServiceV1.Settings]::new($env:HOST_ARGS)
$expected='"C:\Trusted Python\python.exe" -I -B "C:\Trusted Code\ha_control\launcher.py" serve --plan "C:\Private\plan.json" --approval "C:\Private\approval.json" --journal "C:\Private\journal\controller-state.json" --arm'
if($value.CommandLine() -cne $expected){throw 'not_exact_command'}
$cases=@(($env:HOST_ARGS.Replace('mode=serve','mode=cmd')),($env:HOST_ARGS.Replace('plan_path=C:\Private\plan.json','plan_path=C:\Private\..\plan.json')),($env:HOST_ARGS+"extra=anything`n"),($env:HOST_ARGS.Replace('format=enforced-service-v1','format=other')),($env:HOST_ARGS.Replace('service_sid=S-1-5-80-1-2-3-4-5','service_sid=S-1-5-18')),($env:HOST_ARGS.Replace("`n","`r`n")))
$i=0;foreach($bad in $cases){$i++;$closed=$false;try{[EnforcedServiceV1.Settings]::new($bad)|Out-Null}catch{$closed=$true};if(-not $closed){throw ('unsafe_args_accepted_'+$i)}}
$initialize=[EnforcedServiceV1.Settings]::new($env:HOST_ARGS.Replace('mode=serve','mode=initialize'))
if($initialize.CommandLine().EndsWith('--arm')){throw 'initialize_armed'}
$advance=[EnforcedServiceV1.Settings]::new($env:HOST_ARGS.Replace('mode=serve','mode=advance'))
if($advance.CommandLine() -cne $expected.Replace(' serve ',' advance ').Replace(' --arm','')){throw 'advance_not_exact_fixed_command'}
if($advance.CommandLine().EndsWith('--arm')){throw 'advance_armed'}
$ti='S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
if(-not [EnforcedServiceV1.Settings]::IsTrustedOsRootOwner($ti,'c:\','C:\',2)){throw 'os_root_owner_rejected'}
foreach($case in @(@($ti,'C:\','C:\',0),@($ti,'C:\','C:\',1),@($ti,'C:\Private','C:\',3),@($ti,'D:\','C:\',3),@($ti,'C:\..\','C:\',3),@('S-1-1-0','C:\','C:\',3))){
 if([EnforcedServiceV1.Settings]::IsTrustedOsRootOwner($case[0],$case[1],$case[2],[int]$case[3])){throw 'unsafe_owner_exception'}
}
foreach($mode in @('initialize','advance')){
 if([EnforcedServiceV1.Settings]::ServiceExitCode($mode,0) -ne 0){throw 'successful_oneshot_failed'}
 if([EnforcedServiceV1.Settings]::ServiceExitCode($mode,1) -ne 1){throw 'failed_oneshot_succeeded'}
}
foreach($mode in @('serve','unknown')){if([EnforcedServiceV1.Settings]::ServiceExitCode($mode,0) -ne 1){throw 'unexpected_exit_succeeded'}}
'managed-grammar-only'
'''
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout.decode().strip(),'managed-grammar-only')

    def test_pre_start_service_inventory_and_acl_provider_boundaries(self):
        # Production enumeration/hash/mask logic with explicit OS/ACL providers.
        # This never executes StartService, the EXE, or a Windows ACL API.
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'windows-service-host.exe').write_bytes(b'fixture-exe')
            (root/'service.args').write_bytes(b'fixture-args')
            env=dict(os.environ, PREFLIGHT=str(ROOT/'infra/k8s/ha_control/windows-service-preflight.ps1'), FIXTURE_ROOT=str(root))
            script=r'''
$ErrorActionPreference='Stop'
. $env:PREFLIGHT
$HostRoot='C:\Dedicated';$HostSha256=Get-PreflightHash ([Text.Encoding]::UTF8.GetBytes('fixture-exe'))
$ArgsSha256=Get-PreflightHash ([Text.Encoding]::UTF8.GetBytes('fixture-args'))
$ServiceSid='S-1-5-80-1-2-3-4-5';$TrustedAdminSids=@('S-1-5-21-1')
$script:bad='';$script:checks=0
function Get-PreflightPlatform{return [PlatformID]::Win32NT}
function Get-LocalGroupMember($SID,$ErrorAction){return [pscustomobject]@{SID=[pscustomobject]@{Value='S-1-5-21-1'}}}
function Get-Item($LiteralPath,[switch]$Force,$ErrorAction){
 if($LiteralPath -ceq 'C:\Dedicated'){return [pscustomobject]@{Name='Dedicated';FullName=$env:FIXTURE_ROOT;PSIsContainer=$true;Attributes=0;Parent=[pscustomobject]@{Name='Private';FullName='C:\Private';Parent=$null}}}
 return Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force
}
function Get-ChildItem($LiteralPath,[switch]$Force,$ErrorAction){
 $items=@(Microsoft.PowerShell.Management\Get-ChildItem -LiteralPath $LiteralPath -Force)
 if($script:bad -ceq 'config'){return @($items+[pscustomobject]@{Name='windows-service-host.exe.config'})}
 if($script:bad -ceq 'sidecar'){return @($items+[pscustomobject]@{Name='AppDomainManager.dll'})}
 return $items
}
function Assert-PreflightEntry($Entry,$Writers,$Readers,$Private,$Ancestor){
 $script:checks++
 if($ServiceSid -in $Writers -or $ServiceSid -notin $Readers){throw 'service_write_or_read_boundary'}
 foreach($sid in $Writers){Assert-PreflightMask $sid 268435456 $Writers $Readers $Private $Ancestor}
 Assert-PreflightMask $ServiceSid 1 $Writers $Readers $Private $Ancestor
 if($script:bad -ceq 'read' -and $Entry.Name -ceq 'service.args'){Assert-PreflightMask 'S-1-1-0' 1 $Writers $Readers $true $false}
 if($script:bad -ceq 'write' -and $Entry.Name -ceq 'windows-service-host.exe'){Assert-PreflightMask 'S-1-5-32-545' 2 $Writers $Readers $true $false}
 if($script:bad -ceq 'servicewrite'){Assert-PreflightMask $ServiceSid 2 $Writers $Readers $true $false}
 if($script:bad -ceq 'reparse'){throw 'fixture_reparse_rejected'}
}
$good=Test-EnforcedServicePreflight|ConvertFrom-Json
if($good.ready -or $good.execution_performed -or $good.native_service_accepted -or $script:checks -ne 6){throw 'native_acceptance_overclaimed'}
$closed=0
foreach($bad in @('config','sidecar','read','write','servicewrite','reparse')){
 $script:bad=$bad;try{Test-EnforcedServicePreflight|Out-Null}catch{$closed++}
}
$script:bad='';$HostSha256='0'*64
try{Test-EnforcedServicePreflight|Out-Null}catch{$closed++}
if($closed -ne 7){throw 'preflight_boundary_accepted'}
'provider-only-preflight'
'''
            result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            self.assertEqual(result.stdout.decode().strip(),'provider-only-preflight')

    def test_new_powershell_scripts_parse_and_do_not_install_or_start(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        for name in ('build-service-host.ps1','windows-service-preflight.ps1'):
            source=ROOT/'infra/k8s/ha_control'/name
            env=dict(os.environ, CHECK_SCRIPT=str(source))
            script=r'''
$errors=$null;$tokens=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:CHECK_SCRIPT,[ref]$tokens,[ref]$errors)
if(@($errors).Count -ne 0){throw 'powershell_parse_failed'}
$commands=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst]},$true)|ForEach-Object {$_.GetCommandName()})
foreach($forbidden in @('Start-Service','New-Service','Set-Acl','Start-Process','Invoke-Expression','Restart-Computer','Start-VM','Set-VM')){if($forbidden -in $commands){throw 'native_mutation_in_preparation'}}
'AST-only'
'''
            result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',script],env=env,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            self.assertEqual(result.stdout.decode().strip(),'AST-only')

    def test_builder_rejects_changed_pins_and_preserves_existing_artifact(self):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        builder=ROOT/'infra/k8s/ha_control/build-service-host.ps1'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);pins=root/'pins.json';pins.write_text('{}')
            old=root/'old.exe';old.write_bytes(b'previous-artifact')
            for output,expected in [(root/'new.exe','build_input_changed'),(old,'build_output_exists')]:
                result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-File',str(builder),'-Source',str(HOST),'-SourceSha256',P.sha(HOST.read_bytes()),'-ReferenceRoot',str(root),'-Pins',str(pins),'-PinsSha256','0'*64,'-Output',str(output)],capture_output=True,timeout=20)
                self.assertNotEqual(result.returncode,0)
                self.assertIn(expected,result.stderr.decode(errors='replace'))
                self.assertEqual(old.read_bytes(),b'previous-artifact')
                self.assertFalse((root/'new.exe').exists())

    def test_private_inputs_owner_mode_link_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'private.json';path.write_bytes(b'{}');path.chmod(0o600)
            self.assertEqual(P.private(path,P.sha(b'{}')),b'{}')
            for mode in (0o644,0o660):
                path.chmod(mode)
                with self.assertRaises(P.ProvisionError):P.private(path,P.sha(b'{}'))
            path.chmod(0o600)
            with self.assertRaises(P.ProvisionError):P.private(path,'0'*64)
            link=root/'link';link.symlink_to(path)
            with self.assertRaises(P.ProvisionError):P.private(link,P.sha(b'{}'))
            link.unlink();os.link(path,link)
            with self.assertRaises(P.ProvisionError):P.private(path,P.sha(b'{}'))
            link.unlink();root.chmod(0o755)
            with self.assertRaises(P.ProvisionError):P.private(path,P.sha(b'{}'))

    def make_inputs(self, root):
        bundle=root/'bundle';bundle.mkdir();(bundle/'source-manifest.json').write_bytes(b'fixture-bundle')
        (bundle/'files/ha_control').mkdir(parents=True);shutil.copy2(ROOT/'infra/k8s/ha_control/windows-service-preflight.ps1',bundle/'files/ha_control/windows-service-preflight.ps1')
        artifact=root/'host.exe';artifact.write_bytes(pe());artifact.chmod(0o600)
        source=HOST.read_bytes();args=settings();args['host_sha256']=P.sha(pe())
        trust=dict(format=1,approved_commits=[COMMIT],bundle_manifest_sha256=P.sha(b'fixture-bundle'),
            host_source_sha256=P.sha(source),host_exe_sha256=P.sha(pe()),service_args=args,
            build=dict(source_sha256=P.sha(source),artifact_sha256=P.sha(pe()),target='.NETFramework,Version=v4.8',
                       platform='AnyCPU',output_kind='ConsoleApplication',native_accepted=False,deterministic_outputs=2,
                       compiler={'Microsoft.CodeAnalysis.dll':'1'*64,'Microsoft.CodeAnalysis.CSharp.dll':'2'*64},
                       references={name:'3'*64 for name in ('mscorlib.dll','System.dll','System.Core.dll','System.ServiceProcess.dll')},pins_sha256='0'*64))
        trust['build']['pins_sha256']=P.sha(P.controller_bundle.encode({key:trust['build'][key] for key in ('compiler','references')}))
        manifest=root/'private.json'
        def save():
            raw=json.dumps(trust).encode();manifest.write_bytes(raw);manifest.chmod(0o600);return P.sha(raw)
        return bundle,artifact,manifest,trust,source,save

    def test_inert_package_verified_then_tamper_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bundle,artifact,manifest,trust,source,save=self.make_inputs(root)
            with patch.object(P.controller_bundle,'verify',return_value={'source_commit':COMMIT}),patch.object(P,'source_at',return_value=source):
                result=P.prepare(COMMIT,bundle,artifact,manifest,save(),root/'out')
                self.assertFalse(result['ready']);self.assertFalse(result['execution_performed']);self.assertFalse(result['installed'])
                self.assertNotIn('private.json',P.controller_bundle.inventory(root/'out'))
                plan=json.loads((root/'out/install-plan.json').read_bytes())
                self.assertEqual(plan['preflight']['run_before'],'any_StartService')
                self.assertEqual(plan['journal']['path'],r'C:\Private\journal\controller-state.json')
                self.assertTrue(plan['journal']['authority_parent_immutable'])
                self.assertEqual(plan['preflight']['host_files'],{'windows-service-host.exe':P.sha(pe()),'service.args':P.sha(P.render_args(trust['service_args']))})
                self.assertEqual(plan['preflight']['sha256'],P.sha((bundle/'files/ha_control/windows-service-preflight.ps1').read_bytes()))
                P.verify(root/'out',result['manifest_sha256'])
                with self.assertRaises(P.ProvisionError):P.prepare(COMMIT,bundle,artifact,manifest,save(),root/'out')
                (root/'out'/'service.args').chmod(0o600)
                (root/'out'/'service.args').write_bytes(b'changed')
                with self.assertRaises(P.ProvisionError):P.verify(root/'out',result['manifest_sha256'])
                (root/'out'/'service.args').unlink();(root/'out'/'service.args').symlink_to(artifact)
                with self.assertRaises(P.controller_bundle.BundleError):P.verify(root/'out',result['manifest_sha256'])

    def bundle_provision_roundtrip(self, artifact_bytes, build_evidence, evidence_directory=None):
        """Exact source Git/bundle APIs; caller identifies artifact evidence scope."""
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);root.chmod(0o700)
            repo=root/'source';repo.mkdir()
            for name in P.controller_bundle.SOURCES:
                target=repo/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((ROOT/name).read_bytes())
            def git(*args):
                result=subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*args],capture_output=True,check=True)
                return result.stdout.decode().strip()
            git('init','-q');git('add','.')
            git('-c','user.name=Isolated Fixture','-c','user.email=fixture@example.invalid',
                '-c','commit.gpgsign=false','commit','-qm','isolated source fixture')
            commit=git('rev-parse','HEAD')
            private=root/'private';private.mkdir(mode=0o700)
            artifact=private/'host.exe';artifact.write_bytes(artifact_bytes);artifact.chmod(0o600)
            args=settings();args['host_sha256']=P.sha(artifact_bytes)
            trust=dict(format=1,approved_commits=[commit],bundle_manifest_sha256=None,
                host_source_sha256=P.sha(HOST.read_bytes()),host_exe_sha256=P.sha(artifact_bytes),
                service_args=args,build=build_evidence)
            with patch.object(P.controller_bundle,'ROOT',repo):
                bundle=root/'bundle'
                prepared=P.controller_bundle.prepare(commit,bundle)
                bound=P.controller_bundle.verify(bundle,prepared['manifest_sha256'])
                self.assertFalse(bound['ready']);self.assertFalse(bound['approval_issued'])
                trust['bundle_manifest_sha256']=prepared['manifest_sha256']
                manifest=private/'private.json';raw=P.controller_bundle.encode(trust)
                manifest.write_bytes(raw);manifest.chmod(0o600)
                output=root/'provision'
                result=P.prepare(commit,bundle,artifact,manifest,P.sha(raw),output)
                checked=P.verify(output,result['manifest_sha256'])
                plan=json.loads((output/'install-plan.json').read_bytes())
                preflight=output/plan['preflight']['bundle_path']
                self.assertTrue(preflight.is_file())
                self.assertEqual(P.sha(preflight.read_bytes()),plan['preflight']['sha256'])
                self.assertEqual(plan['preflight']['bundle_path'],'controller/files/ha_control/windows-service-preflight.ps1')
                self.assertEqual((output/'windows-service-host.exe').read_bytes(),artifact_bytes)
                self.assertEqual(checked['source_commit'],commit)
                for key in ('ready','execution_performed','native_accepted','installed'):
                    self.assertIs(checked[key],False)
                if evidence_directory is not None:
                    evidence_directory=Path(evidence_directory);evidence_directory.mkdir(mode=0o700)
                    shutil.copytree(bundle,evidence_directory/'bundle')
                    shutil.copytree(output,evidence_directory/'provision')
                return {'scope':'offline_exact_Git_bundle_provision_roundtrip','source_commit':commit,
                    'bundle_manifest_sha256':prepared['manifest_sha256'],
                    'provision_manifest_sha256':result['manifest_sha256'],
                    'host_sha256':P.sha(artifact_bytes),'preflight_sha256':P.sha(preflight.read_bytes()),
                    'native_accepted':False,'approval_issued':False,'execution_performed':False}

    def test_exact_git_bundle_to_provision_roundtrip_shape_fixture(self):
        # Real source/bundle/provision functions; PE fixture is shape-only.
        # A separately recorded run passes the actual net48 EXE/build evidence.
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,_,_,trust,_,_=self.make_inputs(root)
            self.bundle_provision_roundtrip(pe(),trust['build'])

    def test_allowlist_source_build_and_artifact_pins_fail_closed(self):
        for mutation in ('commit','source','artifact','build','native','compiler','references','pins','deterministic'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);bundle,artifact,manifest,trust,source,save=self.make_inputs(root)
                if mutation=='commit':trust['approved_commits']=['0'*40]
                elif mutation=='source':trust['host_source_sha256']='0'*64
                elif mutation=='artifact':artifact.write_bytes(b'changed')
                elif mutation=='build':trust['build']['source_sha256']='0'*64
                elif mutation=='native':trust['build']['native_accepted']=True
                elif mutation=='compiler':trust['build']['compiler']['Microsoft.CodeAnalysis.dll']='0'*64
                elif mutation=='references':trust['build']['references']['mscorlib.dll']='0'*64
                elif mutation=='pins':trust['build']['pins_sha256']='0'*64
                else:trust['build']['deterministic_outputs']=1
                with patch.object(P.controller_bundle,'verify',return_value={'source_commit':COMMIT}),patch.object(P,'source_at',return_value=source):
                    with self.assertRaises(P.ProvisionError):P.prepare(COMMIT,bundle,artifact,manifest,save(),root/'out')
                self.assertFalse((root/'out').exists())

    def test_pe_shape_and_duplicate_manifest_not_native_proof(self):
        P.host_pe(pe())
        for raw in (b'not-pe',pe()[:200],pe().replace(b'PE\0\0',b'NO\0\0')):
            with self.assertRaises(P.ProvisionError):P.host_pe(raw)
        with self.assertRaises(P.ProvisionError):P.decode(b'{"format":1,"format":1}')


if __name__=='__main__':
    unittest.main()
