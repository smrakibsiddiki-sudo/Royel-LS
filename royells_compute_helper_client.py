"""Minimal, explicit client for the separate Royells CPU-only helper service.

This module is intentionally not imported by the Telegram bot.  It is a
manual/sidecar tool for testing a separately deployed helper.  It never reads
Telegram credentials, queue state, or runtime files, and it refuses plaintext
remote HTTP endpoints by default.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from royells_compute_helper_service import (
    INSPECT_PATH,
    MAX_RESPONSE_BYTES,
    sign_request,
    verify_response_signature,
)


def _secret_from_environment() -> bytes:
    """Load only the dedicated helper secret, never a Telegram credential."""

    secret = os.getenv("ROYELLS_COMPUTE_HELPER_SHARED_SECRET", "").encode("utf-8")
    if len(secret) < 32:
        raise ValueError("ROYELLS_COMPUTE_HELPER_SHARED_SECRET must be at least 32 bytes")
    return secret


@dataclass(frozen=True)
class ComputeHelperClientConfig:
    """Safe immutable controls for one explicit remote inspection call."""

    url: str
    shared_secret: bytes
    timeout_seconds: int = 60
    max_input_bytes: int = 512 * 1024 * 1024
    allow_insecure_loopback: bool = False

    def __post_init__(self) -> None:
        secret = self.shared_secret if isinstance(self.shared_secret, bytes) else str(self.shared_secret or "").encode("utf-8")
        weak_markers = (b"replace-with", b"change-me", b"your-helper", b"example-secret")
        if len(secret) < 32 or any(marker in secret.lower() for marker in weak_markers):
            raise ValueError("compute helper shared secret must be at least 32 bytes")
        if not 5 <= int(self.timeout_seconds) <= 900:
            raise ValueError("timeout_seconds must be between 5 and 900")
        if not 1 <= int(self.max_input_bytes) <= 2 * 1024 * 1024 * 1024:
            raise ValueError("max_input_bytes is outside the supported range")
        _validated_endpoint(self.url, allow_insecure_loopback=self.allow_insecure_loopback)
        object.__setattr__(self, "shared_secret", secret)
        object.__setattr__(self, "url", str(self.url))
        object.__setattr__(self, "timeout_seconds", int(self.timeout_seconds))
        object.__setattr__(self, "max_input_bytes", int(self.max_input_bytes))


def _validated_endpoint(url: str, *, allow_insecure_loopback: bool) -> tuple[str, str, int, str]:
    """Validate a no-redirect HTTPS endpoint and return connection details."""

    parsed = urlsplit(str(url or "").strip())
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("compute helper URL must not contain credentials, query, or fragment")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (allow_insecure_loopback and scheme == "http" and is_loopback):
        raise ValueError("compute helper URL must use HTTPS (HTTP is allowed only for explicit loopback tests)")
    if not host:
        raise ValueError("compute helper URL requires a host")
    try:
        port = int(parsed.port or (443 if scheme == "https" else 80))
    except ValueError as exc:
        raise ValueError("invalid compute helper URL port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid compute helper URL port")
    path = parsed.path or INSPECT_PATH
    if path != INSPECT_PATH:
        raise ValueError(f"compute helper URL path must be {INSPECT_PATH}")
    return scheme, host, port, path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_remote_file(
    path: str | os.PathLike[str],
    config: ComputeHelperClientConfig,
    *,
    display_name: Optional[str] = None,
) -> Mapping[str, Any]:
    """Send one explicit local file to the helper and verify its signed response.

    This is deliberately a caller-controlled operation rather than a bot queue
    stage.  The main bot remains the only authority for Telegram uploads,
    retries, ledgers, and dead-media decisions.
    """

    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("inspect_remote_file requires a local non-symlink regular file")
    size_bytes = int(source.stat().st_size)
    if size_bytes <= 0 or size_bytes > config.max_input_bytes:
        raise ValueError("file is outside the configured helper upload limit")
    filename = Path(display_name or source.name).name
    if not filename or len(filename) > 256 or "\x00" in filename or "\r" in filename or "\n" in filename:
        raise ValueError("invalid display name")

    scheme, host, port, request_path = _validated_endpoint(
        config.url,
        allow_insecure_loopback=config.allow_insecure_loopback,
    )
    body_sha256 = _sha256_file(source)
    request_id = secrets.token_urlsafe(18).replace("=", "")
    timestamp = str(int(time.time()))
    signature = sign_request(
        config.shared_secret,
        timestamp=timestamp,
        request_id=request_id,
        content_length=str(size_bytes),
        content_sha256=body_sha256,
        filename=filename,
    )
    connection: http.client.HTTPConnection
    if scheme == "https":
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=config.timeout_seconds,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=config.timeout_seconds)

    try:
        connection.putrequest("POST", request_path, skip_accept_encoding=True)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(size_bytes))
        connection.putheader("X-Royells-Request-Id", request_id)
        connection.putheader("X-Royells-Timestamp", timestamp)
        connection.putheader("X-Royells-Content-SHA256", body_sha256)
        connection.putheader("X-Royells-Filename", filename)
        connection.putheader("X-Royells-Signature", signature)
        connection.endheaders()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                connection.send(chunk)
        response = connection.getresponse()
        raw_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise RuntimeError("compute helper response exceeded protocol limit")
        response_id = str(response.getheader("X-Royells-Request-Id") or "")
        response_hash = str(response.getheader("X-Royells-Response-SHA256") or "")
        response_signature = str(response.getheader("X-Royells-Response-Signature") or "")
        if response_id != request_id:
            raise RuntimeError("compute helper response request ID mismatch")
        actual_response_hash = hashlib.sha256(raw_body).hexdigest()
        if actual_response_hash != response_hash.lower() or not verify_response_signature(
            config.shared_secret,
            response_signature,
            request_id=request_id,
            status_code=response.status,
            body_sha256=actual_response_hash,
        ):
            raise RuntimeError("compute helper response signature verification failed")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("compute helper response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("compute helper response must be a JSON object")
        if response.status != 200:
            raise RuntimeError(f"compute helper rejected request: {payload.get('code') or response.status}")
        if str(payload.get("request_id") or "") != request_id:
            raise RuntimeError("compute helper response payload request ID mismatch")
        if str(payload.get("input_sha256") or "").lower() != body_sha256:
            raise RuntimeError("compute helper response payload hash mismatch")
        if int(payload.get("input_size_bytes", -1)) != size_bytes:
            raise RuntimeError("compute helper response payload size mismatch")
        return payload
    finally:
        connection.close()


def main(argv: Optional[list[str]] = None) -> int:
    """Run a one-file explicit inspection for deployment validation."""

    parser = argparse.ArgumentParser(description="Call an authenticated Royells compute helper")
    parser.add_argument("--url", required=True, help="HTTPS helper endpoint ending in /v1/inspect")
    parser.add_argument("--file", required=True, help="Local file to inspect explicitly")
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout in seconds")
    parser.add_argument(
        "--allow-insecure-loopback",
        action="store_true",
        help="Permit http://127.0.0.1 only for a local smoke test",
    )
    args = parser.parse_args(argv)
    try:
        config = ComputeHelperClientConfig(
            url=args.url,
            shared_secret=_secret_from_environment(),
            timeout_seconds=args.timeout,
            allow_insecure_loopback=bool(args.allow_insecure_loopback),
        )
        result = inspect_remote_file(args.file, config)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Compute helper client failed: {type(exc).__name__}: {str(exc)[:180]}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
