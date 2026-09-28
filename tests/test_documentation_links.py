"""Keep repository-local Markdown links and image references resolvable."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


class DocumentationLinksTests(unittest.TestCase):
    def test_html_image_missing_target_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "README.md").write_text('<img src="docs/images/missing.svg">', encoding="utf-8")
            (root / "AGENTS.md").write_text("", encoding="utf-8")
            (root / "CLAUDE.md").write_text("", encoding="utf-8")
            with mock.patch(f"{__name__}.ROOT", root):
                with self.assertRaisesRegex(AssertionError, "docs/images/missing.svg"):
                    self.test_local_documentation_targets_exist()

    def test_local_documentation_targets_exist(self):
        documents = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md"]
        documents.extend((ROOT / "docs").rglob("*.md"))
        missing = []
        for document in documents:
            content = document.read_text(encoding="utf-8")
            for match in [*INLINE_LINK.finditer(content), *HTML_IMAGE.finditer(content)]:
                target = match.group(1).strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                path = (document.parent / unquote(parsed.path)).resolve()
                if not path.is_file():
                    missing.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(missing, [])
