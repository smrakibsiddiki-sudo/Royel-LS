"""Production build manifest generation and validation for Royells."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFEST_FILENAME = "build_manifest.json"
MANIFEST_VERSION = 1
MINIMUM_PYTHON_VERSION = "3.11"
SUPPORTED_PLATFORMS = ["linux", "windows", "darwin"]
GENERATED_BY = "royells-build-manifest-v1"

DEFAULT_ENGINE_VERSIONS = {
    "worker": "21.0.0",
    "upload": "21.0.0",
    "download": "21.0.0",
    "duplicate": "21.0.0",
    "recovery": "21.0.0",
    "metrics": "1",
}

CRITICAL_FILES = {
    "app.py",
    "Dockerfile",
    "requirements.txt",
    "royells_media_bot_ready.py",
    "royells_v21_micro_workers.py",
    "royells_build_manifest.py",
    "royells_production_intelligence.py",
}
CRITICAL_PREFIXES = (
    "royells_v20_core/",
    "royells_v20_postgres/",
    "royells_v20_redis/",
    "migrations/postgres/",
)
EXCLUDED_DIRS = {
    ".git",
    ".agents",
    ".codex",
    "__pycache__",
    ".pytest_cache",
    "tests",
    "work_smoke_tmp",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip", ".tmp"}
EXCLUDED_FILES = {
    MANIFEST_FILENAME,
    "ROYELLS_V20_MANIFEST.sha256",
    "ROYELLS_V21_MANIFEST.sha256",
}

ENVIRONMENT_SPEC = [
    ("ROYELLS_API_ID", "required", "Telegram API ID"),
    ("ROYELLS_API_HASH", "required", "Telegram API hash"),
    ("ROYELLS_BOT_TOKEN", "required", "Telegram bot token"),
    ("ROYELLS_OWNER_ID", "required", "Owner Telegram user ID"),
    ("ROYELLS_TARGET_CHAT_ID", "required", "Target channel ID"),
    ("ROYELLS_USER_SESSION_STRING", "optional", "Userbot session string"),
    ("ROYELLS_DATA_DIR", "optional", "Persistent data directory"),
    ("ROYELLS_RUNTIME_DIR", "optional", "Runtime directory"),
    ("ROYELLS_SESSION_DIR", "optional", "Pyrogram session directory"),
    ("DATABASE_URL", "optional", "PostgreSQL adapter URL"),
    ("POSTGRES_URL", "optional", "PostgreSQL adapter URL alias"),
    ("REDIS_URL", "optional", "Redis adapter URL"),
    ("UPSTASH_URL", "optional", "Upstash REST URL"),
    ("UPSTASH_TOKEN", "optional", "Upstash REST token"),
    ("UPSTASH_REDIS_REST_URL", "optional", "Upstash REST URL alias"),
    ("UPSTASH_REDIS_REST_TOKEN", "optional", "Upstash REST token alias"),
    ("ROYELLS_GEMINI_API_KEY", "deprecated", "Removed AI hot-patching key"),
    ("GEMINI_API_KEY", "deprecated", "Removed AI hot-patching key"),
]


class BuildManifestError(RuntimeError):
    """Raised when the deployed build cannot be trusted."""


@dataclass
class ManifestValidationResult:
    status: str
    manifest_path: str
    build_id: str = ""
    build_number: str = ""
    version: str = ""
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    repaired: bool = False
    checked_files: int = 0
    critical_files: int = 0

    @property
    def ok(self) -> bool:
        return not self.fatal

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "manifest_path": self.manifest_path,
            "build_id": self.build_id,
            "build_number": self.build_number,
            "version": self.version,
            "fatal": list(self.fatal),
            "warnings": list(self.warnings),
            "repaired": bool(self.repaired),
            "checked_files": int(self.checked_files),
            "critical_files": int(self.critical_files),
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def normalize_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if path.name == "Dockerfile":
        return "dockerfile"
    if suffix == ".py":
        return "python"
    if suffix == ".sql":
        return "sql"
    if suffix in {".txt", ".in"}:
        return "requirements" if path.name.startswith("requirements") else "text"
    if suffix == ".md":
        return "documentation"
    if suffix == ".json":
        return "json"
    if suffix in {".sha256", ".sha"}:
        return "checksum"
    return suffix.lstrip(".") or "file"


def is_critical_file(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return normalized in CRITICAL_FILES or any(
        normalized.startswith(prefix) for prefix in CRITICAL_PREFIXES
    )


def production_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in relative_parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files, key=lambda item: normalize_rel(item, root).lower())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_inventory(root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in production_files(root):
        rel = normalize_rel(path, root)
        stat = path.stat()
        inventory.append(
            {
                "path": rel,
                "size": int(stat.st_size),
                "sha256": sha256_file(path),
                "mtime": int(stat.st_mtime),
                "last_modified": datetime.fromtimestamp(
                    stat.st_mtime,
                    timezone.utc,
                ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "type": file_type(path),
                "category": file_type(path),
                "critical": is_critical_file(rel),
            }
        )
    return inventory


def git_state(root: Path) -> dict[str, Any]:
    def run_git(args: Sequence[str]) -> str:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()

    try:
        commit = run_git(["rev-parse", "HEAD"])
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        dirty = bool(run_git(["status", "--porcelain"]))
        return {
            "available": True,
            "commit": commit,
            "short_commit": commit[:12],
            "branch": branch,
            "dirty": dirty,
            "state": "dirty" if dirty else "clean",
        }
    except Exception:
        return {
            "available": False,
            "commit": "",
            "short_commit": "",
            "branch": "",
            "dirty": False,
            "state": "not_git",
        }


def installed_packages() -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name") or distribution.metadata.get("Summary") or ""
        if not name:
            continue
        packages.append({"name": str(name), "version": str(distribution.version)})
    return sorted(packages, key=lambda item: item["name"].lower())


def latest_postgres_migration(root: Path) -> int:
    migrations_dir = root / "migrations" / "postgres"
    versions: list[int] = []
    if migrations_dir.is_dir():
        for path in migrations_dir.glob("*.sql"):
            prefix = path.name.split("_", 1)[0]
            if prefix.isdigit():
                versions.append(int(prefix))
    return max(versions or [0])


def runtime_profile_from_environment(env: Mapping[str, str]) -> str:
    """Return the canonical build/deployment profile without reading secret values."""

    raw_profile = str(env.get("ROYELLS_RUNTIME_PROFILE") or "").strip().lower().replace("_", "-")
    if parse_bool(env.get("ROYELLS_ORACLE_PROFILE_LOCK"), False) and raw_profile not in {
        "oracle-a1",
        "a1",
        "oracle-a1-flex",
    }:
        raw_profile = "oracle-e2-micro"
    if raw_profile in {"oracle-e2", "e2", "oracle-e2-micro", "oracle-e2.1-micro"}:
        return "oracle-e2-micro"
    if raw_profile in {"oracle-a1", "a1", "oracle-a1-flex"}:
        return "oracle-a1"
    if raw_profile in {"huggingface", "huggingface-space", "hf", "space"}:
        return "huggingface"
    if raw_profile:
        return raw_profile
    return "huggingface" if parse_bool(env.get("ROYELLS_HUGGINGFACE_SPACE"), False) else "local"


def build_environment(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    env = dict(os.environ if environ is None else environ)
    items: list[dict[str, Any]] = []
    for name, status, description in ENVIRONMENT_SPEC:
        value = env.get(name, "")
        missing = status == "required" and not str(value).strip()
        items.append(
            {
                "name": name,
                "status": status,
                "required": status == "required",
                "optional": status == "optional",
                "deprecated": status == "deprecated",
                "present": bool(str(value).strip()),
                "missing": bool(missing),
                "masked": bool(str(value).strip()),
                "description": description,
            }
        )
    return items


def feature_flags(environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    env = dict(os.environ if environ is None else environ)
    return {
        "Hybrid Download": True,
        "Micro Workers": parse_bool(env.get("ROYELLS_V21_MICRO_WORKERS"), True),
        "Checkpoint": parse_bool(env.get("ROYELLS_RUNTIME_CHECKPOINT"), True),
        "Queue Recovery": True,
        "Delivery Intents": True,
        "Startup Temp Cleanup": parse_bool(env.get("ROYELLS_V21_CLEANUP_STALE_TEMP_ON_BOOT"), True),
        "Retry Queue": True,
        "Recovery": True,
        "Startup Source Scan": True,
        "Historical Scan": parse_bool(env.get("ROYELLS_HISTORICAL_BACKFILL"), True),
        "Duplicate Protection": True,
        "Album Protection": True,
        "Manifest Engine": True,
        "Health Manifest": True,
        "Incident Engine": True,
        "Discord Adapter": parse_bool(env.get("ROYELLS_DISCORD_ADAPTER"), False),
        "Postgres Adapter": parse_bool(env.get("ENABLE_POSTGRES_ADAPTER"), False),
        "Redis Adapter": parse_bool(env.get("ENABLE_REDIS_ADAPTER"), False),
        "BotAPI Download": False,
        "Userbot Download": True,
        "SQLite": parse_bool(env.get("USE_SQLITE"), True),
        "JSON Runtime": parse_bool(env.get("USE_JSON_STATE"), True),
        "Dual Write": parse_bool(env.get("ENABLE_DUAL_WRITE"), False),
        "Rollback": parse_bool(env.get("ENABLE_ROLLBACK"), False),
        "Adaptive Intake": parse_bool(env.get("ROYELLS_ADAPTIVE_INTAKE"), True),
        "Historical Backfill": parse_bool(env.get("ROYELLS_HISTORICAL_BACKFILL"), True),
        "Target Media Index": parse_bool(env.get("ROYELLS_TARGET_MEDIA_INDEX"), True),
    }


def context_value(context: Mapping[str, Any], name: str, default: Any) -> Any:
    value = context.get(name, default)
    return default if value is None else value


def build_numbers(context: Mapping[str, Any], repository: Mapping[str, Any]) -> tuple[str, str]:
    build_number = str(
        context_value(
            context,
            "build_number",
            os.getenv("ROYELLS_BUILD_NUMBER", ""),
        )
        or ""
    ).strip()
    if not build_number:
        build_number = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    seed = "|".join(
        [
            str(context_value(context, "royells_version", "21.0.0")),
            build_number,
            str(repository.get("short_commit") or "nogit"),
            str(int(time.time())),
        ]
    )
    build_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return build_id, build_number


def generate_manifest(
    root: str | os.PathLike[str] = ".",
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    previous_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    ctx = dict(context or {})
    env = dict(os.environ if environ is None else environ)
    runtime_profile = runtime_profile_from_environment(env)
    container_profile = runtime_profile in {
        "huggingface",
        "oracle-e2-micro",
        "oracle-a1",
    }
    repository = git_state(root_path)
    build_id, build_number = build_numbers(ctx, repository)
    python_version = platform.python_version()
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "build_id": build_id,
        "build_uuid": build_id,
        "build_timestamp": utc_now_iso(),
        "royells_version": str(context_value(ctx, "royells_version", "21.0.0")),
        "build_number": build_number,
        "build_type": str(context_value(ctx, "build_type", env.get("ROYELLS_BUILD_TYPE", "production"))),
        "build_channel": str(context_value(ctx, "build_channel", env.get("ROYELLS_BUILD_CHANNEL", "stable"))),
        "git_commit": str(repository.get("commit") or ""),
        "git_branch": str(repository.get("branch") or ""),
        "python_version": python_version,
        "platform": platform.system().lower(),
        "operating_system": platform.system().lower(),
        "architecture": platform.machine(),
        "cpu_architecture": platform.machine(),
        "container_type": str(context_value(ctx, "container_type", "docker" if container_profile else "local")),
        "docker_image": str(context_value(ctx, "docker_image", env.get("ROYELLS_DOCKER_IMAGE", ""))),
        "deployment_target": str(context_value(ctx, "deployment_target", runtime_profile)),
        "repository_state": repository,
        "migration_epoch": str(context_value(ctx, "migration_epoch", env.get("ROYELLS_MIGRATION_EPOCH", "legacy-v1"))),
        "database_version": str(context_value(ctx, "database_version", "sqlite-v1")),
        "database_backend": "sqlite" if parse_bool(env.get("USE_SQLITE"), True) else "postgres",
        "queue_backend": str(context_value(ctx, "queue_backend", "legacy_composite")),
        "runtime_backend": str(context_value(ctx, "runtime_backend", "legacy_json")),
        "checkpoint_backend": str(context_value(ctx, "checkpoint_backend", "legacy_json")),
        "runtime_schema_version": int(context_value(ctx, "runtime_schema_version", 2)),
        "checkpoint_version": int(context_value(ctx, "checkpoint_version", 2)),
        "queue_version": int(context_value(ctx, "queue_version", 2)),
        "worker_engine_version": str(context_value(ctx, "worker_engine_version", DEFAULT_ENGINE_VERSIONS["worker"])),
        "upload_engine_version": str(context_value(ctx, "upload_engine_version", DEFAULT_ENGINE_VERSIONS["upload"])),
        "download_engine_version": str(context_value(ctx, "download_engine_version", DEFAULT_ENGINE_VERSIONS["download"])),
        "retry_engine_version": str(context_value(ctx, "retry_engine_version", "21.0.0")),
        "duplicate_engine_version": str(context_value(ctx, "duplicate_engine_version", DEFAULT_ENGINE_VERSIONS["duplicate"])),
        "recovery_engine_version": str(context_value(ctx, "recovery_engine_version", DEFAULT_ENGINE_VERSIONS["recovery"])),
        "dashboard_version": str(context_value(ctx, "dashboard_version", "21.0.0")),
        "metrics_version": str(context_value(ctx, "metrics_version", DEFAULT_ENGINE_VERSIONS["metrics"])),
        "compatibility_version": str(context_value(ctx, "compatibility_version", "v19-v21")),
        "minimum_supported_version": str(context_value(ctx, "minimum_supported_version", "19.0.0")),
        "feature_flags": feature_flags(env),
        "deployment_profile": str(
            context_value(
                ctx,
                "deployment_profile",
                runtime_profile,
            )
        ),
        "supported_platforms": list(SUPPORTED_PLATFORMS),
        "minimum_python_version": MINIMUM_PYTHON_VERSION,
        "generated_at": utc_now_iso(),
        "generated_by": GENERATED_BY,
        "file_inventory": file_inventory(root_path),
        "database_state": {
            "database_backend": "sqlite" if parse_bool(env.get("USE_SQLITE"), True) else "postgres",
            "sqlite_version": sqlite3.sqlite_version,
            "sqlite_schema_version": int(context_value(ctx, "sqlite_schema_version", 1)),
            "postgres_schema_version": int(context_value(ctx, "postgres_schema_version", latest_postgres_migration(root_path))),
            "migration_version": str(context_value(ctx, "migration_version", ctx.get("migration_epoch", "legacy-v1"))),
            "dual_write_state": parse_bool(env.get("ENABLE_DUAL_WRITE"), False),
            "dual_read_state": parse_bool(env.get("ENABLE_DUAL_READ"), False),
            "rollback_state": parse_bool(env.get("ENABLE_ROLLBACK"), False),
            "compatibility_state": "legacy-compatible",
        },
        "runtime_state": {
            "runtime_schema": int(context_value(ctx, "runtime_schema_version", 2)),
            "checkpoint_schema": int(context_value(ctx, "checkpoint_version", 2)),
            "delivery_intent_schema": int(context_value(ctx, "delivery_intent_schema_version", 1)),
            "queue_schema": int(context_value(ctx, "queue_version", 2)),
        },
        "environment": build_environment(env),
        "dependencies": {
            "python": python_version,
            "packages": installed_packages(),
            "system_libraries": ["ca-certificates"],
        },
        "rollback": {
            "current_build": build_id,
            "previous_build": (
                (previous_manifest or {}).get("build_id")
                or env.get("ROYELLS_PREVIOUS_BUILD_ID", "")
            ),
            "rollback_compatible": True,
            "migration_compatible": True,
            "compatible_migration_epochs": ["legacy-v1"],
        },
        "generated_artifact_exclusions": [MANIFEST_FILENAME],
    }
    return manifest


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise BuildManifestError("Build manifest root must be a JSON object")
    return payload


def write_manifest(
    path: str | os.PathLike[str] = MANIFEST_FILENAME,
    *,
    root: str | os.PathLike[str] = ".",
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    manifest_path = Path(path)
    previous: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            previous = load_manifest(manifest_path)
        except Exception:
            previous = {}
    manifest = generate_manifest(
        root=root,
        context=context,
        environ=environ,
        previous_manifest=previous,
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for item in str(value).split("."):
        if not item.isdigit():
            break
        parts.append(int(item))
    return tuple(parts or [0])


def current_context_defaults(context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ctx = dict(context or {})
    return {
        "royells_version": str(context_value(ctx, "royells_version", "21.0.0")),
        "migration_epoch": str(context_value(ctx, "migration_epoch", "legacy-v1")),
        "database_version": str(context_value(ctx, "database_version", "sqlite-v1")),
        "runtime_schema_version": int(context_value(ctx, "runtime_schema_version", 2)),
        "checkpoint_version": int(context_value(ctx, "checkpoint_version", 2)),
        "queue_version": int(context_value(ctx, "queue_version", 2)),
        "worker_engine_version": str(context_value(ctx, "worker_engine_version", "21.0.0")),
        "upload_engine_version": str(context_value(ctx, "upload_engine_version", "21.0.0")),
        "download_engine_version": str(context_value(ctx, "download_engine_version", "21.0.0")),
        "duplicate_engine_version": str(context_value(ctx, "duplicate_engine_version", "21.0.0")),
        "recovery_engine_version": str(context_value(ctx, "recovery_engine_version", "21.0.0")),
        "metrics_version": str(context_value(ctx, "metrics_version", "1")),
        "sqlite_schema_version": int(context_value(ctx, "sqlite_schema_version", 1)),
        "postgres_schema_version": int(context_value(ctx, "postgres_schema_version", 3)),
        "delivery_intent_schema_version": int(context_value(ctx, "delivery_intent_schema_version", 1)),
    }


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    root: str | os.PathLike[str] = ".",
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ManifestValidationResult:
    root_path = Path(root).resolve()
    manifest_path = root_path / MANIFEST_FILENAME
    ctx = current_context_defaults(context)
    env = dict(os.environ if environ is None else environ)
    result = ManifestValidationResult(
        status="validating",
        manifest_path=str(manifest_path),
        build_id=str(manifest.get("build_id") or ""),
        build_number=str(manifest.get("build_number") or ""),
        version=str(manifest.get("royells_version") or ""),
    )

    manifest_version = int(manifest.get("manifest_version") or 0)
    if manifest_version != MANIFEST_VERSION:
        result.fatal.append(
            f"Unsupported build manifest version {manifest_version}; expected {MANIFEST_VERSION}"
        )
    if version_tuple(platform.python_version()) < version_tuple(str(manifest.get("minimum_python_version") or MINIMUM_PYTHON_VERSION)):
        result.fatal.append(
            f"Python {platform.python_version()} is below required {manifest.get('minimum_python_version')}"
        )
    if platform.system().lower() not in set(manifest.get("supported_platforms") or SUPPORTED_PLATFORMS):
        result.fatal.append(f"Unsupported platform: {platform.system().lower()}")

    for item in manifest.get("environment") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("required") and not str(env.get(str(item.get("name") or ""), "")).strip():
            result.fatal.append(f"Missing required environment variable: {item.get('name')}")
        if item.get("deprecated") and str(env.get(str(item.get("name") or ""), "")).strip():
            result.warnings.append(f"Deprecated environment variable is still configured: {item.get('name')}")

    inventory = manifest.get("file_inventory") or []
    if not isinstance(inventory, list):
        result.fatal.append("Build manifest file_inventory must be a list")
        inventory = []
    seen_paths: set[str] = set()
    for record in inventory:
        if not isinstance(record, Mapping):
            result.fatal.append("Invalid file inventory record")
            continue
        relative_path = str(record.get("path") or "").replace("\\", "/")
        if not relative_path or relative_path.startswith("../") or "/../" in relative_path:
            result.fatal.append(f"Invalid file inventory path: {relative_path}")
            continue
        seen_paths.add(relative_path)
        file_path = root_path / relative_path
        critical = bool(record.get("critical"))
        result.checked_files += 1
        if critical:
            result.critical_files += 1
        if not file_path.is_file():
            message = f"Manifest file missing: {relative_path}"
            (result.fatal if critical else result.warnings).append(message)
            continue
        actual_size = file_path.stat().st_size
        expected_size = int(record.get("size") or -1)
        actual_sha = sha256_file(file_path)
        expected_sha = str(record.get("sha256") or "")
        if expected_size != actual_size or expected_sha != actual_sha:
            message = f"Checksum mismatch: {relative_path}"
            (result.fatal if critical else result.warnings).append(message)

    for required in sorted(CRITICAL_FILES):
        if required not in seen_paths:
            result.fatal.append(f"Critical file is absent from manifest inventory: {required}")

    runtime_state = manifest.get("runtime_state") or {}
    if int(manifest.get("runtime_schema_version") or 0) > int(ctx["runtime_schema_version"]):
        result.fatal.append("Runtime schema in manifest is newer than this code supports")
    if int(manifest.get("checkpoint_version") or 0) > int(ctx["checkpoint_version"]):
        result.fatal.append("Checkpoint schema in manifest is newer than this code supports")
    if int(manifest.get("queue_version") or 0) > int(ctx["queue_version"]):
        result.fatal.append("Queue schema in manifest is newer than this code supports")
    if int(runtime_state.get("delivery_intent_schema") or 0) > int(ctx["delivery_intent_schema_version"]):
        result.fatal.append("Delivery intent schema in manifest is newer than this code supports")
    if str(manifest.get("migration_epoch") or "") not in {"legacy-v1", str(ctx["migration_epoch"])}:
        result.fatal.append(f"Unsupported migration epoch: {manifest.get('migration_epoch')}")

    flags = manifest.get("feature_flags") or {}
    if flags.get("Dual Write"):
        result.fatal.append("Dual write is not a supported production feature for this build")
    if flags.get("Rollback") and not (manifest.get("rollback") or {}).get("rollback_compatible"):
        result.fatal.append("Rollback enabled but manifest is not rollback compatible")

    result.status = "failed" if result.fatal else ("warning" if result.warnings else "ok")
    return result


def validate_build_manifest(
    *,
    root: str | os.PathLike[str] = ".",
    manifest_path: str | os.PathLike[str] | None = None,
    context: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    repair_missing: bool = True,
) -> tuple[dict[str, Any], ManifestValidationResult]:
    root_path = Path(root).resolve()
    path = Path(manifest_path) if manifest_path is not None else root_path / MANIFEST_FILENAME
    repaired = False
    if not path.exists():
        if not repair_missing:
            raise BuildManifestError(f"Build manifest is missing: {path}")
        manifest = write_manifest(path, root=root_path, context=context, environ=environ)
        repaired = True
    else:
        manifest = load_manifest(path)
    result = validate_manifest(manifest, root=root_path, context=context, environ=environ)
    result.manifest_path = str(path)
    result.repaired = repaired
    if repaired and result.ok:
        result.status = "regenerated"
        result.warnings.append("Build manifest was missing and has been regenerated")
    return manifest, result


def validation_diagnostics(result: ManifestValidationResult) -> str:
    lines = [
        "Royells build manifest validation failed.",
        f"Manifest: {result.manifest_path}",
        f"Build: {result.build_id or 'unknown'}",
        "",
        "Fatal:",
    ]
    lines.extend(f"- {item}" for item in (result.fatal or ["none"]))
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in result.warnings)
    return "\n".join(lines)


def validate_or_raise(**kwargs: Any) -> tuple[dict[str, Any], ManifestValidationResult]:
    manifest, result = validate_build_manifest(**kwargs)
    if not result.ok:
        raise BuildManifestError(validation_diagnostics(result))
    return manifest, result


def build_info_lines(
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any] | ManifestValidationResult | None = None,
) -> list[str]:
    if isinstance(validation, ManifestValidationResult):
        validation_payload = validation.to_dict()
    else:
        validation_payload = dict(validation or {})
    database = manifest.get("database_state") or {}
    runtime = manifest.get("runtime_state") or {}
    repository = manifest.get("repository_state") or {}
    flags = manifest.get("feature_flags") or {}
    enabled = [name for name, enabled_flag in flags.items() if enabled_flag]
    return [
        "BUILD INFO",
        "==========",
        f"Version  : {manifest.get('royells_version', 'unknown')}",
        f"Build    : {manifest.get('build_number', 'unknown')} | {manifest.get('build_id', 'unknown')}",
        f"Profile  : {manifest.get('deployment_profile', 'unknown')}",
        f"Database : {database.get('database_backend', 'unknown')} | sqlite {database.get('sqlite_schema_version', 'n/a')} | pg {database.get('postgres_schema_version', 'n/a')}",
        f"Runtime  : runtime {runtime.get('runtime_schema', 'n/a')} | checkpoint {runtime.get('checkpoint_schema', 'n/a')} | queue {runtime.get('queue_schema', 'n/a')}",
        f"Worker   : {manifest.get('worker_engine_version', 'unknown')}",
        f"Upload   : {manifest.get('upload_engine_version', 'unknown')}",
        f"Download : {manifest.get('download_engine_version', 'unknown')}",
        f"Git      : {repository.get('short_commit') or 'not available'} | {repository.get('state', 'unknown')}",
        f"Features : {', '.join(enabled[:8]) if enabled else 'none'}",
        f"Manifest : {validation_payload.get('status', 'unknown')} | files {validation_payload.get('checked_files', 0)} | critical {validation_payload.get('critical_files', 0)}",
        f"Warnings : {len(validation_payload.get('warnings') or [])}",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or validate Royells build_manifest.json")
    parser.add_argument("manifest_path", nargs="?", help="Manifest path, kept for Dockerfile/backward compatibility")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--path", default=MANIFEST_FILENAME, help="Manifest path")
    parser.add_argument("--write", action="store_true", help="Generate manifest")
    parser.add_argument("--validate", action="store_true", help="Validate manifest")
    args = parser.parse_args(argv)
    if args.manifest_path:
        args.path = args.manifest_path

    if args.write:
        manifest = write_manifest(args.path, root=args.root)
        print(f"build_manifest written: {args.path}")
        print(f"build_id={manifest.get('build_id')}")
    if args.validate or not args.write:
        _manifest, result = validate_build_manifest(root=args.root, manifest_path=args.path)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        if not result.ok:
            print(validation_diagnostics(result), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
