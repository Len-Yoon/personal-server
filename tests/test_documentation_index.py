from pathlib import Path
import unittest


class DocumentationIndexTests(unittest.TestCase):
    def test_project_readme_exposes_quick_start_verification_and_next_steps(self):
        content = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("## 빠른 상태 확인", content)
        self.assertIn("## 검증", content)
        self.assertIn("## 운영 문서", content)
        self.assertIn("Cloudflare Tunnel", content)
        self.assertIn("K3s", content)
        self.assertIn("Telegram", content)

    def test_document_index_exposes_only_current_operations_documents(self):
        content = Path("docs/README.md").read_text(encoding="utf-8")

        self.assertIn("## 현재 운영 기준", content)
        self.assertIn("Cloudflare Tunnel", content)
        self.assertIn("K3s·모니터링·백업", content)
        self.assertIn("codex-work-loop.md", content)
        self.assertIn("agent-loop-evidence.md", content)
        self.assertNotIn("superpowers/", content)

    def test_operations_reference_describes_current_runtime_split(self):
        content = Path("docs/operations-reference.md").read_text(encoding="utf-8")

        self.assertIn("Cloudflare Tunnel → Caddy → K3s Portal", content)
        self.assertIn("portal-web", content)
        self.assertIn("crawler-worker", content)
        self.assertIn("공개 상태 Telegram 알림", content)

    def test_subagent_workflow_defines_mandatory_routes(self):
        project_rules = Path("AGENTS.md").read_text(encoding="utf-8")
        workflow = Path("docs/codex-work-loop.md").read_text(encoding="utf-8")

        self.assertIn("기능 구현·테스트·설정 파일", project_rules)
        self.assertIn("전문 검토 에이전트를 필수로 포함해 최대 4명", project_rules)
        self.assertIn("에이전트 운영 기록", workflow)
        self.assertIn("전문 검토를 필수로 적용함", workflow)


if __name__ == "__main__":
    unittest.main()
