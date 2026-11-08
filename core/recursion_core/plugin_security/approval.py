"""Core approval gate (O11, grill Q24): the core draws every approval for any effect.

Domain-neutral. An effect (``csl-effect/vNext``) is a canonical object naming its kind,
the plugin, the run and connection it came from, its scope, its amount in integer minor
units and an opaque ``payload``. The gate never interprets the payload.

* ``request`` stores the effect's canonical bytes (immutable) and hands the core-owned
  ``draw`` callback the approval id, the SHA-256 digest and those same bytes. The
  plugin supplies no display text, so what is drawn is exactly what is approved.
* ``decide`` accepts only a credential the injected authenticator maps to the
  ``operator_ui`` principal. Plugin and agent tokens map to other principals and are
  refused; the authenticator is the core's UI adapter, never a plugin. The decision must
  name the digest that was drawn.
* ``take`` (called by the core service on the effect owner's behalf) recomputes the
  digest from the effect presented, requires an approved, unexpired, unused approval for
  exactly that digest, and consumes it. A second use is refused (replay).

Approvals live only in memory: a restart drops every pending or unused approval, so a
restart can only ask again, never approve.

State is bounded. A terminal approval (used, vetoed or expired) drops its effect bytes
and keeps only its digest and state as a replay tombstone; at most ``max_retained``
tombstones are kept, oldest evicted first. An evicted id is unknown, which ``take``
still refuses (``approval_required``). Each ``request`` sweeps expired pending or
approved entries into tombstones from an expiry heap (only due entries are touched), so
live entries are bounded by request rate times TTL. The heap itself is compacted when
stale tuples exceed ``max(max_retained, live_count)``, so the heap is at least about
half garbage (or past the retention cap when few entries are live). Early use or veto
cannot grow heap storage unbounded, and total rebuild work stays linear instead of
rescanning every pending approval on every other veto.
"""

from __future__ import annotations

import hashlib
import heapq
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .canonical import canonical
from .errors import Refused

__all__ = ["EFFECT_SCHEMA", "ApprovalGate", "check_effect", "effect_digest"]

EFFECT_SCHEMA = "csl-effect/vNext"
_FIELDS = frozenset({"schema", "kind", "plugin_id", "run_id", "connection", "scope",
                     "currency", "amount_minor", "payload"})
_KIND = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
OPERATOR = "operator_ui"


def check_effect(effect: object) -> bytes:
    """Return the effect's canonical bytes, or refuse a malformed effect."""
    try:
        raw = canonical(effect)
    except Refused:
        raise Refused("effect_noncanonical") from None
    if type(effect) is not dict or set(effect) != _FIELDS or effect["schema"] != EFFECT_SCHEMA:
        raise Refused("effect_fields")
    if type(effect["kind"]) is not str or not _KIND.fullmatch(effect["kind"]):
        raise Refused("effect_kind")
    scope = effect["scope"]
    if type(scope) is not dict or set(scope) != {"account", "venue"} \
            or any(type(v) is not str or not v for v in scope.values()):
        raise Refused("effect_scope")
    if type(effect["amount_minor"]) is not int or effect["amount_minor"] < 0:
        raise Refused("effect_amount")
    if type(effect["currency"]) is not str or not re.fullmatch(r"[A-Z]{3}", effect["currency"]):
        raise Refused("effect_currency")
    if type(effect["payload"]) is not dict:
        raise Refused("effect_payload")
    return raw


def effect_digest(effect: object) -> str:
    return hashlib.sha256(check_effect(effect)).hexdigest()


@dataclass
class _Approval:
    digest: str
    raw: bytes | None  # None once terminal: a tombstone keeps no effect bytes
    expires_at: int
    state: str = "pending"  # pending -> approved | vetoed | expired; approved -> used | expired

_TERMINAL = frozenset({"used", "vetoed", "expired"})


