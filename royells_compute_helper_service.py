"""Authenticated, CPU-only Royells compute-helper service.

This process is intentionally **not** a Telegram worker.  It accepts one
already-obtained media artifact over an authenticated HTTP boundary, performs
bounded local inspection, returns signed diagnostic metadata, and deletes the
temporary artifact before responding.  It has no Pyrogram dependency, no
Telegram credentials, no queue/state database, and no target/source access.

Deploy it behind HTTPS (or a private tunnel) on a separate host.  The default
bind address is loopback to prevent an accidental public unauthenticated
exposure.  HMAC protects the request and response payloads; it is not a
replacement for transport encryption.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional

from royells_compute_helper import HelperLimits, inspect_local_file


PROTOCOL_VERSION = "royells-compute-v1"
INSPECT_PATH = "/v1/inspect"
HEALTH_PATH = "/health"
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read one bounded integer environment value without leaking it."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _secret_bytes(value: str | bytes) -> bytes:
    """Normalize a helper-only secret; reject weak/missing configuration."""

    secret = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    weak_markers = (b"replace-with", b"change-me", b"your-helper", b"example-secret")
    if len(secret) < 32 or any(marker in secret.lower() for marker in weak_markers):
        raise ValueError("compute helper shared secret must be at least 32 bytes")
    return secret


@dataclass(frozen=True)
class ComputeHelperServiceConfig:
    """Immutable resource and authentication controls for the helper process."""

    shared_secret: bytes
    host: str = "127.0.0.1"
    port: int = 8080
    max_inflight: int = 1
    read_timeout_seconds: int = 60
    timestamp_skew_seconds: int = 300
    replay_ttl_seconds: int = 600
    replay_capacity: int = 4096
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "royells-compute-helper")
    limits: HelperLimits = field(
        default_factory=lambda: HelperLimits(
            max_input_bytes=512 * 1024 * 1024,
            hash_chunk_bytes=1024 * 1024,
            mp4_probe_bytes=4 * 1024 * 1024,
            mp4_max_box_bytes=2 * 1024 * 1024,
            max_mp4_boxes=512,
            max_mp4_depth=12,
        )
    )

    def __post_init__(self) -> None:
        secret = _secret_bytes(self.shared_secret)
        if not self.host or len(self.host) > 255:
            raise ValueError("invalid compute helper bind host")
        # Port zero is useful only for an ephemeral local test listener; the
        # environment loader below still requires an explicit production port.
        if not 0 <= int(self.port) <= 65535:
            raise ValueError("invalid compute helper port")
        if not 1 <= int(self.max_inflight) <= 16:
            raise ValueError("max_inflight must be between 1 and 16")
        if not 5 <= int(self.read_timeout_seconds) <= 900:
            raise ValueError("read_timeout_seconds must be between 5 and 900")
        if not 30 <= int(self.timestamp_skew_seconds) <= 3600:
            raise ValueError("timestamp_skew_seconds must be between 30 and 3600")
        if not self.temp_dir:
            raise ValueError("temp_dir is required")
        # Keep the service internals type-stable even when a deployment
        # framework constructs this dataclass from strings.
        object.__setattr__(self, "shared_secret", secret)
        object.__setattr__(self, "host", str(self.host))
        object.__setattr__(self, "port", int(self.port))
        object.__setattr__(self, "max_inflight", int(self.max_inflight))
        object.__setattr__(self, "read_timeout_seconds", int(self.read_timeout_seconds))
        object.__setattr__(self, "timestamp_skew_seconds", int(self.timestamp_skew_seconds))
        object.__setattr__(self, "replay_ttl_seconds", int(self.replay_ttl_seconds))
        object.__setattr__(self, "replay_capacity", int(self.replay_capacity))
        object.__setattr__(self, "temp_dir", Path(self.temp_dir))

    @classmethod
    def from_environment(cls) -> "ComputeHelperServiceConfig":
        """Build a safe service configuration from helper-only environment values."""

        secret = _secret_bytes(os.getenv("ROYELLS_COMPUTE_HELPER_SHARED_SECRET", ""))
        max_bytes = _bounded_env_int(
            "ROYELLS_COMPUTE_HELPER_MAX_BYTES",
            512 * 1024 * 1024,
            1024,
            2 * 1024 * 1024 * 1024,
        )
        temp_dir = Path(
            os.getenv("ROYELLS_COMPUTE_HELPER_TEMP_DIR", "").strip()
            or (Path(tempfile.gettempdir()) / "royells-compute-helper")
        )
        return cls(
            shared_secret=secret,
            host=os.getenv("ROYELLS_COMPUTE_HELPER_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_bounded_env_int("PORT", 8080, 1, 65535),
            max_inflight=_bounded_env_int("ROYELLS_COMPUTE_HELPER_MAX_INFLIGHT", 1, 1, 16),
            read_timeout_seconds=_bounded_env_int(
                "ROYELLS_COMPUTE_HELPER_READ_TIMEOUT_SECONDS", 60, 5, 900
            ),
            timestamp_skew_seconds=_bounded_env_int(
                "ROYELLS_COMPUTE_HELPER_TIMESTAMP_SKEW_SECONDS", 300, 30, 3600
            ),
            replay_ttl_seconds=_bounded_env_int(
                "ROYELLS_COMPUTE_HELPER_REPLAY_TTL_SECONDS", 600, 60, 3600
            ),
            replay_capacity=_bounded_env_int(
                "ROYELLS_COMPUTE_HELPER_REPLAY_CAPACITY", 4096, 64, 100000
            ),
            temp_dir=temp_dir,
            limits=HelperLimits(
                max_input_bytes=max_bytes,
                hash_chunk_bytes=min(1024 * 1024, max_bytes),
                mp4_probe_bytes=min(4 * 1024 * 1024, max_bytes),
                mp4_max_box_bytes=min(2 * 1024 * 1024, max_bytes),
                max_mp4_boxes=512,
                max_mp4_depth=12,
            ),
        )


def canonical_request_bytes(
    *,
    timestamp: str,
    request_id: str,
    content_length: str,
    content_sha256: str,
    filename: str = "",
) -> bytes:
    """Return the exact HMAC material for an inspection request."""

    fields = (
        PROTOCOL_VERSION,
        "POST",
        INSPECT_PATH,
        str(timestamp),
        str(request_id),
        str(content_length),
        str(content_sha256).lower(),
        str(filename),
    )
    return "\n".join(fields).encode("utf-8")


def sign_request(
    shared_secret: str | bytes,
    *,
    timestamp: str,
    request_id: str,
    content_length: str,
    content_sha256: str,
    filename: str = "",
) -> str:
    """Return a lower-case HMAC-SHA256 request signature for a trusted client."""

    return hmac.new(
        _secret_bytes(shared_secret),
        canonical_request_bytes(
            timestamp=timestamp,
            request_id=request_id,
            content_length=content_length,
            content_sha256=content_sha256,
            filename=filename,
        ),
        hashlib.sha256,
    ).hexdigest()


def verify_request_signature(
    shared_secret: str | bytes,
    signature: str,
    **request_fields: str,
) -> bool:
    """Constant-time verify one HMAC request signature."""

    if not isinstance(signature, str) or not SHA256_PATTERN.fullmatch(signature):
        return False
    expected = sign_request(shared_secret, **request_fields)
    return hmac.compare_digest(expected, signature.lower())


def canonical_response_bytes(*, request_id: str, status_code: int, body_sha256: str) -> bytes:
    """Return the HMAC material used to authenticate one helper response."""

    return "\n".join(
        (PROTOCOL_VERSION, "RESPONSE", str(request_id), str(int(status_code)), str(body_sha256).lower())
    ).encode("utf-8")


def sign_response(
    shared_secret: str | bytes,
    *,
    request_id: str,
    status_code: int,
    body_sha256: str,
) -> str:
    """Return an HMAC-SHA256 response signature for a trusted client."""

    return hmac.new(
        _secret_bytes(shared_secret),
        canonical_response_bytes(
            request_id=request_id,
            status_code=status_code,
            body_sha256=body_sha256,
        ),
        hashlib.sha256,
    ).hexdigest()


def verify_response_signature(
    shared_secret: str | bytes,
    signature: str,
    *,
    request_id: str,
    status_code: int,
    body_sha256: str,
) -> bool:
    """Constant-time verify a signed helper response before consuming it."""

    if not isinstance(signature, str) or not SHA256_PATTERN.fullmatch(signature):
        return False
    expected = sign_response(
        shared_secret,
        request_id=request_id,
        status_code=status_code,
        body_sha256=body_sha256,
    )
    return hmac.compare_digest(expected, signature.lower())


class ReplayGuard:
    """Bounded replay protection for authenticated request IDs."""

    def __init__(self, *, ttl_seconds: int, capacity: int) -> None:
        self._ttl_seconds = int(ttl_seconds)
        self._capacity = int(capacity)
        self._entries: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, request_id: str, now_epoch: Optional[float] = None) -> bool:
        """Claim one ID once, returning false for replay or exhausted capacity."""

        now = float(time.time() if now_epoch is None else now_epoch)
        with self._lock:
            expired = [key for key, expires_at in self._entries.items() if expires_at <= now]
            for key in expired:
                self._entries.pop(key, None)
            if request_id in self._entries or len(self._entries) >= self._capacity:
                return False
            self._entries[request_id] = now + self._ttl_seconds
            return True


def _safe_suffix(filename: str) -> str:
    """Return a harmless file suffix for a private temporary artifact."""

    candidate = Path(str(filename or "")).name
    suffix = Path(candidate).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        return ".bin"
    return suffix


def _read_authenticated_payload(
    stream: Any,
    *,
    content_length: int,
    expected_sha256: str,
    temp_dir: Path,
    filename: str,
) -> tuple[Optional[Path], Optional[str]]:
    """Stream one bounded upload to a private file and verify its declared hash."""

    try:
        temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            pass
        descriptor, raw_path = tempfile.mkstemp(
            prefix="inspect-",
            suffix=_safe_suffix(filename),
            dir=temp_dir,
        )
    except OSError:
        return None, "temporary_storage_unavailable"

    path = Path(raw_path)
    digest = hashlib.sha256()
    remaining = int(content_length)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return None, "truncated_request_body"
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual = digest.hexdigest()
        if not hmac.compare_digest(actual, str(expected_sha256).lower()):
            return None, "content_hash_mismatch"
        return path, None
    except (OSError, TimeoutError):
        return None, "request_body_read_failed"
    finally:
        if remaining > 0 or not path.exists() or (
            path.exists() and digest.hexdigest().lower() != str(expected_sha256).lower()
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class ComputeHelperHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying only helper configuration and bounded guards."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: ComputeHelperServiceConfig) -> None:
        self.config = config
        self.replay_guard = ReplayGuard(
            ttl_seconds=config.replay_ttl_seconds,
            capacity=config.replay_capacity,
        )
        self.inflight = threading.BoundedSemaphore(config.max_inflight)
        super().__init__((config.host, config.port), ComputeHelperRequestHandler)


class ComputeHelperRequestHandler(BaseHTTPRequestHandler):
    """Serve HMAC-authenticated inspection requests without logging payload data."""

    server: ComputeHelperHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.config.read_timeout_seconds)

    def log_message(self, _format: str, *_args: object) -> None:
        # Request URLs and filenames can be sensitive; caller owns operational logs.
        return

    def _write_json(
        self,
        status_code: int,
        payload: Mapping[str, Any],
        *,
        request_id: str = "",
        sign: bool = False,
    ) -> None:
        # One request per connection avoids leaving an attacker-controlled
        # surplus body available for HTTP/1.1 request smuggling on a reused
        # socket. The helper has tiny control traffic, so keep-alive is not a
        # meaningful throughput optimization here.
        self.close_connection = True
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            body = b'{"code":"response_too_large","ok":false}'
            status_code = 500
            sign = False
        self.send_response(int(status_code))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        if request_id:
            self.send_header("X-Royells-Request-Id", request_id)
        if sign and request_id:
            body_sha256 = hashlib.sha256(body).hexdigest()
            self.send_header("X-Royells-Response-SHA256", body_sha256)
            self.send_header(
                "X-Royells-Response-Signature",
                sign_response(
                    self.server.config.shared_secret,
                    request_id=request_id,
                    status_code=status_code,
                    body_sha256=body_sha256,
                ),
            )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != HEALTH_PATH:
            self._write_json(404, {"ok": False, "code": "not_found"})
            return
        self._write_json(
            200,
            {
                "ok": True,
                "service": "royells-compute-helper",
                "protocol": PROTOCOL_VERSION,
                "max_bytes": self.server.config.limits.max_input_bytes,
            },
        )

    def do_POST(self) -> None:
        if self.path != INSPECT_PATH:
            self._write_json(404, {"ok": False, "code": "not_found"})
            return
        content_type = str(self.headers.get("Content-Type") or "").lower()
        if not content_type.startswith("application/octet-stream"):
            self._write_json(415, {"ok": False, "code": "content_type_required"})
            return

        request_id = str(self.headers.get("X-Royells-Request-Id") or "")
        timestamp = str(self.headers.get("X-Royells-Timestamp") or "")
        content_length_text = str(self.headers.get("Content-Length") or "")
        content_sha256 = str(self.headers.get("X-Royells-Content-SHA256") or "")
        filename = str(self.headers.get("X-Royells-Filename") or "")
        signature = str(self.headers.get("X-Royells-Signature") or "")

        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            self._write_json(400, {"ok": False, "code": "invalid_request_id"})
            return
        if not SHA256_PATTERN.fullmatch(content_sha256):
            self._write_json(400, {"ok": False, "code": "invalid_content_hash"}, request_id=request_id)
            return
        if len(filename) > 256 or "\x00" in filename or "\n" in filename or "\r" in filename:
            self._write_json(400, {"ok": False, "code": "invalid_filename"}, request_id=request_id)
            return
        try:
            content_length = int(content_length_text)
            timestamp_epoch = int(timestamp)
        except (TypeError, ValueError):
            self._write_json(400, {"ok": False, "code": "invalid_request_headers"}, request_id=request_id)
            return
        if content_length < 0 or content_length > self.server.config.limits.max_input_bytes:
            self._write_json(413, {"ok": False, "code": "payload_too_large"}, request_id=request_id)
            return
        if abs(int(time.time()) - timestamp_epoch) > self.server.config.timestamp_skew_seconds:
            self._write_json(401, {"ok": False, "code": "stale_request"}, request_id=request_id)
            return
        if not verify_request_signature(
            self.server.config.shared_secret,
            signature,
            timestamp=timestamp,
            request_id=request_id,
            content_length=content_length_text,
            content_sha256=content_sha256,
            filename=filename,
        ):
            self._write_json(401, {"ok": False, "code": "invalid_signature"}, request_id=request_id)
            return
        if not self.server.replay_guard.claim(request_id):
            self._write_json(409, {"ok": False, "code": "replayed_or_busy_request"}, request_id=request_id)
            return
        if not self.server.inflight.acquire(blocking=False):
            self._write_json(429, {"ok": False, "code": "helper_busy"}, request_id=request_id, sign=True)
            return

        temporary_path: Optional[Path] = None
        try:
            temporary_path, error_code = _read_authenticated_payload(
                self.rfile,
                content_length=content_length,
                expected_sha256=content_sha256,
                temp_dir=self.server.config.temp_dir,
                filename=filename,
            )
            if error_code is not None or temporary_path is None:
                self._write_json(
                    400,
                    {"ok": False, "code": error_code or "request_body_rejected"},
                    request_id=request_id,
                    sign=True,
                )
                return
            inspection = inspect_local_file(
                temporary_path,
                allowed_roots=self.server.config.temp_dir,
                include_sha256=True,
                inspect_mp4=True,
                limits=self.server.config.limits,
            )
            result = inspection.to_dict()
            if isinstance(result.get("metadata"), dict):
                # Return only the client-provided display name, never the
                # random helper temp-file name or any local filesystem path.
                result["metadata"]["display_name"] = Path(filename).name if filename else None
            result.update(
                {
                    "protocol": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "input_sha256": content_sha256.lower(),
                    "input_size_bytes": content_length,
                    "helper_verdict_is_advisory": True,
                }
            )
            # Do not acknowledge a successful remote inspection until the
            # uploaded artifact is gone.  This makes the privacy/lifecycle
            # guarantee observable to the caller and avoids a race with a
            # client that immediately checks helper cleanup.
            try:
                temporary_path.unlink(missing_ok=True)
                temporary_path = None
            except OSError:
                self._write_json(
                    500,
                    {"ok": False, "code": "temporary_cleanup_failed"},
                    request_id=request_id,
                    sign=True,
                )
                return
            self._write_json(200, result, request_id=request_id, sign=True)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            # Do not serialize stack traces, local paths, secrets, or media metadata.
            self._write_json(
                500,
                {"ok": False, "code": "internal_helper_error"},
                request_id=request_id,
                sign=True,
            )
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.server.inflight.release()


def run_server(config: ComputeHelperServiceConfig) -> None:
    """Run the standalone helper until its supervising platform stops it."""

    config.temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = ComputeHelperHTTPServer(config)
    print(
        "Royells compute helper ready "
        f"host={config.host} port={server.server_address[1]} "
        f"max_bytes={config.limits.max_input_bytes} max_inflight={config.max_inflight}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main() -> int:
    """CLI entrypoint; fail closed when the helper-only secret is absent."""

    try:
        run_server(ComputeHelperServiceConfig.from_environment())
    except (OSError, ValueError) as exc:
        print(f"Royells compute helper configuration failed: {type(exc).__name__}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
