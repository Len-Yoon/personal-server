from pathlib import Path
import unittest


class DocumentationIndexTests(unittest.TestCase):
    def test_document_index_separates_current_operations_and_history(self):
        content = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 운영 기준", content)
        self.assertIn("## 참고 자료", content)
        self.assertIn("## 과거 이력", content)
        self.assertIn("codex-work-loop.md", content)
        self.assertIn("agent-loop-evidence.md", content)


if __name__ == "__main__":
    unittest.main()
