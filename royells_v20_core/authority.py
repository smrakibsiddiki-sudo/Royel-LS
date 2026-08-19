"""Validated migration authority enforcement."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from .errors import AuthorityError
from .interfaces import AuthorityPolicyInterface
from .models import AuthorityPolicy


class StaticAuthorityPolicy(AuthorityPolicyInterface):
    """Immutable authority policy selected at process startup."""

    _DOMAIN_FIELDS = {
        "database": "database",
        "queue": "queue",
        "runtime": "runtime",
        "checkpoint": "checkpoint",
    }

    def __init__(self, policy: AuthorityPolicy) -> None:
        self._policy = policy

    def current(self) -> AuthorityPolicy:
        return self._policy

    def require_write_authority(self, domain: str, backend: str) -> None:
        field = self._DOMAIN_FIELDS.get(str(domain).strip().lower())
        if field is None:
            raise AuthorityError(f"Unknown authority domain: {domain}")
        selected = getattr(self._policy, field).value
        candidate = str(backend).strip().lower()
        if candidate == selected:
            return
        if self._policy.dual_write and candidate in {
            "postgres",
            "legacy_sqlite",
            "legacy_json",
            "legacy_composite",
        }:
            return
        raise AuthorityError(
            f"{candidate!r} is not an authorized writer for {domain}; "
            f"selected={selected!r}"
        )

    def snapshot(self) -> Mapping[str, Any]:
        payload = asdict(self._policy)
        return {
            key: value.value if hasattr(value, "value") else value
            for key, value in payload.items()
        }
