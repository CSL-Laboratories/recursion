"""The dollar reservation: admission at the breaker's admission (F9; grill Q44 and O59; pillar P10). See README.md.

``admit(connection, route, run_id)`` is called by the route service at the breaker's admission, before any
adapter runs (lane DRIVER-CC keeps ``routes/service.py`` and applies the call there through a guarded import;
with the module ABSENT, launch is as at the base). It answers one of::

    {"allowed": True,  "code": None,                  "reservation_id": "rsv-...", "reason": <word>, "metered": bool}
    {"allowed": False, "code": "reservation_refused", "reservation_id": None,      "reason": <word>, "metered": bool}

and a refusal launches nothing. On the wire a refused start answers as every launch-path refusal does today:
``start_failed`` with ``start_error`` {code: reservation_refused, detail: <reason>}. Admission is where the
reservation is made: there is no separate ``reserve`` call. ``release(run_id)`` ends a hold whose final
usage is known. A metered hold with no final usage, or with an unreconciled unknown effect, is marked
``unresolved`` and still counts. Amounts are integer micro-USD. Until composition installs a ``Reservations``
(``install``), nothing can be recorded and no cap read,
so a route that can spend money is refused (``quota_not_composed``) and one that cannot is admitted with no row.

The rules. Decided: money is a reservation at zero unless the user opts in with their own pay-per-token key
under a cap the user sets (grill Q44, O59); unknown stays unknown (P10); the meter is the connection (grill Q8).

* A route that cannot spend money (a subscription that bills nothing) is admitted with a zero hold.
* An API-key route whose key is the stored key (``key_source`` absent or ``custody``) is admitted only when
  KEYS' spend policy for that key says the user opted in, with a cap above 0 (an absent cap is 0), and the
  connection's spend is KNOWN to be below the cap. A spend that is unknown, or only a lower bound below the
  cap, refuses: nothing shows the cap is not reached.
* An API-key route whose key comes from an ``api_key_helper`` is refused (``consent_unsupported``) until that
  helper's key is bound. KEYS is not asked. The stored key's consent and known spend are not applied, even
  when the connection id is the same.
* Every other route that can spend money is refused (``consent_unsupported``) until a consent interface for its
  funding source exists. KEYS' policy belongs to a stored key and is never applied to another kind of
  connection that happens to share its id. These are a passthrough route (a cloud or gateway bills it); a
  subscription route the user told to bill its apiKeyHelper (``billing_ack``); and a subscription on its plan's
  own paid fallback (usage credits, overage).
* A subscription's paid-fallback state comes from the meters: True is paid; False is free; None (no reading)
  keeps the connection's last typed reading, and is free only when there was none. A read that fails or is not
  typed refuses (``paid_fallback_unknown``).
* A metered hold reserves the headroom left under the cap at its admission. The holds of other runs on the same
  connection count against the cap, so while one paid run holds all of it a second is refused (``cap_held``). A
  raised cap opens new headroom that another run can take.
* Amounts are an integer number of micro-USD. The decimal text is that integer written exactly. An unknown
  cost is null, never 0. ``release`` of a metered hold with no final known usage marks it ``unresolved``; the
  hold still counts, so a second admission on that key is ``cap_held``. ``settle`` writes one row per
  reservation id, status ``settled`` or ``unresolved``; a second settle under that id leaves the first row.
* A restart sweep marks a metered hold whose run reported no final usage ``provisional`` and does not end it.
* A hold whose run has an unreconciled ``outcome_unknown`` effect survives ``release`` until ``reconcile_effect``
  and a settle at the real cost. It is never settled at 0.
* The daemon's own spend for a session is counted once across resumes (a later cumulative total replaces the
  earlier one). It is never taken from the engine's budget flag. Admission uses it only when the spend reader
  still has nothing.
* One current reservation per run. A retried start is checked again. It reuses the run's hold when that hold
  still fits: free for a free attempt, or within the headroom now left for a paid one. Otherwise the hold is
  released (``superseded``) and a new one made, in the same transaction. On another connection the old hold is
  released (``superseded``). A retry that is refused releases the run's hold (``refused_on_retry``).

KEYS' policy is read BEFORE the transaction (it is KEYS' own file, not the event database). The connection's
spend, holds and meter state are read, and the rows written, in ONE transaction, so two admissions never both
take the same headroom. Nothing here calls the registry, the daemon or an engine, inside the transaction or out.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

RESERVATION_REFUSED = "reservation_refused"

# Why an admission was allowed.
NO_SPEND = "no_spend"
WITHIN_CAP = "within_cap"
# Why one was refused. The word is the refusal's detail; nothing downstream reads wording.
NO_OPT_IN = "no_opt_in"
CAP_ZERO = "cap_zero"
CAP_REACHED = "cap_reached"
CAP_HELD = "cap_held"
SPEND_UNKNOWN = "spend_unknown"
CONSENT_UNSUPPORTED = "consent_unsupported"
PAID_FALLBACK_UNKNOWN = "paid_fallback_unknown"
POLICY_UNAVAILABLE = "policy_unavailable"
POLICY_INVALID = "policy_invalid"
STORE_UNAVAILABLE = "store_unavailable"
ROUTE_INVALID = "route_invalid"
NOT_COMPOSED = "quota_not_composed"
ALLOWED_REASONS = frozenset({NO_SPEND, WITHIN_CAP})
REFUSAL_REASONS = frozenset({NO_OPT_IN, CAP_ZERO, CAP_REACHED, CAP_HELD, SPEND_UNKNOWN, CONSENT_UNSUPPORTED,
                             PAID_FALLBACK_UNKNOWN, POLICY_UNAVAILABLE, POLICY_INVALID, STORE_UNAVAILABLE,
                             ROUTE_INVALID, NOT_COMPOSED})

# Why a route can spend money. None means it cannot.
API_KEY = "api_key"
PASSTHROUGH = "passthrough"
API_KEY_HELPER = "api_key_helper"  # an unbound helper key, not the stored key the connection id names
API_KEY_HELPER_BILLING = "api_key_helper_billing"
PLAN_PAID_FALLBACK = "plan_paid_fallback"

# Why a held reservation ended.
RUN_ENDED = "run_ended"
SUPERSEDED = "superseded"
REFUSED_ON_RETRY = "refused_on_retry"

CONNECTION_KINDS = ("subscription", "api_key", "passthrough")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
QUALITIES = ("known", "lower_bound", "unknown")
PROVENANCES = ("observed", "modelled", "inferred", "unavailable")
ZERO = Decimal(0)
MICRO_USD = 1_000_000
_FAILED = object()  # a paid-fallback read that failed or answered something that is not typed


def micro_usd(value: object) -> int:
    """An amount as an integer number of micro-USD. A fraction of a micro-USD is refused.

    Unknown is not an amount. Callers pass None around this function and store null, never 0.
    """

    amount = dollars(value)
    scaled = amount * MICRO_USD
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError("an amount is an integer number of micro-USD")
    return int(integral)


def text_of_micro(micro: int) -> str:
    """An integer micro-USD amount as the decimal text the reservation rows keep."""

    if isinstance(micro, bool) or not isinstance(micro, int) or micro < 0:
        raise ValueError("an amount is an integer number of micro-USD")
    whole, frac = divmod(micro, MICRO_USD)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:06d}".rstrip("0")


def _micro_int(value: int | None) -> int | None:
    """A caller-supplied micro-USD integer, or None when the cost is unknown. A bool is not an integer."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("a cost is an integer number of micro-USD, or unknown")
    return value


