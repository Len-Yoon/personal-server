import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "k8s" / "apps" / "youtube-memo.yaml"
CUTOVER = ROOT / "infra" / "k8s" / "tools" / "youtube-memo-cutover.sh"
PREPARE = ROOT / "infra" / "k8s" / "tools" / "youtube-memo-prepare.sh"
UNCONFIGURED_IMAGE = "personal-server-youtube-memo:unconfigured-do-not-run"


class YouTubeMemoCutoverTests(unittest.TestCase):
    """Exercise the operator script with isolated Docker/K3s executables."""

    def run_cutover(self, *modes, compose_state="exited", scenario="ok", image=None,
                    database="youtube_memo.sqlite3"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        source, target, binaries = (base / name for name in ("source", "target", "bin"))
        for item in (source, target, binaries):
            item.mkdir()
        with sqlite3.connect(source / "youtube_memo.sqlite3") as connection:
            connection.execute("create table memo (text text)")
            connection.execute("insert into memo values ('private-fixture')")
        if scenario == "bad_sqlite":
            (source / "youtube_memo.sqlite3").write_text("not a database")
        if scenario == "symlink":
            (source / "link").symlink_to(source / "youtube_memo.sqlite3")
        if scenario == "occupied_pvc":
            (target / "keep").write_text("existing")
        calls = base / "calls"
        calls.touch()
        state = {"compose": "running", "replicas": 0, "helper": False}
        if scenario == "helper_collision":
            state["helper"] = True
        if modes == ("--rollback",):
            state.update(compose="exited", replicas=1)
            (target / "youtube_memo.sqlite3").write_bytes((source / "youtube_memo.sqlite3").read_bytes())
            if scenario == "changed_data":
                with sqlite3.connect(target / "youtube_memo.sqlite3") as connection:
                    connection.execute("insert into memo values ('new-data')")
        (base / "state").write_text(json.dumps(state))
        chosen_image = image or "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        fixture = {"base": str(base), "source": str(source), "target": str(target),
                   "scenario": scenario, "after_stop": compose_state, "image": chosen_image}
        deployed = next(item for item in yaml.safe_load_all(MANIFEST.read_text()) if item["kind"] == "Deployment")
        deployed["spec"]["template"]["spec"]["containers"][0]["image"] = chosen_image
        if scenario == "foreign_volume":
            deployed["spec"]["template"]["spec"]["volumes"].append(
                {"name": "foreign", "persistentVolumeClaim": {"claimName": "other-data"}})
        if scenario == "init_writer":
            deployed["spec"]["template"]["spec"]["initContainers"] = [{"name": "writer", "image": chosen_image}]
        if scenario == "inline_env":
            deployed["spec"]["template"]["spec"]["containers"][0]["env"] = [
                {"name": "YOUTUBE_MEMO_DB_PATH", "value": "/tmp/override.sqlite3"},
            ]
        fixture["deployment"] = deployed
        (base / "fixture").write_text(json.dumps(fixture))
        mock = r'''import json, os, pathlib, sqlite3, subprocess, sys
base = pathlib.Path(os.environ['CUTOVER_FIXTURE'])
fixture = json.loads((base / 'fixture').read_text())
state = json.loads((base / 'state').read_text())
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (base / 'calls').open('a') as handle:
    handle.write(json.dumps([name, *args]) + '\n')
scenario = fixture['scenario']
def save(): (base / 'state').write_text(json.dumps(state))
def output(value): print(json.dumps(value))
def fail():
    print('SECRET-private-fixture should be suppressed', file=sys.stderr)
    sys.exit(1)
if name == 'docker':
    if args[:2] == ['inspect', '--format']:
        if args[2] == '{{json .State}}':
            output({'Status': state['compose'], 'Running': state['compose'] == 'running',
                    'Health': {'Status': 'healthy'}})
        elif args[2] == '{{json .Mounts}}':
            output([{'Type': 'bind', 'Source': fixture['source'], 'Destination': '/data/youtube-memo', 'RW': True}])
        else: fail()
    elif args[:1] == ['stop']:
        state['compose'] = fixture['after_stop']; save()
    elif args[:1] == ['start']:
        state['compose'] = 'running'; save()
        if scenario in ('rollback_divergence', 'rollback_unverifiable'):
            source = pathlib.Path(fixture['source']) / 'youtube_memo.sqlite3'
            if scenario == 'rollback_divergence':
                with sqlite3.connect(source) as connection:
                    connection.execute("insert into memo values ('after-compose-start')")
            else:
                source.write_text('invalid sqlite')
            fail()
    else: fail()
elif name == 'sudo':
    if args[:3] == ['-n', 'k3s', 'ctr']:
        if args[3:] != ['images', 'list']: fail()
        if scenario != 'missing_image':
            size = '10 MiB' if scenario == 'ctr_size_unit' else '10MiB'
            platform = 'linux/arm64' if scenario == 'wrong_platform' else 'linux/amd64'
            listed_image = ('docker.io/library/personal-server-youtube-memo:k3s-test'
                            if scenario == 'alias_absent' else fixture['image'])
            print(listed_image + ' application/vnd.oci.image.manifest.v1+json sha256:' + 'a'*64 + ' ' + size + ' ' + platform + ' -')
    else:
        if args[:5] != ['-n', 'k3s', 'kubectl', '-n', 'personal-server']: fail()
        command = args[5:]
        if command[:2] == ['get', 'deployment']:
            document = fixture['deployment']
            document['spec']['replicas'] = state['replicas']
            output(document)
        elif command[:2] == ['get', 'service']:
            output({'metadata': {'name': 'youtube-memo', 'namespace': 'personal-server'},
                    'spec': {'type': 'ClusterIP',
                             'selector': {'app.kubernetes.io/name': 'youtube-memo'},
                             'ports': [{'name': 'http', 'port': 8002, 'targetPort': 'http'}]}})
        elif command[:2] == ['get', 'pods']:
            pods = []
            if state['replicas'] or scenario == 'foreign_writer':
                pods.append({'metadata':{'name':'youtube-memo-writer'}, 'spec': {
                    'volumes':[{'persistentVolumeClaim':{'claimName':'youtube-memo-data'}}]}})
            if state['helper']:
                pods.append({'metadata':{'name':'youtube-memo-cutover-data'}, 'spec': {
                    'volumes':[] if scenario == 'helper_collision' else [{'persistentVolumeClaim':{'claimName':'youtube-memo-data'}}]}})
            output({'items':pods})
        elif command[:2] == ['get', 'pod']:
            if state['helper']:
                if scenario == 'helper_collision':
                    output({'metadata': {'name': 'youtube-memo-cutover-data', 'uid': 'existing-uid'}})
                else:
                    helper = json.loads((base / 'helper').read_text())
                    if scenario == 'foreign_helper_before_copy':
                        helper['metadata']['labels']['app.kubernetes.io/cutover-owner'] = 'foreign-owner'
                        (base / 'helper').write_text(json.dumps(helper))
                    output(helper)
        elif command[:2] == ['get', 'pvc']:
            output({'metadata':{'name':'youtube-memo-data'}, 'status':{'phase': 'Pending' if scenario == 'pending_pvc' else 'Bound'},
                    'spec':{'accessModes':['ReadWriteOnce']}})
        elif command[:2] == ['apply', '--dry-run=server']:
            pass
        elif command[:1] == ['create']:
            if state['helper']: fail()
            payload = json.load(sys.stdin)
            assert payload['kind'] == 'Pod'
            assert payload['spec']['containers'][0]['image'] == fixture['image']
            assert 'envFrom' not in payload['spec']['containers'][0]
            payload['metadata']['uid'] = 'created-uid'
            (base / 'helper').write_text(json.dumps(payload))
            state['helper'] = True; save()
            if scenario == 'create_response_lost': fail()
            output(payload)
        elif command[:1] == ['wait']:
            pass
        elif command[:2] == ['delete', 'pod']:
            state['helper'] = False; save()
        elif command[:1] == ['delete'] and command[1].startswith('--raw='):
            options = json.load(sys.stdin)
            existing = json.loads((base / 'helper').read_text())
            if scenario == 'helper_replaced':
                existing['metadata']['uid'] = 'replacement-uid'
                (base / 'helper').write_text(json.dumps(existing))
            if options['preconditions']['uid'] != existing['metadata']['uid']: fail()
            state['helper'] = False; save()
        elif command[:1] == ['exec']:
            separator = command.index('--')
            child = command[separator+1:]
            assert child[:2] == ['python3', '-c']
            child = [sys.executable, *child[1:]]
            child = [fixture['target'] if part == '/data/youtube-memo' else part for part in child]
            if scenario == 'copy_failure' and child[-1] == 'extract': fail()
            result = subprocess.run(child, stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if scenario == 'digest_mismatch' and child[-1] == 'verify': print('0'*64)
            else: sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            sys.exit(result.returncode)
        elif command[:1] == ['scale']:
            desired = int(next(x for x in command if x.startswith('--replicas=')).split('=')[1])
            state['replicas'] = desired; save()
        elif command[:2] == ['rollout', 'status']:
            if scenario in ('rollout_divergence', 'rollout_unverifiable'):
                target = pathlib.Path(fixture['target']) / 'youtube_memo.sqlite3'
                if scenario == 'rollout_divergence':
                    with sqlite3.connect(target) as connection:
                        connection.execute("insert into memo values ('new-data')")
                else:
                    target.write_text('invalid sqlite')
                fail()
            if scenario == 'rollout_failure': fail()
        else: fail()
else: fail()
'''
        for name in ("docker", "sudo"):
            executable = binaries / name
            executable.write_text(f"#!{sys.executable}\n" + mock)
            executable.chmod(0o755)
        environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                       "CUTOVER_FIXTURE": str(base)}
        result = subprocess.run(
            ["bash", str(CUTOVER), *modes, "--source", str(source), "--database", database, "--image", chosen_image],
            env=environment, text=True, capture_output=True, timeout=20,
        )
        self.last_state = json.loads((base / "state").read_text())
        self.last_target = target
        self.assertNotIn("private-fixture", result.stdout + result.stderr)
        self.assertNotIn(str(source), result.stdout + result.stderr)
        return result, calls

    def test_go_requires_compose_writer_stopped_before_any_data_copy(self):
        result, calls = self.run_cutover("--go", compose_state="running")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("youtube_memo_cutover=FAIL", result.stderr)
        self.assertNotIn('"create"', calls.read_text())
        self.assertEqual(list(self.last_target.iterdir()), [])

    def test_combined_go_and_rollback_is_rejected_without_external_calls(self):
        result, calls = self.run_cutover("--go", "--rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text(), "")

    def test_non_youtube_database_is_rejected_without_external_calls(self):
        result, calls = self.run_cutover("--check", database="other.sqlite3")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text(), "")

    def test_missing_repeated_and_unknown_modes_make_no_external_calls(self):
        for modes in ((), ("--go", "--go"), ("--invalid",)):
            with self.subTest(modes=modes):
                result, calls = self.run_cutover(*modes)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("youtube_memo_cutover=FAIL", result.stderr)
                self.assertEqual(calls.read_text(), "")

    def test_check_and_prepare_never_change_writers_or_copy_data(self):
        for mode in ("--check", "--prepare"):
            with self.subTest(mode=mode):
                result, calls = self.run_cutover(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = calls.read_text()
                for forbidden in ('"stop"', '"start"', '"create"', '"scale"', '"exec"'):
                    self.assertNotIn(forbidden, commands)
                self.assertEqual('--dry-run=server' in commands, mode == '--prepare')

    def test_prerequisite_failure_prevents_compose_stop(self):
        for scenario in ("missing_image", "wrong_platform", "alias_absent", "pending_pvc", "foreign_writer", "symlink"):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"stop"', calls.read_text())
                self.assertEqual(self.last_state["compose"], "running")

    def test_mutable_or_foreign_image_is_rejected_without_external_calls(self):
        for image in ("personal-server-youtube-memo:latest", "portal-web@sha256:" + "a"*64):
            with self.subTest(image=image):
                result, calls = self.run_cutover("--go", image=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.read_text(), "")

    def test_go_verifies_data_and_hands_over_one_writer(self):
        result, calls = self.run_cutover("--go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("youtube_memo_cutover=PASS", result.stdout)
        self.assertEqual(self.last_state, {"compose": "exited", "replicas": 1, "helper": False})
        with sqlite3.connect(self.last_target / "youtube_memo.sqlite3") as connection:
            self.assertEqual(connection.execute("select count(*) from memo").fetchone()[0], 1)
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        scale = next(i for i, c in enumerate(commands) if '--replicas=1' in c)
        verify = max(i for i, c in enumerate(commands) if c[-1] == 'verify')
        deletion = max(i for i, c in enumerate(commands) if 'delete' in c)
        self.assertGreater(scale, verify)
        self.assertGreater(scale, deletion)

    def test_failed_go_restores_compose_and_stops_k3s(self):
        for scenario in ("bad_sqlite", "copy_failure", "digest_mismatch", "rollout_failure", "occupied_pvc"):
            with self.subTest(scenario=scenario):
                result, _ = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r"^youtube_memo_cutover=FAIL stage=[a-z_]+\n$")
                self.assertEqual(self.last_state, {"compose": "running", "replicas": 0, "helper": False})
                if scenario == "occupied_pvc":
                    self.assertEqual((self.last_target / "keep").read_text(), "existing")

    def test_containerd_human_readable_size_does_not_hide_amd64_platform(self):
        result, _ = self.run_cutover("--check", scenario="ctr_size_unit")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tampered_deployment_blocks_handoff_before_compose_stop(self):
        for scenario in ("foreign_volume", "init_writer", "inline_env"):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("stage=deployment", result.stderr)
                self.assertNotIn('"stop"', calls.read_text())

    def test_foreign_helper_replacement_blocks_copy_and_k3s_start(self):
        result, calls = self.run_cutover("--go", scenario="foreign_helper_before_copy")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"exec"', calls.read_text())
        self.assertNotIn('--replicas=1', calls.read_text())
        self.assertEqual(self.last_state, {"compose": "exited", "replicas": 0, "helper": True})

    def test_rollback_stops_k3s_before_restoring_unchanged_compose(self):
        result, calls = self.run_cutover("--rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.last_state, {"compose": "running", "replicas": 0, "helper": False})
        commands = calls.read_text()
        self.assertLess(commands.index('--replicas=0'), commands.index('"start"'))

    def test_rollback_refuses_stale_compose_data_and_restores_k3s(self):
        result, calls = self.run_cutover("--rollback", scenario="changed_data")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"start"', calls.read_text())
        self.assertEqual(self.last_state, {"compose": "exited", "replicas": 1, "helper": False})

    def test_failed_rollback_start_never_reactivates_stale_or_unverifiable_pvc(self):
        for scenario, stage in (("rollback_divergence", "recovery_data_divergence"),
                                ("rollback_unverifiable", "recovery_data_unverified")):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--rollback", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, f"youtube_memo_cutover=FAIL stage={stage}\n")
                self.assertEqual(self.last_state, {"compose": "exited", "replicas": 0, "helper": False})
                self.assertNotIn('--replicas=1', calls.read_text())
                with sqlite3.connect(self.last_target / "youtube_memo.sqlite3") as connection:
                    self.assertEqual(connection.execute("select count(*) from memo").fetchone()[0], 1)
                if scenario == "rollback_divergence":
                    with sqlite3.connect(self.last_target.parent / "source" / "youtube_memo.sqlite3") as connection:
                        self.assertEqual(connection.execute("select count(*) from memo").fetchone()[0], 2)

    def test_failed_handoff_never_restores_stale_or_unverifiable_compose_data(self):
        for scenario, stage in (("rollout_divergence", "recovery_data_divergence"),
                                ("rollout_unverifiable", "recovery_data_unverified")):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, f"youtube_memo_cutover=FAIL stage={stage}\n")
                self.assertNotIn('"start"', calls.read_text())
                self.assertEqual(self.last_state, {"compose": "exited", "replicas": 0, "helper": False})
                if scenario == "rollout_divergence":
                    with sqlite3.connect(self.last_target / "youtube_memo.sqlite3") as connection:
                        self.assertEqual(connection.execute("select count(*) from memo").fetchone()[0], 2)

    def test_existing_helper_name_collision_never_deletes_the_existing_pod(self):
        result, calls = self.run_cutover("--go", scenario="helper_collision")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"delete"', calls.read_text())
        self.assertNotIn('"stop"', calls.read_text())
        self.assertEqual(self.last_state, {"compose": "running", "replicas": 0, "helper": True})

    def test_lost_create_response_cleans_up_only_the_owned_helper(self):
        result, calls = self.run_cutover("--go", scenario="create_response_lost")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.last_state, {"compose": "running", "replicas": 0, "helper": False})
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        deletes = [command for command in commands if 'delete' in command]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(any(argument.startswith('--raw=') for argument in deletes[0]))

    def test_replaced_helper_uid_is_never_deleted(self):
        result, _ = self.run_cutover("--go", scenario="helper_replaced")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_cutover=FAIL stage=recovery_required\n")
        self.assertEqual(self.last_state, {"compose": "exited", "replicas": 0, "helper": True})


