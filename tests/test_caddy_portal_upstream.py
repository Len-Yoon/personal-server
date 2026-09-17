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


def _function_body(script: str, function: str) -> str:
    match = re.search(
        rf"^{re.escape(function)}\(\) \{{\n(?P<body>.*?)(?=^[A-Za-z_][A-Za-z0-9_]*\(\) \{{)",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Missing function: {function}")
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
        deploy = (ROOT / "scripts" / "deploy-n100.sh").read_text(encoding="utf-8")
        bootstrap = (ROOT / "scripts" / "windows-bootstrap.sh").read_text(encoding="utf-8")

        for script in (deploy, bootstrap):
            self.assertIn("resolve_book_memo_caddy_upstream", script)
            self.assertIn(".spec.selector.app\\.kubernetes\\.io/name", script)
            self.assertIn(".spec.ports[0].targetPort", script)
            self.assertIn("BOOK_MEMO_UPSTREAM", script)
            self.assertIn("sudo -n k3s kubectl", script)

        # deploy-n100.sh shares one resolver across all K3s runtime services.
        self.assertIn('get "service/$service"', deploy)
        self.assertIn('get "endpoints/$service"', deploy)
        # windows-bootstrap.sh keeps its service-specific bootstrap resolvers.
        self.assertIn("get service/book-memo", bootstrap)
        self.assertIn("get endpoints/book-memo", bootstrap)

        deploy_books = _function_body(deploy, "resolve_book_memo_caddy_upstream")
        bootstrap_books = _function_body(bootstrap, "resolve_book_memo_caddy_upstream")
        self.assertIn(
            'require_k3s_service_endpoint book-memo book-memo-data 8003 "Book Memo"',
            deploy_books,
        )
        self.assertIn("get service/book-memo", bootstrap_books)
        self.assertIn("get endpoints/book-memo", bootstrap_books)
        self.assertIn("get pvc/book-memo-data", bootstrap_books)
        self.assertIn('"$service_port" == 8003', bootstrap_books)

        for script in (deploy, bootstrap):
            calls = list(re.finditer(
                r"(?m)^ {8}resolve_book_memo_caddy_upstream$", script,
            ))
            self.assertGreaterEqual(len(calls), 1)
            for call in calls:
                next_book_call = script.find("\n        resolve_book_memo_caddy_upstream", call.end())
                next_caddy_use = script.find("caddy", call.end())
                self.assertNotEqual(next_caddy_use, -1)
                self.assertTrue(
                    next_book_call == -1 or next_caddy_use < next_book_call,
                    "Books resolver must be called before each Caddy recreation path",
                )


if __name__ == "__main__":
    unittest.main()
