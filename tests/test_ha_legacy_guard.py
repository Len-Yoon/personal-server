"""Source-bound guard composition and native bridge protocol; no remote actions."""
import base64
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import queue
import sys
import subprocess
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/k8s"))
from ha_control import legacy_guard as guard
from ha_control.controller import TransitionBlocked
from ha_control.windows import WindowsFence


class LegacyGuardTests(unittest.TestCase):
    def setUp(self):
        self.observation = dict(schema_version=1, complete=True, boot_ticks="123456",
                                principal_sha256="a" * 64, services=[], tasks=[], processes=[],
                                startup_sha256="b" * 64)
        self.guard = dict(schema_version=1, windows=dict(
            schema_version=1, boot_ticks="123456", principal_sha256="a" * 64,
            inventory_sha256=guard.digest(self.observation), frozen_services=[], frozen_tasks=[],
            provider_services=[], provider_tasks=[], provider_processes=[]),
            linux=dict(distribution="Fixture-Linux", reader_user="root", spec={"uid": 0}))
        self.plan = dict(source_commit="a" * 40, cluster="fixture", generation=7,
                         legacy_guard=self.guard, legacy_linux_sha256="c" * 64,
                         legacy_guard_code_sha256="d" * 64)
        self.request = dict(schema_version=1, nonce="e" * 64, plan_sha256="0" * 64,
                            cluster="fixture", generation=7, collector_sha256="c" * 64,
                            guard=self.guard, windows=self.observation, linux_receipt={})
        self.seal()
        self.source = patch.object(guard, "__collector_sha256__", "d" * 64, create=True)
        self.source.start()
        self.addCleanup(self.source.stop)

    def seal(self):
        raw = json.dumps(self.plan).encode()
        self.request["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        self.request["plan_raw"] = base64.b64encode(raw).decode()
        self.approval = dict(action="ha-transition", source_commit="a" * 40,
                             plan_sha256=self.request["plan_sha256"],
                             expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        self.request["approval_raw"] = base64.b64encode(json.dumps(self.approval).encode()).decode()
        self.request["linux_receipt"] = {key: self.request[key] for key in (
            "schema_version", "nonce", "plan_sha256", "cluster", "generation", "collector_sha256")}
        self.request["linux_receipt"].update(complete=True, verified=True)

    def reject(self):
        with self.assertRaises(guard.GuardBlocked):
            guard.verify(self.request)

    def test_complete_live_receipt_is_bound_to_exact_plan_and_approval(self):
        self.assertEqual(guard.verify(self.request), self.request["linux_receipt"])

    def test_actual_isolated_bootstrap_executes_only_the_pinned_source_bytes(self):
        directory = Path(guard.__file__).parent
        source = (directory / "legacy_guard.py").read_bytes()
        source_sha = hashlib.sha256(source).hexdigest()
        self.plan["legacy_guard_code_sha256"] = source_sha
        self.seal()
        line = next(line for line in (directory / "windows-legacy-guard.ps1").read_text().splitlines()
                    if line.strip().startswith("$bootstrap = "))
        bootstrap = ast.literal_eval(line.split(" = ", 1)[1])
        envelope = dict(code=base64.b64encode(source).decode(), sha256=source_sha, request=self.request)
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", bootstrap],
                                input=json.dumps(envelope), capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.request["linux_receipt"])
        envelope["code"] = base64.b64encode(source + b"\n").decode()
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", bootstrap],
                                input=json.dumps(envelope), capture_output=True, text=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"verified": true', result.stdout)

    def test_missing_source_binding_cannot_compare_as_two_nulls(self):
        self.plan["legacy_guard_code_sha256"] = None
        self.seal()
        with patch.object(guard, "__collector_sha256__", None):
            self.reject()

    def test_receipt_cannot_replay_nonce_plan_cluster_or_generation(self):
        for key, value in (("nonce", "f" * 64), ("plan_sha256", "f" * 64),
                           ("cluster", "other"), ("generation", 8), ("collector_sha256", "f" * 64)):
            request = deepcopy(self.request)
            self.request["linux_receipt"][key] = value
            self.reject()
            self.request = request

    def test_boolean_generation_and_incomplete_or_extended_receipts_are_denied(self):
        for key, value in (("schema_version", True), ("complete", 1), ("verified", False), ("unknown", 1)):
            receipt = dict(self.request["linux_receipt"])
            self.request["linux_receipt"][key] = value
            self.reject()
            self.request["linux_receipt"] = receipt
        self.request["generation"] = True
        self.reject()

    def test_raw_plan_and_approval_tampering_is_denied(self):
        for field in ("plan_raw", "approval_raw"):
            original = self.request[field]
            self.request[field] = base64.b64encode(b"{}").decode()
            self.reject()
            self.request[field] = original

    def test_duplicate_plan_keys_are_denied_even_when_bytes_are_pinned(self):
        raw = json.dumps(self.plan).encode()
        raw = raw[:-1] + b',"generation":7}'
        self.request["plan_raw"] = base64.b64encode(raw).decode()
        self.request["plan_sha256"] = hashlib.sha256(raw).hexdigest()
        self.reject()

    def test_expired_approval_and_wrong_commit_are_denied(self):
        for key, value in (("expires_at", "2020-01-01T00:00:00Z"), ("source_commit", "f" * 40), ("action", "observe")):
            approval = dict(self.approval, **{key: value})
            self.request["approval_raw"] = base64.b64encode(json.dumps(approval).encode()).decode()
            self.reject()

    def test_windows_snapshot_change_is_not_auto_approved(self):
        self.observation["startup_sha256"] = "f" * 64
        self.reject()

    def test_approved_running_recreator_still_cannot_pass(self):
        row = dict(id="1" * 64, spec_sha256="2" * 64, creator=True, provider=False, disabled=False, stopped=False)
        self.observation["services"] = [row]
        self.guard["windows"]["frozen_services"] = [row["id"]]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()
        row.update(disabled=True, stopped=True)
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        guard.verify(self.request)

    def test_unregistered_recreator_and_duplicate_service_ids_are_denied(self):
        for rows in ([dict(id="1" * 64, spec_sha256="2" * 64, creator=True, provider=False, disabled=True, stopped=True)],
                     [dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=True, disabled=True, stopped=True)] * 2):
            self.observation["services"] = rows
            self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
            self.seal()
            self.reject()

    def test_incomplete_windows_observation_is_denied_even_when_pinned(self):
        self.observation["complete"] = False
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()

    def test_default_reader_is_not_silently_promoted_to_root(self):
        self.guard["linux"]["spec"]["uid"] = 1000
        self.seal()
        self.reject()

    def test_registered_unknown_client_is_not_a_trusted_os_provider(self):
        row = dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=False)
        self.observation["processes"] = [row]
        self.guard["windows"]["provider_processes"] = [row["id"]]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()
        row["provider"] = True
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        guard.verify(self.request)

    def test_verified_provider_not_in_approval_is_denied(self):
        self.observation["processes"] = [dict(id="1" * 64, spec_sha256="2" * 64, creator=False, provider=True)]
        self.guard["windows"]["inventory_sha256"] = guard.digest(self.observation)
        self.seal()
        self.reject()


