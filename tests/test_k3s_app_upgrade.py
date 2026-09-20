"""Contracts for the image-only K3s application upgrade operator."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "k8s" / "tools" / "k3s-app-upgrade.sh"
OLD = "docker.io/library/personal-server-crawler-worker@sha256:" + "a" * 64
TARGET = "docker.io/library/personal-server-crawler-worker@sha256:" + "b" * 64
PORTAL_OLD = "docker.io/library/personal-server-portal-web@sha256:" + "c" * 64
PORTAL_TARGET = "docker.io/library/personal-server-portal-web@sha256:" + "d" * 64


class K3sAppUpgradeTests(unittest.TestCase):
    """Run the operator against isolated command doubles and persisted fake state."""

    def run_operator(self, *arguments, app="crawler-worker", image=TARGET,
                     expected=OLD, scenario="ok"):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        base = Path(temporary_directory.name)
        command_directory = base / "bin"
        command_directory.mkdir()
        call_log = base / "calls.jsonl"
        deployment_path = base / "deployment.json"
        fixture_path = base / "fixture.json"

        current = PORTAL_OLD if app == "portal-web" else OLD
        target = PORTAL_TARGET if app == "portal-web" and image == TARGET else image
        if app == "portal-web" and expected == OLD:
            expected = PORTAL_OLD
        deployment = self.deployment_for(app, current)
        deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
        fixture_path.write_text(json.dumps({
            "app": app,
            "scenario": scenario,
            "target": target,
            "old": current,
            "external_calls": 0,
        }), encoding="utf-8")
        self.write_command_doubles(command_directory)

        result = subprocess.run(
            ["bash", str(SCRIPT), *arguments, "--app", app, "--image", target,
             *(["--expected-current-image", expected] if "--go" in arguments else [])],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "CALL_LOG": str(call_log),
                "UPGRADE_DEPLOYMENT": str(deployment_path),
                "UPGRADE_FIXTURE": str(fixture_path),
                "PATH": f"{command_directory}:{os.environ['PATH']}",
            },
        )
        calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()] if call_log.exists() else []
        final_deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        return result, calls, deployment, final_deployment, fixture

    @staticmethod
    def deployment_for(app, image):
        port = 8000 if app == "portal-web" else 8001
        pvc = "portal-web-state-dynamic" if app == "portal-web" else "crawler-worker-data"
        return {
            "metadata": {"name": app, "namespace": "personal-server"},
            "spec": {
                "replicas": 1,
                "template": {"spec": {
                    "containers": [{
                        "name": app,
                        "image": image,
                        "ports": [{"name": "http", "containerPort": port}],
                        "envFrom": [{"secretRef": {"name": app + "-runtime"}}],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}}],
                }},
            },
            "status": {"readyReplicas": 1, "availableReplicas": 1},
        }

    @staticmethod
    def write_command_doubles(directory):
        scripts = {
            "sudo": r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['sudo', *sys.argv[1:]]) + '\n')
args = sys.argv[1:]
if args and args[0] == '-n': args = args[1:]
sys.exit(subprocess.run(args).returncode)
''',
            "k3s": r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['k3s', *sys.argv[1:]]) + '\n')
sys.exit(subprocess.run(sys.argv[1:]).returncode)
''',
            "ctr": r'''#!/usr/bin/env python3
import json, os, pathlib, sys
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['ctr', *sys.argv[1:]]) + '\n')
fixture = json.loads(pathlib.Path(os.environ['UPGRADE_FIXTURE']).read_text())
if sys.argv[1:] != ['images', 'list']: sys.exit(64)
if fixture['scenario'] == 'missing_ctr': sys.exit(0)
platform = 'linux/arm64' if fixture['scenario'] == 'wrong_platform' else 'linux/amd64'
digest = fixture.get('ctr_digest', 'sha256:' + fixture['target'].split(':')[-1])
if fixture['scenario'] == 'ctr_digest_mismatch':
    digest = 'sha256:' + 'f' * 64
print(f"{fixture['target']} application/vnd.oci.image.manifest.v1+json {digest} 10MiB {platform} -")
''',
            "kubectl": r'''#!/usr/bin/env python3
import json, os, pathlib, sys
base = pathlib.Path(os.environ['UPGRADE_DEPLOYMENT'])
fixture_path = pathlib.Path(os.environ['UPGRADE_FIXTURE'])
fixture = json.loads(fixture_path.read_text())
args = sys.argv[1:]
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['kubectl', *args]) + '\n')
if args[:2] == ['-n', 'personal-server']: args = args[2:]
if args and args[0].startswith('--request-timeout='): args = args[1:]
deployment = json.loads(base.read_text())
app = fixture['app']
port = 8000 if app == 'portal-web' else 8001
if args[:2] == ['get', 'deployment']:
    print(json.dumps(deployment)); sys.exit(0)
if args[:2] == ['get', 'service']:
    service_type = {'nodeport_service': 'NodePort', 'loadbalancer_service': 'LoadBalancer'}.get(fixture['scenario'], 'ClusterIP')
    print(json.dumps({'metadata': {'name': app}, 'spec': {'type': service_type, 'clusterIP': '10.43.0.17', 'ports': [{'name': 'http', 'port': port, 'targetPort': 'http'}]}})); sys.exit(0)
if args[:2] == ['get', 'endpoints']:
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    suffix = 'target' if image == fixture['target'] else 'old'
    if fixture['scenario'] == 'target_stale_endpoint' and image == fixture['target']: suffix = 'old'
    print(json.dumps({'subsets': [{'addresses': [{'ip': '10.42.0.21', 'targetRef': {'kind': 'Pod', 'name': app + '-' + suffix}}], 'ports': [{'port': port}]}]})); sys.exit(0)
if args[:2] == ['get', 'pods']:
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    suffix = 'target' if image == fixture['target'] else 'old'
    pod_image = image
    if fixture['scenario'] == 'target_stale_pod' and image == fixture['target']:
        suffix, pod_image = 'old', fixture['old']
    print(json.dumps({'items': [{'metadata': {'name': app + '-' + suffix}, 'status': {'phase': 'Running', 'containerStatuses': [{'name': app, 'ready': True, 'image': pod_image}]}}]})); sys.exit(0)
if args[:2] == ['rollout', 'status']:
    if args.count('--timeout=120s') != 1: sys.exit(67)
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    if fixture['scenario'] == 'target_rollout_fail' and image == fixture['target']: sys.exit(1)
    if fixture['scenario'] == 'rollback_rollout_fail' and image == fixture['old']: sys.exit(1)
    sys.exit(0)
if args[:2] == ['patch', 'deployment']:
    if '--type=json' not in args or '--patch' not in args: sys.exit(64)
    payload = json.loads(args[args.index('--patch') + 1])
    image_path = '/spec/template/spec/containers/0/image'
    if len(payload) != 2 or payload[0].get('op') != 'test' or payload[1].get('op') != 'replace' or payload[0].get('path') != image_path or payload[1].get('path') != image_path: sys.exit(65)
    current = deployment['spec']['template']['spec']['containers'][0]['image']
    if payload[0].get('value') != current: sys.exit(66)
    deployment['spec']['template']['spec']['containers'][0]['image'] = payload[1]['value']
    base.write_text(json.dumps(deployment))
    sys.exit(0)
if args[:1] == ['exec'] and len(args) > 1 and args[1].startswith('pod/' + app + '-'):
    sys.exit(0)
sys.exit(64)
''',
            "curl": r'''#!/usr/bin/env python3
import json, os, pathlib, sys
fixture_path = pathlib.Path(os.environ['UPGRADE_FIXTURE'])
fixture = json.loads(fixture_path.read_text())
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['curl', *sys.argv[1:]]) + '\n')
url = sys.argv[-1]
if url == 'https://len.pe.kr/health':
    fixture['external_calls'] += 1
    fixture_path.write_text(json.dumps(fixture))
    deployment = json.loads(pathlib.Path(os.environ['UPGRADE_DEPLOYMENT']).read_text())
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    if fixture['scenario'] == 'portal_non_200' and image == fixture['target'] and fixture['external_calls'] == 2:
        print('503'); sys.exit(0)
    if fixture['scenario'] == 'target_health_fail' and image == fixture['target']:
        sys.exit(22)
    print('200'); sys.exit(0)
if fixture['scenario'] in ('target_health_fail', 'rollback_rollout_fail'):
    deployment = json.loads(pathlib.Path(os.environ['UPGRADE_DEPLOYMENT']).read_text())
    if deployment['spec']['template']['spec']['containers'][0]['image'] == fixture['target']: sys.exit(22)
if fixture['scenario'] == 'target_curl_blackhole':
    deployment = json.loads(pathlib.Path(os.environ['UPGRADE_DEPLOYMENT']).read_text())
    if deployment['spec']['template']['spec']['containers'][0]['image'] == fixture['target']:
        if '--connect-timeout' not in sys.argv or '--max-time' not in sys.argv: sys.exit(64)
        sys.exit(28)
if fixture['scenario'] == 'check_health_fail': sys.exit(22)
sys.exit(0)
''',
            "sleep": r'''#!/usr/bin/env python3
import json, os, pathlib, sys
with pathlib.Path(os.environ['CALL_LOG']).open('a') as output: output.write(json.dumps(['sleep', *sys.argv[1:]]) + '\n')
''',
        }
        for name, content in scripts.items():
            path = directory / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)

    @staticmethod
    def kubectl_calls(calls, verb=None):
        selected = [entry[1:] for entry in calls if entry[0] == "kubectl"]
        return [entry for entry in selected if verb is None or verb in entry]

    def test_check_is_read_only_after_full_preflight(self):
        """Removing the check-mode guard must produce a patch or rollout and fail this test."""
        result, calls, before, after, _ = self.run_operator("--check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("k3s_app_upgrade=PASS", result.stdout)
        self.assertEqual(after, before)
        self.assertFalse(any("patch" in call or "rollout" in call for call in self.kubectl_calls(calls)))
        self.assertTrue(any(entry[0] == "ctr" for entry in calls))

    def test_check_accepts_a_nodeport_service_with_a_ready_http_endpoint(self):
        """Restricting a healthy application Service to ClusterIP rejects the live Portal topology."""
        result, calls, before, after, _ = self.run_operator("--check", app="portal-web", scenario="nodeport_service")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("k3s_app_upgrade=PASS", result.stdout)
        self.assertEqual(after, before)
        self.assertFalse(any("patch" in call or "rollout" in call for call in self.kubectl_calls(calls)))

    def test_check_rejects_a_loadbalancer_service(self):
        """Allowing every Service type would weaken the explicit internal routing contract."""
        result, calls, before, after, _ = self.run_operator("--check", scenario="loadbalancer_service")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)
        self.assertFalse(any("patch" in call or "rollout" in call for call in self.kubectl_calls(calls)))

    def test_check_health_failure_is_read_only(self):
        """Skipping check-mode health would report a failing active Crawler as safe."""
        result, calls, before, after, _ = self.run_operator("--check", scenario="check_health_fail")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)
        self.assertTrue(any(call[0] == "curl" for call in calls))
        self.assertFalse(any("patch" in call or "rollout" in call for call in self.kubectl_calls(calls)))

    def test_rejects_mutable_and_foreign_images_without_cluster_access(self):
        """Removing canonical digest validation would allow an unsafe target before preflight."""
        for image in ("personal-server-crawler-worker:latest", "docker.io/library/personal-server-portal-web@sha256:" + "b" * 64):
            with self.subTest(image=image):
                result, calls, _, _, _ = self.run_operator("--check", image=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("k3s_app_upgrade=FAIL", result.stderr)
                self.assertEqual(calls, [])

    def test_expected_current_image_mismatch_stops_before_patch(self):
        """Dropping compare-and-stop could overwrite a concurrent Deployment image change."""
        wrong = "docker.io/library/personal-server-crawler-worker@sha256:" + "e" * 64
        result, calls, before, after, _ = self.run_operator("--go", expected=wrong)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected current image mismatch", result.stderr)
        self.assertEqual(after, before)
        self.assertFalse(any("patch" in call for call in self.kubectl_calls(calls)))

    def test_rejects_ctr_manifest_digest_mismatch_before_kubernetes_access(self):
        """Ignoring ctr's manifest digest could patch a target different from --image."""
        result, calls, before, after, _ = self.run_operator("--go", scenario="ctr_digest_mismatch")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)
        self.assertTrue(any(call[0] == "ctr" for call in calls))
        self.assertFalse(any(call[0] == "kubectl" for call in calls))

    def test_curl_blackhole_rolls_back_once_with_bounded_requests(self):
        """Removing request limits could leave a target rollout blocked forever without rollback."""
        result, calls, before, after, _ = self.run_operator("--go", scenario="target_curl_blackhole")
        patches = [call for call in self.kubectl_calls(calls) if "patch" in call]
        curl_calls = [call for call in calls if call[0] == "curl"]
        kubectl_calls = self.kubectl_calls(calls)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback=PASS", result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(len(patches), 2)
        self.assertTrue(curl_calls)
        self.assertTrue(all("--connect-timeout" in call and "--max-time" in call for call in curl_calls))
        self.assertTrue(kubectl_calls)
        self.assertTrue(all(any(arg.startswith("--request-timeout=") for arg in call) for call in kubectl_calls))

    def test_rollout_status_requires_one_bounded_timeout(self):
        """Removing or duplicating rollout's timeout could keep a target image active indefinitely."""
        result, calls, _, _, _ = self.run_operator("--go")
        rollouts = [call for call in self.kubectl_calls(calls) if "rollout" in call and "status" in call]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(rollouts), 1)
        self.assertEqual(rollouts[0].count("--timeout=120s"), 1)

    def test_target_failure_rolls_back_only_once_with_test_replace_image_patch(self):
        """Removing rollback or retrying it would leave a failed target or add a third patch."""
        result, calls, before, after, _ = self.run_operator("--go", scenario="target_health_fail")
        patches = [call for call in self.kubectl_calls(calls) if "patch" in call]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback=PASS", result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(len(patches), 2)
        payloads = [json.loads(call[call.index("--patch") + 1]) for call in patches]
        self.assertEqual(payloads[0][0]["value"], OLD)
        self.assertEqual(payloads[0][1]["value"], TARGET)
        self.assertEqual(payloads[1][0]["value"], TARGET)
        self.assertEqual(payloads[1][1]["value"], OLD)
        for payload in payloads:
            self.assertEqual([item["op"] for item in payload], ["test", "replace"])
            self.assertEqual({item["path"] for item in payload}, {"/spec/template/spec/containers/0/image"})

    def test_target_health_rejects_stale_ready_pod_or_endpoint(self):
        """Trusting an old ready Pod or Endpoint can falsely pass an image rollout."""
        for scenario in ("target_stale_pod", "target_stale_endpoint"):
            with self.subTest(scenario=scenario):
                result, calls, before, after, _ = self.run_operator("--go", scenario=scenario)
                patches = [call for call in self.kubectl_calls(calls) if "patch" in call]

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rollback=PASS", result.stderr)
                self.assertEqual(after, before)
                self.assertEqual(len(patches), 2)

    def test_rollback_failure_does_not_retry(self):
        """A rollback retry after a failed rollback rollout could amplify an outage."""
        result, calls, _, _, _ = self.run_operator("--go", scenario="rollback_rollout_fail")
        patches = [call for call in self.kubectl_calls(calls) if "patch" in call]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback=FAIL", result.stderr)
        self.assertEqual(len(patches), 2)

    def test_portal_requires_three_successful_external_health_probes(self):
        """Accepting fewer probes would miss an intermittent Portal external health failure."""
        result, calls, _, _, fixture = self.run_operator("--go", app="portal-web", scenario="ok")
        external = [call for call in calls if call[0] == "curl" and call[-1] == "https://len.pe.kr/health"]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture["external_calls"], 3)
        self.assertEqual(len(external), 3)
        self.assertEqual([call for call in calls if call[0] == "sleep"], [["sleep", "10"], ["sleep", "10"]])

    def test_portal_check_uses_the_ready_endpoint_without_requiring_a_pod_curl_binary(self):
        """Requiring curl inside the Portal image rejects a healthy runtime that exposes its ready Endpoint."""
        result, calls, before, after, _ = self.run_operator("--check", app="portal-web")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(after, before)
        self.assertFalse(any("exec" in call for call in self.kubectl_calls(calls)))
        endpoint_calls = [call for call in calls if call[0] == "curl" and call[-1] == "http://10.42.0.21:8000/health"]
        self.assertEqual(len(endpoint_calls), 1)

    def test_portal_non_200_probe_is_not_a_successful_upgrade(self):
        """Ignoring a non-200 Portal probe would accept an externally unhealthy target."""
        result, calls, _, _, _ = self.run_operator("--go", app="portal-web", scenario="portal_non_200")
        patches = [call for call in self.kubectl_calls(calls) if "patch" in call]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback=PASS", result.stderr)
        self.assertEqual(len(patches), 2)

    def test_crawler_patch_preserves_non_image_deployment_spec(self):
        """Replacing a Deployment rather than its image field would alter PVC or Secret references."""
        result, calls, before, after, _ = self.run_operator("--go")

        self.assertEqual(result.returncode, 0, result.stderr)
        expected = copy.deepcopy(before)
        expected["spec"]["template"]["spec"]["containers"][0]["image"] = TARGET
        self.assertEqual(after, expected)
        patch = next(call for call in self.kubectl_calls(calls) if "patch" in call)
        self.assertIn("--type=json", patch)


if __name__ == "__main__":
    unittest.main()
