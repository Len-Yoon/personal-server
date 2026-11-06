"""Run the actual source parent assignment against raw .NET filesystem types."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infra/k8s/ha_control/windows-legacy-guard.ps1"
SCRIPT = r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:HA_PATH_HELPER_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'source_parse_failed'}
$function=@($ast.FindAll({param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -ceq 'Assert-LegacyBundle'},$true))
if($function.Count -ne 1){throw 'source_function_required'}
$assignment=@($function[0].FindAll({param($a) $a -is [Management.Automation.Language.AssignmentStatementAst] -and $a.Extent.Text -match '^\s*\$entry\s*=\s*if\s*\('},$true))
if($assignment.Count -ne 1){throw 'source_assignment_required'}
$step=[ScriptBlock]::Create($assignment[0].Extent.Text)
$file=Get-Item -LiteralPath $env:HA_PATH_TEST_FILE
$cases=@($file,[IO.FileInfo]::new($file.FullName),$file.Directory,[IO.DirectoryInfo]::new($file.Directory.FullName))
$counts=@()
foreach($initial in $cases) {
 $entry=$initial;$seen=New-Object 'Collections.Generic.HashSet[string]'
 while($null -ne $entry) {
  if(-not $seen.Add($entry.FullName) -or $seen.Count -gt 64){throw 'parent_walk_cycle'}
  . $step
 }
 $counts+=@($seen.Count)
}
@{status='PASS';counts=$counts;strict_mode=$true;actual_source_assignment=$true}|ConvertTo-Json -Compress
"""


class WindowsParentTypesTests(unittest.TestCase):
    def test_real_provider_and_dotnet_filesystem_objects_reach_root(self):
        executable = shutil.which("pwsh")
        if executable is None:
            local = Path("/private/tmp/n100-ha-powershell/runtime/pwsh")
            executable = str(local) if local.is_file() else None
        self.assertIsNotNone(executable, "PowerShell runtime required; do not skip this regression")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "nested" / "fixture.txt"
            path.parent.mkdir()
            path.write_text("synthetic fixture")
            environment = dict(os.environ, HA_PATH_HELPER_SOURCE=str(SOURCE), HA_PATH_TEST_FILE=str(path))
            completed = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                                       env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            value = json.loads(completed.stdout)
            self.assertEqual(value["status"], "PASS")
            self.assertEqual(value["counts"], [len(path.parents) + 1] * 2 + [len(path.parent.parents) + 1] * 2)
            self.assertTrue(value["strict_mode"])
            self.assertTrue(value["actual_source_assignment"])
            # The original provider-only property must fail this real-object
            # fixture; otherwise the regression would not cover the defect.
            original = SOURCE.read_text()
            broken = original.replace("$entry -is [IO.DirectoryInfo]", "$entry.PSIsContainer")
            self.assertNotEqual(broken, original)
            mutant = Path(temporary) / "old-provider-property.ps1"
            mutant.write_text(broken)
            environment["HA_PATH_HELPER_SOURCE"] = str(mutant)
            rejected = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                                      env=environment, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
