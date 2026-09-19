import importlib.util
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "setup-local-test-venvs.py"
SPEC = importlib.util.spec_from_file_location("setup_local_test_venvs", MODULE_PATH)
setup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(setup)


class SetupLocalTestVenvsTests(unittest.TestCase):
    def test_go_creates_each_matrix_environment_and_installs_declared_dependencies(self):
        matrix = [
            {
                "name": "portal",
                "python_version": "3.11",
                "requirements": "portal-web/requirements.txt",
                "extra_packages": ["example-extra"],
                "pythonpath": "portal-web",
                "test_command": "python3 -m unittest tests.test_portal_dashboard",
            }
        ]
        with TemporaryDirectory() as directory, mock.patch.object(setup, "load_matrix", return_value=matrix), mock.patch.object(setup, "find_interpreter", return_value="python3.11"), mock.patch.object(setup.subprocess, "run") as run:
            self.assertEqual(setup.main(["--go", "--venv-root", directory]), 0)

        commands = [call.args[0] for call in run.call_args_list]
        python = str(Path(directory) / "portal" / "bin" / "python")
        self.assertEqual(commands[0], ["python3.11", "-m", "venv", str(Path(directory) / "portal")])
        self.assertIn([python, "-m", "pip", "install", "--requirement", str(ROOT / "portal-web/requirements.txt")], commands)
        self.assertIn([python, "-m", "pip", "install", "example-extra"], commands)
        self.assertEqual(commands[-1], [python, "-m", "pip", "check"])

    def test_check_fails_without_a_prepared_environment(self):
        matrix = [{"name": "portal", "python_version": "3.11", "requirements": "", "extra_packages": [], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_portal_dashboard"}]
        with TemporaryDirectory() as directory, mock.patch.object(setup, "load_matrix", return_value=matrix):
            self.assertEqual(setup.main(["--check", "--venv-root", directory]), 1)

    def test_check_validates_an_existing_environment(self):
        matrix = [{"name": "portal", "python_version": "3.11", "requirements": "", "extra_packages": [], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_portal_dashboard"}]
        with TemporaryDirectory() as directory:
            python = Path(directory) / "portal" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            python.chmod(0o755)
            with mock.patch.object(setup, "load_matrix", return_value=matrix), mock.patch.object(setup, "environment_metadata", return_value=("3.11", {})), mock.patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(setup.main(["--check", "--venv-root", directory]), 0)
        self.assertEqual(run.call_args.args[0], [str(python), "-m", "pip", "check"])

    def test_check_rejects_a_wrong_python_version_or_missing_declared_package(self):
        matrix = [{"name": "portal", "python_version": "3.11", "requirements": "", "extra_packages": ["example-extra"], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_portal_dashboard"}]
        with TemporaryDirectory() as directory:
            python = Path(directory) / "portal" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            python.chmod(0o755)
            with mock.patch.object(setup, "load_matrix", return_value=matrix), mock.patch.object(setup, "environment_metadata", return_value=("3.12", {"example-extra": "1.0"})):
                self.assertEqual(setup.main(["--check", "--venv-root", directory]), 1)
            with mock.patch.object(setup, "load_matrix", return_value=matrix), mock.patch.object(setup, "environment_metadata", return_value=("3.11", {})):
                self.assertEqual(setup.main(["--check", "--venv-root", directory]), 1)

    def test_check_rejects_a_wrong_pinned_requirement_version(self):
        matrix = [{"name": "portal", "python_version": "3.11", "requirements": "portal-web/requirements.txt", "extra_packages": [], "pythonpath": ".", "test_command": "python3 -m unittest tests.test_portal_dashboard"}]
        with TemporaryDirectory() as directory:
            python = Path(directory) / "portal" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            python.chmod(0o755)
            installed = {"fastapi": "0.1.0", "uvicorn": "0.34.0", "jinja2": "3.1.5", "requests": "2.32.3", "python-multipart": "0.0.30", "httpx": "0.28.1", "markdown-it-py": "3.0.0"}
            with mock.patch.object(setup, "load_matrix", return_value=matrix), mock.patch.object(setup, "environment_metadata", return_value=("3.11", installed)):
                self.assertEqual(setup.main(["--check", "--venv-root", directory]), 1)


if __name__ == "__main__":
    unittest.main()
