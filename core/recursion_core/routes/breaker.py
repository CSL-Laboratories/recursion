"""Connection-keyed breaker (CSL-797, candidate). See README.md.

Lesson: CSL-608 (https://linear.app/csl-laboratories/issue/CSL-608, Done research) records that
master's breaker is keyed by engine id, is shared by every tenant on that engine and never resets.
This breaker is the other way round (PRODUCT.md 055cc7a5, CSLR-D-011: 3.4 S3 and S4, grill Q8 and Q23):

* It is keyed by the connection a route is billed through (``"<kind>:<id>"``, an API key or a
  subscription login), and by (connection, model) only when the typed limit signal says the
  vendor metered that model's own bucket. Tenants never appear in a connection key: a plan limit
  on a shared login fences every tenant on that login, and no other connection.
* Only a typed limit signal (``contract.limit_signal``, built by an adapter from a typed vendor
  field) opens a connection. Recursion's own deadlines and every other engine failure leave it
  alone; a hung run is fenced for its own request only.
* A vague quota-sounding error is a SHORT_FAULT: it fences one model for one tenant for
  ``short_fault_s`` (grill Q23), never the connection and never another tenant. The refusal while
  that fence holds is itself ``SHORT_FAULT``, not ``BREAKER_OPEN``, so it is never reported as a
  usage limit and never offered as a switch point. Open and half-open keys are checked first, so a
  short fault never hides a real limit on the same connection from that tenant.
* It always recovers. An open key has a finite ``open_until`` (the signal's ``reset_at`` when that
  instant is still in the future, capped at ``max_open_s``; otherwise a cooldown with backoff up to
  ``max_cooldown_s``). A signal marked ``reset_unknown`` is not a vendor reset: the window is the
  later of that capped known instant and this breaker's escalating cooldown.
  Repeated typed limits while that window still holds are one incident: they do not stack the
  failure count or extend a cooldown that has no later vendor reset. A probe admitted after the
  window, whose own result is a limit, is the next incident and doubles the cooldown.
  ``reset_at`` is a reading of this breaker's clock, not a vendor epoch: adapters translate
  ``resetsAt`` first (``contract.vendor_reset``). After it, exactly one leased half-open probe is
  admitted at a time. The
  lease is bounded at the probe run's own deadline plus ``probe_grace_s`` (PRODUCT.md 11.2, C1-18),
  or ``probe_lease_s`` when the caller gives no deadline: a probe that never reports cannot hold it
  forever, and a probe still inside its deadline is never joined by a second one.
  Probe success closes; a probe that hits a limit reopens with backoff (the key the limit names);
  its lease is freed on whichever key held it. A limit from a different request does not drop a
  lease that is still inside its deadline, and that probe's later success does not clear a limit
  window opened under the lease. A refusal reports the open window that lasts longest, so a probe
  on one slot cannot hide a longer limit on another. ``reset`` closes at once. A probe token is booked
  once. A second ``record`` for that token, or a record whose token is no longer the current probe,
  does not release or replace the probe a newer lease holds. A limit on that stale token whose
  ``reset_at`` is still in the future still opens or extends the window.

The cooldown, lease and short-fault numbers are uncalibrated defaults (PRODUCT.md 11.2, B-10);
this module does not claim to have measured anything. It is thread-safe: one lock guards all
state, so it holds on the free-threaded build.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable

from recursion_core.routes import contract as c

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

# Above a 30-day translated window, inside a 35-45 day band. A later vendor instant is clamped
# here: a year-9999 reset, a 1e308 epoch, or a millisecond epoch (wall*1000) must not fence
# until that instant. Callers override it with max_open_s, and that argument is the ceiling.
_DEFAULT_MAX_OPEN_S = 40 * 24 * 3600.0


class ConnectionBreaker:
    def __init__(
        self,
        clock: Callable[[], float],
        *,
        cooldown_s: float = 300.0,
        max_cooldown_s: float = 3600.0,
        max_open_s: float = _DEFAULT_MAX_OPEN_S,
        probe_lease_s: float = 150.0,
        short_fault_s: float = 60.0,
        probe_grace_s: float = 30.0,
    ) -> None:
        self._clock = clock
        self._cooldown_s, self._max_cooldown_s, self._max_open_s = cooldown_s, max_cooldown_s, max_open_s
        self._lease_s, self._short_s, self._grace_s = probe_lease_s, short_fault_s, probe_grace_s
        self._lock = threading.Lock()
        # Slot is (connection,) or (connection, model). A string key would let a connection id that
        # contains "#model:" collide with another connection's model bucket.
        self._keys: dict[tuple, dict] = {}
        self._short: dict[tuple[str, str, str], float] = {}  # (connection, model, tenant) -> until
        self._tokens = itertools.count(1)

    # -- keys ---------------------------------------------------------------------------------
    @staticmethod
    def model_key(connection: str, model: str) -> str:
        """Display form of a model bucket. Storage uses a tuple, so this string is not a dict key."""

        return f"{connection}#model:{model}"

    @staticmethod
    def _slot(connection: str, model: str | None) -> tuple:
        return (connection,) if model is None else (connection, model)

    def _display(self, slot: tuple) -> str:
        return slot[0] if len(slot) == 1 else self.model_key(slot[0], slot[1])

    def _slots_for(self, connection: str, model: str) -> tuple[tuple, ...]:
        slots = [self._slot(connection, None), self._slot(connection, model)]
        # Claude's seven_day_opus / seven_day_sonnet signals use the family token as the model id
        # (claude_code._MODEL_FAMILY). A later request for another id in that family hits the same slot.
        # Family windows are tokens ("claude-opus-4"), not substrings ("notopus").
        for family in ("opus", "sonnet"):
            if family in _model_tokens(model) and family != model.lower():
                slots.append(self._slot(connection, family))
        return tuple(slots)

    # -- admission ----------------------------------------------------------------------------
    def admit(self, connection: str, model: str, tenant: str, *, deadline_s: float | None = None) -> dict:
        """Decide whether one request may launch. Returns ``{"admitted", "code", "probe", "retry_at", "key"}``.

        ``deadline_s`` is the run's own deadline; a probe lease lasts that long plus ``probe_grace_s``.
        """

        with self._lock:
            now = self._clock()
            # Open and half-open keys first: a short fault never hides a real connection or model limit
            # from its tenant (grill Q23), so that tenant still gets the limit notice and its on_limit.
            probe_key = None
            open_block = None  # (retry_at, display) the open window that ends latest
            lease_block = None
            for key in self._slots_for(connection, model):
                rec = self._keys.get(key)
                if rec is None or rec["state"] == CLOSED:
                    continue
                display = self._display(key)
                if rec["state"] == OPEN and _future(rec.get("open_until"), now):
                    if open_block is None or rec["open_until"] > open_block[0]:
                        open_block = (rec["open_until"], display)
                if _live_lease(rec, now):
                    if lease_block is None or rec["lease_until"] > lease_block[0]:
                        lease_block = (rec["lease_until"], display)
                    continue
                if rec["state"] == OPEN and _future(rec.get("open_until"), now):
                    continue
                probe_key = probe_key or key  # cooldown over, or the last probe's lease expired
            # An open window outranks a probe lease: the caller cannot run until that window ends,
            # even when another slot still holds a probe.
            if open_block is not None:
                return _refused(c.BREAKER_OPEN, open_block[0], open_block[1])
            if lease_block is not None:
                return _refused(c.PROBE_IN_FLIGHT, lease_block[0], lease_block[1])
            until = self._short.get((connection, model, tenant))
            if until is not None:
                if now < until:
                    return _refused(c.SHORT_FAULT, until, f"short_fault:{model}")
                del self._short[(connection, model, tenant)]
            if probe_key is None:
                return {"admitted": True, "code": None, "probe": None, "retry_at": None, "key": None}
            rec = self._keys[probe_key]
            token = f"probe-{next(self._tokens)}"
            lease = self._lease_s if deadline_s is None else float(deadline_s) + self._grace_s
            rec.update(state=HALF_OPEN, probe=token, lease_until=now + lease)
            return {"admitted": True, "code": None, "probe": token, "retry_at": None, "key": self._display(probe_key)}

    # -- outcomes -----------------------------------------------------------------------------
    def record(self, connection: str, model: str, tenant: str, outcome: dict, probe: str | None = None) -> str:
        """Book one route result. Returns the effect: ``opened``, ``short_fault``, ``closed``,
        ``probe_released`` or ``none``. Only ``outcome["limit"]`` on a PLAN_LIMIT opens a key."""

        code = outcome.get("code")
        with self._lock:
            now = self._clock()
            # A replay or a stale token must not release the probe that replaced it or stack a
            # failure that has no future vendor reset. A limit whose reset is still ahead still
            # opens or extends that window; the token itself is not the current lease.
            if probe is not None and self._find_probe(probe) is None:
                signal = outcome.get("limit")
                reset_at = signal.get("reset_at") if isinstance(signal, dict) else None
                if code == c.PLAN_LIMIT and _is_signal(signal) and _future(reset_at, now):
                    until = self._open(connection, signal, now, None)
                    outcome["retry_at"] = until
                    return "opened"
                return "none"
            if code == c.PLAN_LIMIT and _is_signal(outcome.get("limit")):
                until = self._open(connection, outcome["limit"], now, probe)
                # The wait is this open_until, including when a later signal names an earlier vendor
                # reset. The vendor time stays on the signal; it is not the retry the breaker allows.
                outcome["retry_at"] = until
                return "opened"
            if code == c.SHORT_FAULT:
                self._short[(connection, model, tenant)] = now + self._short_s
                self._release(probe, now)
                return "short_fault"
            if code is None:
                return self._close_probe(probe, now) or "none"
            # Deadlines, engine errors, auth, protocol, exit: request-local. A probe that ends this way
            # proved nothing, so its lease is released for the next request to probe.
            return self._release(probe, now) or "none"

    def _open(self, connection: str, signal: dict, now: float, probe: str | None = None) -> float:
        slot = self._slot(connection, None if signal["scope"] == "connection" else signal["model"])
        # The limit can land on the other key than the one the probe leased (a model-scoped limit on a
        # connection probe, or the reverse). That probe is over either way: free its lease so the key
        # it held does not refuse with PROBE_IN_FLIGHT while no probe runs.
        self._release(probe, now)
        rec = self._keys.setdefault(slot, {"state": CLOSED, "failures": 0, "probe": None, "lease_until": None})
        reset_at = signal.get("reset_at")
        # reset_at is already on this clock. A future instant is the window, capped at max_open_s.
        # The default sits above a 30-day translated reset. A later instant, including one that
        # looks like a millisecond epoch, clamps to the ceiling so a probe is eventually admitted.
        # reset_unknown keeps that known instant but does not treat it as the whole window: the
        # cooldown still escalates, and the later of the two is the hold.
        bounded = self._cap_reset(reset_at, now)
        unknown = signal.get("reset_unknown") is True
        open_until = rec.get("open_until")
        if rec["state"] == OPEN and isinstance(open_until, (int, float)) and now < open_until:
            # One incident per open window. A later vendor reset can extend it; a cooldown is not stacked.
            if bounded is not None and bounded > open_until:
                rec["open_until"] = bounded
            rec["source"] = signal["source"]
            return rec["open_until"]
        rec["failures"] += 1
        backoff = self._backoff(rec["failures"])
        cooled = now + backoff
        if unknown:
            until = bounded if bounded is not None and bounded > cooled else cooled
        else:
            until = bounded if bounded is not None else cooled
        # A probe that is still inside its deadline keeps its lease. Clearing it here would let the
        # next admit start a second probe while the first run is still going.
        if _live_lease(rec, now):
            rec.update(state=OPEN, open_until=until, source=signal["source"])
        else:
            rec.update(state=OPEN, open_until=until, probe=None, lease_until=None, source=signal["source"])
        return until

    def _cap_reset(self, reset_at: object, now: float) -> float | None:
        """A future vendor reset on this clock, clamped to ``max_open_s``. None when it is not ahead."""

        if not _future(reset_at, now):
            return None
        cap = now + float(self._max_open_s)
        if reset_at > cap:
            return cap
        return float(reset_at)

    def _backoff(self, failures: int) -> float:
        """Cooldown for this incident. Doubling stops at the cap, before ``2**1024`` overflows float."""

        cooldown, cap = float(self._cooldown_s), float(self._max_cooldown_s)
        if failures <= 1 or cooldown <= 0 or cap <= cooldown:
            return min(cooldown, cap)
        delay = cooldown
        for _ in range(failures - 1):
            if delay >= cap:
                return cap
            nxt = delay * 2
            if nxt >= cap or nxt == float("inf"):
                return cap
            delay = nxt
        return delay

    def _find_probe(self, probe: str | None) -> dict | None:
        if probe is None:
            return None
        return next((rec for rec in self._keys.values() if rec.get("probe") == probe), None)

    def _close_probe(self, probe: str | None, now: float) -> str | None:
        rec = self._find_probe(probe)
        if rec is None:
            return None
        # A limit opened under this lease is newer than the probe's success. Drop the lease and
        # leave the window; closing here would admit the next request while the limit still holds.
        if rec["state"] == OPEN and _future(rec.get("open_until"), now):
            rec.update(probe=None, lease_until=None)
            return "probe_released"
        rec.update(state=CLOSED, failures=0, probe=None, lease_until=None)
        return "closed"

    def _release(self, probe: str | None, now: float) -> str | None:
        rec = self._find_probe(probe)
        if rec is None:
            return None
        rec.update(probe=None, lease_until=None)  # stays half-open; next admit leases a new probe
        return "probe_released"

    # -- operator -----------------------------------------------------------------------------
    def reset(self, connection: str, model: str | None = None) -> None:
        """Close a connection (and its model buckets) or one model bucket at once."""

        with self._lock:
            for slot in list(self._keys):
                if slot[0] != connection:
                    continue
                if model is None or (len(slot) == 2 and slot[1] == model):
                    del self._keys[slot]
            for key in [k for k in self._short if k[0] == connection and (model is None or k[1] == model)]:
                del self._short[key]

    def state(self, connection: str, model: str | None = None) -> str:
        """Read-only view: ``closed``, ``open`` or ``half_open`` (an expired cooldown reads ``half_open``)."""

        with self._lock:
            rec = self._keys.get(self._slot(connection, model))
            if rec is None or rec["state"] == CLOSED:
                return CLOSED
            if rec["state"] == OPEN and self._clock() < rec["open_until"]:
                return OPEN
            return HALF_OPEN


def _future(value: object, now: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and now < value


def _live_lease(rec: dict, now: float) -> bool:
    return isinstance(rec.get("probe"), str) and _future(rec.get("lease_until"), now)


def _model_tokens(model: str) -> set[str]:
    token: list[str] = []
    tokens: set[str] = set()
    for ch in model.lower():
        if ch.isalnum():
            token.append(ch)
        elif token:
            tokens.add("".join(token))
            token = []
    if token:
        tokens.add("".join(token))
    return tokens


def _is_signal(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("scope") in ("connection", "model")
        and isinstance(value.get("source"), str)
        and (value["scope"] == "connection" or isinstance(value.get("model"), str))
    )


def _refused(code: str, retry_at: float, key: str) -> dict:
    return {"admitted": False, "code": code, "probe": None, "retry_at": retry_at, "key": key}
