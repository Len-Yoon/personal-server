"""Contracts for building K3s application OCI images."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "infra/k8s/tools/k3s-app-image-build.sh"


class K3sAppImageBuildTests(unittest.TestCase):
    def _run_build(self, app="portal-web", tag="k3s-test"):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            call_log = temporary_path / "docker.args"
            (bin_path / "uname").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' Darwin\n", encoding="utf-8"
            )
            (bin_path / "docker").write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$CALL_LOG\"\ntouch \"$BUILD_OUTPUT\"\n",
                encoding="utf-8",
            )
            (bin_path / "uname").chmod(0o755)
            (bin_path / "docker").chmod(0o755)
            output = temporary_path / "portal-web.oci.tar"
            result = subprocess.run(
                [
                    "bash",
                    str(BUILD),
                    "--app",
                    app,
                    "--tag",
                    tag,
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CALL_LOG": str(call_log),
                    "BUILD_OUTPUT": str(output),
                    "PATH": f"{bin_path}:{os.environ['PATH']}",
                },
            )
            return result, call_log.read_text(encoding="utf-8") if call_log.exists() else ""

    def test_portal_web_is_supported_and_builds_linux_amd64_with_immutable_tag(self):
        result, docker_args = self._run_build()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("personal-server-portal-web:k3s-test", docker_args)
        self.assertIn("--platform linux/amd64", docker_args)

    def test_latest_tag_is_rejected_as_mutable(self):
        result, docker_args = self._run_build(tag="latest")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable tag is required", result.stderr)
        self.assertEqual(docker_args, "")

    def test_unknown_app_is_rejected_before_docker_build(self):
        result, docker_args = self._run_build(app="unknown-app")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported app", result.stderr)
        self.assertEqual(docker_args, "")


if __name__ == "__main__":
    unittest.main()
