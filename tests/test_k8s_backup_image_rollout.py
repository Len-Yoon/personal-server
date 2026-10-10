"""Synthetic controller/adapters only: no live commands, jobs or credentials."""
import copy
import importlib.util
import json
import signal
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[1] / "infra/k8s/tools/backup-image-rollout.py"
SPEC = importlib.util.spec_from_file_location("backup_image_rollout", PATH)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


def image_record(app, number):
    old, new = "sha256:" + str(number) * 64, "sha256:" + hex(number + 4)[2:] * 64
    prefix = "docker.io/library/personal-server-" + app + "-pvc-backup@"
    record = {"app": app, "cronjob": app + "-pvc-backup", "uid": "uid-" + app,
              "archive_path": "/private/synthetic/" + app + ".oci.tar", "archive_sha256": "f" * 64}
    for side, value in (("old", old), ("new", new)):
        record[side] = {"reference": prefix + value, "config_digest": "sha256:" + "c" * 64,
                        "graph": {x: {"size": 10, "mediaType": "application/vnd.oci.image.manifest.v1+json"}
                                  for x in (value, "sha256:" + "c" * 64, "sha256:" + "d" * 64)}}
    spec = {"suspend": True, "schedule": "30 2 * * *", "timeZone": "Asia/Seoul",
            "jobTemplate": {"spec": {"template": {"spec": {"restartPolicy": "Never",
             "initContainers": [{"name": "prepare-writable-scratch" if app == "portal" else "prepare-scratch",
                                 "image": prefix + old, "imagePullPolicy": "Never"}],
             "containers": [{"name": "backup", "image": prefix + old, "imagePullPolicy": "Never"}]}}}}}
    record["old_spec_sha256"] = m.hash_object(spec)
    record["target_spec_sha256"] = m.hash_object(m.transform_spec(spec, record["new"]["reference"]))
    cron = {"kind": "CronJob", "metadata": {"name": record["cronjob"], "namespace": m.NS,
            "uid": record["uid"], "resourceVersion": "1"}, "spec": spec}
    return record, cron


def fixture():
    pairs = [image_record(app, n) for n, app in enumerate(m.APPS, 1)]
    resources = [{"kind": kind, "namespace": m.NS, "name": "synthetic-" + kind, "sha256": "a" * 64}
                 for kind in sorted(m.RESOURCE_KINDS - {"networkpolicy"})]
    resources += [{"kind": "deployment", "namespace": m.NS, "name": app + "-writer", "sha256": "a" * 64}
                  for app in m.APPS]
    resources += [{"kind": "configmap", "namespace": ns, "name": name, "sha256": "a" * 64}
                  for ns, name in [(m.NS, "pvc-backup-sequence-state"), ("monitoring", "sre-telegram-backup-status")]
                  + [(m.NS, a + "-pvc-backup-state") for a in m.APPS[1:]]]
    p = {"schema_version": 1, "c1": "1" * 40, "c2": "2" * 40, "c3": "3" * 40, "origin_main": "3" * 40,
         "pinned_release_ancestor_ack": True, "image_rollout_scope_ack": True,
         "ci": [{"commit": c * 40, "workflow": "CI", "branch": "main", "event": "push", "conclusion": "success",
                 "url": "https://github.com/example/repo/actions/runs/" + c} for c in ("2", "3")],
         "reviews": [{"commit": c * 40, "conclusion": "success", "url": "https://github.com/example/repo/pull/" + c}
                     for c in ("2", "3")],
         "pin_files": {path: {"c1": "a" * 64, "c2": "b" * 64, "c3": "a" * 64} for path in m.PIN_FILES},
         "allowed_forward_paths": list(m.PIN_FILES) + ["infra/k8s/tools/backup-image-rollout.py"],
         "allowed_inverse_paths": list(m.PIN_FILES), "images": [p[0] for p in pairs],
         "untracked": {}, "protected_files": {"/var/lib/personal-server/k3s-runtime-services.state": "a" * 64},
         "protected_resources": resources, "networkpolicy_inventory_sha256": m.hash_object([]),
         "writers": {app: app + "-writer" for app in m.APPS},
         "journal_dir": "/private/synthetic", "journal_name": "operation.jsonl",
         "budget": {"measured": True, "forward_seconds": 60, "rollback_seconds": 60, "margin_seconds": 60}}
    return p, {r[0]["app"]: r[1] for r in pairs}


