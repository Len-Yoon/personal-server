import unittest
from pathlib import Path


GITHUB_CONFIG = Path(__file__).resolve().parents[1] / ".github"


class DependabotDisabledContractTests(unittest.TestCase):
    def test_dependabot_version_updates_have_no_repository_configuration(self):
        for name in ("dependabot.yml", "dependabot.yaml"):
            with self.subTest(name=name):
                self.assertFalse((GITHUB_CONFIG / name).exists())


if __name__ == "__main__":
    unittest.main()
