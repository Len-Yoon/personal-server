import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "infra" / "k8s" / "tools"
FIXED_ALERTMANAGER_TEMPLATE = (
    ROOT / "infra" / "k8s" / "sre-telegram" / "alertmanager.yaml.tmpl"
)

BYPASS_ALERTMANAGER_CONFIG = """\
route:
  receiver: sre-telegram-noop
  group_by:
    - alertname
  repeat_interval: 4h
  routes:
    - matchers:
        - 'sre_telegram="true"'
      receiver: sre-telegram-noop
    - matchers:
        - 'sre_telegram="false"'
      receiver: sre-telegram-relay
receivers:
  - name: sre-telegram-noop
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
"""

NESTED_RECEIVER_OVERRIDE_ALERTMANAGER_CONFIG = """\
route:
  receiver: sre-telegram-noop
  group_by:
    - alertname
  repeat_interval: 4h
  routes:
    - matchers:
        - 'sre_telegram="true"'
      receiver: sre-telegram-relay
      routes:
        - receiver: sre-telegram-noop
receivers:
  - name: sre-telegram-noop
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
"""

CONTINUE_PARALLEL_NONRELAY_ALERTMANAGER_CONFIG = """\
route:
  receiver: sre-telegram-noop
  group_by:
    - alertname
  repeat_interval: 4h
  routes:
    - matchers:
        - 'sre_telegram="true"'
      receiver: sre-telegram-relay
      continue: true
    - receiver: sre-telegram-noop
receivers:
  - name: sre-telegram-noop
  - name: sre-telegram-relay
    webhook_configs:
      - url: http://sre-telegram-relay.monitoring.svc:8080/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/sre-telegram-relay-runtime/alertmanager_auth_token
"""

