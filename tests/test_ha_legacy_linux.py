"""Closed live graph/schema/capability evidence; never connects to N100."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/k8s"))
from ha_control import legacy_linux as graph


def fixture():
    resources = [graph.record("docker", hashlib.sha256(service.encode()).hexdigest(), service, {"service": service}, {"stopped": True}, graph.capability(recreator_disabled=True)) for service in sorted(graph.SERVICES)]
    protected = graph.record("docker", "protected", "", {}, {}, graph.capability(isolated=True, writable=False), image="sha256:" + "d" * 64)
    resources.append(protected)
    tcb = [{"pid": 100, "start_ticks": 44, "uid": 0, "exe_sha256": "1" * 64, "cmdline_sha256": "2" * 64, "cgroup_sha256": "3" * 64, "unit_id": "docker.service", "unit_spec_sha256": "4" * 64}]
    snapshot = dict(uid=0, boot_id="11111111-1111-1111-1111-111111111111", engine_id="synthetic-engine", kube_system_uid="22222222-2222-2222-2222-222222222222", providers={p: graph.digest({"provider": p}) for p in graph.PROVIDERS}, resources=resources, tcb_processes=tcb, complete=True)
    pins = [{k: value[k] for k in ("provider", "id", "service", "spec_sha256", "runtime_sha256")} | {"role": "protected" if value["id"] == "protected" else "legacy"} for value in resources]
    pins[-1].update(image=protected["image"], profile="service", state_mounts=[])
    spec = {k: deepcopy(snapshot[k]) for k in ("uid", "boot_id", "engine_id", "kube_system_uid", "providers", "tcb_processes")}
    spec.update(schema_version=1, resources=pins)
    request = dict(schema_version=1, nonce="a" * 64, plan_sha256="b" * 64, cluster="synthetic", generation=3, collector_sha256="c" * 64, spec=spec)
    return request, snapshot


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.request, self.snapshot = fixture()

    def evaluate(self, collector=None):
        with patch.object(graph, "__collector_sha256__", "c" * 64, create=True):
            return graph.evaluate(self.request, collector=collector or (lambda budget: deepcopy(self.snapshot)))

    def test_verified_response_is_fresh_bound_and_has_no_inventory_or_source_values(self):
        calls = []
        def collect(budget):
            calls.append(id(budget))
            budget.left()
            return deepcopy(self.snapshot)
        result = self.evaluate(collect)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(set(result), graph.REQUEST - {"spec"} | {"complete", "verified"})
        self.assertIs(result["complete"], True)
        self.assertIs(result["verified"], True)
        self.assertNotIn("resources", json.dumps(result))
        self.assertNotIn("synthetic-engine", json.dumps(result))

    def test_client_claim_alone_never_attests_collector_source(self):
        with patch.object(graph, "__collector_sha256__", None, create=True), self.assertRaisesRegex(graph.Blocked, "verified_collector_source_required"):
            graph.evaluate(self.request, collector=lambda b: self.snapshot)
        with patch.object(graph, "__collector_sha256__", "d" * 64, create=True), self.assertRaisesRegex(graph.Blocked, "verified_collector_source_required"):
            graph.evaluate(self.request, collector=lambda b: self.snapshot)

    def test_duplicate_json_keys_bool_integers_unknown_fields_and_null_are_rejected(self):
        with self.assertRaisesRegex(graph.Blocked, "duplicate_json_key"):
            graph.decode('{"schema_version":1,"schema_version":1}')
        mutations = [lambda r: r.update(generation=True), lambda r: r.update(schema_version=True), lambda r: r.update(unknown=1), lambda r: r["spec"].update(uid=False), lambda r: r["spec"].update(resources=None), lambda r: r["spec"]["resources"][0].update(id=None), lambda r: r["spec"]["tcb_processes"][0].update(pid=True)]
        for mutate in mutations:
            request = deepcopy(self.request)
            mutate(request)
            with self.subTest(case=mutations.index(mutate)), self.assertRaises(graph.Blocked):
                graph.validate_request(request)

    def test_unknown_missing_duplicate_or_relabelled_service_resource_fails(self):
        for change in ("unknown", "missing", "duplicate", "service", "spec", "restart"):
            snapshot = deepcopy(self.snapshot)
            if change == "unknown": snapshot["resources"].append(dict(snapshot["resources"][0], id="unknown"))
            elif change == "missing": snapshot["resources"].pop(0)
            elif change == "duplicate": snapshot["resources"].append(deepcopy(snapshot["resources"][0]))
            elif change == "service": snapshot["resources"][0]["service"] = ""
            elif change == "spec": snapshot["resources"][0]["spec_sha256"] = "f" * 64
            else: snapshot["resources"][0]["capabilities"]["restart"] = "always"
            with self.subTest(change=change), self.assertRaises(graph.Blocked):
                graph.validate_snapshot(self.request, snapshot)

    def test_seven_service_coverage_cannot_be_omitted_in_both_plan_and_live_graph(self):
        self.request["spec"]["resources"].pop(0)
        self.snapshot["resources"].pop(0)
        with self.assertRaisesRegex(graph.Blocked, "seven_service_inventory_required"):
            graph.validate_snapshot(self.request, self.snapshot)

    def test_protected_capabilities_cannot_be_asserted_by_plan_role(self):
        for changes in ({"writable": True}, {"management": True}, {"privileged": True}, {"host_namespace": True}, {"isolated": False}):
            snapshot = deepcopy(self.snapshot)
            snapshot["resources"][-1]["capabilities"].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(graph.Blocked, "protected_capability_unverified"):
                graph.validate_snapshot(self.request, snapshot)

    def test_legacy_pending_pods_active_jobs_desired_replicas_and_cron_are_denied(self):
        for changes in ({"running": True}, {"desired": 1}, {"active": 1}, {"suspended": False}, {"recreator_disabled": False}):
            snapshot = deepcopy(self.snapshot)
            snapshot["resources"][0]["capabilities"].update(changes)
            with self.assertRaisesRegex(graph.Blocked, "legacy_writer_or_recreator_active"):
                graph.validate_snapshot(self.request, snapshot)

    def test_managed_ha_requires_actual_image_label_generation_and_service(self):
        observed = self.snapshot["resources"][0]
        observed.update(image="sha256:" + "d" * 64, cluster="synthetic", generation=3)
        observed["capabilities"].update(running=True, active=1, suspended=False)
        self.request["spec"]["resources"][0].update(role="managed_ha", image=observed["image"], cluster="synthetic", generation=3)
        graph.validate_snapshot(self.request, self.snapshot)
        for changes in ({"generation": 2}, {"generation": True}, {"image": "sha256:" + "e" * 64}, {"cluster": "foreign"}):
            snapshot = deepcopy(self.snapshot)
            snapshot["resources"][0].update(changes)
            with self.assertRaises(graph.Blocked):
                graph.validate_snapshot(self.request, snapshot)

    def test_provider_identity_tcb_or_boot_changes_and_incomplete_reads_are_denied(self):
        for change in ("uid", "boot", "engine", "kube", "provider", "tcb", "complete"):
            snapshot = deepcopy(self.snapshot)
            if change == "uid": snapshot["uid"] = 1000
            elif change == "boot": snapshot["boot_id"] = "0" * 36
            elif change == "engine": snapshot["engine_id"] = "other"
            elif change == "kube": snapshot["kube_system_uid"] = "other"
            elif change == "provider": snapshot["providers"]["mounts"] = "0" * 64
            elif change == "tcb": snapshot["tcb_processes"][0]["start_ticks"] += 1
            else: snapshot["complete"] = False
            with self.subTest(change=change), self.assertRaises(graph.Blocked):
                graph.validate_snapshot(self.request, snapshot)

    def test_race_between_two_independently_valid_samples_is_not_accepted(self):
        second = deepcopy(self.snapshot)
        # Both legacy capability states are individually safe; their variation
        # still proves collection was not one stable closed graph.
        second["resources"][0]["capabilities"]["isolated"] = True
        samples = iter([self.snapshot, second])
        with self.assertRaisesRegex(graph.Blocked, "live_graph_changed_during_collection"):
            self.evaluate(lambda b: deepcopy(next(samples)))

    def test_shared_budget_enforces_deadline_and_total_payload(self):
        with patch.object(graph.time, "monotonic", side_effect=[0, 2]), self.assertRaisesRegex(graph.Blocked, "live_graph_deadline_exceeded"):
            self.evaluate()
        def overflow(budget):
            budget.count(b"x" * (graph.LIMIT + 1))
        with self.assertRaisesRegex(graph.Blocked, "live_graph_payload_exceeded"):
            self.evaluate(overflow)

    def test_unprivileged_linux_reader_has_no_automatic_elevation_or_partial_success(self):
        with patch.object(graph.sys, "platform", "linux"), patch.object(graph.os, "geteuid", return_value=1000), patch.object(graph, "collect_docker") as docker:
            with self.assertRaisesRegex(graph.Blocked, "complete_linux_reader_required"):
                graph.collect_snapshot(graph.Budget())
            docker.assert_not_called()

    def test_provider_failure_never_prints_exception_credential_paths_or_args(self):
        output = io.StringIO()
        with patch.object(graph.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(self.request).encode()))), patch.object(graph.sys, "stdout", output), patch.object(graph, "evaluate", side_effect=OSError("private credentials path endpoint SQL")):
            code = graph.main()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "blocked", "reason": "linux_live_graph_unavailable"})

    @patch.object(graph, "verify_kubeconfig", new=lambda budget: None)
    def test_unknown_kubernetes_provider_is_not_silently_skipped(self):
        class FakeBudget:
            def command(self, args):
                return b"pods\nunknowns.external.invalid\n" if "api-resources" in args else b'{"items":[]}'
        with self.assertRaisesRegex(graph.Blocked, "unsupported_kubernetes_provider"):
            graph.collect_kubernetes(FakeBudget())

    def test_actual_container_capabilities_not_name_determine_protected_eligibility(self):
        identifier = "a" * 64
        container = dict(Id=identifier, Image="sha256:" + "b" * 64, Config=dict(Labels={}, Env=[]), State=dict(Running=False, Restarting=False),
                         HostConfig=dict(ReadonlyRootfs=True, NetworkMode="none", CapDrop=["ALL"], CapAdd=[], SecurityOpt=["no-new-privileges"], Privileged=False, RestartPolicy={"Name": "no"}), Mounts=[])
        class FakeBudget:
            def docker(self, endpoint):
                if endpoint == "info": return {"ID": "synthetic-engine", "Swarm": {"LocalNodeState": "inactive"}}
                if endpoint == "plugins": return []
                if endpoint == "containers/json?all=1": return [{"Id": identifier}]
                if endpoint.startswith("containers/"): return deepcopy(container)
                return []
        _, _, records, _ = graph.collect_docker(FakeBudget())
        self.assertTrue(records[0]["capabilities"]["isolated"])
        self.assertFalse(records[0]["capabilities"]["writable"])
        container["Mounts"] = [{"Source": "/var/run/docker.sock", "Destination": "/control", "RW": False}]
        with patch.object(graph, "canonical_source", side_effect=lambda path: "file:" + path):
            _, _, records, _ = graph.collect_docker(FakeBudget())
        self.assertTrue(records[0]["capabilities"]["management"])
        self.assertFalse(records[0]["capabilities"]["isolated"])

    def test_network_service_with_separate_own_state_is_allowed_but_app_ancestor_is_not(self):
        # Networking and Caddy's own certificate state are not app-data writes.
        old = {"Id": self.snapshot["resources"][0]["id"], "Config": {"Labels": {"com.docker.compose.service": self.snapshot["resources"][0]["service"]}}, "Mounts": [{"Source": "/data/app", "RW": True}]}
        caddy = {"Id": "protected", "Config": {"Labels": {}}, "Mounts": [{"Source": "/data/caddy", "RW": True}]}
        own = hashlib.sha256(b"file:/data/caddy").hexdigest()
        self.snapshot["resources"][-1]["state_mounts"] = [own]
        self.request["spec"]["resources"][-1]["state_mounts"] = [own]
        with patch.object(graph, "canonical_source", side_effect=lambda p: "file:" + p):
            graph.protect_app_storage(self.snapshot["resources"], {"containers": [old, caddy]}, {"objects": []})
            graph.validate_snapshot(self.request, self.snapshot)
            caddy["Mounts"][0]["Source"] = "/data/app/attachments"
            graph.protect_app_storage(self.snapshot["resources"], {"containers": [old, caddy]}, {"objects": []})
        with self.assertRaisesRegex(graph.Blocked, "protected_capability_unverified"):
            graph.validate_snapshot(self.request, self.snapshot)

    def test_protected_own_state_and_image_are_explicit_exact_pins(self):
        for field, value in (("state_mounts", ["1" * 64]), ("image", "sha256:" + "1" * 64)):
            snapshot = deepcopy(self.snapshot)
            snapshot["resources"][-1][field] = value
            with self.assertRaisesRegex(graph.Blocked, "protected_state_or_image_changed"):
                graph.validate_snapshot(self.request, snapshot)

    def readonly_pod(self):
        account = {"kind": "ServiceAccount", "metadata": {"name": "reader", "namespace": "fixture", "uid": "sa"}}
        role = {"kind": "ClusterRole", "metadata": {"name": "reader", "uid": "role"}, "rules": [{"apiGroups": [""], "resources": ["pods", "services", "endpoints"], "verbs": ["get", "list", "watch"]}]}
        binding = {"kind": "ClusterRoleBinding", "metadata": {"name": "reader", "uid": "binding"}, "roleRef": {"kind": "ClusterRole", "name": "reader"}, "subjects": [{"kind": "ServiceAccount", "name": "reader", "namespace": "fixture"}]}
        pod = {"kind": "Pod", "metadata": {"name": "dns-reader", "namespace": "fixture", "uid": "pod"}, "spec": {"serviceAccountName": "reader", "containers": [{"name": "reader", "image": "fixture@sha256:" + "a" * 64, "securityContext": {"runAsUser": 10001, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "volumeMounts": [{"name": "token", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount", "readOnly": True}]}], "volumes": [{"name": "token", "projected": {"sources": [{"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}}]}}]}}
        return pod, [account, role, binding, pod]

    def test_generic_readonly_kubernetes_service_account_is_allowed_without_name_exemption(self):
        pod, objects = self.readonly_pod()
        cap, mounts = graph.pod_capabilities(pod, objects)
        self.assertTrue(cap["isolated"])
        self.assertFalse(cap["management"])
        self.assertFalse(cap["observer_safe"])
        self.assertFalse(cap["privileged"])
        self.assertFalse(any(write for _, write, _ in mounts))
        for rule in ({"resources": ["secrets"], "verbs": ["get"]}, {"resources": ["pods/exec"], "verbs": ["get"]}, {"resources": ["pods"], "verbs": ["create"]}, {"resources": ["*"], "verbs": ["get"]}, {"resources": ["serviceaccounts/token"], "verbs": ["create"]}):
            modified = deepcopy(objects)
            modified[1]["rules"] = [dict(apiGroups=[""], **rule)]
            actual, _ = graph.pod_capabilities(pod, modified)
            self.assertTrue(actual["management"])

    def test_readonly_host_observer_requires_nonroot_no_caps_no_token_and_all_mounts_ro(self):
        pod, objects = self.readonly_pod()
        pod["spec"]["automountServiceAccountToken"] = False
        pod["spec"]["volumes"] = [{"name": "host", "hostPath": {"path": "/proc"}}]
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "host", "mountPath": "/host/proc", "readOnly": True}]
        with patch.object(graph, "canonical_source", side_effect=lambda p: "file:" + p):
            cap, _ = graph.pod_capabilities(pod, objects)
            self.assertTrue(cap["observer_safe"])
            self.assertTrue(cap["isolated"])
            pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] = False
            cap, _ = graph.pod_capabilities(pod, objects)
            self.assertFalse(cap["observer_safe"])
            self.assertFalse(cap["isolated"])

    def test_runtime_executable_cannot_turn_kubectl_client_into_tcb_provider(self):
        self.assertTrue(graph.runtime_provider_argv("/usr/local/bin/k3s", b"/usr/local/bin/k3s\x00server\x00"))
        self.assertFalse(graph.runtime_provider_argv("/usr/local/bin/k3s", b"/usr/local/bin/k3s\\0server\\0"))
        self.assertTrue(graph.runtime_provider_argv("/usr/local/bin/k3s", b"k3s\0server\0"))
        self.assertFalse(graph.runtime_provider_argv("/usr/local/bin/k3s", b"k3s\0kubectl\0apply\0"))
        self.assertFalse(graph.runtime_provider_argv("/usr/local/bin/k3s", b"k3s\0server\0--config=/outside/config\0"))

    def test_real_hyphen_labels_include_managed_postgres_without_replacing_app_coverage(self):
        labels = {"personal-server.ha-cluster": "synthetic", "personal-server.ha-generation": "3", "personal-server.ha-service": "postgres-standby"}
        observed = graph.record("docker", "db", graph.service_of(labels), {}, {}, graph.capability(running=True, active=1), image="sha256:" + "e" * 64, labels=labels)
        self.assertEqual((observed["service"], observed["cluster"], observed["generation"]), ("postgres-standby", "synthetic", 3))
        self.snapshot["resources"].append(observed)
        self.request["spec"]["resources"].append({k: observed[k] for k in ("provider", "id", "service", "spec_sha256", "runtime_sha256", "image", "cluster", "generation")} | {"role": "managed_ha"})
        graph.validate_snapshot(self.request, self.snapshot)
        self.request["spec"]["resources"].pop(0)
        self.snapshot["resources"].pop(0)
        with self.assertRaisesRegex(graph.Blocked, "seven_service_inventory_required"):
            graph.validate_snapshot(self.request, self.snapshot)

    def test_host_namespace_exception_requires_derived_readonly_observer_profile(self):
        pod, objects = self.readonly_pod()
        pod["spec"].update(hostPID=True, hostNetwork=True, automountServiceAccountToken=False, volumes=[])
        pod["spec"]["containers"][0]["volumeMounts"] = []
        cap, _ = graph.pod_capabilities(pod, objects)
        self.assertTrue(cap["host_namespace"])
        self.assertTrue(cap["observer_safe"])
        self.snapshot["resources"][-1]["capabilities"].update(cap)
        with self.assertRaisesRegex(graph.Blocked, "protected_capability_unverified"):
            graph.validate_snapshot(self.request, self.snapshot)
        self.request["spec"]["resources"][-1]["profile"] = "host-observer"
        graph.validate_snapshot(self.request, self.snapshot)

    def test_safe_bind_capability_and_delegated_review_are_not_general_mutation(self):
        pod, objects = self.readonly_pod()
        pod["spec"]["containers"][0]["securityContext"]["capabilities"]["add"] = ["NET_BIND_SERVICE"]
        cap, _ = graph.pod_capabilities(pod, objects)
        self.assertFalse(cap["privileged"])
        objects[1]["rules"].append({"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]})
        objects[1]["rules"].append({"apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"], "verbs": ["create"]})
        cap, _ = graph.pod_capabilities(pod, objects)
        self.assertTrue(cap["delegated_review"])
        self.assertFalse(cap["management"])
        self.snapshot["resources"][-1]["capabilities"].update(cap)
        with self.assertRaisesRegex(graph.Blocked, "delegated_reader_profile_required"):
            graph.validate_snapshot(self.request, self.snapshot)
        self.request["spec"]["resources"][-1]["profile"] = "delegated-reader"
        graph.validate_snapshot(self.request, self.snapshot)
        objects[1]["rules"].append({"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"]})
        cap, _ = graph.pod_capabilities(pod, objects)
        self.assertTrue(cap["management"])

    @patch.object(graph, "verify_kubeconfig", new=lambda budget: None)
    def test_collector_consumes_real_kubernetes_shapes_with_metrics_backing_identity(self):
        pod, objects = self.readonly_pod()
        pod["status"] = {"phase": "Running", "podIP": "192.0.2.1", "containerStatuses": [{"name": "reader", "imageID": "sha256:" + "a" * 64, "state": {"running": {"startedAt": "fixture"}}}]}
        objects[1]["rules"].append({"apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"], "verbs": ["create"]})
        objects += [{"kind": "Namespace", "metadata": {"name": "kube-system", "uid": self.snapshot["kube_system_uid"]}}, {"kind": "Service", "metadata": {"name": "metrics", "namespace": "fixture", "uid": "service"}, "spec": {"selector": {}}}, {"kind": "Endpoints", "metadata": {"name": "metrics", "namespace": "fixture", "uid": "endpoints"}, "subsets": [{"addresses": [{"ip": "192.0.2.1", "targetRef": {"kind": "Pod", "uid": "pod"}}]}]}, {"kind": "APIService", "metadata": {"name": "v1beta1.metrics.k8s.io", "uid": "api"}, "spec": {"group": "metrics.k8s.io", "service": {"name": "metrics", "namespace": "fixture"}}}]
        class FakeBudget:
            def command(self, args):
                if "api-resources" in args:
                    return b"pods\nnamespaces\nservices\nendpoints\nserviceaccounts\nclusterroles.rbac.authorization.k8s.io\nclusterrolebindings.rbac.authorization.k8s.io\napiservices.apiregistration.k8s.io\npods.metrics.k8s.io\nnodes.metrics.k8s.io\n"
                return json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}).encode()
        uid, catalog, records, _ = graph.collect_kubernetes(FakeBudget())
        self.assertEqual(uid, self.snapshot["kube_system_uid"])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["capabilities"]["delegated_review"])
        self.assertFalse(records[0]["capabilities"]["management"])
        self.assertEqual(records[0]["image"], "sha256:" + "a" * 64)
        pod["spec"]["initContainers"] = [{"name": "init", "image": "fixture-init@sha256:" + "b" * 64}]
        pod["status"]["initContainerStatuses"] = [{"name": "init", "imageID": "sha256:" + "b" * 64}]
        _, _, unsupported, _ = graph.collect_kubernetes(FakeBudget())
        self.assertEqual(unsupported[0]["image"], "")
        pod["status"]["initContainerStatuses"][0]["imageID"] = "sha256:" + "c" * 64
        _, _, changed, _ = graph.collect_kubernetes(FakeBudget())
        self.assertEqual(changed[0]["image"], "")
        pod["spec"].pop("initContainers")
        pod["status"].pop("initContainerStatuses")
        objects[-2]["subsets"][0]["addresses"][0]["ip"] = "192.0.2.2"
        with self.assertRaisesRegex(graph.Blocked, "aggregator_endpoint_identity_mismatch"):
            graph.collect_kubernetes(FakeBudget())

    def test_transient_reader_excludes_only_real_root_init_parent_chain(self):
        class File:
            def __init__(self, name): self.name = name
            def __truediv__(self, value): return File(self.name + "/" + value)
            def __str__(self): return self.name
            def stat(self): return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        class FakeBudget:
            def file(self, path):
                if str(path) == "/proc/self/cmdline": return b"/usr/bin/python3\x00-I\x00-B\x00-S\x00-c\x00bootstrap\x00"
                if str(path).endswith("/status"): return b"Uid:\t0 0 0 0\nPPid:\t1\n"
                if str(path).endswith("/cmdline"): return b"/init\x00"
                return b"0::/init.scope\n"
            def hash_file(self, path): return "a" * 64
        def readlink(path): return "/init" if "/200/" in str(path) else "/usr/lib/systemd/systemd"
        with patch.object(graph, "Path", side_effect=lambda p: File(p)), patch.object(graph.os, "getppid", return_value=200), patch.object(graph.os, "readlink", side_effect=readlink), patch.object(graph, "__collector_sha256__", "c" * 64, create=True):
            excluded, receipt = graph.reader_session_ancestors(FakeBudget())
        self.assertEqual(excluded, {200})
        self.assertNotIn(201, excluded)
        self.assertEqual(receipt["collector_sha256"], "c" * 64)
        self.assertEqual(receipt["interpreter"]["exe_sha256"], "a" * 64)
        self.assertEqual(receipt["interpreter"]["cmdline_sha256"], hashlib.sha256(b"/usr/bin/python3\0-I\0-B\0-S\0-c\0bootstrap\0").hexdigest())
        self.assertNotIn("pid", str(receipt))

    def test_packaged_os_activation_uses_real_unit_and_manifest_bytes_not_name(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            unit, script = root / "apt-daily.service", root / "apt.systemd.daily"
            unit.write_bytes(b"[Service]\nType=oneshot\n")
            script.write_bytes(b"#!/bin/sh\nexit 0\n")
            raw = b"".join(hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest().encode() + b"  " + str(p).lstrip("/").encode() + b"\n" for p in (unit, script))
            class FakeBudget:
                def left(self): return 1
                def file(self, path): return raw
                def command(self, argv): return b""
            properties = dict(Id="apt-daily.service", FragmentPath=str(unit), DropInPaths="", EnvironmentFiles="", ExecStart="{ path=" + str(script) + " ; argv[]=" + str(script) + " update ; }", ActiveState="inactive", MainPID="0", UnitFileState="static")
            original_stat, original_file = Path.stat, Path.is_file
            def observed_stat(path, **kwargs):
                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755) if path in (unit, script) else original_stat(path, **kwargs)
            def observed_file(path): return True if str(path).startswith("/var/lib/dpkg/info/") else original_file(path)
            with patch.object(Path, "stat", observed_stat), patch.object(Path, "is_file", observed_file):
                approved, proof = graph.os_activation_units(FakeBudget(), {"apt-daily.service": properties}, {})
                self.assertEqual(approved, {"apt-daily.service"})
                self.assertEqual(proof["apt-daily.service"]["fragment"]["package"], "apt")
                unit.write_bytes(b"[Service]\nExecStart=/some/app-writer\n")
                approved, _ = graph.os_activation_units(FakeBudget(), {"apt-daily.service": properties}, {})
                self.assertEqual(approved, set())

    def test_package_unit_custom_wants_requires_and_upholds_links_cannot_start_disabled_app(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            os_unit, app_unit = root / "apt-daily.service", root / "legacy-app.service"
            os_unit.write_text("[Service]\nType=oneshot\n")
            app_unit.write_text("[Service]\nExecStart=/unverified/app-writer\n")
            original = os_unit.read_bytes()
            for suffix, property_name in (("wants", "Wants"), ("requires", "Requires"), ("upholds", "Upholds")):
                directory = root / (os_unit.name + "." + suffix)
                directory.mkdir()
                (directory / app_unit.name).symlink_to(app_unit)
                # These are the actual loaded dependency names systemctl show
                # returns for link-based dependencies. Fragment bytes stay the
                # original package's bytes; disabled does not prevent starts.
                observed_dependencies = " ".join(path.name for path in directory.iterdir())
                registry = {os_unit.name: {"Id": os_unit.name, property_name: observed_dependencies}, app_unit.name: {"Id": app_unit.name, "ActiveState": "inactive", "UnitFileState": "disabled"}}
                self.assertEqual(os_unit.read_bytes(), original)
                with self.subTest(edge=property_name), self.assertRaisesRegex(graph.Blocked, "unsupported_os_activation_dependency"):
                    graph.validate_activation_closure(registry, {os_unit.name})
            self.assertIn("Upholds", graph.UNIT_PROPERTIES)

    def test_activation_target_transitive_unknown_and_ordering_edges_are_distinct(self):
        registry = {"os.service": {"Wants": "os.target", "Before": "legacy.service", "After": "legacy.service"}, "os.target": {"Requires": "legacy.service"}, "legacy.service": {"ActiveState": "inactive", "UnitFileState": "disabled"}}
        with self.assertRaisesRegex(graph.Blocked, "unsupported_os_activation_dependency"):
            graph.validate_activation_closure(registry, {"os.service", "os.target"})
        registry["os.target"] = {"Requires": "unknown.target"}
        with self.assertRaisesRegex(graph.Blocked, "unsupported_os_activation_dependency"):
            graph.validate_activation_closure(registry, {"os.service", "os.target"})
        registry["os.target"] = {"Requires": "os.service"}
        graph.validate_activation_closure(registry, {"os.service", "os.target"})
        # The same rule applies to the complete registered user-manager graph.
        user = {"user:1000:os.service": {"_manager_uid": 1000, "Upholds": "app.service"}, "user:1000:app.service": {"_manager_uid": 1000, "UnitFileState": "disabled"}}
        with self.assertRaisesRegex(graph.Blocked, "unsupported_os_activation_dependency"):
            graph.validate_activation_closure(user, {"user:1000:os.service"})

    def test_self_expiry_kills_and_reaps_query_group_and_restores_signal_handlers(self):
        import signal
        import subprocess
        from unittest.mock import MagicMock
        processes = []
        original_popen = subprocess.Popen
        executable = MagicMock()
        executable.resolve.return_value = executable
        executable.parents = ()
        executable.stat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        def query(argv, **options):
            child = original_popen([sys.executable, "-I", "-B", "-S", "-c", "import time; time.sleep(30)"], **options)
            processes.append(child)
            return child
        def collect(budget):
            budget.command(["/usr/bin/systemctl", "--no-pager", "list-units"])
        before = signal.getsignal(signal.SIGTERM)
        with patch.object(graph, "SECONDS", 0.1), patch.object(graph, "Path", return_value=executable), patch.object(graph.subprocess, "Popen", side_effect=query):
            with self.assertRaisesRegex(graph.Blocked, "live_graph_deadline_exceeded"):
                self.evaluate(collect)
        self.assertTrue(processes)
        self.assertTrue(all(child.poll() is not None for child in processes))
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_external_termination_cleans_query_children_before_returning_blocked(self):
        import os
        import signal
        import subprocess
        import threading
        from unittest.mock import MagicMock
        original_popen, processes = subprocess.Popen, []
        executable = MagicMock()
        executable.resolve.return_value = executable
        executable.parents = ()
        executable.stat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        def query(argv, **options):
            process = original_popen([sys.executable, "-I", "-B", "-S", "-c", "import time; time.sleep(30)"], **options)
            processes.append(process)
            return process
        def collect(budget):
            timer = threading.Timer(0.03, lambda: os.kill(os.getpid(), signal.SIGTERM))
            timer.start()
            try:
                budget.command(["/usr/bin/systemctl", "--no-pager", "list-units"])
            finally:
                timer.cancel()
                timer.join()
        with patch.object(graph, "Path", return_value=executable), patch.object(graph.subprocess, "Popen", side_effect=query):
            with self.assertRaisesRegex(graph.Blocked, "collector_cancelled"):
                self.evaluate(collect)
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_linux_fixed_query_installs_parent_death_kill_before_exec(self):
        from unittest.mock import MagicMock
        import signal
        executable = MagicMock()
        executable.resolve.return_value = executable
        executable.parents = ()
        executable.stat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        process = MagicMock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(SimpleNamespace(fileobj=process.stdout), None)]
        with patch.object(graph.sys, "platform", "linux"), patch.object(graph, "Path", return_value=executable), patch.object(graph.subprocess, "Popen", return_value=process) as launch, patch.object(graph.selectors, "DefaultSelector", return_value=selector), patch.object(graph.os, "set_blocking"), patch.object(graph.os, "killpg"), patch.object(graph.os, "read", return_value=b""):
            graph.Budget().command(["/usr/bin/systemctl", "list-units"])
        callback = launch.call_args.kwargs["preexec_fn"]
        self.assertIsNotNone(callback)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        with patch.object(graph.ctypes, "CDLL") as library, patch.object(graph.os, "getppid", return_value=graph.os.getpid()), patch.object(graph.os, "_exit") as exit_process:
            library.return_value.prctl.return_value = 0
            callback()
            library.return_value.prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)
            exit_process.assert_not_called()

    def test_canonical_storage_aliases_and_nameless_readonly_control_socket_are_detected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = root / "data"
            data.mkdir()
            (data / "nested").mkdir()
            alias = root / "alias"
            alias.symlink_to(data, target_is_directory=True)
            self.assertTrue(graph.overlaps(graph.canonical_source(str(alias / "nested")), graph.canonical_source(str(data))))
            control = data / "opaque"
            control.write_bytes(b"")
            original_lstat = Path.lstat
            def observed_lstat(path):
                # A socket inode is modeled; this unit fixture opens no socket
                # or listener, and leaves the real directory walk intact.
                return SimpleNamespace(st_mode=stat.S_IFSOCK) if path == control else original_lstat(path)
            with patch.object(Path, "lstat", observed_lstat):
                self.assertTrue(graph.mounted_control_socket(graph.canonical_source(str(data)), graph.Budget()))

    def test_cross_namespace_service_account_rolebinding_is_not_omitted(self):
        pod, objects = self.readonly_pod()
        objects += [{"kind": "Role", "metadata": {"name": "writer", "namespace": "other", "uid": "other-role"}, "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["create"]}]}, {"kind": "RoleBinding", "metadata": {"name": "writer", "namespace": "other", "uid": "other-binding"}, "roleRef": {"kind": "Role", "name": "writer"}, "subjects": [{"kind": "ServiceAccount", "name": "reader", "namespace": "fixture"}]}]
        self.assertFalse(graph.readonly_service_account(pod["spec"], "fixture", objects))
        cap, _ = graph.pod_capabilities(pod, objects)
        self.assertTrue(cap["management"])

    def test_kernel_mount_roots_detect_bind_alias_without_visible_path_ancestry(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app, alias = root / "app", root / "alias"
            app.mkdir()
            alias.mkdir()
            (alias / "child").mkdir()
            device = str(os.major(root.stat().st_dev)) + ":" + str(os.minor(root.stat().st_dev))
            mountinfo = "1 0 " + device + " / / rw - ext4 fixture rw\n2 1 " + device + " " + str(app) + " " + str(alias) + " rw - ext4 fixture rw\n"
            with patch.object(graph, "_MOUNT_ENTRIES", graph.parse_mountinfo(mountinfo)), patch.object(graph, "_SOURCE_PATHS", {}):
                self.assertFalse(str(alias / "child").startswith(str(app)))
                self.assertTrue(graph.overlaps(graph.canonical_source(str(app)), graph.canonical_source(str(alias / "child"))))
            with patch.object(graph, "_MOUNT_ENTRIES", graph.parse_mountinfo(mountinfo.replace("ext4", "9p"))), patch.object(graph, "_SOURCE_PATHS", {}):
                with self.assertRaisesRegex(graph.Blocked, "unsupported_filesystem_alias_provider"):
                    graph.canonical_source(str(app))

    def test_hardlink_data_alias_is_rejected_by_real_inode_metadata(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data, alias = root / "app.sqlite", root / "protected-copy.sqlite"
            data.write_bytes(b"synthetic database")
            os.link(data, alias)
            self.assertEqual(data.stat().st_ino, alias.stat().st_ino)
            with self.assertRaisesRegex(graph.Blocked, "unsupported_hardlinked_mount_source"):
                graph.mounted_control_socket(graph.canonical_source(str(alias)), graph.Budget())

    def test_k3s_generated_inline_tls_subset_rejects_plugins_and_unsafe_yaml_before_query(self):
        import base64
        ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n").decode()
        key = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----\n").decode()
        text = "apiVersion: v1\nclusters:\n- cluster:\n    certificate-authority-data: " + ca + "\n    server: https://127.0.0.1:6443\n  name: default\ncontexts:\n- context:\n    cluster: default\n    user: default\n  name: default\ncurrent-context: default\nkind: Config\npreferences: {}\nusers:\n- name: default\n  user:\n    client-certificate-data: " + ca + "\n    client-key-data: " + key + "\n"
        self.assertEqual(len(graph.parse_kubeconfig(text.encode())), 3)
        variants = [text + "    exec: {command: untrusted}\n", text.replace("  user:\n", "  user:\n    auth-provider: {name: untrusted}\n"), text.replace("server: https://127.0.0.1:6443", "server: https://outside.invalid:6443"), text.replace("preferences: {}", "preferences: &alias {}"), text.replace("kind: Config", "kind: !!python/object Config"), text.replace("    server:", "    insecure-skip-tls-verify: true\n    server:"), text.replace("    server:", "    proxy-url: https://outside.invalid\n    server:")]
        for index, value in enumerate(variants):
            with self.subTest(index=index), self.assertRaisesRegex(graph.Blocked, "unsupported_kubeconfig_profile"):
                graph.parse_kubeconfig(value.encode())
        budget = SimpleNamespace(command=lambda args: self.fail("query before kubeconfig verification"))
        with patch.object(graph, "verify_kubeconfig", side_effect=graph.Blocked("unsupported_kubeconfig_profile")):
            with self.assertRaisesRegex(graph.Blocked, "unsupported_kubeconfig_profile"):
                graph.collect_kubernetes(budget)

    def test_unknown_credential_environment_is_not_assumed_to_be_app_scoped(self):
        self.assertTrue(graph.credential_environment(["NONSTANDARD_CONFIG=opaque"] ))
        self.assertTrue(graph.credential_environment(["PGPASSWORD=synthetic"]))
        self.assertFalse(graph.credential_environment(["PATH=/usr/bin:/bin", "APP_ENV=production"]))

    def test_enrolled_managed_lifecycle_normalizes_only_runtime_and_rejects_control_power(self):
        identifier = self.snapshot["resources"][0]["id"]
        service = self.snapshot["resources"][0]["service"]
        labels = {"personal-server.ha-cluster": "synthetic", "personal-server.ha-generation": "3", "personal-server.ha-service": service}
        container = dict(Id=identifier, Image="sha256:" + "b" * 64, Config=dict(Labels=labels, Env=[]),
                         State=dict(Running=True, Restarting=False, Pid=101, StartedAt="old", Health={"Status": "healthy", "Log": [{"End": "old"}]}),
                         RestartCount=0, NetworkSettings={"Networks": {"fixture": {"NetworkID": "network", "EndpointID": "old", "IPAddress": "192.0.2.1"}}},
                         HostConfig=dict(NetworkMode="fixture", Privileged=False, RestartPolicy={"Name": "no"}), Mounts=[])
        class FakeBudget:
            def docker(self, endpoint):
                if endpoint == "info": return {"ID": "engine", "Swarm": {"LocalNodeState": "inactive"}, "ContainersRunning": int(container["State"]["Running"]), "ContainersStopped": int(not container["State"]["Running"])}
                if endpoint == "plugins": return []
                if endpoint == "containers/json?all=1": return [{"Id": identifier}]
                if endpoint.startswith("containers/"): return deepcopy(container)
                if endpoint == "networks": return [{"Id": "network", "Containers": {identifier: {"IPv4Address": "192.0.2.1"}} if container["State"]["Running"] else {}}]
                return []
        _, initial, records, _ = graph.collect_docker(FakeBudget())
        self.snapshot["resources"][0] = records[0]
        self.request["spec"]["resources"][0] = {k: records[0][k] for k in ("provider", "id", "service", "spec_sha256", "runtime_sha256", "image", "cluster", "generation")} | {"role": "managed_ha"}
        with patch.object(graph, "_LIVE_SPEC", self.request["spec"]):
            baseline = graph.digest(graph.docker_identity_catalog(initial))
            self.snapshot["providers"]["docker"] = self.request["spec"]["providers"]["docker"] = baseline
            graph.validate_snapshot(self.request, self.snapshot)
            self.assertFalse(records[0]["capabilities"]["app_credentials"])
            self.assertFalse(records[0]["capabilities"]["management"])
            container["State"].update(Running=False, Pid=0, StartedAt="later", Health={"Status": "healthy", "Log": [{"End": "later"}]})
            container["RestartCount"] = 1
            container["NetworkSettings"]["Networks"]["fixture"].update(EndpointID="later", IPAddress="192.0.2.2")
            _, stopped, records, _ = graph.collect_docker(FakeBudget())
            self.assertEqual(baseline, graph.digest(graph.docker_identity_catalog(stopped)))
            self.snapshot["resources"][0] = records[0]
            graph.validate_snapshot(self.request, self.snapshot)
            unknown = deepcopy(stopped)
            unknown["containers"].append(dict(unknown["containers"][0], Id="f" * 64))
            self.assertNotEqual(baseline, graph.digest(graph.docker_identity_catalog(unknown)))
        for field in ("management", "privileged", "host_namespace"):
            snapshot = deepcopy(self.snapshot)
            snapshot["resources"][0]["capabilities"][field] = True
            with self.assertRaisesRegex(graph.Blocked, "managed_control_capability_unverified"):
                graph.validate_snapshot(self.request, snapshot)
        snapshot = deepcopy(self.snapshot)
        snapshot["resources"][0]["capabilities"]["app_credentials"] = True
        with self.assertRaisesRegex(graph.Blocked, "managed_credential_profile_unverified"):
            graph.validate_snapshot(self.request, snapshot)

    def test_runtime_tcb_has_exact_executable_uid_start_unit_binding_and_python_is_client(self):
        class File:
            def __init__(self, name): self.name = name
            def __truediv__(self, child): return File(self.name + "/" + child)
            def __str__(self): return self.name
            def stat(self): return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755)
        class Proc:
            def iterdir(self): return [File("100")]
        class FakeBudget:
            def file(self, path):
                if str(path).endswith("/status"): return b"Uid:\t0 0 0 0\nCapEff:\t0\n"
                if str(path).endswith("/stat"): return ("100 (runtime) S " + "0 " * 18 + "44 0").encode()
                if str(path).endswith("/cmdline"): return b"runtime\x00"
                return b"0::/system.slice/docker.service\n"
            def hash_file(self, path): return "a" * 64
        registry = {"docker.service": dict(Id="docker.service", MainPID="100", ControlGroup="/system.slice/docker.service")}
        with patch.object(graph, "scoped_runtime_helpers", return_value={}), patch.object(graph, "reader_session_ancestors", return_value=(set(), {})), patch.object(graph, "Path", return_value=Proc()), patch.object(graph.os, "getpid", return_value=999), patch.object(graph.os, "readlink", return_value="/usr/bin/dockerd"):
            _, tcb, resources, units, _ = graph.collect_processes(FakeBudget(), registry, {})
        self.assertEqual(tcb[0]["pid"], 100)
        self.assertEqual(tcb[0]["uid"], 0)
        self.assertEqual(tcb[0]["start_ticks"], 44)
        self.assertEqual(tcb[0]["exe_sha256"], "a" * 64)
        self.assertEqual(tcb[0]["unit_spec_sha256"], graph.digest(registry["docker.service"]))
        self.assertEqual(resources, [])
        self.assertEqual(units, {"docker.service"})
        with patch.object(graph, "scoped_runtime_helpers", return_value={}), patch.object(graph, "reader_session_ancestors", return_value=(set(), {})), patch.object(graph, "Path", return_value=Proc()), patch.object(graph.os, "getpid", return_value=999), patch.object(graph.os, "readlink", return_value="/usr/bin/python3"):
            _, tcb, resources, _, _ = graph.collect_processes(FakeBudget(), registry, {})
        self.assertEqual(tcb, [])
        self.assertEqual(len(resources), 1)
        self.assertTrue(resources[0]["capabilities"]["running"])
        self.assertTrue(resources[0]["capabilities"]["host_namespace"])


if __name__ == "__main__":
    unittest.main()
