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
``open_cap_reached``, so one plugin cannot flood the operator's inbox. The cap
counts latched state: an expired request is not open, and stepping the clock
backward cannot make it open again.

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
  the requests it covers. At ``expires_at`` and after, the scope refuses. The first
  observation of that expiry latches it: the request or scope is terminal, and a
  later clock reading below ``expires_at`` does not clear the latch. The authority
  also keeps the greatest time it has observed, so a backward step cannot un-expire
  a row that this process has already seen past.

The first valid answer to a request wins; any later answer, through this scope or
another, is refused. Answered, expired and fully used rows are dropped. A bounded
tombstone of their ids remains so a replay is refused; an id that falls out of that
tombstone is still refused. A value that is not a 32-digit lowercase hex string,
the shape minted ids have, is not retired: ``state`` reports ``unknown``, and
``show``, ``grant``, ``approve`` and ``deny`` refuse it as unknown. One request
holds at most ``MAX_GRANTS_PER_REQUEST`` live grants. State lives only in memory:
a restart can only ask again.
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

__all__ = ["APPROVER_KINDS", "KINDS", "MAX_BINDINGS", "MAX_GRANTS_PER_REQUEST",
           "MAX_OPEN_PER_OPENER", "SCOPE_SCHEMA", "SCOPES", "ApprovalAuthority", "ApprovalScope",
           "Credential", "CredentialRegistry", "OpenedRequest"]

SCOPE_SCHEMA = "csl-approval-scope/v0"
KINDS = frozenset({"operator_ui", "paired_device", "agent", "plugin"})
APPROVER_KINDS = frozenset({"operator_ui", "paired_device"})
SCOPES = frozenset({"observe", "control", "manage", "approve"})
MAX_BINDINGS = 16
# Open (pending, unexpired) requests one opener principal may hold at once.
MAX_OPEN_PER_OPENER = 32
# Live grants one request may hold at once. Expired and fully used grants are not live.
MAX_GRANTS_PER_REQUEST = 8
# Draws for a fresh id. Past this, refuse instead of spinning under the locks.
_ID_ATTEMPTS = 8
# Detailed terminal records kept so a replay still resolves to a specific refusal.
# Past this, the id is remembered only as retired and every later use is refused.
_MAX_TOMBSTONES = 64
# Live grants retained at once, across every request. Terminal grants are not live.
_MAX_LIVE_GRANTS = 256
# Revoked token ids retained so a just-revoked id is not minted on the next draw.
# After this many later revokes the id may recur. Shown prompts and grants bind to
# the per-credential serial, so recurrence does not revive the revoked credential.
_MAX_RECENTLY_REVOKED = 1024
_ID = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _minted_id(value: object) -> bool:
    """True for the 32-digit lowercase hex shape ``secrets.token_hex(16)`` mints."""
    return type(value) is str and _ID.fullmatch(value) is not None


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


