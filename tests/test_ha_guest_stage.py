"""Stage input and bounded decompression, never Windows ACL/VM acceptance."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('HA_GUEST_STAGE_SOURCE',str(ROOT/'infra/k8s/ha_control/windows-guest-stage.ps1')))


class GuestStageTests(unittest.TestCase):
    def ps(self,body,**extra):
        pwsh=shutil.which('pwsh') or '/private/tmp/n100-ha-powershell/runtime/pwsh'
        result=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-Command',"$ErrorActionPreference='Stop'\n. $env:STAGE_SOURCE\n"+body],env=dict(os.environ,STAGE_SOURCE=str(SOURCE),**extra),capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def test_exact_manifest_and_native_intent(self):
        value={'schema_version':1,'source_commit':'a'*40,'installation_id':'b'*64,'compressed_sha256':'c'*64,'compressed_bytes':2097152,'vhdx_sha256':'d'*64,'vhdx_bytes':4194304}
        raw=json.dumps(value).encode()
        self.assertEqual(self.ps(r'''
$script:Manifest=$env:MANIFEST;$script:ManifestSha256=$env:MANIFEST_SHA
$v=Read-GuestStageManifest
if($v.vhdx_bytes -ne 4194304){throw 'manifest_value_changed'}
$blocked=$false;try{Invoke-GuestStage}catch{$blocked=$true}
if(-not $blocked){throw 'unarmed_stage_accepted'}
foreach($field in @('command','unknown')){
 $v|Add-Member -NotePropertyName $field -NotePropertyValue 'x'
 $bytes=[Text.Encoding]::UTF8.GetBytes(($v|ConvertTo-Json -Compress));$script:Manifest=[Convert]::ToBase64String($bytes);$script:ManifestSha256=Get-GuestStageHash $bytes
 $blocked=$false;try{Read-GuestStageManifest|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'extra_fields_accepted'}
 $v.PSObject.Properties.Remove($field)
}
'manifest-only'
''',MANIFEST=base64.b64encode(raw).decode(),MANIFEST_SHA=hashlib.sha256(raw).hexdigest()),'manifest-only')

    def test_real_gzip_expansion_checks_bound_length_and_hash(self):
        raw=b'bounded actual decompression\n'*4096;compressed=gzip.compress(raw,mtime=0)
        self.assertEqual(self.ps(r'''
$bytes=[Convert]::FromBase64String($env:GZIP)
$source=[IO.MemoryStream]::new($bytes);$target=[IO.MemoryStream]::new()
Expand-GuestStageStream $source $target ([long]$env:EXPECTED_SIZE) $env:EXPECTED_SHA
if($target.Length -ne [long]$env:EXPECTED_SIZE){throw 'wrong_output'}
foreach($kind in @('oversize','undersize','hash')){
 $source.Position=0;$target=[IO.MemoryStream]::new();$length=[long]$env:EXPECTED_SIZE;$hash=$env:EXPECTED_SHA
 if($kind -ceq 'oversize'){$length=16}elseif($kind -ceq 'undersize'){$length++}else{$hash='0'*64}
 $blocked=$false;try{Expand-GuestStageStream $source $target $length $hash}catch{$blocked=$true}
 if(-not $blocked){throw 'invalid_expansion_accepted'}
 if($kind -ceq 'oversize' -and $target.Length -gt 16){throw 'over_limit_write'}
}
'bounded-gzip-only'
''',GZIP=base64.b64encode(compressed).decode(),EXPECTED_SIZE=str(len(raw)),EXPECTED_SHA=hashlib.sha256(raw).hexdigest()),'bounded-gzip-only')


if __name__=='__main__':
    unittest.main()
