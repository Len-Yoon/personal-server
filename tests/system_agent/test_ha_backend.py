import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from tests._test_support import prepare_service_import


class SystemAgentHATests(unittest.TestCase):
    def test_identity_and_values_are_checked_from_one_snapshot(self):
        prepare_service_import("system-agent")
        from app.services import metrics
        backend = Mock()
        backend.node_id.return_value = "A"
        source = Mock()
        source.exists.return_value = True
        source.read_text.side_effect = [json.dumps({"node_id": "B", "cpu_percent": 99}),
                                        json.dumps({"node_id": "A", "cpu_percent": 1})]
        with patch.object(metrics, "runtime", return_value=backend):
            result = metrics._read_host_metrics(source, 60, [])
        source.read_text.assert_called_once()
        self.assertEqual(result["status_reason"], "host_node_unverified")
        self.assertIsNone(result["cpu_percent"])

    def test_copied_host_snapshot_does_not_impersonate_current_node(self):
        prepare_service_import("system-agent")
        from app.services import metrics, host_disk_metrics
        backend = Mock()
        backend.node_id.return_value = "B"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "host.json"
            source.write_text(json.dumps({"node_id": "A", "captured_at": datetime.now(timezone.utc).isoformat(),
                                          "cpu_percent": 90, "memory_percent": 90,
                                          "host_disk": {"source": "windows-cim", "volume": "C:", "total_bytes": 1000, "available_bytes": 100}}))
            with patch.object(metrics, "runtime", return_value=backend), patch.object(host_disk_metrics, "runtime", return_value=backend):
                result = metrics.collect_metrics(data_root=root, host_metrics_path=source)
                output = host_disk_metrics.render_host_disk_metrics(source)
            self.assertEqual(result["node_id"], "B")
            self.assertEqual(result["host"]["status_reason"], "host_node_unverified")
            self.assertIsNone(result["host"]["cpu_percent"])
            self.assertIn('node_id="B"', output)
            self.assertNotIn("capacity_bytes", output)
            self.assertIn('collection_ok{node_id="B",host="windows",volume="C:"} 0', output)

    def test_host_observations_require_target_and_disallow_global_cache(self):
        prepare_service_import("system-agent")
        from app import main
        from fastapi.testclient import TestClient
        backend = Mock()
        backend.node_id.return_value = "fixture-node"
        with patch.object(main, "runtime", return_value=backend), patch.object(main, "collect_metrics", return_value={"status": "ok"}) as collect, TestClient(main.app) as client:
            self.assertEqual(client.get("/health").json()["node_id"], "fixture-node")
            self.assertEqual(client.get("/metrics").status_code, 403)
            self.assertEqual(client.get("/metrics", headers={"X-HA-Target-Node": "other"}).status_code, 403)
            collect.assert_not_called()
            response = client.get("/metrics", headers={"X-HA-Target-Node": "fixture-node"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-HA-Node-ID"], "fixture-node")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