def dollars(value: object) -> Decimal:
    """An amount in US dollars as a Decimal: a finite number of 0 or more. Text and booleans are refused."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("an amount in dollars is a number")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise ValueError("an amount in dollars is a number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("an amount in dollars is finite and not negative")
    return amount


def text_of(amount: Decimal) -> str:
    """An amount as the decimal text the tables keep: the exact integer micro-USD, no exponent."""

    return text_of_micro(micro_usd(amount))


@dataclass(frozen=True)
class Spend:
    """What is known of a connection's spend, by the event schema's rules for a dollar amount: ``unknown`` has no
    value and is ``unavailable``; ``known`` and ``lower_bound`` carry a value and say where it came from."""

    value: Decimal | None
    quality: str
    provenance: str

    def __post_init__(self) -> None:
        if self.quality not in QUALITIES or self.provenance not in PROVENANCES:
            raise ValueError("a spend's quality and provenance come from the schema's lists")
        if self.quality == "unknown":
            if self.value is not None or self.provenance != "unavailable":
                raise ValueError("an unknown spend has no value and is unavailable, never 0")
        elif (not isinstance(self.value, Decimal) or not self.value.is_finite() or self.value < 0
              or self.provenance == "unavailable"):
            raise ValueError("a known or lower-bound spend has a value of 0 or more and a provenance")


UNKNOWN_SPEND = Spend(None, "unknown", "unavailable")


def known_spend(value: object, provenance: str = "observed") -> Spend:
    return Spend(dollars(value), "known", provenance)


def lower_bound_spend(value: object, provenance: str = "observed") -> Spend:
    return Spend(dollars(value), "lower_bound", provenance)


class NoReads:
    """What admission can read before the ledger exists (slice 2): nothing. Every spend is unknown, and no
    subscription has a paid-fallback reading. The two calls, which slice 2's ledger answers inside admission's
    transaction through ``tx``:

    * ``spent(tx, connection) -> Spend``: a ``Spend``, or anything else, which counts as unknown;
    * ``paid_fallback_active(tx, connection) -> bool | None``: True or False is a typed reading, None is no reading
      (the last typed reading of the connection stands), and anything else, or an exception, refuses.
    """

    def spent(self, tx, connection_id: str) -> Spend:
        return UNKNOWN_SPEND

    def paid_fallback_active(self, tx, connection_id: str) -> bool | None:
        return None


def connection_key(route: Mapping) -> str:
    """The connection a route is metered through, ``<kind>:<id>``: the same string as ``routes.contract``'s."""

    conn = route["connection"]
    return f"{conn['kind']}:{conn['id']}"


