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

    def test_missing_native_ext4_path_keeps_static_local_pv_gate_blocked(self):
        document = (ROOT / "docs/k3s-flux-transition-draft.md").read_text()

        self.assertIn("미통과(경로 미준비)", document)
        self.assertIn("디렉터리 생성·소유권/권한 설정 전까지 미통과", document)
        self.assertIn("Static Local PV 게이트는 계속 차단 상태", document)


if __name__ == "__main__":
    unittest.main()
