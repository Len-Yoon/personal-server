"""Portable observed-raw trust boundary tests; never execute native Windows APIs."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
import unittest

from tests import test_ha_namespace_policy as N
from tests import test_ha_enforced_windows as W
from tests import test_ha_enforced_provision as P

sys.path.insert(0, str(N.ROOT / 'infra/k8s'))
from ha_control import namespace_contract as C
from ha_control import enforced_windows as E


class NamespaceObservedTests(unittest.TestCase):
    setUpClass = classmethod(N.NamespacePolicyTests.setUpClass.__func__)
    ps = N.NamespacePolicyTests.ps
    manifest = N.NamespacePolicyTests.manifest
    closure_transaction = N.NamespacePolicyTests.closure_transaction

    def observed_transaction(self, scenario):
        return self.closure_transaction(r'''
$spec|Add-Member NoteProperty descriptor_profile 'strict-canonical-observed-v1'
$spec.schema_version=3;$spec|Add-Member NoteProperty comparator_sha256 '__COMPARATOR__'
Initialize-NpCanonical $spec
$spec|Add-Member NoteProperty closure_original_canonical_sha256 @((Get-NpCanonicalHash $original),(Get-NpCanonicalHash $original),(Get-NpCanonicalHash $original))
$spec|Add-Member NoteProperty closure_target_canonical_sha256 @((Get-NpCanonicalHash $target),(Get-NpCanonicalHash $target),(Get-NpCanonicalHash $target))
for($i=0;$i -lt 3;$i++){
 $path=Join-Path $root ('namespace-closure-'+$i+'-evidence.json');$e=[IO.File]::ReadAllText($path)|ConvertFrom-Json
 $e.schema_version=2;$e.format='ha-namespace-closure-evidence-canonical-v1';$e.provider_roundtrip_exact=$false;$e.original_restore_exact=$false
 $e|Add-Member NoteProperty descriptor_profile $spec.descriptor_profile;$e|Add-Member NoteProperty comparator_sha256 $spec.comparator_sha256
 $e|Add-Member NoteProperty provider_roundtrip_canonical $true;$e|Add-Member NoteProperty original_restore_canonical $true
 $b=[Text.Encoding]::UTF8.GetBytes(($e|ConvertTo-Json -Compress));[IO.File]::WriteAllBytes($path,$b);$spec.closure_classification_sha256[$i]=Get-NpHash $b
}
$candidate=New-NpPlan $spec $original $original $parent $children @('S-1-5-21-1-2-3-4') ('b'*64);$PlanSha256=$candidate.hash
if($candidate.plan.Contains('runtime_namespace_closure')){throw 'preapply_runtime_pin_created'}
$script:badObserved=$false;$script:failReturn=$false;$script:nonzeroReturn=$false;$script:driftAfter=-1
function Set-NpClosureNamespace($Namespace,[byte[]]$Bytes,$Spec){
 $index=[array]::IndexOf($names,$Namespace);$suffix=if($script:trace.Count -lt 3){'apply'}else{'rollback'}
 $events=@([IO.File]::ReadAllLines((Join-Path $root ('namespace-policy-'+$suffix+'-journal.jsonl')))|ForEach-Object{$_|ConvertFrom-Json})
 if($events[-1].event -cne 'namespace_closure_set_intent' -or $events[-1].index -ne $index){throw 'missing_durable_intent'}
 $path=Join-Path $root ('namespace-'+$suffix+'-'+$index+'-requested.bin')
 if((Get-NpHash ([IO.File]::ReadAllBytes($path))) -cne (Get-NpHash $Bytes)){throw 'missing_durable_request'}
 $script:trace.Add($index)
 # Simulate provider layout normalization with trailing zero allocation. This
 # changes raw hashes without changing ANY SD header or ordered ACE bytes.
 $packed=[StrictCanonicalSd]::Canonicalize($Bytes);$script:live[$Namespace]=[byte[]]($packed+@(0,0,0,0))
 if($script:badObserved){$script:live[$Namespace][1]=1}
 if($script:driftAfter -ge 0){$script:live[$names[$script:driftAfter]][1]=2}
 if($script:failReturn){throw 'lost_response'}
 if($script:nonzeroReturn){return [uint32]5}
 return [uint32]0
}
'''.replace('__COMPARATOR__', C.COMPARATOR_SHA256) + scenario)

    def test_canonical_parser_cross_language_pins_and_negative_layouts(self):
        raw = N.descriptor()
        # Append unused zero allocation, change component physical order, and
        # compact ACL slack independently while preserving all meaningful bytes.
        parts = N.components(raw)
        padded = bytearray(parts[3] + b'\0' * 12)
        struct.pack_into('<H', padded, 2, len(padded))
        parts[3] = bytes(padded)
        reordered = bytearray(raw[:20]); offsets = [0] * 4
        for k in (3, 2, 1, 0):
            offsets[k] = len(reordered); reordered += parts[k]
        struct.pack_into('<4I', reordered, 4, *offsets)
        reordered += b'\0' * 8
        self.assertEqual(C.canonical(raw), C.canonical(bytes(reordered)))
        bad = []
        for offset in (0, 1, 2, offsets[3] + 1, offsets[3] + 8 + 1, offsets[3] + 8 + 4):
            value = bytearray(reordered); value[offset] ^= 1; bad.append(bytes(value))
        nonzero = bytearray(reordered); nonzero[-1] = 1; bad.append(bytes(nonzero))
        overlap = bytearray(reordered); struct.pack_into('<I', overlap, 8, offsets[0]); bad.append(bytes(overlap))
        # Unknown SACL ACE must be rejected; null vs empty SACL differs.
        unknown = bytearray(reordered); unknown[offsets[2] + 8] = 3; bad.append(bytes(unknown))
        # Empty and null SACL are distinct representations; ALL ACL headers,
        # revision/reserved/control bits and ACE order stay semantically pinned.
        null_sacl=bytearray(raw[:20]); struct.pack_into('<I',null_sacl,12,0)
        null_parts=N.components(raw)
        for k in (0,1,3):
            struct.pack_into('<I',null_sacl,4+4*k,len(null_sacl));null_sacl+=null_parts[k]
        empty_sacl=bytearray(raw[:20])
        for k in range(4):
            struct.pack_into('<I',empty_sacl,4+4*k,len(empty_sacl));empty_sacl+=N.acl([]) if k==2 else null_parts[k]
        self.assertNotEqual(C.canonical(bytes(null_sacl)),C.canonical(bytes(empty_sacl)))
        header=bytearray(raw);header[struct.unpack_from('<I',raw,16)[0]+1]=1
        self.assertNotEqual(C.canonical(raw),C.canonical(bytes(header)))
        values = [raw, bytes(reordered)] + bad + [bytes(null_sacl),bytes(empty_sacl),bytes(header)]
        encoded = base64.b64encode(json.dumps([base64.b64encode(v).decode() for v in values]).encode()).decode()
        output = self.ps("Add-Type -TypeDefinition ([IO.File]::ReadAllText('" + str(N.SOURCE.parent / 'strict-canonical-sd.cs') + "'));$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'))|ConvertFrom-Json;foreach($item in $v){try{Get-NpHash ([StrictCanonicalSd]::Canonicalize([Convert]::FromBase64String($item)))}catch{'rejected'}}").splitlines()
        self.assertEqual(hashlib.sha256((N.SOURCE.parent / 'strict-canonical-sd.cs').read_bytes()).hexdigest(), C.COMPARATOR_SHA256)
        for raw, actual in zip(values, output):
            try:
                expected = C.sha(C.canonical(raw))
            except (ValueError, struct.error):
                expected = 'rejected'
            self.assertEqual(actual, expected)
        self.assertEqual(output[0], output[1])
        self.assertTrue(all(value != output[0] for value in output[2:]))

    def test_apply_observed_raw_receipt_to_runtime_and_rollback_observed_raw(self):
        output = self.observed_transaction(r'''
Invoke-NpTransaction $candidate $original $root $false|Out-Null
$r=Read-NpJson ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-receipt.json')))
Assert-NpObservedReceipt $r $candidate
if(($trace -join ',') -cne '1,2,0' -or $r.observed_sd_sha256[0] -ceq $spec.closure_target_sd_sha256[0]){throw 'observed_raw_not_pinned'}
$runtime=Read-NpJson ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-runtime-observed.json')))
if($runtime.sd_sha256[0] -cne $r.observed_sd_sha256[0] -or $runtime.version -cne 'direct-children-observed-v1'){throw 'requested_runtime_pin'}
$ApplyReceiptSha256=Get-NpHash ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-receipt.json')))
[ordered]@{receipt=[Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-receipt.json')));journal=[Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-apply-journal.jsonl')));plan=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($candidate.plan|ConvertTo-Json -Depth 12 -Compress)));runtime=$runtime}|ConvertTo-Json -Depth 6 -Compress
Invoke-NpTransaction $candidate $original $root $true|Out-Null
if(($trace -join ',') -cne '1,2,0,0,2,1'){throw 'rollback_order'}
$r=Read-NpJson ([IO.File]::ReadAllBytes((Join-Path $root 'namespace-policy-rollback-receipt.json')))
if($r.observed_canonical_sha256[0] -cne $spec.closure_original_canonical_sha256[0] -or $r.observed_sd_sha256[0] -ceq $spec.closure_original_sd_sha256[0]){throw 'restore_observed_raw_missing'}
''')
        package = json.loads(output)
        closure = package['runtime']
        raw = [base64.b64decode(package[k]) for k in ('receipt', 'journal', 'plan')]
        receipt = json.loads(raw[0])
        kwargs = dict(commit=receipt['source_commit'], installation=receipt['installation_id'], service_sid=N.SERVICE_SID, driver_policy_sha256=receipt['script_sha256'])
        self.assertEqual(E.namespace_closure(closure), closure)
        C.verify_apply_receipt(*raw, closure, **kwargs)
        for field, value in [('complete', False),('schema_version', True),('observed_sd_sha256',receipt['requested_sd_sha256']),('source_commit','e'*40),('service_identity_sha256','a'*64)]:
            bad=deepcopy(receipt);bad[field]=value
            mutated=json.dumps(bad).encode();rebound=deepcopy(closure);rebound['apply_receipt_sha256']=C.sha(mutated)
            with self.assertRaises(ValueError): C.verify_apply_receipt(mutated, raw[1], raw[2], rebound, **kwargs)
        with self.assertRaises(ValueError): C.verify_apply_receipt(raw[0], raw[1]+b'\n', raw[2], closure, **kwargs)
        # Rehash all transport pins: boolean numeric fields and incomplete
        # return/readback journals still cannot be promoted to a final receipt.
        events=[json.loads(line) for line in raw[1].splitlines()]
        events[0]['sequence']=True
        altered_journal=b'\n'.join(json.dumps(e).encode() for e in events)+b'\n'
        altered_receipt=deepcopy(receipt);altered_receipt['journal_sha256']=C.sha(altered_journal)
        altered_raw=json.dumps(altered_receipt).encode();altered_closure=deepcopy(closure);altered_closure['apply_receipt_sha256']=C.sha(altered_raw)
        with self.assertRaises(ValueError): C.verify_apply_receipt(altered_raw,altered_journal,raw[2],altered_closure,**kwargs)

        # Real provisioning API requires receipt material and validates exact
        # source, service, final plan/policy bindings before returning evidence.
        fixture=P.EnforcedProvisionTests()
        _, finalplan, policy, trust, bind=fixture.managed_inputs()
        finalplan['source_commit']=kwargs['commit'];finalplan['installation_id']=kwargs['installation']
        finalplan['windows_enforced'].update(service_sid=N.SERVICE_SID,namespace_closure=closure)
        trust['service_args']['service_sid']=N.SERVICE_SID
        policy.update(service_sid=N.SERVICE_SID,namespace_closure=deepcopy(closure))
        binding=bind();binding['namespace_policy_sha256']=kwargs['driver_policy_sha256']
        for label, data in zip(('namespace_apply_receipt_bytes_base64','namespace_apply_journal_bytes_base64','namespace_plan_bytes_base64'),raw): trust[label]=base64.b64encode(data).decode()
        P.P.managed_profile_binding(trust,binding,kwargs['commit'])
        trust.pop('namespace_apply_receipt_bytes_base64')
        with self.assertRaises(P.P.ProvisionError): P.P.managed_profile_binding(trust,binding,kwargs['commit'])

    def test_partial_lost_return_and_semantic_change_preserve_journal_without_runtime(self):
        for flag in ('$script:failReturn=$true', '$script:badObserved=$true', '$script:nonzeroReturn=$true'):
            self.observed_transaction(flag+r'''
$closed=$false;try{Invoke-NpTransaction $candidate $original $root $false|Out-Null}catch{$closed=$true}
if(-not $closed -or $trace.Count -ne 1 -or -not (Test-Path (Join-Path $root 'namespace-policy-apply-journal.jsonl')) -or (Test-Path (Join-Path $root 'namespace-policy-apply-receipt.json')) -or (Test-Path (Join-Path $root 'namespace-runtime-observed.json'))){throw 'partial_accepted'}
$closed=$false;try{Invoke-NpTransaction $candidate $original $root $false|Out-Null}catch{$closed=$true};if(-not $closed -or $trace.Count -ne 1){throw 'partial_retried'}
if($script:nonzeroReturn){$events=@([IO.File]::ReadAllLines((Join-Path $root 'namespace-policy-apply-journal.jsonl'))|ForEach-Object{$_|ConvertFrom-Json});if($events[-1].event -cne 'namespace_closure_set_return' -or $events[-1].return_value -ne 5){throw 'nonzero_return_lost'}}
''')

    def test_applied_member_raw_drift_blocks_next_step_even_when_canonical_would_match(self):
        self.observed_transaction(r'''
$script:driftAfter=0
$closed=$false;try{Invoke-NpTransaction $candidate $original $root $false|Out-Null}catch{$closed=$true}
if(-not $closed -or $trace.Count -ne 1 -or (Test-Path (Join-Path $root 'namespace-policy-apply-receipt.json'))){throw 'other_member_drift_accepted'}
''')

    def test_observed_runtime_hash_cross_language_and_no_canonical_runtime_fallback(self):
        fixture = W.EnforcedWindowsTests()
        value=fixture.closure();value.update(version=C.OBSERVED_VERSION,descriptor_profile=C.PROFILE,comparator_sha256=C.COMPARATOR_SHA256,apply_receipt_sha256='1'*64,plan_sha256='2'*64,service_identity_sha256='3'*64)
        W.EnforcedWindowsTests.setUpClass()
        encoded=base64.b64encode(json.dumps(value).encode()).decode()
        actual=fixture.ps(f"$v=Convert-EnforcedJson ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')));Get-EnforcedNamespaceClosureHash $v").strip()
        self.assertEqual(actual,E.namespace_closure_digest(value))
        text=W.DRIVER.read_text()
        self.assertIn('(Get-EnforcedHash $bytes) -cne $closure.sd_sha256[$i]',text)
        self.assertNotIn('Canonicalize',text)
        for field in C.OBSERVED_FIELDS:
            bad=deepcopy(value);del bad[field]
            with self.assertRaises(E.TransitionBlocked): E.namespace_closure(bad)

    def test_explicit_root_read_traverse_exception_is_installation_only_and_closed(self):
        self.ps(r'''
$installation='__INSTALLATION__';$sid=[HaNamespacePolicyCoreV1]::ServiceSid([HaNamespacePolicyCoreV1]::ServiceName($installation))
$aces=@([pscustomobject]@{sid='S-1-5-18';mask=2032127;flags=3;simple_allow=$true},[pscustomobject]@{sid='S-1-5-32-544';mask=2032127;flags=3;simple_allow=$true},[pscustomobject]@{sid=$sid;mask=131232;flags=0;simple_allow=$true})
Assert-NpObservedRootFacts 'S-1-5-18' $true $aces $installation
foreach($key in @('sid','mask','flags','simple_allow')){
 $saved=$aces[2].$key;$aces[2].$key=switch($key){'sid'{'S-1-5-80-1-2-3-4-5'}'mask'{2032127}'flags'{3}'simple_allow'{$false}}
 $closed=$false;try{Assert-NpObservedRootFacts 'S-1-5-18' $true $aces $installation}catch{$closed=$true};if(-not $closed){throw 'root_service_acl_widened'};$aces[2].$key=$saved
}
foreach($mask in @(131104,131233,131234)){
 $aces[2].mask=$mask;$closed=$false;try{Assert-NpObservedRootFacts 'S-1-5-18' $true $aces $installation}catch{$closed=$true};if(-not $closed){throw 'root_rights_boundary_bypass'}
};$aces[2].mask=131232
foreach($variant in @('owner','unprotected','duplicate','additional','wrong_installation','sy_flags')){
 $owner='S-1-5-18';$protected=$true;$selected=$aces;$id=$installation
 switch($variant){'owner'{$owner='S-1-5-21-1-2-3-4'}'unprotected'{$protected=$false}'duplicate'{$selected=@($aces[0],$aces[1],$aces[1])}'additional'{$selected=@($aces)+@($aces[2])}'wrong_installation'{$id='5'*64}'sy_flags'{$aces[0].flags=19}}
 $closed=$false;try{Assert-NpObservedRootFacts $owner $protected $selected $id}catch{$closed=$true};if(-not $closed){throw 'root_boundary_bypass'};$aces[0].flags=3
}
$raw=[pscustomobject]@{};$observed=[pscustomobject]@{descriptor_profile='strict-canonical-observed-v1'}
Assert-NpSelectedProfile $raw 'raw-exact-v1';Assert-NpSelectedProfile $observed 'strict-canonical-observed-v1'
foreach($binding in @(@($raw,'strict-canonical-observed-v1'),@($observed,'raw-exact-v1'))){$closed=$false;try{Assert-NpSelectedProfile $binding[0] $binding[1]}catch{$closed=$true};if(-not $closed){throw 'profile_silently_inferred'}}
'''.replace('__INSTALLATION__',N.INSTALLATION))

    def test_owned_host_image_acl_and_native_set_return_types_are_exact(self):
        self.ps(r'''
$id='__INSTALLATION__';$sid=[HaNamespacePolicyCoreV1]::ServiceSid([HaNamespacePolicyCoreV1]::ServiceName($id))
$aces=@([pscustomobject]@{sid='S-1-5-18';mask=2032127;flags=0;simple_allow=$true},[pscustomobject]@{sid='S-1-5-32-544';mask=2032127;flags=0;simple_allow=$true},[pscustomobject]@{sid=$sid;mask=0x1200a9;flags=0;simple_allow=$true})
Assert-NpObservedRootFacts 'S-1-5-32-544' $true $aces $id $true
foreach($row in @(@('mask',2032127),@('mask',131232),@('flags',16),@('flags',3),@('sid','S-1-5-80-1-2-3-4-5'))){
 $old=$aces[2].($row[0]);$aces[2].($row[0])=$row[1];$closed=$false;try{Assert-NpObservedRootFacts 'S-1-5-32-544' $true $aces $id $true}catch{$closed=$true};if(-not $closed){throw 'host_exe_acl_widened'};$aces[2].($row[0])=$old
}
foreach($answer in @($null,[pscustomobject]@{ReturnValue=$null},[pscustomobject]@{ReturnValue=[int]0},[pscustomobject]@{ReturnValue='0'},[pscustomobject]@{ReturnValue=$false})){
 $closed=$false;try{$null=Get-NpSetReturnValue $answer}catch{$closed=$true};if(-not $closed){throw 'native_return_coerced_to_zero'}
}
$result=Get-NpSetReturnValue ([pscustomobject]@{ReturnValue=[uint32]0});if($result -isnot [uint32] -or $result -ne 0){throw 'native_zero_changed'}
$result=Get-NpSetReturnValue ([pscustomobject]@{ReturnValue=[uint32]5});if($result -isnot [uint32] -or $result -ne 5){throw 'native_error_changed'}
'''.replace('__INSTALLATION__',N.INSTALLATION))

    def test_rollback_rejects_rehashed_incomplete_or_semantically_changed_apply_journal(self):
        self.observed_transaction(r'''
Invoke-NpTransaction $candidate $original $root $false|Out-Null
$receiptPath=Join-Path $root 'namespace-policy-apply-receipt.json';$journalPath=Join-Path $root 'namespace-policy-apply-journal.jsonl'
$receiptBytes=[IO.File]::ReadAllBytes($receiptPath);$journalBytes=[IO.File]::ReadAllBytes($journalPath)
foreach($mutation in @('missing','order','return','sequence_type','extra')){
 $events=@([Text.Encoding]::UTF8.GetString($journalBytes).TrimEnd("`n").Split("`n")|ForEach-Object{$_|ConvertFrom-Json})
 switch($mutation){'missing'{$events=@($events|Select-Object -SkipLast 1)}'order'{$events[1].index=0}'return'{$events[2].return_value=5}'sequence_type'{$events[0].sequence=$true}'extra'{$events[3]|Add-Member NoteProperty fabricated $true}}
 $changed=[Text.Encoding]::UTF8.GetBytes((($events|ForEach-Object{$_|ConvertTo-Json -Compress}) -join "`n")+"`n")
 [IO.File]::WriteAllBytes($journalPath,$changed);$r=Read-NpJson $receiptBytes;$r.journal_sha256=Get-NpHash $changed
 [IO.File]::WriteAllText($receiptPath,($r|ConvertTo-Json -Depth 12 -Compress));$ApplyReceiptSha256=Get-NpHash ([IO.File]::ReadAllBytes($receiptPath))
 $closed=$false;try{Invoke-NpTransaction $candidate $original $root $true|Out-Null}catch{$closed=$true}
 if(-not $closed -or $trace.Count -ne 3 -or (Test-Path (Join-Path $root 'namespace-policy-rollback-journal.jsonl'))){throw 'invalid_apply_journal_accepted_for_restore'}
}
''')
