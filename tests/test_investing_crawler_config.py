import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InvestingCrawlerConfigTests(unittest.TestCase):
    def test_environment_example_declares_importer_settings(self):
        content = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "OBSIDIAN_VAULT_PATH",
            "OBSIDIAN_NEWS_DIR",
            "INVESTING_NEWS_URL",
            "INVESTING_NEWS_LIMIT",
            "INVESTING_NEWS_TIMEZONE",
        ):
            self.assertIn(name, content)

    def test_compose_defines_one_shot_investing_crawler_with_vault_mount(self):
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("investing-crawler:", content)
        self.assertIn("dockerfile: investing_crawler/Dockerfile", content)
        self.assertIn("${OBSIDIAN_VAULT_PATH}:/vault", content)
        self.assertIn("python -m app.main", content)
        self.assertNotIn("restart: unless-stopped\n    investing-crawler", content)


if __name__ == "__main__":
    unittest.main()
