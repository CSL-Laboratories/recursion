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
* It always recovers. An open key has a finite ``open_until`` (the signal's ``reset_at`` when it
  carries one, capped at ``max_open_s``; otherwise a cooldown with backoff up to ``max_cooldown_s``).
  ``reset_at`` is a reading of this breaker's clock, not a vendor epoch: adapters translate
  ``resetsAt`` first (``contract.vendor_reset``). After it, exactly one leased half-open probe is
  admitted at a time. The
  lease is bounded at the probe run's own deadline plus ``probe_grace_s`` (PRODUCT.md 11.2, C1-18),
  or ``probe_lease_s`` when the caller gives no deadline: a probe that never reports cannot hold it
  forever, and a probe still inside its deadline is never joined by a second one.
  Probe success closes; a probe that hits a limit reopens with backoff (the key the limit names);
  its lease is freed on whichever key held it; ``reset`` closes at once.

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


class ConnectionBreaker:
    def __init__(
        self,
        clock: Callable[[], float],
        *,
        cooldown_s: float = 300.0,
        max_cooldown_s: float = 3600.0,
        max_open_s: float = 8 * 24 * 3600.0,
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
        lower = model.lower()
        for family in ("opus", "sonnet"):
            if family in lower and family != model:
                slots.append(self._slot(connection, family))
        return tuple(slots)

    # -- admission ----------------------------------------------------------------------------
    def probe_lease_s(self, deadline_s: float | None = None) -> float:
        """How long a probe with this run deadline holds its lease: the deadline plus grace."""

        return self._lease_s if deadline_s is None else float(deadline_s) + self._grace_s

    def admit(self, connection: str, model: str, tenant: str, *, deadline_s: float | None = None) -> dict:
        """Decide whether one request may launch. Returns ``{"admitted", "code", "probe", "retry_at", "key"}``.

        ``deadline_s`` is the run's own deadline; a probe lease lasts that long plus ``probe_grace_s``.
        """

        with self._lock:
            now = self._clock()
            # Open and half-open keys first: a short fault never hides a real connection or model limit
            # from its tenant (grill Q23), so that tenant still gets the limit notice and its on_limit.
            probe_key = None
            for key in self._slots_for(connection, model):
                rec = self._keys.get(key)
                if rec is None or rec["state"] == CLOSED:
                    continue
                if rec["state"] == OPEN and now < rec["open_until"]:
                    return _refused(c.BREAKER_OPEN, rec["open_until"], self._display(key))
                if rec["state"] == HALF_OPEN and rec["lease_until"] is not None and now < rec["lease_until"]:
                    return _refused(c.PROBE_IN_FLIGHT, rec["lease_until"], self._display(key))
                probe_key = probe_key or key  # cooldown over, or the last probe's lease expired
            until = self._short.get((connection, model, tenant))
            if until is not None:
                if now < until:
                    return _refused(c.SHORT_FAULT, until, f"short_fault:{model}")
                del self._short[(connection, model, tenant)]
            if probe_key is None:
                return {"admitted": True, "code": None, "probe": None, "retry_at": None, "key": None}
            rec = self._keys[probe_key]
            token = f"probe-{next(self._tokens)}"
            rec.update(state=HALF_OPEN, probe=token, lease_until=now + self.probe_lease_s(deadline_s))
            return {"admitted": True, "code": None, "probe": token, "retry_at": None, "key": self._display(probe_key)}

    # -- outcomes -----------------------------------------------------------------------------
    def record(self, connection: str, model: str, tenant: str, outcome: dict, probe: str | None = None) -> str:
        """Book one route result. Returns the effect: ``opened``, ``short_fault``, ``closed``,
        ``probe_released`` or ``none``. Only ``outcome["limit"]`` on a PLAN_LIMIT opens a key."""

        code = outcome.get("code")
        with self._lock:
            now = self._clock()
            if code == c.PLAN_LIMIT and _is_signal(outcome.get("limit")):
                until = self._open(connection, outcome["limit"], now, probe)
                limit = outcome.get("limit")
                if isinstance(limit, dict) and limit.get("reset_at") is None:
                    outcome["retry_at"] = until  # the cooldown the notice can tell the user to wait for
                return "opened"
            if code == c.SHORT_FAULT:
                self._short[(connection, model, tenant)] = now + self._short_s
                return self._release(probe, now) or "short_fault"
            if code is None:
                return self._close_probe(probe) or "none"
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
        rec["failures"] += 1
        backoff = min(self._cooldown_s * 2 ** (rec["failures"] - 1), self._max_cooldown_s)
        reset_at = signal.get("reset_at")
        # reset_at is already on this clock. A raw vendor epoch that was not translated is far past
        # now + max_open_s and is capped here; adapters translate so a real reset is not the cap.
        until = min(reset_at, now + self._max_open_s) if reset_at is not None and reset_at > now else now + backoff
        rec.update(state=OPEN, open_until=until, probe=None, lease_until=None, source=signal["source"])
        return until

    def _find_probe(self, probe: str | None) -> dict | None:
        if probe is None:
            return None
        return next((rec for rec in self._keys.values() if rec.get("probe") == probe), None)

    def _close_probe(self, probe: str | None) -> str | None:
        rec = self._find_probe(probe)
        if rec is None:
            return None
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


def _is_signal(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("scope") in ("connection", "model")
        and isinstance(value.get("source"), str)
        and (value["scope"] == "connection" or isinstance(value.get("model"), str))
    )


def _refused(code: str, retry_at: float, key: str) -> dict:
    return {"admitted": False, "code": code, "probe": None, "retry_at": retry_at, "key": key}
