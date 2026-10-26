"""Canonical session identity for one process-local owner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionKey:
    """One mutable session. Distinct triples do not share an owner."""

    tenant: str
    route: str
    session: str

    def __post_init__(self) -> None:
        for label, value in (
            ("tenant", self.tenant),
            ("route", self.route),
            ("session", self.session),
        ):
            if not isinstance(value, str) or not value or "/" in value or " " in value:
                raise ValueError(f"{label} must be a non-empty token")