EXTRA_ROUTE_ALERTMANAGER_CONFIG = """\
route:
  receiver: sre-telegram-relay
  group_by: [alertname, namespace, pod, deployment, persistentvolumeclaim]
  repeat_interval: 4h
  routes:
    - matchers: ['sre_telegram="true"']
      receiver: sre-telegram-relay
    - matchers: ['sre_telegram="false"']
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


def read_tool(name: str) -> str:
    path = TOOLS / name
    if not path.is_file():
        raise AssertionError(f"required tool is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


class SreTelegramToolContractTest(unittest.TestCase):
    def run_tool(
        self, name, *args, stubs=None, env_overrides=None, files=None, file_modes=None
    ):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calls = directory_path / "calls"
            operator_config = directory_path / "operator-alertmanager.yaml"
            operator_config.write_text(
                FIXED_ALERTMANAGER_TEMPLATE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            operator_config.chmod(0o600)
            for relative_path, body in (files or {}).items():
                path = directory_path / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                path.chmod((file_modes or {}).get(relative_path, 0o600))
            for command, body in (stubs or {}).items():
                path = directory_path / command
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "CALLS": str(calls),
                "SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": str(operator_config),
            }
            env.update(
                {
                    key: value.format(tmp=directory) if isinstance(value, str) else value
                    for key, value in (env_overrides or {}).items()
                }
            )
            result = subprocess.run(
                ["bash", str(TOOLS / name), *args],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return result, recorded

    def test_preflight_invokes_only_read_only_k3s_and_helm_commands(self):
        script = read_tool("sre-telegram-preflight.sh")

        self.assertIn("kubectl get nodes --no-headers", script)
        self.assertIn('helm status "$RELEASE" --namespace "$NAMESPACE"', script)
        self.assertNotRegex(script, r"kubectl (apply|create|delete|patch|replace|edit)")
        self.assertNotRegex(script, r"helm (install|upgrade|uninstall|rollback)")

    def test_render_never_imports_image_or_applies_resources(self):
        script = read_tool("sre-telegram-install.sh")
        render = script.split("render()", 1)[1].split("require_secret_contract", 1)[0]

        self.assertIn("helm template", render)
        self.assertIn("--dry-run=client", render)
        self.assertNotIn("ctr -n k8s.io images import", render)
        self.assertNotIn("docker build", render)
        self.assertNotIn("--apply", render)

    def test_apply_requires_all_secret_keys_by_name_without_reading_values(self):
        script = read_tool("sre-telegram-install.sh")

        for key in ("telegram_bot_token", "allowed_chat_id", "alertmanager_auth_token", "alertmanager.yaml"):
            self.assertIn(key, script)
        self.assertRegex(script, r"kubectl(?: -n [^ ]+)? describe secret")
        self.assertNotRegex(script, r"secret[^\n]*(jsonpath|\.data|-o +yaml|-o +json)")
        self.assertIn('mode="${1:---render}"', script)
        self.assertIn("--apply)", script)

    def test_verify_never_reads_or_prints_secret_data(self):
        script = read_tool("sre-telegram-verify.sh")

        self.assertIn("port-forward --address 127.0.0.1", script)
        self.assertIn("/healthz", script)
        self.assertIn("/api/v1/targets", script)
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+get\s+secrets?")
        self.assertNotRegex(script, r"kubectl(?:\s+-\S+\s+\S+)*\s+describe\s+secrets?")
        self.assertNotIn(".data", script)

    def test_preflight_runs_amtool_then_fixed_template_validator_without_echoing_config(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nprintf 'amtool %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
                "python3": f"#!/bin/sh\nprintf 'validator %s\\n' \"$*\" >> \"$CALLS\"\nexec {shlex.quote(sys.executable)} \"$@\"\n",
            },
            files={"effective-alertmanager.yaml": FIXED_ALERTMANAGER_TEMPLATE.read_text(encoding="utf-8")},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/effective-alertmanager.yaml"},
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=PASS"))
        self.assertIn("amtool check-config", calls)
        self.assertIn("validator", calls)
        self.assertLess(calls.index("amtool check-config"), calls.index("validator"))
        self.assertNotIn("route:", result.stdout)
        self.assertNotIn("route:", calls)
        self.assertNotIn("get secret", calls)

    def test_preflight_fails_closed_when_operator_config_file_is_not_0600(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nprintf 'amtool %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            files={"non-private-alertmanager.yaml": FIXED_ALERTMANAGER_TEMPLATE.read_text(encoding="utf-8")},
            file_modes={"non-private-alertmanager.yaml": 0o640},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/non-private-alertmanager.yaml"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertNotIn("amtool", calls)

    def test_preflight_reads_linux_mode_when_gnu_stat_accepts_macos_flag(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "printf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "stat": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  -f) printf 'filesystem\\n'; exit 0;;\n"
                "  -c) printf '600\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "uname": "#!/bin/sh\nprintf 'Linux\\n'\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=PASS"))
        self.assertNotIn("filesystem", result.stdout)
        self.assertNotIn("filesystem", calls)

    def test_preflight_fails_when_validator_rejects_extra_route(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nprintf 'amtool %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            files={"extra-route.yaml": EXTRA_ROUTE_ALERTMANAGER_CONFIG},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/extra-route.yaml"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("amtool check-config", calls)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_fails_closed_when_amtool_rejects_operator_local_config(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 1\n",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_rejects_amtool_valid_config_when_sre_route_does_not_select_relay(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
            files={"bypass-alertmanager.yaml": BYPASS_ALERTMANAGER_CONFIG},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/bypass-alertmanager.yaml"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_rejects_nested_matcherless_child_that_overrides_relay_receiver(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
            files={"nested-receiver-override.yaml": NESTED_RECEIVER_OVERRIDE_ALERTMANAGER_CONFIG},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/nested-receiver-override.yaml"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_rejects_continue_path_that_reaches_nonrelay_receiver(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\ncase \"$1\" in status) printf '{\\\"info\\\":{\\\"status\\\":\\\"deployed\\\"}}\\n'; exit 0;; esac\nexit 1\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
            files={"continue-parallel-nonrelay.yaml": CONTINUE_PARALLEL_NONRELAY_ALERTMANAGER_CONFIG},
            env_overrides={"SRE_TELEGRAM_ALERTMANAGER_CONFIG_FILE": "{tmp}/continue-parallel-nonrelay.yaml"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check=alertmanager_effective_config status=FAIL", result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_verify_checks_namespaced_denials_in_monitoring_and_personal_server(self):
        result, calls = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) printf 'no\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *18080*) printf 'ok\\n';;\n"
                "  *) printf '{\"status\":\"success\",\"data\":{\"activeTargets\":[{\"health\":\"up\"}]}}\\n';;\n"
                "esac\n"
                "exit 0\n",
            },
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--namespace monitoring", calls)
        self.assertIn("--namespace personal-server", calls)

    def test_secret_guidance_requires_0600_file_until_install_returns_then_removal(self):
        result = subprocess.run(
            ["bash", str(TOOLS / "sre-telegram-secret-template.sh")],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("alertmanager.yaml.tmpl", result.stdout)
        self.assertIn("chmod 600", result.stdout)
        self.assertIn("keep", result.stdout.lower())
        self.assertIn("install", result.stdout.lower())
        self.assertIn("returns", result.stdout.lower())
        self.assertIn("do not print config or secret values", result.stdout.lower())
        self.assertNotRegex(result.stdout, r"kubectl +(create|apply|patch|replace)")
        self.assertNotIn("alertmanager_auth_token=", result.stdout)

    def test_readme_keeps_bearer_out_of_temporary_alertmanager_config(self):
        readme = (ROOT / "infra" / "k8s" / "README.md").read_text(encoding="utf-8")

        self.assertIn("runtime Secret 키 `alertmanager_auth_token`에만 입력", readme)
        self.assertIn(
            "임시 Alertmanager 설정 파일에는 `credentials_file` 경로만 유지하며 bearer 값은 포함하지 않는다",
            readme,
        )
        self.assertNotIn("bearer 값은 로컬 편집기로만 입력", readme)

    def test_preflight_rejects_a_non_deployed_release_even_when_helm_status_returns_zero(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"failed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=FAIL"))

    def test_preflight_rejects_zero_byte_secret_keys_without_reading_values(self):
        result, _ = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 0 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("telegram_bot_token=", result.stdout)

    def test_preflight_uses_existing_prometheus_service_and_label_discovery(self):
        result, calls = self.run_tool(
            "sre-telegram-preflight.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'get nodes --no-headers'*) printf 'n100 Ready\\n'; exit 0;;\n"
                "  *'get deployment personal-server-monitoring-grafana'*) printf '1\\n'; exit 0;;\n"
                "  *'get statefulset -l'*) printf '1\\n'; exit 0;;\n"
                "  *'get service personal-server-monitoring-prometheus'*) exit 0;;\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'ctr version'*) exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\"}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "docker": "#!/bin/sh\nexit 0\n",
                "amtool": "#!/bin/sh\nexit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_preflight=PASS"))
        self.assertIn("get statefulset -l app.kubernetes.io/name=prometheus", calls)
        self.assertIn("get service personal-server-monitoring-prometheus", calls)

    def test_install_stops_before_helm_when_image_save_fails(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'sre_telegram_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'describe secret sre-telegram-relay-runtime'*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\n'; exit 0;;\n"
                "  *'describe secret sre-telegram-alertmanager-config'*) printf 'alertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *images*import*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  build) exit 0;;\n"
                "  save) exit 7;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertNotIn("helm upgrade", calls)

    def test_install_stops_before_resource_create_when_imported_image_has_no_digest(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x <none> 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertNotIn("create -f", calls)
        self.assertNotIn("helm upgrade", calls)

    def test_install_applies_relay_before_atomic_helm_upgrade_and_rolls_back_namespaced_resources(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'sre_telegram_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  build) exit 0;;\n"
                "  save) printf 'image-stream'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; exit 0;;\n"
                "  get) case \"$2\" in values) printf 'replicaCount: 1\\n';; manifest) printf 'apiVersion: v1\\nkind: ConfigMap\\n';; esac; exit 0;;\n"
                "  upgrade) printf 'upgrade failed\\n' >&2; exit 1;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("monitoring-ConfigMap-sre-telegram-relay-state.yaml", calls)
        self.assertIn("helm upgrade", calls)
        base_index = calls.index("monitoring-ConfigMap-sre-telegram-relay-state.yaml")
        upgrade_index = calls.index("helm upgrade")
        self.assertLess(base_index, upgrade_index)
        self.assertIn("--atomic", calls)
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_install_rolls_back_only_personal_server_binding_when_monitoring_binding_preexists(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'monitoring-RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'Error from server (AlreadyExists): rolebindings.rbac.authorization.k8s.io \\\"sre-telegram-relay-workload-reader\\\" already exists\\n'; exit 1;;\n"
                "  *'personal-server-RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *'monitoring-Deployment-sre-telegram-relay.yaml'*) printf 'Error from server (InternalError): later resource create failed\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)
        self.assertNotIn("-n monitoring delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_install_handles_cluster_role_binding_as_cluster_scoped_during_create_and_rollback(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'ClusterRoleBinding-sre-telegram-relay-node-reader.yaml'*) printf 'clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader created\\n'; exit 0;;\n"
                "  *'monitoring-Deployment-sre-telegram-relay.yaml'*) printf 'Error from server (InternalError): later resource create failed\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        cluster_binding = "clusterrolebinding.rbac.authorization.k8s.io/sre-telegram-relay-node-reader"
        cluster_manifest = "ClusterRoleBinding-sre-telegram-relay-node-reader.yaml"
        cluster_create_calls = [
            line for line in calls.splitlines() if cluster_manifest in line and " create -f " in line
        ]
        self.assertTrue(cluster_create_calls)
        self.assertTrue(all(" kubectl create -f " in line for line in cluster_create_calls))
        self.assertIn(f"sudo k3s kubectl delete {cluster_binding} --ignore-not-found", calls)
        self.assertNotIn(f"-n monitoring delete {cluster_binding}", calls)

    def test_install_refuses_preexisting_relay_resources_before_helm_upgrade(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'monitoring-ConfigMap-sre-telegram-relay-state.yaml'*) printf 'configmap/sre-telegram-relay-state already exists\\n'; exit 1;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\nexit 0\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("create -f", calls)
        self.assertNotIn("helm upgrade", calls)

    def test_install_signal_runs_cleanup_and_helm_rollback(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'RoleBinding-sre-telegram-relay-workload-reader.yaml'*) printf 'rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\n"
                "case \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\nprintf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status) printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; exit 0;;\n"
                "  get) case \"$2\" in values) printf 'replicaCount: 1\\n';; manifest) printf 'apiVersion: v1\\nkind: ConfigMap\\n';; esac; exit 0;;\n"
                "  upgrade) kill -TERM \"$PPID\"; sleep 1; exit 0;;\n"
                "  rollback) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )
        self.assertEqual(result.returncode, 130)
        self.assertIn("helm rollback personal-server-monitoring 2 --namespace monitoring", calls)
        self.assertIn("-n personal-server delete rolebinding.rbac.authorization.k8s.io/sre-telegram-relay-workload-reader", calls)

    def test_failed_upgrade_rolls_back_prior_revision_and_requires_deployed_status(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nprintf 'sre_telegram_preflight=PASS\\n'\nexit 0\n",
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *'create -f'*) printf 'configmap/sre-telegram-relay-state created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\ncase \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status)\n"
                "    count=$(grep -c '^helm status' \"$CALLS\" 2>/dev/null || true)\n"
                "    if [ \"$count\" -lt 1 ]; then printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; else printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"3\"}}\\n'; fi\n"
                "    printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "    exit 0;;\n"
                "  get)\n"
                "    printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "    case \"$2\" in values) printf 'replicaCount: 1\\n';; manifest) printf 'apiVersion: v1\\nkind: ConfigMap\\n';; esac\n"
                "    exit 0;;\n"
                "  upgrade) printf 'helm %s\\n' \"$*\" >> \"$CALLS\"; exit 1;;\n"
                "  rollback) printf 'helm %s\\n' \"$*\" >> \"$CALLS\"; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertIn("helm rollback personal-server-monitoring 2 --namespace monitoring", calls)
        self.assertIn(
            "helm status personal-server-monitoring --namespace monitoring --output json",
            calls,
        )

    def test_install_never_snapshots_helm_values_or_manifest(self):
        result, calls = self.run_tool(
            "sre-telegram-install.sh",
            "--apply",
            stubs={
                "preflight": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *describe*) printf 'telegram_bot_token: 3 bytes\\nallowed_chat_id: 2 bytes\\nalertmanager_auth_token: 3 bytes\\nalertmanager.yaml: 4 bytes\\n'; exit 0;;\n"
                "  *'apply --dry-run=client'*) exit 0;;\n"
                "  *'images list'*) printf 'REF TYPE DIGEST SIZE PLATFORMS LABELS\\npersonal-server-sre-telegram-relay:latest x sha256:abc 1MB linux/amd64 -\\n'; exit 0;;\n"
                "  *'create -f'*) printf 'configmap/sre-telegram-relay-state created\\n'; exit 0;;\n"
                "  *delete*) exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
                "docker": "#!/bin/sh\ncase \"$1\" in build) exit 0;; save) printf image-stream; exit 0;; esac\n",
                "helm": "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  template) exit 0;;\n"
                "  status)\n"
                "    count=$(grep -c '^helm status' \"$CALLS\" 2>/dev/null || true)\n"
                "    if [ \"$count\" -lt 1 ]; then printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"2\"}}\\n'; else printf '{\"info\":{\"status\":\"deployed\",\"revision\":\"4\"}}\\n'; fi\n"
                "    printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "    exit 0;;\n"
                "  get)\n"
                "    count=$(grep -c \"^helm get $2\" \"$CALLS\" 2>/dev/null || true)\n"
                "    printf 'helm %s\\n' \"$*\" >> \"$CALLS\"\n"
                "    if [ \"$count\" -lt 1 ]; then printf 'pre-upgrade-%s\\n' \"$2\"; else printf 'drifted-%s\\n' \"$2\"; fi\n"
                "    exit 0;;\n"
                "  upgrade) printf 'helm %s\\n' \"$*\" >> \"$CALLS\"; exit 1;;\n"
                "  rollback) printf 'helm %s\\n' \"$*\" >> \"$CALLS\"; exit 0;;\n"
                "  *) exit 0;;\n"
                "esac\n",
            },
            env_overrides={"SRE_TELEGRAM_PREFLIGHT_SCRIPT": "preflight"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_install=FAIL"))
        self.assertNotIn("helm get values", calls)
        self.assertNotIn("helm get manifest", calls)

    def test_verify_fails_closed_when_rbac_can_i_errors(self):
        result, _ = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) exit 1;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\nprintf 'ok\\n'\nexit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))

    def test_verify_rejects_any_down_active_prometheus_target(self):
        result, _ = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) printf 'no\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *18080*) printf 'ok\\n';;\n"
                "  *) printf '{\"status\":\"success\",\"data\":{\"activeTargets\":[{\"health\":\"up\"},{\"health\":\"down\"}]}}\\n';;\n"
                "esac\n"
                "exit 0\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))

    def test_verify_rejects_external_service_exposure_drift_and_uses_existing_prometheus_service(self):
        result, calls = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"externalIPs\":[\"10.0.0.5\"],\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *) exit 1;;\n"
                "esac\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=FAIL"))
        self.assertNotIn("kube-prometheus-prometheus", calls)

    def test_verify_passes_only_when_all_rbac_denials_and_targets_are_verified(self):
        result, calls = self.run_tool(
            "sre-telegram-verify.sh",
            stubs={
                "sudo": "#!/bin/sh\nprintf 'sudo %s\\n' \"$*\" >> \"$CALLS\"\n"
                "case \"$*\" in\n"
                "  *'rollout status deployment/sre-telegram-relay'*) exit 0;;\n"
                "  *'get service sre-telegram-relay -o json'*) printf '{\"spec\":{\"type\":\"ClusterIP\",\"ports\":[{\"port\":8080}]}}\\n'; exit 0;;\n"
                "  *'get prometheusrule sre-telegram-k3s-alerts'*) exit 0;;\n"
                "  *'auth can-i'*) printf 'no\\n'; exit 0;;\n"
                "  *'port-forward'*) sleep 30;;\n"
                "  *) exit 1;;\n"
                "esac\n",
                "curl": "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *18080*) printf 'ok\\n';;\n"
                "  *) printf '{\"status\":\"success\",\"data\":{\"activeTargets\":[{\"health\":\"up\"},{\"health\":\"up\"}]}}\\n';;\n"
                "esac\n"
                "exit 0\n",
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.rstrip().endswith("sre_telegram_verify=PASS"))
        self.assertIn("service/personal-server-monitoring-prometheus", calls)
        self.assertGreaterEqual(calls.count("auth can-i"), 12)


if __name__ == "__main__":
    unittest.main()
