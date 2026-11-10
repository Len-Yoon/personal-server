"""Portable SD and real mode-dispatch contracts, not native WMI or HA acceptance.

No WMI, SCM, service, or host mutation is executed by these fixtures.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'infra/k8s/ha_control/windows-namespace-policy.ps1'
PWSH = ROOT / 'build/ha-predeploy-evidence/windows-build/powershell-7.5.4/pwsh'
INSTALLATION = '4' * 64
SERVICE = 'N100HA_' + INSTALLATION[:24] + '_controller'
SERVICE_SID = 'S-1-5-80-' + '-'.join(str(x) for x in struct.unpack('<5I', hashlib.sha1(SERVICE.upper().encode('utf-16le')).digest()))


def sid(value: str) -> bytes:
    parts = value.split('-')
    return bytes([1, len(parts) - 3]) + int(parts[2]).to_bytes(6, 'big') + b''.join(struct.pack('<I', int(x)) for x in parts[3:])


def ace(identity: str, mask: int, flags: int = 0x12, kind: int = 0) -> bytes:
    value = sid(identity)
    return struct.pack('<BBHI', kind, flags, 8 + len(value), mask) + value


def acl(rows: list[bytes]) -> bytes:
    return struct.pack('<BBHHH', 2, 0, 8 + sum(map(len, rows)), len(rows), 0) + b''.join(rows)


def descriptor(rows: list[bytes] | None = None, *, flags: int = 0x8414) -> bytes:
    if rows is None:
        rows = [ace('S-1-5-32-578', 0x21), ace('S-1-5-32-544', 0x6003F), ace('S-1-5-18', 0x6003F), ace('S-1-5-20', 19), ace('S-1-5-19', 19), ace('S-1-5-11', 19)]
    # Audit ACE is preserved byte-for-byte, including flags and mask.
    components = [sid('S-1-5-32-544'), sid('S-1-5-18'), acl([ace('S-1-1-0', 2, 0xC0, 2)]), acl(rows)]
    result = bytearray(20)
    offsets = []
    for value in components:
        result += b'\0' * ((-len(result)) % 4)
        offsets.append(len(result))
        result += value
    struct.pack_into('<BBHIIII', result, 0, 1, 0, flags, *offsets)
    return bytes(result)


def components(value: bytes) -> list[bytes]:
    result = []
    for index, offset in enumerate(struct.unpack_from('<IIII', value, 4)):
        size = 8 + value[offset + 1] * 4 if index < 2 else struct.unpack_from('<H', value, offset + 2)[0]
        result.append(value[offset:offset + size])
    return result


def aces(value: bytes) -> list[bytes]:
    dacl = components(value)[3]
    result, offset = [], 8
    for _ in range(struct.unpack_from('<H', dacl, 4)[0]):
        size = struct.unpack_from('<H', dacl, offset + 2)[0]
        result.append(dacl[offset:offset + size])
        offset += size
    return result


class NamespacePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = str(PWSH) if PWSH.is_file() else shutil.which('pwsh')
        if not cls.pwsh:
            raise RuntimeError('PowerShell parser/runtime is required for policy tests')

    def ps(self, body: str) -> str:
        preamble = f"$ErrorActionPreference='Stop'\n. '{SOURCE}'\nInitialize-NpCore\n"
        with tempfile.TemporaryDirectory(prefix='ha-namespace-policy-') as directory:
            script = Path(directory) / 'test.ps1'
            script.write_text(preamble + body)
            result = subprocess.run([self.pwsh, '-NoLogo', '-NoProfile', '-File', str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2500:] + result.stdout[-1000:])
        return result.stdout

    def build(self, original: bytes, transition='protect-and-materialize-v1', acknowledge=True) -> bytes:
        output = self.ps(f"$b=[Convert]::FromBase64String('{base64.b64encode(original).decode()}');$r=[HaNamespacePolicyCoreV1]::Build($b,'{SERVICE_SID}','{transition}',${str(acknowledge).lower()},$true,[string[]]@('S-1-5-21-1-2-3-4'));[Console]::Out.WriteLine([Convert]::ToBase64String($r))")
        return base64.b64decode(output.strip())

    def test_materialization_preserves_full_sd_and_non_target_aces(self):
        original = descriptor()
        target = self.build(original)
        before, after = components(original), components(target)
        self.assertEqual(before[:3], after[:3])
        self.assertEqual(struct.unpack_from('<H', target, 2)[0], struct.unpack_from('<H', original, 2)[0] | 0x1000)
        old, new = aces(original), aces(target)
        self.assertEqual(len(new), len(old) + 1)
        for index, row in enumerate(old):
            expected = bytearray(row)
            expected[1] &= ~16
            if index >= 3:
                struct.pack_into('<I', expected, 4, 1)
            self.assertEqual(new[index], expected)
        self.assertEqual(new[-1], ace(SERVICE_SID, 0x20003, 0))
        # Match the actual enforcer write mask, not an invented access model.
        self.assertIn('Assert-EnforcedDescriptor $sd $writers 1343029278', (ROOT / 'infra/k8s/ha_control/windows-enforced-vm.ps1').read_text())
        for row in new:
            if row[8:] not in [sid('S-1-5-18'), sid('S-1-5-32-544'), sid(SERVICE_SID)]:
                self.assertEqual(struct.unpack_from('<I', row, 4)[0] & 1343029278, 0)

    def test_preserve_blocks_inherited_targets_and_requires_explicit_ack(self):
        value = base64.b64encode(descriptor()).decode()
        self.ps(f"$b=[Convert]::FromBase64String('{value}');foreach($case in @(@('preserve',$false),@('protect-and-materialize-v1',$false),@('unknown',$true))){{$closed=$false;try{{$null=[HaNamespacePolicyCoreV1]::Build($b,'{SERVICE_SID}',$case[0],$case[1],$true,[string[]]@())}}catch{{$closed=$true}};if(-not $closed){{throw 'inheritance_bypass'}}}}")

    def test_preserve_explicit_target_aces_does_not_change_other_flags(self):
        rows = aces(descriptor())
        rows = [row[:1] + bytes([row[1] & ~16]) + row[2:] if index >= 3 else row for index, row in enumerate(rows)]
        original = descriptor(rows)
        target = self.build(original, 'preserve', False)
        new = [row for row in aces(target) if row[8:] != sid(SERVICE_SID)]
        self.assertEqual(new[:3], rows[:3])
        self.assertEqual(struct.unpack_from('<H', target, 2)[0], struct.unpack_from('<H', original, 2)[0])

    def test_descriptor_rejects_duplicates_unknown_writers_callbacks_and_missing_sacl_proof(self):
        rows = aces(descriptor())
        invalid = [descriptor(rows + [ace('S-1-5-11', 19)]), descriptor(rows + [ace('S-1-5-21-9-9-9-9', 2)]), descriptor(rows + [ace(SERVICE_SID, 3)]), descriptor(rows[:-1] + [ace('S-1-5-11', 19, kind=1)]), descriptor(rows[:-1] + [ace('S-1-5-11', 19, kind=9)]), descriptor(rows[:-1] + [ace('S-1-5-11', 19, flags=0x1A)]), descriptor()[:19]]
        values = ','.join("'" + base64.b64encode(x).decode() + "'" for x in invalid)
        self.ps(f"foreach($v in @({values})){{$closed=$false;try{{$null=[HaNamespacePolicyCoreV1]::Build([Convert]::FromBase64String($v),'{SERVICE_SID}','protect-and-materialize-v1',$true,$true,[string[]]@())}}catch{{$closed=$true}};if(-not $closed){{throw 'bad_descriptor_accepted'}}}};$closed=$false;try{{$null=[HaNamespacePolicyCoreV1]::Build([Convert]::FromBase64String('{base64.b64encode(descriptor()).decode()}'),'{SERVICE_SID}','protect-and-materialize-v1',$true,$false,[string[]]@())}}catch{{$closed=$true}};if(-not $closed){{throw 'sacl_unknown_accepted'}}")

    def test_scm_control_writers_include_pause_user_defined_and_generic_execute(self):
        originals = [descriptor(aces(descriptor()) + [ace('S-1-5-21-9-9-9-9', mask, flags=0)]) for mask in (0x40, 0x100, 0x20000000)]
        values = ','.join("'" + base64.b64encode(x).decode() + "'" for x in originals)
        self.ps(f"foreach($value in @({values})){{$closed=$false;try{{[HaNamespacePolicyCoreV1]::AssertWriters([Convert]::FromBase64String($value),[string[]]@('S-1-5-18','S-1-5-32-544'),1879900530)}}catch{{$closed=$true}};if(-not $closed){{throw 'scm_untrusted_control_accepted'}}}}")

    def test_service_sid_uses_actual_derived_sha1_contract(self):
        self.ps(f"if([HaNamespacePolicyCoreV1]::ServiceName('{INSTALLATION}') -cne '{SERVICE}' -or [HaNamespacePolicyCoreV1]::ServiceSid('{SERVICE}') -cne '{SERVICE_SID}'){{throw 'service_sid_mismatch'}}")

    def manifest(self):
        return dict(schema_version=1, source_commit='a' * 40, installation_id=INSTALLATION, service_source_commit='b' * 40, service_name=SERVICE, service_sid=SERVICE_SID, service_config_sha256='1' * 64, service_sd_sha256='9' * 64, service_image_sha256='2' * 64, service_ownership_receipt_sha256='3' * 64, protected_workloads_receipt_sha256='4' * 64, provider_semantics_receipt_sha256='5' * 64, original_sd_sha256='6' * 64, parent_sd_sha256='7' * 64, children_sha256='8' * 64, inheritance_transition='protect-and-materialize-v1', acknowledge_parent_policy_no_longer_inherited=True, acknowledge_namespace_wide_not_vm_specific=True, trusted_admin_sids=['S-1-5-21-1-2-3-4'])

    def test_manifest_strict_types_service_binding_unknown_fields_and_escaped_duplicate_keys(self):
        encoded = base64.b64encode(json.dumps(self.manifest()).encode()).decode()
        self.ps(f"$v=Read-NpJson ([Convert]::FromBase64String('{encoded}'));Assert-NpManifest $v;foreach($case in @(@('schema_version',$true),@('acknowledge_parent_policy_no_longer_inherited','true'),@('acknowledge_namespace_wide_not_vm_specific',$false),@('service_sid','S-1-5-18'),@('service_name','existing-service'),@('inheritance_transition','implicit'))){{$old=$v.($case[0]);$v.($case[0])=$case[1];$closed=$false;try{{Assert-NpManifest $v}}catch{{$closed=$true}};$v.($case[0])=$old;if(-not $closed){{throw 'manifest_coercion'}}}};$v|Add-Member NoteProperty extra 'x';$closed=$false;try{{Assert-NpManifest $v}}catch{{$closed=$true}};if(-not $closed){{throw 'extra_accepted'}}")
        self.ps(r'''foreach($text in @('{"schema_version":1,"schema_version":2}','{"schema_version":1,"\u0073chema_version":2}')){$closed=$false;try{$null=Read-NpJson ([Text.Encoding]::UTF8.GetBytes($text))}catch{$closed=$true};if(-not $closed){throw 'duplicate_accepted'}};if((Get-NpJsonHash @()) -cne (Get-NpHash ([Text.Encoding]::UTF8.GetBytes('[]')))){throw 'empty_inventory_not_pinned'}''')

    def test_service_facts_reject_existing_identity_running_shared_and_configuration_drift(self):
        self.ps(f"""
