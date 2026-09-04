import subprocess
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
LOADER = REPO_ROOT / "scripts" / "runtime-service-state.sh"


def run_state_loader(project_root: Path) -> subprocess.CompletedProcess[str]:
    command = [
        "bash",
        "-c",
        textwrap.dedent(
            """
            source "$1"
            load_service_runtime_state_test_fixture "$2"
            """
        ),
        "loader-test",
        str(LOADER),
        str(project_root),
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def run_production_loader(project_root: Path) -> subprocess.CompletedProcess[str]:
    command = [
        "bash", "-c", "source \"$1\"; load_service_runtime_state \"$2\"",
        "loader-test", str(LOADER), str(project_root),
    ]
    return subprocess.run(command, text=True, capture_output=True, check=False)


class RuntimeServiceStateTests(unittest.TestCase):
    def test_unknown_service_in_runtime_state_is_rejected(self):
        project_root = self._temporary_project()
        try:
            state = project_root / "data" / "k3s-runtime-services.state"
            state.parent.mkdir()
            state.write_text("crawler-worker=compose\nunknown=k3s\n", encoding="utf-8")
            self.assertNotEqual(run_state_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    def test_missing_runtime_state_defaults_all_services_to_compose(self):
        project_root = self._temporary_project()
        try:
            (project_root / "data").mkdir()
            result = run_state_loader(project_root)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "crawler-worker=compose",
                    "youtube-memo=compose",
                    "book-memo=compose",
                ],
            )
        finally:
            self._remove_project(project_root)

    def test_duplicate_empty_and_malformed_rows_are_rejected(self):
        for contents in (
            "crawler-worker=compose\ncrawler-worker=k3s\n",
            "crawler-worker=compose\n\n",
            "crawler-worker=compose\nnot-a-row\n",
            "crawler-worker=compose\ncrawler-worker=\n",
            "crawler-worker=compose\ncrawler-worker=compose\n",
        ):
            with self.subTest(contents=repr(contents)):
                project_root = self._temporary_project()
                try:
                    state = project_root / "data" / "k3s-runtime-services.state"
                    state.parent.mkdir()
                    state.write_text(contents, encoding="utf-8")
                    self.assertNotEqual(run_state_loader(project_root).returncode, 0)
                finally:
                    self._remove_project(project_root)

    def test_valid_state_is_output_in_fixed_service_order(self):
        project_root = self._temporary_project()
        try:
            state = project_root / "data" / "k3s-runtime-services.state"
            state.parent.mkdir()
            state.write_text(
                "book-memo=k3s\ncrawler-worker=compose\nyoutube-memo=k3s\n",
                encoding="utf-8",
            )
            result = run_state_loader(project_root)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "crawler-worker=compose",
                    "youtube-memo=k3s",
                    "book-memo=k3s",
                ],
            )
        finally:
            self._remove_project(project_root)

    def test_dangling_runtime_state_symlink_is_rejected(self):
        project_root = self._temporary_project()
        try:
            state = project_root / "data" / "k3s-runtime-services.state"
            state.parent.mkdir()
            state.symlink_to(project_root / "data" / "missing.state")
            self.assertNotEqual(run_state_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    def test_nul_containing_runtime_state_row_is_rejected(self):
        project_root = self._temporary_project()
        try:
            state = project_root / "data" / "k3s-runtime-services.state"
            state.parent.mkdir()
            state.write_bytes(b"crawler-worker=compose\x00\n")
            self.assertNotEqual(run_state_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    def test_unreadable_data_directory_is_rejected_instead_of_defaulting(self):
        project_root = self._temporary_project()
        data_dir = project_root / "data"
        try:
            data_dir.mkdir()
            data_dir.chmod(0)
            self.assertNotEqual(run_production_loader(project_root).returncode, 0)
        finally:
            data_dir.chmod(0o700)
            self._remove_project(project_root)

    def test_missing_project_root_is_rejected(self):
        project_root = self._temporary_project()
        self._remove_project(project_root)
        result = run_production_loader(project_root)
        self.assertNotEqual(result.returncode, 0)

    def test_project_root_must_be_a_real_directory(self):
        project_root = self._temporary_project() / "root-file"
        project_root.write_text("not a directory", encoding="utf-8")
        self.assertNotEqual(run_production_loader(project_root).returncode, 0)
        project_root.unlink()

    def test_missing_data_directory_is_rejected(self):
        project_root = self._temporary_project()
        try:
            self.assertNotEqual(run_production_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    def test_data_directory_symlink_is_rejected(self):
        project_root = self._temporary_project()
        try:
            (project_root / "data-target").mkdir()
            (project_root / "data").symlink_to(project_root / "data-target", target_is_directory=True)
            self.assertNotEqual(run_production_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    def test_state_file_symlink_is_rejected_even_when_target_is_valid(self):
        project_root = self._temporary_project()
        try:
            data_dir = project_root / "data"
            data_dir.mkdir()
            target = data_dir / "target.state"
            target.write_text("crawler-worker=compose\n", encoding="utf-8")
            (data_dir / "k3s-runtime-services.state").symlink_to(target)
            self.assertNotEqual(run_production_loader(project_root).returncode, 0)
        finally:
            self._remove_project(project_root)

    @staticmethod
    def _temporary_project() -> Path:
        import tempfile

        return Path(tempfile.mkdtemp())

    @staticmethod
    def _remove_project(project_root: Path) -> None:
        import shutil

        shutil.rmtree(project_root)


if __name__ == "__main__":
    unittest.main()
