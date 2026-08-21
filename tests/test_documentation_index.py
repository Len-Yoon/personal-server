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

    def test_history_plans_keep_examples_inside_fenced_blocks(self):
        plan_paths = [
            "docs/superpowers/plans/2026-08-21-agent-loop-ci-gate.md",
            "docs/superpowers/plans/2026-08-21-codex-work-completion-loop.md",
        ]

        for path in plan_paths:
            in_fence = False
            headings = []
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                if line.startswith("```"):
                    in_fence = not in_fence
                    continue
                if not in_fence and line.startswith("# "):
                    headings.append(line)
            self.assertEqual(len(headings), 1, path)


if __name__ == "__main__":
    unittest.main()
