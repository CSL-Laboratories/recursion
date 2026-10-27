"""Bearer-token authentication and tenant scoping for the Control API.

Only the SHA-256 digest of a token is kept. A token is bound to one tenant and
a closed set of scopes, so a caller can neither name another tenant nor use a
scope it was not issued. There is no anonymous principal: every method,
including read-only ones, needs a valid token.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from dataclasses import dataclass

from scripts.v3.control_api.errors import Forbidden, Unauthenticated

SCOPE_OBSERVE = "observe"
SCOPE_CONTROL = "control"
SCOPES = frozenset({SCOPE_OBSERVE, SCOPE_CONTROL})

MIN_TOKEN_CHARS = 32
MAX_TOKEN_CHARS = 256
_TOKEN = re.compile(r"\A[A-Za-z0-9._~+/=-]+\Z")
_IDENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Forbidden("scope_denied")


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def parse_bearer(header: str | None) -> str:
    """Extract the token from ``Authorization: Bearer <token>`` or refuse."""

    if not isinstance(header, str):
        raise Unauthenticated("missing_credentials")
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer" or not rest or rest != rest.strip():
        raise Unauthenticated("malformed_credentials")
    if not MIN_TOKEN_CHARS <= len(rest) <= MAX_TOKEN_CHARS or not _TOKEN.match(rest):
        raise Unauthenticated("malformed_credentials")
    return rest


class TokenRegistry:
    """In-memory registry of token digests. Not persisted, not shared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[bytes, Principal]] = []

    def issue(self, principal_id: str, tenant: str, scopes: frozenset[str] | set[str]) -> str:
        """Mint a token for one principal. The token is returned once and not stored."""

        if not _IDENT.match(principal_id or "") or not _IDENT.match(tenant or ""):
            raise ValueError("principal_id and tenant must be short identifiers")
        granted = frozenset(scopes)
        if not granted or not granted <= SCOPES:
            raise ValueError("scopes must be a non-empty subset of the closed scope set")
        token = secrets.token_urlsafe(32)
        principal = Principal(principal_id, tenant, granted)
        with self._lock:
            self._entries.append((_digest(token), principal))
        return token

    def revoke(self, principal_id: str) -> int:
        with self._lock:
            kept = [item for item in self._entries if item[1].principal_id != principal_id]
            removed = len(self._entries) - len(kept)
            self._entries = kept
        return removed

    def authenticate(self, authorization: str | None) -> Principal:
        token = parse_bearer(authorization)
        candidate = _digest(token)
        with self._lock:
            entries = list(self._entries)
        match: Principal | None = None
        for stored, principal in entries:
            if hmac.compare_digest(stored, candidate) and match is None:
                match = principal
        if match is None:
            raise Unauthenticated("invalid_credentials")
        return match

    def __repr__(self) -> str:
        return f"TokenRegistry(principals={len(self._entries)})"
