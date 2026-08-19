"""Bounded, local-only media inspection helper.

This module is deliberately independent from the Telegram bot.  It accepts
only bytes or a local regular-file path, never reads environment variables,
never opens a socket, and has no concept of bot tokens, sessions, queues, or
remote URLs.  It is suitable for an optional CPU/I/O helper process, provided
the caller supplies an explicit ``allowed_roots`` allow-list.

The MP4 inspection is advisory.  A bounded probe that cannot find a ``moov``
box is reported as limited/unknown; it must *not* be interpreted as proof that
the media is invalid.  This prevents a helper capacity limit from turning a
recoverable Telegram media item into a permanent failure.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Union


PathInput = Union[str, os.PathLike[str]]
ByteInput = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True)
class HelperLimits:
    """Hard resource limits for one inspection request.

    The defaults permit normal media files while ensuring that a malformed
    input cannot cause unbounded hashing, reads, MP4 recursion, or box scans.
    Deployments with a smaller worker budget should pass stricter limits.
    """

    max_input_bytes: int = 2 * 1024 * 1024 * 1024
    hash_chunk_bytes: int = 1024 * 1024
    mp4_probe_bytes: int = 4 * 1024 * 1024
    mp4_max_box_bytes: int = 2 * 1024 * 1024
    max_mp4_boxes: int = 512
    max_mp4_depth: int = 12
    max_path_characters: int = 4096

    def __post_init__(self) -> None:
        positive_values = (
            self.max_input_bytes,
            self.hash_chunk_bytes,
            self.mp4_probe_bytes,
            self.mp4_max_box_bytes,
            self.max_mp4_boxes,
            self.max_mp4_depth,
            self.max_path_characters,
        )
        if any(not isinstance(value, int) or value <= 0 for value in positive_values):
            raise ValueError("all helper limits must be positive integers")
        if self.hash_chunk_bytes > self.max_input_bytes:
            raise ValueError("hash_chunk_bytes cannot exceed max_input_bytes")
        if self.mp4_max_box_bytes > self.mp4_probe_bytes:
            raise ValueError("mp4_max_box_bytes cannot exceed mp4_probe_bytes")


DEFAULT_LIMITS = HelperLimits()


@dataclass(frozen=True)
class FileMetadata:
    """Safe, path-free metadata about the submitted payload."""

    source_kind: str
    display_name: Optional[str]
    suffix: str
    size_bytes: int


@dataclass(frozen=True)
class Mp4Inspection:
    """Best-effort, bounded MP4 container metadata.

    ``inspection_complete=False`` means the helper did not have enough bounded
    data to make a complete metadata observation.  It does not mean the media
    is invalid.
    """

    recognized: bool
    inspection_complete: bool
    ftyp_major_brand: Optional[str]
    moov_found: bool
    moov_parsed: bool
    moov_at_end: Optional[bool]
    duration_seconds: Optional[float]
    video_dimensions: tuple[tuple[int, int], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ComputeResult:
    """Structured, JSON-serializable result for a local inspection request.

    ``status`` is one of ``ok``, ``limited``, ``rejected``, or ``error``.
    ``limited`` is deliberately non-terminal: callers should retain the media
    and retry/validate it later rather than classifying it as invalid.
    """

    status: str
    code: str
    metadata: Optional[FileMetadata] = None
    sha256: Optional[str] = None
    mp4: Optional[Mp4Inspection] = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return true only when all requested work completed within bounds."""

        return self.status == "ok"

    def to_dict(self) -> dict:
        """Return primitives suitable for JSON encoding or IPC."""

        payload = asdict(self)
        # ``asdict`` preserves tuples, which JSON encoders handle differently;
        # use lists so the IPC contract is deterministic for every caller.
        if self.mp4 is not None:
            payload["mp4"]["video_dimensions"] = [
                [width, height] for width, height in self.mp4.video_dimensions
            ]
        payload["warnings"] = list(self.warnings)
        if self.mp4 is not None:
            payload["mp4"]["warnings"] = list(self.mp4.warnings)
        return payload