class FakeJournal:
    def __init__(self, *args):
        self.events, self.closed = [], False

    def write(self, event, **fields):
        self.events.append((event, fields))

    def close(self):
        self.closed = True


class HeldForTest(BaseException):
    pass


class FakeBackend:
    def __init__(self, p, crons, inject=()):
        self.p, self.crons = p, copy.deepcopy(crons)
        self.current = p["c1"]
        self.inject = set(inject)
        self.active, self.acquisitions, self.releases = False, 0, 0
        self.events, self.writes = [], []
        self.dirty, self.drift = False, False
        self.source_checks = []
        self.final_checks = []

    @contextmanager
    def local_lock(self):
        if "contention" in self.inject:
            raise BlockingIOError()
        if self.active:
            raise AssertionError("nested lock")
        self.active = True
        self.acquisitions += 1
        try:
            yield
        finally:
            self.active = False
            self.releases += 1

    def phase_deadline(self, recovery=False):
        pass

    def head(self):
        return self.current

    def source_guard(self, expected):
        self.source_checks.append(expected)
        m.require(self.active and self.current == expected and not self.dirty, "source_guard")
        m.require(expected in (self.p["c1"], self.p["c2"], self.p["c3"]), "source_guard")

    def commits_guard(self):
        m.require("bad_commit_proof" not in self.inject, "commit_proof")

    def guards(self):
        m.require(self.active and not self.drift, "protected_drift")

    def images_guard(self):
        m.require("bad_graph" not in self.inject, "content_digest")

    def cron(self, image):
        return copy.deepcopy(self.crons[image["app"]])

    def patch(self, image, operations):
        app = image["app"]
        old = self.crons[app]
        new_side = "new" if operations[-1]["value"] == image["new"]["reference"] else "old"
        key = "patch_" + app + "_" + new_side
        self.events.append(key)
        m.require(self.active, "patch_without_lock")
        if key + "_before" in self.inject:
            raise m.Blocked("lost_response")
        expected_rv = next(x["value"] for x in operations if x["path"] == "/metadata/resourceVersion")
        m.require(expected_rv == old["metadata"]["resourceVersion"], "rv_cas")
        self.crons[app]["spec"] = m.transform_spec(old["spec"], image[new_side]["reference"])
        self.crons[app]["metadata"]["resourceVersion"] = str(int(expected_rv) + 1)
        self.writes.append(key)
        if key + "_foreign" in self.inject:
            self.crons[app]["spec"]["schedule"] = "foreign"
        if key + "_drift" in self.inject:
            self.drift = True
        if key + "_after" in self.inject or key + "_foreign" in self.inject:
            raise m.Blocked("lost_response")

    def ff(self, target):
        m.require(self.active, "ff_without_lock")
        phase = "forward" if target == self.p["c2"] else "inverse"
        self.events.append("ff_" + phase)
        if "ff_" + phase + "_before" in self.inject:
            raise m.Blocked("source_failure")
        self.current = target
        self.writes.append("ff_" + phase)
        if "ff_" + phase + "_dirty" in self.inject:
            self.dirty = True
        if "ff_" + phase + "_unknown" in self.inject:
            self.current = "9" * 40
        if any("ff_" + phase + suffix in self.inject for suffix in ("_after", "_dirty", "_unknown")):
            raise m.Blocked("source_response_lost")

    def final_check(self):
        self.final_checks.append(self.current)
        if self.current == self.p["c2"] and "final_failure" in self.inject:
            raise m.Blocked("final_failure")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.p, self.crons = fixture()

    def execute(self, inject=(), apply=True, mutate=None):
        b = FakeBackend(self.p, self.crons, inject)
        if mutate:
            mutate(b)
        c = m.Controller(b, self.p, journal_factory=FakeJournal, emit=Mock())
        c.budget = Mock()
        return c, b, c.execute(apply)

    def test_check_default_has_no_journal_or_mutation(self):
        c, b, result = self.execute(apply=False)
        self.assertEqual(result, "CHECK_PASSED_NO_MUTATION")
        self.assertIsNone(c.journal)
        self.assertEqual(b.writes, [])
        self.assertEqual((b.acquisitions, b.releases), (1, 1))

    def test_forward_acquires_existing_lock_once_and_pins_ancestor(self):
        c, b, result = self.execute()
        self.assertEqual(result, "FORWARD_COMPLETE_PINNED_RELEASE_ANCESTOR")
        self.assertEqual(b.current, self.p["c2"])
        self.assertEqual(self.p["origin_main"], self.p["c3"])
        self.assertEqual((b.acquisitions, b.releases), (1, 1))
        self.assertEqual(b.final_checks, [self.p["c1"], self.p["c2"]])
        self.assertEqual(b.writes[-1], "ff_forward")
        self.assertTrue(c.journal.closed)
        self.assertIsNone(c.token)

    def test_ff_refuses_no_token_without_backend_mutation(self):
        b = FakeBackend(self.p, self.crons)
        c = m.Controller(b, self.p)
        with self.assertRaisesRegex(m.Blocked, "owned_lock_required"):
            c.ff(self.p["c2"], None)
        self.assertEqual(b.writes, [])
        self.assertEqual(b.acquisitions, 0)

    def test_contention_aborts_without_mutation(self):
        b = FakeBackend(self.p, self.crons, ["contention"])
        with self.assertRaises(BlockingIOError):
            m.Controller(b, self.p).execute(True)
        self.assertEqual(b.writes, [])
        self.assertEqual(b.acquisitions, 0)

    def test_preflight_bad_proof_or_graph_or_protection_does_not_mutate(self):
        for inject in ("bad_commit_proof", "bad_graph"):
            with self.subTest(inject=inject):
                b = FakeBackend(self.p, self.crons, [inject])
                c = m.Controller(b, self.p)
                c.budget = Mock()
                with self.assertRaises(m.Blocked):
                    c.execute(True)
                self.assertEqual(b.writes, [])
                self.assertFalse(b.active)

    def test_foreign_spec_uid_and_container_shape_fail_before_mutation(self):
        for mutate in (lambda x: x["spec"].update(schedule="foreign"),
                       lambda x: x["metadata"].update(uid="replaced"),
                       lambda x: x["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"].append({})):
            crons = copy.deepcopy(self.crons)
            mutate(crons["portal"])
            b = FakeBackend(self.p, crons)
            c = m.Controller(b, self.p)
            c.budget = Mock()
            with self.assertRaises(m.Blocked):
                c.execute(True)
            self.assertEqual(b.writes, [])

    def test_lost_patch_response_reads_exact_target_without_repeat(self):
        _, b, result = self.execute(["patch_book_new_after"])
        self.assertIn("FORWARD_COMPLETE", result)
        self.assertEqual(b.events.count("patch_book_new"), 1)
        self.assertEqual(b.acquisitions, 1)

    def test_patch_failure_before_write_reverses_confirmed_changes_only(self):
        _, b, result = self.execute(["patch_book_new_before"])
        self.assertEqual(result, "ROLLED_BACK")
        self.assertEqual(b.writes, ["patch_portal_new", "patch_portal_old"])
        self.assertEqual(b.current, self.p["c1"])
        self.assertEqual((b.acquisitions, b.releases), (1, 1))

    def test_source_forward_failure_before_write_reverses_four_in_order(self):
        _, b, result = self.execute(["ff_forward_before"])
        self.assertEqual(result, "ROLLED_BACK")
        self.assertEqual(b.writes[-4:], ["patch_crawler_old", "patch_youtube_old", "patch_book_old", "patch_portal_old"])
        self.assertEqual(b.current, self.p["c1"])
        self.assertEqual(b.events.count("ff_forward"), 1)

    def test_source_lost_response_accepts_exact_clean_target_without_repeat(self):
        _, b, result = self.execute(["ff_forward_after"])
        self.assertIn("FORWARD_COMPLETE", result)
        self.assertEqual(b.events.count("ff_forward"), 1)

    def test_post_ff_failure_recovers_source_to_prepared_c3(self):
        _, b, result = self.execute(["final_failure"])
        self.assertEqual(result, "ROLLED_BACK")
        self.assertEqual(b.current, self.p["c3"])
        self.assertEqual(b.events[-1], "ff_inverse")
        self.assertEqual((b.acquisitions, b.releases), (1, 1))

    def test_inverse_source_lost_response_accepts_clean_c3(self):
        _, b, result = self.execute(["final_failure", "ff_inverse_after"])
        self.assertEqual(result, "ROLLED_BACK")
        self.assertEqual(b.current, self.p["c3"])
        self.assertEqual(b.events.count("ff_inverse"), 1)

    def test_held_recovery_keeps_owner_alive_with_no_repeat_mutation(self):
        scenarios = (["patch_book_new_foreign"], ["patch_book_new_drift"],
                     ["ff_forward_dirty"], ["ff_forward_unknown"],
                     ["final_failure", "ff_inverse_before"],
                     ["final_failure", "patch_crawler_old_before"])
        for inject in scenarios:
            with self.subTest(inject=inject):
                b = FakeBackend(self.p, self.crons, inject)
                observed = []
                def sleep(seconds):
                    self.assertTrue(b.active)
                    self.assertIsNotNone(c.token)
                    self.assertEqual(seconds, 30)
                    observed.append(list(b.events))
                    if len(observed) == 2:
                        self.assertEqual(observed[0], observed[1])
                        raise HeldForTest()
                c = m.Controller(b, self.p, journal_factory=FakeJournal, sleep=sleep, emit=Mock())
                c.budget = Mock()
                with patch.object(m.signal, "signal"), self.assertRaises(HeldForTest):
                    c.execute(True)
                self.assertEqual(c.emit.call_args.args, ("HELD_RECOVERY",))
                self.assertEqual(b.acquisitions, 1)

    def test_broken_observer_does_not_break_completion_or_release_early(self):
        with patch("builtins.print", side_effect=BrokenPipeError):
            m.Controller.safe_emit("HELD_RECOVERY")

    def test_inverse_has_fresh_resource_version_and_exactly_two_replaces(self):
        record = self.p["images"][0]
        cron = copy.deepcopy(self.crons["portal"])
        cron["spec"] = m.transform_spec(cron["spec"], record["new"]["reference"])
        cron["metadata"]["resourceVersion"] = "fresh-37"
        patch_ops = m.image_patch(cron, record, "new", "old")
        self.assertEqual([x["path"] for x in patch_ops if x["op"] == "replace"], list(m.IMAGE_PATHS))
        self.assertIn({"op": "test", "path": "/metadata/resourceVersion", "value": "fresh-37"}, patch_ops)
        self.assertIn({"op": "test", "path": "/metadata/uid", "value": record["uid"]}, patch_ops)
        self.assertEqual(sum(x["path"].endswith("/name") for x in patch_ops), 2)


