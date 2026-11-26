"""Core approval gate (O11, grill Q24): the core draws every approval for any effect.

Domain-neutral. An effect (``csl-effect/vNext``) is a canonical object naming its kind,
the plugin, the run and connection it came from, its scope (account, venue, contract
family, adapter and environment, so paper and live differ in the digest), its amount in integer minor
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

A veto is final for that intent (CSL-743; Lucas's decision on csl-research #1161 item
5): once the operator vetoes an approval, ``request`` refuses the same effect digest
with ``approval_vetoed``, without drawing it or storing anything. A still-pending
approval for that digest can no longer be approved, and one approved earlier can no
longer be taken; both refuse ``approval_vetoed``. A retry must be a new intent, which
is a different effect and so a different digest. Only a veto blocks a digest: an
expired, used or still-undecided approval leaves its effect requestable again.

Given the event store (``store``, the one the approval queue writes to), the gate keeps
every approval as an ``approval.v1`` record of source ``plugin_effect`` in the queue's own
table, written and committed before it is drawn, and a gate made again on the same store
(a restart) reads each one back in the state it had. A pending one whose deadline passed,
the daemon running or not, is written ``expired`` when it is next read and is refused
``approval_expired``. An approved one is taken once: the take writes it ``consumed`` in
the same transaction, and a second take, after a restart too, is ``approval_replayed``.
The take consumes through the approval queue's own ``store.consume_in``, the one place every
consumer of the store goes through: the deadline and the veto are read there, inside the
transaction that marks the record consumed, so the queue's ``consume`` refuses what the take
refuses. An approval covers exactly the effect it was drawn for: another effect presented with it
is ``approval_digest_mismatch``. An approved one found past its deadline, by a take or
any other read, is written ``expired`` in the same transaction and stays so for good,
whatever the clock says later; approval.v1 keeps no answer on an expired record, so the
record drops it, and the ``approval.answered`` event written when it was approved still
says who approved it. A veto is final for every gate on the same store and across a
restart: ``request``, ``decide`` and ``take`` look for a refused record of the same digest
(vetoed through a gate, or denied through the approval queue) inside the same
transaction. A record that cannot be read (a torn write), or an effect the approval
record cannot hold, is refused ``record_invalid`` (its detail names the rule) and never
approved; a store that cannot be written is refused ``store_unavailable``, and nothing
was done; a store that has failed (it refuses every use until it is opened again) is refused
``store_failed``. The operator's answer is kept as the principal ``OPERATOR_PRINCIPAL``
(approval.v1 names an owner or a device as the one who answered, and nothing else).

A decision, a veto or an expiry the gate writes wakes, once it commits, the waiters an approval
queue attached to the same store in this process holds for that approval.

The daemon does not yet give its gate the store: until that join, the product's
approvals still live only in memory, do not last across a restart and reach no person.

Without a store, approvals live only in memory, as before: a restart drops every pending
or unused approval, so a restart can only ask again, never approve, and it forgets every
veto.

``run_state(run_id)`` says where a run that met an approval stands: ``waiting_approval``
while its newest approval is unanswered, and ``stalled`` once that approval is still
unanswered at its stall time (by default the deadline). An approval ends at the earlier
of its deadline and its stall time: from then on it can be neither answered nor taken,
so a stalled run never proceeds. With a store, both are the gate's own figures when the record is read: a
gate made with a shorter deadline or stall time ends a record it reads at its request time plus the
shorter figure, and a longer figure never makes an ended approval live again. Every code the gate
answers has its sentence in the one table ``SAYS``: what happened, and the one thing to do.

State is bounded (without a store). A terminal approval (used, vetoed or expired) drops its effect bytes
and keeps only its digest and state as a replay tombstone; at most ``max_retained``
tombstones are kept, oldest evicted first. An evicted id is unknown, which ``take``
still refuses (``approval_required``). Each ``request`` sweeps expired pending or
approved entries into tombstones from an expiry heap (only due entries are touched), so
live entries are bounded by request rate times TTL. The heap itself is compacted when
stale tuples exceed ``max(max_retained, live_count)``, so the heap is at least about
half garbage (or past the retention cap when few entries are live). Early use or veto
cannot grow heap storage unbounded, and total rebuild work stays linear instead of
rescanning every pending approval on every other veto.

Without a store, the vetoed digests are the one structure ``max_retained`` does not bound. They are kept
apart from the tombstones, so a burst of other approvals cannot evict a veto and undo
its finality. Each entry is one SHA-256 hex digest (no effect bytes), and the set grows
only by one per distinct digest the operator vetoes: only the ``operator_ui`` principal
can add to it, never a plugin request.
"""

