"""Behavioral checks for the existing SRE relay image update path."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/k8s/tools/sre-telegram-update.sh"
DIGEST_IMAGE = "docker.io/library/personal-server-sre-telegram-relay@sha256:" + "a" * 64
OLD_IMAGE = "personal-server-sre-telegram-relay:latest"


SUDO_STUB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

base = Path(os.environ["RELAY_TEST_DIR"])
state_file = base / "state.json"
calls_file = base / "calls.jsonl"
args = sys.argv[1:]
with calls_file.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] != ["-n", "k3s"]:
    sys.exit(91)
args = args[2:]
state = json.loads(state_file.read_text(encoding="utf-8"))
if args[:4] == ["ctr", "-n", "k8s.io", "images"] and args[4:] == ["list", "-q"]:
    print(os.environ["RELAY_TEST_IMAGE"])
    print("docker.io/library/" + state["old_image"] if os.environ.get("RELAY_TEST_CANONICAL_OLD") == "1" else state["old_image"])
elif args[:4] == ["kubectl", "-n", "monitoring", "get"] and args[4:6] == ["deployment", "sre-telegram-relay"]:
    print(json.dumps({
        "metadata": {"uid": "relay-uid", "resourceVersion": state["resource_version"]},
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "template": {"spec": {
            "containers": [{"name": "relay", "image": state["image"], "imagePullPolicy": "Never"}]
        }}},
        "status": {"availableReplicas": state["ready"]},
    }))
elif args[:4] == ["kubectl", "-n", "monitoring", "get"] and args[4] == "pods":
    image_id = "containerd://sha256:" + ("b" if state["image"] == state["old_image"] else "c") * 64
    if state["image"] == state["old_image"] and state.get("resource_version") != "8" and os.environ.get("RELAY_TEST_WRONG_ROLLBACK_ID") == "1":
        image_id = "containerd://sha256:" + "d" * 64
    print(json.dumps({"items": [{"spec": {"containers": [{"name": "relay", "image": state["image"]}]},
        "status": {"containerStatuses": [{"name": "relay", "ready": True,
            "imageID": image_id}]}}]}))
elif args[:4] == ["kubectl", "-n", "monitoring", "patch"] and args[4:6] == ["deployment", "sre-telegram-relay"]:
    patch = json.loads(args[args.index("-p") + 1])
    if os.environ.get("RELAY_TEST_DRIFT_BEFORE_PATCH") == "1" and state["image"] == state["old_image"]:
        state["image"] = "third-party/relay:changed"
        state_file.write_text(json.dumps(state), encoding="utf-8")
    if os.environ.get("RELAY_TEST_ROLLBACK_FAIL") == "1" and patch[-1]["value"] == state["old_image"]:
        sys.exit(1)
    if patch[0]["path"] == "/metadata/resourceVersion" and patch[0]["value"] != state["resource_version"]:
        sys.exit(1)
    if patch[-2]["value"] != state["image"]:
        sys.exit(1)
    state["image"] = patch[-1]["value"]
    state["resource_version"] = str(int(state["resource_version"]) + 1)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    if os.environ.get("RELAY_TEST_PATCH_AMBIGUOUS") == "1" and state["image"] == os.environ["RELAY_TEST_IMAGE"]:
        sys.exit(1)
    print("deployment.apps/sre-telegram-relay patched")
elif args[:4] == ["kubectl", "-n", "monitoring", "rollout"] and args[4:6] == ["status", "deployment/sre-telegram-relay"]:
    if os.environ.get("RELAY_TEST_ROLLOUT_FAIL") == "1" and state["image"] == os.environ["RELAY_TEST_IMAGE"]:
        sys.exit(1)
    print("deployment successfully rolled out")
else:
    print("unexpected command", file=sys.stderr)
    sys.exit(92)
'''


class RelayUpdateTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "relay update tool is missing")

    def run_update(self, mode, *, image=DIGEST_IMAGE, overrides=None, ready=1):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "sudo").write_text(SUDO_STUB, encoding="utf-8")
            (base / "sudo").chmod(0o755)
            script = base / SCRIPT.name
            script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)
            verify = base / "sre-telegram-verify.sh"
            verify.write_text(
                "#!/bin/sh\n"
                "python3 -c 'import json, os, pathlib, sys; "
                "state = json.loads((pathlib.Path(os.environ[\"RELAY_TEST_DIR\"]) / \"state.json\").read_text()); "
                "sys.exit(1 if os.environ.get(\"RELAY_TEST_VERIFY_FAIL\") == \"1\" "
                "and state[\"image\"] == os.environ[\"RELAY_TEST_IMAGE\"] else 0)'\n",
                encoding="utf-8",
            )
            verify.chmod(0o644)
            state_file = base / "state.json"
            state_file.write_text(
                json.dumps({"image": OLD_IMAGE, "old_image": OLD_IMAGE, "resource_version": "8", "ready": ready}),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PATH": f"{base}:{os.environ['PATH']}",
                "RELAY_TEST_DIR": directory,
                "RELAY_TEST_IMAGE": DIGEST_IMAGE,
                **(overrides or {}),
            }
            result = subprocess.run(
                ["bash", str(script), mode, "--image", image],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            calls_file = base / "calls.jsonl"
            calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return result, calls, state

    def test_check_is_read_only_and_preserves_runtime_state(self):
        for mode in ("--check", "--dry-run"):
            with self.subTest(mode=mode):
                result, calls, state = self.run_update(mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(state["image"], OLD_IMAGE)
                self.assertFalse(any("patch" in call or "apply" in call or "replace" in call for call in calls))
                self.assertIn("sre_telegram_update=CHECK_PASS", result.stdout)

    def test_check_accepts_containerd_canonical_name_for_existing_image(self):
        result, calls, state = self.run_update("--check", overrides={"RELAY_TEST_CANONICAL_OLD": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["image"], OLD_IMAGE)
        self.assertFalse(any("patch" in call for call in calls))

    def test_rejects_mutable_tag_without_cluster_call(self):
        result, calls, _ = self.run_update("--apply", image="example/relay:latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_apply_changes_only_deployment_image_and_verifies_rollout(self):
        result, calls, state = self.run_update("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["image"], DIGEST_IMAGE)
        self.assertIn("sre_telegram_update=PASS", result.stdout)
        patches = [call for call in calls if "patch" in call]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][6:8], ["deployment", "sre-telegram-relay"])
        self.assertFalse(any("configmap" in call or "secret" in call or "apply" in call or "replace" in call for call in calls))

    def test_failed_verification_restores_original_image(self):
        result, calls, state = self.run_update("--apply", overrides={"RELAY_TEST_VERIFY_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], OLD_IMAGE)
        self.assertEqual(len([call for call in calls if "patch" in call]), 2)
        self.assertIn("sre_telegram_rollback=PASS", result.stdout)

    def test_ambiguous_patch_response_restores_if_target_image_was_applied(self):
        result, calls, state = self.run_update("--apply", overrides={"RELAY_TEST_PATCH_AMBIGUOUS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], OLD_IMAGE)
        self.assertEqual(len([call for call in calls if "patch" in call]), 2)

    def test_unready_relay_stops_before_mutation(self):
        result, calls, state = self.run_update("--apply", ready=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], OLD_IMAGE)
        self.assertFalse(any("patch" in call for call in calls))

    def test_concurrent_image_change_is_not_overwritten_by_rollback(self):
        result, calls, state = self.run_update("--apply", overrides={"RELAY_TEST_DRIFT_BEFORE_PATCH": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], "third-party/relay:changed")
        self.assertEqual(len([call for call in calls if "patch" in call]), 1)
        self.assertIn("sre_telegram_rollback=UNVERIFIED", result.stdout)

    def test_rollback_failure_reports_unverified_and_keeps_target_visible(self):
        result, calls, state = self.run_update(
            "--apply",
            overrides={"RELAY_TEST_VERIFY_FAIL": "1", "RELAY_TEST_ROLLBACK_FAIL": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], DIGEST_IMAGE)
        self.assertEqual(len([call for call in calls if "patch" in call]), 2)
        self.assertIn("sre_telegram_rollback=UNVERIFIED", result.stdout)

    def test_rollback_requires_original_runtime_image_id(self):
        result, calls, state = self.run_update(
            "--apply",
            overrides={"RELAY_TEST_VERIFY_FAIL": "1", "RELAY_TEST_WRONG_ROLLBACK_ID": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["image"], OLD_IMAGE)
        self.assertEqual(len([call for call in calls if "patch" in call]), 2)
        self.assertIn("sre_telegram_rollback=UNVERIFIED", result.stdout)
        self.assertNotIn("sre_telegram_rollback=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
