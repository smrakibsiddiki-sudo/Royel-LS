import ast
import tempfile
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from royells_v21_micro_workers import EventBus, MediaValidationEngine


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def _valid_mp4_bytes(payload_size: int = 2048) -> bytes:
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    moov = _box(b"moov")
    mdat = _box(b"mdat", (b"royells-video-payload" * 200)[:payload_size])
    return ftyp + moov + mdat


def _fragmented_mp4_bytes(fragment_count: int = 600) -> bytes:
    """Build a valid fragmented layout that exceeds the bounded box inspection limit."""

    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    moov = _box(b"moov")
    fragments = b"".join(
        _box(b"moof") + _box(b"mdat", f"fragment-{index:04d}".encode("ascii"))
        for index in range(fragment_count)
    )
    return ftyp + moov + fragments


def _video_message(
    *,
    file_size: int,
    width: int = 1280,
    height: int = 720,
    duration: int = 12,
    mime_type: str = "video/mp4",
):
    return SimpleNamespace(
        photo=None,
        video=SimpleNamespace(
            file_size=file_size,
            width=width,
            height=height,
            duration=duration,
            mime_type=mime_type,
            file_unique_id="video-uid",
            file_name="source.mp4",
        ),
    )


class MediaValidationEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bus = EventBus()
        self.engine = MediaValidationEngine(self.bus)

    def write_file(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    async def validate(self, message, path, probe=None, photo_validator=None):
        return await self.engine.validate_file(
            message=message,
            path=path,
            job_id="test-job",
            worker_id="test-worker",
            photo_validator=photo_validator,
            video_metadata_loader=probe,
        )

    async def test_valid_video_uses_telegram_dimensions_when_probe_returns_zeros(self):
        payload = _valid_mp4_bytes()
        path = self.write_file("valid.mp4", payload)
        message = _video_message(file_size=len(payload))

        result = await self.validate(
            message,
            path,
            probe=lambda _path: {"width": 0, "height": 0, "duration": 0},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.metadata["dimension_source"], "telegram_source")
        self.assertEqual(result.metadata["width"], 1280)
        self.assertEqual(result.metadata["height"], 720)
        self.assertEqual(result.size_bytes, len(payload))
        self.assertTrue(result.sha256)

    async def test_unknown_dimensions_are_advisory_for_complete_telegram_video(self):
        payload = _valid_mp4_bytes()
        path = self.write_file("unknown-dimensions.mp4", payload)
        message = _video_message(file_size=len(payload), width=0, height=0)

        result = await self.validate(
            message,
            path,
            probe=lambda _path: {"width": 0, "height": 0},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok_metadata_unknown")
        self.assertEqual(result.metadata["dimension_source"], "unavailable")
        self.assertFalse(result.retryable)

    async def test_probe_exception_is_advisory_when_source_metadata_is_coherent(self):
        payload = _valid_mp4_bytes()
        path = self.write_file("probe-failure.mp4", payload)
        message = _video_message(file_size=len(payload))

        def failing_probe(_path):
            raise TimeoutError("probe unavailable")

        result = await self.validate(message, path, probe=failing_probe)

        self.assertTrue(result.ok)
        self.assertEqual(result.metadata["dimension_source"], "telegram_source")
        self.assertIn("TimeoutError", result.metadata["metadata_probe_warning"])

    async def test_source_size_mismatch_is_retryable_incomplete_download(self):
        payload = _valid_mp4_bytes()
        path = self.write_file("truncated.mp4", payload)
        message = _video_message(file_size=len(payload) + 100)

        result = await self.validate(message, path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "incomplete")
        self.assertTrue(result.retryable)
        self.assertIn("size mismatch", result.reason)

    async def test_zero_byte_video_is_rejected_as_empty(self):
        path = self.write_file("empty.mp4", b"")
        message = _video_message(file_size=100)

        result = await self.validate(message, path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "empty")
        self.assertTrue(result.retryable)

    async def test_same_size_unrecognized_mp4_is_advisory_not_a_delivery_veto(self):
        payload = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2") + _box(
            b"mdat", b"payload" * 300
        )
        path = self.write_file("missing-moov.mp4", payload)
        message = _video_message(file_size=len(payload))

        result = await self.validate(message, path)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertIn("moov/moof", result.metadata["container_missing"])
        self.assertIn("source-size integrity", result.metadata["container_advisory"])

    async def test_many_fragment_mp4_is_advisory_when_bounded_inspection_limit_is_reached(self):
        payload = _fragmented_mp4_bytes()
        path = self.write_file("many-fragments.mp4", payload)
        message = _video_message(file_size=len(payload))

        result = await self.validate(message, path)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.metadata["container_status"], "indeterminate")
        self.assertTrue(result.metadata["container_inspection_limited"])
        self.assertEqual(result.metadata["container_box_count"], 512)
        self.assertGreater(result.metadata["container_trailing_bytes"], 0)
        self.assertIn("inspection limit", result.metadata["container_advisory"])

    async def test_truncated_mp4_box_is_advisory_when_telegram_size_is_coherent(self):
        valid_prefix = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2") + _box(b"moov")
        declared_size = 4096
        truncated_mdat = declared_size.to_bytes(4, "big") + b"mdat" + (b"x" * 2048)
        payload = valid_prefix + truncated_mdat
        path = self.write_file("truncated-box.mp4", payload)
        message = _video_message(file_size=len(payload))

        result = await self.validate(message, path)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.metadata["container_status"], "invalid")
        self.assertFalse(result.metadata["container_inspection_limited"])
        self.assertIn("truncated", result.metadata["container_error"])
        self.assertIn("source-size integrity", result.metadata["container_advisory"])

    async def test_photo_decoder_failure_remains_invalid(self):
        payload = b"not-an-image" * 100
        path = self.write_file("bad-photo.jpg", payload)
        message = SimpleNamespace(
            photo=SimpleNamespace(file_size=len(payload), width=10, height=10),
            video=None,
        )

        result = await self.validate(
            message,
            path,
            photo_validator=lambda _path: False,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "photo_decoder_rejected")
        self.assertTrue(result.retryable)

    async def test_513_byte_photo_uses_the_same_minimum_as_the_worker_pipeline(self):
        payload = b"\xff\xd8" + (b"r" * 509) + b"\xff\xd9"
        path = self.write_file("small-valid-photo.jpg", payload)
        message = SimpleNamespace(
            photo=SimpleNamespace(file_size=len(payload), width=1, height=1),
            video=None,
        )

        result = await self.validate(
            message,
            path,
            photo_validator=lambda _path: True,
        )

        self.assertEqual(len(payload), 513)
        self.assertEqual(self.engine.minimum_complete_size("photo"), 513)
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")

    async def test_512_byte_photo_remains_retryable_incomplete(self):
        payload = b"p" * 512
        path = self.write_file("too-small-photo.jpg", payload)
        message = SimpleNamespace(
            photo=SimpleNamespace(file_size=len(payload), width=1, height=1),
            video=None,
        )

        result = await self.validate(
            message,
            path,
            photo_validator=lambda _path: True,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "incomplete")
        self.assertTrue(result.retryable)

    async def test_unsupported_telegram_document_is_rejected(self):
        payload = b"document" * 200
        path = self.write_file("document.bin", payload)
        message = SimpleNamespace(
            photo=None,
            video=None,
            document=SimpleNamespace(file_size=len(payload)),
        )

        result = await self.validate(message, path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unsupported")
        self.assertFalse(result.retryable)


class LegacyFalseDeadMigrationTests(unittest.TestCase):
    def load_migration_function(self, state, saves, in_memory_dead):
        source_path = Path(__file__).resolve().parents[1] / "royells_media_bot_ready.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        function_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "release_legacy_false_zero_byte_quarantine"
        )
        module = ast.Module(body=[function_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {
            "STATE": state,
            "state_mutex": threading.RLock(),
            "dead_media_uids": in_memory_dead,
            "defaultdict": defaultdict,
            "now_iso": lambda: "2026-08-10T01:00:00+06:00",
            "save_state": saves.append,
            "LEGACY_FALSE_ZERO_BYTE_MIGRATION_ID": "migration-test-v2",
            "PREVIOUS_FALSE_ZERO_BYTE_MIGRATION_ID": "migration-test-v1",
            "MEDIA_VALIDATOR_SCHEMA_VERSION": 2,
            "guard_key": lambda value: str(value or "").strip(),
        }
        exec(compile(module, str(source_path), "exec"), namespace)
        return namespace["release_legacy_false_zero_byte_quarantine"]

    def test_migration_reopens_legacy_upload_verdicts_but_preserves_true_source_absence(self):
        state = {
            "dead_media": {
                "items": {
                    "false-video": {
                        "status": "dead",
                        "reason": "0-byte media terminal after 2 item attempts",
                        "source_chat_id": "-1001",
                        "source_message_id": 13386,
                        "source_title": "Source One",
                    },
                    "legacy-media-empty": {
                        "status": "dead",
                        "reason": "MEDIA_EMPTY from Telegram",
                        "source_chat_id": "-1002",
                        "source_message_id": 13387,
                        "source_title": "Source Two",
                        "updated_at": "2026-08-09T00:00:00+06:00",
                    },
                    "legacy-file-part": {
                        "status": "dead",
                        "reason": "Telegram says: [400 FILE_PART_X_MISSING]",
                        "source_chat_id": "-1003",
                        "source_message_id": 13388,
                        "source_title": "Source Three",
                    },
                    "true-source-absence": {
                        "status": "dead",
                        "reason": "source media no longer available (MESSAGE_ID_INVALID)",
                        "source_chat_id": "-1004",
                        "source_message_id": 13389,
                        "source_title": "Source Four",
                    },
                },
                "source_counts": {
                    "-1001": {"count": 99},
                    "-1002": {"count": 1},
                    "-1003": {"count": 1},
                    "-1004": {"count": 1},
                },
                "events": [],
            },
            "sync_source_manager": {
                "startup_hot_scan": {
                    "status": "complete",
                    "source_order": ["-1001", "-1002", "-1003", "-1004"],
                    "completed_sources": ["-1001", "-1002", "-1003", "-1004"],
                    "current_source": "",
                }
            },
        }
        saves = []
        in_memory_dead = {
            "false-video": 0,
            "legacy-media-empty": 0,
            "legacy-file-part": 0,
            "true-source-absence": 0,
        }
        migrate = self.load_migration_function(state, saves, in_memory_dead)

        released, affected = migrate()

        self.assertEqual(released, 3)
        self.assertEqual(affected, ["-1001", "-1002", "-1003"])
        self.assertNotIn("false-video", state["dead_media"]["items"])
        self.assertNotIn("legacy-media-empty", state["dead_media"]["items"])
        self.assertNotIn("legacy-file-part", state["dead_media"]["items"])
        self.assertIn("true-source-absence", state["dead_media"]["items"])
        self.assertNotIn("false-video", in_memory_dead)
        self.assertNotIn("legacy-media-empty", in_memory_dead)
        self.assertNotIn("legacy-file-part", in_memory_dead)
        self.assertIn("true-source-absence", in_memory_dead)
        self.assertEqual(state["dead_media"]["source_counts"]["-1004"]["count"], 1)
        scan_state = state["sync_source_manager"]["startup_hot_scan"]
        self.assertEqual(scan_state["completed_sources"], ["-1004"])
        self.assertEqual(
            scan_state["validator_repair_sources"],
            {"-1001": 1, "-1002": 1, "-1003": 1},
        )
        self.assertEqual(
            scan_state["validator_repair_items"],
            {
                "-1001": [{"message_id": 13386, "media_uid": "false-video"}],
                "-1002": [{"message_id": 13387, "media_uid": "legacy-media-empty"}],
                "-1003": [{"message_id": 13388, "media_uid": "legacy-file-part"}],
            },
        )
        self.assertEqual(scan_state["status"], "running")
        self.assertIn("dead_media", saves)
        self.assertIn("sync_source_manager", saves)

        snapshot = repr(state)
        second_result = migrate()
        self.assertEqual(second_result, (0, []))
        self.assertEqual(repr(state), snapshot)

    def test_v3_reopens_sources_already_released_by_v2(self):
        state = {
            "dead_media": {
                "items": {},
                "source_counts": {},
                "events": [],
                "migrations": {
                    "migration-test-v1": {
                        "released": 2,
                        "released_by_source": {"-1009": 2},
                    }
                },
            },
            "sync_source_manager": {
                "startup_hot_scan": {
                    "status": "complete",
                    "source_order": ["-1009"],
                    "completed_sources": ["-1009"],
                }
            },
        }
        saves = []
        migrate = self.load_migration_function(state, saves, {})

        released, affected = migrate()

        self.assertEqual(released, 2)
        self.assertEqual(affected, ["-1009"])
        scan = state["sync_source_manager"]["startup_hot_scan"]
        self.assertEqual(scan["completed_sources"], [])
        self.assertEqual(scan["validator_repair_sources"], {"-1009": 2})
        self.assertEqual(scan["validator_repair_items"], {})
        self.assertEqual(scan["status"], "running")


if __name__ == "__main__":
    unittest.main()
