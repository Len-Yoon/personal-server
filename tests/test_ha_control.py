"""Transition ordering, durable retry bounds and legacy exclusion contracts."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control.controller import Controller, Journal, TransitionBlocked, exclusive
from ha_control.windows import WindowsFence


class Backend:
    def __init__(self):
        self.actions, self.stopped, self.primary, self.ready, self.peer = [], False, False, False, False
        self.failure = None

    @contextmanager
    def guard(self):
        if self.failure == "legacy":
            raise TransitionBlocked("legacy_active")
        yield

    def close(self, state): self.actions.append("close"); self.ready = False
    def fenced(self, state): return self.stopped
    def fence(self, state):
        self.actions.append("fence")
        if self.failure != "fence": self.stopped = True
        if self.failure == "fence_unknown": raise OSError("unknown")
    def promoted(self, state): return self.primary
    def promote(self, state):
        self.actions.append("promote")
        self.primary = True
        if self.failure == "promote_unknown": raise OSError("unknown")
    def rejoin(self, state):
        self.actions.append("rejoin")
        if self.failure == "rejoin": raise TransitionBlocked("peer_unavailable")
        self.peer = True
        if self.failure == "rejoin_unknown": raise OSError("unknown")
    def rejoined(self, state): return self.peer
    def validate(self, state):
        self.actions.append("validate")
        if self.failure == "proof": raise TransitionBlocked("peer_proof_unavailable")
    def open(self, state):
        self.actions.append("open"); self.ready = True
        if self.failure == "open_unknown": raise OSError("unknown")
    def assert_ready(self, state):
        if not self.ready: raise TransitionBlocked("not_ready")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.json"
        self.backend = Backend()
        self.control = Controller(self.path, self.backend)
        self.control.initialize(cluster="synthetic", generation=3, old="A", target="B", timeline=1)

    def test_fence_promote_rejoin_proof_open_order(self):
        self.assertEqual(self.control.advance()["phase"], "ready")
        self.assertEqual(self.backend.actions, ["close", "fence", "promote", "rejoin", "validate", "open"])

    def test_unconfirmed_fence_never_promotes(self):
        self.backend.failure = "fence"
        with self.assertRaisesRegex(TransitionBlocked, "positive_fence_required"):
            self.control.advance()
        self.assertNotIn("promote", self.backend.actions)
        self.assertEqual(Journal(self.path).read()["phase"], "fencing")

    def test_unknown_fence_is_reobserved_without_repeating_stop(self):
        self.backend.failure = "fence_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("fence"), 1)

    def test_unknown_promotion_is_reobserved_without_second_promotion(self):
        self.backend.failure = "promote_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("promote"), 1)

    def test_rejoin_or_proof_failure_keeps_writes_closed(self):
        for failure in ("rejoin", "proof"):
            with self.subTest(failure=failure):
                self.backend.failure = failure
                with self.assertRaises(TransitionBlocked): self.control.advance()
                self.assertNotIn("open", self.backend.actions)

    def test_unknown_rejoin_is_observed_without_second_resource_creation(self):
        self.backend.failure = "rejoin_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions.count("rejoin"), 1)

    def test_unknown_open_closes_again_and_reproves_without_new_generation(self):
        self.backend.failure = "open_unknown"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.backend.failure = None
        state = Controller(self.path, self.backend).advance()
        self.assertEqual(state["generation"], 3)
        self.assertEqual(self.backend.actions[-3:], ["close", "validate", "open"])

    def test_three_attempt_bound_survives_controller_restart(self):
        self.backend.failure = "fence"
        for _ in range(3):
            with self.assertRaises(TransitionBlocked): Controller(self.path, self.backend).advance()
        actions = list(self.backend.actions)
        with self.assertRaisesRegex(TransitionBlocked, "exhausted"):
            Controller(self.path, self.backend).advance()
        self.assertEqual(self.backend.actions, actions)
        self.assertEqual(Journal(self.path).read()["attempts"], 3)

    def test_journal_corruption_and_failed_persistence_deny_actions(self):
        self.path.write_text("corrupt")
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.assertEqual(self.backend.actions, [])
        with patch.object(Journal, "save", side_effect=TransitionBlocked("not_durable")):
            with self.assertRaises(TransitionBlocked): self.control.advance()

    def test_legacy_guard_excludes_all_actions_and_duplicate_controller(self):
        self.backend.failure = "legacy"
        with self.assertRaises(TransitionBlocked): self.control.advance()
        self.assertEqual(self.backend.actions, [])
        if sys.platform != "win32":
            with exclusive(self.path.with_suffix(".lock")):
                with self.assertRaises(TransitionBlocked): self.control.advance()

    def test_native_adapter_cannot_be_armed_on_mac_or_linux(self):
        with patch("ha_control.windows.sys.platform", "darwin"):
            with self.assertRaisesRegex(TransitionBlocked, "native_windows_required"):
                WindowsFence("unused", "unused")

    def test_every_legacy_start_mutator_checks_durable_ownership(self):
        source = (ROOT / "scripts/windows-bootstrap.ps1").read_text(encoding="utf-8-sig")
        for function in ("Invoke-WslCommand", "Start-CloudflareTunnel", "Start-PersonalServerStack", "Invoke-TargetedRecovery", "Request-EmergencyReboot", "Install-ScheduledTask"):
            start = source.index("function " + function)
            end = source.index("\nfunction ", start + 1) if "\nfunction " in source[start + 1:] else len(source)
            self.assertIn("Assert-HaLegacyAllowed", source[start:end])
        self.assertIn('"ha-controller-owner.json"', source)

    def test_windows_fence_has_exact_inventory_engine_and_legacy_barriers(self):
        source = (ROOT / "infra/k8s/ha_control/windows-fence.ps1").read_text()
        for text in ("Sort-Object -Unique", "Compare-Object $expected $observed", "unix:///var/run/docker.sock", "owned_engine_mismatch", "ha-controller-owner.json", "bootstrap_sha256", "legacy_process_running", "shutdown_state_unverified", '"supervisor.lock", "daemon.lock", "recovery.lock"', "Assert-PrivateDirectory", "PropagationFlags]::InheritOnly", "ChangePermissions", "TakeOwnership"):
            self.assertIn(text, source)
        self.assertNotIn("Restart-Computer", source)
        self.assertNotIn("wsl --shutdown", source)
