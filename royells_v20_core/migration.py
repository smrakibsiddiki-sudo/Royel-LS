"""Atomic migration journal and compatibility coordinators."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .errors import MigrationError
from .interfaces import MigrationJournalInterface
from .models import ValidationMismatch
from .adapters.json_runtime import JsonRuntimeAdapter


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JsonMigrationJournal(MigrationJournalInterface):
    """Checksummed, previous-generation migration journal for legacy mode."""

    STATE_NAME = "migration_journal"

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        destination = Path(path).expanduser()
        root = destination.parent / ".v20_state"
        self._state = JsonRuntimeAdapter(root, fsync=fsync)
        self._lock = threading.RLock()

    def _load_all(self) -> dict[str, Any]:
        document = self._state.restore(self.STATE_NAME)
        if document is None:
            return {"schema_version": 1, "updated_at": "", "entries": {}}
        if not isinstance(document, dict) or not isinstance(
            document.get("entries"), dict
        ):
            raise MigrationError("Migration journal payload is invalid")
        return document

    def _save_all(self, document: Mapping[str, Any]) -> None:
        payload = dict(document)
        payload["updated_at"] = _utc_now()
        self._state.save(self.STATE_NAME, payload)

    def begin(
        self, operation: str, metadata: Optional[Mapping[str, Any]] = None
    ) -> str:
        journal_id = f"migration_{uuid.uuid4().hex}"
        with self._lock:
            document = self._load_all()
            document["entries"][journal_id] = {
                "journal_id": journal_id,
                "operation": str(operation),
                "status": "running",
                "cursor": "",
                "counts": {},
                "metadata": dict(metadata or {}),
                "mismatches": [],
                "started_at": _utc_now(),
                "updated_at": _utc_now(),
                "finished_at": "",
            }
            self._save_all(document)
        return journal_id

    def checkpoint(
        self,
        journal_id: str,
        *,
        cursor: str,
        counts: Optional[Mapping[str, int]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self._lock:
            document = self._load_all()
            entry = document["entries"].get(str(journal_id))
            if not entry:
                raise MigrationError(f"Unknown migration journal: {journal_id}")
            entry["cursor"] = str(cursor)
            entry["counts"] = {
                str(key): int(value) for key, value in (counts or {}).items()
            }
            entry["metadata"].update(dict(metadata or {}))
            entry["updated_at"] = _utc_now()
            self._save_all(document)

    def mismatch(self, journal_id: str, mismatch: ValidationMismatch) -> None:
        with self._lock:
            document = self._load_all()
            entry = document["entries"].get(str(journal_id))
            if not entry:
                raise MigrationError(f"Unknown migration journal: {journal_id}")
            item = asdict(mismatch)
            item["detected_at"] = item.get("detected_at") or _utc_now()
            entry["mismatches"].append(item)
            entry["updated_at"] = _utc_now()
            self._save_all(document)

    def complete(
        self,
        journal_id: str,
        *,
        status: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        normalized = str(status).strip().lower()
        if normalized not in {"succeeded", "failed", "rolled_back"}:
            raise MigrationError(f"Invalid terminal migration status: {status}")
        with self._lock:
            document = self._load_all()
            entry = document["entries"].get(str(journal_id))
            if not entry:
                raise MigrationError(f"Unknown migration journal: {journal_id}")
            entry["status"] = normalized
            entry["metadata"].update(dict(metadata or {}))
            entry["updated_at"] = _utc_now()
            entry["finished_at"] = _utc_now()
            self._save_all(document)

    def load(self, journal_id: str) -> Optional[Mapping[str, Any]]:
        with self._lock:
            entry = self._load_all()["entries"].get(str(journal_id))
            return None if entry is None else json.loads(json.dumps(entry))

    def list_open(self) -> list[Mapping[str, Any]]:
        with self._lock:
            entries = self._load_all()["entries"].values()
            return [
                json.loads(json.dumps(entry))
                for entry in entries
                if entry.get("status") == "running"
            ]


def canonical_hash(value: Any) -> str:
    """Return the deterministic hash used by dual-read validation."""

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DualWriteCoordinator:
    """Execute primary and shadow mutations with durable mismatch evidence."""

    def __init__(
        self,
        journal: MigrationJournalInterface,
        *,
        enabled: bool = False,
    ) -> None:
        self.journal = journal
        self.enabled = bool(enabled)

    def execute(
        self,
        operation: str,
        primary: Callable[[], Any],
        shadow: Optional[Callable[[], Any]] = None,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        result = primary()
        if not self.enabled or shadow is None:
            return result
        journal_id = self.journal.begin(operation, metadata)
        try:
            shadow_result = shadow()
            if canonical_hash(result) != canonical_hash(shadow_result):
                self.journal.mismatch(
                    journal_id,
                    ValidationMismatch(
                        domain="write",
                        record_key=operation,
                        mismatch_type="result_hash",
                        legacy_hash=canonical_hash(result),
                        candidate_hash=canonical_hash(shadow_result),
                    ),
                )
            self.journal.complete(journal_id, status="succeeded")
        except Exception as exc:
            self.journal.complete(
                journal_id,
                status="failed",
                metadata={"error": f"{type(exc).__name__}: {exc}"[:1000]},
            )
        return result
