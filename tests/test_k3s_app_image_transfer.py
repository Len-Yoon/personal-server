"""K3s application image archive transfer contracts."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "infra/k8s/tools/k3s-app-image-build.sh"
IMPORT = ROOT / "infra/k8s/tools/k3s-app-image-import.sh"


class K3sAppImageTransferTests(unittest.TestCase):
    def _add_blob(self, blobs, document):
        payload = json.dumps(document, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        blobs[f"blobs/sha256/{digest}"] = payload
        return f"sha256:{digest}"

    def _write_nested_oci_archive(self, path, config_architecture):
        blobs = {}
        config_digest = self._add_blob(
            blobs,
            {"architecture": config_architecture, "os": "linux"},
        )
        manifest_digest = self._add_blob(
            blobs,
            {
                "config": {
                    "digest": config_digest,
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                },
                "schemaVersion": 2,
            },
        )
        nested_index_digest = self._add_blob(
            blobs,
            {
                "manifests": [
                    {
                        "digest": manifest_digest,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
                "schemaVersion": 2,
            },
        )
        index = {
            "manifests": [
                {
                    "digest": nested_index_digest,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
            "schemaVersion": 2,
        }
        with tarfile.open(path, "w") as archive:
            for name, payload in {"index.json": json.dumps(index).encode(), **blobs}.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    def _write_fake_sudo(self, directory):
        fake_sudo = directory / "sudo"
        fake_sudo.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
            "if [ \"$3\" = kubectl ]; then exit 0; fi\n"
            "if [ \"$3 $4 $5\" = 'ctr images list' ]; then printf '%s\\n' 'personal-server-book-memo:v1'; fi\n",
            encoding="utf-8",
        )
        fake_sudo.chmod(0o755)
        fake_sha256sum = directory / "sha256sum"
        fake_sha256sum.write_text(
            "#!/usr/bin/env bash\n"
            "[ \"$1 $2\" = '--check --status' ] || exit 64\n"
            "cat >/dev/null\n",
            encoding="utf-8",
        )
        fake_sha256sum.chmod(0o755)

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

    def test_import_accepts_nested_oci_index_when_resolved_config_is_linux_amd64(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            archive = temporary_path / "image.tar"
            command_directory = temporary_path / "bin"
            command_directory.mkdir()
            call_log = temporary_path / "calls.log"
            self._write_nested_oci_archive(archive, "amd64")
            self._write_fake_sudo(command_directory)

            result = subprocess.run(
                [
                    "bash", str(IMPORT), "--go", "--archive", str(archive),
                    "--sha256", hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "--image", "personal-server-book-memo:v1",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CALL_LOG": str(call_log),
                    "PATH": f"{command_directory}:{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("image_import=PASS", result.stdout)
            self.assertIn("k3s ctr images import", call_log.read_text(encoding="utf-8"))

    def test_import_rejects_amd64_descriptor_when_resolved_config_is_arm64(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            archive = temporary_path / "image.tar"
            command_directory = temporary_path / "bin"
            command_directory.mkdir()
            call_log = temporary_path / "calls.log"
            self._write_nested_oci_archive(archive, "arm64")
            self._write_fake_sudo(command_directory)

            result = subprocess.run(
                [
                    "bash", str(IMPORT), "--go", "--archive", str(archive),
                    "--sha256", hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "--image", "personal-server-book-memo:v1",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CALL_LOG": str(call_log),
                    "PATH": f"{command_directory}:{os.environ['PATH']}",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("archive platform is not linux/amd64", result.stderr)
            self.assertFalse(call_log.exists(), "K3s commands must not run before platform validation")


if __name__ == "__main__":
    unittest.main()
