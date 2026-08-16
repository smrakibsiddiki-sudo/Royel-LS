"""Atomic JSON implementation of RuntimeStateInterface."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ..errors import CheckpointError
from ..interfaces import RuntimeStateInterface


_STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JsonRuntimeAdapter(RuntimeStateInterface):
    """Versioned, checksummed, previous-good JSON state storage."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        schema_version: int = 1,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.schema_version = max(1, int(schema_version))
        self.fsync = bool(fsync)
        self._mutex = threading.RLock()
        self._revisions: dict[str, int] = {}
        self._locks: dict[str, tuple[str, threading.RLock]] = {}
        self._token_to_name: dict[str, str] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    def _validate_name(self, name: str) -> str:
        normalized = str(name or "").strip()
        if not _STATE_NAME.fullmatch(normalized):
            raise CheckpointError(f"Invalid runtime state name: {name!r}")
        return normalized

    def _path(self, name: str) -> Path:
        return self.root / f"{self._validate_name(name)}.json"

    def _previous_path(self, name: str) -> Path:
        return self.root / f"{self._validate_name(name)}.previous.json"

    @staticmethod
    def _canonical(payload: Any) -> bytes:
        try:
            return json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CheckpointError("Runtime payload is not JSON serializable") from exc

    def _envelope(self, name: str, payload: Any, revision: int) -> dict[str, Any]:
        body = {
            "name": name,
            "schema_version": self.schema_version,
            "revision": revision,
            "written_at": _utc_now(),
            "payload": deepcopy(payload),
        }
        body["sha256"] = hashlib.sha256(self._canonical(body["payload"])).hexdigest()
        return body

    def _fsync_directory(self) -> None:
        if not self.fsync or os.name == "nt":
            return
        descriptor = os.open(str(self.root), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, destination: Path, envelope: Mapping[str, Any]) -> None:
        temporary_path: Path | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=str(destination.parent),
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    envelope,
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
            if destination.exists():
                os.replace(destination, self._previous_path(destination.stem))
            os.replace(temporary_path, destination)
            temporary_path = None
            self._fsync_directory()
        except OSError as exc:
            raise CheckpointError(f"Atomic state write failed: {destination}") from exc
        finally:
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink()

    def _decode(self, path: Path) -> tuple[Any, int]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Runtime state read failed: {path}") from exc
        if not isinstance(document, dict) or "payload" not in document:
            # Legacy JSON remains readable during incremental migration.
            return document, 0
        expected = str(document.get("sha256") or "")
        actual = hashlib.sha256(self._canonical(document.get("payload"))).hexdigest()
        if not expected or not hmac.compare_digest(expected, actual):
            raise CheckpointError(f"Runtime state checksum mismatch: {path}")
        version = int(document.get("schema_version") or 0)
        if version < 1 or version > self.schema_version:
            raise CheckpointError(
                f"Unsupported runtime schema {version}; supported <= {self.schema_version}"
            )
        return document.get("payload"), int(document.get("revision") or 0)

    def load(self, name: str = "runtime_checkpoint") -> Any:
        normalized = self._validate_name(name)
        path = self._path(normalized)
        if not path.exists():
            return None
        payload, revision = self._decode(path)
        self._revisions[normalized] = max(self._revisions.get(normalized, 0), revision)
        return payload

    def save(self, name: str, payload: Any) -> int:
        normalized = self._validate_name(name)
        with self._mutex:
            current_revision = self._revisions.get(normalized, 0)
            if self._path(normalized).exists() and current_revision == 0:
                with contextlib.suppress(CheckpointError):
                    _, current_revision = self._decode(self._path(normalized))
            revision = current_revision + 1
            self._atomic_write(
                self._path(normalized),
                self._envelope(normalized, payload, revision),
            )
            self._revisions[normalized] = revision
            return revision

    def delete(self, name: str) -> None:
        normalized = self._validate_name(name)
        with self._mutex:
            for path in (self._path(normalized), self._previous_path(normalized)):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            self._revisions.pop(normalized, None)

    def checkpoint(self, payload: Any, name: str = "runtime_checkpoint") -> int:
        return self.save(name, payload)

    def restore(self, name: str = "runtime_checkpoint") -> Any:
        normalized = self._validate_name(name)
        errors: list[str] = []
        for path in (self._path(normalized), self._previous_path(normalized)):
            if not path.exists():
                continue
            try:
                payload, revision = self._decode(path)
                self._revisions[normalized] = max(
                    self._revisions.get(normalized, 0), revision
                )
                return payload
            except CheckpointError as exc:
                errors.append(str(exc))
        if errors:
            raise CheckpointError("; ".join(errors))
        return None

    def heartbeat(
        self, component: str, metadata: Optional[Mapping[str, Any]] = None
    ) -> int:
        normalized = self._validate_name(f"heartbeat.{component}")
        return self.save(
            normalized,
            {
                "component": str(component),
                "seen_at": _utc_now(),
                "metadata": dict(metadata or {}),
            },
        )

    def lock(self, name: str = "runtime") -> str:
        normalized = self._validate_name(name)
        with self._mutex:
            _, mutex = self._locks.setdefault(normalized, ("", threading.RLock()))
        mutex.acquire()
        token = uuid.uuid4().hex
        with self._mutex:
            self._locks[normalized] = (token, mutex)
            self._token_to_name[token] = normalized
        return token

    def unlock(self, token: str) -> None:
        with self._mutex:
            normalized = self._token_to_name.pop(str(token), None)
            if normalized is None:
                raise CheckpointError("Unknown runtime lock token")
            active_token, mutex = self._locks[normalized]
            if active_token != token:
                raise CheckpointError("Runtime lock ownership mismatch")
            self._locks[normalized] = ("", mutex)
        mutex.release()
