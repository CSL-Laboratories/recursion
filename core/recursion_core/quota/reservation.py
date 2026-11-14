"""The dollar reservation: admission at the breaker's admission (F9; grill Q44 and O59; pillar P10). See README.md.

``admit(connection, route, run_id)`` is called by the route service at the breaker's admission, before any
adapter runs (lane DRIVER-CC keeps ``routes/service.py`` and applies the call there through a guarded import).
It answers one of::

    {"allowed": True,  "code": None,                  "reservation_id": "rsv-...", "reason": <word>, "metered": bool}
    {"allowed": False, "code": "reservation_refused", "reservation_id": None,      "reason": <word>, "metered": bool}

and a refusal launches nothing. On the wire a refused start answers as every launch-path refusal does today:
``start_failed`` with ``start_error`` {code: reservation_refused, detail: <reason>}. ``release(run_id)`` ends
the run's reservation when the run is over.

The rules. Decided: money is a reservation at zero unless the user opts in with their own pay-per-token key
under a cap the user sets (grill Q44, O59); unknown stays unknown (P10); the meter is the connection (grill Q8).

* A route that cannot spend money (a subscription that bills nothing) is admitted with a zero hold.
* A route that can spend money is admitted only when KEYS' spend policy for its connection says the user opted
  in, with a cap above 0 (an absent cap is 0), and the connection's spend is KNOWN to be below the cap. A spend
  that is unknown, or only a lower bound below the cap, refuses: nothing shows the cap is not reached.
  What can spend money: an API-key route; a passthrough route (a cloud or a gateway bills it); a subscription
  route the user told to bill its apiKeyHelper (``billing_ack``); a subscription that the meters show drawing
  on the plan's own paid fallback (usage credits, overage).
* A metered hold reserves the whole headroom left under the cap, and the holds of other runs on the same
  connection count against the cap: a second run on one paid connection is refused (``cap_held``) until the
  first one releases.
* One reservation per run. A retried start on the same connection checks again and reuses the run's held
  reservation; on another connection the old hold is released (``superseded``) and a new one is made.

KEYS' policy is read BEFORE the transaction (it is KEYS' own file, not the event database). The connection's
spend and holds are read, and the row is written, in ONE transaction, so two admissions never both take the
same headroom. Nothing here calls the registry, the daemon or an engine, inside the transaction or outside it.
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
POLICY_UNAVAILABLE = "policy_unavailable"
POLICY_INVALID = "policy_invalid"
STORE_UNAVAILABLE = "store_unavailable"
ROUTE_INVALID = "route_invalid"
NOT_COMPOSED = "quota_not_composed"
ALLOWED_REASONS = frozenset({NO_SPEND, WITHIN_CAP})
REFUSAL_REASONS = frozenset({NO_OPT_IN, CAP_ZERO, CAP_REACHED, CAP_HELD, SPEND_UNKNOWN, POLICY_UNAVAILABLE,
                             POLICY_INVALID, STORE_UNAVAILABLE, ROUTE_INVALID, NOT_COMPOSED})

# Why a route can spend money. None means it cannot.
API_KEY = "api_key"
PASSTHROUGH = "passthrough"
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


def _text(amount: Decimal) -> str:
    return format(amount.normalize(), "f")


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
    subscription is known to be drawing on a paid fallback. Slice 2's ledger answers the same two calls, each
    inside the admission's transaction, through ``tx``."""

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
            "amount_usd", "cap_usd", "spent_usd", "spent_quality", "spent_provenance")
_INSERT = (f"INSERT INTO quota_reservation ({', '.join(_COLUMNS)}) "
           f"VALUES ({', '.join('?' for _ in _COLUMNS)})")
