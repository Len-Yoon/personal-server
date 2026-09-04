import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "n100-remote-dev.sh"


class N100RemoteDevHostTests(unittest.TestCase):
    def run_script(self, *args, env=None):
        variables = os.environ.copy()
        variables.update(env or {})
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=ROOT,
            env=variables,
            text=True,
            capture_output=True,
        )

    def test_keygen_creates_default_private_key_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.run_script("keygen", env={"HOME": home})
            key_path = Path(home) / ".ssh" / "id_ed25519_n100"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(key_path.is_file())
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
            self.assertIn("administrators_authorized_keys", result.stdout)

    def test_keygen_does_not_overwrite_existing_key(self):
        with tempfile.TemporaryDirectory() as home:
            key_path = Path(home) / ".ssh" / "id_ed25519_n100"
            key_path.parent.mkdir()
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                check=True,
            )
            original = key_path.read_text(encoding="utf-8")
            result = self.run_script("keygen", env={"HOME": home})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(key_path.read_text(encoding="utf-8"), original)

    def test_start_rejects_non_regular_task_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script("start", "--task-file", directory)
            self.assertNotEqual(result.returncode, 0)

    def test_start_rejects_task_file_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real-task.txt"
            target.write_text("task", encoding="utf-8")
            link = root / "task-link.txt"
            link.symlink_to(target)
            result = self.run_script("start", "--task-file", str(link))
            self.assertNotEqual(result.returncode, 0)

    def test_start_rejects_task_file_larger_than_64_kib(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task.txt"
            task.write_bytes(b"x" * (64 * 1024 + 1))
            result = self.run_script("start", "--task-file", str(task))
            self.assertNotEqual(result.returncode, 0)

    def test_unknown_command_is_rejected(self):
        result = self.run_script("unknown")
        self.assertNotEqual(result.returncode, 0)

    def test_real_connection_defaults_and_strict_host_key_checking(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEFAULT_TARGET="n100-codex"', script)
        self.assertIn('N100_SSH_KEY:-${HOME}/.ssh/id_ed25519_n100', script)
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertNotIn("StrictHostKeyChecking=accept-new", script)

    def test_start_sends_task_on_stdin_to_fixed_remote_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            key = home / ".ssh" / "n100-key"
            key.parent.mkdir(parents=True)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            task = root / "task.txt"
            task.write_text("do-not-put-this-in-command", encoding="utf-8")
            args_file = root / "args"
            stdin_file = root / "stdin"
            ssh_stub = root / "ssh"
            ssh_stub.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$N100_ARGS_FILE\"\ncat > \"$N100_STDIN_FILE\"\n",
                encoding="utf-8",
            )
            ssh_stub.chmod(0o755)
            result = self.run_script(
                "start", "--task-file", str(task),
                env={
                    "HOME": str(home),
                    "N100_SSH_KEY": str(key),
                    "N100_ARGS_FILE": str(args_file),
                    "N100_STDIN_FILE": str(stdin_file),
                    "PATH": f"{root}:{os.environ['PATH']}",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stdin_file.read_text(encoding="utf-8"), task.read_text(encoding="utf-8"))
            command = args_file.read_text(encoding="utf-8")
            self.assertIn("wsl.exe", command)
            self.assertNotIn("do-not-put-this-in-command", command)


if __name__ == "__main__":
    unittest.main()
