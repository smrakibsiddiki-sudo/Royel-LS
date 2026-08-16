"""Local temporary storage adapter."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..errors import StorageError
from ..interfaces import StorageInterface


class LocalStorageAdapter(StorageInterface):
    """Filesystem adapter constrained to configured data and temporary roots."""

    def __init__(
        self,
        data_root: str | os.PathLike[str],
        temp_root: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.temp_root = Path(temp_root or self.data_root / "tmp").expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def _resolve_owned(self, path: str | os.PathLike[str]) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not (
            resolved == self.data_root
            or resolved.is_relative_to(self.data_root)
            or resolved == self.temp_root
            or resolved.is_relative_to(self.temp_root)
        ):
            raise StorageError(f"Path is outside configured storage roots: {resolved}")
        return resolved

    def download(
        self,
        source: str | os.PathLike[str],
        destination: Optional[str | os.PathLike[str]] = None,
    ) -> Path:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise StorageError(f"Download source is missing: {source_path}")
        destination_path = (
            self._resolve_owned(destination)
            if destination is not None
            else self.temp_path(suffix=source_path.suffix)
        )
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            return destination_path
        except OSError as exc:
            raise StorageError("Local download/copy failed") from exc

    def upload(
        self,
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> Path:
        source_path = self._resolve_owned(source)
        if not source_path.is_file():
            raise StorageError(f"Upload source is missing: {source_path}")
        destination_path = self._resolve_owned(destination)
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            return destination_path
        except OSError as exc:
            raise StorageError("Local upload/copy failed") from exc

    def delete(self, path: str | os.PathLike[str]) -> None:
        target = self._resolve_owned(path)
        try:
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Local delete failed: {target}") from exc

    def exists(self, path: str | os.PathLike[str]) -> bool:
        return self._resolve_owned(path).exists()

    def size(self, path: str | os.PathLike[str]) -> int:
        target = self._resolve_owned(path)
        try:
            return int(target.stat().st_size)
        except OSError as exc:
            raise StorageError(f"Local size failed: {target}") from exc

    def hash(self, path: str | os.PathLike[str], algorithm: str = "sha256") -> str:
        target = self._resolve_owned(path)
        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            raise StorageError(f"Unsupported hash algorithm: {algorithm}") from exc
        try:
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError as exc:
            raise StorageError(f"Local hash failed: {target}") from exc

    def cleanup(
        self,
        path: Optional[str | os.PathLike[str]] = None,
        *,
        older_than_seconds: Optional[float] = None,
    ) -> int:
        root = self._resolve_owned(path or self.temp_root)
        cutoff = (
            None
            if older_than_seconds is None
            else time.time() - max(0.0, float(older_than_seconds))
        )
        removed = 0
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        for candidate in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
            if candidate.is_dir():
                with contextlib.suppress(OSError):
                    candidate.rmdir()
                continue
            try:
                if cutoff is not None and candidate.stat().st_mtime > cutoff:
                    continue
                candidate.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StorageError(f"Local cleanup failed: {candidate}") from exc
        return removed

    def temp_path(self, suffix: str = "", prefix: str = "royells-") -> Path:
        try:
            descriptor, name = tempfile.mkstemp(
                suffix=str(suffix),
                prefix=str(prefix),
                dir=str(self.temp_root),
            )
            os.close(descriptor)
            path = Path(name)
            path.unlink()
            return path
        except OSError as exc:
            raise StorageError("Temporary path allocation failed") from exc