_HELD = ("SELECT h.reservation_id, h.run_id, h.connection_id, h.engine, h.model, h.metered, h.basis, h.reason, "
         "h.amount_usd, h.cap_usd FROM quota_reservation h WHERE h.state = 'held' AND h.{column} = ? AND NOT EXISTS "
         "(SELECT 1 FROM quota_reservation r WHERE r.reservation_id = h.reservation_id AND r.state = 'released') "
         "ORDER BY h.row_id")
_HELD_NAMES = ("reservation_id", "run_id", "connection_id", "engine", "model", "metered", "basis", "reason",
               "amount_usd", "cap_usd")


class Reservations:
    """Admission and release over the event store's transaction helper.

    ``db`` is the helper (``transaction()``), with the lane's tables declared (``tables.declare``).
    ``spend_policy(connection_id)`` is KEYS': ``{metered_spend_allowed, cap_usd}`` for the id part of the
    connection (KEYS names a key by the route's connection id); a KeyError means KEYS keeps no key by that id.
    ``reads`` answers ``spent(tx, connection)`` and ``paid_fallback_active(tx, connection)``; ``NoReads`` until
    the ledger exists.
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
        except (ValueError, KeyError, TypeError):
            return _answer(False, None, ROUTE_INVALID, False)
        policy = self._policy(connection_id)  # outside the transaction: KEYS' own file
        try:
            with self._db.transaction() as tx:
                return self._decide(tx, connection, run_id, engine, model, basis, policy)
        except Exception:  # noqa: BLE001 - a store that cannot record the reservation admits nothing
            log.exception("quota: the reservation for run %s could not be recorded", run_id)
            return _answer(False, None, STORE_UNAVAILABLE, basis is not None)

    def release(self, run_id: str) -> list[str] | None:
        """End the run's held reservations. Returns their ids ([] when none is held, so a second release writes
        nothing), or None when the store could not record it (the hold then stays, and still counts)."""

        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return []
        try:
            with self._db.transaction() as tx:
                held = self._held(tx, "run_id", run_id)
                for row in held:
                    self._close(tx, row, RUN_ENDED)
                return [row["reservation_id"] for row in held]
        except Exception:  # noqa: BLE001 - the run is over either way; the hold is what is left behind
            log.exception("quota: the release of run %s could not be recorded", run_id)
            return None

    # -- inside one transaction

    def _decide(self, tx, connection: str, run_id: str, engine: str, model: str, basis: str | None,
                policy: _Policy | str) -> dict:
        own = None
        for row in self._held(tx, "run_id", run_id):
            if own is None and row["connection_id"] == connection:
                own = row  # a retried start: this run's reservation on this connection
            else:
                self._close(tx, row, SUPERSEDED)  # the run is admitted on another connection now
        if basis is None and self._paid_fallback(tx, connection) is True:
            basis = PLAN_PAID_FALLBACK
        if basis is None:
            if own is not None:
                return _answer(True, own["reservation_id"], NO_SPEND, bool(own["metered"]))
            return self._hold(tx, run_id, connection, engine, model, None, NO_SPEND, ZERO, None, None)

        spent = self._spent(tx, connection)
        refusal, headroom = None, ZERO
        if isinstance(policy, str):
            refusal = policy
        elif not policy.opted_in:
            refusal = NO_OPT_IN
        elif policy.cap <= 0:
            refusal = CAP_ZERO
        elif spent.quality != "unknown" and spent.value >= policy.cap:
            refusal = CAP_REACHED  # a lower bound at or past the cap already shows it reached
        elif spent.quality != "known":
            refusal = SPEND_UNKNOWN
        else:
            others = sum((Decimal(row["amount_usd"]) for row in self._held(tx, "connection_id", connection)
                          if row["metered"] and row["run_id"] != run_id), ZERO)
            headroom = policy.cap - spent.value - others
            if headroom <= 0:
                refusal = CAP_HELD
        cap = None if isinstance(policy, str) else policy.cap
        if refusal is not None:
            if own is not None:
                self._close(tx, own, REFUSED_ON_RETRY)
            self._insert(tx, "rsv-" + secrets.token_hex(16), run_id, connection, engine, model, "refused", True,
                         basis, refusal, ZERO, cap, spent)
            return _answer(False, None, refusal, True)
        if own is not None:
            if own["metered"]:
                return _answer(True, own["reservation_id"], WITHIN_CAP, True)  # no second hold for one run
            self._close(tx, own, SUPERSEDED)  # it held nothing; this attempt can spend
        return self._hold(tx, run_id, connection, engine, model, basis, WITHIN_CAP, headroom, cap, spent)

    def _hold(self, tx, run_id, connection, engine, model, basis, reason, amount, cap, spent) -> dict:
        reservation_id = "rsv-" + secrets.token_hex(16)
        self._insert(tx, reservation_id, run_id, connection, engine, model, "held", basis is not None, basis, reason,
                     amount, cap, spent)
        return _answer(True, reservation_id, reason, basis is not None)

    def _insert(self, tx, reservation_id, run_id, connection, engine, model, state, metered, basis, reason, amount,
                cap, spent: Spend | None) -> None:
        tx.execute(_INSERT, (reservation_id, run_id, connection, engine, model, state, 1 if metered else 0, basis,
                             reason, _text(amount), None if cap is None else _text(cap),
                             None if spent is None or spent.value is None else _text(spent.value),
                             None if spent is None else spent.quality, None if spent is None else spent.provenance))

    def _close(self, tx, row: dict, reason: str) -> None:
        cap = None if row["cap_usd"] is None else Decimal(row["cap_usd"])
        self._insert(tx, row["reservation_id"], row["run_id"], row["connection_id"], row["engine"], row["model"],
                     "released", bool(row["metered"]), row["basis"], reason, Decimal(row["amount_usd"]), cap, None)

    def _held(self, tx, column: str, value: str) -> list[dict]:
        assert column in ("run_id", "connection_id"), column
        return [dict(zip(_HELD_NAMES, row)) for row in tx.execute(_HELD.format(column=column), (value,))]

    def _spent(self, tx, connection: str) -> Spend:
        try:
            spent = self._reads.spent(tx, connection)
        except Exception:  # noqa: BLE001 - a reader that fails knows nothing: the spend is unknown
            log.exception("quota: the spend of %s could not be read", connection)
            return UNKNOWN_SPEND
        return spent if isinstance(spent, Spend) else UNKNOWN_SPEND

    def _paid_fallback(self, tx, connection: str) -> bool | None:
        try:
            active = self._reads.paid_fallback_active(tx, connection)
        except Exception:  # noqa: BLE001 - unknown, as before the meters existed
            log.exception("quota: the paid-fallback state of %s could not be read", connection)
            return None
        return active if isinstance(active, bool) else None

    # -- outside the transaction

    def _policy(self, connection_id: str) -> _Policy | str:
        """KEYS' spend policy for the connection, or the refusal word when it cannot be used."""

        if self._spend_policy is None:
            return POLICY_UNAVAILABLE
        try:
            raw = self._spend_policy(connection_id)
        except KeyError:
            return NO_OPT_IN  # KEYS keeps no key by that id, so nobody opted in
        except Exception:  # noqa: BLE001 - KEYS failing is not an opt-in
            log.exception("quota: the spend policy of connection %s could not be read", connection_id)
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
    """Composition: the ``Reservations`` that ``admit`` and ``release`` answer from. Once, before the daemon serves."""

    global _installed
    if not isinstance(reservations, Reservations):
        raise TypeError("install takes a Reservations")
    if _installed is not None:
        raise RuntimeError("quota_already_installed")
    _installed = reservations


def admit(connection: str, route: Mapping, run_id: str) -> dict:
    """Admission for the route service. Before composition installs a ``Reservations`` there is nowhere to record
    a reservation and no cap to read, so a route that cannot spend is admitted with no reservation id and a route
    that can is refused (``quota_not_composed``)."""

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