$service=[pscustomobject]@{{Name='{SERVICE}';State='Stopped';ProcessId=0;StartName='NT SERVICE\\{SERVICE}';PathName='"C:\\Private\\new\\host.exe" --config private';StartMode='Manual';ServiceType='Own Process';DesktopInteract=$false}}
$config=[ordered]@{{name=$service.Name;start_name=$service.StartName;path_name=$service.PathName;start_mode=$service.StartMode;service_type=$service.ServiceType;desktop_interact=[bool]$service.DesktopInteract}}
$spec=[pscustomobject]@{{service_name='{SERVICE}';service_sid='{SERVICE_SID}';service_config_sha256=(Get-NpJsonHash $config)}}
$null=Assert-NpServiceFacts $service $spec 'C:\\Private' '{SERVICE_SID}' 1
foreach($case in @(@('State','Running'),@('ProcessId',9),@('StartName','LocalSystem'),@('ServiceType','Share Process'),@('DesktopInteract',$true),@('PathName','"C:\\Existing\\host.exe"'))){{$old=$service.($case[0]);$service.($case[0])=$case[1];$closed=$false;try{{$null=Assert-NpServiceFacts $service $spec 'C:\\Private' '{SERVICE_SID}' 1}}catch{{$closed=$true}};$service.($case[0])=$old;if(-not $closed){{throw 'existing_or_active_service_accepted'}}}}
foreach($sidType in @(0,3)){{$closed=$false;try{{$null=Assert-NpServiceFacts $service $spec 'C:\\Private' '{SERVICE_SID}' $sidType}}catch{{$closed=$true}};if(-not $closed){{throw 'sid_type_accepted'}}}}
""")

    def transaction(self, scenario):
        original = base64.b64encode(descriptor()).decode()
        return self.ps(f"""
