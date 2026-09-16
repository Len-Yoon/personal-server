import unittest
from copy import deepcopy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "k8s" / "apps" / "book-memo.yaml"


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