def inspect_bytes(
    payload: ByteInput,
    *,
    filename: Optional[str] = None,
    include_sha256: bool = True,
    inspect_mp4: bool = True,
    limits: HelperLimits = DEFAULT_LIMITS,
) -> ComputeResult:
    """Inspect an in-memory payload without performing any network I/O.

    Args:
        payload: A contiguous bytes-like object.  Objects that require an
            implicit copy are rejected instead of being copied without bounds.
        filename: Optional display-only name; it is never used as a path.
        include_sha256: Hash the whole bounded input in fixed-size chunks.
        inspect_mp4: Run the lightweight, advisory MP4 metadata parser.
        limits: Explicit resource limits for this single request.

    Returns:
        A :class:`ComputeResult`; malformed/untrusted input is represented as
        a result, rather than raising an exception into a queue worker.
    """

    try:
        view = _as_contiguous_byte_view(payload)
    except (TypeError, ValueError):
        return _result("rejected", "unsupported_byte_buffer")

    display_name = _safe_display_name(filename, limits)
    if display_name is _INVALID_NAME:
        return _result("rejected", "invalid_display_name")

    metadata = FileMetadata(
        source_kind="bytes",
        display_name=display_name,
        suffix=_suffix_from_name(display_name),
        size_bytes=view.nbytes,
    )
    if view.nbytes > limits.max_input_bytes:
        return _result("limited", "input_too_large", metadata=metadata)

    digest = _hash_byte_view(view, limits.hash_chunk_bytes) if include_sha256 else None
    mp4 = None
    if inspect_mp4:
        mp4 = _inspect_mp4(view.nbytes, lambda offset, count: bytes(view[offset : offset + count]), limits)
    return _completed_result(metadata, digest, mp4)


def inspect_local_file(
    path: PathInput,
    *,
    allowed_roots: Optional[Union[PathInput, Sequence[PathInput]]] = None,
    include_sha256: bool = True,
    inspect_mp4: bool = True,
    limits: HelperLimits = DEFAULT_LIMITS,
) -> ComputeResult:
    """Inspect one local regular file through a race-aware, bounded reader.

    ``allowed_roots`` should be supplied by every service deployment.  A path
    outside those resolved roots is rejected before it is opened.  Symlinks,
    directories, devices, and file changes detected during the read are also
    rejected.  No absolute path is returned in the result.
    """

    candidate, error_code = _resolve_local_path(path, allowed_roots, limits)
    if error_code is not None:
        return _result("rejected", error_code)
    assert candidate is not None

    try:
        before = candidate.lstat()
    except FileNotFoundError:
        return _result("rejected", "file_not_found")
    except OSError:
        return _result("error", "file_stat_failed")

    if stat.S_ISLNK(before.st_mode):
        return _result("rejected", "symlink_not_allowed")
    if not stat.S_ISREG(before.st_mode):
        return _result("rejected", "regular_file_required")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    # O_NOFOLLOW is not present on every supported platform.  The lstat/fstat
    # identity comparison below remains active on those platforms.
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(candidate), flags)
    except FileNotFoundError:
        return _result("rejected", "file_not_found")
    except OSError:
        return _result("rejected", "file_open_failed")

    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return _result("rejected", "regular_file_required")
            if not _same_file_identity(before, opened):
                return _result("rejected", "file_changed_before_open")

            metadata = FileMetadata(
                source_kind="local_file",
                display_name=candidate.name,
                suffix=_suffix_from_name(candidate.name),
                size_bytes=opened.st_size,
            )
            if opened.st_size > limits.max_input_bytes:
                return _result("limited", "input_too_large", metadata=metadata)

            digest = _hash_stream(stream, limits.hash_chunk_bytes, opened.st_size) if include_sha256 else None
            if digest is _READ_CHANGED:
                return _result("rejected", "file_changed_during_read", metadata=metadata)

            mp4 = None
            if inspect_mp4:
                mp4 = _inspect_mp4(
                    opened.st_size,
                    lambda offset, count: _read_stream_range(stream, offset, count),
                    limits,
                )

            after = os.fstat(stream.fileno())
            if not _same_file_snapshot(opened, after):
                return _result("rejected", "file_changed_during_read", metadata=metadata)
            return _completed_result(metadata, digest, mp4)
    except OSError:
        return _result("error", "file_read_failed")