from __future__ import annotations

import hashlib
import heapq
import logging
import re
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .canonical import canonical, parse
from .errors import Refused

log = logging.getLogger(__name__)

__all__ = ["APPROVAL_DEADLINE_SECONDS", "EFFECT_SCHEMA", "OPERATOR_PRINCIPAL", "SAYS", "SCOPE_FIELDS",
           "STALL_SECONDS", "TOMBSTONES_KEPT", "ApprovalGate", "check_effect", "effect_digest", "words_for"]

EFFECT_SCHEMA = "csl-effect/vNext"
_FIELDS = frozenset({"schema", "kind", "plugin_id", "run_id", "connection", "scope",
                     "currency", "amount_minor", "payload"})
# The K3 scope an effect names and its digest binds; the authority profile must match it
# exactly (PRODUCT.md 4.1; K2 binds account, venue and mode).
SCOPE_FIELDS = frozenset({"account", "venue", "contract_family", "adapter", "environment"})
_KIND = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
OPERATOR = "operator_ui"
# The principal an approval record keeps for the operator's answer: approval.v1 names an owner or a
# device as the one who answered (``approvals.record.APPROVER_KINDS``), and nothing else.
OPERATOR_PRINCIPAL = {"kind": "owner", "id": OPERATOR}

# The gate's figures. Each becomes a row of the person's one settings file once that file is in this tree;
# until then each is this one named value, and a caller may pass its own to the gate.
APPROVAL_DEADLINE_SECONDS = 300  # how long an approval can be answered and then taken; the approval queue reads it too
TOMBSTONES_KEPT = 4096  # without a store: how many used, vetoed or expired approvals are remembered
STALL_SECONDS = None  # how long a run waits on an unanswered approval before it stalls and the approval ends; None: the deadline

# Every sentence a person reads from the gate: what happened, and the one thing they can do.
_MALFORMED = "The plugin described this action in a form that is not accepted, so nothing was done. " \
             "The plugin has to ask again in the accepted form."
SAYS = {
    "waiting_approval": "This run is waiting for your answer to one action. "
                        "Approve or deny it where your approvals are listed.",
    "stalled": "This run's action was not approved and used in time, so it was not done. "
               "Ask for the action again if you still want it.",
    "approval_expired": "This approval's time ran out before it was used, so nothing was done. "
                        "Ask for the action again if you still want it.",
    "approval_replayed": "This approval was already used once and cannot be used again. Nothing more was done.",
    "approval_digest_mismatch": "This approval was given for a different action, so it was not used. "
                                "Nothing was done.",
    "approval_vetoed": "This action was refused, so it will not run. There is nothing for you to do.",
    "approval_required": "This action needs an approval it does not have, so it did not run. "
                         "There is nothing for you to do.",
    "approval_not_granted": "This approval was not used, because it is not approved: it is still waiting "
                            "for an answer, or it was refused. Nothing was done.",
    "approval_unknown": "There is no such approval. Nothing was changed.",
    "approval_already_decided": "This approval was already answered. Nothing was changed.",
    "approval_requires_operator": "Only the owner, from the operator screen, can answer here. Nothing was changed.",
    "decision_invalid": "The answer was neither approve nor deny, so nothing was changed.",
    "record_invalid": "This action could not be kept as an approval record, or its saved record could not be "
                      "read, so nothing was done. Ask for the action again; if it is refused again, its details "
                      "do not fit an approval record.",
    "store_unavailable": "The approval store could not be written, so nothing was done. Try again later.",
    "store_failed": "The approval store failed, so nothing was done, and it refuses everything until it is "
                    "opened again. Open the store again, then try again.",
    "effect_noncanonical": _MALFORMED,
    "effect_fields": _MALFORMED,
    "effect_kind": _MALFORMED,
    "effect_scope": _MALFORMED,
    "effect_amount": _MALFORMED,
    "effect_currency": _MALFORMED,
    "effect_payload": _MALFORMED,
}


