import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class K3sStorageDraftTests(unittest.TestCase):
    def test_storage_contract_uses_dynamic_local_path_without_static_pv_constructs(self):
        contract = (ROOT / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl").read_text()
        self.assertIn("DRAFT ONLY", contract)
        self.assertIn("storageClassName: local-path", contract)
        self.assertIn("ReadWriteOnce", contract)
        self.assertNotIn("kind: StorageClass", contract)
        self.assertNotIn("kind: PersistentVolume\n", contract)
        self.assertNotIn("kubernetes.io/no-provisioner", contract)
        self.assertNotIn("nodeAffinity", contract)
        self.assertNotIn("__CONFIRM_N100_HOST_PATH__", contract)
        self.assertNotIn("/var/lib/rancher/k3s/storage", contract)
        self.assertNotIn("volumeName:", contract)

    def test_native_ext4_and_smoke_evidence_boundaries_are_documented(self):
        document = (ROOT / "docs/k3s-flux-transition-draft.md").read_text()

        self.assertIn("통과(경로 준비 완료)", document)
        self.assertIn("`/dev/sdd` ext4의 `rw`", document)
        self.assertIn("`desktop-utu2qat` Ready", document)
        self.assertIn("`root:root`·`0750`", document)
        self.assertIn("scratch PVC/Pod", document)
        self.assertIn("파일 I/O와 SQLite `BEGIN IMMEDIATE` 잠금", document)
        self.assertIn("임시 namespace와 PV 삭제 완료", document)
        self.assertIn("앱별 UID/GID", document)
        self.assertIn("`local-path` 동적", document)
        self.assertIn("앱 데이터 복사·복원", document)
        self.assertIn("단일 writer cutover", document)
        self.assertIn("EXIT trap", document)

    def test_portal_draft_matches_container_contract(self):
        deployment = (ROOT / "infra/k8s/clusters/n100/apps/portal-web/deployment.yaml.tmpl").read_text()
        service = (ROOT / "infra/k8s/clusters/n100/apps/portal-web/service.yaml.tmpl").read_text()
        self.assertIn("replicas: 0", deployment)
        self.assertIn("__CONFIRM_PORTAL_IMAGE_REF__", deployment)
        self.assertIn("containerPort: 8000", deployment)
        self.assertIn("path: /health", deployment)
        self.assertIn("FILE_STORAGE_PATH", deployment)
        self.assertIn("value: /data/files", deployment)
        self.assertIn("claimName: portal-web-files-dynamic-draft", deployment)
        self.assertIn("port: 8000", service)

    def test_scope_keeps_first_wave_and_dynamic_storage_gate(self):
        scope = (ROOT / "infra/k8s/clusters/n100/apps/transition-scope.yaml.tmpl").read_text()
        self.assertIn("portal-web,crawler-worker,youtube-memo,book-memo", scope)
        self.assertIn("system-agent,homeops-executor,car-care-worker,caddy", scope)
        self.assertIn("Dynamic local-path", scope)
        self.assertNotIn("Static Local PV blocked", scope)

    def test_root_kustomization_keeps_drafts_inactive(self):
        kustomization = (ROOT / "infra/k8s/kustomization.yaml").read_text()
        self.assertIn("resources: []", kustomization)
        self.assertNotIn(".tmpl", kustomization)


if __name__ == "__main__":
    unittest.main()