class PlanAndNativeTests(unittest.TestCase):
    def test_valid_plan_and_incomplete_proof_rejected(self):
        p, _ = fixture()
        m.validate_plan(p)
        for key in ("ci", "reviews", "protected_resources", "images", "pin_files"):
            bad = copy.deepcopy(p)
            bad[key] = [] if isinstance(p[key], list) else {}
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.validate_plan(bad)
        for key in ("pinned_release_ancestor_ack", "image_rollout_scope_ack"):
            bad = copy.deepcopy(p)
            bad[key] = False
            with self.subTest(key=key), self.assertRaises(m.Blocked):
                m.validate_plan(bad)

    def test_c3_must_restore_exact_pin_hash_and_inverse_path_scope(self):
        p, _ = fixture()
        p["pin_files"][m.PIN_FILES[0]]["c3"] = "f" * 64
        with self.assertRaisesRegex(m.Blocked, "plan_pin_hashes"):
            m.validate_plan(p)
        p, _ = fixture()
        p["allowed_inverse_paths"].append("scripts/scheduler.py")
        with self.assertRaisesRegex(m.Blocked, "inverse_path_scope"):
            m.validate_plan(p)

    def native(self):
        p, _ = fixture()
        b = object.__new__(m.NativeBackend)
        b.root, b.plan = Path("/synthetic/repo"), p
        return b

    def test_secret_command_projects_metadata_without_requesting_payload_json(self):
        b = self.native()
        b.run = Mock(return_value=b"synthetic-uid 42 Opaque\n")
        result = b.get("secret", m.NS, "runtime")
        self.assertEqual(result, {"metadata": {"uid": "synthetic-uid", "resourceVersion": "42"}, "type": "Opaque"})
        command = b.run.call_args.args[0]
        self.assertIn("-o=custom-columns=UID:.metadata.uid,RV:.metadata.resourceVersion,TYPE:.type", command)
        self.assertNotIn("json", command)
        self.assertNotIn("go-template", " ".join(command))

    def test_secret_projection_excludes_payload(self):
        obj = {"metadata": {"uid": "u", "resourceVersion": "v"}, "type": "Opaque", "data": {"key": "hidden"}}
        result = m.resource_projection("secret", obj)
        self.assertEqual(result, {"uid": "u", "resourceVersion": "v", "type": "Opaque"})
        self.assertNotIn("hidden", json.dumps(result))

    def test_writer_not_ready_restart_and_pvc_unbound_are_rejected(self):
        pod = {"metadata": {"uid": "u"}, "spec": {}, "status": {"phase": "Running",
               "conditions": [{"type": "Ready", "status": "True"}],
               "containerStatuses": [{"ready": True, "restartCount": 0}]}}
        m.resource_projection("pod", pod)
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
        with self.assertRaisesRegex(m.Blocked, "writer_restart"):
            m.resource_projection("pod", pod)
        with self.assertRaisesRegex(m.Blocked, "pvc_not_bound"):
            m.resource_projection("persistentvolumeclaim", {"metadata": {"uid": "u"}, "status": {"phase": "Pending"}})

    def test_git_no_hook_or_fsmonitor_disable_flags(self):
        b = self.native()
        b.run = Mock(return_value=b"head")
        b.git("rev-parse", "HEAD")
        self.assertEqual(b.run.call_args.args, (["git", "rev-parse", "HEAD"],))

    def test_final_check_uses_fresh_processes_and_never_go_apply(self):
        b = self.native()
        b.run = Mock()
        b.final_check()
        commands = [x.args[0] for x in b.run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(x[-1] == "--check" for x in commands))
        self.assertTrue(commands[1][1].endswith("service-pvc-backup-production-state.py"))

    def test_subprocess_error_hides_sensitive_stderr(self):
        b = self.native()
        completed = Mock(returncode=1)
        completed.communicate.return_value = (b"hidden", None)
        completed.poll.return_value = 1
        with patch.object(m.subprocess, "Popen", return_value=completed) as run:
            with self.assertRaisesRegex(m.Blocked, "command_failed"):
                b.run(["synthetic"])
            self.assertIs(run.call_args.kwargs["stderr"], m.subprocess.DEVNULL)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_journal_intent_precedes_first_mutation(self):
        p, crons = fixture()
        b = FakeBackend(p, crons)
        c = m.Controller(b, p, journal_factory=FakeJournal, emit=Mock())
        c.budget = Mock()
        original = b.patch
        def guarded_patch(*args):
            self.assertEqual(c.journal.events[-1][0], "cron_intent")
            original(*args)
        b.patch = guarded_patch
        c.execute(True)
        self.assertFalse(any("spec" in entry[1] or "data" in entry[1] for entry in c.journal.events))