def words_for(code: str) -> str | None:
    """The plain sentence a person reads for a gate state or refusal code, or ``None`` for another code."""
    return SAYS.get(code)


def _durable():
    """The approval queue's store, record and time helpers, imported when a gate is given a store (they import
    this package's canonical form, so they are not imported while this package loads)."""
    from recursion_core.approvals import model
    from recursion_core.approvals import store as rows
    from recursion_core.approvals.queue import _OUTCOME_UNKNOWN, timestamp_of
    from recursion_core.approvals.record import milliseconds
    return rows, model, timestamp_of, milliseconds, _OUTCOME_UNKNOWN


def _waker():
    """The approval queue's own way to wake the waiters of approvals settled on a store (see ``_durable``)."""
    from recursion_core.approvals.queue import _wake_waiters_of
    return _wake_waiters_of


def check_effect(effect: object) -> bytes:
    """Return the effect's canonical bytes, or refuse a malformed effect."""
    try:
        raw = canonical(effect)
        effect = parse(raw)  # Validate exactly the immutable bytes the approval will draw.
    except Refused:
        raise Refused("effect_noncanonical") from None
    if type(effect) is not dict or set(effect) != _FIELDS or effect["schema"] != EFFECT_SCHEMA:
        raise Refused("effect_fields")
    if type(effect["kind"]) is not str or not _KIND.fullmatch(effect["kind"]):
        raise Refused("effect_kind")
    scope = effect["scope"]
    if type(scope) is not dict or set(scope) != SCOPE_FIELDS \
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
    run_id: object = None
    requested_at: int = 0
    answered: bool = False

_TERMINAL = frozenset({"used", "vetoed", "expired"})


