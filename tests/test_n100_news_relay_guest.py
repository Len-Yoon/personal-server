import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/k8s/tools/n100_news_relay_guest.py"
SPEC = importlib.util.spec_from_file_location("n100_news_relay_guest", SCRIPT)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


class NewsRelayPolicyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "guest": "192.0.2.10", "relay": "192.0.2.1", "interface": "ha-data",
            "boot_sha256": "0" * 64,
            "providers": {"kr.investing.com": "1.1.1.1", "news.google.com": "8.8.8.8",
                          "api.telegram.org": "9.9.9.9"},
        }

    def test_only_known_provider_endpoints_from_broker_uid(self):
        hosts, routes, rules = relay.policy(self.config)
        self.assertEqual(routes, [("1.1.1.1", 46191), ("8.8.8.8", 46192), ("9.9.9.9", 46193)])
        self.assertEqual(rules.count("meta skuid 10002 ip daddr"), 3)
        self.assertIn("meta skuid != 10002 reject", rules)
        self.assertEqual(hosts.count(b".com") + hosts.count(b".org"), 3)

    def test_unexpected_provider_and_local_or_shared_provider_address_rejected(self):
        self.config["providers"]["api.telegram.org"] = "127.0.0.1"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["providers"]["api.telegram.org"] = "8.8.8.8"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["providers"]["other.example"] = "8.8.4.4"
        with self.assertRaises(ValueError):
            relay.policy(self.config)

    def test_invalid_link_and_shell_metacharacters_rejected(self):
        self.config["interface"] = "ha-data; ip"
        with self.assertRaises(ValueError):
            relay.policy(self.config)
        self.config["interface"] = "ha-data"
        self.config["relay"] = self.config["guest"]
        with self.assertRaises(ValueError):
            relay.policy(self.config)


if __name__ == "__main__":
    unittest.main()