def _completed_result(
    metadata: FileMetadata,
    digest: Optional[str],
    mp4: Optional[Mp4Inspection],
) -> ComputeResult:
    warnings: tuple[str, ...] = ()
    if mp4 is not None and mp4.warnings:
        warnings = tuple(f"mp4:{warning}" for warning in mp4.warnings)
    if mp4 is not None and mp4.recognized and not mp4.inspection_complete:
        return _result("limited", "mp4_probe_limited", metadata, digest, mp4, warnings)
    return _result("ok", "ok", metadata, digest, mp4, warnings)


def _result(
    status: str,
    code: str,
    metadata: Optional[FileMetadata] = None,
    sha256: Optional[str] = None,
    mp4: Optional[Mp4Inspection] = None,
    warnings: Iterable[str] = (),
) -> ComputeResult:
    return ComputeResult(
        status=status,
        code=code,
        metadata=metadata,
        sha256=sha256,
        mp4=mp4,
        warnings=tuple(warnings),
    )


_INVALID_NAME = object()
_READ_CHANGED = object()


def _safe_display_name(filename: Optional[str], limits: HelperLimits) -> Optional[str] | object:
    if filename is None:
        return None
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        return _INVALID_NAME
    if len(filename) > limits.max_path_characters:
        return _INVALID_NAME
    # This is display-only.  Strip any supplied directory so callers cannot
    # accidentally leak a host path into an IPC response.
    return Path(filename).name


def _suffix_from_name(name: Optional[str]) -> str:
    return Path(name).suffix.lower() if name else ""


def _as_contiguous_byte_view(payload: ByteInput) -> memoryview:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    view = memoryview(payload)
    if not view.c_contiguous:
        raise ValueError("payload must be C-contiguous")
    return view.cast("B")


def _resolve_local_path(
    path: PathInput,
    allowed_roots: Optional[Union[PathInput, Sequence[PathInput]]],
    limits: HelperLimits,
) -> tuple[Optional[Path], Optional[str]]:
    if not isinstance(path, (str, os.PathLike)):
        return None, "invalid_path"
    raw_path = os.fspath(path)
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None, "invalid_path"
    if len(raw_path) > limits.max_path_characters:
        return None, "invalid_path"

    try:
        requested = Path(raw_path)
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        # Check lexical path components before resolve() removes evidence of a
        # symlink.  The later lstat/fstat comparison closes the remaining
        # time-of-check/time-of-use window around the final open.
        requested = Path(os.path.abspath(os.fspath(requested)))
        symlink_error = _reject_symlink_components(requested)
        if symlink_error is not None:
            return None, symlink_error
        candidate = requested.resolve(strict=True)
    except FileNotFoundError:
        return None, "file_not_found"
    except (OSError, RuntimeError):
        return None, "invalid_path"

    roots, root_error = _resolve_allowed_roots(allowed_roots, limits)
    if root_error is not None:
        return None, root_error
    if roots and not any(_is_within(candidate, root) for root in roots):
        return None, "path_outside_allowed_roots"
    return candidate, None