class ApprovalGate:
    def __init__(self, draw: Callable[[str, str, bytes], None],
                 authenticate: Callable[[object], str | None], clock: Callable[[], int],
                 ttl_seconds: int = APPROVAL_DEADLINE_SECONDS, max_retained: int = TOMBSTONES_KEPT, *,
                 store=None, stall_seconds: int | None = STALL_SECONDS, declare_tables: bool = True) -> None:
        if type(max_retained) is not int or max_retained < 1:
            raise ValueError("max_retained must be a positive int")
        if stall_seconds is not None and (type(stall_seconds) is not int or stall_seconds < 0):
            raise ValueError("stall_seconds must be None or an int of zero or more")
        self._store = store
        self._stall = ttl_seconds if stall_seconds is None else stall_seconds
        # An approval ends at the earlier of its deadline and its stall time: a run shown stalled never
        # goes on, so its approval can then be neither answered nor taken.
        self._end = min(ttl_seconds, self._stall)
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
        # Digests the operator vetoed: final for that intent. Not tombstones, so
        # max_retained eviction never drops one. Digest only, no effect bytes. Growth
        # is bounded by operator veto decisions (operator_ui principal only), not by
        # plugin requests. Without a store, in memory only: a restart forgets every veto. With
        # one, unused: the store is asked inside each transaction, so every gate on it sees a veto.
        self._vetoed: set[str] = set()
        self._settling = threading.local()  # the approvals this thread's open transaction settled, if any
        if store is not None:
            rows = _durable()[0]
            if declare_tables:
                store.register_tables(rows.LANE, rows.VERSION, rows.DDL)

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
        if self._store is not None:
            return self._request_kept(effect, raw, digest)
        with self._lock:
            # Before any id, storage or draw: a refused request leaves no state.
            if digest in self._vetoed:
                raise Refused("approval_vetoed")
            approval_id = "apr_" + secrets.token_hex(16)
            now = self._clock()
            self._sweep(now)
            self._approvals[approval_id] = _Approval(digest, raw, now + self._end, run_id=effect["run_id"],
                                                     requested_at=now)
            self._live += 1
            heapq.heappush(self._expiry, (now + self._end, approval_id))
        # Drawn from the stored bytes, after storage: the caller cannot alter either.
        self._draw(approval_id, digest, raw)
        return approval_id, digest

    def decide(self, approval_id: str, digest: str, credential: object, approve: bool) -> str:
        principal = self._authenticate(credential)
        if principal != OPERATOR:
            raise Refused("approval_requires_operator")
        if type(approve) is not bool:
            raise Refused("decision_invalid")
        if self._store is not None:
            return self._decide_kept(approval_id, digest, approve)
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
                # A veto of another approval for this digest is final for the intent.
                # This one stays pending until it is vetoed or expires.
                if item.digest in self._vetoed:
                    raise Refused("approval_vetoed")
                item.state = "approved"
                item.answered = True
            else:
                self._vetoed.add(item.digest)
                self._retire(approval_id, item, "vetoed")
            return item.state

    def take(self, approval_id: str, effect: dict) -> str:
        """Consume the approval for exactly this effect. One use only."""
        raw = check_effect(effect)
        digest = hashlib.sha256(raw).hexdigest()
        if self._store is not None:
            return self._take_kept(approval_id, raw, digest)
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
            # Approved before another approval for this digest was vetoed: not usable.
            if item.digest in self._vetoed:
                raise Refused("approval_vetoed")
            self._retire(approval_id, item, "used")
            return digest

    def is_vetoed(self, digest: str) -> bool:
        """Whether the operator vetoed this digest. The core's commit section asks it again
        after ``take``, so a veto acknowledged before the commit refuses it (R4-1). With a store,
        the store is asked, so a veto made through any gate on it counts."""
        with self._lock:
            if self._store is not None:
                return self._in_store(lambda tx: self._vetoed_in(tx, digest))
            return digest in self._vetoed

    def run_state(self, run_id: object) -> dict | None:
        """Where a run that met an approval stands: ``{"state", "approval_id", "says"}`` for its newest
        approval, ``waiting_approval`` while it is unanswered and ``stalled`` once it is still unanswered at
        its stall time, or once it ended unused; ``None`` when the run met none, or its newest one is
        approved and not yet ended, used or refused."""
        with self._lock:
            now = self._clock()
            if self._store is None:
                self._sweep(now)
                met = [(item.requested_at, number, approval_id, item.state, item.answered)
                       for number, (approval_id, item) in enumerate(self._approvals.items())
                       if item.run_id == run_id]
                if not met:
                    return None
                at, _, approval_id, state, _ = max(met)
            else:
                found = self._newest_kept(run_id, now)
                if found is None:
                    return None
                at, approval_id, state = found
        # An approval that ended unused (unanswered, or approved and not taken in time) leaves its run stalled.
        if state not in ("pending", "expired"):
            return None
        state = "waiting_approval" if state == "pending" and now < at + self._stall else "stalled"
        return {"state": state, "approval_id": approval_id, "says": SAYS[state]}

    # -- kept in the event store ------------------------------------------------------------------------
    # Each runs under ``_lock`` and inside one transaction of the store; a refusal is raised only after
    # that transaction has ended, so an expiry written while it read is kept.

    def _in_store(self, work):
        """``work(tx)`` inside one transaction of the store. A store that cannot be written is
        ``store_unavailable`` (nothing was written); an effect the approval record cannot hold, or a record
        that cannot be read, is ``record_invalid`` with the rule as its detail. A commit whose outcome is
        unknown is raised as it is, as the approval queue does; a store that has failed (the event store's own
        code ``store_failed``: it refuses every use until it is opened again) is ``store_failed``."""
        _, model, _, _, unknown = _durable()
        settled: list = []
        outer = getattr(self._settling, "ids", None)
        self._settling.ids = settled
        try:
            with self._store.transaction() as tx:
                result = work(tx)
        except Refused:
            raise
        except model.ApprovalRefused as refused:
            raise Refused("record_invalid", refused.code) from None
        except unknown:
            raise
        except Exception as exc:  # noqa: BLE001 - a store's own failure becomes the gate's typed refusal
            if isinstance(exc, RuntimeError) and str(exc) == "store_failed":
                raise Refused("store_failed") from exc
            raise Refused("store_unavailable", type(exc).__name__) from exc
        finally:
            self._settling.ids = outer
        if settled:
            _waker()(self._store, settled)  # committed: the approval queue's waiters of these approvals wake
        return result

    def _settled_here(self, approval_id: str) -> None:
        """Note, inside a transaction of ``_in_store``, an approval this write settles (not pending any more)."""
        ids = getattr(self._settling, "ids", None)
        if ids is not None:
            ids.append(approval_id)

    def _vetoed_in(self, tx, digest: str) -> bool:
        """Whether the store holds a refused plugin approval for this digest: vetoed through any gate on the
        store, or denied through the approval queue. Read inside the caller's transaction, by the one query of
        the approval store (``store.vetoed``)."""
        return _durable()[0].vetoed(tx, digest)

    def _reloaded(self, tx, held: dict, now: int) -> dict:
        """A record as it stands now: a pending one whose deadline has come, or an approved one found past
        it, is written ``expired`` (with ``approval.answered``) before anything reads it, for good. Its end is
        the earlier of its own deadline and its request time plus this gate's end, so a deadline or stall
        time made shorter since the request takes effect at once."""
        rows, model, timestamp_of, milliseconds, _ = _durable()
        changed = model.expire_if_due(held, timestamp_of(now))
        ended = now * 1000 >= milliseconds(held["requested_at"]) + self._end * 1000
        if changed is None and held["state"] == "pending" and ended:
            changed = model.expire_if_due(held, held["expires_at"])  # ended by this gate's shorter figure
        if changed is None and held["state"] == "approved" and (
                ended or now * 1000 >= milliseconds(held["expires_at"])):
            # approval.v1 keeps no answer on an expired record; the approval.answered event of the approval
            # still says who approved it.
            changed = dict(held, state="expired", answered_by=None, answer=None, answered_at=None)
        if changed is None:
            return held
        rows.save(tx, changed, answered=True)
        self._settled_here(changed["approval_id"])
        return changed

    def _covers(self, held: dict, digest: str, raw: bytes) -> bool:
        """Whether this approval was drawn for exactly the effect presented."""
        return held["digest"] == digest and canonical(held["effect"]) == raw

    def _load(self, tx, approval_id: object, now: int) -> dict | None:
        rows, model, _, _, _ = _durable()
        try:
            held = rows.load(tx, approval_id)
        except model.ApprovalRefused:
            raise Refused("record_invalid") from None
        if held is None or held["source"] != "plugin_effect":
            return None
        return self._reloaded(tx, held, now)

    def _request_kept(self, effect: dict, raw: bytes, digest: str) -> tuple[str, str]:
        rows, model, timestamp_of, _, unknown = _durable()
        with self._lock:
            approval_id = "apr-" + secrets.token_hex(16)
            now = self._clock()

            def work(tx):
                # Before any record or draw: a digest refused through any gate on this store, or through the
                # queue, is refused here and nothing is written.
                if self._vetoed_in(tx, digest):
                    return "approval_vetoed"
                rows.insert(tx, model.new_request(
                    approval_id=approval_id, source="plugin_effect", effect=effect, session_id=None,
                    run_id=effect["run_id"], turn_id=None, requested_at=timestamp_of(now),
                    expires_at=timestamp_of(now + self._end)), None)
                return None

            refusal = self._in_store(work)
        if refusal is not None:
            raise Refused(refusal)
        # Committed before it is drawn, from the stored bytes; marked shown once the draw took it, so a
        # request the draw never took is offered again by the queue's next start.
        self._draw(approval_id, digest, raw)
        try:
            self._in_store(lambda tx: rows.mark_shown(tx, approval_id))
        except Refused as refused:
            # Committed and drawn, so the request stands; left unmarked, the queue offers it again.
            log.error("approval %s was drawn but not marked shown: %s", approval_id, refused.code)
        except unknown:
            # The request committed before the draw; whether the mark did is not known, and the store now
            # refuses every use (``store_failed``) until it is opened again.
            log.error("approval %s was drawn; whether it was marked shown is not known", approval_id)
        return approval_id, digest

    def _decide_kept(self, approval_id: str, digest: str, approve: bool) -> str:
        rows, _, timestamp_of, _, _ = _durable()
        with self._lock:
            now = self._clock()

            def work(tx):
                held = self._load(tx, approval_id, now)
                if held is None:
                    return "approval_unknown", None
                if held["state"] == "expired":
                    return "approval_expired", None
                if held["state"] != "pending":
                    return "approval_already_decided", None
                if held["digest"] != digest:
                    return "approval_digest_mismatch", None
                if approve and self._vetoed_in(tx, held["digest"]):
                    return "approval_vetoed", None
                held = dict(held, state="approved" if approve else "vetoed",
                            answer="approve" if approve else "deny", answered_by=dict(OPERATOR_PRINCIPAL),
                            answered_at=timestamp_of(now))
                rows.save(tx, held, answered=True)
                self._settled_here(approval_id)
                return None, held["state"]

            refusal, state = self._in_store(work)
        if refusal is not None:
            raise Refused(refusal)
        return state

    def _take_kept(self, approval_id: str, raw: bytes, digest: str) -> str:
        rows = _durable()[0]
        with self._lock:
            now = self._clock()

            def work(tx):
                held = self._load(tx, approval_id, now)  # one found past its deadline is ended here, for good
                if held is None:
                    return ("approval_required",)
                if not self._covers(held, digest, raw):
                    return ("approval_digest_mismatch",)
                if held["state"] == "consumed":
                    return ("approval_replayed",)
                if held["state"] == "expired":
                    return ("approval_expired",)
                if held["state"] != "approved":
                    return ("approval_not_granted", held["state"])
                # The deadline and the veto are read where every consumer of the store consumes a record.
                refusal, _ = rows.consume_in(tx, held, None, held["revision"], held["digest"], None, now * 1000)
                return None if refusal is None else (refusal,)

            refusal = self._in_store(work)
        if refusal is not None:
            raise Refused(*refusal)
        return digest

    def _newest_kept(self, run_id: object, now: int):
        """``(requested_at seconds, approval_id, state)`` of the run's newest plugin approval, or ``None``."""
        _, _, _, milliseconds, _ = _durable()
        if type(run_id) is not str:
            return None

        def work(tx):
            newest = None
            found = tx.execute("SELECT approval_id FROM approvals_records WHERE source = 'plugin_effect' "
                               "AND log_id = ? ORDER BY approval_id", ("run:" + run_id,))
            for (approval_id,) in found:
                try:
                    held = self._load(tx, approval_id, now)
                except Refused:
                    continue  # a record that cannot be read says nothing about the run
                if held is None:
                    continue
                at = milliseconds(held["requested_at"]) / 1000
                if newest is None or at >= newest[0]:
                    newest = (at, approval_id, held["state"])
            return newest

        return self._in_store(work)