def spend_basis(route: Mapping) -> str | None:
    """Why ``route`` can spend money, or None when the route itself cannot (the meters may still say otherwise)."""

    conn = route["connection"]
    kind = conn["kind"]
    if kind == "api_key":
        # The user's helper supplies this key. It is not the stored key the connection id names, and nothing
        # in this slice binds the two. A stored key's consent must not cover it.
        if conn.get("key_source") == API_KEY_HELPER:
            return API_KEY_HELPER
        return API_KEY
    if kind == "passthrough":
        return PASSTHROUGH
    if kind == "subscription":
        # An apiKeyHelper outranks the sign-in; billing_ack is the user's choice to run anyway, billing that key.
        # Without the acknowledgement the adapter asks the user (route_choice_required) and nothing is billed.
        if route.get("api_key_helper") == "present" and route.get("billing_ack") == "api_key_helper":
            return API_KEY_HELPER_BILLING
        return None
    raise ValueError("route.connection.kind")


def _checked(connection: object, route: object, run_id: object) -> tuple[str, str, str]:
    """``(connection id, engine, model)`` of a well-formed call, or ValueError."""

    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("run_id")
    if not isinstance(route, Mapping) or not isinstance(route.get("connection"), Mapping):
        raise ValueError("route")
    conn = route["connection"]
    if conn.get("kind") not in CONNECTION_KINDS or not isinstance(conn.get("id"), str) or not conn["id"]:
        raise ValueError("route.connection")
    for name in ("engine", "model"):
        if not isinstance(route.get(name), str) or not route[name]:
            raise ValueError(f"route.{name}")
    if connection != connection_key(route):
        raise ValueError("connection")
    return conn["id"], route["engine"], route["model"]


def _answer(allowed: bool, reservation_id: str | None, reason: str, metered: bool) -> dict:
    return {"allowed": allowed, "code": None if allowed else RESERVATION_REFUSED,
            "reservation_id": reservation_id if allowed else None, "reason": reason, "metered": metered}


@dataclass(frozen=True)
class _Policy:
    opted_in: bool
    cap: Decimal


_COLUMNS = ("reservation_id", "run_id", "connection_id", "engine", "model", "state", "metered", "basis", "reason",
            "amount_usd", "amount_micro_usd", "cap_usd", "spent_usd", "spent_quality", "spent_provenance",
            "paid_fallback")
_INSERT = (f"INSERT INTO quota_reservation ({', '.join(_COLUMNS)}) "
           f"VALUES ({', '.join('?' for _ in _COLUMNS)})")
_HELD_WHERE = ("h.state = 'held' AND NOT EXISTS (SELECT 1 FROM quota_reservation r "
               "WHERE r.reservation_id = h.reservation_id AND r.state = 'released')")
_HELD = ("SELECT h.reservation_id, h.run_id, h.connection_id, h.engine, h.model, h.metered, h.basis, h.reason, "
         "h.amount_usd, h.cap_usd FROM quota_reservation h WHERE h.{column} = ? AND " + _HELD_WHERE +
         " ORDER BY h.row_id")
