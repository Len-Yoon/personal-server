"""Fault matrix for HA admission; no production data, DB, Docker or network."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "n100_ha_contract", Path(__file__).resolve().parents[1] / "infra/k8s/tools/n100_ha_contract.py",
)
HA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HA
SPEC.loader.exec_module(HA)
LAB_SPEC = importlib.util.spec_from_file_location(
    "n100_ha_pg_lab", Path(__file__).resolve().parents[1] / "infra/k8s/tools/n100_ha_pg_lab.py",
)
LAB = importlib.util.module_from_spec(LAB_SPEC)
LAB_SPEC.loader.exec_module(LAB)


class HALabSafetyTests(unittest.TestCase):
    def fake_docker(self, *, remote=False, cleanup_failure=False):
        calls = []
        ownership = {}

        def execute(command, **kwargs):
            self.assertEqual(command[:3], ["docker", "--context", "orbstack"])
            self.assertFalse(any(key.startswith("DOCKER_") for key in kwargs["env"]))
            args = command[3:]
            calls.append(args)
            output = ""
            code = 0
            if args[:2] == ["context", "inspect"]:
                host = "tcp://remote.invalid" if remote else "unix://" + str(Path.home() / ".orbstack/run/docker.sock")
                output = json.dumps([{"Endpoints": {"docker": {"Host": host}}}])
            elif args[:2] == ["image", "inspect"]:
                output = "sha256:" + "a" * 64
            elif args[:2] == ["network", "create"]:
                ownership["label"] = args[args.index("--label") + 1].split("=", 1)[1]
                output = "n" * 64
            elif args[0] == "create":
                # Engine created two owned resources, but client lost response.
                raise subprocess.TimeoutExpired(command, 45)
            elif args[:2] in (["container", "inspect"], ["network", "inspect"]):
                labels = {"personal-server.ha-lab-owner": ownership["label"]}
                output = json.dumps([{"Id": args[2], "Config": {"Labels": labels}, "Labels": labels}])
            elif args[0] == "ps":
                output = "c" * 64 + "\n" + "d" * 64
            elif args[:2] == ["network", "ls"]:
                output = "n" * 64
            elif args[0] == "rm":
                code = int(cleanup_failure and args[-1] == "c" * 64)
            elif args[:2] != ["network", "rm"]:
                self.fail("Unexpected Docker operation: " + args[0])
            return subprocess.CompletedProcess(command, code, output, "")

        return execute, calls

    def test_remote_context_never_creates_resources(self):
        fake, calls = self.fake_docker(remote=True)
        with patch.object(LAB.subprocess, "run", side_effect=fake), self.assertRaisesRegex(LAB.LabError, "local_context_required"):
            LAB.run_lab()
        self.assertEqual(len(calls), 1)

    def test_lost_create_response_rediscovers_and_removes_owned_resources(self):
        fake, calls = self.fake_docker()
        with patch.object(LAB.subprocess, "run", side_effect=fake), self.assertRaises(subprocess.TimeoutExpired):
            LAB.run_lab()
        self.assertEqual(len([call for call in calls if call[0] == "create"]), 1)
        self.assertEqual({call[-1] for call in calls if call[0] == "rm"}, {"c" * 64, "d" * 64})
        self.assertTrue(any(call[:2] == ["network", "rm"] for call in calls))

    def test_one_cleanup_failure_preserves_original_error_and_continues(self):
        fake, calls = self.fake_docker(cleanup_failure=True)
        with patch.object(LAB.subprocess, "run", side_effect=fake), self.assertRaisesRegex(
            LAB.LabError, "TimeoutExpired;owned_fixture_cleanup_incomplete",
        ):
            LAB.run_lab()
        self.assertEqual(len([call for call in calls if call[0] == "rm"]), 2)
        self.assertTrue(any(call[:2] == ["network", "rm"] for call in calls))


class HAContractTests(unittest.TestCase):
    def setUp(self):
        self.authority = HA.Authority(
            cluster="lab-cluster", epoch=3, revision=7, primary="A", partner="B",
            timeline=2, serving=frozenset({"B"}), worker="B", mode="AS",
            writes_open=True, issued_at=10.0, deadline=15.0,
        )
        self.binding = HA.RequestBinding("book-memo", "save-1", "memo.save", "a" * 64, "b" * 64)
        self.proof = HA.DurabilityEvidence(
            authority_before=self.authority, authority_after=self.authority,
            attempt="fresh-attempt", primary_attempt="fresh-attempt", partner_attempt="fresh-attempt",
            binding=self.binding, primary_binding=self.binding, partner_binding=self.binding,
            watermark="1/100", primary_flush="1/101", partner_flush="1/100", partner_replay="1/100",
            primary="A", partner="B", cluster="lab-cluster", epoch=3, timeline=2,
            partner_source_timeline=2, primary_in_recovery=False, partner_in_recovery=True,
            sync_state="sync", fsync=True, full_page_writes=True,
            partner_fsync=True, partner_full_page_writes=True, synchronous_commit="on",
            configured_partner="B", protection_history_safe=True, observed_from=11.0, observed_until=11.5,
        )

    def admit(self, **changes):
        HA.require_protected_result(self.authority, self.binding, replace(self.proof, **changes),
                                    attempt="fresh-attempt", now=12.0)

    def test_fresh_sync_proof_admits_result_after_uncertain_local_commit(self):
        # The commit's driver return is intentionally not an admission input.
        self.admit()

    def test_unprotected_ledger_row_never_admits_retry(self):
        for changes in (
            {"partner_flush": "1/FF"}, {"partner_replay": "1/FF"},
            {"sync_state": "async"}, {"sync_state": "potential"},
            {"partner_binding": replace(self.binding, request_id="different")},
        ):
            with self.subTest(changes=changes), self.assertRaises(HA.AdmissionDenied):
                self.admit(**changes)

    def test_payload_and_result_mismatch_conflict_on_either_copy(self):
        for source in ("primary_binding", "partner_binding"):
            for field in ("payload_sha256", "result_sha256", "operation"):
                with self.subTest(source=source, field=field), self.assertRaises(HA.RequestConflict):
                    self.admit(**{source: replace(self.binding, **{field: "c" * 64})})

    def test_stale_cross_cluster_cross_timeline_or_wrong_node_proof_is_denied(self):
        cases = {
            "primary": "B", "partner": "A", "cluster": "other-cluster", "epoch": 2,
            "timeline": 1, "partner_source_timeline": 1, "configured_partner": "A",
            "primary_in_recovery": True, "partner_in_recovery": False,
            "fsync": False, "full_page_writes": False, "synchronous_commit": "local",
            "partner_fsync": False, "partner_full_page_writes": False,
            "protection_history_safe": False, "primary_attempt": "previous-attempt",
            "partner_attempt": "previous-attempt", "attempt": "previous-attempt",
            "primary_flush": "1/FF", "watermark": "0/0",
        }
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaises(HA.AdmissionDenied):
                self.admit(**{field: value})

    def test_authority_changes_mid_verification_close_admission(self):
        for field, value in (("epoch", 4), ("revision", 8), ("worker", "A"), ("writes_open", False)):
            for side in ("authority_before", "authority_after"):
                with self.subTest(field=field, side=side), self.assertRaises(HA.AdmissionDenied):
                    self.admit(**{side: replace(self.authority, **{field: value})})

    def test_old_proof_cannot_be_reused_for_new_attempt(self):
        with self.assertRaises(HA.AdmissionDenied):
            HA.require_protected_result(self.authority, self.binding, self.proof,
                                        attempt="new-retry", now=12.0)

    def test_expired_future_pre_authority_and_nonfinite_observations_are_denied(self):
        for changes in (
            {"observed_from": 9.9}, {"observed_until": 12.1},
            {"observed_from": 11.6}, {"observed_from": float("nan")},
            {"observed_until": float("inf")}, {"observed_from": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(HA.AdmissionDenied):
                self.admit(**changes)
        for now in (13.01, 15.0, float("nan"), float("inf"), -1, True):
            with self.subTest(now=now), self.assertRaises(HA.AdmissionDenied):
                HA.require_protected_result(self.authority, self.binding, self.proof,
                                            attempt="fresh-attempt", now=now)

    def test_invalid_authorities_cannot_serve_or_write(self):
        for changes in (
            {"epoch": True}, {"revision": 0}, {"timeline": -1}, {"cluster": ""},
            {"primary": "B"}, {"partner": "C"}, {"worker": "C"},
            {"serving": frozenset()}, {"serving": {"A"}},
            {"serving": frozenset({"A", "B"})}, {"mode": "unknown"},
            {"writes_open": 1}, {"issued_at": 12.1}, {"deadline": 12.0},
            {"deadline": 16.0}, {"issued_at": float("nan")},
        ):
            authority = replace(self.authority, **changes)
            with self.subTest(changes=changes), self.assertRaises(HA.AdmissionDenied):
                HA.require_serving_owner(authority, service="book-memo", node="B", now=12.0)

    def test_read_serving_can_continue_while_strict_writes_closed(self):
        authority = replace(self.authority, writes_open=False)
        HA.require_serving_owner(authority, service="book-memo", node="B", now=12.0)
        with self.assertRaises(HA.AdmissionDenied):
            HA.require_protected_result(authority, self.binding, self.proof,
                                        attempt="fresh-attempt", now=12.0)

    def test_lsn_numeric_order_and_malformed_values(self):
        self.assertGreater(HA.lsn_number("1/0"), HA.lsn_number("0/FFFFFFFF"))
        self.assertGreater(HA.lsn_number("0/100"), HA.lsn_number("0/FF"))
        for value in (None, 42, "1/", "-1/0", "0/100000000", "100000000/0", "0/ff", "0/1\n", " 0/1"):
            with self.subTest(value=value), self.assertRaises(HA.AdmissionDenied):
                HA.lsn_number(value)

    def test_binding_is_namespaced_by_service_and_request(self):
        for changes in ({"service": "youtube-memo"}, {"request_id": "save-2"}):
            with self.subTest(changes=changes), self.assertRaises(HA.AdmissionDenied):
                self.admit(primary_binding=replace(self.binding, **changes))
        for changes in ({"request_id": ""}, {"service": "toy-app"}, {"payload_sha256": "not-a-hash"}):
            with self.subTest(changes=changes), self.assertRaises(HA.AdmissionDenied):
                HA.require_protected_result(self.authority, replace(self.binding, **changes), self.proof,
                                            attempt="fresh-attempt", now=12.0)

    def test_app_switch_keeps_healthy_database_primary(self):
        # Serving/worker B with DB A is a valid independent role assignment.
        HA.require_serving_owner(self.authority, service="book-memo", node="B", now=12.0)
        self.admit()
        with self.assertRaises(HA.AdmissionDenied):
            HA.require_serving_owner(self.authority, service="book-memo", node="A", now=12.0)

    def test_active_active_webs_do_not_grant_duplicate_effect_ownership(self):
        authority = replace(self.authority, mode="AA", serving=frozenset({"A", "B"}))
        for service in HA.WEB_SERVICES:
            for node in ("A", "B"):
                HA.require_serving_owner(authority, service=service, node=node, now=12.0)
        for service in {"car-care-worker"}:
            HA.require_serving_owner(authority, service=service, node="B", now=12.0)
            with self.assertRaises(HA.AdmissionDenied):
                HA.require_serving_owner(authority, service=service, node="A", now=12.0)
        for service in ("system-agent", "homeops-executor"):
            for node in ("A", "B"):
                HA.require_serving_owner(authority, service=service, node=node, target_node=node, now=12.0)
                with self.assertRaises(HA.AdmissionDenied):
                    HA.require_serving_owner(authority, service=service, node=node, now=12.0)

    def test_outbox_must_reprove_durability_before_dispatch(self):
        HA.require_effect_owner(self.authority, self.binding, self.proof,
                                node="B", attempt="fresh-attempt", now=12.0)
        with self.assertRaises(HA.AdmissionDenied):
            HA.require_effect_owner(self.authority, self.binding, replace(self.proof, partner_flush="1/FF"),
                                    node="B", attempt="fresh-attempt", now=12.0)
        with self.assertRaises(HA.AdmissionDenied):
            HA.require_effect_owner(self.authority, self.binding, self.proof,
                                    node="A", attempt="fresh-attempt", now=12.0)

    def test_host_effect_requires_explicit_target_node(self):
        for service in ("homeops-executor", "system-agent"):
            binding = replace(self.binding, service=service, target_node="A")
            proof = replace(self.proof, binding=binding, primary_binding=binding, partner_binding=binding)
            for target in (None, "unknown", "B"):
                with self.subTest(service=service, target=target), self.assertRaises(HA.AdmissionDenied):
                    HA.require_effect_owner(self.authority, binding, proof, node="B",
                                            attempt="fresh-attempt", target_node=target, now=12.0)
            HA.require_effect_owner(self.authority, binding, proof, node="B",
                                    attempt="fresh-attempt", target_node="A", now=12.0)


if __name__ == "__main__":
    unittest.main()