class YouTubeMemoPrepareTests(unittest.TestCase):
    def run_prepare(self, *arguments, secret_exists=True, listed_image=None,
                    foreign_resource_after_dry_run=False, unexpected_dry_run_resource=False,
                    sequential_dry_run_output=False, manifest_line_endings=None,
                    manifest_mutation=None, binder_scenario="ok",
                    existing_prepared=False, existing_mutation=None,
                    existing_api_defaults=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        binaries = base / "bin"
        binaries.mkdir()
        script = PREPARE
        if manifest_line_endings or manifest_mutation:
            tools = base / "tools"
            apps = base / "apps"
            tools.mkdir()
            apps.mkdir()
            script = tools / PREPARE.name
            script.write_text(PREPARE.read_text(encoding="utf-8"), encoding="utf-8")
            script.chmod(0o755)
            manifest = MANIFEST.read_text(encoding="utf-8")
            if manifest_mutation == "bad_replica":
                manifest = manifest.replace("  replicas: 0", "  replicas: 1", 1)
            elif manifest_mutation == "duplicate_sentinel":
                manifest += f"\n          image: {UNCONFIGURED_IMAGE}\n"
            elif manifest_mutation is not None:
                raise ValueError(f"unsupported manifest mutation: {manifest_mutation}")
            if manifest_line_endings == "crlf":
                manifest = manifest.replace("\n", "\r\n")
            elif manifest_line_endings is not None:
                raise ValueError(f"unsupported manifest line ending: {manifest_line_endings}")
            (apps / MANIFEST.name).write_text(manifest, encoding="utf-8", newline="")
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        fixture = {"image": image, "listed_image": listed_image or image,
                   "secret_exists": secret_exists, "applied": existing_prepared,
                   "foreign_resource_after_dry_run": foreign_resource_after_dry_run,
                   "foreign_resource_present": False,
                   "unexpected_dry_run_resource": unexpected_dry_run_resource,
                   "sequential_dry_run_output": sequential_dry_run_output,
                   "binder_scenario": binder_scenario,
                   "existing_mutation": existing_mutation,
                   "existing_api_defaults": existing_api_defaults,
                   "binder": None,
                   "binder_get_count": 0,
                   "pvc_bound": False}
        (base / "fixture").write_text(json.dumps(fixture))
        (base / "calls").touch()
        mock = r'''import json, os, pathlib, sys
base = pathlib.Path(os.environ["PREPARE_FIXTURE"])
fixture = json.loads((base / "fixture").read_text())
args = sys.argv[1:]
with (base / "calls").open("a") as handle:
    handle.write(json.dumps(args) + "\n")
if args[:3] != ["-n", "k3s", "kubectl"] and args[:4] != ["-n", "k3s", "ctr", "images"]:
    sys.exit(1)
if args[:4] == ["-n", "k3s", "ctr", "images"]:
    if args[4:] != ["list"]:
        sys.exit(1)
    digest = fixture["image"].split("@", 1)[1]
    print("REF TYPE DIGEST SIZE PLATFORMS LABELS")
    print(fixture["listed_image"] + " application/vnd.oci.image.manifest.v1+json " + digest + " 10MiB linux/amd64 -")
    sys.exit(0)
command = args[5:]
if command[:2] == ["get", "secret"]:
    sys.exit(0 if fixture["secret_exists"] else 1)
def pvc_document():
    return {"kind": "PersistentVolumeClaim", "metadata": {"name": "youtube-memo-data", "namespace": "personal-server"},
            "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
            "status": {"phase": "Bound" if fixture["pvc_bound"] else "Pending"}}
def deployment_document():
    document = {"kind": "Deployment", "metadata": {"name": "youtube-memo", "namespace": "personal-server", "labels": {"app.kubernetes.io/name": "youtube-memo"}},
      "spec": {"replicas": 0, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app.kubernetes.io/name": "youtube-memo"}},
      "template": {"metadata": {"labels": {"app.kubernetes.io/name": "youtube-memo"}}, "spec": {
      "automountServiceAccountToken": False,
      "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
      "containers": [{"name": "youtube-memo", "image": fixture["image"], "imagePullPolicy": "Never",
        "envFrom": [{"secretRef": {"name": "youtube-memo-runtime"}}],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "livenessProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "volumeMounts": [{"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"}, {"name": "tmp", "mountPath": "/tmp"}]}],
      "volumes": [{"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}}, {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}]}}}}
    if fixture["existing_mutation"] == "wrong_image":
        document["spec"]["template"]["spec"]["containers"][0]["image"] = "personal-server-youtube-memo:mutable"
    if fixture["existing_api_defaults"]:
        container = document["spec"]["template"]["spec"]["containers"][0]
        container["readinessProbe"]["httpGet"]["scheme"] = "HTTP"
        container["livenessProbe"]["httpGet"]["scheme"] = "HTTP"
    return document
def service_document():
    document = {"kind": "Service", "metadata": {"name": "youtube-memo", "namespace": "personal-server"},
                "spec": {"type": "ClusterIP", "selector": {"app.kubernetes.io/name": "youtube-memo"},
                         "ports": [{"name": "http", "port": 8002, "targetPort": "http"}]}}
    if fixture["existing_mutation"] == "bad_service":
        document["spec"]["type"] = "NodePort"
    if fixture["existing_api_defaults"]:
        document["spec"]["ports"][0]["protocol"] = "TCP"
    return document
if command[:1] == ["get"] and command[1] in {"pvc", "deployment", "service"}:
    resource = command[1]
    if fixture["existing_mutation"] == "missing_" + resource:
        sys.exit(0)
    if fixture["applied"] and command[-2:] == ["-o", "name"]:
        print(resource + "/" + ("youtube-memo-data" if resource == "pvc" else "youtube-memo"))
    if fixture["applied"] and command[-2:] == ["-o", "json"]:
        print(json.dumps({"pvc": pvc_document, "deployment": deployment_document, "service": service_document}[resource]()))
    if command[1] == "service" and fixture["foreign_resource_present"]:
        print("service/youtube-memo")
    sys.exit(0)
if command[:2] == ["get", "pods"]:
    pods = []
    if fixture["existing_mutation"] == "writer_pod":
        pods.append({"metadata": {"name": "foreign-writer"}, "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "youtube-memo-data"}}]}})
    print(json.dumps({"items": pods}))
    sys.exit(0)
if command[:2] == ["get", "pod"]:
    binder = fixture["binder"]
    if binder is None:
        sys.exit(0)
    fixture["binder_get_count"] += 1
    scenario = fixture["binder_scenario"]
    returned = json.loads(json.dumps(binder))
    if scenario == "binder_unowned":
        returned["metadata"]["labels"]["personal-server.io/pvc-binder-owner"] = "foreign-owner"
    elif scenario == "binder_replaced" and fixture["binder_get_count"] >= 2:
        returned["metadata"]["uid"] = "replacement-uid"
    (base / "fixture").write_text(json.dumps(fixture))
    if command[-2:] == ["-o", "name"]:
        print("pod/" + returned["metadata"]["name"])
    else:
        print(json.dumps(returned))
    sys.exit(0)
if command[:2] == ["create", "--dry-run=server"]:
    rendered = sys.stdin.read()
    if rendered.lstrip().startswith("{"):
        binder = json.loads(rendered)
        scenario = fixture["binder_scenario"]
        if scenario == "binder_server_hook":
            binder["spec"]["initContainers"] = [{"name": "writer", "image": fixture["image"]}]
        if scenario == "binder_server_ephemeral":
            binder["spec"]["ephemeralContainers"] = [{"name": "writer", "image": fixture["image"]}]
        if scenario == "binder_server_lifecycle":
            binder["spec"]["containers"][0]["lifecycle"] = {"postStart": {"exec": {"command": ["true"]}}}
        if scenario == "binder_server_environment":
            binder["spec"]["containers"][0]["env"] = [{"name": "WRITER_HOOK", "value": "blocked"}]
        if scenario == "binder_server_readiness_probe":
            binder["spec"]["containers"][0]["readinessProbe"] = {"exec": {"command": ["true"]}}
        if scenario == "binder_server_liveness_probe":
            binder["spec"]["containers"][0]["livenessProbe"] = {"exec": {"command": ["true"]}}
        if scenario == "binder_server_startup_probe":
            binder["spec"]["containers"][0]["startupProbe"] = {"exec": {"command": ["true"]}}
        print(json.dumps(binder))
        sys.exit(0)
    if "unconfigured-do-not-run" in rendered or fixture["image"] not in rendered or "replicas: 0" not in rendered:
        sys.exit(1)
    pvc = {"kind": "PersistentVolumeClaim", "metadata": {"name": "youtube-memo-data", "namespace": "personal-server"}, "spec": {"accessModes": ["ReadWriteOnce"]}}
    deployment = {"kind": "Deployment", "metadata": {"name": "youtube-memo", "namespace": "personal-server"}, "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": fixture["image"], "volumeMounts": [{"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"}, {"name": "tmp", "mountPath": "/tmp"}]}], "volumes": [{"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}}, {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}}]}}}}
    service = {"kind": "Service", "metadata": {"name": "youtube-memo", "namespace": "personal-server"}, "spec": {"selector": {"app.kubernetes.io/name": "youtube-memo"}}}
    items = [pvc, deployment, service]
    if fixture["unexpected_dry_run_resource"]:
        items.append({"kind": "ConfigMap", "metadata": {"name": "foreign", "namespace": "personal-server"}})
    (base / "dry-run-rendered").write_text(rendered)
    if fixture["sequential_dry_run_output"]:
        for item in items:
            print(json.dumps(item))
    else:
        print(json.dumps({"kind": "List", "items": items}))
    if fixture["foreign_resource_after_dry_run"]:
        fixture["foreign_resource_present"] = True
        (base / "fixture").write_text(json.dumps(fixture))
    sys.exit(0)
if command[:1] == ["create"]:
    rendered = sys.stdin.read()
    if rendered.lstrip().startswith("{"):
        if not fixture["applied"] or fixture["binder"] is not None:
            sys.exit(1)
        binder = json.loads(rendered)
        if binder.get("kind") != "Pod":
            sys.exit(1)
        binder.setdefault("metadata", {})["uid"] = "created-uid"
        fixture["binder"] = binder
        (base / "binder-rendered").write_text(json.dumps(binder))
        (base / "fixture").write_text(json.dumps(fixture))
        if fixture["binder_scenario"] == "binder_create_uncertain_owned":
            sys.exit(1)
        if fixture["binder_scenario"] == "binder_create_uncertain_unowned":
            fixture["binder"]["metadata"]["labels"]["personal-server.io/pvc-binder-owner"] = "foreign-owner"
            (base / "fixture").write_text(json.dumps(fixture))
            sys.exit(1)
        print(json.dumps(binder))
        sys.exit(0)
    if "unconfigured-do-not-run" in rendered or fixture["image"] not in rendered or "replicas: 0" not in rendered:
        sys.exit(1)
    if fixture["foreign_resource_present"]:
        sys.exit(1)
    (base / "create-rendered").write_text(rendered)
    fixture["applied"] = True
    (base / "fixture").write_text(json.dumps(fixture))
    sys.exit(0)
if command[:1] == ["wait"]:
    target = command[-2] if command[-1].startswith("--timeout=") else command[-1]
    scenario = fixture["binder_scenario"]
    if target.startswith("pod/"):
        if command[1] == "--for=delete":
            sys.exit(0 if fixture["binder"] is None else 1)
        if fixture["binder"] is None or scenario == "binder_not_ready":
            sys.exit(1)
        fixture["pvc_bound"] = True
        (base / "fixture").write_text(json.dumps(fixture))
        sys.exit(0)
    if target.startswith("pvc/"):
        sys.exit(0 if fixture["pvc_bound"] else 1)
    sys.exit(1)
if command[:1] == ["delete"] and command[1].startswith("--raw="):
    if fixture["binder"] is None:
        sys.exit(1)
    delete_options = json.load(sys.stdin)
    current = fixture["binder"]
    if fixture["binder_scenario"] == "binder_replaced":
        current = json.loads(json.dumps(current))
        current["metadata"]["uid"] = "replacement-uid"
    if delete_options.get("preconditions", {}).get("uid") != current["metadata"].get("uid"):
        sys.exit(1)
    fixture["binder"] = None
    (base / "fixture").write_text(json.dumps(fixture))
    sys.exit(0)
sys.exit(1)
'''
        executable = binaries / "sudo"
        executable.write_text(f"#!{sys.executable}\n" + mock)
        executable.chmod(0o755)
        environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                       "PREPARE_FIXTURE": str(base)}
        result = subprocess.run(["bash", str(script), *arguments], env=environment,
                                text=True, capture_output=True, timeout=20)
        self.last_prepare_rendered = tuple(
            (base / name).read_text() if (base / name).exists() else None
            for name in ("dry-run-rendered", "create-rendered")
        )
        self.last_binder_rendered = (
            json.loads((base / "binder-rendered").read_text())
            if (base / "binder-rendered").exists() else None
        )
        self.last_prepare_fixture = json.loads((base / "fixture").read_text())
        return result, (base / "calls").read_text()

    def test_prepare_applies_only_rendered_replica_zero_resources(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare("--go", "--image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "youtube_memo_prepare=PASS\n")
        self.assertIn('"create", "--dry-run=server"', calls)
        self.assertIn('"create", "-f", "-"', calls)
        self.assertEqual(self.last_prepare_rendered[0], self.last_prepare_rendered[1])
        self.assertNotIn("docker", calls)
        self.assertNotIn("scale", calls)

    def test_prepare_binds_pending_pvc_with_owned_nonwriter_binder_then_removes_it(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare("--go", "--image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "youtube_memo_prepare=PASS\n")
        binder = self.last_binder_rendered
        self.assertIsNotNone(binder)
        self.assertEqual(binder["metadata"]["namespace"], "personal-server")
        self.assertRegex(binder["metadata"]["name"], r"^youtube-memo-pvc-binder-[0-9a-f]{32}$")
        labels = binder["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/managed-by"], "youtube-memo-prepare")
        self.assertRegex(labels["personal-server.io/pvc-binder-owner"], r"^[0-9a-f]{32}$")
        pod = binder["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["securityContext"], {
            "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"},
        })
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["volumes"], [{"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}}])
        self.assertEqual(len(pod["containers"]), 1)
        container = pod["containers"][0]
        self.assertEqual(container["image"], image)
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertEqual(container["command"], ["python3", "-c", "import time; time.sleep(180)"])
        self.assertEqual(container["volumeMounts"], [{
            "name": "youtube-memo-data", "mountPath": "/data/youtube-memo", "readOnly": True,
        }])
        self.assertEqual(container["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        })
        self.assertNotIn("env", container)
        self.assertNotIn("envFrom", container)
        self.assertTrue(self.last_prepare_fixture["applied"])
        self.assertTrue(self.last_prepare_fixture["pvc_bound"])
        self.assertIsNone(self.last_prepare_fixture["binder"])
        commands = [json.loads(line) for line in calls.splitlines()]
        self.assertTrue(any("--for=condition=Ready" in command for command in commands))
        self.assertTrue(any("--for=jsonpath={.status.phase}=Bound" in command for command in commands))
        self.assertTrue(any(any(item.startswith("--raw=") for item in command) for command in commands))
        self.assertTrue(any("--for=delete" in command for command in commands))

    def test_prepare_binder_failure_never_deletes_base_resources_or_foreign_binder(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for scenario in ("binder_not_ready", "binder_unowned", "binder_replaced"):
            with self.subTest(scenario=scenario):
                result, calls = self.run_prepare(
                    "--go", "--image", image, binder_scenario=scenario,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r"^youtube_memo_prepare=FAIL stage=(pvc_bind|recovery)\n$")
                self.assertTrue(self.last_prepare_fixture["applied"])
                self.assertNotIn('"delete", "pvc"', calls)
                self.assertNotIn('"delete", "deployment"', calls)
                self.assertNotIn('"delete", "service"', calls)
                if scenario in {"binder_unowned", "binder_replaced"}:
                    self.assertNotIn("--raw=", calls)
                    self.assertIsNotNone(self.last_prepare_fixture["binder"])

    def test_prepare_rejects_server_dry_run_binder_writer_hooks_before_actual_create(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for scenario in (
            "binder_server_hook", "binder_server_ephemeral", "binder_server_lifecycle", "binder_server_environment",
            "binder_server_readiness_probe", "binder_server_liveness_probe", "binder_server_startup_probe",
        ):
            with self.subTest(scenario=scenario):
                result, calls = self.run_prepare("--go", "--image", image, binder_scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=binder_contract\n")
                commands = [json.loads(line) for line in calls.splitlines()]
                self.assertTrue(any("--dry-run=server" in command for command in commands))
                self.assertFalse(any(command[-5:] == ["create", "-o", "json", "-f", "-"] for command in commands))
                self.assertIsNone(self.last_prepare_fixture["binder"])

    def test_prepare_uncertain_binder_create_recovers_only_verified_owned_pod(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for scenario, raw_delete_expected in (
            ("binder_create_uncertain_owned", True),
            ("binder_create_uncertain_unowned", False),
        ):
            with self.subTest(scenario=scenario):
                result, calls = self.run_prepare("--go", "--image", image, binder_scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=recovery\n")
                self.assertEqual("--raw=" in calls, raw_delete_expected)
                self.assertTrue(self.last_prepare_fixture["applied"])
                if raw_delete_expected:
                    self.assertIsNone(self.last_prepare_fixture["binder"])
                else:
                    self.assertIsNotNone(self.last_prepare_fixture["binder"])

    def test_bind_existing_only_binds_exact_inert_resources_without_persistent_create(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--bind-existing", "--image", image, existing_prepared=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "youtube_memo_prepare=PASS\n")
        self.assertTrue(self.last_prepare_fixture["pvc_bound"])
        self.assertIsNone(self.last_prepare_fixture["binder"])
        self.assertIsNone(self.last_prepare_rendered[0])
        self.assertIsNone(self.last_prepare_rendered[1])
        commands = [json.loads(line) for line in calls.splitlines()]
        self.assertTrue(any(command[-5:] == ["get", "pvc", "youtube-memo-data", "-o", "json"] for command in commands))
        self.assertTrue(any(command[-5:] == ["get", "deployment", "youtube-memo", "-o", "json"] for command in commands))
        self.assertTrue(any(command[-5:] == ["get", "service", "youtube-memo", "-o", "json"] for command in commands))
        self.assertFalse(any(command[-3:] == ["create", "-f", "-"] for command in commands))

    def test_prepare_and_bind_existing_accept_only_kubernetes_probe_and_service_defaults(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for arguments, prepared in (
            (("--go", "--image", image), False),
            (("--bind-existing", "--image", image), True),
        ):
            with self.subTest(arguments=arguments):
                result, _ = self.run_prepare(
                    *arguments, existing_prepared=prepared, existing_api_defaults=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "youtube_memo_prepare=PASS\n")

    def test_bind_existing_rejects_missing_mutated_or_writer_resources_without_binder(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for mutation in ("missing_deployment", "wrong_image", "bad_service", "writer_pod"):
            with self.subTest(mutation=mutation):
                result, calls = self.run_prepare(
                    "--bind-existing", "--image", image,
                    existing_prepared=True, existing_mutation=mutation,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=existing\n")
                self.assertIsNone(self.last_prepare_fixture["binder"])
                self.assertNotIn("--raw=", calls)

    def test_prepare_rejects_combined_create_and_bind_existing_modes_without_external_calls(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--bind-existing", "--image", image, existing_prepared=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=arguments\n")
        self.assertEqual(calls, "")

    def test_prepare_missing_secret_stops_before_image_or_resource_changes(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare("--go", "--image", image, secret_exists=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=secret\n")
        self.assertNotIn("ctr", calls)
        self.assertNotIn("create", calls)

    def test_prepare_rejects_tag_only_listing_before_any_resource_create(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--image", image,
            listed_image="docker.io/library/personal-server-youtube-memo:k3s-test",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=image\n")
        self.assertIn("ctr", calls)
        self.assertNotIn("create", calls)

    def test_prepare_fails_closed_when_resource_appears_after_dry_run(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--image", image, foreign_resource_after_dry_run=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=create\n")
        self.assertIn('"create", "--dry-run=server"', calls)
        self.assertIn('"create", "-f", "-"', calls)
        self.assertNotIn("apply", calls)

    def test_prepare_rejects_any_unexpected_rendered_resource_before_create(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--image", image, unexpected_dry_run_resource=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=server_dry_run\n")
        self.assertIn('"create", "--dry-run=server"', calls)
        self.assertNotIn('"create", "-f", "-"', calls)

    def test_prepare_accepts_exact_sequential_json_objects_from_server_dry_run(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--image", image, sequential_dry_run_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"create", "--dry-run=server"', calls)
        self.assertIn('"create", "-f", "-"', calls)

    def test_prepare_accepts_equivalent_crlf_manifest_without_relaxing_inert_contract(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        result, calls = self.run_prepare(
            "--go", "--image", image, manifest_line_endings="crlf",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "youtube_memo_prepare=PASS\n")
        self.assertIn('"create", "--dry-run=server"', calls)
        self.assertIn('"create", "-f", "-"', calls)

    def test_prepare_rejects_crlf_manifest_without_exact_inert_contract(self):
        image = "docker.io/library/personal-server-youtube-memo@sha256:" + "a" * 64
        for mutation in ("bad_replica", "duplicate_sentinel"):
            with self.subTest(mutation=mutation):
                result, calls = self.run_prepare(
                    "--go", "--image", image, manifest_line_endings="crlf",
                    manifest_mutation=mutation,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "youtube_memo_prepare=FAIL stage=manifest\n")
                self.assertEqual(calls, "")


class YouTubeMemoManifestTests(unittest.TestCase):
    def assert_manifest_contract(self, documents):
        self.assertEqual(
            [document["kind"] for document in documents],
            ["PersistentVolumeClaim", "Deployment", "Service"],
        )
        pvc, deployment, service = documents
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pvc["metadata"], {"name": "youtube-memo-data", "namespace": "personal-server"})
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
        self.assertEqual(deployment["spec"]["replicas"], 0)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(len(pod["containers"]), 1)
        container = pod["containers"][0]
        self.assertEqual(container["image"], UNCONFIGURED_IMAGE)
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(
            container["volumeMounts"],
            [
                {"name": "youtube-memo-data", "mountPath": "/data/youtube-memo"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        )
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(
            volumes["youtube-memo-data"],
            {"name": "youtube-memo-data", "persistentVolumeClaim": {"claimName": "youtube-memo-data"}},
        )
        self.assertEqual(volumes["tmp"]["emptyDir"]["medium"], "Memory")
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertNotIn("env", container)
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "youtube-memo-runtime"}}])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 8002, "targetPort": "http"}])
        self.assertNotIn("nodePort", yaml.safe_dump(service))

    def test_manifest_contract_rejects_secret_inline_env_rolling_update_and_extra_writers(self):
        documents = [item for item in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if item]

        mutations = []

        secret_document = deepcopy(documents)
        secret_document.insert(0, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "youtube-memo-runtime"}})
        mutations.append(secret_document)

        inline_environment = deepcopy(documents)
        deployment = next(item for item in inline_environment if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        container.pop("envFrom")
        container["env"] = [{"name": "YOUTUBE_MEMO_DB_PATH", "value": "/tmp/youtube_memo.sqlite3"}]
        mutations.append(inline_environment)

        rolling_update = deepcopy(documents)
        deployment = next(item for item in rolling_update if item["kind"] == "Deployment")
        deployment["spec"]["strategy"] = {"type": "RollingUpdate"}
        mutations.append(rolling_update)

        second_writer = deepcopy(documents)
        deployment = next(item for item in second_writer if item["kind"] == "Deployment")
        deployment["spec"]["replicas"] = 2
        mutations.append(second_writer)

        extra_container = deepcopy(documents)
        deployment = next(item for item in extra_container if item["kind"] == "Deployment")
        deployment["spec"]["template"]["spec"]["containers"].append(
            deepcopy(deployment["spec"]["template"]["spec"]["containers"][0])
        )
        mutations.append(extra_container)

        for mutation in mutations:
            with self.assertRaises(AssertionError):
                self.assert_manifest_contract(mutation)

    def test_youtube_memo_manifest_has_single_nonroot_writer_and_clusterip_service(self):
        documents = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        service = next(item for item in documents if item["kind"] == "Service")
        self.assertEqual(deployment["spec"]["replicas"], 0)
        self.assertTrue(deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertNotIn("nodePort", yaml.safe_dump(service))

    def test_youtube_memo_manifest_uses_its_pvc_and_hardened_local_image_contract(self):
        documents = [item for item in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if item]
        self.assert_manifest_contract(documents)

    def test_prepare_contract_keeps_static_manifest_inert_and_requires_immutable_image(self):
        documents = [item for item in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if item]
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]

        self.assertEqual(deployment["spec"]["replicas"], 0)
        self.assertEqual(container["image"], UNCONFIGURED_IMAGE)
        self.assertTrue(PREPARE.is_file())

        source = PREPARE.read_text(encoding="utf-8")
        self.assertIn('--go)', source)
        self.assertIn('^docker\\.io/library/personal-server-youtube-memo@sha256:', source)
        self.assertIn('get secret youtube-memo-runtime', source)
        self.assertIn('ctr images list', source)
        self.assertIn('create --dry-run=server', source)
        self.assertNotIn('"docker"', source)
        self.assertNotIn('--replicas=1', source)
        self.assertNotIn('.data', source)


if __name__ == "__main__":
    unittest.main()
