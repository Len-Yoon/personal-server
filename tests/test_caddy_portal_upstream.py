import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTAL_HOSTS = (
    "len.pe.kr",
    "portfolio.len.pe.kr",
    "file.len.pe.kr",
    "admin.len.pe.kr",
)


def _site_block(caddyfile: str, host: str) -> str:
    match = re.search(
        rf"^{re.escape(host)} \{{\n(?P<body>.*?)(?=^\S|\Z)",
        caddyfile,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing Caddy site block: {host}")
    return match.group("body")


class CaddyPortalUpstreamContractTest(unittest.TestCase):
    def test_all_portal_hosts_use_one_environment_backed_upstream(self):
        caddyfile = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")

        for host in PORTAL_HOSTS:
            block = _site_block(caddyfile, host)
            self.assertEqual(block.count("reverse_proxy"), 1)
            self.assertIn("reverse_proxy {env.PORTAL_UPSTREAM}", block)

        self.assertEqual(caddyfile.count("{env.PORTAL_UPSTREAM}"), len(PORTAL_HOSTS))

    def test_n100_caddy_defaults_to_current_compose_upstream_and_host_gateway(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        caddy = compose[compose.index("  caddy:"):]

        self.assertIn("PORTAL_UPSTREAM: ${PORTAL_UPSTREAM:-portal-web:8000}", caddy)
        self.assertIn("host.docker.internal:host-gateway", caddy)


if __name__ == "__main__":
    unittest.main()
