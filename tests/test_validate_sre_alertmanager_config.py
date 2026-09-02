import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "k8s" / "sre-telegram" / "alertmanager.yaml.tmpl"
VALIDATOR = ROOT / "infra" / "k8s" / "tools" / "validate-sre-alertmanager-config.py"


class ValidateSreAlertmanagerConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(TEMPLATE.is_file(), "Alertmanager fixed template must exist")
        self.assertTrue(VALIDATOR.is_file(), "Alertmanager validator must exist")

    def template_document(self):
        return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))

    def run_validator(self, config_text):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "operator-alertmanager.yaml"
            config_path.write_text(config_text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(config_path)],
                capture_output=True,
                text=True,
                check=False,
            )

    def assert_rejected(self, document):
        self.assert_rejected_text(yaml.safe_dump(document, sort_keys=False))

    def assert_rejected_text(self, config_text):
        marker = "operator-secret-marker-must-not-echo"
        result = self.run_validator(config_text)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "invalid_alertmanager_config\n")
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_accepts_only_noop_root_and_sre_labeled_relay_child(self):
        document = self.template_document()

        self.assertEqual(document["route"]["receiver"], "sre-telegram-noop")
        self.assertEqual(
            document["route"]["routes"],
            [
                {
                    "matchers": ['sre_telegram="true"'],
                    "receiver": "sre-telegram-relay",
                }
            ],
        )
        self.assertEqual(
            document["receivers"][0],
            {"name": "sre-telegram-noop"},
        )
        self.assertEqual(document["receivers"][1]["name"], "sre-telegram-relay")

        result = self.run_validator(TEMPLATE.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_rejects_root_relay_and_any_global_configuration(self):
        root_relay = self.template_document()
        root_relay["route"]["receiver"] = "sre-telegram-relay"
        empty_global = self.template_document()
        empty_global["global"] = {}
        nonempty_global = self.template_document()
        nonempty_global["global"] = {"resolve_timeout": "5m"}

        for document in (root_relay, empty_global, nonempty_global):
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_rejects_any_extra_route_or_receiver(self):
        extra_route = self.template_document()
        extra_route["route"]["routes"].append(
            {
                "matchers": ['sre_telegram="false"'],
                "receiver": "sre-telegram-relay",
            }
        )
        extra_receiver = self.template_document()
        extra_receiver["receivers"].append(
            {
                "name": "unexpected-receiver",
                "webhook_configs": [
                    {
                        "url": "http://sre-telegram-relay.monitoring.svc:8080/alertmanager",
                        "send_resolved": True,
                        "http_config": {
                            "authorization": {
                                "type": "Bearer",
                                "credentials_file": "/etc/alertmanager/secrets/"
                                "sre-telegram-relay-runtime/alertmanager_auth_token",
                            }
                        },
                    }
                ],
            }
        )

        for document in (extra_route, extra_receiver):
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_rejects_continue_or_nested_routes(self):
        continued = self.template_document()
        continued["route"]["routes"][0]["continue"] = True
        nested = self.template_document()
        nested["route"]["routes"][0]["routes"] = [
            {"receiver": "sre-telegram-relay"}
        ]

        for document in (continued, nested):
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_rejects_duplicate_route_key_hidden_by_later_valid_route(self):
        duplicate_route_config = """\
route:
  receiver: unexpected-receiver
  group_by: []
  repeat_interval: 5m
  routes:
    - matchers: ['sre_telegram="false"']
      receiver: unexpected-receiver
    - matchers: ['sre_telegram="true"']
      receiver: unexpected-receiver
route:
  receiver: sre-telegram-relay
  group_by: [alertname, namespace, pod, deployment, persistentvolumeclaim]
  repeat_interval: 4h
  routes:
    - matchers: ['sre_telegram="true"']
      receiver: sre-telegram-relay
receivers:
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
"""

        self.assert_rejected_text(duplicate_route_config)

    def test_rejects_duplicate_receivers_key_hidden_by_later_valid_receivers(self):
        duplicate_receivers_config = """\
route:
  receiver: sre-telegram-relay
  group_by: [alertname, namespace, pod, deployment, persistentvolumeclaim]
  repeat_interval: 4h
  routes:
    - matchers: ['sre_telegram="true"']
      receiver: sre-telegram-relay
receivers:
  - name: unexpected-receiver
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
receivers:
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
"""

        self.assert_rejected_text(duplicate_receivers_config)

    def test_rejects_wrong_matcher_receiver_url_or_credentials_file(self):
        wrong_matcher = self.template_document()
        wrong_matcher["route"]["routes"][0]["matchers"] = ['sre_telegram="false"']
        wrong_receiver = self.template_document()
        wrong_receiver["route"]["routes"][0]["receiver"] = "unexpected-receiver"
        wrong_url = self.template_document()
        wrong_url["receivers"][1]["webhook_configs"][0]["url"] = "http://unexpected.example/"
        wrong_credentials_file = self.template_document()
        wrong_credentials_file["receivers"][1]["webhook_configs"][0]["http_config"][
            "authorization"
        ]["credentials_file"] = "/tmp/operator-secret-marker-must-not-echo"
        empty_group_by = self.template_document()
        empty_group_by["route"]["group_by"] = []
        wrong_repeat_interval = self.template_document()
        wrong_repeat_interval["route"]["repeat_interval"] = "5m"
        unresolved_webhook = self.template_document()
        unresolved_webhook["receivers"][1]["webhook_configs"][0]["send_resolved"] = False

        for document in (
            wrong_matcher,
            wrong_receiver,
            wrong_url,
            wrong_credentials_file,
            empty_group_by,
            wrong_repeat_interval,
            unresolved_webhook,
        ):
            with self.subTest(document=document):
                self.assert_rejected(copy.deepcopy(document))

    def test_rejects_multiple_yaml_documents(self):
        multiple_documents = TEMPLATE.read_text(encoding="utf-8") + "---\nroute: {}\n"

        self.assert_rejected_text(multiple_documents)


if __name__ == "__main__":
    unittest.main()
