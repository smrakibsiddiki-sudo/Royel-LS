"""PostgreSQL-specific errors mapped from driver exceptions."""

from royells_v20_core.errors import DatabaseError


class PostgresError(DatabaseError):
    """Base PostgreSQL adapter error."""


class ConnectionFailure(PostgresError):
    """Connection or pool acquisition failed."""


class PostgresTimeout(PostgresError):
    """Connection, pool, statement, or transaction timed out."""


class SerializationFailure(PostgresError):
    """Transaction serialization or deadlock failure."""


class UniqueViolation(PostgresError):
    """Unique constraint violation."""


class ForeignKeyViolation(PostgresError):
    """Foreign-key constraint violation."""


class IntegrityFailure(PostgresError):
    """Other PostgreSQL integrity failure."""
