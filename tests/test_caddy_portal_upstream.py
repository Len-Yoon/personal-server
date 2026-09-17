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
    def test_portal_alias_uses_the_cloudflare_dns_tls_policy(self):
        caddyfile = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")

        block = _site_block(caddyfile, "portal.len.pe.kr")
        self.assertIn("import common_tls", block)
        self.assertIn("redir https://len.pe.kr{uri} permanent", block)
        common_tls = re.search(
            r"^\(common_tls\) \{\n(?P<body>.*?)(?=^\S|\Z)",
            caddyfile,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(common_tls)
        self.assertIn("dns cloudflare {env.CLOUDFLARE_API_TOKEN}", common_tls.group("body"))

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

    def test_n100_caddy_does_not_wait_for_k3s_book_memo_rollback_container(self):
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")
        caddy = compose[compose.index("  caddy:"):]

        self.assertIn("  book-memo:\n", compose)
        self.assertNotIn("      book-memo:\n        condition: service_healthy", caddy)

    def test_books_upstream_is_runtime_configured_with_a_compose_safe_default(self):
        """Books must use K3s discovery at runtime without committing a ClusterIP."""
        caddyfile = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.n100.yml").read_text(encoding="utf-8")

        books = _site_block(caddyfile, "books.len.pe.kr")
        self.assertIn("reverse_proxy {env.BOOK_MEMO_UPSTREAM}", books)
        self.assertNotRegex(books, r"\b10\.\d+\.\d+\.\d+\b")
        caddy = compose[compose.index("  caddy:"):]
        self.assertIn("BOOK_MEMO_UPSTREAM: ${BOOK_MEMO_UPSTREAM:-book-memo:8003}", caddy)

    def test_startup_scripts_resolve_k3s_books_before_recreating_caddy(self):
        """A K3s Books marker must gate Caddy recreation on ready Service endpoints."""
        for filename in ("deploy-n100.sh", "windows-bootstrap.sh"):
            script = (ROOT / "scripts" / filename).read_text(encoding="utf-8")

            self.assertIn("resolve_book_memo_caddy_upstream", script)
            self.assertIn("service/book-memo", script)
            self.assertIn("endpoints/book-memo", script)
            self.assertIn(".spec.selector.app\\.kubernetes\\.io/name", script)
            self.assertIn(".spec.ports[0].targetPort", script)
            self.assertIn("BOOK_MEMO_UPSTREAM", script)
            self.assertIn("sudo -n k3s kubectl", script)


if __name__ == "__main__":
    unittest.main()
