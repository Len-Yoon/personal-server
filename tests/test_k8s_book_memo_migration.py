import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "k8s" / "apps" / "book-memo.yaml"


class BookMemoManifestTests(unittest.TestCase):
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
        pvc = next(item for item in documents if item["kind"] == "PersistentVolumeClaim")
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        service = next(item for item in documents if item["kind"] == "Service")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]

        self.assertEqual(pvc["metadata"], {"name": "book-memo-data", "namespace": "personal-server"})
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "1Gi")
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
        self.assertEqual(
            {volume["name"]: volume for volume in pod["volumes"]}["book-memo-data"],
            {"name": "book-memo-data", "persistentVolumeClaim": {"claimName": "book-memo-data"}},
        )
        self.assertIn("medium", {volume["name"]: volume for volume in pod["volumes"]}["tmp"]["emptyDir"])
        self.assertEqual(container["readinessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertEqual(container["livenessProbe"]["httpGet"], {"path": "/health", "port": "http"})
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "book-memo-runtime"}}])
        self.assertEqual(service["spec"]["ports"], [{"name": "http", "port": 8003, "targetPort": "http"}])


if __name__ == "__main__":
    unittest.main()