class WindowsBridgeProofTests(unittest.TestCase):
    def setUp(self):
        self.bridge = WindowsFence.__new__(WindowsFence)
        self.bridge.process = Mock()
        self.bridge.process.poll.return_value = None
        self.bridge.process.stdin = io.StringIO()
        self.bridge.answers = queue.Queue()
        self.bridge.plan_sha256 = "a" * 64
        self.bridge.plan_data = dict(cluster="fixture", legacy_linux_sha256="b" * 64)
        self.state = dict(old="A", generation=7)
        self.nonce = patch("ha_control.windows.secrets.token_hex", return_value="c" * 64)
        self.nonce.start()
        self.addCleanup(self.nonce.stop)
        self.answer = dict(nonce="c" * 64, node="A", generation=7, status="ok", fenced=True,
                           legacy_guard=dict(schema_version=1, nonce="c" * 64, plan_sha256="a" * 64,
                                             cluster="fixture", generation=7, collector_sha256="b" * 64,
                                             complete=True, verified=True))

    def ask(self):
        self.bridge.answers.put(json.dumps(self.answer))
        return self.bridge.fenced(self.state)

    def test_exact_live_protocol_succeeds(self):
        self.assertTrue(self.ask())

    def test_old_protocol_without_legacy_guard_is_denied(self):
        del self.answer["legacy_guard"]
        with self.assertRaises(TransitionBlocked): self.ask()

    def test_boolean_schema_or_wrong_collector_cannot_pass(self):
        for key, value in (("schema_version", True), ("collector_sha256", "f" * 64), ("nonce", "f" * 64)):
            original = self.answer["legacy_guard"][key]
            self.answer["legacy_guard"][key] = value
            with self.assertRaises(TransitionBlocked): self.ask()
            self.answer["legacy_guard"][key] = original

    def test_end_to_end_observe_deadline_includes_transport(self):
        with patch("ha_control.windows.time.monotonic", side_effect=[10, 12.001]):
            with self.assertRaises(TransitionBlocked): self.ask()

    def test_non_object_and_duplicate_key_responses_have_static_failure(self):
        for raw in ('[]', '{"nonce":"a","nonce":"b"}'):
            self.bridge.answers.put(raw)
            with self.assertRaisesRegex(TransitionBlocked, "windows_fence_unconfirmed"):
                self.bridge.fenced(self.state)


if __name__ == "__main__":
    unittest.main()
