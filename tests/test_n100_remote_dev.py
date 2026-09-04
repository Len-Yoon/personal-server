import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "n100-remote-dev.sh"
REMOTE_SCRIPT = ROOT / "scripts" / "n100-remote-dev-remote.sh"
DOCUMENTATION = ROOT / "docs" / "n100-remote-development.md"


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


class N100RemoteDevRemoteTests(unittest.TestCase):
    def remote_script_text(self):
        return REMOTE_SCRIPT.read_text(encoding="utf-8")

    def run_remote_script(self, *args, input_text="", env=None):
        variables = os.environ.copy()
        variables.update(env or {})
        return subprocess.run(
            [str(REMOTE_SCRIPT), *args],
            cwd=ROOT,
            env=variables,
            input=input_text,
            text=True,
            capture_output=True,
        )

    def make_source_repo(self, root):
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test User"], check=True)
        (source / "tracked.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
        return source

    def make_tool_stubs(self, root):
        tools = root / "tools"
        tools.mkdir()
        for name, content in {
            "codex": "#!/bin/sh\nexit 0\n",
            "gh": "#!/bin/sh\n[ \"$1\" = auth ] && [ \"$2\" = status ]\n",
            "tmux": (
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  has-session) exit \"${N100_TMUX_HAS_SESSION:-1}\" ;;\n"
                "  new-session|kill-session) printf '%s\\n' \"$@\" > \"$N100_TMUX_ARGS\" ;;\n"
                "esac\n"
            ),
        }.items():
            path = tools / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        return tools

    def test_remote_start_contains_fixed_safe_codex_prompt(self):
        text = self.remote_script_text()
        self.assertIn("Do not push, create a pull request, merge, deploy", text)
        self.assertIn("access or store credentials", text)
        self.assertIn("server, scheduler, K3s, Compose, Caddy, or tunnel", text)

    def test_remote_stop_only_targets_fixed_tmux_session(self):
        self.assertIn('tmux kill-session -t "$SESSION_NAME"', self.remote_script_text())

    def test_preflight_does_not_create_state_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            home = root / "home"
            result = self.run_remote_script(
                "preflight", str(source),
                env={"HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("n100_remote_dev=PASS", result.stdout)
            self.assertFalse(home.exists())

    def test_preflight_rejects_missing_codex_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            (tools / "codex").write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = login ] && [ \"$2\" = status ] && exit 1\n"
                "exit 0\n",
                encoding="utf-8",
            )
            home = root / "home"
            result = self.run_remote_script(
                "preflight", str(source),
                env={"HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("n100_remote_dev=FAIL", result.stderr)
            self.assertFalse(home.exists())

    def test_start_uses_dedicated_worktree_without_changing_dirty_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            (source / "tracked.txt").write_text("dirty source\n", encoding="utf-8")
            tools = self.make_tool_stubs(root)
            home = root / "home"
            tmux_args = root / "tmux-args"
            result = self.run_remote_script(
                "start", str(source), input_text="run focused tests\n",
                env={
                    "HOME": str(home),
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "N100_TMUX_ARGS": str(tmux_args),
                },
            )
            state_dir = home / ".local/state/personal-server/n100-dev"
            task_file = state_dir / "task.txt"
            worktree = home / ".local/share/personal-server/n100-dev-worktree"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((source / "tracked.txt").read_text(encoding="utf-8"), "dirty source\n")
            self.assertTrue(worktree.is_dir())
            self.assertTrue((worktree / ".git").exists())
            self.assertEqual(task_file.read_text(encoding="utf-8"), "run focused tests\n")
            self.assertEqual(stat.S_IMODE(task_file.stat().st_mode), 0o600)
            self.assertIn("personal-server-codex-dev", tmux_args.read_text(encoding="utf-8"))

    def test_start_rejects_an_active_fixed_tmux_session_without_writing_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            home = root / "home"
            result = self.run_remote_script(
                "start", str(source), input_text="must not be stored\n",
                env={
                    "HOME": str(home),
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "N100_TMUX_HAS_SESSION": "0",
                    "N100_TMUX_ARGS": str(root / "tmux-args"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("n100_remote_dev=PASS", result.stdout)
            self.assertFalse(home.exists())

    def test_start_rejects_task_larger_than_64_kib(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            home = root / "home"
            result = self.run_remote_script(
                "start", str(source), input_text="x" * (64 * 1024 + 1),
                env={
                    "HOME": str(home),
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "N100_TMUX_ARGS": str(root / "tmux-args"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((home / ".local/state/personal-server/n100-dev/task.txt").exists())

    def test_start_rejects_dirty_existing_worktree_without_replacing_prior_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            home = root / "home"
            tmux_args = root / "tmux-args"
            environment = {
                "HOME": str(home),
                "PATH": f"{tools}:{os.environ['PATH']}",
                "N100_TMUX_ARGS": str(tmux_args),
            }
            first = self.run_remote_script("start", str(source), input_text="prior task\n", env=environment)
            worktree = home / ".local/share/personal-server/n100-dev-worktree"
            task_file = home / ".local/state/personal-server/n100-dev/task.txt"
            self.assertEqual(first.returncode, 0, first.stderr)
            (worktree / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

            second = self.run_remote_script("start", str(source), input_text="replacement task\n", env=environment)

            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(task_file.read_text(encoding="utf-8"), "prior task\n")

    def test_start_refuses_a_same_named_branch_from_another_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source_repo(root)
            tools = self.make_tool_stubs(root)
            home = root / "home"
            unrelated = home / ".local/share/personal-server/n100-dev-worktree"
            unrelated.parent.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "codex/n100-dev-unrelated", str(unrelated)], check=True)
            task_file = home / ".local/state/personal-server/n100-dev/task.txt"
            task_file.parent.mkdir(parents=True)
            task_file.write_text("prior task\n", encoding="utf-8")
            result = self.run_remote_script(
                "start", str(source), input_text="must not run here\n",
                env={
                    "HOME": str(home),
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "N100_TMUX_ARGS": str(root / "tmux-args"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(task_file.read_text(encoding="utf-8"), "prior task\n")
            self.assertFalse((root / "tmux-args").exists())


class N100RemoteDevDocumentationTests(unittest.TestCase):
    def documentation_text(self):
        return DOCUMENTATION.read_text(encoding="utf-8")

    def test_documentation_describes_existing_alias_and_key(self):
        text = self.documentation_text()
        self.assertIn("n100-codex", text)
        self.assertIn("~/.ssh/id_ed25519_n100", text)
        self.assertIn("직접 IP", text)

    def test_documentation_contains_supported_commands(self):
        text = self.documentation_text()
        for command in ("preflight", "start --task-file /absolute/path/task.txt", "status", "logs", "stop"):
            self.assertIn(command, text)

    def test_documentation_describes_dedicated_worktree_and_dirty_source_safety(self):
        text = self.documentation_text()
        self.assertIn("전용 WSL worktree", text)
        self.assertIn("dirty source tree", text)
        self.assertIn("/mnt/c/personal-server", text)
        self.assertIn("personal-server-codex-dev", text)

    def test_documentation_prohibits_credential_storage_and_external_changes(self):
        text = self.documentation_text()
        for secret in ("sudo 비밀번호", "rclone", "GitHub", "Codex"):
            self.assertIn(secret, text)
        for prohibited in ("push", "pull request", "merge", "deploy"):
            self.assertIn(prohibited, text)
        self.assertNotIn("비밀번호를 파일에 저장", text)

    def test_documentation_includes_disconnect_recovery(self):
        text = self.documentation_text()
        self.assertIn("SSH 연결이 끊겨도", text)
        self.assertIn("Mac이 깨어난 뒤", text)
        self.assertIn("중복 시작하지 않음", text)


if __name__ == "__main__":
    unittest.main()
