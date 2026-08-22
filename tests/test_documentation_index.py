from pathlib import Path
import unittest


class DocumentationIndexTests(unittest.TestCase):
    def test_project_readme_exposes_quick_start_verification_and_next_steps(self):
        content = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("## 🚀 빠른 시작", content)
        self.assertIn("## ✅ 검증", content)
        self.assertIn("## 🔎 더 알아보기", content)

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

    def test_subagent_workflow_defines_mandatory_routes(self):
        project_rules = Path("AGENTS.md").read_text(encoding="utf-8")
        workflow = Path("docs/codex-work-loop.md").read_text(encoding="utf-8")

        self.assertIn("기능 구현·테스트·설정 파일", project_rules)
        self.assertIn("전문 검토 에이전트를 필수로 포함해 최대 4명", project_rules)
        self.assertIn("에이전트 운영 기록", workflow)
        self.assertIn("전문 검토를 필수로 적용함", workflow)


if __name__ == "__main__":
    unittest.main()