$original=[Convert]::FromBase64String('{original}');$target=[HaNamespacePolicyCoreV1]::Build($original,'{SERVICE_SID}','protect-and-materialize-v1',$true,$true,[string[]]@());$script:current=$original;$script:parent=[byte[]]@(7,8);$script:sets=0;$script:readbackBad=$false;$script:parentBad=$false
$root=Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString());$null=[IO.Directory]::CreateDirectory($root)
function Get-NpNamespace($Namespace){{if($Namespace -ceq 'root/virtualization'){{return ,$script:parent}};return ,$script:current}}
function Set-NpNamespace([byte[]]$Bytes){{$script:sets++;if(-not $script:readbackBad){{$script:current=$Bytes}};if($script:parentBad){{$script:parent=[byte[]]@(9)}}}}
function Get-NpChildren{{return ,@()}}
function Open-NpInput($Path,$Hash,$Limit=65536){{$b=[IO.File]::ReadAllBytes($Path);if((Get-NpHash $b) -cne $Hash){{throw 'mock_input_hash_changed'}};return ,$b}}
$plan=[ordered]@{{source_commit=('a'*40);script_sha256=('b'*64);installation_id='{INSTALLATION}';original_sd_sha256=(Get-NpHash $original);target_sd_sha256=(Get-NpHash $target);parent_sd_sha256=(Get-NpHash $script:parent);children_sha256=(Get-NpJsonHash @());inheritance_transition='protect-and-materialize-v1'}}
$candidate=[pscustomobject]@{{plan=$plan;hash=('c'*64);target=$target}};$PlanSha256=$candidate.hash;$Go=$true
try{{
{scenario}
}}finally{{[IO.Directory]::Delete($root,$true)}}
""")

    def test_apply_writeahead_readback_and_exact_original_rollback_without_replay(self):
        self.transaction(r"""