_ALL_HELD = ("SELECT h.reservation_id, h.run_id, h.connection_id, h.engine, h.model, h.metered, h.basis, h.reason, "
             "h.amount_usd, h.cap_usd FROM quota_reservation h WHERE " + _HELD_WHERE + " ORDER BY h.row_id")
_HAS_STATE = "SELECT 1 FROM quota_reservation WHERE reservation_id = ? AND state = ?"
_USAGE = "SELECT cost_micro_usd FROM quota_usage WHERE reservation_id = ? ORDER BY row_id"
_INSERT_USAGE = "INSERT INTO quota_usage (reservation_id, run_id, cost_micro_usd) VALUES (?, ?, ?)"
_EFFECT = "SELECT outcome FROM quota_effect WHERE run_id = ? ORDER BY row_id"
_INSERT_EFFECT = "INSERT INTO quota_effect (reservation_id, run_id, outcome) VALUES (?, ?, ?)"
_SETTLE_READ = "SELECT status, amount_micro_usd FROM quota_settle WHERE reservation_id = ?"
_INSERT_SETTLE = ("INSERT INTO quota_settle (reservation_id, run_id, status, amount_micro_usd) VALUES (?, ?, ?, ?)")
_RUN_OF = "SELECT run_id FROM quota_reservation WHERE reservation_id = ? ORDER BY row_id LIMIT 1"
_SESSION = ("SELECT run_id, cost_micro_usd, reported_total_micro_usd FROM quota_session_spend "
            "WHERE connection_id = ? AND session_id = ? ORDER BY row_id")
_INSERT_SESSION = ("INSERT INTO quota_session_spend (session_id, connection_id, run_id, cost_micro_usd, "
                   "reported_total_micro_usd, max_budget_micro_usd) VALUES (?, ?, ?, ?, ?, ?)")
_HELD_NAMES = ("reservation_id", "run_id", "connection_id", "engine", "model", "metered", "basis", "reason",
               "amount_usd", "cap_usd")
_LAST_READING = ("SELECT paid_fallback FROM quota_reservation WHERE connection_id = ? AND paid_fallback IS NOT NULL "
                 "ORDER BY row_id DESC LIMIT 1")


