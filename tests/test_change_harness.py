import json
import subprocess
import sys
import tempfile
import unittest
import os
import uuid
from unittest.mock import patch
from pathlib import Path

from scripts.run_change_harness import REPO_ROOT, build_evidence, run_harness
from scripts.review_provenance import EvidenceError, collect_activity, git_target, review_evidence


class ChangeHarnessTests(unittest.TestCase):
    def test_documentation_check_is_recorded_without_full_maintenance(self):
        code, evidence = run_harness(["README.md"], check_results=("documentation=success",))
        self.assertEqual(code, 0)
        self.assertEqual(evidence["check_results"], [{"name": "documentation", "result": "success"}])

    def test_k8s_contracts_is_a_known_check(self):
        code, evidence = run_harness(
            ["infra/k8s/README.md"], check_results=("maintenance=success", "k8s-contracts=success")
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            evidence["check_results"],
            [
                {"name": "maintenance", "result": "success"},
                {"name": "k8s-contracts", "result": "success"},
            ],
        )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the public CLI so argument parsing stays part of the contract."""
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_change_harness.py"), *arguments],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_cli_input_error(self, completed: subprocess.CompletedProcess[str]):
        self.assertEqual(completed.returncode, 1)
        evidence = json.loads(completed.stdout)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])

    def test_successful_required_check_is_ready_for_review(self):
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("portal=success",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["work_status"], "ready_for_review")
        self.assertEqual(evidence["summary"]["required_checks"], ["portal"])

    def test_windows_bootstrap_without_maintenance_is_incomplete(self):
        code, evidence = run_harness(["scripts/windows-bootstrap.ps1"])

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "verification_incomplete")
        self.assertEqual(evidence["summary"]["missing_checks"], ["maintenance"])

    def test_windows_bootstrap_with_maintenance_is_ready_for_review(self):
        code, evidence = run_harness(
            ["scripts/windows-bootstrap.ps1"], check_results=("maintenance=success",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["work_status"], "ready_for_review")

    def test_gitops_infrastructure_with_maintenance_is_ready_for_review(self):
        code, evidence = run_harness(
            ["infra/k8s/README.md"], check_results=("maintenance=success",)
        )

        self.assertEqual(code, 0)
        self.assertEqual(evidence["work_status"], "ready_for_review")
        self.assertEqual(evidence["summary"]["required_checks"], ["maintenance"])
        self.assertEqual(evidence["summary"]["unclassified_files"], [])

    def test_host_maintenance_requires_full_verification(self):
        code, evidence = run_harness(
            ["infra/k8s/README.md", "scripts/maintenance.py"],
            check_results=("maintenance=success", "k8s-contracts=success"),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "verification_incomplete")
        self.assertEqual(evidence["summary"]["blocked_files"], [])
        self.assertEqual(evidence["summary"]["missing_checks"], [
            "portal", "system-agent", "crawler-worker", "homeops-executor",
            "youtube-memo", "book-memo", "car-care-worker",
        ])

    def test_risk_and_verification_states_follow_the_policy_priority(self):
        """Changing a state branch must not mark risky work ready for review."""
        cases = [
            (["scripts/other-tool.py"], (), "blocked", 2),
            (["unknown-area/config.toml"], (), "blocked", 2),
            (["portal-web/app/main.py"], (), "verification_incomplete", 2),
            (["portal-web/app/main.py"], ("portal=failure",), "verification_failed", 2),
        ]

        for paths, results, expected_status, expected_code in cases:
            with self.subTest(paths=paths, results=results):
                code, evidence = run_harness(paths, check_results=results)

                self.assertEqual(code, expected_code)
                self.assertEqual(evidence["work_status"], expected_status)
                self.assertTrue(evidence["human_review_required"])

    def test_blocked_status_retains_failed_check_reason(self):
        """A blocked-path branch must retain simultaneous failed validation evidence."""
        code, evidence = run_harness(
            ["scripts/other-tool.py", "portal-web/app/main.py"],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "blocked")
        self.assertEqual(
            evidence["human_review_reasons"], ["blocked_files", "failed_checks"],
        )

    def test_unknown_check_name_is_an_input_error(self):
        """Removing check-name validation must not permit arbitrary verification claims."""
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("unapproved=success",)
        )

        self.assertEqual(code, 1)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])
        self.assertTrue(evidence["human_review_required"])

    def test_unknown_check_result_is_an_input_error(self):
        """Accepting a non-terminal result would allow an unverifiable check claim."""
        code, evidence = run_harness(
            ["portal-web/app/main.py"], check_results=("portal=skipped",)
        )

        self.assertEqual(code, 1)
        self.assertEqual(evidence["work_status"], "input_error")
        self.assertIsNone(evidence["policy_evidence"])

    def test_cli_missing_input_returns_input_error_evidence(self):
        """Removing CLI parser normalization must not expose argparse usage output."""
        self.assert_cli_input_error(self.run_cli())

    def test_cli_invalid_input_format_returns_input_error_evidence(self):
        """Invalid format values must produce the harness error contract, not parser output."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "paths.txt"
            input_path.write_text("portal-web/app/main.py\n", encoding="utf-8")
            completed = self.run_cli(
                "--input", str(input_path), "--input-format", "invalid-format"
            )

        self.assert_cli_input_error(completed)

    def test_cli_unreadable_input_returns_input_error_evidence(self):
        """A missing input file must not bypass the JSON input-error contract."""
        with tempfile.TemporaryDirectory() as directory:
            completed = self.run_cli("--input", str(Path(directory) / "missing.txt"))

        self.assert_cli_input_error(completed)

    def test_cli_agent_context_summarizes_incomplete_portal_verification(self):
        """Removing the prompt-safe summary must not expose raw policy evidence."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "paths.txt"
            input_path.write_text("portal-web/app/main.py\n", encoding="utf-8")
            completed = self.run_cli("--input", str(input_path), "--agent-context")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("verification_incomplete", completed.stdout)
        self.assertIn("portal", completed.stdout)
        self.assertIn("Next action:", completed.stdout)
        self.assertNotIn("policy_evidence", completed.stdout)

    def test_cli_preserves_rename_space_and_korean_paths_from_nul_input(self):
        """A NUL Git diff must retain both rename paths without path splitting."""
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "changed-files.nul"
            input_path.write_bytes(
                "R100\0docs/old name.md\0docs/한글 경로.md\0"
                "M\0portal-web/templates/with space.html\0".encode("utf-8")
            )
            completed = self.run_cli(
                "--input",
                str(input_path),
                "--input-format",
                "git-name-status-z",
                "--check-result",
                "portal=success",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = json.loads(completed.stdout)
        self.assertEqual(evidence["work_status"], "ready_for_review")
        self.assertEqual(
            evidence["policy_evidence"]["changed_files"],
            [
                "docs/old name.md",
                "docs/한글 경로.md",
                "portal-web/templates/with space.html",
            ],
        )

    def test_incomplete_status_retains_missing_and_failed_reasons(self):
        """Dropping either simultaneous reason would hide required follow-up work."""
        code, evidence = run_harness(
            ["portal-web/app/main.py", "system-agent/app/main.py"],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "verification_incomplete")
        self.assertEqual(
            evidence["human_review_reasons"], ["missing_checks", "failed_checks"],
        )

    def test_blocked_status_retains_missing_and_failed_reasons(self):
        """A blocked branch must retain every simultaneous verification reason."""
        code, evidence = run_harness(
            [
                "scripts/other-tool.py",
                "portal-web/app/main.py",
                "system-agent/app/main.py",
            ],
            check_results=("portal=failure",),
        )

        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "blocked")
        self.assertEqual(
            evidence["human_review_reasons"],
            ["blocked_files", "missing_checks", "failed_checks"],
        )


class IndependentReviewEvidenceTests(unittest.TestCase):
    """Real temporary Git repos and runtime records, with no production access."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("before\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "README.md").write_text("after\n")
        self.commit("implementation")
        self.author, self.reviewer = str(uuid.uuid4()), str(uuid.uuid4())
        self.author_log = self.session("author", self.author)
        self.reviewer_log = self.session("reviewer", self.reviewer)
        self.write_event(self.author_log, "README.md")
        self.sessions = [self.author_log, self.reviewer_log]
        self.receipt = self.directory / "receipt.json"

    def git(self, *args):
        p = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True)
        return p.stdout

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def session(self, name, principal):
        path = self.directory / (name + ".jsonl")
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": principal, "agent_path": name}}) + "\n")
        return path

    def append(self, path, item):
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "item_completed", "item": item}}) + "\n")

    def write_event(self, log, path, **kwargs):
        self.append(log, {"type": "FileChange", "id": str(uuid.uuid4()), "status": "completed",
                          "changes": {str(self.repo / path): {"type": "update", "unified_diff": "fixture", **kwargs}}})

    def command_event(self, log, command):
        self.append(log, {"type": "CommandExecution", "id": str(uuid.uuid4()),
                          "command": ["/bin/bash", "-lc", command]})

    def target(self):
        target, history = git_target(self.repo, self.base, "HEAD")
        activity = collect_activity(self.repo, self.sessions, history)
        target["activity_sha256"] = activity["activity_sha256"]
        return target

    def approve(self, reviewer=None, **kwargs):
        data = {"schema_version": 1, "target": self.target(),
                "reviewer_session_id": reviewer or self.reviewer, "decision": "pass", **kwargs}
        self.receipt.write_text(json.dumps(data))

    def review(self, paths=None):
        return review_evidence(self.repo, ["README.md"] if paths is None else paths,
                               self.base, "HEAD", self.sessions, self.receipt)

    def test_distinct_runtime_principal_and_exact_target_pass(self):
        self.approve()
        result = self.review()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["activity"]["contributors"], [self.author])

    def test_self_review_is_rejected(self):
        self.approve(self.author)
        self.assertIn("reviewer_contributed_to_change", self.review()["reasons"])

    def test_sed_additional_script_cannot_claim_read_only(self):
        self.command_event(self.reviewer_log, "sed -n '1p' -e 'w README.md' README.md")
        self.approve()
        self.assertIn("reviewer_contributed_to_change", self.review()["reasons"])

    def test_unfinished_execution_cannot_claim_read_only(self):
        with self.reviewer_log.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "pending",
                "input": "opaque"}}) + "\n")
        self.approve()
        self.assertIn("reviewer_contributed_to_change", self.review()["reasons"])

    def test_malformed_event_and_empty_completed_event_are_rejected(self):
        for item in ({}, {"id": [], "type": "CommandExecution", "command": []}):
            with self.subTest(item=item):
                log = self.session("malformed", str(uuid.uuid4()))
                self.append(log, item)
                with self.assertRaises(EvidenceError):
                    collect_activity(self.repo, [log], {"README.md"})

    def test_reused_event_id_with_different_payload_is_rejected(self):
        for command in ("python3 first.py", "python3 second.py"):
            self.append(self.reviewer_log, {"type": "CommandExecution", "id": "same-id",
                                           "command": ["/bin/sh", "-c", command]})
        with self.assertRaisesRegex(EvidenceError, "review_event_id_reused"):
            self.approve()

    def test_same_principal_under_an_alias_cannot_pass(self):
        alias = self.session("another-name", self.author)
        self.sessions.append(alias)
        with self.assertRaises(EvidenceError):
            self.approve()

    def test_unregistered_reviewer_cannot_pass(self):
        self.approve(str(uuid.uuid4()))
        self.assertIn("reviewer_session_unknown", self.review()["reasons"])

    def test_reviewer_editing_test_is_an_implementer_too(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_case.py").write_text("pass\n")
        self.commit("tests")
        self.write_event(self.reviewer_log, "tests/test_case.py")
        self.approve()
        self.assertIn("reviewer_contributed_to_change", self.review(["README.md", "tests/test_case.py"])["reasons"])

    def test_reverted_contribution_is_not_forgotten(self):
        self.git("revert", "--no-edit", "HEAD")
        self.write_event(self.reviewer_log, "README.md")
        self.approve()
        self.assertEqual(self.target()["scope"], [])
        self.assertIn("reviewer_contributed_to_change", self.review([])["reasons"])

    def test_rename_includes_both_paths_and_preserves_contributor(self):
        self.git("mv", "README.md", "한글 이름.md")
        self.commit("rename")
        self.write_event(self.reviewer_log, "README.md", move_path=str(self.repo / "한글 이름.md"))
        self.approve()
        self.assertIn("reviewer_contributed_to_change", self.review(["README.md", "한글 이름.md"])["reasons"])

    def test_unattributed_intermediate_path_blocks_completion(self):
        (self.repo / "temporary.py").write_text("hidden\n")
        self.commit("intermediate")
        (self.repo / "temporary.py").unlink()
        self.commit("revert intermediate")
        self.approve()
        self.assertIn("review_unattributed_changes", self.review()["reasons"])

    def test_omitted_actual_changed_path_is_rejected(self):
        self.approve()
        self.assertIn("review_scope_mismatch", self.review([])["reasons"])

    def test_unstaged_staged_and_untracked_changes_are_not_reviewable(self):
        self.approve()
        for mode in ["unstaged", "staged", "untracked"]:
            with self.subTest(mode=mode):
                path = self.repo / ("extra.py" if mode == "untracked" else "README.md")
                path.write_text("new\n")
                if mode == "staged":
                    self.git("add", "README.md")
                with self.assertRaises(EvidenceError):
                    self.review()
                self.git("reset", "--hard", "-q", "HEAD")
                if mode == "untracked":
                    path.unlink()

    def test_new_commit_invalidates_receipt_even_if_diff_is_same(self):
        self.approve()
        self.git("commit", "--allow-empty", "-qm", "another commit")
        self.assertIn("review_target_stale", self.review()["reasons"])

    def test_changed_blob_invalidates_receipt(self):
        self.approve()
        (self.repo / "README.md").write_text("changed after review\n")
        self.commit("later")
        self.write_event(self.author_log, "README.md")
        self.assertIn("review_target_stale", self.review()["reasons"])

    def test_new_contribution_without_tree_change_invalidates_receipt(self):
        self.approve()
        self.write_event(self.author_log, "README.md")
        self.assertIn("review_target_stale", self.review()["reasons"])

    def test_read_only_commands_do_not_invalidate_receipt(self):
        self.approve()
        self.command_event(self.reviewer_log, "git diff --stat; sed -n '1,40p' README.md; rg -n example README.md")
        self.assertEqual(self.review()["status"], "passed")

    def test_unknown_shell_execution_is_conservatively_a_writer(self):
        for command in ["python3 tool.py", "sed -i s/a/b/ README.md", "cat README.md > changed", "git diff --output=README.md", "git cat-file --filters HEAD:README.md", "git log --show-signature", "rg --hostname-bin tool word", "rg --pre tool word", "cat $(touch README.md)", "cat =(touch README.md)"]:
            with self.subTest(command=command):
                self.reviewer_log = self.session("reviewer", self.reviewer)
                self.sessions[1] = self.reviewer_log
                self.command_event(self.reviewer_log, command)
                self.approve()
                self.assertIn("reviewer_contributed_to_change", self.review()["reasons"])

    def test_relative_activity_path_is_rejected(self):
        self.append(self.reviewer_log, {"type": "FileChange", "id": "relative", "status": "completed",
                                      "changes": {"README.md": {"type": "update"}}})
        with self.assertRaises(EvidenceError):
            self.approve()

    def test_unsupported_tool_cannot_claim_read_only_review(self):
        self.append(self.reviewer_log, {"type": "McpToolCall", "id": "unknown-tool", "tool": "unknown"})
        self.approve()
        self.assertIn("reviewer_contributed_to_change", self.review()["reasons"])

    def test_truncated_session_and_missing_identity_are_rejected(self):
        self.approve()
        self.reviewer_log.write_text('{"partial":')
        with self.assertRaises(EvidenceError):
            self.review()
        self.reviewer_log.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n")
        with self.assertRaises(EvidenceError):
            self.review()

    def test_failed_write_does_not_fill_missing_attribution(self):
        self.author_log = self.session("author", self.author)
        self.sessions[0] = self.author_log
        self.append(self.author_log, {"type": "FileChange", "id": "failed", "status": "failed",
                                     "changes": {str(self.repo / "README.md"): {"type": "update"}}})
        self.approve()
        self.assertIn("review_unattributed_changes", self.review()["reasons"])

    def test_symlink_and_executable_mode_affect_target(self):
        original = self.target()["diff_sha256"]
        (self.repo / "README.md").chmod(0o755)
        self.git("update-index", "--chmod=+x", "README.md")
        self.commit("executable")
        self.assertNotEqual(self.target()["diff_sha256"], original)
        (self.repo / "README.md").unlink()
        os.symlink("outside-target", self.repo / "README.md")
        self.commit("symlink")
        self.assertNotEqual(self.target()["diff_sha256"], original)

    def test_changed_scope_and_base_invalidate_receipt(self):
        self.approve()
        data = json.loads(self.receipt.read_text())
        data["target"]["scope"] = []
        self.receipt.write_text(json.dumps(data))
        self.assertIn("review_target_stale", self.review()["reasons"])

    def test_missing_receipt_and_changes_requested_never_pass(self):
        result = review_evidence(self.repo, ["README.md"], self.base, "HEAD", self.sessions, None)
        self.assertIn("independent_review_missing", result["reasons"])
        self.approve(decision="changes_requested")
        self.assertIn("independent_review_failed", self.review()["reasons"])

    def test_receipt_schema_errors_and_in_repository_receipt_are_rejected(self):
        self.approve()
        data = json.loads(self.receipt.read_text())
        data["extra"] = "not allowed"
        self.receipt.write_text(json.dumps(data))
        with self.assertRaises(EvidenceError):
            self.review()
        with self.assertRaises(EvidenceError):
            review_evidence(self.repo, ["README.md"], self.base, "HEAD", self.sessions, self.repo / "ignored-receipt.json")

    def test_strict_harness_requires_proof_without_changing_preflight_contract(self):
        code, evidence = build_evidence(["README.md"], check_results=["documentation=success"], require_independent_review=True)
        self.assertEqual(code, 2)
        self.assertEqual(evidence["work_status"], "independent_review_incomplete")
        self.assertIn("review_base_missing", evidence["human_review_reasons"])
        code, evidence = run_harness(["README.md"], check_results=["documentation=success"])
        self.assertEqual(code, 0)
        self.assertEqual(evidence["independent_review"]["status"], "not_requested")

    def test_harness_binds_real_policy_paths_to_actual_git_scope(self):
        self.approve()
        with patch("scripts.run_change_harness.REPO_ROOT", self.repo):
            code, evidence = build_evidence(["README.md"], check_results=["documentation=success"],
                review_base=self.base, review_sessions=self.sessions, review_receipt=self.receipt)
        self.assertEqual(code, 0)
        self.assertEqual(evidence["independent_review"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
