"""Approval scope on the control API (I-42; PRODUCT.md 3.7, 4.2).

A control API credential carries scopes from ``observe``, ``control``, ``manage`` and
``approve``. Approval needs its own scope, ``approve``; ``manage`` is neither needed
nor enough, and a paired per-device credential never carries ``manage`` (O34).

Only the ``operator_ui`` and ``paired_device`` principal kinds can approve. An
``agent`` or ``plugin`` credential is refused by ``approve`` even if its scope set
names ``approve``.

Credentials come from a ``CredentialRegistry`` (the control API's authenticator). The
authority accepts only the exact credential objects its registry issued and has not
revoked, so a hand-built ``Credential`` naming ``operator_ui`` and ``approve`` is refused
(``credential_unknown``). In-process code that holds the registry itself can still mint;
whether the in-process path is a boundary or only tamper evidence is open under O69.

``ApprovalAuthority.open`` takes the opener's credential; the prompt's ``requester`` line
is that credential's principal, never caller text.

``ApprovalAuthority.grant`` issues an ``ApprovalScope``:

* bound to the one credential it was granted to (``holder``); it is not delegable,
  so another credential presenting it is refused and the wire form refuses
  ``delegable: true``;
* bound to fixed ``(request_id, digest)`` pairs, never a wildcard. One pair is a
  one-use grant; several pairs are a scoped grant, and each pair is still used once;
* carrying ``expires_at`` (integer UNIX seconds), no later than the earliest expiry of
  the requests it covers. At ``expires_at`` and after, the scope refuses.

The first valid answer to a request wins; any later answer, through this scope or
another, is refused. State lives only in memory: a restart can only ask again.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable

from .errors import Refused
from .prompt import _PRINCIPAL, ApprovalRequest, render_prompt

__all__ = ["APPROVER_KINDS", "KINDS", "MAX_BINDINGS", "SCOPE_SCHEMA", "SCOPES",
           "ApprovalAuthority", "ApprovalScope", "Credential", "CredentialRegistry"]

SCOPE_SCHEMA = "csl-approval-scope/v0"
KINDS = frozenset({"operator_ui", "paired_device", "agent", "plugin"})
APPROVER_KINDS = frozenset({"operator_ui", "paired_device"})
SCOPES = frozenset({"observe", "control", "manage", "approve"})
MAX_BINDINGS = 16
_ID = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_WIRE_FIELDS = frozenset({"schema", "scope_id", "holder", "bindings", "expires_at",
                          "delegable"})


@dataclass(frozen=True)
class Credential:
    """What the control API's authenticator resolved a bearer token to."""

    token_id: str
    principal: str
    kind: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        if type(self.token_id) is not str or not _ID.fullmatch(self.token_id):
            raise Refused("credential_token_id")
        if type(self.principal) is not str or not _PRINCIPAL.fullmatch(self.principal):
            raise Refused("credential_principal")
        if self.kind not in KINDS:
            raise Refused("credential_kind")
        if type(self.scopes) is not frozenset or not self.scopes <= SCOPES:
            raise Refused("credential_scopes")
        if self.kind == "paired_device" and "manage" in self.scopes:
            raise Refused("pairing_grants_no_manage")