class _IdBloom:
    """Fixed-size set of ids. A hit can be wrong; a stored id is not forgotten.

    Retired request and scope ids land here once their detailed tombstone is
    dropped. A false hit refuses an id the authority never issued. A stored id
    cannot later look unknown and be minted or approved again.

    65536 bits and 4 hashes, never cleared. Near 100000 retired ids the
    false-hit rate is about 0.99, so the 8 draws in ``_mint_id`` each look
    taken and minting refuses with ``id_exhausted``. Dropping a stored id
    would let it be minted again, so that saturation stays a residual.
    """

    _BITS = 1 << 16
    _MASK = _BITS - 1

    def __init__(self) -> None:
        self._words = [0] * (self._BITS // 64)

    def add(self, key: object) -> None:
        if type(key) is not str:
            return
        for bit in self._bits(key):
            self._words[bit >> 6] |= 1 << (bit & 63)

    def __contains__(self, key: object) -> bool:
        if type(key) is not str:
            return False
        return all(self._words[bit >> 6] & (1 << (bit & 63)) for bit in self._bits(key))

    def _bits(self, key: object) -> tuple[int, int, int, int]:
        # utf-8 surrogatepass keeps the ASCII digest of every minted id and
        # does not raise on a lone surrogate. add and contains skip non-strings.
        if type(key) is str:
            material = key.encode("utf-8", "surrogatepass")
        else:
            material = b""
        digest = hmac.new(b"csl-approval-tomb/v0", material, "sha256").digest()
        mixed = int.from_bytes(digest[:8], "big")
        mask = self._MASK
        return (mixed & mask, (mixed >> 16) & mask, (mixed >> 32) & mask, (mixed >> 48) & mask)


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
    # Latched when any call observes now >= scope.expires_at. Never cleared.
    expired: bool = False


class ApprovalAuthority:
    """Core-owned registry of open requests and the scopes granted to answer them."""

    def __init__(self, clock: Callable[[], int], registry: CredentialRegistry) -> None:
        if type(registry) is not CredentialRegistry:
            raise Refused("registry_type")
        self._clock = clock
        self._registry = registry
        self._lock = threading.Lock()
        self._seen_at: int | None = None
        self._requests: dict[str, _Request] = {}
        self._request_tombs: dict[str, str] = {}
        self._request_bloom = _IdBloom()
        self._grants: dict[str, _Grant] = {}
        self._grant_tombs: dict[str, _Grant] = {}
        self._grant_bloom = _IdBloom()

    def _now(self) -> int:
        """The latest instant this authority has observed.

        Called with this authority's lock held. A commit also calls it while the
        registry lock is held, so the expiry check and ``decided_at`` see the time
        after both locks, not a sample taken while waiting on the registry.
        The clock must not call this authority or its registry: neither lock is
        reentrant, and a clock that did would deadlock.

        The returned value is the high-water mark of every instant observed so far.
        A later reading from the clock that is lower leaves this mark where it is.
        """
        now = self._clock()
        if type(now) is not int:
            raise Refused("clock")
        if self._seen_at is None or now > self._seen_at:
            self._seen_at = now
        return self._seen_at

    def _remember(self, tombs: dict, bloom: _IdBloom, key: str, value: object) -> None:
        """Keep a bounded tombstone. An id pushed out is retired in ``bloom``."""
        if key in tombs:
            del tombs[key]
        elif len(tombs) >= _MAX_TOMBSTONES:
            dropped = next(iter(tombs))
            del tombs[dropped]
            bloom.add(dropped)
        tombs[key] = value

    def _evict_request(self, request_id: str, terminal: str) -> None:
        self._requests.pop(request_id, None)
        self._remember(self._request_tombs, self._request_bloom, request_id, terminal)

    def _evict_grant(self, scope_id: str) -> None:
        grant = self._grants.pop(scope_id, None)
        if grant is None:
            grant = self._grant_tombs.get(scope_id)
            if grant is None:
                return
        self._remember(self._grant_tombs, self._grant_bloom, scope_id, grant)

    def _sweep(self, now: int) -> None:
        """Latch and drop every terminal row visible at ``now``.

        Pending rows whose ``expires_at`` has arrived become ``expired``. Expired
        and fully used grants leave the live map. Caller holds the authority lock.
        """
        for request_id, entry in list(self._requests.items()):
            if entry.state != "pending" or entry.request.expires_at <= now:
                terminal = "expired" if entry.state == "pending" else entry.state
                self._evict_request(request_id, terminal)
        for scope_id, grant in list(self._grants.items()):
            if grant.expired or now >= grant.scope.expires_at:
                grant.expired = True
                self._evict_grant(scope_id)
            elif len(grant.used) >= len(grant.scope.bindings):
                self._evict_grant(scope_id)

    def _request_id_taken(self, candidate: str) -> bool:
        return (candidate in self._requests or candidate in self._request_tombs
                or candidate in self._request_bloom)

    def _scope_id_taken(self, candidate: str) -> bool:
        return (candidate in self._grants or candidate in self._grant_tombs
                or candidate in self._grant_bloom)

    def _request_terminal(self, request_id: object) -> str | None:
        """Latched outcome of a request that is no longer live, if this id is known."""
        if not _minted_id(request_id):
            return None
        found = self._request_tombs.get(request_id)
        if found is not None:
            return found
        if request_id in self._request_bloom:
            # The detailed outcome was dropped. Refuse as expired, never as unknown.
            return "expired"
        return None

    def _refuse_if_terminal(self, request_id: object) -> None:
        terminal = self._request_terminal(request_id)
        if terminal == "expired":
            raise Refused("request_expired")
        if terminal in ("approved", "denied"):
            raise Refused("request_answered")

    def _grant_for(self, scope_id: object) -> _Grant:
        # Live and tombstoned rows are resolved below, before the filter.
        # Anything not minted is unknown and never reaches that filter.
        if not _minted_id(scope_id):
            raise Refused("scope_unknown")
        grant = self._grants.get(scope_id)
        if grant is None:
            grant = self._grant_tombs.get(scope_id)
        if grant is None:
            if scope_id in self._grant_bloom:
                raise Refused("scope_expired")
            raise Refused("scope_unknown")
        return grant

    def _live_request(self, request_id: object, now: int) -> _Request:
        # A live row's id is always minted, so this does not skip one. The
        # retirement filter is consulted only after the live map misses.
        if not _minted_id(request_id):
            raise Refused("request_unknown")
        entry = self._requests.get(request_id)
        if entry is None:
            self._refuse_if_terminal(request_id)
            raise Refused("request_unknown")
        if entry.state != "pending":
            raise Refused("request_answered")
        if entry.request.expires_at <= now:
            self._evict_request(request_id, "expired")
            raise Refused("request_expired")
        return entry

    def _grant_count(self, request_id: str) -> int:
        return sum(1 for grant in self._grants.values()
                   if any(rid == request_id for rid, _digest in grant.scope.bindings))

    def _require_grant_room(self, request_ids: list[str]) -> None:
        if len(self._grants) >= _MAX_LIVE_GRANTS:
            raise Refused("grant_cap_reached")
        for request_id in dict.fromkeys(request_ids):
            if self._grant_count(request_id) >= MAX_GRANTS_PER_REQUEST:
                raise Refused("grant_cap_reached")

    def open(self, credential: object, request: ApprovalRequest) -> "OpenedRequest":
        """Register ``request`` for ``credential`` under a core-assigned id.

        Returns the ``OpenedRequest``: the request re-keyed to the id the core chose, and
        the prompt the core drew. The prompt's requester is ``credential.principal``. Any
        kind may open a request, but only with the ``control`` scope; ``observe`` alone
        is read-only. Each opener principal holds at most ``MAX_OPEN_PER_OPENER`` open
        requests, counted by latched state.
        """
        opener = self._registry.resolve(credential)
        if "control" not in opener.scopes:
            raise Refused("open_scope_required")
        if type(request) is not ApprovalRequest:
            raise Refused("request_type")

        def commit(opener: Credential) -> OpenedRequest:
            # After both locks: the registry lock is held by _call_if_current.
            now = self._now()
            self._sweep(now)
            if "control" not in opener.scopes:
                raise Refused("open_scope_required")
            if request.expires_at <= now:
                raise Refused("request_expired")
            # Latched state only: sweep already dropped expired and answered rows.
            held = sum(1 for entry in self._requests.values()
                       if entry.requester == opener.principal and entry.state == "pending")
            if held >= MAX_OPEN_PER_OPENER:
                raise Refused("open_cap_reached")
            # The core picks the id; the caller's draft id is discarded.
            # Tombstoned and retired ids stay taken so a replay cannot be reopened.
            request_id = _mint_id(self._request_id_taken)
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
            self._sweep(now)
            _require_approver(viewer)
            out = []
            for rid, entry in self._requests.items():
                if entry.state != "pending":
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
            self._sweep(now)
            _require_approver(viewer)
            entry = self._live_request(request_id, now)
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
            self._sweep(now)
            _require_approver(holder)
            pairs = []
            expires_at = now + ttl_seconds
            for rid in request_ids:
                entry = self._live_request(rid, now)
                if holder.serial not in entry.shown:
                    raise Refused("prompt_not_shown")
                pairs.append((rid, entry.request.digest))
                expires_at = min(expires_at, entry.request.expires_at)
            self._require_grant_room([rid for rid, _digest in pairs])
            scope_id = _mint_id(self._scope_id_taken)
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
            now = self._now()
            self._sweep(now)
            # A non-minted id is unknown and skips the filter. A minted id
            # resolves the live row before that filter.
            if not _minted_id(request_id):
                return "unknown"
            entry = self._requests.get(request_id)
            if entry is not None:
                return entry.state
            terminal = self._request_terminal(request_id)
            if terminal is not None:
                return terminal
            return "unknown"

    def _decide(self, credential: object, scope_id: object, request_id: object,
                digest: object, outcome: str) -> dict:
        _check_approver(self._registry, credential)
        if type(digest) is not str or not _HEX64.fullmatch(digest):
            raise Refused("digest_form")

        def commit(holder: Credential) -> dict:
            now = self._now()
            self._sweep(now)
            _require_approver(holder)
            grant = self._grant_for(scope_id)
            scope = grant.scope
            if (not hmac.compare_digest(scope.holder, holder.token_id)
                    or holder.serial != grant.holder_serial):
                raise Refused("scope_not_delegable")
            if grant.expired or now >= scope.expires_at:
                grant.expired = True
                self._evict_grant(scope.scope_id)
                raise Refused("scope_expired")
            bound = dict(scope.bindings).get(request_id) if type(request_id) is str else None
            if bound is None:
                raise Refused("scope_binding")
            if request_id in grant.used:
                raise Refused("scope_replay")
            if not hmac.compare_digest(bound, digest):
                raise Refused("digest_mismatch")
            entry = self._live_request(request_id, now)
            grant.used.add(request_id)
            entry.state = outcome
            result = {"request_id": request_id, "digest": bound, "outcome": outcome,
                      "principal": holder.principal, "requester": entry.requester,
                      "scope_id": scope.scope_id, "decided_at": now}
            self._evict_request(request_id, outcome)
            if len(grant.used) >= len(scope.bindings):
                self._evict_grant(scope.scope_id)
            return result

        with self._lock:
            return self._registry._call_if_current(credential, commit)