class Reservations:
    """Admission and release over the event store's transaction helper.

    ``db`` is the helper (``transaction()``), with the lane's tables declared (``tables.declare``).
    ``spend_policy(key_id)`` is KEYS': ``{metered_spend_allowed, cap_usd}`` for the stored key an API-key route's
    connection id names; a KeyError means KEYS keeps no key by that id. It is asked only for a stored key
    (``key_source`` absent or ``custody``), never for a key an ``api_key_helper`` supplies.
    ``reads`` answers ``spent`` and ``paid_fallback_active`` (see ``NoReads``); ``NoReads`` until the ledger exists.
    """

    def __init__(self, db, spend_policy: Callable[[str], Mapping] | None, *, reads=None) -> None:
        self._db = db
        self._spend_policy = spend_policy
        self._reads = reads if reads is not None else NoReads()

    # -- the two calls of the launch path

    def admit(self, connection: str, route: Mapping, run_id: str) -> dict:
        try:
            connection_id, engine, model = _checked(connection, route, run_id)
            basis = spend_basis(route)
            session_id = route.get("session_id")
            if session_id is not None and (not isinstance(session_id, str) or not RUN_ID.fullmatch(session_id)):
                return _answer(False, None, ROUTE_INVALID, False)
        except (ValueError, KeyError, TypeError):
            return _answer(False, None, ROUTE_INVALID, False)
        # Outside the transaction: KEYS' own file, and only for the key it belongs to.
        policy = self._policy(connection_id) if basis == API_KEY else None
        try:
            with self._db.transaction() as tx:
                return self._decide(tx, connection, run_id, engine, model, basis, policy, session_id)
        except Exception:  # noqa: BLE001 - a store that cannot record the reservation admits nothing
            log.exception("quota: the reservation for run %s could not be recorded", run_id)
            return _answer(False, None, STORE_UNAVAILABLE, basis is not None)

    def release(self, run_id: str) -> list[str] | None:
        """End the run's held reservations, or mark an unsettled metered hold unresolved.

        Returns the ids this call recorded: [] when none was open, or when a metered hold was already
        unresolved (a second release writes nothing). None when the store could not record it, and the hold
        then stays and still counts. A metered hold with no final known usage, or with an unreconciled
        unknown effect, is marked unresolved and still counts. A known final usage, and a hold that was not
        metered, are released.
        """

        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return []
        try:
            with self._db.transaction() as tx:
                ended = []
                for row in self._held(tx, "run_id", run_id):
                    if self._has_state(tx, row["reservation_id"], "unresolved"):
                        continue
                    self._end_run(tx, row)
                    ended.append(row["reservation_id"])
                return ended
        except Exception:  # noqa: BLE001 - the run is over either way; the hold is what is left behind
            log.exception("quota: the release of run %s could not be recorded", run_id)
            return None

    def record_usage(self, run_id: str, *, cost_micro_usd: int | None) -> None:
        """Append the run's usage. ``None`` is an unknown cost and is stored as null, never as 0."""

        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("run_id")
        cost = _micro_int(cost_micro_usd)
        with self._db.transaction() as tx:
            hold = self._open_hold(tx, run_id)
            if hold is None:
                raise ValueError("run has no hold")
            tx.execute(_INSERT_USAGE, (hold["reservation_id"], run_id, cost))

    def record_effect(self, run_id: str, *, outcome: str) -> None:
        """Record ``outcome_unknown`` for the run's open hold. The hold then survives release."""

        if outcome != "outcome_unknown":
            raise ValueError("outcome")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("run_id")
        with self._db.transaction() as tx:
            hold = self._open_hold(tx, run_id)
            if hold is None:
                raise ValueError("run has no hold")
            tx.execute(_INSERT_EFFECT, (hold["reservation_id"], run_id, outcome))

    def reconcile_effect(self, run_id: str) -> None:
        """Append ``reconciled`` when the run's latest effect is still ``outcome_unknown``. Writes nothing otherwise."""

        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("run_id")
        with self._db.transaction() as tx:
            if not self._effect_open(tx, run_id):
                return
            hold = self._open_hold(tx, run_id)
            reservation_id = hold["reservation_id"] if hold is not None else self._effect_reservation(tx, run_id)
            tx.execute(_INSERT_EFFECT, (reservation_id, run_id, "reconciled"))

    def record_session_spend(self, connection: str, session_id: str, run_id: str, *,
                             cost_micro_usd: int | None = None, total_cost_usd: object = None,
                             max_budget_usd: object = None) -> None:
        """Append one daemon observation for a session.

        ``cost_micro_usd`` is the daemon's own cost for ``run_id``. ``total_cost_usd`` is the engine's
        cumulative figure for the session and replaces the earlier one; it is not added to it.
        ``max_budget_usd`` is the engine's budget flag and is stored unused: it is not spend.
        """

        if not isinstance(connection, str) or not connection:
            raise ValueError("connection")
        if not isinstance(session_id, str) or not RUN_ID.fullmatch(session_id):
            raise ValueError("session_id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("run_id")
        cost = _micro_int(cost_micro_usd)
        reported = None if total_cost_usd is None else micro_usd(total_cost_usd)
        budget = None if max_budget_usd is None else micro_usd(max_budget_usd)
        if cost is None and reported is None:
            raise ValueError("a session record has a daemon cost or a cumulative total")
        with self._db.transaction() as tx:
            tx.execute(_INSERT_SESSION, (session_id, connection, run_id, cost, reported, budget))

    def restart_sweep(self) -> list[str]:
        """Mark each open metered hold with no final usage as provisional. Does not end any hold.

        A second call writes nothing more. Holds that already have a final known cost are left as they are.
        Returns the reservation ids of the holds this rule covers.
        """

        with self._db.transaction() as tx:
            marked = []
            for raw in tx.execute(_ALL_HELD):
                row = dict(zip(_HELD_NAMES, raw))
                if not row["metered"]:
                    continue
                if self._final_usage_micro(tx, row["reservation_id"]) is not None:
                    continue
                marked.append(row["reservation_id"])
                if self._has_state(tx, row["reservation_id"], "provisional"):
                    continue
                self._mark(tx, row, "provisional", "no_final_usage")
            return marked

    def settle(self, reservation_id: str, *, status: str, amount_micro_usd: int | None = None) -> dict:
        """One settle row for ``reservation_id``. A second call writes nothing and returns the first row.

        ``unresolved`` carries no amount (unknown is null, never 0). ``settled`` carries an integer micro-USD
        amount and ends the hold. A settled write is refused, and nothing is written, while the run has an
        unreconciled unknown effect, so the hold is never settled at 0 in that case.
        """

        if not isinstance(reservation_id, str) or not reservation_id.startswith("rsv-"):
            raise ValueError("reservation_id")
        if status not in ("settled", "unresolved"):
            raise ValueError("status")
        if status == "unresolved":
            if amount_micro_usd is not None:
                raise ValueError("an unresolved amount is unknown, never a number")
            amount = None
        else:
            amount = _micro_int(amount_micro_usd)
            if amount is None:
                raise ValueError("a settled amount is an integer number of micro-USD")
        with self._db.transaction() as tx:
            existing = self._read_settle(tx, reservation_id)
            if existing is not None:
                return existing
            run_rows = tx.execute(_RUN_OF, (reservation_id,))
            if not run_rows:
                raise ValueError("unknown reservation")
            run_id = run_rows[0][0]
            if status == "settled" and self._effect_open(tx, run_id):
                return {"reservation_id": reservation_id, "status": "unresolved",
                        "amount_micro_usd": None, "written": False}
            tx.execute(_INSERT_SETTLE, (reservation_id, run_id, status, amount))
            if status == "settled":
                for row in self._held(tx, "run_id", run_id):
                    if row["reservation_id"] == reservation_id:
                        self._close(tx, row, "settled")
            return {"reservation_id": reservation_id, "status": status,
                    "amount_micro_usd": amount, "written": True}

    # -- inside one transaction

    def _decide(self, tx, connection: str, run_id: str, engine: str, model: str, basis: str | None,
                policy: _Policy | str | None, session_id: str | None = None) -> dict:
        own = None
        for row in self._held(tx, "run_id", run_id):
            if own is None and row["connection_id"] == connection:
                own = row  # a retried start: this run's reservation on this connection
            else:
                self._close(tx, row, SUPERSEDED)  # the run is admitted on another connection now
        reading = stored = None
        if basis is None:
            observed = self._paid_fallback(tx, connection)
            if observed is _FAILED:
                return self._refuse(tx, own, run_id, connection, engine, model, None, PAID_FALLBACK_UNKNOWN,
                                    None, None, None)
            if observed is not None:
                reading = "active" if observed else "inactive"
            stored = self._last_reading(tx, connection)
            if (reading or stored) == "active":
                basis = PLAN_PAID_FALLBACK
        if basis is None:
            if own is not None:
                # A free hold is reused only when there is no fresh typed reading to keep, or it is the one already
                # kept. A changed reading (inactive after another run's active) is written with a new zero hold, so
                # the next attempt that gets no reading finds it (second reads, change 1 of both).
                if not own["metered"] and (reading is None or reading == stored):
                    return _answer(True, own["reservation_id"], NO_SPEND, False)
                self._close(tx, own, SUPERSEDED)  # it held money, or a fresh reading must be kept
            return self._hold(tx, run_id, connection, engine, model, None, NO_SPEND, ZERO, None, None, reading)

        if basis != API_KEY:
            return self._refuse(tx, own, run_id, connection, engine, model, basis, CONSENT_UNSUPPORTED, None,
                                None, reading)
        spent = self._spent(tx, connection, session_id)
        refusal = None
        headroom_micro = 0
        if isinstance(policy, str):
            refusal = policy
        elif policy is None or not policy.opted_in:
            refusal = NO_OPT_IN
        elif policy.cap <= 0:
            refusal = CAP_ZERO
        elif spent.quality != "unknown" and micro_usd(spent.value) >= micro_usd(policy.cap):
            refusal = CAP_REACHED  # a lower bound at or past the cap already shows it reached
        elif spent.quality != "known":
            refusal = SPEND_UNKNOWN
        else:
            others = sum((micro_usd(Decimal(row["amount_usd"])) for row in self._held(tx, "connection_id", connection)
                          if row["metered"] and row["run_id"] != run_id), 0)
            headroom_micro = micro_usd(policy.cap) - micro_usd(spent.value) - others
            if headroom_micro <= 0:
                refusal = CAP_HELD
        cap = policy.cap if isinstance(policy, _Policy) else None
        if refusal is not None:
            return self._refuse(tx, own, run_id, connection, engine, model, basis, refusal, cap, spent, None)
        if own is not None:
            own_micro = micro_usd(Decimal(own["amount_usd"]))
            if own["metered"] and own_micro <= headroom_micro:
                return _answer(True, own["reservation_id"], WITHIN_CAP, True)  # the hold still fits: no second one
            self._close(tx, own, SUPERSEDED)  # a zero hold, or one larger than the headroom the cap now leaves
        amount = Decimal(text_of_micro(headroom_micro))
        return self._hold(tx, run_id, connection, engine, model, basis, WITHIN_CAP, amount, cap, spent, None)

    def _refuse(self, tx, own, run_id, connection, engine, model, basis, reason, cap, spent, reading) -> dict:
        if own is not None:
            self._close(tx, own, REFUSED_ON_RETRY)
        self._insert(tx, "rsv-" + secrets.token_hex(16), run_id, connection, engine, model, "refused", True,
                     basis, reason, ZERO, cap, spent, reading)
        return _answer(False, None, reason, True)

    def _hold(self, tx, run_id, connection, engine, model, basis, reason, amount, cap, spent, reading) -> dict:
        reservation_id = "rsv-" + secrets.token_hex(16)
        self._insert(tx, reservation_id, run_id, connection, engine, model, "held", basis is not None, basis, reason,
                     amount, cap, spent, reading)
        return _answer(True, reservation_id, reason, basis is not None)

    def _insert(self, tx, reservation_id, run_id, connection, engine, model, state, metered, basis, reason, amount,
                cap, spent: Spend | None, reading: str | None) -> None:
        tx.execute(_INSERT, (reservation_id, run_id, connection, engine, model, state, 1 if metered else 0, basis,
                             reason, text_of(amount), micro_usd(amount), None if cap is None else text_of(cap),
                             None if spent is None or spent.value is None else text_of(spent.value),
                             None if spent is None else spent.quality, None if spent is None else spent.provenance,
                             reading))

    def _close(self, tx, row: dict, reason: str) -> None:
        self._mark(tx, row, "released", reason)

    def _mark(self, tx, row: dict, state: str, reason: str) -> None:
        cap = None if row["cap_usd"] is None else Decimal(row["cap_usd"])
        self._insert(tx, row["reservation_id"], row["run_id"], row["connection_id"], row["engine"], row["model"],
                     state, bool(row["metered"]), row["basis"], reason, Decimal(row["amount_usd"]), cap, None, None)

    def _end_run(self, tx, row: dict) -> None:
        """A known final usage releases the hold. An unsettled metered hold is marked unresolved and still counts."""

        if not row["metered"]:
            self._close(tx, row, RUN_ENDED)
            return
        if self._effect_open(tx, row["run_id"]):
            self._mark(tx, row, "unresolved", "effect_unknown")
            return
        if self._final_usage_micro(tx, row["reservation_id"]) is None:
            self._mark(tx, row, "unresolved", "cost_unknown")
            return
        self._close(tx, row, RUN_ENDED)

    def _held(self, tx, column: str, value: str) -> list[dict]:
        assert column in ("run_id", "connection_id"), column
        return [dict(zip(_HELD_NAMES, row)) for row in tx.execute(_HELD.format(column=column), (value,))]

    def _last_reading(self, tx, connection: str) -> str | None:
        rows = tx.execute(_LAST_READING, (connection,))
        return rows[0][0] if rows else None

    def _spent(self, tx, connection: str, session_id: str | None = None) -> Spend:
        try:
            spent = self._reads.spent(tx, connection)
        except Exception:  # noqa: BLE001 - a reader that fails knows nothing: the spend is unknown
            log.exception("quota: the spend of %s could not be read", connection)
            spent = UNKNOWN_SPEND
        if not isinstance(spent, Spend):
            spent = UNKNOWN_SPEND
        # The daemon's own session book counts only while the reader has nothing. It is not added on top of a
        # known figure, which would count the same spend twice.
        if session_id and spent.quality == "unknown":
            micro = self._daemon_session_micro(tx, connection, session_id)
            if micro is not None:
                return known_spend(Decimal(text_of_micro(micro)), "observed")
        return spent

    def _has_state(self, tx, reservation_id: str, state: str) -> bool:
        return bool(tx.execute(_HAS_STATE, (reservation_id, state)))

    def _open_hold(self, tx, run_id: str) -> dict | None:
        found = self._held(tx, "run_id", run_id)
        return found[-1] if found else None

    def _final_usage_micro(self, tx, reservation_id: str) -> int | None:
        """The latest known cost, or None when the run has no final usage (no row, or the latest cost is unknown)."""

        found = tx.execute(_USAGE, (reservation_id,))
        if not found or found[-1][0] is None:
            return None
        return int(found[-1][0])

    def _effect_open(self, tx, run_id: str) -> bool:
        found = tx.execute(_EFFECT, (run_id,))
        return bool(found) and found[-1][0] == "outcome_unknown"

    def _effect_reservation(self, tx, run_id: str) -> str:
        found = tx.execute("SELECT reservation_id FROM quota_effect WHERE run_id = ? ORDER BY row_id DESC LIMIT 1",
                           (run_id,))
        if not found:
            raise ValueError("run has no effect")
        return found[0][0]

    def _read_settle(self, tx, reservation_id: str) -> dict | None:
        found = tx.execute(_SETTLE_READ, (reservation_id,))
        if not found:
            return None
        status, amount = found[0]
        return {"reservation_id": reservation_id, "status": status,
                "amount_micro_usd": None if amount is None else int(amount), "written": False}

    def _daemon_session_micro(self, tx, connection: str, session_id: str) -> int | None:
        """The session's spend in micro-USD, counted once.

        The latest cumulative total replaces every earlier one and already includes the runs, so per-run costs
        are not added to it. With no cumulative total, the daemon's own cost of each run is summed once.
        The engine budget flag is not read.
        """

        found = tx.execute(_SESSION, (connection, session_id))
        if not found:
            return None
        latest_total = None
        per_run: dict[str, int] = {}
        for run_id, cost, reported in found:
            if cost is not None:
                per_run[run_id] = int(cost)
            if reported is not None:
                latest_total = int(reported)
        if latest_total is not None:
            return latest_total
        if not per_run:
            return None
        return sum(per_run.values())

    def _paid_fallback(self, tx, connection: str):
        try:
            active = self._reads.paid_fallback_active(tx, connection)
        except Exception:  # noqa: BLE001 - a failed read is not a reading of free billing
            log.exception("quota: the paid-fallback state of %s could not be read", connection)
            return _FAILED
        if active is None or isinstance(active, bool):
            return active
        return _FAILED  # not a typed answer: neither free nor paid is shown

    # -- outside the transaction

    def _policy(self, key_id: str) -> _Policy | str:
        """KEYS' spend policy for the key, or the refusal word when it cannot be used."""

        if self._spend_policy is None:
            return POLICY_UNAVAILABLE
        try:
            raw = self._spend_policy(key_id)
        except KeyError:
            return NO_OPT_IN  # KEYS keeps no key by that id, so nobody opted in
        except Exception:  # noqa: BLE001 - KEYS failing is not an opt-in
            log.exception("quota: the spend policy of key %s could not be read", key_id)
            return POLICY_UNAVAILABLE
        if not isinstance(raw, Mapping):
            return POLICY_INVALID
        opted_in = raw.get("metered_spend_allowed", False)
        if not isinstance(opted_in, bool):
            return POLICY_INVALID
        cap = raw.get("cap_usd")
        try:
            cap = ZERO if cap is None else dollars(cap)  # an absent cap is 0
        except ValueError:
            return POLICY_INVALID
        return _Policy(opted_in, cap)


# -- the module's own answer, which the route service calls

_installed: Reservations | None = None


def install(reservations: Reservations) -> None:
    """Composition: the ``Reservations`` that ``admit`` and ``release`` answer from. Once, before the daemon serves;
    a second call raises RuntimeError, and anything but a ``Reservations`` raises TypeError."""

    global _installed
    if not isinstance(reservations, Reservations):
        raise TypeError("install takes a Reservations")
    if _installed is not None:
        raise RuntimeError("quota_already_installed")
    _installed = reservations


def admit(connection: str, route: Mapping, run_id: str) -> dict:
    """Admission for the route service. Until composition installs a ``Reservations`` there is nowhere to record a
    reservation and no cap to read: a route that cannot spend is admitted with no reservation id and no row, and a
    route that can is refused (``quota_not_composed``). Absence of the MODULE is the caller's case, not this one."""

    current = _installed
    if current is not None:
        return current.admit(connection, route, run_id)
    try:
        _checked(connection, route, run_id)
        basis = spend_basis(route)
    except (ValueError, KeyError, TypeError):
        return _answer(False, None, ROUTE_INVALID, False)
    if basis is None:
        return _answer(True, None, NO_SPEND, False)
    return _answer(False, None, NOT_COMPOSED, True)


def release(run_id: str) -> list[str] | None:
    """Release for the route service: [] before composition, since nothing could have been held."""

    current = _installed
    return current.release(run_id) if current is not None else []
