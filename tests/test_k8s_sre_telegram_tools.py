import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra" / "k8s" / "tools"


def read_tool(name: str) -> str:
    path = TOOLS / name
    if not path.is_file():
        raise AssertionError(f"required tool is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


class SreTelegramToolContractTest(unittest.TestCase):
    def test_preflight_invokes_only_read_only_k3s_and_helm_commands(self):
        script = read_tool("sre-telegram-preflight.sh")

        self.assertIn("kubectl get nodes --no-headers", script)
        self.assertIn('helm status "$RELEASE" --namespace "$NAMESPACE"', script)
        self.assertNotRegex(script, r"kubectl (apply|create|delete|patch|replace|edit)")
        self.assertNotRegex(script, r"helm (install|upgrade|uninstall|rollback)")

    def test_render_never_imports_image_or_applies_resources(self):
        script = read_tool("sre-telegram-install.sh")
        render = script.split("render()", 1)[1].split("require_secret_contract", 1)[0]

        self.assertIn("helm template", render)
        self.assertIn("--dry-run=client", render)
        self.assertNotIn("ctr -n k8s.io images import", render)
        self.assertNotIn("docker build", render)
        self.assertNotIn("--apply", render)

    def test_apply_requires_all_secret_keys_by_name_without_reading_values(self):
        script = read_tool("sre-telegram-install.sh")

        for key in ("telegram_bot_token", "allowed_chat_id", "alertmanager_auth_token", "alertmanager.yaml"):
            self.assertIn(key, script)
        self.assertRegex(script, r"kubectl(?: -n [^ ]+)? describe secret")
        self.assertNotRegex(script, r"secret[^\n]*(jsonpath|\.data|-o +yaml|-o +json)")
        self.assertIn('mode="${1:---render}"', script)
        self.assertIn("--apply)", script)

    def test_verify_never_reads_or_prints_secret_data(self):
        script = read_tool("sre-telegram-verify.sh")

        self.assertIn("port-forward --address 127.0.0.1", script)
        self.assertIn("/healthz", script)
        self.assertIn("/api/v1/targets", script)
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+get\s+secrets?")
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+describe\s+secrets?")
        self.assertNotIn(".data", script)

    def test_secret_guidance_only_names_required_keys_and_manual_procedure(self):
        script = read_tool("sre-telegram-secret-template.sh")

        self.assertIn("N100", script)
        self.assertIn("manual", script.lower())
        self.assertIn("telegram_bot_token", script)
        self.assertIn("allowed_chat_id", script)
        self.assertIn("alertmanager_auth_token", script)
        self.assertIn("alertmanager.yaml", script)
        self.assertNotRegex(script, r"kubectl +(create|apply|patch|replace)")
        self.assertNotRegex(script, r"(echo|printf).*TOKEN")


if __name__ == "__main__":
    unittest.main()
