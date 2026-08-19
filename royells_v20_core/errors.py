"""Stable Royells v20 exception hierarchy.

Application code catches these technology-neutral errors. Concrete adapters
must translate backend-specific exceptions before they cross the abstraction
boundary.
"""


class RoyellsError(Exception):
    """Base class for errors raised by Royells v20."""


# Compatibility alias retained for Book 17 consumers.
Book17Error = RoyellsError


class ConfigurationError(RoyellsError):
    """Invalid, incomplete, or unsafe runtime configuration."""


class CompatibilityError(ConfigurationError):
    """A selected migration mode is incompatible with the active release."""


class AuthorityError(CompatibilityError):
    """A component attempted to use a backend that is not authoritative."""


class DatabaseError(RoyellsError):
    """Database connection, query, transaction, backup, or restore failure."""


class DatabaseConflictError(DatabaseError):
    """An optimistic version, idempotency, or uniqueness conflict."""


class QueueError(RoyellsError):
    """Queue persistence, reservation, acknowledgement, or replay failure."""


class QueueLeaseError(QueueError):
    """Queue ownership or visibility lease is stale or invalid."""


class StorageError(RoyellsError):
    """Local or remote storage operation failure."""


class TelegramError(RoyellsError):
    """Telegram adapter or Telegram API operation failure."""


class TelegramAmbiguityError(TelegramError):
    """Telegram may have accepted an operation whose response was lost."""


class CheckpointError(RoyellsError):
    """Runtime checkpoint, generation, checksum, or compatibility failure."""


class RecoveryError(RoyellsError):
    """Crash recovery could not produce a consistent resumable state."""


class MigrationError(RoyellsError):
    """Migration journal, dual-write, validation, or rollback failure."""


class ValidationError(RoyellsError):
    """Cross-backend validation detected an invalid or mismatched state."""


class ServiceResolutionError(RoyellsError):
    """A required dependency is missing or registered more than once."""


class WorkerError(RoyellsError):
    """Worker lifecycle, ownership, cancellation, or drain failure."""
