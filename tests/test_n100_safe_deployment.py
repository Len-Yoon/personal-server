import importlib.util
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "classify-n100-safe-deployment.py"
SPEC = importlib.util.spec_from_file_location("classify_n100_safe_deployment", MODULE_PATH)
classifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = classifier
SPEC.loader.exec_module(classifier)


class N100SafeDeploymentClassifierTests(unittest.TestCase):
    def test_allowlisted_service_change_deploys_only_that_service(self):
        decision = classifier.classify_changed_paths(["crawler-worker/app/main.py"])
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(decision.services, ("crawler-worker",))

    def test_multiple_allowlisted_services_are_sorted_and_deployed(self):
        decision = classifier.classify_changed_paths(
            ["youtube-memo/app/main.py", "book-memo/app/main.py"]
        )
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(decision.services, ("book-memo", "youtube-memo"))

    def test_shared_n100_compose_change_deploys_all_safe_services(self):
        decision = classifier.classify_changed_paths(["docker-compose.n100.yml"])
        self.assertEqual(decision.action, "deploy")
        self.assertEqual(
            decision.services,
            ("book-memo", "car-care-worker", "crawler-worker", "youtube-memo"),
        )

    def test_allowlisted_and_unknown_runtime_change_is_blocked(self):
        decision = classifier.classify_changed_paths(
            ["crawler-worker/app/main.py", "scripts/deploy-n100.sh"]
        )
        self.assertEqual(decision.action, "blocked")

    def test_portal_or_k3s_change_is_blocked_before_deployment(self):
        for path in ("portal-web/app/main.py", "infra/k8s/deployment.yaml"):
            with self.subTest(path=path):
                decision = classifier.classify_changed_paths([path])
                self.assertEqual(decision.action, "blocked")
                self.assertEqual(decision.services, ())

    def test_documentation_and_tests_only_changes_are_skipped(self):
        decision = classifier.classify_changed_paths(["README.md", "tests/test_example.py"])
        self.assertEqual(decision.action, "skip")
        self.assertEqual(decision.services, ())

    def test_cli_writes_github_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            result = classifier.main(
                [
                    "--base",
                    "base",
                    "--head",
                    "head",
                    "--github-output",
                    str(output),
                ],
                changed_paths=["crawler-worker/app/main.py"],
            )
            self.assertEqual(result, 0)
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
            )
            self.assertEqual(values["deploy_action"], "deploy")
            self.assertEqual(values["deploy_services"], "crawler-worker")

    def test_sensitive_service_local_runtime_paths_are_blocked(self):
        for path in ("crawler-worker/.env", "crawler-worker/data/runtime.sqlite"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_traversal_segments_are_blocked(self):
        for path in ("crawler-worker/../portal-web/app.py", "crawler-worker/./app.py"):
            with self.subTest(path=path):
                self.assertEqual(classifier.classify_changed_paths([path]).action, "blocked")

    def test_rename_input_includes_old_and_new_paths(self):
        decision = classifier.classify_changed_paths(
            ["crawler-worker/app/old.py", "portal-web/app/new.py"]
        )
        self.assertEqual(decision.action, "blocked")

    def test_cli_git_discovery_is_nul_safe_and_expands_rename_paths(self):
        completed = mock.Mock(stdout=b"R100\0crawler-worker/.env\0crawler-worker/app/new.py\0")
        with mock.patch.object(classifier.subprocess, "run", return_value=completed) as run:
            paths = classifier._changed_paths("base", "head")
        self.assertEqual(paths, ["crawler-worker/.env", "crawler-worker/app/new.py"])
        self.assertEqual(run.call_args.args[0][1:5], ["diff", "--name-status", "-z", "--find-renames"])


if __name__ == "__main__":
    unittest.main()
