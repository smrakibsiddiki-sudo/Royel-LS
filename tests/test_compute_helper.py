"""Focused tests for the isolated, local-only compute helper."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from royells_compute_helper import HelperLimits, inspect_bytes, inspect_local_file


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def _valid_video_mp4(width: int = 1280, height: int = 720) -> bytes:
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    # FullBox + creation + modification + timescale + duration.
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + (1000).to_bytes(4, "big") + (3250).to_bytes(4, "big"))
    tkhd_payload = bytearray(84)
    tkhd_payload[0] = 0
    tkhd_payload[76:80] = (width << 16).to_bytes(4, "big")
    tkhd_payload[80:84] = (height << 16).to_bytes(4, "big")
    tkhd = _box(b"tkhd", bytes(tkhd_payload))
    hdlr = _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00" * 4 + b"vide")
    trak = _box(b"trak", tkhd + _box(b"mdia", hdlr))
    return ftyp + _box(b"mdat", b"payload") + _box(b"moov", mvhd + trak)


class ComputeHelperTests(unittest.TestCase):
    def test_bytes_hash_and_metadata_are_structured(self):
        payload = b"royells-helper-payload"

        result = inspect_bytes(payload, filename="/not-leaked/example.bin", inspect_mp4=False)

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.metadata.display_name, "example.bin")
        self.assertNotIn("/not-leaked", str(result.to_dict()))

    def test_bytes_limit_is_non_terminal_and_does_not_hash(self):
        limits = HelperLimits(
            max_input_bytes=8,
            hash_chunk_bytes=4,
            mp4_probe_bytes=16,
            mp4_max_box_bytes=16,
        )

        result = inspect_bytes(b"123456789", limits=limits)

        self.assertEqual(result.status, "limited")
        self.assertEqual(result.code, "input_too_large")
        self.assertIsNone(result.sha256)
        self.assertEqual(result.metadata.size_bytes, 9)

    def test_mp4_probe_returns_video_dimensions_and_duration(self):
        result = inspect_bytes(_valid_video_mp4(), filename="clip.mp4")

        self.assertTrue(result.ok)
        self.assertTrue(result.mp4.recognized)
        self.assertTrue(result.mp4.moov_found)
        self.assertTrue(result.mp4.moov_parsed)
        self.assertEqual(result.mp4.ftyp_major_brand, "isom")
        self.assertEqual(result.mp4.video_dimensions, ((1280, 720),))
        self.assertEqual(result.mp4.duration_seconds, 3.25)

    def test_local_file_requires_allow_list_and_never_returns_host_path(self):
        with tempfile.TemporaryDirectory() as root_directory, tempfile.TemporaryDirectory() as outside_directory:
            root = Path(root_directory)
            allowed_path = root / "allowed.bin"
            allowed_path.write_bytes(b"allowed")
            outside_path = Path(outside_directory) / "outside.bin"
            outside_path.write_bytes(b"outside")

            accepted = inspect_local_file(allowed_path, allowed_roots=root, inspect_mp4=False)
            rejected = inspect_local_file(outside_path, allowed_roots=root, inspect_mp4=False)

            self.assertTrue(accepted.ok)
            self.assertEqual(accepted.metadata.display_name, "allowed.bin")
            self.assertNotIn(str(root), str(accepted.to_dict()))
            self.assertEqual(rejected.status, "rejected")
            self.assertEqual(rejected.code, "path_outside_allowed_roots")

    def test_large_mp4_metadata_box_is_limited_not_declared_invalid(self):
        payload = _box(b"ftyp", b"isom\x00\x00\x02\x00isom") + _box(b"moov", b"x" * 128)
        limits = HelperLimits(
            max_input_bytes=1024,
            hash_chunk_bytes=64,
            mp4_probe_bytes=256,
            mp4_max_box_bytes=64,
        )

        result = inspect_bytes(payload, filename="bounded.mp4", limits=limits)

        self.assertEqual(result.status, "limited")
        self.assertEqual(result.code, "mp4_probe_limited")
        self.assertTrue(result.mp4.recognized)
        self.assertIn("moov_box_exceeds_parser_limit", result.mp4.warnings)


if __name__ == "__main__":
    unittest.main()
