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
MANIFEST = ROOT / "infra" / "k8s" / "apps" / "book-memo.yaml"
CUTOVER = ROOT / "infra" / "k8s" / "tools" / "book-memo-cutover.sh"


class BookMemoCutoverTests(unittest.TestCase):
    """Exercise the operator script with isolated Docker/K3s executables."""

    def run_cutover(self, *modes, compose_state="exited", scenario="ok", image=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        source, target, binaries = (base / name for name in ("source", "target", "bin"))
        for item in (source, target, binaries):
            item.mkdir()
        with sqlite3.connect(source / "memo.sqlite3") as connection:
            connection.execute("create table memo (text text)")
            connection.execute("insert into memo values ('private-fixture')")
        if scenario == "bad_sqlite":
            (source / "memo.sqlite3").write_text("not a database")
        if scenario == "symlink":
            (source / "link").symlink_to(source / "memo.sqlite3")
        if scenario == "occupied_pvc":
            (target / "keep").write_text("existing")
        calls = base / "calls"
        calls.touch()
        state = {"compose": "running", "replicas": 0, "helper": False}
        if modes == ("--rollback",):
            state.update(compose="exited", replicas=1)
            (target / "memo.sqlite3").write_bytes((source / "memo.sqlite3").read_bytes())
            if scenario == "changed_data":
                with sqlite3.connect(target / "memo.sqlite3") as connection:
                    connection.execute("insert into memo values ('new-data')")
        (base / "state").write_text(json.dumps(state))
        chosen_image = image or "personal-server-book-memo@sha256:" + "a" * 64
        fixture = {"base": str(base), "source": str(source), "target": str(target),
                   "scenario": scenario, "after_stop": compose_state, "image": chosen_image}
        deployed = next(item for item in yaml.safe_load_all(MANIFEST.read_text()) if item["kind"] == "Deployment")
        deployed["spec"]["template"]["spec"]["containers"][0]["image"] = chosen_image
        if scenario == "foreign_volume":
            deployed["spec"]["template"]["spec"]["volumes"].append(
                {"name": "foreign", "persistentVolumeClaim": {"claimName": "other-data"}})
        if scenario == "init_writer":
            deployed["spec"]["template"]["spec"]["initContainers"] = [{"name": "writer", "image": chosen_image}]
        fixture["deployment"] = deployed
        (base / "fixture").write_text(json.dumps(fixture))
        mock = r'''import json, os, pathlib, subprocess, sys
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
            output([{'Type': 'bind', 'Source': fixture['source'], 'Destination': '/data/book-memo', 'RW': True}])
        else: fail()
    elif args[:1] == ['stop']:
        state['compose'] = fixture['after_stop']; save()
    elif args[:1] == ['start']:
        state['compose'] = 'running'; save()
    else: fail()
elif name == 'sudo':
    if args[:3] == ['-n', 'k3s', 'ctr']:
        if args[3:] != ['images', 'list']: fail()
        if scenario != 'missing_image':
            size = '10 MiB' if scenario == 'ctr_size_unit' else '10MiB'
            platform = 'linux/arm64' if scenario == 'wrong_platform' else 'linux/amd64'
            print(fixture['image'] + ' application/vnd.oci.image.manifest.v1+json sha256:' + 'a'*64 + ' ' + size + ' ' + platform + ' -')
    else:
        if args[:5] != ['-n', 'k3s', 'kubectl', '-n', 'personal-server']: fail()
        command = args[5:]
        if command[:2] == ['get', 'deployment']:
            document = fixture['deployment']
            document['spec']['replicas'] = state['replicas']
            output(document)
        elif command[:2] == ['get', 'pods']:
            pods = []
            if state['replicas'] or scenario == 'foreign_writer':
                pods.append({'metadata':{'name':'book-memo-writer'}, 'spec': {
                    'volumes':[{'persistentVolumeClaim':{'claimName':'book-memo-data'}}]}})
            if state['helper']:
                pods.append({'metadata':{'name':'book-memo-cutover-data'}, 'spec': {
                    'volumes':[{'persistentVolumeClaim':{'claimName':'book-memo-data'}}]}})
            output({'items':pods})
        elif command[:2] == ['get', 'pvc']:
            output({'metadata':{'name':'book-memo-data'}, 'status':{'phase': 'Pending' if scenario == 'pending_pvc' else 'Bound'},
                    'spec':{'accessModes':['ReadWriteOnce']}})
        elif command[:2] == ['apply', '--dry-run=server']:
            pass
        elif command[:1] == ['create']:
            payload = json.load(sys.stdin)
            assert payload['kind'] == 'Pod'
            assert payload['spec']['containers'][0]['image'] == fixture['image']
            assert 'envFrom' not in payload['spec']['containers'][0]
            state['helper'] = True; save()
        elif command[:1] == ['wait']:
            pass
        elif command[:2] == ['delete', 'pod']:
            state['helper'] = False; save()
        elif command[:1] == ['exec']:
            separator = command.index('--')
            child = command[separator+1:]
            assert child[:2] == ['python3', '-c']
            child = [sys.executable, *child[1:]]
            child = [fixture['target'] if part == '/data/book-memo' else part for part in child]
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
            ["bash", str(CUTOVER), *modes, "--source", str(source), "--database", "memo.sqlite3", "--image", chosen_image],
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
        self.assertIn("book_memo_cutover=FAIL", result.stderr)
        self.assertNotIn('"create"', calls.read_text())
        self.assertEqual(list(self.last_target.iterdir()), [])

    def test_combined_go_and_rollback_is_rejected_without_external_calls(self):
        result, calls = self.run_cutover("--go", "--rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.read_text(), "")

    def test_missing_repeated_and_unknown_modes_make_no_external_calls(self):
        for modes in ((), ("--go", "--go"), ("--invalid",)):
            with self.subTest(modes=modes):
                result, calls = self.run_cutover(*modes)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("book_memo_cutover=FAIL", result.stderr)
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
        for scenario in ("missing_image", "wrong_platform", "pending_pvc", "foreign_writer", "symlink"):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"stop"', calls.read_text())
                self.assertEqual(self.last_state["compose"], "running")

    def test_mutable_or_foreign_image_is_rejected_without_external_calls(self):
        for image in ("personal-server-book-memo:latest", "portal-web@sha256:" + "a"*64):
            with self.subTest(image=image):
                result, calls = self.run_cutover("--go", image=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls.read_text(), "")

    def test_go_verifies_data_and_hands_over_one_writer(self):
        result, calls = self.run_cutover("--go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("book_memo_cutover=PASS", result.stdout)
        self.assertEqual(self.last_state, {"compose": "exited", "replicas": 1, "helper": False})
        with sqlite3.connect(self.last_target / "memo.sqlite3") as connection:
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
                self.assertRegex(result.stderr, r"^book_memo_cutover=FAIL stage=[a-z_]+\n$")
                self.assertEqual(self.last_state, {"compose": "running", "replicas": 0, "helper": False})
                if scenario == "occupied_pvc":
                    self.assertEqual((self.last_target / "keep").read_text(), "existing")

    def test_containerd_human_readable_size_does_not_hide_amd64_platform(self):
        result, _ = self.run_cutover("--check", scenario="ctr_size_unit")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_foreign_volume_or_init_writer_blocks_handoff_before_compose_stop(self):
        for scenario in ("foreign_volume", "init_writer"):
            with self.subTest(scenario=scenario):
                result, calls = self.run_cutover("--go", scenario=scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("stage=deployment", result.stderr)
                self.assertNotIn('"stop"', calls.read_text())

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


class BookMemoManifestTests(unittest.TestCase):
    def assert_manifest_contract(self, documents):
        self.assertEqual(
            [document["kind"] for document in documents],
            ["PersistentVolumeClaim", "Deployment", "Service"],
        )
        pvc, deployment, service = documents
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pvc["metadata"], {"name": "book-memo-data", "namespace": "personal-server"})
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(len(pod["containers"]), 1)
        container = pod["containers"][0]
        self.assertEqual(container["image"], "personal-server-book-memo:v1")
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(
            container["volumeMounts"],
            [
                {"name": "book-memo-data", "mountPath": "/data/book-memo"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        )
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(
            volumes["book-memo-data"],
            {"name": "book-memo-data", "persistentVolumeClaim": {"claimName": "book-memo-data"}},
        )
        self.assertEqual(volumes["tmp"]["emptyDir"]["medium"], "Memory")
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertNotIn("env", container)
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "book-memo-runtime"}}])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 8003, "targetPort": "http"}])
        self.assertNotIn("nodePort", yaml.safe_dump(service))

    def test_manifest_contract_rejects_secret_inline_env_rolling_update_and_extra_writers(self):
        documents = [item for item in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if item]

        mutations = []

        secret_document = deepcopy(documents)
        secret_document.insert(0, {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "book-memo-runtime"}})
        mutations.append(secret_document)

        inline_environment = deepcopy(documents)
        deployment = next(item for item in inline_environment if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        container.pop("envFrom")
        container["env"] = [{"name": "BOOK_MEMO_DB_PATH", "value": "/tmp/book_memo.sqlite3"}]
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

    def test_book_memo_manifest_has_single_nonroot_writer_and_clusterip_service(self):
        documents = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        service = next(item for item in documents if item["kind"] == "Service")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertTrue(deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertNotIn("nodePort", yaml.safe_dump(service))

    def test_book_memo_manifest_uses_its_pvc_and_hardened_local_image_contract(self):
        documents = [item for item in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if item]
        self.assert_manifest_contract(documents)


if __name__ == "__main__":
    unittest.main()