def _reject_symlink_components(path: Path) -> Optional[str]:
    """Reject a symlink in any existing component of an absolute path."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "file_not_found"
        except OSError:
            return "invalid_path"
        if stat.S_ISLNK(info.st_mode):
            return "symlink_not_allowed"
    return None


def _resolve_allowed_roots(
    allowed_roots: Optional[Union[PathInput, Sequence[PathInput]]],
    limits: HelperLimits,
) -> tuple[tuple[Path, ...], Optional[str]]:
    if allowed_roots is None:
        return (), None
    if isinstance(allowed_roots, (str, os.PathLike)):
        root_values: Sequence[PathInput] = (allowed_roots,)
    else:
        root_values = allowed_roots
    if not root_values:
        return (), "invalid_allowed_roots"

    resolved: list[Path] = []
    for root in root_values:
        if not isinstance(root, (str, os.PathLike)):
            return (), "invalid_allowed_roots"
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root or "\x00" in raw_root:
            return (), "invalid_allowed_roots"
        if len(raw_root) > limits.max_path_characters:
            return (), "invalid_allowed_roots"
        try:
            root_path = Path(raw_root).resolve(strict=True)
        except (OSError, RuntimeError):
            return (), "invalid_allowed_roots"
        if not root_path.is_dir():
            return (), "invalid_allowed_roots"
        resolved.append(root_path)
    return tuple(resolved), None


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_file_identity(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _hash_byte_view(view: memoryview, chunk_size: int) -> str:
    digest = hashlib.sha256()
    for offset in range(0, view.nbytes, chunk_size):
        digest.update(view[offset : offset + chunk_size])
    return digest.hexdigest()


def _hash_stream(stream, chunk_size: int, expected_size: int) -> str | object:
    stream.seek(0)
    digest = hashlib.sha256()
    read_total = 0
    while True:
        block = stream.read(chunk_size)
        if not block:
            break
        read_total += len(block)
        if read_total > expected_size:
            return _READ_CHANGED
        digest.update(block)
    if read_total != expected_size:
        return _READ_CHANGED
    return digest.hexdigest()


def _read_stream_range(stream, offset: int, count: int) -> bytes:
    stream.seek(offset)
    return stream.read(count)


@dataclass
class _Mp4State:
    box_count: int = 0
    warnings: list[str] = None  # type: ignore[assignment]
    duration_seconds: Optional[float] = None
    video_dimensions: list[tuple[int, int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []
        if self.video_dimensions is None:
            self.video_dimensions = []

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)


def _inspect_mp4(
    size_bytes: int,
    read_range: Callable[[int, int], bytes],
    limits: HelperLimits,
) -> Mp4Inspection:
    """Inspect only a bounded head/tail window of an MP4-like byte stream."""

    if size_bytes < 8:
        return Mp4Inspection(False, True, None, False, False, None, None, (), ())

    head_size, tail_size = _probe_window_sizes(size_bytes, limits.mp4_probe_bytes)
    try:
        head = read_range(0, head_size)
        tail_offset = size_bytes - tail_size
        tail = head if tail_offset == 0 else read_range(tail_offset, tail_size)
    except (OSError, ValueError):
        return Mp4Inspection(False, False, None, False, False, None, None, (), ("probe_read_failed",))

    if len(head) != head_size or len(tail) != tail_size:
        return Mp4Inspection(False, False, None, False, False, None, None, (), ("probe_read_truncated",))

    ftyp_brand, head_moov, head_partial = _scan_head_top_level(head, size_bytes, limits)
    recognized = ftyp_brand is not None
    moov_bytes = head_moov
    moov_found = head_moov is not None
    moov_at_end: Optional[bool] = None
    state = _Mp4State()
    if head_partial:
        state.warn("head_probe_ended_before_next_box")

    if moov_bytes is None and tail_offset > 0:
        tail_moov, tail_at_end, tail_warning = _find_tail_moov(tail, tail_offset, size_bytes, limits)
        if tail_warning is not None:
            state.warn(tail_warning)
        if tail_moov is not None:
            moov_found = True
            moov_bytes = tail_moov
            moov_at_end = tail_at_end

    moov_parsed = False
    if moov_bytes is not None:
        if len(moov_bytes) > limits.mp4_max_box_bytes:
            state.warn("moov_box_exceeds_parser_limit")
        else:
            moov_parsed = _parse_moov(moov_bytes, state, limits)
            if moov_at_end is None:
                moov_at_end = len(moov_bytes) == size_bytes
    elif recognized:
        state.warn("moov_not_found_in_bounded_probe")

    inspection_complete = not recognized or (moov_parsed and not state.warnings)
    dimensions = tuple(_deduplicate_dimensions(state.video_dimensions))
    return Mp4Inspection(
        recognized=recognized,
        inspection_complete=inspection_complete,
        ftyp_major_brand=ftyp_brand,
        moov_found=moov_found,
        moov_parsed=moov_parsed,
        moov_at_end=moov_at_end,
        duration_seconds=state.duration_seconds,
        video_dimensions=dimensions,
        warnings=tuple(state.warnings),
    )


def _probe_window_sizes(size_bytes: int, maximum: int) -> tuple[int, int]:
    if size_bytes <= maximum:
        return size_bytes, size_bytes
    head_size = maximum // 2
    return head_size, maximum - head_size


def _scan_head_top_level(
    head: bytes,
    total_size: int,
    limits: HelperLimits,
) -> tuple[Optional[str], Optional[bytes], bool]:
    """Parse complete top-level boxes from byte zero only.

    A top-level media box may correctly run beyond a bounded head window, so
    the third return value means "more data exists", not malformed input.
    """

    position = 0
    major_brand: Optional[str] = None
    moov: Optional[bytes] = None
    partial = False
    boxes = 0
    while position + 8 <= len(head) and boxes < limits.max_mp4_boxes:
        header = _box_header(head, position, len(head))
        if header is None:
            partial = True
            break
        box_size, header_size, box_type = header
        if box_size < header_size or position + box_size > total_size:
            break
        end = position + box_size
        boxes += 1
        if end > len(head):
            partial = True
            break
        if box_type == b"ftyp" and box_size >= header_size + 4:
            major_brand = _safe_brand(head[position + header_size : position + header_size + 4])
        elif box_type == b"moov":
            moov = head[position:end]
            break
        position = end
    return major_brand, moov, partial


def _find_tail_moov(
    tail: bytes,
    tail_offset: int,
    total_size: int,
    limits: HelperLimits,
) -> tuple[Optional[bytes], Optional[bool], Optional[str]]:
    """Locate a complete ``moov`` box in the bounded tail without trusting it."""

    candidate_warning: Optional[str] = None
    max_start = max(0, len(tail) - 8)
    for type_offset in range(4, max_start + 1):
        if tail[type_offset : type_offset + 4] != b"moov":
            continue
        start = type_offset - 4
        header = _box_header(tail, start, len(tail))
        if header is None:
            candidate_warning = "truncated_moov_header"
            continue
        box_size, header_size, box_type = header
        if box_type != b"moov" or box_size < header_size:
            continue
        absolute_end = tail_offset + start + box_size
        if absolute_end > total_size:
            candidate_warning = "malformed_moov_size"
            continue
        if box_size > limits.mp4_max_box_bytes:
            return None, None, "moov_box_exceeds_parser_limit"
        if start + box_size > len(tail):
            candidate_warning = "moov_outside_bounded_tail_probe"
            continue
        return tail[start : start + box_size], absolute_end == total_size, None
    return None, None, candidate_warning


def _box_header(data: bytes, offset: int, end: int) -> Optional[tuple[int, int, bytes]]:
    if offset < 0 or offset + 8 > end:
        return None
    size = int.from_bytes(data[offset : offset + 4], "big")
    box_type = data[offset + 4 : offset + 8]
    if size == 1:
        if offset + 16 > end:
            return None
        return int.from_bytes(data[offset + 8 : offset + 16], "big"), 16, box_type
    if size == 0:
        return end - offset, 8, box_type
    return size, 8, box_type


def _parse_moov(moov: bytes, state: _Mp4State, limits: HelperLimits) -> bool:
    header = _box_header(moov, 0, len(moov))
    if header is None or header[2] != b"moov" or header[0] != len(moov):
        state.warn("malformed_moov_box")
        return False
    _, header_size, _ = header
    return _parse_moov_children(moov, header_size, len(moov), state, limits, 0)


def _parse_moov_children(
    data: bytes,
    start: int,
    end: int,
    state: _Mp4State,
    limits: HelperLimits,
    depth: int,
) -> bool:
    if depth > limits.max_mp4_depth:
        state.warn("mp4_nesting_limit_reached")
        return False
    complete = True
    position = start
    while position < end:
        if state.box_count >= limits.max_mp4_boxes:
            state.warn("mp4_box_count_limit_reached")
            return False
        header = _box_header(data, position, end)
        if header is None:
            state.warn("truncated_mp4_box_header")
            return False
        box_size, header_size, box_type = header
        if box_size < header_size or position + box_size > end:
            state.warn("malformed_mp4_box_size")
            return False
        state.box_count += 1
        payload_start = position + header_size
        box_end = position + box_size
        if box_type == b"mvhd":
            _parse_mvhd(data[payload_start:box_end], state)
        elif box_type == b"trak":
            if not _parse_trak(data, payload_start, box_end, state, limits, depth + 1):
                complete = False
        position = box_end
    return complete


def _parse_trak(
    data: bytes,
    start: int,
    end: int,
    state: _Mp4State,
    limits: HelperLimits,
    depth: int,
) -> bool:
    if depth > limits.max_mp4_depth:
        state.warn("mp4_nesting_limit_reached")
        return False
    position = start
    dimensions: Optional[tuple[int, int]] = None
    handler_type: Optional[bytes] = None
    complete = True
    while position < end:
        if state.box_count >= limits.max_mp4_boxes:
            state.warn("mp4_box_count_limit_reached")
            return False
        header = _box_header(data, position, end)
        if header is None:
            state.warn("truncated_mp4_box_header")
            return False
        box_size, header_size, box_type = header
        if box_size < header_size or position + box_size > end:
            state.warn("malformed_mp4_box_size")
            return False
        state.box_count += 1
        payload_start = position + header_size
        box_end = position + box_size
        if box_type == b"tkhd":
            dimensions = _parse_tkhd(data[payload_start:box_end], state)
        elif box_type == b"mdia":
            parsed_handler, parsed_complete = _parse_mdia_handler(
                data, payload_start, box_end, state, limits, depth + 1
            )
            handler_type = parsed_handler or handler_type
            complete = complete and parsed_complete
        position = box_end
    if handler_type == b"vide" and dimensions is not None:
        state.video_dimensions.append(dimensions)
    return complete


def _parse_mdia_handler(
    data: bytes,
    start: int,
    end: int,
    state: _Mp4State,
    limits: HelperLimits,
    depth: int,
) -> tuple[Optional[bytes], bool]:
    if depth > limits.max_mp4_depth:
        state.warn("mp4_nesting_limit_reached")
        return None, False
    position = start
    handler_type: Optional[bytes] = None
    while position < end:
        if state.box_count >= limits.max_mp4_boxes:
            state.warn("mp4_box_count_limit_reached")
            return handler_type, False
        header = _box_header(data, position, end)
        if header is None:
            state.warn("truncated_mp4_box_header")
            return handler_type, False
        box_size, header_size, box_type = header
        if box_size < header_size or position + box_size > end:
            state.warn("malformed_mp4_box_size")
            return handler_type, False
        state.box_count += 1
        if box_type == b"hdlr":
            payload_start = position + header_size
            handler_type = _parse_hdlr(data[payload_start : position + box_size], state) or handler_type
        position += box_size
    return handler_type, True


def _parse_mvhd(payload: bytes, state: _Mp4State) -> None:
    if len(payload) < 20:
        state.warn("truncated_mvhd")
        return
    version = payload[0]
    if version == 0:
        if len(payload) < 20:
            state.warn("truncated_mvhd")
            return
        timescale = int.from_bytes(payload[12:16], "big")
        duration = int.from_bytes(payload[16:20], "big")
    elif version == 1:
        if len(payload) < 32:
            state.warn("truncated_mvhd")
            return
        timescale = int.from_bytes(payload[20:24], "big")
        duration = int.from_bytes(payload[24:32], "big")
    else:
        state.warn("unsupported_mvhd_version")
        return
    if timescale <= 0:
        state.warn("invalid_mvhd_timescale")
        return
    state.duration_seconds = duration / timescale


def _parse_tkhd(payload: bytes, state: _Mp4State) -> Optional[tuple[int, int]]:
    if not payload:
        state.warn("truncated_tkhd")
        return None
    version = payload[0]
    offsets = {0: (76, 80), 1: (88, 92)}
    positions = offsets.get(version)
    if positions is None or len(payload) < positions[1] + 4:
        state.warn("truncated_tkhd")
        return None
    width = int.from_bytes(payload[positions[0] : positions[0] + 4], "big") >> 16
    height = int.from_bytes(payload[positions[1] : positions[1] + 4], "big") >> 16
    if width <= 0 or height <= 0:
        return None
    return width, height


def _parse_hdlr(payload: bytes, state: _Mp4State) -> Optional[bytes]:
    # FullBox (4) + pre_defined (4) + handler_type (4).
    if len(payload) < 12:
        state.warn("truncated_hdlr")
        return None
    return payload[8:12]


def _safe_brand(value: bytes) -> Optional[str]:
    if len(value) != 4:
        return None
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    return decoded if decoded.isprintable() else None


def _deduplicate_dimensions(values: Iterable[tuple[int, int]]) -> Iterable[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            yield value


__all__ = [
    "ByteInput",
    "ComputeResult",
    "DEFAULT_LIMITS",
    "FileMetadata",
    "HelperLimits",
    "Mp4Inspection",
    "PathInput",
    "inspect_bytes",
    "inspect_local_file",
]
