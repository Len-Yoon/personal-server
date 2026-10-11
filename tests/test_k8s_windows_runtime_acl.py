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


COUNTS = {"total": 27, "files": 8, "directories": 19, "git_unsafe": 18,
          "tracked_unsafe": 0, "ignored_unsafe": 1, "other_owner": 1}
GIT_DIRECTORIES = [".git/objects", ".git/objects/info", ".git/objects/pack",
                   ".git/refs", ".git/refs/heads", ".git/refs/tags"]
GIT_DIRECTORIES += [".git/other" + str(i) for i in range(11)]


def scope():
    return {"schema": 1, "root": "C:\\personal-server", "tracked_paths": ["tracked.py"],
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
    paths += ["tracked.py", "ignored", ".git/objects/pack/value", ".git/refs/heads/value"]
    directories = {"", ".git", *GIT_DIRECTORIES}
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
        result["entries"][target] = copy.deepcopy(approved)
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


if __name__ == "__main__":
    unittest.main()
