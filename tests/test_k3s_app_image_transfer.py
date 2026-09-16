"""K3s application image archive transfer contracts."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "infra/k8s/tools/k3s-app-image-build.sh"
IMPORT = ROOT / "infra/k8s/tools/k3s-app-image-import.sh"


class K3sAppImageTransferTests(unittest.TestCase):
    def test_build_script_requires_supported_app_amd64_and_explicit_output(self):
        text = BUILD.read_text(encoding="utf-8")

        self.assertIn('SUPPORTED_APPS="book-memo youtube-memo crawler-worker car-care-worker"', text)
        self.assertIn("--platform linux/amd64", text)
        self.assertIn("--output type=oci,dest=", text)
        self.assertIn("uname -s", text)
        self.assertIn("Darwin", text)
        self.assertIn("latest", text)
        self.assertIn("image_build=PASS", text)

    def test_import_script_requires_go_and_rejects_missing_archive_before_ctr_access(self):
        result = subprocess.run(
            ["bash", str(IMPORT), "--archive", "/missing", "--image", "x:y"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image_import=FAIL", result.stderr)
        self.assertNotIn("ctr images import", result.stdout + result.stderr)

    def test_import_script_verifies_digest_platform_and_node_before_import(self):
        text = IMPORT.read_text(encoding="utf-8")

        self.assertIn('"$go" = true', text)
        self.assertIn("sha256sum --check --status", text)
        self.assertIn("verify_oci_linux_amd64", text)
        self.assertIn("k3s kubectl get node -o name", text)
        self.assertIn("k3s ctr images import", text)
        self.assertIn("image_import=PASS", text)


if __name__ == "__main__":
    unittest.main()