$null=Invoke-NpTransaction $candidate $original $root $false
if($script:sets -ne 1 -or (Get-NpHash $script:current) -cne $plan.target_sd_sha256){throw 'apply_failed'}
$apply=Join-Path $root 'namespace-policy-apply-receipt.json';$ApplyReceiptSha256=Get-NpHash ([IO.File]::ReadAllBytes($apply));$journalHash=Get-NpHash ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-journal.jsonl')))
$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 1){throw 'apply_replayed'}
$null=Invoke-NpTransaction $candidate $original $root $true
if($script:sets -ne 2 -or (Get-NpHash $script:current) -cne (Get-NpHash $original) -or (Get-NpHash ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-journal.jsonl')))) -cne $journalHash){throw 'rollback_not_exact_or_original_journal_changed'}
$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $true}catch{$closed=$true};if(-not $closed -or $script:sets -ne 2){throw 'rollback_replayed'}
""")

    def test_no_go_plan_or_current_drift_never_creates_intent(self):
        self.transaction(r"""
$Go=$false;$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 0 -or @(Get-ChildItem $root).Count -ne 0){throw 'no_go_mutated'}
$Go=$true;$PlanSha256='d'*64;$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 0){throw 'plan_not_pinned'}
$PlanSha256=$candidate.hash;$script:current=[byte[]]@(1);$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 0 -or @(Get-ChildItem $root).Count -ne 0){throw 'drift_mutated'}
""")

    def test_failed_readback_preserves_intent_and_never_automatically_rolls_back_or_retries(self):
        self.transaction(r"""
