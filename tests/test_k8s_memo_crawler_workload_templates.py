import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "crawler-worker": (
        8001,
        "/data/crawler-worker",
        "NEWS_DB_PATH",
        "/data/crawler-worker/news_summaries.sqlite3",
    ),
    "youtube-memo": (
        8002,
        "/data/youtube-memo",
        "YOUTUBE_MEMO_DB_PATH",
        "/data/youtube-memo/youtube_memo.sqlite3",
    ),
    "book-memo": (
        8003,
        "/data/book-memo",
        "BOOK_MEMO_DB_PATH",
        "/data/book-memo/book_memo.sqlite3",
    ),
}


class WorkloadTemplateTests(unittest.TestCase):
    def test_templates_are_inactive_and_preserve_data_contracts(self):
        for name, (port, mount, env_name, db_path) in SERVICES.items():
            deployment = (
                ROOT / "infra/k8s/clusters/n100/apps" / name / "deployment.yaml.tmpl"
            ).read_text()
            service = (
                ROOT / "infra/k8s/clusters/n100/apps" / name / "service.yaml.tmpl"
            ).read_text()
            self.assertIn("DRAFT ONLY — NOT FOR APPLY", deployment)
            self.assertIn("replicas: 0", deployment)
            self.assertIn(f"containerPort: {port}", deployment)
            self.assertIn("path: /health", deployment)
            self.assertIn(f"mountPath: {mount}", deployment)
            self.assertIn(env_name, deployment)
            self.assertIn(f"value: {db_path}", deployment)
            self.assertIn("__CONFIRM_", deployment)
            self.assertIn("type: ClusterIP", service)
            self.assertIn(f"port: {port}", service)
            self.assertNotIn("NodePort", service)
            self.assertNotIn("Ingress", service)
            self.assertNotIn("hostPath", deployment)
            self.assertNotIn("nodeAffinity", deployment)

    def test_crawler_contract_has_archive_and_matching_runtime_resources(self):
        deployment = (
            ROOT
            / "infra/k8s/clusters/n100/apps/crawler-worker/deployment.yaml.tmpl"
        ).read_text()
        storage = (
            ROOT
            / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl"
        ).read_text()
        self.assertIn("NEWS_ARCHIVE_PATH", deployment)
        self.assertIn("value: /data/crawler-worker/news_archive.json", deployment)
        self.assertIn("name: crawler-worker-runtime", deployment)
        self.assertIn("claimName: crawler-worker-data-dynamic-draft", deployment)
        self.assertIn("name: crawler-worker-data-dynamic-draft", storage)
        self.assertIn("storage: __CONFIRM_CRAWLER_DATA_CAPACITY__", storage)

    def test_memo_contracts_have_independent_secrets_and_pvcs(self):
        storage = (
            ROOT
            / "infra/k8s/clusters/n100/infra/storage/storage-contract.yaml.tmpl"
        ).read_text()
        for name, secret, pvc, capacity in (
            (
                "youtube-memo",
                "youtube-memo-runtime",
                "youtube-memo-data-dynamic-draft",
                "__CONFIRM_YOUTUBE_DATA_CAPACITY__",
            ),
            (
                "book-memo",
                "book-memo-runtime",
                "book-memo-data-dynamic-draft",
                "__CONFIRM_BOOK_DATA_CAPACITY__",
            ),
        ):
            deployment = (
                ROOT / "infra/k8s/clusters/n100/apps" / name / "deployment.yaml.tmpl"
            ).read_text()
            self.assertIn(f"name: {secret}", deployment)
            self.assertIn(f"claimName: {pvc}", deployment)
            self.assertIn(f"name: {pvc}", storage)
            self.assertIn(f"storage: {capacity}", storage)

    def test_root_kustomization_stays_empty(self):
        root = (ROOT / "infra/k8s/kustomization.yaml").read_text()
        self.assertIn("resources: []", root)
        for service in SERVICES:
            self.assertNotIn(service, root)


if __name__ == "__main__":
    unittest.main()
