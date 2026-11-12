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

``ApprovalAuthority.open`` takes the opener's credential, which must hold ``control``
(a non-read scope); an ``observe``-only or scope-less credential cannot write to the
operator's inbox. The prompt's ``requester`` line is that credential's principal, never
caller text. The core assigns the request id: ``open`` ignores whatever id the request
object carries and returns an ``OpenedRequest`` holding the request under a fresh
core-chosen id. One opener principal may hold at most ``MAX_OPEN_PER_OPENER`` open
(pending, unexpired) requests at a time; past that ``open`` refuses with
``open_cap_reached``, so one plugin cannot flood the operator's inbox.

The approver reads the prompt the core drew from the core, not from the opener:
``pending`` (the inbox, ``approval.list``) and ``show`` return the stored prompt text to an
approver credential and record that this credential was shown it. ``grant`` refuses a
request whose prompt this credential was never shown (``prompt_not_shown``), so a request
id and digest passed along by a plugin are not enough to approve.

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
from dataclasses import dataclass, field, replace
from typing import Callable

from .errors import Refused
from .prompt import _PRINCIPAL, ApprovalRequest, render_prompt

__all__ = ["APPROVER_KINDS", "KINDS", "MAX_BINDINGS", "MAX_OPEN_PER_OPENER", "SCOPE_SCHEMA",
           "SCOPES", "ApprovalAuthority", "ApprovalScope", "Credential", "CredentialRegistry",
           "OpenedRequest"]

SCOPE_SCHEMA = "csl-approval-scope/v0"
KINDS = frozenset({"operator_ui", "paired_device", "agent", "plugin"})
APPROVER_KINDS = frozenset({"operator_ui", "paired_device"})
SCOPES = frozenset({"observe", "control", "manage", "approve"})
MAX_BINDINGS = 16
# Open (pending, unexpired) requests one opener principal may hold at once.
MAX_OPEN_PER_OPENER = 32
# Draws for a fresh id. Past this, refuse instead of spinning under the locks.
_ID_ATTEMPTS = 8
# Revoked token ids retained so a just-revoked id is not minted on the next draw.
# After this many later revokes the id may recur. Shown prompts and grants bind to
# the per-credential serial, so recurrence does not revive the revoked credential.
_MAX_RECENTLY_REVOKED = 1024
_ID = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_WIRE_FIELDS = frozenset({"schema", "scope_id", "holder", "bindings", "expires_at",
                          "delegable"})


def _mint_id(taken: Callable[[str], bool]) -> str:
    """A 128-bit hex id for which ``taken`` is false.

    Stops after ``_ID_ATTEMPTS`` so a collision loop cannot spin while the
    registry lock, the authority lock, or both are held.
    """
    for _ in range(_ID_ATTEMPTS):
        candidate = secrets.token_hex(16)
        if not taken(candidate):
            return candidate
    raise Refused("id_exhausted")


