import importlib.util
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
