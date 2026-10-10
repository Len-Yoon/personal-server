"""Retention policy checks use an in-memory rclone boundary; no Drive access."""

from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra/k8s/tools/pvc_backup_retention.py"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def load_retention():
    spec = importlib.util.spec_from_file_location("pvc_backup_retention", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def name(days_ago: int, pid: int = 100) -> str:
    when = NOW - timedelta(days=days_ago)
    return f"{when:%Y%m%dT%H%M%SZ}-{pid}.tar.age"


def entry(file_name: str, *, size: int = 100) -> dict:
    return {
        "Path": file_name, "Name": file_name, "ID": "object-" + file_name, "Size": size, "MimeType": "application/octet-stream",
        "ModTime": "2026-09-27T11:00:00Z", "IsDir": False,
    }


def evidence(service: str = "book-memo", *, backup_name: str | None = None) -> str:
    prefix = {"book-memo": "book", "youtube-memo": "youtube", "crawler-worker": "crawler", "portal": "portal"}[service]
    newest = backup_name or name(0)
    return "\n".join((
        "schema_version=1", f"scope={service}", "backup_status=success", "encrypted=true",
        "backup_completed_at=2026-09-27T12:00:00Z", "restore_status=success",
        "restore_verified_at=2026-09-27T12:00:00Z", "evidence_expires_at=2026-09-28T12:00:00Z",
        f"backup_id={prefix}-{newest.removesuffix('.tar.age')}",
        "artifact_digest=sha256:" + "a" * 64, "source_digest=sha256:" + "b" * 64,
        "source_runtime=k3s-pvc", "restore_path_check=success", "",
    ))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.retention = load_retention()
        self.retention._clock = lambda: NOW
        self.calls = []

    def run_policy(self, items, *, service="book-memo", proof=None, delete=False):
        remote_items = list(items)
        def command(*argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "lsjson" in argv:
                return json.dumps(remote_items)
            if "deletefile" in argv:
                remote_items[:] = [item for item in remote_items if item.get("Name") != argv[-1].rsplit("/", 1)[-1]]
                return ""
            raise AssertionError(argv)

        return self.retention.run_retention(
            service, proof if proof is not None else evidence(service),
            ("rclone", "--config", "/run/secrets/rclone-config"), command,
            delete=delete, now=NOW,
        )

    def test_portal_root_preserves_other_service_folders_and_unrelated_files(self):
        items = [{**entry("portal-" + name(day)), "ModTime": "2026-09-27T11:00:00Z"}
                 for day in range(10)]
        items += [{"Name": "book-memo", "Path": "book-memo", "IsDir": True}, entry("keep.txt")]
        proof = evidence().replace("scope=book-memo", "scope=portal").replace("backup_id=book-", "backup_id=portal-")
        candidates = self.run_policy(items, service="portal", proof=proof, delete=True)
        self.assertEqual(set(candidates), {"portal-" + name(8), "portal-" + name(9)})
        deletes = [argv[-1] for argv, _ in self.calls if "deletefile" in argv]
        self.assertTrue(all(value.startswith("gdrive:PersonalServer-encrypted-backups/portal-") for value in deletes))

    def test_retention_months_cross_year_and_preserve_exact_three_previous_months(self):
        now = datetime(2027, 1, 15, 0, tzinfo=timezone.utc)
        days = [datetime(2026, month, 1, tzinfo=timezone.utc) for month in (9, 10, 11, 12)]
        items = [(day, day.isoformat(), 1, "unused") for day in reversed(days)]
        self.assertEqual(self.retention.retained_names(items, now, minimum=0),
                         {day.isoformat() for day in days[1:]})

    def test_inventory_change_after_first_delete_stops_before_second_delete(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32, 33)]
        deleted = []
        def command(*argv, **kwargs):
            if "lsjson" in argv:
                remaining = [item for item in items if item["Name"] not in deleted]
                if deleted:
                    remaining[0] = {**remaining[0], "Size": 101}
                return json.dumps(remaining)
            deleted.append(argv[-1].rsplit("/", 1)[-1])
            return ""
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
        self.assertEqual(deleted, [name(33)])

    def test_remote_object_identity_change_blocks_deletion_even_when_size_time_match(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        listings = 0
        def command(*argv, **kwargs):
            nonlocal listings
            self.assertIn("lsjson", argv)
            listings += 1
            changed = [dict(item) for item in items]
            if listings > 1:
                changed[-1]["ID"] = "replaced-object"
            return json.dumps(changed)
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
        self.assertEqual(listings, 2)

    def test_tiered_policy_keeps_daily_weekly_monthly_representatives(self):
        days = list(range(121))
        candidates = self.run_policy([entry(name(day)) for day in days])
        kept = set(days) - {day for day in days if name(day) in candidates}
        self.assertEqual(kept, {0, 1, 2, 3, 4, 5, 6, 7, 14, 21, 28, 35, 58, 89})

    def test_same_day_duplicates_are_removed_but_latest_seven_are_protected(self):
        # Distinct creation seconds retain the existing seven-archive floor.
        names = [f"{NOW - timedelta(seconds=offset):%Y%m%dT%H%M%SZ}-100.tar.age"
                 for offset in range(10)]
        candidates = self.run_policy([entry(value) for value in names])
        self.assertEqual(len(candidates), 3)
        self.assertTrue(set(candidates).isdisjoint(names[:7]))

    def test_verified_same_second_pid_digit_transition_is_allowed_for_every_service(self):
        for service in ("portal", "book-memo", "youtube-memo", "crawler-worker"):
            with self.subTest(service=service):
                self.calls.clear()
                prefix = "portal-" if service == "portal" else ""
                old, protected = name(0, 9999), name(0, 10000)
                items = [{**entry(prefix + old), "ModTime": "2026-09-27T11:59:59.900000Z"},
                         {**entry(prefix + protected), "ModTime": "2026-09-27T11:59:59.100000Z"}]
                self.assertEqual(self.run_policy(items, service=service,
                                 proof=evidence(service, backup_name=protected), delete=True), [])
                self.assertFalse(any("deletefile" in argv for argv, _ in self.calls))

    def test_all_maximum_timestamp_ties_are_retained_beyond_seven_with_exact_report(self):
        cohort = [name(0, pid) for pid in range(9991, 10001)]
        items = [entry(value) for value in cohort] + [entry(name(31)), entry(name(32))]
        report = {}; deleted = []; remote = list(items)
        def command(*argv, **kwargs):
            if "lsjson" in argv: return json.dumps(remote)
            filename = argv[-1].rsplit("/", 1)[-1]; deleted.append(filename)
            remote[:] = [item for item in remote if item["Name"] != filename]
            return ""
        candidates = self.retention.run_retention("book-memo", evidence(backup_name=cohort[-1]),
                     ("rclone",), command, delete=True, now=NOW, report=report)
        self.assertEqual(candidates, [name(32)]); self.assertEqual(deleted, candidates)
        self.assertTrue(set(cohort).isdisjoint(deleted))
        self.assertEqual(report, {"total_count": 12, "retained_count": 11, "delete_count": 1,
                                  "total_bytes": 1200, "retained_bytes": 1100, "delete_bytes": 100})

    def test_protected_older_timestamp_or_missing_from_latest_cohort_blocks_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        for protected in (name(1), name(0, 999)):
            with self.subTest(protected=protected):
                self.calls.clear()
                with self.assertRaises(self.retention.RetentionError):
                    self.run_policy(items, proof=evidence(backup_name=protected), delete=True)
                self.assertFalse(any("deletefile" in argv for argv, _ in self.calls))

    def test_missing_restore_evidence_with_timestamp_ties_blocks_deletion(self):
        items = [entry(name(0, pid)) for pid in range(9991, 10001)] + [entry(name(32))]
        with self.assertRaises(self.retention.RetentionError):
            self.run_policy(items, proof="", delete=True)
        self.assertEqual(self.calls, [])

    def test_latest_timestamp_cohort_change_before_delete_blocks_all_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        listings = 0
        def command(*argv, **kwargs):
            nonlocal listings
            self.calls.append((argv, kwargs))
            if "lsjson" in argv:
                listings += 1
                return json.dumps(items if listings == 1 else items + [entry(name(0, 10000))])
            raise AssertionError("changed tie cohort must block deletion")
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command,
                                        delete=True, now=NOW)
        self.assertEqual(listings, 2)
        self.assertFalse(any("deletefile" in argv for argv, _ in self.calls))

    def test_korean_midnight_separates_daily_buckets(self):
        first = "20260926T150001Z-100.tar.age"
        second = "20260926T145959Z-100.tar.age"
        items = [entry(name(day)) for day in range(7)] + [entry(first), entry(second)]
        candidates = self.run_policy(items)
        # The pure bucket selector must distinguish the two Korean dates.
        parsed = self.retention._inventory(json.dumps([entry(first), entry(second)]), NOW)
        self.assertEqual(len(self.retention.retained_names(parsed, NOW, minimum=0)), 2)

    def test_expired_months_removed_but_sparse_history_keeps_seven(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 120, 180)]
        self.assertEqual(set(self.run_policy(items)), {name(120), name(180)})
        self.assertEqual(self.run_policy([entry(name(day)) for day in (0, 120, 180)]), [])

    def test_preview_and_go_preserve_monthly_representative(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        self.assertEqual(self.run_policy(items), [name(32)])
        self.assertTrue(all("deletefile" not in call[0] for call in self.calls))
        self.calls.clear()
        self.assertEqual(self.run_policy(items, delete=True), [name(32)])
        delete_calls = [call[0] for call in self.calls if "deletefile" in call[0]]
        self.assertEqual(
            [call[-1] for call in delete_calls],
            [f"gdrive:PersonalServer-encrypted-backups/book-memo/{name(32)}"],
        )

    def test_old_files_are_kept_when_fewer_than_seven_remain(self):
        items = [entry(name(day)) for day in (0, 31, 32, 33, 34, 35, 36)]
        self.assertEqual(self.run_policy(items), [])

    def test_thirty_day_boundary_is_not_deleted(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 30)]
        self.assertEqual(self.run_policy(items), [])

    def test_service_allowlist_and_fixed_remote_are_enforced(self):
        with self.assertRaises(self.retention.RetentionError):
            self.run_policy([], service="other", proof=evidence())
        self.assertEqual(self.calls, [])
        self.run_policy([entry(name(0))], service="youtube-memo")
        self.assertEqual(self.calls[0][0][-1], "gdrive:PersonalServer-encrypted-backups/youtube-memo")

    def test_unexpected_item_blocks_all_deletion(self):
        valid = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        for bad in (entry("readme.txt"), entry("../escape.tar.age"),
                    {**entry(name(32)), "IsDir": True},
                    {**entry(name(32)), "Path": "other/" + name(32)},
                    {**entry(name(32)), "Size": 0}, entry(name(32)), entry(name(32))):
            with self.subTest(bad=bad):
                self.calls.clear()
                entries = valid + ([bad] if not isinstance(bad, list) else bad)
                if bad == entry(name(32)):
                    entries.append(bad)
                with self.assertRaises(self.retention.RetentionError):
                    self.run_policy(entries, delete=True)
                self.assertFalse(any("deletefile" in call[0] for call in self.calls))

    def test_future_artifact_blocks_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        items.append(entry("20260928T120001Z-100.tar.age"))
        with self.assertRaises(self.retention.RetentionError):
            self.run_policy(items, delete=True)
        self.assertFalse(any("deletefile" in call[0] for call in self.calls))

    def test_missing_or_mismatched_or_stale_restore_proof_blocks_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        proofs = (
            "", evidence().replace("restore_status=success", "restore_status=failed"),
            evidence().replace("scope=book-memo", "scope=youtube-memo"),
            evidence().replace("backup_id=book-", "backup_id=other-"),
            evidence().replace("restore_verified_at=2026-09-27T12:00:00Z", "restore_verified_at=2026-09-25T11:00:00Z"),
            evidence().replace("backup_completed_at=2026-09-27T12:00:00Z", "backup_completed_at=2026-09-25T11:00:00Z"),
            evidence().replace("evidence_expires_at=2026-09-28T12:00:00Z", "evidence_expires_at=2026-09-27T11:59:59Z"),
            evidence().replace("backup_id=book-20260927T120000Z", "backup_id=book-20260926T120000Z"),
            evidence().replace("artifact_digest=sha256:" + "a" * 64, "artifact_digest=sha256:abc"),
        )
        for proof in proofs:
            with self.subTest(proof=proof[:30]):
                self.calls.clear()
                with self.assertRaises(self.retention.RetentionError):
                    self.run_policy(items, proof=proof, delete=True)
                self.assertFalse(any("deletefile" in call[0] for call in self.calls))

    def test_remote_inventory_errors_do_not_delete(self):
        for output in ("not-json", "{}", "[]"):
            self.calls.clear()
            def command(*argv, **kwargs):
                self.calls.append((argv, kwargs))
                return output
            with self.subTest(output=output), self.assertRaises(self.retention.RetentionError):
                self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
            self.assertFalse(any("deletefile" in call[0] for call in self.calls))

    def test_inventory_change_before_delete_blocks_permanent_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        listings = 0
        def command(*argv, **kwargs):
            nonlocal listings
            self.calls.append((argv, kwargs))
            if "lsjson" in argv:
                listings += 1
                return json.dumps(items if listings == 1 else items + [entry(name(33))])
            raise AssertionError("delete must not be reached")
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
        self.assertEqual(listings, 2)

    def test_delete_failure_stops_before_next_candidate(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        def command(*argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "lsjson" in argv:
                return json.dumps(items)
            if "deletefile" in argv:
                raise OSError("rclone failed")
            raise AssertionError(argv)
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
        delete_calls = [call[0] for call in self.calls if "deletefile" in call[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertIn("--drive-use-trash=false", delete_calls[0])

    def test_evidence_expiring_between_preview_and_delete_blocks_deletion(self):
        items = [entry(name(day)) for day in (0, 1, 2, 3, 4, 5, 6, 31, 32)]
        self.retention._clock = lambda: NOW + timedelta(days=1, seconds=1)
        def command(*argv, **kwargs):
            self.calls.append((argv, kwargs))
            if "lsjson" in argv:
                return json.dumps(items)
            raise AssertionError("expired evidence must prevent deletion")
        with self.assertRaises(self.retention.RetentionError):
            self.retention.run_retention("book-memo", evidence(), ("rclone",), command, delete=True, now=NOW)
        self.assertEqual(sum("lsjson" in call[0] for call in self.calls), 1)
        self.assertFalse(any("deletefile" in call[0] for call in self.calls))


if __name__ == "__main__":
    unittest.main()
