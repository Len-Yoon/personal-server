import unittest
from pathlib import Path
import re
from pip._vendor.packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILES = (
    ROOT / "book-memo" / "requirements.txt",
    ROOT / "crawler-worker" / "requirements.txt",
    ROOT / "portal-web" / "requirements.txt",
    ROOT / "youtube-memo" / "requirements.txt",
)


class PythonMultipartSecurityTests(unittest.TestCase):
    def test_all_service_requirements_pin_security_fixed_python_multipart(self):
        for requirements_file in REQUIREMENTS_FILES:
            with self.subTest(requirements_file=requirements_file):
                lines = requirements_file.read_text(encoding="utf-8").splitlines()
                pins = [
                    re.fullmatch(r"python-multipart==([^\s#]+)", line.strip())
                    for line in lines
                    if line.strip().startswith("python-multipart==")
                ]
                self.assertEqual(len(pins), 1)
                assert pins[0] is not None
                self.assertGreaterEqual(Version(pins[0].group(1)), Version("0.0.30"))


if __name__ == "__main__":
    unittest.main()
