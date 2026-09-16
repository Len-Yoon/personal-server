import importlib.util
import json
import math
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "infra/k8s/tools/slo-daily-evidence.py"
SPEC = importlib.util.spec_from_file_location("slo_daily_evidence", MODULE_PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

MANIFEST = ROOT / "infra/k8s/slo-evidence/slo-daily-evidence-cronjob.yaml"
DOCKERFILE = ROOT / "infra/k8s/slo-evidence/Dockerfile"


class SloDailyEvidenceManifestTests(unittest.TestCase):
    def test_daily_evidence_tests_are_executed_by_k8s_ci(self):
        matrix = json.loads((ROOT / "tests/ci_test_matrix.json").read_text())
        command = next(job["test_command"] for job in matrix if job["name"] == "k8s-contracts")
        self.assertIn("tests.test_k8s_slo_daily_evidence", command.split())

    def documents(self):
        self.assertTrue(MANIFEST.is_file(), "daily evidence manifest is missing")
        return [item for item in yaml.safe_load_all(MANIFEST.read_text()) if item]

    def find(self, kind):
        matches = [item for item in self.documents() if item["kind"] == kind]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["metadata"]["name"], "slo-daily-evidence")
        self.assertEqual(matches[0]["metadata"]["namespace"], "monitoring")
        return matches[0]

    def test_daily_cronjob_is_suspended_bounded_and_has_no_sensitive_storage(self):
        spec = self.find("CronJob")["spec"]
        self.assertEqual(spec["schedule"], "15 2 * * *")
        self.assertEqual(spec["timeZone"], "Asia/Seoul")
        self.assertTrue(spec["suspend"])
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        job = spec["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        self.assertGreater(job["activeDeadlineSeconds"], 0)
        self.assertLessEqual(job["activeDeadlineSeconds"], 300)
        self.assertGreater(job["ttlSecondsAfterFinished"], 0)
        pod = job["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "slo-daily-evidence")
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertNotIn("secret", str(pod).lower())
        self.assertNotIn("persistentVolumeClaim", str(pod))
        self.assertNotIn("hostPath", str(pod))
        self.assertFalse(pod.get("hostNetwork", False))
        self.assertFalse(pod.get("hostPID", False))
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertGreater(pod["securityContext"]["runAsUser"], 0)
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertEqual(len(pod["containers"]), 1)
        container = pod["containers"][0]
        self.assertEqual(container["imagePullPolicy"], "Never")
        security = container["securityContext"]
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["volumeMounts"], [{"name": "tmp", "mountPath": "/tmp"}])
        self.assertEqual(len(pod["volumes"]), 1)
        self.assertIn("sizeLimit", pod["volumes"][0]["emptyDir"])
        self.assertIn("limits", container["resources"])
        environment = {item["name"]: item["value"] for item in container["env"]}
        self.assertEqual(environment["PROMETHEUS_URL"], module.PROMETHEUS_URL)
        self.assertEqual(environment["EVIDENCE_CONFIGMAP"], "slo-daily-evidence")

    def test_daily_rbac_can_only_get_and_patch_fixed_evidence_configmap(self):
        self.assertEqual({item["kind"] for item in self.documents()}, {
            "ServiceAccount", "ConfigMap", "Role", "RoleBinding", "CronJob",
        })
        self.find("ServiceAccount")
        self.assertEqual(self.find("ConfigMap")["data"], {"records.json": "[]"})
        self.assertEqual(self.find("Role")["rules"], [{
            "apiGroups": [""], "resources": ["configmaps"],
            "resourceNames": ["slo-daily-evidence"], "verbs": ["get", "patch"],
        }])
        binding = self.find("RoleBinding")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "slo-daily-evidence",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": "slo-daily-evidence", "namespace": "monitoring",
        }])

    def test_image_has_collector_runtime_dependencies_and_nonroot_entrypoint(self):
        self.assertTrue(DOCKERFILE.is_file(), "daily evidence Dockerfile is missing")
        source = DOCKERFILE.read_text()
        self.assertIn("FROM python:3.11-slim@sha256:", source)
        self.assertIn("ca-certificates", source)
        self.assertIn("kubernetes-client", source)
        self.assertIn("USER 10001:10001", source)
        copies = [line.split() for line in source.splitlines() if line.startswith("COPY ")]
        self.assertEqual(copies, [[
            "COPY", "infra/k8s/tools/slo-daily-evidence.py", "/opt/personal-server/slo-daily-evidence.py",
        ]])
        self.assertTrue((ROOT / copies[0][1]).is_file())
        entrypoint = next(line.removeprefix("ENTRYPOINT ") for line in source.splitlines() if line.startswith("ENTRYPOINT "))
        self.assertEqual(json.loads(entrypoint), ["python3", "-B", copies[0][2]])


