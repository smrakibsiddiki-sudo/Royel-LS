"""Versioned PostgreSQL migration runner."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from royells_v20_core.interfaces import DatabaseInterface

from .errors import IntegrityFailure
from .sql import split_sql_statements


_MIGRATION_NAME = re.compile(
    r"^(\d{4})_([a-z0-9_]+)(?:\.(up|down))?\.sql$"
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str
    down_sql: str
    checksum: str

    @property
    def reversible(self) -> bool:
        return bool(self.down_sql.strip())


class MigrationManager:
    """Apply, validate, and roll back ordered SQL migrations."""

    ADVISORY_LOCK_NAMESPACE = 1380931909  # ASCII-compatible "ROYE"

    def __init__(
        self,
        database: DatabaseInterface,
        migrations_dir: str | Path,
    ) -> None:
        self.database = database
        self.migrations_dir = Path(migrations_dir)

    def _load(self) -> list[Migration]:
        grouped: dict[tuple[int, str], dict[str, str]] = {}
        for path in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if not match:
                continue
            version = int(match.group(1))
            name = match.group(2)
            direction = match.group(3) or "up"
            grouped.setdefault((version, name), {})[direction] = self._read_script(path)
        migrations: list[Migration] = []
        for (version, name), scripts in sorted(grouped.items()):
            if "up" not in scripts:
                raise IntegrityFailure(
                    f"Migration {version:04d}_{name} requires an up script"
                )
            checksum = hashlib.sha256(
                scripts["up"].encode("utf-8")
            ).hexdigest()
            migrations.append(
                Migration(
                    version=version,
                    name=name,
                    up_sql=scripts["up"],
                    down_sql=scripts.get("down", ""),
                    checksum=checksum,
                )
            )
        versions = [migration.version for migration in migrations]
        if len(versions) != len(set(versions)):
            raise IntegrityFailure("Duplicate PostgreSQL migration version")
        if versions and versions != list(range(versions[0], versions[-1] + 1)):
            raise IntegrityFailure("PostgreSQL migration versions must be contiguous")
        return migrations

    def _read_script(self, path: Path, seen: set[Path] | None = None) -> str:
        """Expand psql-style include directives inside the Book 18 root."""

        root = self.migrations_dir.parent.resolve()
        resolved = path.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise IntegrityFailure(f"Migration include escapes Book 18 root: {path}")
        active = set() if seen is None else set(seen)
        if resolved in active:
            raise IntegrityFailure(f"Recursive migration include: {resolved}")
        active.add(resolved)
        output: list[str] = []
        for line in resolved.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("\\ir "):
                include_name = stripped[4:].strip().strip("'\"")
                include_path = (resolved.parent / include_name).resolve()
                output.append(self._read_script(include_path, active))
            elif stripped.startswith("\\i "):
                include_name = stripped[3:].strip().strip("'\"")
                include_path = (resolved.parent / include_name).resolve()
                output.append(self._read_script(include_path, active))
            else:
                output.append(line)
        return "\n".join(output) + "\n"

    def _ensure_table(self) -> None:
        with self.database.transaction():
            self.database.execute("CREATE SCHEMA IF NOT EXISTS royells")
            self.database.execute(
                """
                CREATE TABLE IF NOT EXISTS royells.schema_migrations (
                    version BIGINT PRIMARY KEY,
                    description TEXT NOT NULL,
                    checksum_sha256 CHAR(64) NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    applied_by TEXT NOT NULL DEFAULT CURRENT_USER,
                    dirty BOOLEAN NOT NULL DEFAULT false,
                    reversible BOOLEAN NOT NULL DEFAULT false,
                    execution_ms BIGINT NOT NULL DEFAULT 0
                )
                """
            )
            self.database.execute(
                """
                ALTER TABLE royells.schema_migrations
                ADD COLUMN IF NOT EXISTS dirty BOOLEAN NOT NULL DEFAULT false
                """
            )
            self.database.execute(
                """
                ALTER TABLE royells.schema_migrations
                ADD COLUMN IF NOT EXISTS reversible BOOLEAN NOT NULL DEFAULT false
                """
            )
            self.database.execute(
                """
                ALTER TABLE royells.schema_migrations
                ADD COLUMN IF NOT EXISTS execution_ms BIGINT NOT NULL DEFAULT 0
                """
            )

    def _lock(self, version: int) -> None:
        self.database.fetchone(
            "SELECT pg_advisory_xact_lock(?, ?)",
            (self.ADVISORY_LOCK_NAMESPACE, int(version)),
        )

    def _execute_script(self, script: str) -> int:
        statements = split_sql_statements(script)
        for statement in statements:
            if statement.strip().upper() in {"BEGIN", "COMMIT"}:
                continue
            self.database.execute(statement)
        return len(statements)

    def applied(self) -> dict[int, dict[str, Any]]:
        self._ensure_table()
        rows = self.database.fetchall(
            """
            SELECT version, description, checksum_sha256, dirty,
                   reversible, execution_ms
            FROM royells.schema_migrations
            ORDER BY version
            """
        )
        return {
            int(row["version"]): {
                "name": str(row["description"]),
                "checksum": str(row["checksum_sha256"]),
                "dirty": bool(row["dirty"]),
                "reversible": bool(row["reversible"]),
                "execution_ms": int(row["execution_ms"] or 0),
            }
            for row in rows
        }

    def verify(self) -> None:
        local = {migration.version: migration for migration in self._load()}
        for version, record in self.applied().items():
            if record["dirty"]:
                raise IntegrityFailure(
                    f"Migration {version} is marked dirty; restore the last "
                    "validated backup before retrying"
                )
            if version not in local:
                raise IntegrityFailure(
                    f"Applied migration {version} is missing locally"
                )
            migration = local[version]
            if record["name"] != migration.name:
                raise IntegrityFailure(
                    f"Migration {version} name mismatch"
                )
            if record["checksum"] != migration.checksum:
                raise IntegrityFailure(
                    f"Migration {version} checksum mismatch"
                )
            if bool(record["reversible"]) != migration.reversible:
                raise IntegrityFailure(
                    f"Migration {version} reversibility metadata mismatch"
                )

    def upgrade(self, target: int | None = None) -> list[int]:
        self._ensure_table()
        self.verify()
        completed: list[int] = []
        for migration in self._load():
            if target is not None and migration.version > int(target):
                break

            # Persist the dirty marker before executing DDL. If the migration
            # transaction fails or the process dies, the marker survives and
            # prevents an unsafe automatic retry.
            with self.database.transaction():
                self._lock(migration.version)
                existing = self.database.fetchone(
                    """
                    SELECT description, checksum_sha256, dirty
                    FROM royells.schema_migrations
                    WHERE version=?
                    FOR UPDATE
                    """,
                    (migration.version,),
                )
                if existing is not None:
                    if bool(existing["dirty"]):
                        raise IntegrityFailure(
                            f"Migration {migration.version} is marked dirty"
                        )
                    if (
                        str(existing["description"]) != migration.name
                        or str(existing["checksum_sha256"]) != migration.checksum
                    ):
                        raise IntegrityFailure(
                            f"Migration {migration.version} conflicts with "
                            "the applied migration record"
                        )
                    continue
                self.database.execute(
                    """
                    INSERT INTO royells.schema_migrations(
                        version, description, checksum_sha256,
                        dirty, reversible, execution_ms
                    )
                    VALUES (?, ?, ?, true, ?, 0)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        migration.reversible,
                    ),
                )

            started = time.perf_counter()
            with self.database.transaction():
                self._lock(migration.version)
                marker = self.database.fetchone(
                    """
                    SELECT description, checksum_sha256, dirty
                    FROM royells.schema_migrations
                    WHERE version=?
                    FOR UPDATE
                    """,
                    (migration.version,),
                )
                if marker is None or not bool(marker["dirty"]):
                    raise IntegrityFailure(
                        f"Migration {migration.version} dirty marker is missing"
                    )
                if (
                    str(marker["description"]) != migration.name
                    or str(marker["checksum_sha256"]) != migration.checksum
                ):
                    raise IntegrityFailure(
                        f"Migration {migration.version} dirty marker conflicts "
                        "with the local migration"
                    )
                self._execute_script(migration.up_sql)
                self.database.execute(
                    """
                    UPDATE royells.schema_migrations
                    SET dirty=false,
                        reversible=?,
                        execution_ms=?,
                        applied_at=clock_timestamp(),
                        applied_by=current_user
                    WHERE version=?
                    """,
                    (
                        migration.reversible,
                        max(0, int((time.perf_counter() - started) * 1000)),
                        migration.version,
                    ),
                )
            completed.append(migration.version)
        self.verify()
        return completed

    def downgrade(self, target: int = 0) -> list[int]:
        self._ensure_table()
        local = {migration.version: migration for migration in self._load()}
        removed: list[int] = []
        for version in sorted(self.applied(), reverse=True):
            if version <= int(target):
                continue
            if version not in local:
                raise IntegrityFailure(
                    f"Cannot downgrade missing migration {version}"
                )
            migration = local[version]
            if not migration.reversible:
                raise IntegrityFailure(
                    f"Migration {version} is irreversible; restore a validated "
                    "backup instead of executing downgrade"
                )

            # As with upgrades, commit the dirty marker before DDL so a failed
            # rollback cannot be mistaken for a clean schema.
            with self.database.transaction():
                self._lock(version)
                record = self.database.fetchone(
                    """
                    SELECT dirty, checksum_sha256
                    FROM royells.schema_migrations
                    WHERE version=?
                    FOR UPDATE
                    """,
                    (version,),
                )
                if record is None:
                    continue
                if bool(record["dirty"]):
                    raise IntegrityFailure(
                        f"Migration {version} is marked dirty"
                    )
                if str(record["checksum_sha256"]) != migration.checksum:
                    raise IntegrityFailure(
                        f"Migration {version} checksum changed before downgrade"
                    )
                self.database.execute(
                    """
                    UPDATE royells.schema_migrations
                    SET dirty=true
                    WHERE version=?
                    """,
                    (version,),
                )

            with self.database.transaction():
                self._lock(version)
                record = self.database.fetchone(
                    """
                    SELECT dirty, checksum_sha256
                    FROM royells.schema_migrations
                    WHERE version=?
                    FOR UPDATE
                    """,
                    (version,),
                )
                if record is None or not bool(record["dirty"]):
                    raise IntegrityFailure(
                        f"Migration {version} dirty marker is missing"
                    )
                if str(record["checksum_sha256"]) != migration.checksum:
                    raise IntegrityFailure(
                        f"Migration {version} checksum changed before downgrade"
                    )
                self._execute_script(migration.down_sql)
                self.database.execute(
                    "DELETE FROM royells.schema_migrations WHERE version=?",
                    (version,),
                )
            removed.append(version)
        return removed
