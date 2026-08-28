import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class K3sStorageDraftTests(unittest.TestCase):
    def test_native_ext4_storage_path_is_a_blocked_contract_not_an_apply_target(self):
        contract = (ROOT / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl").read_text()
        document = (ROOT / "docs/k3s-flux-transition-draft.md").read_text()
        self.assertIn("/var/lib/rancher/k3s/storage", contract)
        self.assertIn("n100-native-storage-validation", contract)
        self.assertIn("/var/lib/rancher/k3s/storage", document)
        self.assertIn("적용 전 검증", document)

    def test_native_ext4_path_is_prepared_but_app_storage_gate_remains_blocked(self):
        document = (ROOT / "docs/k3s-flux-transition-draft.md").read_text()

        self.assertIn("통과(경로 준비 완료)", document)
        self.assertIn("`/dev/sdd` ext4의 `rw`", document)
        self.assertIn("`desktop-utu2qat` Ready", document)
        self.assertIn("`root:root`·`0750`", document)
        self.assertIn("scratch PVC/Pod", document)
        self.assertIn("파일 I/O와 SQLite `BEGIN IMMEDIATE` 잠금", document)
        self.assertIn("임시 namespace와 PV 삭제 완료", document)
        self.assertIn("앱별 UID/GID", document)
        self.assertIn("Static Local PV 게이트는 계속 차단 상태", document)


if __name__ == "__main__":
    unittest.main()