def valid_fields(**overrides):
    fields = {
        "collected_at": "2026-08-30T18:00:00Z",
        "overall": "ok",
        "public_health": "ok",
        "portal_ready": "ok",
        "portal_http": {
            "requests": 100,
            "server_errors": 2,
            "server_error_ratio": 0.02,
            "p95_seconds": 0.25,
        },
        "crawler_freshness": "ok",
        "missing": [],
    }
    fields.update(overrides)
    return fields


class SloDailyEvidenceValidationTests(unittest.TestCase):
    def test_merge_replaces_same_date_and_keeps_thirty_newest(self):
        records = [
            {"date": f"2026-08-{day:02d}", **valid_fields()}
            for day in range(1, 32)
        ]
        replacement = {
            "date": "2026-08-31",
            **valid_fields(
                overall="unobservable",
                portal_ready="unobservable",
                missing=["portal_ready"],
            ),
        }

        result = module.merge_records(records, replacement)

        self.assertEqual(len(result), 30)
        self.assertEqual(result[0]["overall"], "unobservable")
        self.assertNotIn("2026-08-01", [item["date"] for item in result])
        self.assertEqual([item["date"] for item in result], sorted(
            (item["date"] for item in result), reverse=True
        ))

    def test_merge_collapses_preexisting_duplicate_dates(self):
        first = {"date": "2026-08-28", **valid_fields()}
        duplicate = {
            "date": "2026-08-28",
            **valid_fields(overall="unobservable", portal_ready="unobservable", missing=["portal_ready"]),
        }

        result = module.merge_records(
            [first, duplicate],
            {"date": "2026-08-30", **valid_fields()},
        )

        self.assertEqual(len([item for item in result if item["date"] == "2026-08-28"]), 1)

    def test_validate_record_returns_only_validated_fixed_fields(self):
        record = {"date": "2026-08-30", **valid_fields(), "extra": "ignored?"}

        result = module.validate_record(record)

        self.assertEqual(set(result), {
            "date", "collected_at", "overall", "public_health", "portal_ready",
            "portal_http", "crawler_freshness", "missing",
        })
        self.assertNotIn("extra", result)

    def test_validate_record_rejects_invalid_date_and_non_utc_timestamp(self):
        for record in (
            {"date": "2026-02-30", **valid_fields()},
            {"date": "2026-08-30", **valid_fields(collected_at="2026-08-30T18:00:00+09:00")},
        ):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    module.validate_record(record)

    def test_validate_record_rejects_invalid_enum_and_missing_contract(self):
        invalid_records = (
            {"date": "2026-08-30", **valid_fields(overall="failed")},
            {"date": "2026-08-30", **valid_fields(overall="ok", missing=["portal_ready"])},
            {"date": "2026-08-30", **valid_fields(missing=["unknown_source"], overall="unobservable")},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    module.validate_record(record)

    def test_validate_record_rejects_invalid_http_numbers(self):
        invalid_http_values = (
            {"requests": -1, "server_errors": 0, "server_error_ratio": 0.0, "p95_seconds": 0.0},
            {"requests": 1, "server_errors": 2, "server_error_ratio": 2.0, "p95_seconds": 0.0},
            {"requests": 1, "server_errors": 0, "server_error_ratio": math.nan, "p95_seconds": 0.0},
            {"requests": 0, "server_errors": 0, "server_error_ratio": 0.0, "p95_seconds": None},
            {"requests": 1, "server_errors": 0, "server_error_ratio": "0", "p95_seconds": 0.0},
        )
        for portal_http in invalid_http_values:
            with self.subTest(portal_http=portal_http):
                with self.assertRaises(ValueError):
                    module.validate_record({"date": "2026-08-30", **valid_fields(portal_http=portal_http)})

    def test_validate_record_accepts_null_http_and_zero_request_null_metrics(self):
        null_record = {
            "date": "2026-08-30",
            **valid_fields(
                overall="unobservable",
                portal_http=None,
                missing=["portal_http"],
            ),
        }
        zero_record = {
            "date": "2026-08-30",
            **valid_fields(portal_http={
                "requests": 0,
                "server_errors": 0,
                "server_error_ratio": None,
                "p95_seconds": None,
            }),
        }

        self.assertIsNone(module.validate_record(null_record)["portal_http"])
        self.assertEqual(module.validate_record(zero_record)["portal_http"]["requests"], 0)


class SloDailyEvidenceCollectionTests(unittest.TestCase):
    def test_crawler_query_uses_numeric_bool_multiplication_for_stale_conditions(self):
        query = module._QUERY_CRAWLER_FRESHNESS

        self.assertIn("crawler_news_collection_consecutive_failures < bool 3", query)
        self.assertIn(" * ", query)
        self.assertNotIn(" and ", query)

    def test_collector_allows_finite_float_counter_increases(self):
        result = module.collect_record(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            prometheus_query=lambda query: {
                module._QUERY_PORTAL_HTTP_REQUESTS: 100.5,
                module._QUERY_PORTAL_HTTP_ERRORS: 2.5,
                module._QUERY_PORTAL_HTTP_P95: 0.25,
                module._QUERY_PORTAL_READY: 1,
                module._QUERY_CRAWLER_FRESHNESS: 1,
            }[query],
            public_health_probe=lambda: 200,
        )

        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["portal_http"]["requests"], 100.5)
        self.assertEqual(result["portal_http"]["server_errors"], 2.5)

    def test_collector_records_zero_request_nan_p95_without_missing_evidence(self):
        result = module.collect_record(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            prometheus_query=lambda query: {
                module._QUERY_PORTAL_HTTP_REQUESTS: 0,
                module._QUERY_PORTAL_HTTP_ERRORS: 0,
                module._QUERY_PORTAL_HTTP_P95: math.nan,
                module._QUERY_PORTAL_READY: 1,
                module._QUERY_CRAWLER_FRESHNESS: 1,
            }[query],
            public_health_probe=lambda: 200,
        )

        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["portal_http"], {
            "requests": 0, "server_errors": 0,
            "server_error_ratio": None, "p95_seconds": None,
        })

    def test_collector_interprets_empty_error_vector_only_with_valid_request_total(self):
        class Response:
            status = 200

            def __init__(self, result):
                self.result = result

            def read(self):
                return json.dumps({"status": "success", "data": {
                    "resultType": "vector", "result": self.result,
                }}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        for total, expected in (("100", "ok"), ("0", "ok"), (None, "unobservable"), ("NaN", "unobservable")):
            with self.subTest(total=total):
                def fake_urlopen(request, timeout):
                    query = parse_qs(urlparse(request.full_url).query)["query"][0]
                    if query == module._QUERY_PORTAL_HTTP_ERRORS:
                        return Response([])
                    value = total if query == module._QUERY_PORTAL_HTTP_REQUESTS else (
                        "NaN" if query == module._QUERY_PORTAL_HTTP_P95 and total == "0" else "1"
                    )
                    return Response([] if value is None else [{"metric": {}, "value": [0, value]}])

                result = module.collect_record(
                    now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
                    public_health_probe=lambda: 200,
                    urlopen=fake_urlopen,
                )

                self.assertEqual(result["overall"], expected)
                if expected == "ok":
                    self.assertEqual(result["portal_http"]["server_errors"], 0)
                    self.assertEqual(result["portal_http"]["server_error_ratio"], None if total == "0" else 0)
                else:
                    self.assertIsNone(result["portal_http"])
                    self.assertIn("portal_http", result["missing"])

    def test_collector_marks_null_prometheus_data_as_unobservable(self):
        class Response:
            def __init__(self, body):
                self.status = 200
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        def fake_urlopen(request, timeout):
            if request.full_url == "https://public.example/health":
                return Response(b"ok")
            if "portal_http_requests_total%5B24h%5D" in request.full_url:
                return Response(json.dumps({"status": "success", "data": None}).encode())
            return Response(json.dumps({"status": "success", "data": {"result": [{"value": [0, "1"]}]}}).encode())

        result = module.collect_record(
            prometheus_url="http://prometheus.example/api/v1/query",
            public_health_url="https://public.example/health",
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            urlopen=fake_urlopen,
        )

        self.assertEqual(result["overall"], "unobservable")
        self.assertIn("portal_http", result["missing"])

    def test_collector_records_missing_prometheus_metric_as_unobservable(self):
        result = module.collect_record(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            prometheus_query=lambda query: {
                module._QUERY_PORTAL_HTTP_REQUESTS: None,
                module._QUERY_PORTAL_HTTP_ERRORS: 2,
                module._QUERY_PORTAL_HTTP_P95: 0.25,
                module._QUERY_PORTAL_READY: 1,
                module._QUERY_CRAWLER_FRESHNESS: 1,
            }[query],
            public_health_probe=lambda: 200,
        )

        self.assertEqual(result["overall"], "unobservable")
        self.assertIn("portal_http", result["missing"])
        self.assertEqual(result["public_health"], "ok")
        self.assertIsNone(result["portal_http"])

    def test_collector_preserves_public_non_200_as_failed(self):
        result = module.collect_record(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            prometheus_query=lambda query: {
                module._QUERY_PORTAL_HTTP_REQUESTS: 100,
                module._QUERY_PORTAL_HTTP_ERRORS: 2,
                module._QUERY_PORTAL_HTTP_P95: 0.25,
                module._QUERY_PORTAL_READY: 1,
                module._QUERY_CRAWLER_FRESHNESS: 1,
            }[query],
            public_health_probe=lambda: 503,
        )

        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["public_health"], "failed")
        self.assertEqual(result["missing"], [])

    def test_collector_marks_invalid_prometheus_number_unobservable(self):
        result = module.collect_record(
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            prometheus_query=lambda query: {
                module._QUERY_PORTAL_HTTP_REQUESTS: 100,
                module._QUERY_PORTAL_HTTP_ERRORS: 2,
                module._QUERY_PORTAL_HTTP_P95: "NaN",
                module._QUERY_PORTAL_READY: 1,
                module._QUERY_CRAWLER_FRESHNESS: 1,
            }[query],
            public_health_probe=lambda: 200,
        )

        self.assertEqual(result["overall"], "unobservable")
        self.assertIn("portal_http", result["missing"])

    def test_collector_uses_bounded_urllib_for_prometheus_and_public_health(self):
        class Response:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, timeout))
            if request.full_url == "https://public.example/health":
                return Response(200, b"ok")
            values = {
                "status_code%3D~%225..%22": "2",
                "portal_http_requests_total%5B24h%5D": "100",
                "histogram_quantile": "0.25",
                "kube_deployment_status_replicas_available": "1",
                "crawler_news_collection_initialized": "1",
            }
            value = next(value for text, value in values.items() if text in request.full_url)
            return Response(200, json.dumps({"status": "success", "data": {"result": [{"value": [0, value]}]}}).encode())

        result = module.collect_record(
            prometheus_url="http://prometheus.example/api/v1/query",
            public_health_url="https://public.example/health",
            now=datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc),
            urlopen=fake_urlopen,
        )

        self.assertEqual(result["overall"], "ok")
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(timeout <= module.REQUEST_TIMEOUT_SECONDS for _, timeout in calls))

    def test_cli_patches_only_validated_records_json(self):
        existing = [{"date": "2026-08-29", **valid_fields()}]
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "get" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps({"data": {"records.json": json.dumps(existing)}}), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        record = {"date": "2026-08-30", **valid_fields()}
        module.update_configmap(record, subprocess_run=fake_run)

        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[1][:4], ["kubectl", "patch", "configmap", module.EVIDENCE_CONFIGMAP])
        payload = json.loads(commands[1][-1])
        saved = json.loads(payload["data"]["records.json"])
        self.assertEqual([item["date"] for item in saved], ["2026-08-30", "2026-08-29"])


if __name__ == "__main__":
    unittest.main()
