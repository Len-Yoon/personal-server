import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ComposeConfigTests(unittest.TestCase):
    def test_runtime_services_define_healthchecks(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for port in (8000, 8010, 8001, 8002, 8003):
            self.assertIn("healthcheck:", compose)
            self.assertIn(f"127.0.0.1:{port}", compose)

    def test_caddy_waits_for_runtime_services_to_be_healthy(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("portal-web:", compose)
        self.assertIn("crawler-worker:", compose)


if __name__ == "__main__":
    unittest.main()