class CredentialRegistry:
    """Mints credentials and resolves only the ones it minted and has not revoked."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, Credential] = {}

    def issue(self, principal: str, kind: str, scopes: object) -> Credential:
        if type(scopes) not in (set, frozenset):
            raise Refused("credential_scopes")
        with self._lock:
            token_id = secrets.token_hex(16)
            while token_id in self._issued:
                token_id = secrets.token_hex(16)
            credential = Credential(token_id=token_id, principal=principal, kind=kind,
                                    scopes=frozenset(scopes))
            self._issued[token_id] = credential
        return credential

    def revoke(self, token_id: str) -> None:
        with self._lock:
            self._issued.pop(token_id, None)

    def resolve(self, credential: object) -> Credential:
        """Return ``credential`` if this registry issued that very object; else refuse."""
        if type(credential) is not Credential:
            raise Refused("credential_type")
        with self._lock:
            # Identity, not equality: equal fields copied from a wire scope's holder do
            # not make a credential this registry issued.
            if self._issued.get(credential.token_id) is not credential:
                raise Refused("credential_unknown")
        return credential


def _check_approver(registry: CredentialRegistry, credential: object) -> Credential:
    credential = registry.resolve(credential)
    if credential.kind not in APPROVER_KINDS:
        raise Refused("principal_not_approver", credential.kind)
    if "approve" not in credential.scopes:
        raise Refused("approval_scope_required")
    return credential


@dataclass(frozen=True)
class ApprovalScope:
    scope_id: str
    holder: str
    bindings: tuple[tuple[str, str], ...]
    expires_at: int
    delegable: bool = False

    def __post_init__(self) -> None:
        if type(self.scope_id) is not str or not _ID.fullmatch(self.scope_id):
            raise Refused("scope_id")
        if type(self.holder) is not str or not _ID.fullmatch(self.holder):
            raise Refused("scope_holder")
        if self.delegable is not False:
            raise Refused("scope_not_delegable")
        if type(self.bindings) is not tuple or not 1 <= len(self.bindings) <= MAX_BINDINGS:
            raise Refused("scope_bindings")
        for pair in self.bindings:
            if type(pair) is not tuple or len(pair) != 2 \
                    or type(pair[0]) is not str or not _ID.fullmatch(pair[0]) \
                    or type(pair[1]) is not str or not _HEX64.fullmatch(pair[1]):
                raise Refused("scope_bindings")
        if len({rid for rid, _ in self.bindings}) != len(self.bindings) \
                or list(self.bindings) != sorted(self.bindings):
            raise Refused("scope_bindings")
        if type(self.expires_at) is not int or self.expires_at < 0:
            raise Refused("scope_expires_at")

    def to_wire(self) -> dict:
        """The scope as the control API returns it."""
        return {"schema": SCOPE_SCHEMA, "scope_id": self.scope_id, "holder": self.holder,
                "bindings": [{"request_id": r, "digest": d} for r, d in self.bindings],
                "expires_at": self.expires_at, "delegable": False}

    @classmethod
    def from_wire(cls, wire: object) -> "ApprovalScope":
        if type(wire) is not dict or set(wire) != _WIRE_FIELDS or wire["schema"] != SCOPE_SCHEMA:
            raise Refused("scope_wire")
        if wire["delegable"] is not False:
            raise Refused("scope_not_delegable")
        bindings = wire["bindings"]
        if type(bindings) is not list or any(
                type(b) is not dict or set(b) != {"request_id", "digest"} for b in bindings):
            raise Refused("scope_bindings")
        return cls(scope_id=wire["scope_id"], holder=wire["holder"],
                   bindings=tuple((b["request_id"], b["digest"]) for b in bindings),
                   expires_at=wire["expires_at"])


@dataclass
class _Request:
    request: ApprovalRequest
    requester: str
    state: str = "pending"  # pending -> approved | denied


@dataclass
class _Grant:
    scope: ApprovalScope
    used: set[str] = field(default_factory=set)


class ApprovalAuthority:
    """Core-owned registry of open requests and the scopes granted to answer them."""

    def __init__(self, clock: Callable[[], int], registry: CredentialRegistry) -> None:
        if type(registry) is not CredentialRegistry:
            raise Refused("registry_type")
        self._clock = clock
        self._registry = registry
        self._lock = threading.Lock()
        self._requests: dict[str, _Request] = {}
        self._grants: dict[str, _Grant] = {}

    def _now(self) -> int:
        now = self._clock()
        if type(now) is not int:
            raise Refused("clock")
        return now

    def open(self, credential: object, request: ApprovalRequest) -> str:
        """Register ``request`` for ``credential`` and return the prompt the core draws.

        The prompt's requester is ``credential.principal``; any kind may open a request.
        """
        opener = self._registry.resolve(credential)
        if type(request) is not ApprovalRequest:
            raise Refused("request_type")
        with self._lock:
            if request.expires_at <= self._now():
                raise Refused("request_expired")
            if request.request_id in self._requests:
                raise Refused("request_duplicate")
            # Render before registering: a request whose prompt cannot be drawn is not open.
            text = render_prompt(request, opener.principal)
            self._requests[request.request_id] = _Request(request, opener.principal)
        return text

    def grant(self, credential: object, request_ids: object, ttl_seconds: int) -> ApprovalScope:
        """Issue a non-delegable scope to ``credential`` for the named open requests."""
        holder = _check_approver(self._registry, credential)
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise Refused("scope_ttl")
        if type(request_ids) not in (list, tuple) or not request_ids:
            raise Refused("scope_bindings")
        with self._lock:
            now = self._now()
            pairs = []
            expires_at = now + ttl_seconds
            for rid in request_ids:
                entry = self._requests.get(rid) if type(rid) is str else None
                if entry is None:
                    raise Refused("request_unknown")
                if entry.state != "pending":
                    raise Refused("request_answered")
                if entry.request.expires_at <= now:
                    raise Refused("request_expired")
                pairs.append((rid, entry.request.digest))
                expires_at = min(expires_at, entry.request.expires_at)
            scope = ApprovalScope(scope_id=secrets.token_hex(16), holder=holder.token_id,
                                  bindings=tuple(sorted(pairs)), expires_at=expires_at)
            self._grants[scope.scope_id] = _Grant(scope)
        return scope

    def approve(self, credential: object, scope_id: str, request_id: str, digest: str) -> dict:
        return self._decide(credential, scope_id, request_id, digest, "approved")

    def deny(self, credential: object, scope_id: str, request_id: str, digest: str) -> dict:
        return self._decide(credential, scope_id, request_id, digest, "denied")

    def state(self, request_id: str) -> str:
        with self._lock:
            entry = self._requests.get(request_id)
            if entry is None:
                return "unknown"
            if entry.state == "pending" and entry.request.expires_at <= self._now():
                return "expired"
            return entry.state

    def _decide(self, credential: object, scope_id: object, request_id: object,
                digest: object, outcome: str) -> dict:
        holder = _check_approver(self._registry, credential)
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise Refused("digest_form")
        with self._lock:
            now = self._now()
            grant = self._grants.get(scope_id) if type(scope_id) is str else None
            if grant is None:
                raise Refused("scope_unknown")
            scope = grant.scope
            if not hmac.compare_digest(scope.holder, holder.token_id):
                raise Refused("scope_not_delegable")
            if now >= scope.expires_at:
                raise Refused("scope_expired")
            bound = dict(scope.bindings).get(request_id) if type(request_id) is str else None
            if bound is None:
                raise Refused("scope_binding")
            if request_id in grant.used:
                raise Refused("scope_replay")
            if not hmac.compare_digest(bound, digest):
                raise Refused("digest_mismatch")
            entry = self._requests[request_id]
            if entry.state != "pending":
                raise Refused("request_answered")
            if now >= entry.request.expires_at:
                raise Refused("request_expired")
            grant.used.add(request_id)
            entry.state = outcome
            return {"request_id": request_id, "digest": bound, "outcome": outcome,
                    "principal": holder.principal, "requester": entry.requester, "scope_id": scope.scope_id,
                    "decided_at": now}