class ApprovalGate:
    def __init__(self, draw: Callable[[str, str, bytes], None],
                 authenticate: Callable[[object], str | None], clock: Callable[[], int],
                 ttl_seconds: int = 300, max_retained: int = 4096) -> None:
        if type(max_retained) is not int or max_retained < 1:
            raise ValueError("max_retained must be a positive int")
        self._draw = draw
        self._authenticate = authenticate
        self._clock = clock
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._max_retained = max_retained
        self._approvals: dict[str, _Approval] = {}
        self._tombstones: OrderedDict[str, None] = OrderedDict()
        # (expires_at, approval_id) for live approvals; the sweep pops only due
        # entries, so a burst inside the TTL never rescans live approvals.
        # Terminal tuples are dropped once stale entries exceed
        # max(max_retained, live), so a burst of use or veto cannot grow
        # the heap unbounded and rebuilds stay amortized.
        self._expiry: list[tuple[int, str]] = []
        self._live = 0

    def _retire(self, approval_id: str, item: _Approval, state: str) -> None:
        """Make an entry terminal: drop its bytes, keep a bounded tombstone. Lock held."""
        item.state = state
        item.raw = None
        self._tombstones[approval_id] = None
        self._tombstones.move_to_end(approval_id)
        while len(self._tombstones) > self._max_retained:
            old, _ = self._tombstones.popitem(last=False)
            self._approvals.pop(old, None)
        if self._live > 0:
            self._live -= 1
        self._compact_expiry_if_needed()

    def _compact_expiry_if_needed(self) -> None:
        """Rebuild the expiry heap only when stale tuples are a large fraction.

        Compact when stale > max(max_retained, live) so the heap is at least
        about half garbage, or past the retention cap when few entries are
        live. That keeps total rebuild work linear in the number of terminal
        operations instead of rescanning every pending approval on every
        other veto. Lock held.
        """
        stale = len(self._expiry) - self._live
        if stale <= max(self._max_retained, self._live):
            return
        kept: list[tuple[int, str]] = []
        for expires_at, approval_id in self._expiry:
            item = self._approvals.get(approval_id)
            if item is not None and item.state not in _TERMINAL:
                kept.append((expires_at, approval_id))
        self._expiry = kept
        heapq.heapify(self._expiry)

    def _sweep(self, now: int) -> None:
        """Retire live entries whose TTL has passed, touching only due ones. Lock held."""
        while self._expiry and self._expiry[0][0] <= now:
            _, approval_id = heapq.heappop(self._expiry)
            item = self._approvals.get(approval_id)
            if item is not None and item.state not in _TERMINAL:
                self._retire(approval_id, item, "expired")

    def request(self, effect: dict) -> tuple[str, str]:
        raw = check_effect(effect)
        digest = hashlib.sha256(raw).hexdigest()
        approval_id = "apr_" + secrets.token_hex(16)
        with self._lock:
            now = self._clock()
            self._sweep(now)
            self._approvals[approval_id] = _Approval(digest, raw, now + self._ttl)
            self._live += 1
            heapq.heappush(self._expiry, (now + self._ttl, approval_id))
        # Drawn from the stored bytes, after storage: the caller cannot alter either.
        self._draw(approval_id, digest, raw)
        return approval_id, digest

    def decide(self, approval_id: str, digest: str, credential: object, approve: bool) -> str:
        principal = self._authenticate(credential)
        if principal != OPERATOR:
            raise Refused("approval_requires_operator")
        if type(approve) is not bool:
            raise Refused("decision_invalid")
        with self._lock:
            item = self._approvals.get(approval_id)
            if item is None:
                raise Refused("approval_unknown")
            if item.state == "expired":
                raise Refused("approval_expired")
            if item.state != "pending":
                raise Refused("approval_already_decided")
            if item.digest != digest:
                raise Refused("approval_digest_mismatch")
            if self._clock() >= item.expires_at:
                self._retire(approval_id, item, "expired")
                raise Refused("approval_expired")
            if approve:
                item.state = "approved"
            else:
                self._retire(approval_id, item, "vetoed")
            return item.state

    def take(self, approval_id: str, effect: dict) -> str:
        """Consume the approval for exactly this effect. One use only."""
        raw = check_effect(effect)
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            item = self._approvals.get(approval_id)
            if item is None:
                raise Refused("approval_required")
            if item.digest != digest or (item.raw is not None and item.raw != raw):
                raise Refused("approval_digest_mismatch")
            if item.state == "used":
                raise Refused("approval_replayed")
            if item.state == "expired":
                raise Refused("approval_expired")
            if item.state != "approved":
                raise Refused("approval_not_granted", item.state)
            if self._clock() >= item.expires_at:
                self._retire(approval_id, item, "expired")
                raise Refused("approval_expired")
            self._retire(approval_id, item, "used")
            return digest