$script:readbackBad=$true;$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 1 -or -not (Test-Path (Join-Path $root 'namespace-policy-apply-journal.jsonl')) -or (Test-Path (Join-Path $root 'namespace-policy-apply-receipt.json'))){throw 'uncertain_write_not_preserved'}
$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 1){throw 'partial_replayed'}
""")

    def test_parent_drift_after_set_blocks_success_receipt(self):
        self.transaction(r"""
$script:parentBad=$true;$closed=$false;try{$null=Invoke-NpTransaction $candidate $original $root $false}catch{$closed=$true};if(-not $closed -or $script:sets -ne 1 -or (Test-Path (Join-Path $root 'namespace-policy-apply-receipt.json'))){throw 'hierarchy_change_accepted'}
""")

    def test_parser_and_native_helpers_compile_without_native_execution(self):
        self.ps("$tokens=$null;$errors=$null;$null=[Management.Automation.Language.Parser]::ParseFile('" + str(SOURCE) + "',[ref]$tokens,[ref]$errors);if($errors.Count -ne 0){throw 'parse_failed'};Initialize-NpNative;if(-not ('HaNamespacePrivilegeV1' -as [type])){throw 'native_helper_compile_failed'}")

    def test_actual_parameter_binding_routes_mixed_case_inspect_and_plan_readonly(self):
        # Use the actual product ParamBlock, normalization statement and complete
        # dispatch tail. Native acquisition alone is mocked; no copied branch
        # conditions or invented dispatcher replace the source under test.
        with tempfile.TemporaryDirectory(prefix='ha-namespace-mode-') as directory:
            fixture = Path(directory) / 'bound-dispatch.ps1'
            marker = Path(directory) / 'transaction-called.json'
            encoded = base64.b64encode(descriptor()).decode()
            self.ps(r'''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
$invoke=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Invoke-NamespacePolicy'},$true)
$normalize=$invoke.Body.EndBlock.Statements[0].Extent.Text
if($normalize -cne '$Mode=ConvertTo-NpMode $Mode'){throw 'entry_normalization_missing'}
$selected=$null
foreach($try in $invoke.FindAll({param($n)$n -is [Management.Automation.Language.TryStatementAst]},$true)){
 if($try.Body.Extent.Text.Contains("if(`$Mode -ceq 'inspect')")){$selected=$try;break}
}
if($null -eq $selected){throw 'real_dispatch_missing'}
$statements=@($selected.Body.Statements);$index=-1
for($i=0;$i -lt $statements.Count;$i++){if($statements[$i].Extent.Text.StartsWith("if(`$Mode -ceq 'inspect')")){$index=$i;break}}
if($index -lt 0){throw 'real_inspect_branch_missing'}
$tail=($statements[$index..($statements.Count-1)]|ForEach-Object{$_.Extent.Text}) -join "`n"
$setup=@'
$ErrorActionPreference='Stop'
. '__SOURCE__' -Mode $Mode -InstallationId $InstallationId -SourceCommit $SourceCommit -ManifestPath $ManifestPath -Go:$Go
Initialize-NpCore
$fixtureSpec=[pscustomobject]@{source_commit=$SourceCommit;installation_id=$InstallationId;original_sd_sha256=('1'*64)}
function Read-NpJson($Bytes){return $script:fixtureSpec}
function Open-NpInput($Path,$Hash){return ,[byte[]]@(1)}
function Assert-NpManifest($Value){}
function Get-LocalGroupMember{[pscustomobject]@{SID=[pscustomobject]@{Value='S-1-5-21-1-2-3-4'}}}
function New-NpPlan{[pscustomobject]@{plan=[pscustomobject]@{original_sd_sha256=('1'*64);target_sd_sha256=('3'*64)};hash=('2'*64);current_hash=('1'*64)}}
function Get-NpService{}
function Assert-NpEvidence{}
function Get-Acl{[pscustomobject]@{Sddl='same'}}
function Invoke-NpTransaction($Candidate,$Original,$Root,$Rollback){[IO.File]::WriteAllText('__MARKER__',([ordered]@{rollback=$Rollback}|ConvertTo-Json -Compress));[ordered]@{scope='test_transaction'}|ConvertTo-Json -Compress}
function Invoke-RealDispatchTail{
$root='__ROOT__';$current=[Convert]::FromBase64String('__SD__');$parent=$current;$children=@();$rootIdentity='identity';$rootAcl='same'
$rootLease=[pscustomobject]@{};$rootLease|Add-Member ScriptMethod Identity {'identity'}
'@
$text=$ast.ParamBlock.Extent.Text+"`n"+$setup+"`n"+$normalize+"`n"+$tail+"`n}`nInvoke-RealDispatchTail`n"
[IO.File]::WriteAllText('__FIXTURE__',$text)
'''.replace('__SOURCE__', str(SOURCE)).replace('__MARKER__', str(marker)).replace('__ROOT__', directory).replace('__SD__', encoded).replace('__FIXTURE__', str(fixture)))
            for mode in ['inspect', 'Inspect', 'INSPECT', 'iNsPeCt', 'plan', 'Plan', 'PLAN', 'pLaN']:
                result = subprocess.run([self.pwsh, '-NoLogo', '-NoProfile', '-File', str(fixture), '-Mode', mode, '-InstallationId', INSTALLATION, '-SourceCommit', 'a' * 40, '-ManifestPath', str(Path(directory) / 'namespace-policy-request.json'), '-Go'], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[-1200:])
                self.assertEqual(json.loads(result.stdout)['scope'], 'namespace_policy_readonly_' + mode.lower())
                self.assertFalse(marker.exists(), mode + ' reached transaction')
            # Control: identical real tail reaches the transaction only for apply;
            # its effect is the harmless test-owned marker, not a namespace API.
            result = subprocess.run([self.pwsh, '-NoLogo', '-NoProfile', '-File', str(fixture), '-Mode', 'ApPlY', '-InstallationId', INSTALLATION, '-SourceCommit', 'a' * 40, '-ManifestPath', str(Path(directory) / 'namespace-policy-request.json'), '-Go'], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertEqual(json.loads(marker.read_text()), {'rollback': False})
            marker.unlink()
            result = subprocess.run([self.pwsh, '-NoLogo', '-NoProfile', '-File', str(fixture), '-Mode', 'unknown'], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
        self.ps("foreach($value in @('', 'unknown', ' inspect', 'plan ')){$closed=$false;try{$null=ConvertTo-NpMode $value}catch{$closed=$true};if(-not $closed){throw 'unknown_mode_accepted'}}")

    def test_source_acl_bit_expressions_use_byte_enum_values_without_policy_change(self):
        # Evaluate the actual source AST expressions, using the real byte-based
        # AceFlags enum. PS7 accepts the legacy enum operator; Windows PS5 throws
        # for it, as also observed by the separate native compatibility probe.
        body = r'''
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw 'source_parse_failed'}
$paths=@('__SOURCE__','__ENFORCED__','__RUNTIME__','__PREFLIGHT__');$counts=@(2,2,1,1);$all=[Collections.Generic.List[object]]::new()
for($fileIndex=0;$fileIndex -lt $paths.Count;$fileIndex++){
 $tokens=$null;$errors=$null;$fileAst=[Management.Automation.Language.Parser]::ParseFile($paths[$fileIndex],[ref]$tokens,[ref]$errors);if($errors.Count){throw 'owner_source_parse_failed'}
 $fileBits=@($fileAst.FindAll({param($n)$n -is [Management.Automation.Language.BinaryExpressionAst] -and $n.Operator -eq [Management.Automation.Language.TokenKind]::Band -and $n.Left.Extent.Text.Contains('$ace.AceFlags')},$true))
if($fileBits.Count -ne $counts[$fileIndex]){throw 'owner_source_bit_expression_count_changed'};foreach($bit in $fileBits){$all.Add($bit)}
}
$bits=$all.ToArray()
if($bits.Count -ne 6 -or [Enum]::GetUnderlyingType([Security.AccessControl.AceFlags]) -ne [byte]){throw 'actual_byte_enum_bit_expressions_missing'}
$io=@($bits|Where-Object{$_.Right.Extent.Text.Contains('InheritOnly')})
$both=@($bits|Where-Object{-not $_.Right.Extent.Text.Contains('InheritOnly')})
if($io.Count -ne 5 -or $both.Count -ne 1){throw 'source_flags_predicates_changed'}
for($value=0;$value -le 31;$value++){
 $ace=[pscustomobject]@{AceFlags=[Security.AccessControl.AceFlags][byte]$value}
 foreach($expression in $io){$valueIo=Invoke-Expression $expression.Extent.Text;if($valueIo -isnot [int] -or $valueIo -ne ($value -band 8)){throw 'owner_source_io_policy_changed'}}
 $actualIo=Invoke-Expression $io[0].Extent.Text;$actualBoth=Invoke-Expression $both[0].Extent.Text
 if($actualIo -isnot [int] -or $actualBoth -isnot [int] -or $actualIo -ne ($value -band 8) -or $actualBoth -ne ($value -band 3)){throw 'source_flags_value_or_integer_type_changed'}
 $accepted=($actualIo -eq 0 -and $actualBoth -eq 3)
 $intended=($value -in @(3,7,19,23))
 if($accepted -ne $intended){throw 'existing_flags_policy_changed'}
}
foreach($value in @(3,11)){
 $ace=[pscustomobject]@{AceFlags=[Security.AccessControl.AceFlags][byte]$value}
 $failed=$false;$legacy=$null
 try{$legacy=$ace.AceFlags -band [Security.AccessControl.AceFlags]::InheritOnly}catch{
  $failed=$true;$base=$_.Exception.GetBaseException()
  if($base -isnot [InvalidCastException] -or $base.HResult -ne -2147467262){throw 'unexpected_legacy_failure'}
 }
 if($PSVersionTable.PSEdition -ceq 'Desktop' -and $PSVersionTable.PSVersion.Major -eq 5){if(-not $failed){throw 'winps5_legacy_reproduction_missing'}}
 elseif(-not $failed -and [int]$legacy -ne ($value -band 8)){throw 'legacy_success_semantics_changed'}
}
'''.replace('__SOURCE__', str(SOURCE)).replace('__ENFORCED__', str(SOURCE.parent / 'windows-enforced-vm.ps1')).replace('__RUNTIME__', str(SOURCE.parent / 'windows-runtime-stage.ps1')).replace('__PREFLIGHT__', str(SOURCE.parent / 'windows-service-preflight.ps1'))
        with tempfile.TemporaryDirectory(prefix='ha-namespace-byte-enum-') as directory:
            script = Path(directory) / 'source-bit-expressions.ps1'
            script.write_text(body)
            result = subprocess.run([self.pwsh, '-NoLogo', '-NoProfile', '-File', str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:] + result.stdout[-500:])

    def test_only_one_namespace_set_primitive_and_no_scm_group_or_vm_mutator(self):
        text = SOURCE.read_text()
        self.assertEqual(text.count("InvokeMethod('SetSD'"), 1)
        for forbidden in ['New-Service', 'Set-Service', 'Start-Service', 'Stop-Service', 'Add-LocalGroupMember', 'Remove-LocalGroupMember', 'Start-VM', 'Stop-VM', 'New-VM', 'Set-VM', 'CreateInstance', 'DeleteInstance']:
            self.assertNotIn(forbidden, text)
        self.assertIn("Write-NpRecord $journal", text)
        self.assertLess(text.index("Write-NpRecord $journal ([ordered]@{sequence=1"), text.index(' Set-NpNamespace $bytes'))
        self.assertIn('provider_acceptance_proven_by_code=$false', text)
        self.assertIn('existing_service_health_proven_by_code=$false', text)


if __name__ == '__main__':
    unittest.main()
