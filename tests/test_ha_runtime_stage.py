"""Portable archive/manifest rules and parser only; never Windows acceptance."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile

ROOT=Path(os.environ.get('HA_RUNTIME_STAGE_TEST_ROOT',str(Path(__file__).resolve().parents[1])))
SOURCE=Path(os.environ.get('HA_RUNTIME_STAGE_TEST_SOURCE',str(ROOT/'infra/k8s/ha_control/windows-runtime-stage.ps1')))
PWSH=shutil.which('pwsh') or next((str(p) for p in (Path('/private/tmp/n100-ha-powershell/runtime/pwsh'),ROOT/'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh') if p.is_file()),'pwsh')


def fixture(folder, host=False):
    files={'python/python.exe':b'exe','python/python313.dll':b'dll','python/python313.zip':b'zip',
           'python/python313._pth':b'python313.zip\n.\nLib/site-packages\n',
           'python/Lib/site-packages/psycopg/__init__.py':b'',
           'python/Lib/site-packages/psycopg_binary/__init__.py':b'',
           'python/Lib/site-packages/tzdata/__init__.py':b''}
    for i in range(769):files[f'python/filler/f{i:04}.py']=b''
    files.update({'controller/source-manifest.json':b'{}','controller/files/ha_control/launcher.py':b'pass\n'})
    if host:files.update({'host/windows-service-host.exe':b'host','host/service.args':b'fixture-only\n'})
    archive=folder/'runtime.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):z.writestr(name,data)
    install='b'*64;name='N100HA_'+install[:24]+'_controller'
    sid='S-1-5-80-'+'-'.join(map(str,struct.unpack('<IIIII',hashlib.sha1(name.upper().encode('utf-16le')).digest())))
    value=dict(schema_version=1,source_commit='a'*40,installation_id=install,
               archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),archive_bytes=archive.stat().st_size,
               service_name=name,service_sid=sid,
               files={p:dict(filename=p,sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),component='python-runtime' if p.startswith('python/') else 'service-host' if p.startswith('host/') else 'controller-code') for p,data in files.items()})
    (folder/'manifest.json').write_text(json.dumps(value,separators=(',',':')))
    return value


class RuntimeStageTests(unittest.TestCase):
    def ps(self, script, folder=None):
        value=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-Command',
            "$ErrorActionPreference='Stop';. $env:RUNTIME_STAGE_SOURCE;Initialize-RuntimeStage\n"+script],
            env=dict(os.environ,RUNTIME_STAGE_SOURCE=str(SOURCE),FIXTURE=str(folder or '')),
            capture_output=True,text=True,timeout=30)
        self.assertEqual(value.returncode,0,value.stderr)
        return value.stdout.strip()

    def test_portable_compile_and_known_service_sid(self):
        self.assertEqual(self.ps(r'''
$s=[HaRuntimeStageV1.Rules]::ServiceSid('TrustedInstaller')
if($s -cne 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'){throw 'wrong_sha1_sid'}
'portable-only'
'''),'portable-only')

    def test_clr_parent_walk_uses_real_filesystem_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);(folder/'leaf').write_text('fixture')
            self.ps(r'''
$entry=[IO.FileInfo]::new((Join-Path $env:FIXTURE 'leaf'));$count=0
while($null -ne $entry){if($entry -isnot [IO.FileInfo] -and $entry -isnot [IO.DirectoryInfo]){throw 'not_clr_entry'};$count++;$entry=Get-RuntimeStageParent $entry}
if($count -lt 3){throw 'ancestor_not_walked'}
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:RUNTIME_STAGE_SOURCE,[ref]$t,[ref]$e)
$f=$ast.Find({param($a)$a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Assert-RuntimeStagePath'},$true)
if($f.Extent.Text.Contains('.PSIsContainer') -or -not $f.Extent.Text.Contains('Get-RuntimeStageParent')){throw 'raw_parent_extended_property'}
''',folder)

    def test_native_boundary_blocks_before_any_mutation(self):
        self.ps(r'''
$Go=$true;function New-RuntimeStageDirectory{throw 'unexpected_mutation'}
$closed=$false;try{Invoke-RuntimeStage}catch{if($_.Exception.Message -cne 'runtime_stage_native_intent_required'){throw};$closed=$true}
if(-not $closed){throw 'native_gate_open'}
''')

    def test_manifest_zip_exact_inventory_and_optional_host(self):
        for host in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp);fixture(folder,host)
                self.ps(r'''
$s=[HaRuntimeStageV1.Rules]::Manifest([IO.File]::ReadAllText((Join-Path $env:FIXTURE 'manifest.json')))
$f=[IO.File]::OpenRead((Join-Path $env:FIXTURE 'runtime.zip'));$z=[IO.Compression.ZipArchive]::new($f,[IO.Compression.ZipArchiveMode]::Read)
try{[HaRuntimeStageV1.Rules]::Zip($z,$s);if($s.Files.Count -notin @(778,780)){throw 'inventory_count'}}finally{$z.Dispose();$f.Dispose()}
''',folder)

    def test_unsafe_windows_paths_rejected_before_extraction(self):
        self.ps(r'''
foreach($name in @('../escape','/absolute','C:/ads','a\escape','a//b','a/./b','a/../b','a/nul.txt','a/CON','a/COM1.py','a/LPT9.txt','a/trailing.','a/trailing ','a/file:stream','a/hook.pth','a/*',('a/'+[char]0+'b'))){
 $closed=$false;try{$null=[HaRuntimeStageV1.Rules]::Name($name)}catch{$closed=$true};if(-not $closed){throw 'unsafe_path'}
}
''')

    def test_type_duplicates_case_collision_size_and_confused_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);original=fixture(folder);bad=[]
            for key,value in [('schema_version',True),('schema_version',1.0),('archive_bytes','42'),('service_sid','S-1-5-18')]:
                v=json.loads(json.dumps(original));v[key]=value;bad.append(json.dumps(v))
            v=json.loads(json.dumps(original));v['files']['python/python.exe']['filename']='python/other.exe';bad.append(json.dumps(v))
            v=json.loads(json.dumps(original));v['files']['Python/python.exe']=dict(v['files']['python/python.exe'],filename='Python/python.exe');bad.append(json.dumps(v))
            v=json.loads(json.dumps(original));v['files']['python/python.exe']['bytes']=67108865;bad.append(json.dumps(v))
            v=json.loads(json.dumps(original));row=v['files'].pop('python/filler/f0000.py');row['filename']='python/lib/extra.py';v['files'][row['filename']]=row;bad.append(json.dumps(v))
            v=json.loads(json.dumps(original));v['files']['host/service.args']=dict(filename='host/service.args',sha256='a'*64,bytes=0,component='service-host');bad.append(json.dumps(v))
            v=json.loads(json.dumps(original))
            for row in list(v['files'].values())[:9]:row['bytes']=67108864
            bad.append(json.dumps(v))
            bad.append('{"schema_version":1,'+json.dumps(original)[1:])
            (folder/'bad.json').write_text(json.dumps(bad))
            self.ps(r'''
$values=[HaRuntimeStageV1.Json]::Object([IO.File]::ReadAllText((Join-Path $env:FIXTURE 'manifest.json')),2097152)
$cases=[IO.File]::ReadAllText((Join-Path $env:FIXTURE 'bad.json'))|ConvertFrom-Json
foreach($text in $cases){$closed=$false;try{$null=[HaRuntimeStageV1.Rules]::Manifest($text)}catch{$closed=$true};if(-not $closed){throw 'unsafe_manifest'}}
''',folder)

    def test_zip_extra_symlink_and_case_alias_are_rejected(self):
        for kind in ('extra','symlink','case'):
            with tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp);fixture(folder)
                with zipfile.ZipFile(folder/'runtime.zip') as z:rows=[(i,z.read(i)) for i in z.infolist()]
                with zipfile.ZipFile(folder/'bad.zip','w') as z:
                    for i,data in rows:
                        if i.filename=='python/python.exe':
                            if kind=='symlink':i.external_attr=0o120777<<16
                            if kind=='case':i.filename='python/Python.exe'
                        z.writestr(i,data)
                    if kind=='extra':z.writestr('controller/extra.py',b'pass')
                self.ps(r'''
$s=[HaRuntimeStageV1.Rules]::Manifest([IO.File]::ReadAllText((Join-Path $env:FIXTURE 'manifest.json')));$f=[IO.File]::OpenRead((Join-Path $env:FIXTURE 'bad.zip'));$z=[IO.Compression.ZipArchive]::new($f,[IO.Compression.ZipArchiveMode]::Read)
try{$closed=$false;try{[HaRuntimeStageV1.Rules]::Zip($z,$s)}catch{$closed=$true};if(-not $closed){throw 'bad_zip_accepted'}}finally{$z.Dispose();$f.Dispose()}
''',folder)

    def test_extraction_create_new_hash_and_closed_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);fixture(folder)
            self.ps(r'''
$s=[HaRuntimeStageV1.Rules]::Manifest([IO.File]::ReadAllText((Join-Path $env:FIXTURE 'manifest.json')));$f=[IO.File]::OpenRead((Join-Path $env:FIXTURE 'runtime.zip'));$z=[IO.Compression.ZipArchive]::new($f,[IO.Compression.ZipArchiveMode]::Read)
$root=Join-Path $env:FIXTURE 'new';$null=[IO.Directory]::CreateDirectory($root)
try{foreach($e in $z.Entries){[HaRuntimeStageV1.Engine]::Extract($e,$s.Files[$e.FullName],$root)}
[HaRuntimeStageV1.Engine]::RuntimePathFile($root);$items=[HaRuntimeStageV1.Engine]::Inventory($root,$s,$false);if($items.Count -ne 778){throw 'missing_file'}
$extra=Join-Path $root 'undeclared-empty';$null=[IO.Directory]::CreateDirectory($extra);$closed=$false;try{$null=[HaRuntimeStageV1.Engine]::Inventory($root,$s,$false)}catch{$closed=$true};[IO.Directory]::Delete($extra);if(-not $closed){throw 'extra_directory_accepted'}
$e=$z.GetEntry('python/python.exe');$closed=$false;try{[HaRuntimeStageV1.Engine]::Extract($e,$s.Files[$e.FullName],$root)}catch{$closed=$true};if(-not $closed){throw 'overwrite_accepted'}
[IO.File]::WriteAllText((Join-Path $root 'python/python.exe'),'confused');$closed=$false;try{$null=[HaRuntimeStageV1.Engine]::Inventory($root,$s,$false)}catch{$closed=$true};if(-not $closed){throw 'changed_file_accepted'}
}finally{$z.Dispose();$f.Dispose()}
''',folder)

    def test_stream_hash_mismatch_leaves_failure_and_never_follows_parent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);fixture(folder)
            outside=folder/'outside';outside.mkdir();linked=folder/'linked';linked.mkdir();(linked/'python').symlink_to(outside,target_is_directory=True)
            self.ps(r'''
$s=[HaRuntimeStageV1.Rules]::Manifest([IO.File]::ReadAllText((Join-Path $env:FIXTURE 'manifest.json')));$f=[IO.File]::OpenRead((Join-Path $env:FIXTURE 'runtime.zip'));$z=[IO.Compression.ZipArchive]::new($f,[IO.Compression.ZipArchiveMode]::Read)
try{$e=$z.GetEntry('python/python.exe');$root=Join-Path $env:FIXTURE 'wrong-hash';$null=[IO.Directory]::CreateDirectory($root);$s.Files[$e.FullName].Hash='a'*64;$closed=$false;try{[HaRuntimeStageV1.Engine]::Extract($e,$s.Files[$e.FullName],$root)}catch{$closed=$true};if(-not $closed){throw 'hash_mismatch_accepted'}
$closed=$false;try{[HaRuntimeStageV1.Engine]::Extract($e,$s.Files[$e.FullName],(Join-Path $env:FIXTURE 'linked'))}catch{$closed=$true};if(-not $closed){throw 'parent_symlink_accepted'}
if(@(Get-ChildItem -LiteralPath (Join-Path $env:FIXTURE 'outside')).Count){throw 'outside_root_mutated'}
}finally{$z.Dispose();$f.Dispose()}
''',folder)

    def test_signature_gate_publisher_and_status_rules_only(self):
        self.ps(r'''
$script:status='Valid';$script:subject='CN=Python Software Foundation, O=Python Software Foundation'
function Get-AuthenticodeSignature{return [pscustomobject]@{Status=$script:status;SignerCertificate=[pscustomobject]@{Subject=$script:subject}}}
Assert-RuntimeStageSignatures '/portable-fixture'
$script:subject='CN=Other Publisher';$closed=$false;try{Assert-RuntimeStageSignatures '/portable-fixture'}catch{$closed=$true};if(-not $closed){throw 'wrong_publisher'}
$script:subject='O=Python Software Foundation';$script:status='NotSigned';$closed=$false;try{Assert-RuntimeStageSignatures '/portable-fixture'}catch{$closed=$true};if(-not $closed){throw 'unsigned_accepted'}
''')

    def test_ast_scope_write_ahead_and_only_fixed_child(self):
        self.ps(r'''
$t=$null;$e=$null;$ast=[Management.Automation.Language.Parser]::ParseFile($env:RUNTIME_STAGE_SOURCE,[ref]$t,[ref]$e);if($e.Count){throw 'parse_failed'}
$names=@($ast.FindAll({param($a)$a -is [Management.Automation.Language.CommandAst]},$true)|%{$_.GetCommandName()})
foreach($bad in @('New-Service','Start-Service','Set-Service','Add-LocalGroupMember','Set-WmiInstance','Invoke-CimMethod','New-VM','Set-VM','Start-VM','Stop-VM','New-NetNat','Invoke-WebRequest','pip','Remove-Item')){if($bad -in $names){throw 'forbidden_action'}}
$text=[IO.File]::ReadAllText($env:RUNTIME_STAGE_SOURCE)
if($text.IndexOf("Write-RuntimeStageEvent `$journal 'intent'") -gt $text.IndexOf('New-RuntimeStageDirectory $target')){throw 'journal_after_mutation'}
if(-not $text.Contains('[IO.FileMode]::CreateNew') -or -not $text.Contains('$file.Flush($true)') -or -not $text.Contains('-I -B -c') -or -not $text.Contains('15000') -or -not $text.Contains('EnvironmentVariables.Clear()')){throw 'guard_missing'}
''')


if __name__=='__main__':unittest.main()