class NativeContractTests(unittest.TestCase):
    def source_fixture(self):
        p, _ = fixture()
        b = object.__new__(m.NativeBackend)
        b.plan, b.root = p, Path("/synthetic/repo")
        blobs = {}
        for path in m.PIN_FILES:
            parts = []
            for image in p["images"]:
                count = (2 if image["app"] == "portal" else 0) if path == m.PIN_FILES[0] else (
                    (0 if image["app"] == "portal" else 1) if path == m.PIN_FILES[1] else 1)
                parts += [image["old"]["reference"].split("@", 1)[1].encode()] * count
            old = b"\n".join(parts) + b"\nstable bytes\n"
            new = old
            for image in p["images"]:
                new = new.replace(image["old"]["reference"].split("@", 1)[1].encode(),
                                  image["new"]["reference"].split("@", 1)[1].encode())
            for phase, raw in (("c1", old), ("c2", new), ("c3", old)):
                blobs[p[phase] + ":" + path] = raw
                p["pin_files"][path][phase] = m.digest(raw)
        def git(*args):
            if args[0] == "fetch":
                return b""
            if args[0] == "rev-parse":
                return p["c3"].encode()
            if args[0] == "merge-base":
                return b""
            if args[0] == "diff":
                key = "allowed_forward_paths" if args[-2] == p["c1"] else "allowed_inverse_paths"
                return ("\0".join(p[key]) + "\0").encode()
            if args[0] == "show":
                return blobs[args[1]]
            raise AssertionError(args)
        b.git = Mock(side_effect=git)
        return b, git, blobs

    def test_native_exact_nine_pin_pair_and_ancestry_positive(self):
        b, _, _ = self.source_fixture()
        b.commits_guard()
        fetch = b.git.call_args_list[0].args
        self.assertIn("--no-auto-maintenance", fetch)
        self.assertEqual(sum(x.args[0] == "merge-base" for x in b.git.call_args_list), 2)

    def test_native_origin_nonff_and_changed_path_fail_closed(self):
        for failure in ("origin", "ancestor", "paths"):
            b, original, _ = self.source_fixture()
            def git(*args):
                if failure == "origin" and args[0] == "rev-parse":
                    return b"9" * 40
                if failure == "ancestor" and args[0] == "merge-base":
                    raise m.Blocked("not_ancestor")
                if failure == "paths" and args[0] == "diff":
                    return original(*args) + b"scripts/scheduler.py\0"
                return original(*args)
            b.git.side_effect = git
            with self.subTest(failure=failure), self.assertRaises(m.Blocked):
                b.commits_guard()

    def test_native_non_pin_edit_and_c3_not_exact_inverse_fail_closed(self):
        for phase in ("c2", "c3"):
            b, _, blobs = self.source_fixture()
            key = b.plan[phase] + ":" + m.PIN_FILES[2]
            blobs[key] += b"unauthorized logic\n"
            b.plan["pin_files"][m.PIN_FILES[2]][phase] = m.digest(blobs[key])
            with self.subTest(phase=phase), self.assertRaises(m.Blocked):
                b.commits_guard()

    def graph_fixture(self):
        p, _ = fixture()
        b = object.__new__(m.NativeBackend)
        b.root, b.plan = Path("/synthetic/repo"), p
        blobs = {}
        for image in p["images"]:
            for side in ("old", "new"):
                layer = (image["app"] + side).encode()
                layer_digest = "sha256:" + m.digest(layer)
                config = m.canonical({"os": "linux", "architecture": "amd64", "side": side,
                                      "rootfs": {"type": "layers", "diff_ids": [layer_digest]}})
                config_digest = "sha256:" + m.digest(config)
                cfg_desc = {"digest": config_digest, "size": len(config), "mediaType": next(iter(m.CONFIG_TYPES))}
                layer_desc = {"digest": layer_digest, "size": len(layer), "mediaType": "application/vnd.oci.image.layer.v1.tar"}
                media = "application/vnd.oci.image.manifest.v1+json"
                manifest = m.canonical({"schemaVersion": 2, "mediaType": media, "config": cfg_desc, "layers": [layer_desc]})
                manifest_digest = "sha256:" + m.digest(manifest)
                image[side] = {"reference": "docker.io/library/personal-server-" + image["app"] + "-pvc-backup@" + manifest_digest,
                               "config_digest": config_digest,
                               "graph": {manifest_digest: {"size": len(manifest), "mediaType": media},
                                         config_digest: {k: v for k, v in cfg_desc.items() if k != "digest"},
                                         layer_digest: {k: v for k, v in layer_desc.items() if k != "digest"}}}
                blobs.update({manifest_digest: manifest, config_digest: config, layer_digest: layer})
        def aliases(*args, **kwargs):
            rows = []
            for image in p["images"]:
                for side in ("old", "new"):
                    record = image[side]
                    target = record["reference"].split("@", 1)[1]
                    rows.append(record["reference"] + " " + record["graph"][target]["mediaType"] + " " + target)
            return "\n".join(rows).encode()
        b.run = Mock(side_effect=aliases)
        b.read_blob = Mock(side_effect=lambda blob, size, retain: blobs[blob] if retain else None)
        return b, blobs

    def test_native_old_and_new_full_image_graph_positive(self):
        b, _ = self.graph_fixture()
        with patch.object(m, "regular_hash", return_value="f" * 64):
            b.images_guard()
        self.assertEqual(b.run.call_count, 2)

    def test_native_alias_target_and_media_type_are_revalidated(self):
        for field in ("target", "media"):
            b, _ = self.graph_fixture()
            output = b.run().decode()
            rows = output.splitlines()
            cells = rows[0].split()
            cells[2 if field == "target" else 1] = "foreign"
            rows[0] = " ".join(cells)
            b.run.side_effect = None
            b.run.return_value = "\n".join(rows).encode()
            with self.subTest(field=field), self.assertRaisesRegex(m.Blocked, "alias_target"):
                b.aliases_guard()

    def test_native_config_platform_and_descriptor_missing_or_size_drift(self):
        for failure in ("platform", "missing", "size"):
            b, blobs = self.graph_fixture()
            image = b.plan["images"][0]["old"]
            manifest_digest = image["reference"].split("@", 1)[1]
            manifest = json.loads(blobs[manifest_digest])
            if failure == "platform":
                config = json.loads(blobs[image["config_digest"]])
                config["architecture"] = "arm64"
                blobs[image["config_digest"]] = m.canonical(config)
            elif failure == "missing":
                del image["graph"][manifest["layers"][0]["digest"]]
            else:
                image["graph"][manifest["layers"][0]["digest"]]["size"] += 1
            with self.subTest(failure=failure), patch.object(m, "regular_hash", return_value="f" * 64), self.assertRaises(m.Blocked):
                b.images_guard()

    def blob_call(self, raw, size, blob, clock=None):
        b = object.__new__(m.NativeBackend)
        b.root = Path("/synthetic/repo")
        child = Mock()
        child.stdout.fileno.return_value = 12
        child.pid = 12345
        child.wait.return_value = 0
        child.poll.return_value = None
        selector = Mock()
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock(return_value=False)
        selector.select.return_value = [(None, None)]
        with patch.object(m.subprocess, "Popen", return_value=child), patch.object(m.selectors, "DefaultSelector", return_value=selector), \
             patch.object(m.os, "read", side_effect=[raw, b""]), patch.object(m.os, "killpg") as kill, \
             patch.object(m.time, "monotonic", side_effect=clock or [0, 1, 2]):
            try:
                result = b.read_blob(blob, size, True)
            except m.Blocked as exc:
                return str(exc), child, kill.call_args_list
            return result, child, kill.call_args_list

    def test_native_stream_size_and_digest_fail_closed_with_child_cleanup(self):
        result, child, kills = self.blob_call(b"oversize", 2, "sha256:" + "a" * 64)
        self.assertEqual(result, "image_content_size")
        child.stdout.close.assert_called_once()
        self.assertTrue(kills)
        result, _, _ = self.blob_call(b"ok", 2, "sha256:" + "a" * 64)
        self.assertEqual(result, "image_content_digest")

    def test_native_stream_timeout_cleans_child_without_waiting_for_eof(self):
        result, child, kills = self.blob_call(b"data", 4, "sha256:" + "a" * 64, clock=[0, 61])
        self.assertEqual(result, "image_content_timeout")
        self.assertEqual(kills[0].args, (12345, signal.SIGTERM))
        self.assertTrue(all(x.kwargs == {"timeout": 2} for x in child.wait.call_args_list))


