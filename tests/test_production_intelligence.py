import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from royells_production_intelligence import (
    DEFAULT_DISK_CAPACITY_CEILING_BYTES,
    ProductionIntelligenceSystem,
    classify_incident,
    incident_classification_details,
    process_resources,
    sane_disk_snapshot,
)


class DiskSnapshotTests(unittest.TestCase):
    def test_absurd_virtual_capacity_is_untrusted_but_raw_values_are_retained(self):
        raw_total = 47 * 1024**5
        raw_used = 3 * 1024**4
        raw_free = raw_total - raw_used
        mocked_usage = SimpleNamespace(total=raw_total, used=raw_used, free=raw_free)

        with patch("royells_production_intelligence.shutil.disk_usage", return_value=mocked_usage):
            snapshot = sane_disk_snapshot(
                "/virtual/runtime",
                capacity_ceiling_bytes=8 * 1024**5,
            )

        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["trusted"])
        self.assertEqual(snapshot["status"], "untrusted")
        self.assertEqual(snapshot["anomaly"], "capacity_exceeds_ceiling")
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["used"], 0)
        self.assertEqual(snapshot["free"], 0)
        self.assertEqual(snapshot["free_mb"], 0)
        self.assertEqual(snapshot["raw"]["total"], raw_total)
        self.assertEqual(snapshot["raw"]["used"], raw_used)
        self.assertEqual(snapshot["raw"]["free"], raw_free)

    def test_normal_capacity_remains_available_and_trusted(self):
        raw_total = 100 * 1024**3
        raw_used = 40 * 1024**3
        raw_free = 60 * 1024**3
        mocked_usage = SimpleNamespace(total=raw_total, used=raw_used, free=raw_free)

        with patch("royells_production_intelligence.shutil.disk_usage", return_value=mocked_usage):
            snapshot = sane_disk_snapshot("/runtime")

        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["trusted"])
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["anomaly"], "")
        self.assertEqual(snapshot["total"], raw_total)
        self.assertEqual(snapshot["used"], raw_used)
        self.assertEqual(snapshot["free"], raw_free)
        self.assertEqual(snapshot["free_mb"], raw_free // (1024 * 1024))

    def test_process_resources_uses_configurable_sane_disk_snapshot(self):
        raw_total = 47 * 1024**5
        mocked_usage = SimpleNamespace(total=raw_total, used=0, free=raw_total)
        configured_ceiling = 2 * 1024**5

        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.dict(
                os.environ,
                {"ROYELLS_DISK_CAPACITY_CEILING_BYTES": str(configured_ceiling)},
            ), patch(
                "royells_production_intelligence.shutil.disk_usage",
                return_value=mocked_usage,
            ):
                resources = process_resources(temporary_dir)

        disk = resources["disk"]
        self.assertEqual(disk["capacity_ceiling_bytes"], configured_ceiling)
        self.assertEqual(disk["anomaly"], "capacity_exceeds_ceiling")
        self.assertEqual(disk["total"], 0)
        self.assertEqual(disk["raw"]["total"], raw_total)

    def test_non_positive_configured_ceiling_falls_back_to_safe_default(self):
        mocked_usage = SimpleNamespace(total=1024**3, used=0, free=1024**3)

        with patch("royells_production_intelligence.shutil.disk_usage", return_value=mocked_usage):
            snapshot = sane_disk_snapshot("/runtime", capacity_ceiling_bytes=0)

        self.assertEqual(snapshot["capacity_ceiling_bytes"], DEFAULT_DISK_CAPACITY_CEILING_BYTES)
        self.assertTrue(snapshot["trusted"])


class IncidentClassificationTests(unittest.TestCase):
    def test_retryable_media_states_are_temporary(self):
        cases = (
            ("Zero-byte Media", "0-byte media quarantined after bounded item retries"),
            ("MEDIA_EMPTY", "Telegram returned MEDIA_EMPTY; retry scheduled"),
            ("Download Failure", "download incomplete and deferred"),
        )
        for incident_type, detail in cases:
            with self.subTest(incident_type=incident_type, detail=detail):
                self.assertEqual(classify_incident(incident_type, detail), "Temporary")

    def test_only_confirmed_media_corruption_is_permanent(self):
        self.assertEqual(
            classify_incident("Invalid Media", "validation result: corrupt_container"),
            "Permanent",
        )
        self.assertNotEqual(
            classify_incident("Invalid Media", "video metadata missing dimensions"),
            "Permanent",
        )
        self.assertNotEqual(
            classify_incident("Dead Media", "not readable by optional metadata probe"),
            "Permanent",
        )

    def test_record_from_log_uses_raw_text_for_root_cause(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            intelligence = ProductionIntelligenceSystem(Path(temporary_dir))
            confirmed = intelligence.record_from_log(
                "download failed: validation result confirmed corrupt container"
            )
            retryable = intelligence.record_from_log(
                "0-byte media quarantined after bounded item retries: https://t.me/example/1"
            )

        self.assertEqual(confirmed["incident_type"], "Download Failure")
        self.assertEqual(confirmed["root_cause"], "Permanent")
        self.assertEqual(retryable["incident_type"], "Zero-byte Media")
        self.assertEqual(retryable["root_cause"], "Temporary")

    def test_typed_classification_and_build_identity_are_persisted(self):
        details = incident_classification_details(
            "Zero-byte Media",
            "download",
            raw_text="0-byte item deferred for retry",
        )
        self.assertEqual(details["fault_domain"], "media_integrity")
        self.assertEqual(details["retryability"], "retryable")

        with tempfile.TemporaryDirectory() as temporary_dir:
            intelligence = ProductionIntelligenceSystem(Path(temporary_dir))
            incident = intelligence.record_incident(
                incident_type="Zero-byte Media",
                severity="warning",
                subsystem="download",
                context={
                    "job_id": "job-1",
                    "message_id": 13386,
                    "chat_id": -1003980226936,
                    "media_uid": "uid-13386",
                    "build_identity": {
                        "build_id": "build-test",
                        "validator_schema": 3,
                    },
                },
                raw_text="download incomplete; retry scheduled",
                recovery={"attempted": True, "retry_scheduled": True},
            )

        self.assertEqual(incident["retryability"], "retryable")
        self.assertEqual(incident["message_id"], 13386)
        self.assertEqual(incident["media_uid"], "uid-13386")
        self.assertEqual(incident["build_identity"]["build_id"], "build-test")
        self.assertTrue(incident["recovery"]["retry_scheduled"])


if __name__ == "__main__":
    unittest.main()
