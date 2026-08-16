"""Protocol tests for the standalone CPU-only compute helper service."""

from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from royells_compute_helper import HelperLimits
from royells_compute_helper_service import (
    ComputeHelperHTTPServer,
    ComputeHelperServiceConfig,
    sign_request,
    verify_response_signature,
)
from royells_compute_helper_client import ComputeHelperClientConfig, inspect_remote_file


SECRET = b"test-only-helper-secret-that-is-long-enough-123456"


@contextmanager
def helper_server():
    """Run an ephemeral authenticated helper without external network access."""

    with tempfile.TemporaryDirectory() as directory:
        config = ComputeHelperServiceConfig(
            shared_secret=SECRET,
            host="127.0.0.1",
            port=0,
            max_inflight=1,
            temp_dir=Path(directory),
            limits=HelperLimits(
                max_input_bytes=1024,
                hash_chunk_bytes=64,
                mp4_probe_bytes=256,
                mp4_max_box_bytes=128,
            ),
        )
        server = ComputeHelperHTTPServer(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, Path(directory)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def signed_headers(body: bytes, request_id: str, filename: str = "sample.bin") -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hashlib.sha256(body).hexdigest()
    return {
        "Content-Type": "application/octet-stream",
        "X-Royells-Request-Id": request_id,
        "X-Royells-Timestamp": timestamp,
        "X-Royells-Content-SHA256": digest,
        "X-Royells-Filename": filename,
        "X-Royells-Signature": sign_request(
            SECRET,
            timestamp=timestamp,
            request_id=request_id,
            content_length=str(len(body)),
            content_sha256=digest,
            filename=filename,
        ),
    }


def post(server: ComputeHelperHTTPServer, body: bytes, headers: dict[str, str]):
    """Send one bounded local request and return the complete response."""

    _host, port = server.server_address[:2]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST", "/v1/inspect", body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        return response.status, dict(response.getheaders()), response_body
    finally:
        connection.close()


class ComputeHelperServiceTests(unittest.TestCase):
    def test_placeholder_or_short_secret_cannot_start_a_helper(self):
        with self.assertRaises(ValueError):
            ComputeHelperServiceConfig(shared_secret=b"too-short")
        with self.assertRaises(ValueError):
            ComputeHelperServiceConfig(
                shared_secret=b"replace-with-a-random-48-byte-secret-value"
            )

    def test_client_refuses_plaintext_remote_endpoint(self):
        with self.assertRaises(ValueError):
            ComputeHelperClientConfig(
                url="http://helper.example.com/v1/inspect",
                shared_secret=SECRET,
            )

    def test_explicit_client_round_trip_requires_only_helper_secret(self):
        with tempfile.TemporaryDirectory() as client_directory:
            client_file = Path(client_directory) / "local-video.mp4"
            client_file.write_bytes(b"client-side helper proof")
            with helper_server() as (server, temporary_root):
                _host, port = server.server_address[:2]
                result = inspect_remote_file(
                    client_file,
                    ComputeHelperClientConfig(
                        url=f"http://127.0.0.1:{port}/v1/inspect",
                        shared_secret=SECRET,
                        max_input_bytes=1024,
                        allow_insecure_loopback=True,
                    ),
                )

                self.assertEqual(result["input_size_bytes"], client_file.stat().st_size)
                self.assertEqual(result["metadata"]["display_name"], "local-video.mp4")
                self.assertEqual(list(temporary_root.iterdir()), [])

    def test_valid_signed_request_is_inspected_signed_and_deleted(self):
        body = b"royells external compute helper"
        request_id = "valid-request-id-0001"
        with helper_server() as (server, temporary_root):
            status, headers, raw_body = post(server, body, signed_headers(body, request_id, "private.mp4"))

            self.assertEqual(status, 200)
            payload = json.loads(raw_body)
            self.assertEqual(payload["request_id"], request_id)
            self.assertEqual(payload["input_sha256"], hashlib.sha256(body).hexdigest())
            self.assertTrue(payload["helper_verdict_is_advisory"])
            self.assertEqual(payload["metadata"]["display_name"], "private.mp4")
            body_hash = hashlib.sha256(raw_body).hexdigest()
            self.assertEqual(headers["X-Royells-Response-SHA256"], body_hash)
            self.assertTrue(
                verify_response_signature(
                    SECRET,
                    headers["X-Royells-Response-Signature"],
                    request_id=request_id,
                    status_code=status,
                    body_sha256=body_hash,
                )
            )
            self.assertEqual(list(temporary_root.iterdir()), [])

    def test_bad_signature_is_rejected_without_creating_a_temp_artifact(self):
        body = b"not accepted"
        request_id = "bad-signature-0001"
        headers = signed_headers(body, request_id)
        headers["X-Royells-Signature"] = "0" * 64
        with helper_server() as (server, temporary_root):
            status, _headers, raw_body = post(server, body, headers)

            self.assertEqual(status, 401)
            self.assertEqual(json.loads(raw_body)["code"], "invalid_signature")
            self.assertEqual(list(temporary_root.iterdir()), [])

    def test_replay_and_oversize_requests_are_rejected_before_inspection(self):
        body = b"one-time"
        request_id = "replay-request-id-1"
        with helper_server() as (server, temporary_root):
            headers = signed_headers(body, request_id)
            first_status, _first_headers, _first_body = post(server, body, headers)
            replay_status, _replay_headers, replay_body = post(server, body, headers)
            oversized = b"x" * 1025
            oversized_status, _oversized_headers, oversized_body = post(
                server,
                oversized,
                signed_headers(oversized, "oversize-request-1"),
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(replay_status, 409)
            self.assertEqual(json.loads(replay_body)["code"], "replayed_or_busy_request")
            self.assertEqual(oversized_status, 413)
            self.assertEqual(json.loads(oversized_body)["code"], "payload_too_large")
            self.assertEqual(list(temporary_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
