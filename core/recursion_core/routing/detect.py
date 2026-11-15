"""Detection: which engines a routing decision may use, built from what the daemon already knows.

An engine is ROUTABLE when (my brief, step 3):

* it has a loaded route (``routes.json``, as ``load_routes`` gave it) that the daemon would not refuse
  before launch (``route_unsupported``, ``route_unbindable``: the same functions ``route.list`` and the
  starter read);
* its connection's breaker is not open (the breaker's own read-only view, read the way ``route.list``
  reads it: a trip kept in the store from an earlier daemon counts as open, and a store that cannot be
  read is ``breaker_unknown``, because a start on it is refused until the store reads; a breaker whose
  ``state()`` raises is ``detection_source_failed``, like any source that raises: unroutable either way);
* for a key route (``connection.kind`` ``api_key``) and for ``recursion_loop``, its key is present in the
  key store and opted in to spending (grill Q44: a key route with no opt-in never spends).

The answer is the snapshot ``router.decide`` takes: ``{"engines": {engine: {"routable", "reason"}}}``, one
entry for every engine the config may name, each reason a typed code. Nothing here launches, writes,
reserves or emits anything; the sources are read once each per engine.

The sources are passed in, so that this module depends on none of them and its tests need no daemon:

* ``route_refusal(entry) -> None or a code``: why the daemon would refuse the entry before launch.
  ``daemon_route_refusal(breaker)`` gives the daemon's own (it imports the daemon, Linux only);
* ``breaker_state(connection_key) -> "closed" | "half_open" | "open" | "unknown"``: ``breaker_view``
  turns the daemon's breaker into one;
* ``key_state(connection_id) -> "ready" | "absent" | "not_opted_in"``: lane KEYS' key store answers it
  once its first pull request lands; until then a test passes a plain function;
* ``active``: the engines whose adapter the engine table can launch, or None to leave that check to
  ``routes.json`` (the daemon loads an entry only for an engine it has an adapter for).

LAUNCH-CHECK's detection snapshot replaces the first condition (the route part) when it lands.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from recursion_core.routing.config import ENGINES, RECURSION_LOOP, Config

BREAKER_STATES = ("closed", "half_open", "open", "unknown")
KEY_STATES = ("ready", "absent", "not_opted_in")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _code(value: object, fallback: str) -> str:
    """A refusal as a typed code: the word itself when it is one, otherwise ``fallback``."""
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def _connection(entry: object) -> tuple[str, str] | None:
    route = entry.get("route") if isinstance(entry, Mapping) else None
    conn = route.get("connection") if isinstance(route, Mapping) else None
    if not isinstance(conn, Mapping):
        return None
    kind, ident = conn.get("kind"), conn.get("id")
    if not (isinstance(kind, str) and kind and isinstance(ident, str) and ident):
        return None
    return kind, ident


def _engine_reason(engine: str, entry: object, *, route_refusal, breaker_state, key_state, active) -> str | None:
    if active is not None and engine not in active:
        return "engine_not_active"
    if entry is None:
        return "route_not_configured"
    if route_refusal is not None:
        refused = route_refusal(entry)
        if refused is not None:
            return _code(refused, "route_refused")
    conn = _connection(entry)
    if conn is None:
        return "route_no_connection"
    kind, ident = conn
    if breaker_state is not None:
        state = breaker_state(f"{kind}:{ident}")
        if state not in BREAKER_STATES:
            return "breaker_unknown"
        if state == "open":
            return "breaker_open"
        if state == "unknown":
            return "breaker_unknown"
    if engine == RECURSION_LOOP and kind != "api_key":
        return "recursion_loop_needs_key"
    if kind == "api_key":
        if key_state is None:
            return "key_state_unknown"
        state = key_state(ident)
        if state == "absent":
            return "key_absent"
        if state == "not_opted_in":
            return "key_not_opted_in"
        if state != "ready":
            # A key source that answers anything but the three words (a source of the caller's, so it may)
            # is not ready: never routable on a word it did not mean. Same as ``state not in KEY_STATES`` here.
            return "key_state_unknown"
    return None


def detect(config: Config, *, route_refusal: Callable | None = None, breaker_state: Callable | None = None,
           key_state: Callable | None = None, active: Iterable[str] | None = None) -> dict:
    """The detection snapshot for ``config``'s machine. A source that raises an ``Exception`` makes its engine
    unroutable (``detection_source_failed``); a ``BaseException`` such as ``KeyboardInterrupt`` is not caught
    and stops the caller.

    A source left out (None) is not consulted, except the key state: a key route with no key source is
    ``key_state_unknown``, never routable, because spending needs a positive opt-in.
    """
    if isinstance(active, (str, bytes)):
        raise TypeError("active is an iterable of engine ids, not one string")
    active_set = None if active is None else frozenset(active)
    engines: dict[str, dict] = {}
    for engine in ENGINES:
        entry = config.routes.get(engine)
        try:
            reason = _engine_reason(engine, entry, route_refusal=route_refusal, breaker_state=breaker_state,
                                    key_state=key_state, active=active_set)
        except Exception:  # noqa: BLE001 - a source that fails makes its engine unroutable, never routable
            reason = "detection_source_failed"
        engines[engine] = {"routable": reason is None, "reason": reason}
    return {"engines": engines}


def breaker_view(breaker) -> Callable[[str], str]:
    """``connection_key -> state`` from the daemon's breaker, read as ``route.list`` reads it.

    ``breaker.state(key)`` is ``closed``, ``open`` or ``half_open``; ``breaker.tripped(key)``, where the
    breaker has it (the durable one), is True, False, or None when its store could not be read. A trip
    kept in the store reads ``open`` while the in-memory state is still ``closed``; an unreadable store
    reads ``unknown``.
    """
    def state(key: str) -> str:
        current = breaker.state(key)
        tripped_of = getattr(breaker, "tripped", None)
        if tripped_of is not None:
            tripped = tripped_of(key)
            if tripped is None:
                return "unknown"
            if tripped and current == "closed":
                return "open"
        return current if current in BREAKER_STATES else "unknown"
    return state


def daemon_route_refusal(breaker=None) -> Callable[[Mapping], str | None]:
    """The daemon's own pre-launch refusals of a loaded route entry, as a typed code (Linux only)."""
    from recursion_core.daemon.control_adapter import route_unbindable, route_unsupported
    from recursion_core.routes import contract

    def refusal(entry: Mapping) -> str | None:
        unbindable = route_unbindable(entry, breaker)
        if unbindable is not None:
            return unbindable[0]
        if route_unsupported(entry) is not None:
            return contract.LAUNCH_REFUSED
        return None
    return refusal


def key_state_from(metadata: Mapping, *, opted_in: Callable[[Mapping], bool]) -> Callable[[str], str]:
    """``key_id -> "ready" | "absent" | "not_opted_in"`` from a key store's metadata (key id -> mapping).

    ``opted_in(meta)`` reads the store's own opt-in field; the key store owns its name and meaning, so the
    caller passes the reader rather than this module guessing it.
    """
    def state(key_id: str) -> str:
        meta = metadata.get(key_id)
        if not isinstance(meta, Mapping):
            return "absent"
        return "ready" if opted_in(meta) is True else "not_opted_in"
    return state