@dataclass(frozen=True)
class Credential:
    """What the control API's authenticator resolved a bearer token to."""

    token_id: str
    principal: str
    kind: str
    scopes: frozenset[str]
    # One per mint, ignored by equality. Shown prompts and grants bind to this
    # rather than to token_id, so a reused token id is a different credential.
    serial: int = field(default=0, compare=False, hash=False)

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
    """Mints credentials and resolves only the ones it minted and has not revoked.

    A token id is not minted again while it sits in a bounded recently-revoked
    window. Each mint takes a new serial. Shown prompts and grants bind to that
    serial, so a token id reused after it leaves the window does not match the
    revoked credential. The registry lock stays held across an authority commit
    so a revoke cannot land after a successful check and before that commit.
    The commit reads the clock while that lock is held. ``ApprovalAuthority._now``
    must not call this registry or the authority: the lock is not reentrant.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, Credential] = {}
        self._revoked: set[str] = set()
        self._revoked_fifo: list[str] = []
        self._next_serial = 0

    def issue(self, principal: str, kind: str, scopes: object) -> Credential:
        if type(scopes) not in (set, frozenset):
            raise Refused("credential_scopes")
        with self._lock:
            token_id = _mint_id(lambda candidate: candidate in self._issued
                                or candidate in self._revoked)
            self._next_serial += 1
            credential = Credential(token_id=token_id, principal=principal, kind=kind,
                                    scopes=frozenset(scopes), serial=self._next_serial)
            self._issued[token_id] = credential
        return credential

    def revoke(self, token_id: str) -> None:
        with self._lock:
            if self._issued.pop(token_id, None) is None:
                return
            self._revoked.add(token_id)
            self._revoked_fifo.append(token_id)
            if len(self._revoked_fifo) > _MAX_RECENTLY_REVOKED:
                self._revoked.discard(self._revoked_fifo.pop(0))

    def resolve(self, credential: object) -> Credential:
        """Return ``credential`` if this registry issued that very object; else refuse."""
        if type(credential) is not Credential:
            raise Refused("credential_type")
        with self._lock:
            self._require_current(credential)
        return credential

    def _call_if_current(self, credential: object, fn):
        """Call ``fn(credential)`` while revoke cannot retire that object.

        The registry lock is held for the whole call. ``fn`` must not call back into
        this registry: the lock is not reentrant. A commit reads the clock from
        inside ``fn``; that clock must not call the registry or the authority either.
        """
        if type(credential) is not Credential:
            raise Refused("credential_type")
        with self._lock:
            self._require_current(credential)
            return fn(credential)

    def _require_current(self, credential: Credential) -> None:
        # Identity, not equality: equal fields copied from a wire scope's holder do
        # not make a credential this registry issued. Caller holds ``self._lock``.
        if self._issued.get(credential.token_id) is not credential:
            raise Refused("credential_unknown")


def _require_approver(credential: Credential) -> None:
    if credential.kind not in APPROVER_KINDS:
        raise Refused("principal_not_approver", credential.kind)
    if "approve" not in credential.scopes:
        raise Refused("approval_scope_required")


def _check_approver(registry: CredentialRegistry, credential: object) -> Credential:
    credential = registry.resolve(credential)
    _require_approver(credential)
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


@dataclass(frozen=True)
class OpenedRequest:
    """What ``ApprovalAuthority.open`` returns: the request under its core id, and its prompt."""

    request: ApprovalRequest
    prompt: str

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def digest(self) -> str:
        return self.request.digest


@dataclass
class _Request:
    request: ApprovalRequest
    requester: str
    prompt: str
    state: str = "pending"  # pending -> approved | denied
    # Serials of approver credentials the core has handed this prompt to.
    shown: set[int] = field(default_factory=set)


@dataclass
class _Grant:
    scope: ApprovalScope
    holder_serial: int
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
        """The clock's current instant.

        Called with this authority's lock held. A commit also calls it while the
        registry lock is held, so the expiry check and ``decided_at`` see the time
        after both locks, not a sample taken while waiting on the registry.
        The clock must not call this authority or its registry: neither lock is
        reentrant, and a clock that did would deadlock.
        """
        now = self._clock()
        if type(now) is not int:
            raise Refused("clock")
        return now

    def open(self, credential: object, request: ApprovalRequest) -> "OpenedRequest":
        """Register ``request`` for ``credential`` under a core-assigned id.

        Returns the ``OpenedRequest``: the request re-keyed to the id the core chose, and
        the prompt the core drew. The prompt's requester is ``credential.principal``. Any
        kind may open a request, but only with the ``control`` scope; ``observe`` alone
        is read-only. Each opener principal holds at most ``MAX_OPEN_PER_OPENER`` open
        requests.
        """
        opener = self._registry.resolve(credential)
        if "control" not in opener.scopes:
            raise Refused("open_scope_required")
        if type(request) is not ApprovalRequest:
            raise Refused("request_type")

        def commit(opener: Credential) -> OpenedRequest:
            # After both locks: the registry lock is held by _call_if_current.
            now = self._now()
            if "control" not in opener.scopes:
                raise Refused("open_scope_required")
            if request.expires_at <= now:
                raise Refused("request_expired")
            held = sum(1 for e in self._requests.values()
                       if e.requester == opener.principal and e.state == "pending"
                       and e.request.expires_at > now)
            if held >= MAX_OPEN_PER_OPENER:
                raise Refused("open_cap_reached")
            # The core picks the id; the caller's draft id is discarded.
            request_id = _mint_id(self._requests.__contains__)
            opened = replace(request, request_id=request_id)
            # Render before registering: a request whose prompt cannot be drawn is not open.
            text = render_prompt(opened, opener.principal)
            self._requests[request_id] = _Request(opened, opener.principal, text)
            return OpenedRequest(opened, text)

        with self._lock:
            return self._registry._call_if_current(credential, commit)

    def pending(self, credential: object) -> list[dict]:
        """The approver's inbox: every open, unexpired request with the prompt the core drew.

        Each listed prompt counts as shown to ``credential``.
        """
        _check_approver(self._registry, credential)

        def commit(viewer: Credential) -> list[dict]:
            now = self._now()
            _require_approver(viewer)
            out = []
            for rid, entry in self._requests.items():
                if entry.state != "pending" or entry.request.expires_at <= now:
                    continue
                entry.shown.add(viewer.serial)
                out.append(self._inbox_item(rid, entry))
            return out

        with self._lock:
            return self._registry._call_if_current(credential, commit)

    def show(self, credential: object, request_id: object) -> dict:
        """Return one open request's core-drawn prompt to an approver and record it shown."""
        _check_approver(self._registry, credential)

        def commit(viewer: Credential) -> dict:
            now = self._now()
            _require_approver(viewer)
            entry = self._requests.get(request_id) if type(request_id) is str else None
            if entry is None:
                raise Refused("request_unknown")
            if entry.state != "pending":
                raise Refused("request_answered")
            if entry.request.expires_at <= now:
                raise Refused("request_expired")
            entry.shown.add(viewer.serial)
            return self._inbox_item(request_id, entry)

        with self._lock:
            return self._registry._call_if_current(credential, commit)

    @staticmethod
    def _inbox_item(request_id: str, entry: _Request) -> dict:
        return {"request_id": request_id, "digest": entry.request.digest,
                "requester": entry.requester, "expires_at": entry.request.expires_at,
                "prompt": entry.prompt}

    def grant(self, credential: object, request_ids: object, ttl_seconds: int) -> ApprovalScope:
        """Issue a non-delegable scope to ``credential`` for the named open requests.

        Each request's prompt must already have been shown to ``credential`` through
        ``pending`` or ``show``; otherwise the grant refuses with ``prompt_not_shown``.
        """
        _check_approver(self._registry, credential)
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise Refused("scope_ttl")
        if type(request_ids) not in (list, tuple) or not request_ids:
            raise Refused("scope_bindings")

        def commit(holder: Credential) -> ApprovalScope:
            now = self._now()
            _require_approver(holder)
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
                if holder.serial not in entry.shown:
                    raise Refused("prompt_not_shown")
                pairs.append((rid, entry.request.digest))
                expires_at = min(expires_at, entry.request.expires_at)
            scope_id = _mint_id(self._grants.__contains__)
            scope = ApprovalScope(scope_id=scope_id, holder=holder.token_id,
                                  bindings=tuple(sorted(pairs)), expires_at=expires_at)
            self._grants[scope.scope_id] = _Grant(scope, holder.serial)
            return scope

        with self._lock:
            return self._registry._call_if_current(credential, commit)

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
        _check_approver(self._registry, credential)
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise Refused("digest_form")

        def commit(holder: Credential) -> dict:
            now = self._now()
            _require_approver(holder)
            grant = self._grants.get(scope_id) if type(scope_id) is str else None
            if grant is None:
                raise Refused("scope_unknown")
            scope = grant.scope
            if (not hmac.compare_digest(scope.holder, holder.token_id)
                    or holder.serial != grant.holder_serial):
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
                    "principal": holder.principal, "requester": entry.requester,
                    "scope_id": scope.scope_id, "decided_at": now}

        with self._lock:
            return self._registry._call_if_current(credential, commit)
