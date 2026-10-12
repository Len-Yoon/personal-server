"""Transaction regressions use synthetic ACLs; no Windows/production access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "infra/k8s/tools/windows-runtime-acl.py"
SPEC = importlib.util.spec_from_file_location("windows_runtime_acl", PATH)
acl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acl)


COUNTS = {"total": 34, "files": 12, "directories": 22, "git_unsafe": 18,
          "tracked_unsafe": 0, "ignored_unsafe": 1, "other_owner": 1}
GIT_DIRECTORIES = [".git/objects", ".git/objects/info", ".git/objects/pack",
                   ".git/refs", ".git/refs/heads", ".git/refs/tags"]
GIT_DIRECTORIES += [".git/other" + str(i) for i in range(11)]


MODIFIED_CONTRACT = {"version": "exact_runtime_modified_v1", "entries": [
    {"path": "data", "kind": "directory"},
    {"path": "data/system/host-metrics.json", "kind": "file"},
    {"path": "data/car-care", "kind": "directory"},
    {"path": "data/car-care/car-care.sqlite3", "kind": "file"},
]}


LIVE_PATHS = ("data/recovery-events.jsonl", "data/recovery-state.json",
              "data/system/host-metrics.json")
LIVE_CONTRACT = {"version": "exact_runtime_live_metadata_v2", "entries": [
    {"path": "data", "kind": "directory", "fields": ["modified"]},
    {"path": "data/system/host-metrics.json", "kind": "file", "fields": ["size", "modified"]},
    {"path": "data/car-care", "kind": "directory", "fields": ["modified"]},
    {"path": "data/car-care/car-care.sqlite3", "kind": "file", "fields": ["modified"]},
    {"path": "data/recovery-state.json", "kind": "file", "fields": ["size", "modified"]},
    {"path": "data/recovery-events.jsonl", "kind": "file", "fields": ["identity", "size", "modified"]},
]}


def scope():
    return {"schema": 3, "comparison_contract": copy.deepcopy(LIVE_CONTRACT),
            "root": "C:\\personal-server", "tracked_paths": ["tracked.py"],
            "counts": dict(COUNTS), "source_sha256": "1" * 64,
            "fixture_sha256": acl.digest(acl.POWERSHELL.encode())}


def entry(path, index, directory=False, explicit=None, owner="S-1-5-21-1"):
    explicit = explicit or ["original:" + path]
    return {"identity": "volume:" + str(index), "kind": "directory" if directory else "file",
            "attributes": 16 if directory else 32, "created": "1", "modified": "2",
            "size": "0", "links": 1, "owner": owner, "group": "S-1-5-32-544",
            "dacl": "oldroot" if path == "" else "|".join(explicit) + ":oldroot",
            "explicit": explicit, "protected": False, "sd": "originalSD:" + path,
            "unsafe": (directory and (path == ".git" or path.startswith(".git/"))) or path == "ignored"}


def snapshot():
    paths = ["", ".git"] + list(acl.RUNTIME) + GIT_DIRECTORIES
    paths += ["tracked.py", "ignored", ".git/objects/pack/value", ".git/refs/heads/value",
              "data", "data/system", "data/system/host-metrics.json",
              "data/car-care", "data/car-care/car-care.sqlite3",
              "data/recovery-state.json", "data/recovery-events.jsonl"]
    directories = {"", ".git", *GIT_DIRECTORIES, "data", "data/system", "data/car-care"}
    rows = {p: entry(p, i, p in directories) for i, p in enumerate(paths)}
    for path in sorted(rows, key=lambda p: (p.count("/"), len(p), p)):
        if path:
            rows[path]["dacl"] = "|".join(rows[path]["explicit"]) + ":" + rows[path.rpartition("/")[0]]["dacl"]
    rows["ignored"]["owner"] = "S-1-5-21-999"
    return {"entries": rows, "counts": dict(COUNTS),
            "runtime_hashes": {p: acl.digest(("synthetic content " + p).encode()) for p in acl.RUNTIME},
            "principal": "S-1-5-21-1", "projection_contract": "win32_private_object_security_v1"}


class FakeBackend:
    lock_owned = True
    mutation_authorized = True
    recovery_quiescent = False
    ancestor_sha256 = "a" * 64

    def __init__(self):
        self.current = snapshot()
        self.writes = []
        self.mode = None
        self.on_write = None

    def open(self, approved):
        self.approved = approved

    def snapshot(self):
        return copy.deepcopy(self.current)

    stable_snapshot = snapshot

    def project(self, before, target, original):
        result = copy.deepcopy(before)
        row = result["entries"][target]
        if original is None:
            row.update(explicit=["trusted3"], protected=True, unsafe=False,
                       dacl="fixed:" + target, sd="fixedSD:" + target)
        else:
            result["entries"][target] = copy.deepcopy(original)
            row = result["entries"][target]
            if target and not row["protected"]:
                row["dacl"] = "|".join(row["explicit"]) + ":" + result["entries"][target.rpartition("/")[0]]["dacl"]
                row["sd"] = "derived:" + row["dacl"]
        changed = {target}
        for path in sorted(result["entries"], key=lambda p: (p.count("/"), len(p), p)):
            child = result["entries"][path]
            parent = path.rpartition("/")[0]
            if path and path != target and parent in changed and not child["protected"]:
                child["dacl"] = "|".join(child["explicit"]) + ":" + result["entries"][parent]["dacl"]
                child["sd"] = "derived:" + child["dacl"]
                changed.add(path)
        return result

    def set_dacl(self, target, approved):
        self.writes.append(target)
        if self.on_write:
            self.on_write(target)
        original = approved if not approved["protected"] else None
        result = self.project(self.current, target, original)
        # Native SetSecurityInfo changes DACL fields only, even when repairing
        # a mutable runtime descendant from an older encrypted descriptor.
        result["entries"][target] = {
            **self.current["entries"][target],
            **{field: copy.deepcopy(approved[field]) for field in
               ("dacl", "explicit", "protected", "sd", "unsafe")}}
        if self.mode == "partial":
            self.mode = None
            paths = [p for p in result["entries"] if p != target]
            for p in paths[::2]:
                result["entries"][p] = self.current["entries"][p]
            self.current = result
            return
        if self.mode == "lost_after":
            self.current = result
            raise acl.UnknownWrite("native_response_unknown")
        if self.mode == "lost_before":
            raise acl.UnknownWrite("native_response_unknown")
        if self.mode == "known_error":
            self.mode = None
            raise acl.Blocked("native_acl_failure")
        if self.mode == "crash":
            self.current = result
            raise SystemExit("synthetic_crash")
        self.current = result

    def encrypt(self, value):
        return b"FAKE-DPAPI:" + bytes(b ^ 165 for b in value)

    def decrypt(self, value):
        if not value.startswith(b"FAKE-DPAPI:"):
            raise acl.Blocked("fake_cipher_corrupt")
        return bytes(b ^ 165 for b in value[11:])


class ACLTests(unittest.TestCase):
    def setUp(self):
        self.count_patch = mock.patch.object(acl, "EXPECTED_COUNTS", COUNTS)
        self.count_patch.start()
        # Other suites may change tempfile's global/default directory. This
        # fixture deliberately uses the repository's already trusted ancestors;
        # production private-directory checks remain unchanged.
        self.temp = tempfile.TemporaryDirectory(prefix=".windows-acl-test-", dir=PATH.parents[3].resolve())
        self.backend = FakeBackend()
        self.vault = acl.Vault(Path(self.temp.name).resolve() / "vault", self.backend)
        self.transaction = acl.Transaction(self.backend, self.vault)

    def tearDown(self):
        self.vault.close()
        self.temp.cleanup()
        self.count_patch.stop()

    def prepare(self):
        return self.transaction.prepare(scope())["plan_sha256"]

    def assertBlocked(self, reason, callback):
        with self.assertRaisesRegex(acl.Blocked, "^" + reason + "$"):
            callback()

    def test_schema_three_live_contract_has_only_exact_field_exceptions(self):
        self.assertEqual(acl.SCHEMA, 3)
        self.assertEqual(acl.COMPARISON_CONTRACT, LIVE_CONTRACT)

    def test_live_three_fields_share_comparison_hash_and_partial_ownership(self):
        initial = snapshot()
        saved = copy.deepcopy(initial)
        for row in LIVE_CONTRACT["entries"]:
            for field in row["fields"]:
                actual = copy.deepcopy(initial)
                actual["entries"][row["path"]][field] = "live-update"
                with self.subTest(path=row["path"], field=field):
                    self.assertTrue(acl.same_snapshot(actual, initial))
                    self.assertEqual(acl.snapshot_hash(actual), acl.snapshot_hash(initial))
                    self.assertTrue(acl.own_partial(actual, initial, initial))
                    self.assertNotEqual(actual, initial)
        self.assertEqual(initial, saved)

    def test_live_three_other_metadata_and_security_remain_strict(self):
        initial = snapshot()
        values = {"identity": "other-volume", "kind": "directory", "attributes": 1024,
                  "created": "new", "modified": "new", "size": "123", "links": 2,
                  "owner": "S-1-5-21-999", "group": "S-1-5-18", "dacl": "foreign",
                  "explicit": ["foreign"], "protected": True, "unsafe": True}
        for row in LIVE_CONTRACT["entries"]:
            for field, value in values.items():
                if field in row["fields"]:
                    continue
                actual = copy.deepcopy(initial)
                actual["entries"][row["path"]][field] = (
                    "file" if field == "kind" and row["kind"] == "directory" else value)
                with self.subTest(path=row["path"], field=field):
                    self.assertFalse(acl.same_snapshot(actual, initial))
                    self.assertNotEqual(acl.snapshot_hash(actual), acl.snapshot_hash(initial))
                    self.assertFalse(acl.own_partial(actual, initial, initial))
        for path in ("", "data/system", "tracked.py", ".git", *acl.RUNTIME):
            for field in ("identity", "size", "modified"):
                actual = copy.deepcopy(initial)
                actual["entries"][path][field] = "unapproved-update"
                with self.subTest(strict_path=path, field=field):
                    self.assertFalse(acl.same_snapshot(actual, initial))
                    self.assertFalse(acl.own_partial(actual, initial, initial))

    def test_live_contract_kind_inventory_and_projection_are_strict(self):
        for path in LIVE_PATHS:
            for change in ("missing", "directory", "reparse", "hardlink"):
                actual = snapshot()
                if change == "missing":
                    actual["entries"].pop(path)
                elif change == "directory":
                    actual["entries"][path]["kind"] = "directory"
                elif change == "reparse":
                    actual["entries"][path]["attributes"] |= 1024
                else:
                    actual["entries"][path]["links"] = 2
                with self.subTest(path=path, change=change), self.assertRaises(acl.Blocked):
                    acl.validate_snapshot(actual, scope())
        for row in LIVE_CONTRACT["entries"]:
            for field in row["fields"]:
                before = snapshot()
                after = copy.deepcopy(before)
                after["entries"][row["path"]][field] = "live-update"
                with self.subTest(path=row["path"], field=field):
                    self.assertBlocked("projection_metadata_changed",
                        lambda: acl.validate_projection(before, after, ""))

    def test_exact_four_modified_comparison_preserves_raw_snapshot(self):
        initial = snapshot()
        saved = copy.deepcopy(initial)
        for approved in MODIFIED_CONTRACT["entries"]:
            current = copy.deepcopy(initial)
            current["entries"][approved["path"]]["modified"] = "new-runtime-write"
            with self.subTest(path=approved["path"]):
                self.assertTrue(acl.same_snapshot(current, initial))
                self.assertEqual(acl.snapshot_hash(current), acl.snapshot_hash(initial))
                self.assertEqual(current["entries"][approved["path"]]["modified"], "new-runtime-write")
        self.assertEqual(initial, saved)

    def test_modified_contract_is_required_fixed_and_versioned(self):
        approved = scope()
        self.assertEqual(acl.validate_scope(approved), approved)
        for field, value in (("schema", 1), ("schema", 2), ("comparison_contract", {}),
                             ("comparison_contract", MODIFIED_CONTRACT),
                             ("comparison_contract", {**LIVE_CONTRACT, "version": "other"})):
            bad = copy.deepcopy(approved)
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(acl.Blocked):
                acl.validate_scope(bad)
        bad = copy.deepcopy(approved)
        bad.pop("comparison_contract")
        with self.assertRaises(acl.Blocked):
            acl.validate_scope(bad)
        bad = copy.deepcopy(approved)
        bad["comparison_contract"]["entries"].append({"path": "data/other", "kind": "file"})
        with self.assertRaises(acl.Blocked):
            acl.validate_scope(bad)

    def test_mutable_modified_expected_kind_is_required(self):
        for approved in MODIFIED_CONTRACT["entries"]:
            current = snapshot()
            current["entries"][approved["path"]]["kind"] = (
                "file" if approved["kind"] == "directory" else "directory")
            with self.subTest(path=approved["path"]), self.assertRaises(acl.Blocked):
                acl.validate_snapshot(current, scope())

    def test_only_exact_modified_fields_are_excluded(self):
        initial = snapshot()
        for path in ("data/system", "ignored", "tracked.py", ".git", *acl.RUNTIME):
            actual = copy.deepcopy(initial)
            actual["entries"][path]["modified"] = "changed"
            with self.subTest(path=path):
                self.assertFalse(acl.same_snapshot(actual, initial))
        for field, value in (("kind", "directory"), ("size", "123"), ("identity", "other-volume"),
                             ("created", "new"), ("owner", "S-1-5-21-999"),
                             ("group", "S-1-5-18"), ("dacl", "foreign"),
                             ("explicit", ["foreign"]), ("protected", True),
                             ("links", 2), ("attributes", 99), ("unsafe", True)):
            actual = copy.deepcopy(initial)
            actual["entries"]["data/car-care/car-care.sqlite3"][field] = value
            with self.subTest(field=field):
                self.assertFalse(acl.same_snapshot(actual, initial))
        actual = copy.deepcopy(initial)
        actual["runtime_hashes"][".env"] = "0" * 64
        self.assertFalse(acl.same_snapshot(actual, initial))
        actual = copy.deepcopy(initial)
        actual["entries"]["data/extra"] = entry("data/extra", 999)
        self.assertFalse(acl.same_snapshot(actual, initial))

    def test_projection_still_rejects_mutable_modified_changes(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after["entries"]["data/system/host-metrics.json"]["modified"] = "new"
        self.assertBlocked("projection_metadata_changed", lambda: acl.validate_projection(before, after, ""))

    def test_live_modified_apply_status_and_rollback_keep_original_plan(self):
        sha = self.prepare()
        cipher = self.vault.read_bytes("plan.dpapi")
        original = self.vault.read("plan.dpapi")["initial"]
        for approved in MODIFIED_CONTRACT["entries"]:
            self.backend.current["entries"][approved["path"]]["modified"] = "live-before-apply"
        self.transaction.apply(sha)
        self.assertEqual(self.transaction.status(sha)["matched"], "step_23")
        for approved in MODIFIED_CONTRACT["entries"]:
            self.backend.current["entries"][approved["path"]]["modified"] = "live-before-rollback"
        self.transaction.rollback(sha)
        self.assertTrue(acl.same_snapshot(self.backend.current, original))
        self.assertEqual(self.vault.read_bytes("plan.dpapi"), cipher)
        for approved in MODIFIED_CONTRACT["entries"]:
            self.assertEqual(original["entries"][approved["path"]]["modified"], "2")
            self.assertEqual(self.backend.current["entries"][approved["path"]]["modified"], "live-before-rollback")

    def test_partial_rollback_accepts_only_owned_acl_with_live_modified(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        for approved in MODIFIED_CONTRACT["entries"]:
            self.backend.current["entries"][approved["path"]]["modified"] = "live-after-partial"
        fresh = FakeBackend()
        fresh.current = copy.deepcopy(self.backend.current)
        transaction = acl.Transaction(fresh, self.vault)
        self.assertEqual(transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(fresh.current, snapshot()))
        for approved in MODIFIED_CONTRACT["entries"]:
            self.assertEqual(fresh.current["entries"][approved["path"]]["modified"], "live-after-partial")

    def test_live_modified_never_masks_foreign_partial_acl(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        for approved in MODIFIED_CONTRACT["entries"]:
            self.backend.current["entries"][approved["path"]]["modified"] = "active-writer"
        self.backend.current["entries"]["data/car-care/car-care.sqlite3"]["dacl"] = "foreign"
        writes = list(self.backend.writes)
        self.assertBlocked("partial_or_foreign_state", lambda: self.transaction.rollback(sha))
        self.assertEqual(self.backend.writes, writes)

    def update_live_three(self, marker):
        for row in LIVE_CONTRACT["entries"]:
            for field in row["fields"]:
                self.backend.current["entries"][row["path"]][field] = marker + ":" + field

    def test_live_three_apply_status_rollback_preserves_raw_plan_and_targets(self):
        sha = self.prepare()
        cipher = self.vault.read_bytes("plan.dpapi")
        raw = self.vault.read("plan.dpapi")
        original = copy.deepcopy(raw)
        targets = [step["target"] for step in raw["steps"]]
        self.assertEqual(len(targets), 23)
        self.assertFalse(set(targets) & set(LIVE_PATHS))
        self.assertEqual(raw["schema"], 3)
        self.assertEqual(raw["scope"]["comparison_contract"], LIVE_CONTRACT)
        self.update_live_three("before-apply")
        self.backend.on_write = lambda target: self.update_live_three("during-write")
        self.assertEqual(self.transaction.apply(sha)["reason"], "applied")
        self.assertEqual(self.transaction.status(sha)["matched"], "step_23")
        self.update_live_three("before-rollback")
        self.assertEqual(self.transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(self.backend.current, raw["initial"]))
        self.assertEqual(self.vault.read_bytes("plan.dpapi"), cipher)
        self.assertEqual(self.vault.read("plan.dpapi"), original)
        self.assertFalse(set(self.backend.writes) & set(LIVE_PATHS))
        for row in LIVE_CONTRACT["entries"]:
            for field in row["fields"]:
                self.assertNotEqual(self.backend.current["entries"][row["path"]][field],
                                    raw["initial"]["entries"][row["path"]][field])

    def test_live_three_partial_rollback_accepts_only_whole_owned_acl(self):
        sha = self.prepare()
        cipher = self.vault.read_bytes("plan.dpapi")
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        self.update_live_three("after-partial")
        owned = copy.deepcopy(self.backend.current)
        # A live update must never license an outside whole-object ACL.
        for path in LIVE_PATHS:
            for field, value in (("dacl", "foreign"), ("explicit", ["foreign"]),
                                 ("owner", "S-1-5-21-999"), ("group", "S-1-5-18")):
                foreign = copy.deepcopy(owned)
                foreign["entries"][path][field] = value
                with self.subTest(path=path, field=field):
                    self.assertFalse(acl.own_partial(foreign, snapshot(),
                        acl.materialize(self.vault.read("plan.dpapi"), 1)))
        self.assertEqual(self.transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(self.backend.current, snapshot()))
        self.assertEqual(self.vault.read_bytes("plan.dpapi"), cipher)
        for row in LIVE_CONTRACT["entries"]:
            for field in row["fields"]:
                self.assertEqual(self.backend.current["entries"][row["path"]][field],
                                 "after-partial:" + field)

    def test_live_contract_exact_fields_are_part_of_plan_binding(self):
        for mutate in (
            lambda rows: rows[1]["fields"].append("identity"),
            lambda rows: rows[-1]["fields"].append("created"),
            lambda rows: rows[-1].update(path="data/recovery-events.1.tmp"),
            lambda rows: rows[-1].update(kind="directory"),
            lambda rows: rows[-1]["fields"].reverse(),
        ):
            bad = scope()
            mutate(bad["comparison_contract"]["entries"])
            with self.subTest(contract=bad["comparison_contract"]):
                self.assertBlocked("unapproved_comparison_contract", lambda: acl.validate_scope(bad))
        sha = self.prepare()
        plan = self.vault.read("plan.dpapi")
        plan["scope"]["comparison_contract"]["entries"][-1]["fields"].append("created")
        bad_sha = self.vault.write("plan.dpapi", plan)
        state = self.vault.read("state.dpapi")
        state["plan_sha256"] = bad_sha
        self.vault.write("state.dpapi", state)
        with mock.patch.object(self.backend, "open") as opened:
            self.assertBlocked("unapproved_comparison_contract", lambda: self.transaction.status(bad_sha))
            opened.assert_not_called()
        self.assertFalse(self.backend.writes)

    def test_schema_two_plan_and_state_are_rejected_before_native_open(self):
        self.prepare()
        plan = self.vault.read("plan.dpapi")
        state = self.vault.read("state.dpapi")
        plan["schema"] = plan["scope"]["schema"] = state["schema"] = 2
        plan["scope"]["comparison_contract"] = copy.deepcopy(MODIFIED_CONTRACT)
        old_sha = self.vault.write("plan.dpapi", plan)
        state["plan_sha256"] = old_sha
        self.vault.write("state.dpapi", state)
        original_cipher = self.vault.read_bytes("plan.dpapi")
        for action in (self.transaction.status, self.transaction.apply, self.transaction.rollback):
            with self.subTest(action=action.__name__), mock.patch.object(self.backend, "open") as opened:
                self.assertBlocked("journal_plan_mismatch", lambda: action(old_sha))
                opened.assert_not_called()
        self.assertFalse(self.backend.writes)
        self.assertEqual(self.vault.read_bytes("plan.dpapi"), original_cipher)

    def test_old_plan_schema_is_not_reinterpreted(self):
        sha = self.prepare()
        plan = self.vault.read("plan.dpapi")
        plan["schema"] = 1
        old_sha = self.vault.write("plan.dpapi", plan)
        state = self.vault.read("state.dpapi")
        state["plan_sha256"] = old_sha
        self.vault.write("state.dpapi", state)
        self.assertBlocked("journal_plan_mismatch", lambda: self.transaction.status(old_sha))
        self.assertFalse(self.backend.writes)

    def test_prepare_is_readonly_and_compact_encrypted(self):
        sha = self.prepare()
        self.assertFalse(self.backend.writes)
        plan = self.vault.read("plan.dpapi")
        self.assertEqual(len(plan["steps"]), 23)
        self.assertNotIn("final", plan)
        self.assertNotIn("before", plan["steps"][0])
        self.assertEqual(len(plan["steps"][1]["changes"]), 1)
        self.assertEqual(acl.snapshot_hash(acl.materialize(plan, 23)), plan["final_sha256"])
        cipher = self.vault.read_bytes("plan.dpapi")
        self.assertNotIn(b"originalSD", cipher)
        self.assertEqual(acl.digest(cipher), sha)
        for name in ("plan.dpapi", "state.dpapi"):
            self.assertEqual(stat.S_IMODE((self.vault.path / name).stat().st_mode), 0o600)

    def test_forward_and_exact_rollback_preserve_ignored_owner_explicit_content(self):
        initial = self.backend.snapshot()
        sha = self.prepare()
        self.assertEqual(self.transaction.apply(sha)["targets"], 23)
        for path in acl.RUNTIME:
            self.assertTrue(self.backend.current["entries"][path]["protected"])
        ignored = self.backend.current["entries"]["ignored"]
        self.assertEqual(ignored["owner"], initial["entries"]["ignored"]["owner"])
        self.assertEqual(ignored["explicit"], initial["entries"]["ignored"]["explicit"])
        self.assertEqual(self.transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(self.backend.current, initial))
        self.assertEqual(len(self.backend.writes), 46)

    def test_observed_git_directory_inventory_requires_23_targets(self):
        targets = acl.validate_snapshot(snapshot(), scope())
        git = [p for p in targets if p == ".git" or p.startswith(".git/")]
        self.assertEqual(len(targets), 23)
        self.assertEqual(len(git), 18)
        self.assertIn(".git", git)
        self.assertTrue(all(snapshot()["entries"][p]["kind"] == "directory" for p in git))

    def test_git_directory_contract_rejects_missing_root_or_unsafe_file(self):
        for kind in ("safe_git_root", "unsafe_git_file"):
            current = snapshot()
            if kind == "safe_git_root":
                current["entries"][".git"]["unsafe"] = False
            else:
                current["entries"][".git/other0"].update(kind="file", attributes=32)
            self.assertBlocked("git_directory_inventory_changed",
                               lambda: acl.validate_snapshot(current, scope()))

    def test_nested_directory_projection_and_partial_rollback_preserve_owners(self):
        self.backend.current["entries"][".git"]["owner"] = "S-1-5-18"
        self.backend.current["entries"][".git/objects"]["owner"] = "S-1-5-32-544"
        initial = self.backend.snapshot()
        sha = self.prepare()
        plan = self.vault.read("plan.dpapi")
        step = next(s for s in plan["steps"] if s["target"] == ".git/objects")
        leaf = ".git/objects/pack/value"
        self.assertIn(leaf, step["changes"])
        self.assertEqual(step["changes"][leaf]["explicit"], initial["entries"][leaf]["explicit"])
        self.transaction.apply(sha)
        original_set = self.backend.set_dacl
        mixed = []
        def partial_nested(target, approved):
            before = self.backend.snapshot()
            original_set(target, approved)
            if target == ".git/objects" and not approved["protected"]:
                self.backend.current["entries"][leaf] = before["entries"][leaf]
                mixed.append(target)
        self.backend.set_dacl = partial_nested
        self.transaction.rollback(sha)
        self.assertEqual(mixed, [".git/objects"])
        self.assertIn(leaf, self.backend.writes)
        self.assertTrue(acl.same_snapshot(self.backend.current, initial))

    def test_protected_nested_git_directory_stops_parent_propagation(self):
        initial = snapshot()
        protected = self.backend.project(initial, ".git", None)
        changed = self.backend.project(protected, "", None)
        self.assertEqual(changed["entries"][".git/objects/pack/value"],
                         protected["entries"][".git/objects/pack/value"])
        self.assertNotEqual(changed["entries"]["ignored"]["dacl"],
                            protected["entries"]["ignored"]["dacl"])

    def test_first_set_has_durable_original_and_intent(self):
        sha = self.prepare()
        def inspect(_):
            state = self.vault.read("state.dpapi")
            self.assertEqual(state["phase"], "applying")
            self.assertIsNotNone(state["pending"])
            self.assertEqual(acl.digest(self.vault.read_bytes("plan.dpapi")), sha)
        self.backend.on_write = inspect
        self.transaction.apply(sha)

    def test_reviewed_hash_required(self):
        self.prepare()
        self.assertBlocked("plan_pin_mismatch", lambda: self.transaction.apply("0" * 64))
        self.assertFalse(self.backend.writes)

    def test_prepare_does_not_overwrite_existing_plan(self):
        self.prepare()
        old = self.vault.read_bytes("plan.dpapi")
        self.assertBlocked("vault_already_prepared", self.prepare)
        self.assertEqual(old, self.vault.read_bytes("plan.dpapi"))

    def test_default_native_is_blocked_without_guard(self):
        sha = self.prepare()
        self.backend.mutation_authorized = False
        self.assertBlocked("native_fixture_and_guard_required", lambda: self.transaction.apply(sha))
        self.assertFalse(self.backend.writes)

    def test_scope_count_change_blocks_before_plan(self):
        self.backend.current["counts"]["git_unsafe"] -= 1
        self.assertBlocked("scope_counts_changed", self.prepare)
        self.assertFalse(self.vault.exists("plan.dpapi"))

    def test_file_identity_replacement_blocks_apply(self):
        sha = self.prepare()
        self.backend.current["entries"]["tracked.py"]["identity"] = "foreign"
        self.assertBlocked("preapply_drift", lambda: self.transaction.apply(sha))
        self.assertFalse(self.backend.writes)

    def test_runtime_hash_drift_blocks(self):
        sha = self.prepare()
        self.backend.current["runtime_hashes"][".env"] = "2" * 64
        self.assertBlocked("preapply_drift", lambda: self.transaction.apply(sha))

    def test_new_deleted_paths_block(self):
        sha = self.prepare()
        del self.backend.current["entries"]["tracked.py"]
        self.assertBlocked("preapply_drift", lambda: self.transaction.apply(sha))

    def test_reparse_and_hardlink_are_rejected(self):
        for field, value, reason in (("attributes", 1024, "reparse_point_forbidden"),
                                     ("links", 2, "hardlink_forbidden")):
            with self.subTest(field=field):
                bad = snapshot()
                bad["entries"]["tracked.py"][field] = value
                self.assertBlocked(reason, lambda: acl.validate_snapshot(bad, scope()))

    def test_lost_response_before_or_after_never_retries(self):
        for mode in ("lost_before", "lost_after"):
            with self.subTest(mode=mode):
                if self.vault.exists("plan.dpapi"):
                    self.vault.close()
                    self.backend = FakeBackend()
                    self.vault = acl.Vault(Path(self.temp.name).resolve() / "vault-after", self.backend)
                    self.transaction = acl.Transaction(self.backend, self.vault)
                sha = self.prepare()
                self.backend.mode = mode
                self.assertBlocked("native_response_unknown", lambda: self.transaction.apply(sha))
                self.assertBlocked("unfinished_transaction", lambda: self.transaction.apply(sha))
                self.assertBlocked("unknown_native_process_held", lambda: self.transaction.rollback(sha))
                self.assertEqual(len(self.backend.writes), 1)
                self.assertTrue(self.vault.read("state.dpapi")["unknown"])

    def test_unknown_after_readback_status_does_not_clear(self):
        sha = self.prepare()
        self.backend.mode = "lost_after"
        self.assertBlocked("native_response_unknown", lambda: self.transaction.apply(sha))
        result = self.transaction.status(sha)
        self.assertEqual(result["matched"], "step_1")
        self.assertTrue(result["unknown"])
        self.assertIsNotNone(self.vault.read("state.dpapi")["pending"])

    def test_crash_missing_response_needs_quiescence_proof(self):
        sha = self.prepare()
        self.backend.mode = "crash"
        with self.assertRaises(SystemExit):
            self.transaction.apply(sha)
        self.assertBlocked("unresolved_intent_held", lambda: self.transaction.rollback(sha))
        self.assertEqual(len(self.backend.writes), 1)
        self.backend.mode = None
        self.backend.recovery_quiescent = True
        self.transaction.rollback(sha)
        self.assertTrue(acl.same_snapshot(self.backend.current, snapshot()))

    def test_partial_root_propagation_can_be_owned_and_rolled_back(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        self.transaction.rollback(sha)
        self.assertTrue(acl.same_snapshot(self.backend.current, snapshot()))

    def test_partial_rollback_restart_retains_provenance_until_inverse_intent(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        original_write = self.vault.write

        def crash_before_inverse(name, value, **kwargs):
            if name == "state.dpapi" and value["phase"] == "rolling_back":
                raise SystemExit("crash_before_inverse_fsync")
            return original_write(name, value, **kwargs)

        with mock.patch.object(self.vault, "write", side_effect=crash_before_inverse):
            with self.assertRaises(SystemExit):
                self.transaction.rollback(sha)
        durable = self.vault.read("state.dpapi")
        self.assertEqual(durable["pending"]["direction"], "apply")
        self.assertTrue(durable["pending"]["response_complete"])
        fresh = FakeBackend()
        fresh.current = copy.deepcopy(self.backend.current)
        transaction = acl.Transaction(fresh, self.vault)
        self.assertEqual(transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(fresh.current, snapshot()))

    def test_partial_inverse_intent_crash_still_requires_quiescence(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        original_write = self.vault.write

        def crash_after_inverse(name, value, **kwargs):
            result = original_write(name, value, **kwargs)
            if name == "state.dpapi" and value["phase"] == "rolling_back":
                raise SystemExit("crash_after_inverse_fsync")
            return result

        with mock.patch.object(self.vault, "write", side_effect=crash_after_inverse):
            with self.assertRaises(SystemExit):
                self.transaction.rollback(sha)
        fresh = FakeBackend()
        fresh.current = copy.deepcopy(self.backend.current)
        transaction = acl.Transaction(fresh, self.vault)
        self.assertBlocked("unresolved_intent_held", lambda: transaction.rollback(sha))
        self.assertFalse(fresh.writes)
        fresh.recovery_quiescent = True
        self.assertEqual(transaction.rollback(sha)["reason"], "rolled_back")
        self.assertTrue(acl.same_snapshot(fresh.current, snapshot()))

    def test_foreign_acl_in_partial_state_is_never_overwritten(self):
        sha = self.prepare()
        self.backend.mode = "partial"
        self.assertBlocked("propagation_or_metadata_drift", lambda: self.transaction.apply(sha))
        self.backend.current["entries"]["ignored"]["dacl"] = "foreign ACL"
        self.assertBlocked("partial_or_foreign_state", lambda: self.transaction.rollback(sha))
        self.assertEqual(len(self.backend.writes), 1)

    def test_known_completed_error_can_rollback_without_unknown_retry(self):
        sha = self.prepare()
        self.backend.mode = "known_error"
        self.assertBlocked("native_acl_failure", lambda: self.transaction.apply(sha))
        self.assertTrue(self.vault.read("state.dpapi")["pending"]["response_complete"])
        self.transaction.rollback(sha)
        self.assertEqual(len(self.backend.writes), 1)  # request proved no state change
        self.assertIsNone(self.vault.read("state.dpapi")["pending"])

    def test_partial_rollback_repairs_only_owned_children(self):
        sha = self.prepare()
        self.transaction.apply(sha)
        original_set = self.backend.set_dacl
        def partial_root(target, approved):
            if target == "":
                self.backend.mode = "partial"
            original_set(target, approved)
        self.backend.set_dacl = partial_root
        self.transaction.rollback(sha)
        self.assertTrue(acl.same_snapshot(self.backend.current, snapshot()))
        self.assertGreater(len(self.backend.writes), 46)

    def test_restarted_rollback_keeps_original_git_target_inventory(self):
        sha = self.prepare()
        self.transaction.apply(sha)
        fresh = FakeBackend()
        fresh.current = self.backend.current
        fresh.current["counts"]["git_unsafe"] = 0
        transaction = acl.Transaction(fresh, self.vault)
        self.assertEqual(transaction.status(sha)["matched"], "step_23")
        transaction.rollback(sha)
        self.assertEqual(len([p for p in fresh.recovery_targets if p.startswith(".git/")]), 17)
        self.assertIn(".git", fresh.recovery_targets)
        self.assertTrue(acl.same_snapshot(fresh.current, snapshot()))

    def test_restart_safe_but_changed_ancestor_blocks_every_phase_before_write(self):
        sha = self.prepare()
        self.assertEqual(self.vault.read("plan.dpapi")["ancestor_sha256"], "a" * 64)
        fresh = FakeBackend()
        fresh.current = copy.deepcopy(self.backend.current)
        fresh.ancestor_sha256 = "b" * 64
        transaction = acl.Transaction(fresh, self.vault)
        for operation in (transaction.status, transaction.apply, transaction.rollback):
            self.assertBlocked("ancestor_pin_mismatch", lambda: operation(sha))
        self.assertFalse(fresh.writes)

    def test_prepare_rejects_missing_ancestor_pin_before_journal(self):
        self.backend.ancestor_sha256 = ""
        self.assertBlocked("ancestor_guard_missing", self.prepare)
        self.assertFalse(self.backend.writes)
        self.assertFalse((self.vault.path / "plan.dpapi").exists())

    def test_owner_explicit_projection_changes_rejected(self):
        before = snapshot()
        for field, value in (("owner", "foreign"), ("explicit", ["changed"]), ("protected", True)):
            after = copy.deepcopy(before)
            after["entries"]["ignored"][field] = value
            reason = "projection_metadata_changed" if field == "owner" else "unapproved_explicit_change"
            self.assertBlocked(reason, lambda: acl.validate_projection(before, after, ""))

    def test_failure_limit_blocks_rollback(self):
        sha = self.prepare()
        self.transaction.apply(sha)
        state = self.vault.read("state.dpapi")
        state["failures"]["native_acl_failure"] = 3
        self.vault.write("state.dpapi", state)
        self.assertBlocked("failure_limit", lambda: self.transaction.rollback(sha))

    def test_atomic_failed_save_retains_previous_intent(self):
        self.prepare()
        before = self.vault.read_bytes("state.dpapi")
        with mock.patch.object(acl.os, "replace", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                self.vault.write("state.dpapi", {"phase": "changed"})
        self.assertEqual(self.vault.read_bytes("state.dpapi"), before)

    def test_vault_rejects_symlink_and_broad_modes(self):
        file = self.vault.path / "unsafe"
        file.symlink_to(self.vault.path / "state.dpapi")
        with self.assertRaises(OSError):
            self.vault.read_bytes("unsafe")
        file.unlink()
        file.write_bytes(b"x")
        file.chmod(0o644)
        self.assertBlocked("unsafe_vault_file", lambda: self.vault.read_bytes("unsafe"))

    def test_native_source_has_handle_only_dacl_setter_and_short_bootstrap(self):
        source = PATH.read_text()
        self.assertNotIn("SetNamedSecurityInfo", acl.POWERSHELL)
        self.assertIn("SetSecurityInfo(handle,1,flags,IntPtr.Zero,IntPtr.Zero", acl.POWERSHELL)
        self.assertIn("0x00020080u", acl.POWERSHELL)
        self.assertIn("0x02200000", acl.POWERSHELL)
        self.assertIn("writeTargets.Contains(p)", acl.POWERSHELL)
        self.assertIn("if(target || !(bool)row[\"protected\"])", acl.POWERSHELL)
        self.assertNotIn("Directory.Delete", acl.POWERSHELL)
        self.assertIn("base64.b64encode(self.script.encode", source)
        self.assertIn("stderr=subprocess.DEVNULL", source)

    def test_scope_rejects_traversal_and_unapproved_root(self):
        for bad_path in ("../secret", "a\\b", "C:/outside", "a//b"):
            bad = scope()
            bad["tracked_paths"] = [bad_path]
            self.assertBlocked("invalid_relative_path", lambda: acl.validate_scope(bad))
        bad = scope()
        bad["root"] = "C:\\other"
        self.assertBlocked("unapproved_root", lambda: acl.validate_scope(bad))

    def test_private_ancestor_symlink_and_writable_parent_rejected(self):
        parent = Path(self.temp.name).resolve()
        unsafe = parent / "unsafe-parent"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        self.assertBlocked("unsafe_private_ancestor", lambda: acl.Vault(unsafe / "vault", self.backend))
        link = parent / "parent-link"
        link.symlink_to(parent, target_is_directory=True)
        with self.assertRaises(OSError):
            acl.Vault(link / "vault", self.backend)

    def test_native_guard_requires_each_concrete_proof(self):
        native = object.__new__(acl.NativeBackend)
        proof = {key: True for key in ("source_pin", "ci_verified", "sequence_lock", "writers_idle")}
        native.guard = lambda _: proof
        native.check_guard(scope())
        for key in proof:
            bad = {**proof, key: False}
            native.guard = lambda _, bad=bad: bad
            self.assertBlocked("trusted_operational_guard_required", lambda: native.check_guard(scope()))

    def test_unknown_native_refuses_another_process(self):
        native = object.__new__(acl.NativeBackend)
        native.unknown = True
        with mock.patch.object(acl.subprocess, "Popen") as popen:
            self.assertBlocked("native_process_held", lambda: native.request({"op": "snapshot"}))
        popen.assert_not_called()

    def test_mutation_invalid_frame_is_unknown_and_latched(self):
        read_fd, write_fd = os.pipe()
        input_read, input_write = os.pipe()
        os.write(write_fd, b"{}\n{}\n")
        native = object.__new__(acl.NativeBackend)
        native.process = mock.Mock()
        native.process.stdin = os.fdopen(input_write, "wb", buffering=0)
        native.process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        native.startup, native.unknown, native.timeout = b"", False, 1
        try:
            self.assertBlocked("native_response_unknown", lambda: native.request({"op": "set"}, mutation=True))
            self.assertTrue(native.unknown)
        finally:
            native.process.stdin.close()
            native.process.stdout.close()
            os.close(input_read)
            os.close(write_fd)

    def test_close_retains_controller_until_windows_process_exits(self):
        native = object.__new__(acl.NativeBackend)
        native.process = mock.Mock()
        native.process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 10), 0]
        native.process.poll.side_effect = [None, 0]
        native.crypto = None
        native.unknown = False
        observed = []
        native.on_held = lambda reason: observed.append(reason)
        with mock.patch("builtins.print"):
            self.assertBlocked("native_process_termination_unknown", native.close)
        self.assertTrue(native.unknown)
        self.assertEqual(observed, ["native_process_termination_unknown"])
        self.assertEqual(native.process.wait.call_count, 2)

    def test_session_close_unknown_is_durable_even_after_applied(self):
        sha = self.prepare()
        self.transaction.apply(sha)
        self.transaction.session_held("native_process_termination_unknown")
        state = self.vault.read("state.dpapi")
        self.assertTrue(state["unknown"])
        self.assertEqual(state["phase"], "held")
        self.assertBlocked("unknown_native_process_held", lambda: self.transaction.rollback(sha))

    def test_crypto_remains_available_after_mutation_session_unknown(self):
        native = object.__new__(acl.NativeBackend)
        native.unknown = True
        native.crypto = mock.Mock()
        native.crypto.request.return_value = {"data": "Y2lwaGVy"}
        self.assertEqual(native.encrypt(b"private journal"), b"cipher")
        self.assertEqual(native.crypto.request.call_args.args[0]["op"], "encrypt")

    def test_failed_held_fsync_and_broken_stdout_still_wait_for_exit(self):
        native = object.__new__(acl.NativeBackend)
        native.process = mock.Mock()
        native.process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 10), 0]
        native.process.poll.side_effect = [None, 0]
        native.crypto = None
        native.unknown = False
        native.on_held = mock.Mock(side_effect=OSError("synthetic fsync failure"))
        with mock.patch("builtins.print", side_effect=BrokenPipeError):
            self.assertBlocked("native_process_termination_unknown", native.close)
        self.assertEqual(native.process.wait.call_count, 2)
        native.on_held.assert_called_once()

    def test_readonly_temp_probe_exposes_only_allowlisted_fields(self):
        native = object.__new__(acl.NativeBackend)
        native.opened = False
        native.request = mock.Mock(return_value={"reason": "native_temp_checked", "ancestors": [
            {"index": 0, "opened": True, "owner_trusted": True,
             "acl_supported": True, "dangerous_allow": 0}]})
        self.assertEqual(native.native_temp_check()["reason"], "native_temp_checked")
        native.request.assert_called_once_with({"op": "temp_check"})
        native.request.return_value["ancestors"][0]["path"] = "must not escape"
        self.assertBlocked("invalid_temp_check", native.native_temp_check)

    def test_private_fixture_parent_probe_is_distinct_from_temp_and_does_not_relax_guard(self):
        native = object.__new__(acl.NativeBackend)
        native.opened = False
        native.request = mock.Mock(return_value={"reason": "native_temp_checked", "ancestors": []})
        native.native_fixture_parent_check()
        native.request.assert_called_once_with({"op": "fixture_parent_check"})
        self.assertIn("Environment.SpecialFolder.LocalApplicationData", acl.POWERSHELL)
        self.assertIn('string temp=FixtureParent();', acl.POWERSHELL)
        self.assertIn('FixtureStage="ancestor_acl";AncestorPin(h);', acl.POWERSHELL)
        self.assertNotIn("SetEnvironmentVariable", acl.POWERSHELL)

    def test_native_fixture_requires_actual_powershell_original_boundary_proof(self):
        nonce = b"n" * 32
        receipt = {"nonce": nonce.hex(), "binding_sha256": "a" * 64, "objects": 23,
                   "projection": True, "rollback": True, "partialmix": True,
                   "content_hashes": True, "dpapi": True, "cleanup": True,
                   "sacl_contract": "dacl_only_no_privilege_change",
                   "utc": acl.datetime.now(acl.timezone.utc).isoformat()}
        for boundary in (None, False):
            native = object.__new__(acl.NativeBackend)
            native.opened = False
            observed = dict(receipt)
            if boundary is not None:
                observed["ps_original_boundary"] = boundary
            native.request = mock.Mock(return_value=observed)
            with self.subTest(boundary=boundary), mock.patch.object(acl.os, "urandom", return_value=nonce):
                self.assertBlocked("native_fixture_failed", native.native_self_test)
        native.request.return_value = {**receipt, "ps_original_boundary": True}
        with mock.patch.object(acl.os, "urandom", return_value=nonce):
            self.assertEqual(native.native_self_test()["reason"], "native_fixture_passed")

    def test_native_projection_preserves_null_and_exact_nonempty_restore_descriptor(self):
        native = object.__new__(acl.NativeBackend)
        native.request = mock.Mock(return_value={"projected": True})
        original = {"sd": "nonempty-descriptor-bytes", "owner": "synthetic-owner"}
        for value in (None, original):
            native.project({"synthetic": True}, "target", value)
            sent = native.request.call_args.args[0]
            self.assertIs(sent["original"], value)
        self.assertEqual(original["sd"], "nonempty-descriptor-bytes")

    def test_native_projection_rejects_empty_or_missing_restore_before_request(self):
        native = object.__new__(acl.NativeBackend)
        native.request = mock.Mock()
        for original in ({}, {"sd": ""}, {"sd": None}, {"sd": False}, ""):
            with self.subTest(original=original):
                self.assertBlocked("invalid_project_original",
                                   lambda: native.project({"synthetic": True}, "target", original))
        native.request.assert_not_called()


class NativeDiagnosticTests(unittest.TestCase):
    """Frames and CLI use synthetic pipes/backends; no Windows process starts."""
    def diagnostic(self, **changes):
        return {"stage": "set_security_info", "win32_error": 5,
                "path_sha256": "a" * 64, **changes}

    def request_error(self, diagnostic=None, *, reason="native_acl_failure", extra=None):
        read_fd, write_fd = os.pipe()
        input_read, input_write = os.pipe()
        frame = {"error": reason, **(extra or {})}
        if diagnostic is not None:
            frame["diagnostic"] = diagnostic
        os.write(write_fd, json.dumps(frame).encode() + b"\n")
        native = object.__new__(acl.NativeBackend)
        native.process = mock.Mock()
        native.process.stdin = os.fdopen(input_write, "wb", buffering=0)
        native.process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        native.startup, native.unknown, native.timeout = b"", False, 1
        native.last_native_failure = self.diagnostic(stage="invariant", win32_error=None)
        try:
            with self.assertRaises(acl.Blocked) as raised:
                native.request({"op": "set"}, mutation=True)
            self.assertEqual(raised.exception.reason, reason if reason == "native_acl_failure" else "native_acl_failure")
            self.assertFalse(native.unknown)
            return native.last_native_failure
        finally:
            native.process.stdin.close()
            native.process.stdout.close()
            os.close(input_read)
            os.close(write_fd)

    def test_known_error_carries_only_validated_diagnostic_without_changing_reason(self):
        for code in (0, 5, 2 ** 32 - 1):
            with self.subTest(code=code):
                diagnostic = self.diagnostic(win32_error=code)
                self.assertEqual(self.request_error(diagnostic), diagnostic)
        logical = self.diagnostic(stage="invariant", win32_error=None, path_sha256=None)
        self.assertEqual(self.request_error(logical), logical)

    def test_invalid_diagnostic_is_not_trusted_or_exposed(self):
        for value in ("synthetic_private_token", [], {},
                      self.diagnostic(stage="synthetic_private_token"),
                      self.diagnostic(win32_error=True), self.diagnostic(win32_error=-1),
                      self.diagnostic(win32_error=2 ** 32), self.diagnostic(win32_error="5"),
                      self.diagnostic(stage="invariant", win32_error=5),
                      self.diagnostic(path_sha256="C:\\synthetic_private_path"),
                      self.diagnostic(extra="synthetic_private_token")):
            with self.subTest(value=value):
                self.assertIsNone(self.request_error(value))

    def test_absent_or_unrecognized_error_does_not_reuse_old_diagnostic(self):
        self.assertIsNone(self.request_error())
        self.assertIsNone(self.request_error(self.diagnostic(), reason="synthetic_private_token"))
        self.assertIsNone(self.request_error(self.diagnostic(), extra={"private": "synthetic_private_token"}))
        for error in (None, [], {}, True, 5):
            with self.subTest(error=error):
                self.assertIsNone(self.request_error(self.diagnostic(), reason=error))

    def run_main_failure(self, *, close_failure=False, diagnostic=None):
        events, output = [], []
        backend = mock.Mock()
        backend.last_native_failure = self.diagnostic() if diagnostic is None else diagnostic
        def close():
            events.append("close")
            if close_failure:
                raise acl.Blocked("native_process_termination_unknown")
        backend.close.side_effect = close
        transaction = mock.Mock()
        transaction.status.side_effect = acl.Blocked("native_acl_failure")
        def capture(value):
            events.append("print")
            output.append(json.loads(value))
        with mock.patch.object(acl, "sequence_lock") as lock, \
             mock.patch.object(acl, "NativeBackend", return_value=backend), \
             mock.patch.object(acl, "Vault"), \
             mock.patch.object(acl, "Transaction", return_value=transaction), \
             mock.patch("builtins.print", side_effect=capture):
            lock.return_value.__enter__.return_value = 1
            result = acl.main(["--status", "--vault", "synthetic-vault"])
        self.assertEqual(result, 2)
        self.assertEqual(events, ["close", "print"])
        return output[0]

    def test_cli_outputs_diagnostic_only_after_successful_close(self):
        result = self.run_main_failure()
        self.assertEqual(result, {"reason": "native_acl_failure", "diagnostic": self.diagnostic()})
        result = self.run_main_failure(close_failure=True)
        self.assertEqual(result, {"reason": "native_process_termination_unknown"})

    def test_cli_revalidates_cache_and_never_prints_private_values(self):
        result = self.run_main_failure(diagnostic=self.diagnostic(stage="synthetic_private_token"))
        self.assertEqual(result, {"reason": "native_acl_failure"})

    def test_source_captures_api_results_and_never_serializes_exception(self):
        for text in ("uint error=SetSecurityInfo(handle,1,flags", "uint error=GetSecurityInfo(h,1,7",
                     "Marshal.GetLastWin32Error()", "[BoundAcl]::ResetDiagnostic()",
                     "[BoundAcl]::FailureDiagnostic()", "windows-acl-relative-path-v1\\0"):
            self.assertTrue(text in acl.POWERSHELL, text)
        self.assertFalse("GetLastWin32Error());Fail(\"get_security_info\"" in acl.POWERSHELL)
        self.assertFalse("GetLastWin32Error());Fail(\"set_security_info\"" in acl.POWERSHELL)

    def test_diagnostic_values_are_detached_and_all_api_stages_are_bounded(self):
        for stage in acl.NATIVE_DIAGNOSTIC_STAGES:
            value = self.diagnostic(stage=stage, win32_error=None if stage in acl.NATIVE_INVARIANT_STAGES else 5)
            result = acl.native_failure_diagnostic(value)
            self.assertEqual(result, value)
            self.assertIsNot(result, value)
        for path in (True, 5, "A" * 64, "a" * 63, "a" * 65):
            self.assertIsNone(acl.native_failure_diagnostic(self.diagnostic(path_sha256=path)))

    def test_inventory_and_identity_failures_use_fixed_stage_with_no_win32_error(self):
        for stage in ("inventory_unpinned_entry", "inventory_identity_mismatch", "inventory_set_mismatch",
                      "row_identity_mismatch", "set_identity_mismatch"):
            with self.subTest(stage=stage):
                value = self.diagnostic(stage=stage, win32_error=None)
                self.assertEqual(self.request_error(value), value)
                self.assertIsNone(acl.native_failure_diagnostic(self.diagnostic(stage=stage, win32_error=5)))
                self.assertTrue('Fail("' + stage + '"' in acl.POWERSHELL)

class ResidualTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.backend.scope_schema = acl.RESIDUAL_SCHEMA
        for p in acl.READONLY_PATHS:
            self.backend.current = self.backend.project(self.backend.current, p, None)
        initial = self.backend.current
        initial['entries'] = {p: r for p, r in initial['entries'].items()
                              if p in acl.READONLY_PATHS or p in acl.RUNTIME or acl.residual_path(p)}
        initial['projection_contract'] = acl.RESIDUAL_PROJECTION
        rows = initial['entries']
        counts = dict(COUNTS)
        counts.update(total=len(rows), files=sum(r['kind'] == 'file' for r in rows.values()),
                      directories=sum(r['kind'] == 'directory' for r in rows.values()),
                      ignored_unsafe=0, other_owner=0)
        initial['counts'] = counts
        self.git = sorted(p for p, row in rows.items() if acl.residual_path(p) and row['unsafe'])
        self.prior = mock.patch.object(acl, 'PRIOR_TARGETS_SHA256',
                                      acl.object_hash([''] + list(acl.RUNTIME) + self.git))
        self.prior.start()
        self.scope = dict(schema=4, scope_kind=acl.RESIDUAL_KIND, root='C:\\personal-server',
                          git_targets=self.git, prior_targets_sha256=acl.PRIOR_TARGETS_SHA256,
                          counts=counts, source_sha256='1'*64, fixture_sha256=acl.digest(acl.POWERSHELL.encode()),
                          comparison_contract=copy.deepcopy(acl.RESIDUAL_COMPARISON),
                          initial_sha256=acl.snapshot_hash(initial), runtime_hashes=dict(initial['runtime_hashes']))
        self.temp = tempfile.TemporaryDirectory(prefix='.windows-acl-residual-test-', dir=PATH.parents[3].resolve())
        self.vault = acl.Vault(Path(self.temp.name).resolve() / 'vault', self.backend)
        self.transaction = acl.ResidualTransaction(self.backend, self.vault)

    def tearDown(self):
        self.vault.close()
        self.temp.cleanup()
        self.prior.stop()

    def prepare(self):
        return self.transaction.prepare(self.scope)['plan_sha256']

    def test_legacy_backend_cannot_open_residual_scope(self):
        with self.assertRaisesRegex(acl.Blocked, 'residual_backend_required'):
            acl.ResidualTransaction(FakeBackend(), self.vault)
        native = object.__new__(acl.NativeBackend)
        with self.assertRaisesRegex(acl.Blocked, 'native_scope_schema_mismatch'):
            native.open(self.scope)

    def test_only_twenty_targets_and_exact_roundtrip(self):
        readonly = {p: copy.deepcopy(self.backend.current['entries'][p]) for p in acl.READONLY_PATHS}
        sha = self.prepare()
        self.assertEqual(self.transaction.apply(sha)['targets'], 20)
        self.assertEqual(self.backend.writes, list(acl.RESIDUAL_RUNTIME) + self.git)
        for p, row in readonly.items():
            self.assertEqual(self.backend.current['entries'][p], row)
        self.transaction.rollback(sha)
        self.assertEqual(self.backend.writes[20:], list(reversed(list(acl.RESIDUAL_RUNTIME) + self.git)))
        self.assertTrue(acl.same_snapshot(self.backend.current, self.vault.read('plan.dpapi')['initial']))

    def test_schema_three_rejected_before_native_open_or_old_journal_read(self):
        with mock.patch.object(self.backend, 'open') as opened:
            with self.assertRaisesRegex(acl.Blocked, 'transaction_schema_mismatch'):
                self.transaction.prepare(scope())
            opened.assert_not_called()
        sha = self.prepare()
        with self.assertRaisesRegex(acl.Blocked, 'journal_plan_mismatch'):
            acl.Transaction(self.backend, self.vault).load(sha)

    def test_lineage_digest_binds_actual_git_list(self):
        changed = copy.deepcopy(self.scope)
        changed['git_targets'][-1] += 'different'
        changed['git_targets'].sort()
        with self.assertRaisesRegex(acl.Blocked, 'residual_target_lineage_mismatch'):
            acl.validate_residual_scope(changed)

    def test_data_inventory_and_git_locks_fail_closed(self):
        for path in ('data', '.git/index.lock'):
            changed = copy.deepcopy(self.backend.current)
            changed['entries'][path] = entry(path, 999)
            changed['counts']['total'] += 1
            changed['counts']['files'] += 1
            approved = copy.deepcopy(self.scope)
            approved['counts'] = changed['counts']
            approved['initial_sha256'] = acl.snapshot_hash(changed)
            with self.subTest(path=path), self.assertRaises(acl.Blocked):
                acl.validate_residual_snapshot(changed, approved)

    def test_reparse_hardlink_alias_and_wrong_git_kind_rejected(self):
        for change in ('reparse', 'links', 'identity', 'kind'):
            current = copy.deepcopy(self.backend.current)
            row = current['entries']['.git/objects/pack/value']
            if change == 'reparse': row['attributes'] |= 1024
            if change == 'links': row['links'] = 2
            if change == 'identity': row['identity'] = current['entries']['']['identity']
            if change == 'kind': current['entries']['.git']['kind'] = 'file'
            approved = copy.deepcopy(self.scope)
            approved['initial_sha256'] = acl.snapshot_hash(current)
            with self.subTest(change=change), self.assertRaises(acl.Blocked):
                acl.validate_residual_snapshot(current, approved)

    def test_readonly_owner_group_acl_metadata_are_pinned(self):
        sha = self.prepare()
        for field in ('identity', 'owner', 'group', 'dacl', 'modified'):
            original = copy.deepcopy(self.backend.current)
            self.backend.current['entries'][''][field] += 'changed'
            self.assertFalse(acl.same_snapshot(self.backend.current, original))
            self.backend.current = original
        self.backend.current['entries']['.env']['protected'] = False
        with self.assertRaisesRegex(acl.Blocked, 'preapply_drift'):
            self.transaction.apply(sha)
        self.assertEqual(self.backend.writes, [])

    def test_outside_projection_and_runtime_hash_mutation_rejected(self):
        before = self.backend.current
        for target in acl.READONLY_PATHS:
            with self.subTest(target=target), self.assertRaisesRegex(acl.Blocked, 'outside_residual_target'):
                acl.validate_projection(before, before, target)
        for field in ('sd', 'dacl', 'owner'):
            after = self.backend.project(before, acl.RESIDUAL_RUNTIME[0], None)
            after['entries'][''][field] += 'changed'
            with self.subTest(field=field), self.assertRaisesRegex(acl.Blocked, 'outside_residual_projection'):
                acl.validate_projection(before, after, acl.RESIDUAL_RUNTIME[0])
        after = copy.deepcopy(before)
        after['runtime_hashes']['.env'] = '0'*64
        with self.assertRaisesRegex(acl.Blocked, 'projection_content_changed'):
            acl.validate_projection(before, after, acl.RESIDUAL_RUNTIME[0])

    def test_unknown_write_preserves_intent_and_stops(self):
        sha = self.prepare()
        self.backend.mode = 'lost_after'
        with self.assertRaisesRegex(acl.Blocked, 'native_response_unknown'):
            self.transaction.apply(sha)
        state = self.vault.read('state.dpapi')
        self.assertTrue(state['unknown'])
        self.assertEqual(state['completed'], 0)
        self.assertEqual(state['pending']['index'], 0)
        self.assertEqual(len(self.backend.writes), 1)
        with self.assertRaisesRegex(acl.Blocked, 'unknown_native_process_held'):
            self.transaction.rollback(sha)

    def test_tampered_plan_target_rejected_before_setter(self):
        sha = self.prepare()
        plan = self.vault.read('plan.dpapi')
        plan['steps'][0]['target'] = ''
        sha = self.vault.write('plan.dpapi', plan)
        state = self.vault.read('state.dpapi');state['plan_sha256'] = sha
        self.vault.write('state.dpapi', state)
        with self.assertRaisesRegex(acl.Blocked, 'residual_plan_targets_changed'):
            self.transaction.apply(sha)
        self.assertEqual(self.backend.writes, [])

    def test_native_open_rejects_wrong_lineage_before_fixture_or_request(self):
        native = object.__new__(acl.ResidualNativeBackend)
        native.native_self_test = mock.Mock()
        native.request = mock.Mock()
        changed = copy.deepcopy(self.scope)
        changed['git_targets'][-1] += 'different'
        changed['git_targets'].sort()
        with self.assertRaisesRegex(acl.Blocked, 'residual_target_lineage_mismatch'):
            native.open(changed)
        native.native_self_test.assert_not_called()
        native.request.assert_not_called()

    def test_native_open_requires_full_scope_before_fixture_or_request(self):
        native = object.__new__(acl.ResidualNativeBackend)
        native.native_self_test = mock.Mock()
        native.request = mock.Mock()
        changed = copy.deepcopy(self.scope)
        changed['initial_sha256'] = None
        with self.assertRaisesRegex(acl.Blocked, 'missing_residual_pin'):
            native.open(changed)
        native.native_self_test.assert_not_called()
        native.request.assert_not_called()

    def test_native_residual_receipt_requires_fresh_partialmix_proof(self):
        from datetime import datetime, timezone
        for partialmix in (None, False, True):
            native = object.__new__(acl.ResidualNativeBackend)
            native.opened = False
            def respond(request, mutation):
                receipt = {key: True for key in ('projection', 'rollback', 'content_hashes',
                            'dpapi', 'cleanup', 'readonly_boundary', 'outside_preserved', 'descriptor_boundary')}
                receipt.update(nonce=request['nonce'], residual_targets=20, binding_sha256='a'*64,
                               objects=26, sacl_contract='dacl_only_no_privilege_change',
                               utc=datetime.now(timezone.utc).isoformat())
                if partialmix is not None:
                    receipt['partialmix'] = partialmix
                return receipt
            native.request = mock.Mock(side_effect=respond)
            with self.subTest(partialmix=partialmix):
                if partialmix is True:
                    self.assertEqual(native.native_self_test()['reason'], 'native_residual_fixture_passed')
                else:
                    with self.assertRaisesRegex(acl.Blocked, 'native_residual_fixture_failed'):
                        native.native_self_test()
        fixture = acl.POWERSHELL.split('public static Dictionary<string,object> ResidualFixture(', 1)[1]
        fixture = fixture.split('public static void Close()', 1)[0]
        self.assertIn('var repairLeaves=new[]{".git/value",".git/target00/value"};', fixture)
        self.assertIn('Check(GitPath(p)&&writable.Contains(p)&&!residualTargets.Contains(p));', fixture)
        self.assertIn('SetHandleFlags(handles[p],From(restrictedDescriptors[p]),4);', fixture)
        self.assertIn('VerifyMix(baseline,originals,restrictedDescriptors);', fixture)
        self.assertIn('Set(p,identities[p],originals[p]);VerifyMix(', fixture)
        self.assertIn('"partialmix",true', fixture)

    def test_native_python_boundaries_reject_without_request(self):
        native = object.__new__(acl.ResidualNativeBackend)
        native.residual_targets = frozenset(list(acl.RESIDUAL_RUNTIME) + self.git)
        native.request = mock.Mock()
        for target in ('', '.env', 'docker-compose.yml', 'data/recovery.lock'):
            with self.subTest(target=target), self.assertRaisesRegex(acl.Blocked, 'outside_residual_target'):
                native.set_dacl(target, {})
            with self.subTest(project=target), self.assertRaisesRegex(acl.Blocked, 'outside_residual_target'):
                native.project(self.backend.current, target, None)
        current = copy.deepcopy(self.backend.current);current['entries']['data'] = entry('data',999)
        with self.assertRaisesRegex(acl.Blocked, 'outside_residual_inventory'):
            native.project(current, acl.RESIDUAL_RUNTIME[0], None)
        native.request.assert_not_called()

    def test_native_fixture_char_literal_backslashes_are_escaped(self):
        fixture = acl.POWERSHELL.split('public static Dictionary<string,object> ResidualFixture(', 1)[1]
        fixture = fixture.split('public static void Close()', 1)[0]
        wrong = "p.Replace('/','" + chr(92) + "')"
        right = "p.Replace('/','" + chr(92)*2 + "')"
        self.assertNotIn(wrong, fixture)
        self.assertEqual(fixture.count(right), 2)

    def test_residual_native_never_calls_whole_tree_start(self):
        source = acl.POWERSHELL
        section = source.split('public static void StartResidual(', 1)[1].split('static byte[] Bytes(GenericAce', 1)[0]
        self.assertIn('pending.Enqueue(".git")', section)
        self.assertNotIn('pending.Enqueue("")', section)
        self.assertIn('if(residualTargets.Contains(p)||(GitPath(p)&&!(bool)item.Value["protected"]))', section)
        self.assertIn('if(write&&residualMode)Check(relative!=null&&ResidualWritable(relative)&&writable.Contains(relative));', source)
        self.assertIn('if(residualMode)Check(ResidualWritable(p)&&writable.Contains(p));', source)
        self.assertIn('Check(descriptors.Count==handles.Count&&descriptors.Keys.All(p=>handles.ContainsKey(p)));', source)
        self.assertIn('"readonly_boundary",true', source)
        self.assertIn('"outside_preserved",true', source)
        self.assertIn('"descriptor_boundary",true', source)


if __name__ == "__main__":
    unittest.main()
