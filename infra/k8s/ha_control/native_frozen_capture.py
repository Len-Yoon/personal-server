"""Bind the legacy Linux writer and native volume readers to one cutover attempt."""
import re
from pathlib import Path

from ha_runtime import seven_capture
from ha_runtime.config import SERVICES

from .capture_plan import capture_closed_sources
from .compose_capture import ComposeCapture, PATHS, inspect_compose_capture
from .legacy_cutover import _pin
from .legacy_writers import LinuxWriters, _need, inspect_live_baseline
from .pvc_capture import PvcCapture, inspect_pvc_bindings


class NativeFrozenCapture:
    """Own the WSL source side. The host guard supplies fresh external closure evidence."""

    def __init__(self, root, identity, attempt, converter_source_pins, external_writers, observe_external_closed):
        self.root = Path(root) / _pin(attempt)
        _need(set(identity) == {"source_commit", "cluster", "plan_sha256", "target_system_id"},
              "native_capture_identity_invalid")
        formats = {"source_commit": r"[a-f0-9]{40}", "cluster": r"[a-z][a-z0-9_]{0,62}",
                   "target_system_id": r"[1-9][0-9]{0,19}"}
        _need(all(type(identity[name]) is str and re.fullmatch(pattern, identity[name])
                  for name, pattern in formats.items()), "native_capture_identity_invalid")
        self.identity = dict(identity)
        self.attempt = attempt
        self.plan = _pin(identity["plan_sha256"])
        self.converter_source_pins = {name: _pin(digest) for name, digest in converter_source_pins.items()}
        self.external_writers = {service: tuple(ids) for service, ids in external_writers.items()}
        _need(set(self.external_writers) == SERVICES
              and all(ids and len(set(ids)) == len(ids) for ids in self.external_writers.values())
              and all(type(actor) is str and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", actor)
                      for ids in self.external_writers.values() for actor in ids),
              "native_capture_external_graph_invalid")
        self.observe_external_closed = observe_external_closed
        self.fence_sha256 = None

    def _save(self, name, value):
        return seven_capture._write(self.root / name, seven_capture.encoded(value))["sha256"]

    def prepare(self):
        seven_capture.trusted(self.root.parent, directory=True)
        self.converter_reader = seven_capture.Reader()
        converter_sources = seven_capture.inspect_converter_sources(self.converter_reader)
        _need(converter_sources == self.converter_source_pins, "native_capture_converter_source_unbound")
        baseline = inspect_live_baseline()
        pvc = inspect_pvc_bindings()
        compose = inspect_compose_capture({service: baseline["containers"][service]["id"] for service in PATHS})
        _need(all(row["deployment_uid"] == baseline["deployments"][service]["uid"] for service, row in pvc.items()),
              "native_capture_baseline_changed")
        self.writers = LinuxWriters(baseline)
        self.pvc = PvcCapture(pvc, self.attempt)
        self.compose = ComposeCapture(compose)
        self.root.mkdir(mode=0o700)
        (self.root / "sources").mkdir(mode=0o700)
        self.runtime_sha256 = self._save("runtime.json", {
            "schema_version": 1, "plan_sha256": self.plan, "attempt_id": self.attempt,
            "writer_baseline": baseline, "pvc_bindings": pvc, "compose_bindings": compose,
            "converter_sources": converter_sources,
        })
        schedule_claims = {}
        for row in baseline["schedules"]:
            schedule, _ = self.writers._schedule(row)
            template = schedule["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            schedule_claims["kube:cronjob:" + row["uid"]] = self.writers._claims(template)
        self.local_ids = set()
        graph = {}
        for service in sorted(SERVICES):
            actors = {"docker:" + baseline["containers"][service]["id"]}
            if service in baseline["deployments"]:
                actors.add("kube:deployment:" + baseline["deployments"][service]["uid"])
                claims = {claim["name"] for claim in pvc[service]["claims"]}
                actors.update(actor for actor, mounted in schedule_claims.items() if claims & mounted)
            self.local_ids.update(actors)
            graph[service] = sorted(actors | set(self.external_writers[service]))
        self.external_ids = set().union(*self.external_writers.values())
        _need(set(schedule_claims) <= self.local_ids and self.local_ids.isdisjoint(self.external_ids),
              "native_capture_external_graph_invalid")
        self.graph_sha256 = self._save("writer-graph.json", {
            "schema_version": 1, "source_runtime_sha256": self.runtime_sha256, "services": graph,
        })
        self.writers.assert_baseline()
        return self.runtime_sha256

    def block_reactivation(self):
        self.converter_reader.unchanged()
        self.writers.block_reactivation()

    def _external(self, settlement):
        observed = self.observe_external_closed(self.plan, self.attempt, settlement)
        _need(type(observed) is dict and set(observed) == self.external_ids,
              "native_capture_external_closure_incomplete")
        return {actor: _pin(receipt) for actor, receipt in observed.items()}

    def fence(self, reactivation, settlement):
        _pin(reactivation)
        _pin(settlement)
        self._external(settlement)
        self.converter_reader.unchanged()
        _need(inspect_pvc_bindings() == self.pvc.bindings, "native_capture_baseline_changed")
        self.writers.fence()
        closed_at = seven_capture.now()
        receipts = self._external(settlement)
        observed_at = seven_capture.now()
        for actor in self.local_ids:
            receipts[actor] = seven_capture.sha(seven_capture.encoded({
                "actor": actor, "runtime_sha256": self.runtime_sha256,
                "settlement_sha256": settlement, "observed_at": observed_at,
            }))
        self.fence_sha256 = self._save("writer-closure.json", {
            "schema_version": 1, "cluster": self.identity["cluster"], "plan_sha256": self.plan,
            "target_system_id": self.identity["target_system_id"], "services": sorted(SERVICES),
            "source_runtime_sha256": self.runtime_sha256, "writer_graph_sha256": self.graph_sha256,
            "writers_closed_at": closed_at, "attempt_id": self.attempt,
            "observations": [{"writer_id": actor, "state": "closed", "evidence_sha256": receipt,
                              "observed_at": observed_at} for actor, receipt in sorted(receipts.items())],
        })
        self.settlement_sha256 = settlement
        return self._receipt(self.fence_sha256, reactivation_sha256=reactivation,
                             settlement_sha256=settlement, writers_closed=True, services=sorted(SERVICES))

    def _receipt(self, evidence, **facts):
        return {"plan_sha256": self.plan, "attempt_id": self.attempt,
                "evidence_sha256": _pin(evidence), **facts}

    def assert_closed(self, fence):
        _need(self.fence_sha256 is not None and fence == self.fence_sha256, "native_capture_fence_unbound")
        self._external(self.settlement_sha256)
        self.writers.assert_closed()
        self.converter_reader.unchanged()
        expected = {"runtime.json": self.runtime_sha256, "writer-graph.json": self.graph_sha256,
                    "writer-closure.json": fence}
        _need(all(seven_capture.descriptor(self.root / name)["sha256"] == digest
                  for name, digest in expected.items()), "native_capture_evidence_changed")

    def capture(self, fence):
        self.assert_closed(fence)
        result = capture_closed_sources(
            pvc=self.pvc, compose=self.compose, sources_root=self.root / "sources",
            private_root=self.root, output=self.root / "frozen", identity=self.identity,
            evidence={"runtime": self.root / "runtime.json", "writer_graph": self.root / "writer-graph.json",
                      "writer_closure": self.root / "writer-closure.json"},
            expected_evidence_pins={"runtime": self.runtime_sha256, "writer_graph": self.graph_sha256,
                                    "writer_closure": fence},
            assert_writers_closed=lambda: self.assert_closed(fence),
        )
        _need(result["status"] == "captured", "native_capture_unconfirmed")
        reader = seven_capture.Reader()
        frozen = self.root / "frozen"
        manifest_descriptor = seven_capture.descriptor(frozen / "manifest.json")
        _need(manifest_descriptor["sha256"] == result["manifest_sha256"], "native_capture_manifest_changed")
        manifest = seven_capture.parse(reader.read(manifest_descriptor))
        freeze = manifest["freeze_receipt"]
        _need(all(manifest[key] == value for key, value in self.identity.items())
              and set(freeze) == {"file", "sha256", "bytes"} and freeze["file"] == "freeze-receipt.json",
              "native_capture_manifest_unbound")
        freeze_descriptor = {"path": str(frozen / freeze["file"]), "sha256": freeze["sha256"],
                             "bytes": freeze["bytes"]}
        closure = seven_capture.parse(reader.read(freeze_descriptor))
        _need(closure["writer_fence_receipt_sha256"] == fence
              and closure["source_runtime_sha256"] == self.runtime_sha256
              and closure["writer_graph_sha256"] == self.graph_sha256
              and closure["capture_audit"]["sha256"] == result["capture_audit_sha256"],
              "native_capture_manifest_unbound")
        reader.unchanged()
        return self._receipt(result["capture_audit_sha256"], fence_sha256=fence,
                             manifest_sha256=_pin(result["manifest_sha256"]))

    def assert_reader_settled(self):
        self.pvc.assert_settled()

    def restore_writers(self):
        """Caller must separately prove pre-activation rollback is safe."""
        self.assert_reader_settled()
        self.writers.restore()