class GuardBoundaryTests(unittest.TestCase):
    def test_timer_boundary_quiet_window_and_remaining_budget(self):
        p, crons = fixture()
        controller = m.Controller(FakeBackend(p, crons), p)
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        cases = [("00:00:00", True), ("02:20:00", True), ("02:29:59", False),
                 ("02:30:00", False), ("05:59:59", False), ("06:00:00", True)]
        for clock, allowed in cases:
            local = datetime.fromisoformat("2026-10-11T" + clock).replace(tzinfo=ZoneInfo("Asia/Seoul"))
            with self.subTest(clock=clock), patch.object(m, "datetime") as fake:
                fake.now.return_value = local.astimezone(timezone.utc)
                if allowed:
                    controller.budget()
                else:
                    with self.assertRaises(m.Blocked):
                        controller.budget()

    def test_native_phase_budget_clamps_each_command_and_refuses_expired(self):
        p, _ = fixture()
        backend = object.__new__(m.NativeBackend)
        backend.plan = p
        with patch.object(m.time, "monotonic", return_value=100):
            backend.phase_deadline()
        with patch.object(m.time, "monotonic", return_value=155):
            self.assertEqual(backend.remaining(30), 5)
        with patch.object(m.time, "monotonic", return_value=160), self.assertRaisesRegex(m.Blocked, "phase_time_budget"):
            backend.remaining(30)

    def test_native_source_guards_check_actual_config_index_worktree_untracked(self):
        for failure in (None, "hooks", "fsmonitor", "dirty", "hidden", "index", "untracked"):
            p, _ = fixture()
            backend = object.__new__(m.NativeBackend)
            backend.root, backend.plan = Path("/synthetic/repo"), p
            raw = b"pinned bytes"
            for hashes in p["pin_files"].values():
                hashes["c1"] = m.digest(raw)
            config = {"hooks": b"core.hookspath\n/synthetic/hooks\0",
                      "fsmonitor": b"core.fsmonitor\ntrue\0"}.get(failure, b"core.fsmonitor\nfalse\0")
            backend.run = Mock(return_value=config)
            def git(*args):
                if args == ("rev-parse", "--absolute-git-dir"):
                    return b"/synthetic/git"
                if args == ("rev-parse", "HEAD"):
                    return p["c1"].encode()
                if args[0] == "branch":
                    return b"main"
                if args[0] == "status":
                    return b" M tracked.py" if failure == "dirty" else b""
                if args[:3] == ("ls-files", "-v", "-z"):
                    return b"h tracked.py\0" if failure == "hidden" else b"H tracked.py\0"
                if args[0] == "diff":
                    if failure == "index":
                        raise m.Blocked("dirty_index")
                    return b""
                if args[0] == "ls-files":
                    return b"extra.py\0" if failure == "untracked" else b""
                if args[0] == "show":
                    return raw
                raise AssertionError(args)
            backend.git = Mock(side_effect=git)
            with self.subTest(failure=failure), patch.object(m.Path, "exists", return_value=False), \
                 patch.object(m.Path, "is_symlink", return_value=False), patch.object(m, "regular_hash", return_value=m.digest(raw)):
                if failure is None:
                    backend.source_guard(p["c1"])
                else:
                    with self.assertRaises(m.Blocked):
                        backend.source_guard(p["c1"])

    def test_uncertain_child_termination_blocks_future_recovery_mutation_guards(self):
        backend = object.__new__(m.NativeBackend)
        backend.command_termination_unknown = True
        with self.assertRaisesRegex(m.Blocked, "child_termination_unknown"):
            backend.guards()
        with self.assertRaisesRegex(m.Blocked, "child_termination_unknown"):
            backend.source_guard("1" * 40)

    def test_native_command_timeout_cleans_process_group_before_return(self):
        backend = object.__new__(m.NativeBackend)
        backend.root = Path("/synthetic/repo")
        child = Mock(pid=12345)
        child.communicate.side_effect = m.subprocess.TimeoutExpired("synthetic", 1)
        child.poll.return_value = None
        with patch.object(m.subprocess, "Popen", return_value=child), patch.object(m.os, "killpg") as kill:
            with self.assertRaisesRegex(m.Blocked, "command_unavailable"):
                backend.run(["synthetic"])
            self.assertEqual([x.args[1] for x in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL, 0])
            self.assertEqual(child.wait.call_count, 2)


class OwnershipProofTests(unittest.TestCase):
    def test_portal_absent_lock_key_requires_other_owned_lock_and_job_gates(self):
        m.validate_backup_status_lock("portal", {})
        m.validate_backup_status_lock("portal", {"lock_run_id": ""})
        with self.assertRaisesRegex(m.Blocked, "backup_status_lock"):
            m.validate_backup_status_lock("portal", {"lock_run_id": "running"})
        for app in m.APPS[1:]:
            m.validate_backup_status_lock(app, {"lock_run_id": ""})
            with self.assertRaisesRegex(m.Blocked, "backup_status_lock"):
                m.validate_backup_status_lock(app, {})

    def test_child_group_only_esrch_confirms_no_privileged_late_writer(self):
        for outcome, unknown in ((ProcessLookupError(), False), (PermissionError(), True), (None, True)):
            backend = object.__new__(m.NativeBackend)
            backend.command_termination_unknown = False
            child = Mock(pid=12345)
            effects = [None, None, outcome]
            with self.subTest(outcome=type(outcome).__name__), patch.object(m.os, "killpg", side_effect=effects):
                backend.stop_child(child)
            self.assertEqual(backend.command_termination_unknown, unknown)


if __name__ == "__main__":
    unittest.main()
