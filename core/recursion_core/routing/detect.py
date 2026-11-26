"""Detection: which engines a routing decision may use, built from what the daemon already knows.

An engine is ROUTABLE when all of these hold:

* it has a loaded route (``routes.json``, as ``load_routes`` gave it) that the daemon would not refuse
  before launch (``route_unsupported``, ``route_unbindable``: the same functions ``route.list`` and the
  starter read);
* its connection's breaker is not open (the breaker's own read-only view, read the way ``route.list``
  reads it: a trip kept in the store from an earlier daemon counts as open, and a store that cannot be
  read is ``breaker_unknown``, because a start on it is refused until the store reads; a breaker whose
  ``state()`` raises is ``detection_source_failed``, like any source that raises: unroutable either way);
* for a key route (``connection.kind`` ``api_key``) and for ``recursion_loop``, its key is present in the
  key store and opted in to spending (a key route with no opt-in never spends).

The answer is the snapshot ``router.decide`` takes: ``{"engines": {engine: {"routable", "reason"}}}``, one
entry for every engine the config may name, each reason a typed code. Nothing here launches, writes,
reserves or emits anything; the sources are read once each per engine.

The sources are passed in, so that this module depends on none of them and its tests need no daemon:

* ``route_refusal(entry) -> None or a code``: why the daemon would refuse the entry before launch.
  ``daemon_route_refusal(breaker)`` gives the daemon's own (it imports the daemon, Linux only);
* ``breaker_state(connection_key) -> "closed" | "half_open" | "open" | "unknown"``: ``breaker_view``
  turns the daemon's breaker into one;
* ``key_state(connection_id) -> "ready" | "absent" | "not_opted_in"``: the key store answers it
  (``key_state_from`` builds one from the store's metadata); a test passes a plain function;
* ``active``: the engines whose adapter the engine table can launch.

``NOT_CONSULTED``, passed for a source, leaves that source out on purpose: no daemon refusal, no breaker, or
no engine table is read (the daemon loads an entry only for an engine it has an adapter for, so ``routes.json``
still holds that check). It is the one road to that reading. A source passed as None is read narrow, never as
left out, because None is what a caller writes for an argument it has nothing for: no engine is routable on a
refusal, a breaker or an engine table passed as None (``route_refused``, ``breaker_unknown``,
``engine_not_active``). A key route is never routable without a key source, whatever is passed for it
(``key_state_unknown``): spending needs a positive opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from recursion_core.routing.config import ENGINES, RECURSION_LOOP, Config

BREAKER_STATES = ("closed", "half_open", "open", "unknown")
KEY_STATES = ("ready", "absent", "not_opted_in")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class _NotConsulted:
    """The type of ``NOT_CONSULTED``: one value, compared by identity."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "NOT_CONSULTED"


NOT_CONSULTED = _NotConsulted()  # a source left out on purpose: the one road to not reading it


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
    if active is not NOT_CONSULTED and engine not in active:  # None reached here as no engine active
        return "engine_not_active"
    if entry is None:
        return "route_not_configured"
    if route_refusal is None:
        return "route_refused"  # passed as None: the daemon's refusal was not read, so the route is not taken
    if route_refusal is not NOT_CONSULTED:
        refused = route_refusal(entry)
        if refused is not None:
            return _code(refused, "route_refused")
    conn = _connection(entry)
    if conn is None:
        return "route_no_connection"
    kind, ident = conn
    if breaker_state is None:
        return "breaker_unknown"  # passed as None: the breaker was not read, so the connection is not taken
    if breaker_state is not NOT_CONSULTED:
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
        if key_state is None or key_state is NOT_CONSULTED:
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


def detect(config: Config, *, route_refusal: Callable | None, breaker_state: Callable | None,
           key_state: Callable | None, active: Iterable[str] | None) -> dict:
    """The detection snapshot for ``config``'s machine. A source that raises an ``Exception`` makes its engine
    unroutable (``detection_source_failed``); a ``BaseException`` such as ``KeyboardInterrupt`` is not caught
    and stops the caller.

    Every source is a keyword the caller passes, none of them defaulted, so that no call leaves a check out by
    leaving an argument out (a call without one raises ``TypeError`` naming it), nor by writing None: a
    refusal, a breaker or an engine table passed as None leaves no engine routable (``route_refused``,
    ``breaker_unknown``, ``engine_not_active``). Only ``NOT_CONSULTED`` leaves one of those out on purpose.
    A key route with no key source (None or ``NOT_CONSULTED``) is ``key_state_unknown``, never routable,
    because spending needs a positive opt-in.

    ``active`` is not a source: it is the caller's own iterable of engine ids (the composition's engine
    table), read once. A single string is refused (TypeError); an iterable that raises, or a value that is
    not iterable, raises as any wrong argument does.
    """
    if isinstance(active, (str, bytes)):
        raise TypeError("active is an iterable of engine ids, not one string")
    active_set = active if active is NOT_CONSULTED else frozenset() if active is None else frozenset(active)
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

    A trip kept by an earlier daemon therefore reads ``open`` for as long as the breaker's in-memory state
    is ``closed``, that is, until the breaker restores the trip. The cooldown of such a trip begins only
    when the breaker restores it (the durable breaker does so when it first admits a request on that
    connection); this function only reads, and starts no cooldown. Whatever wires detection into the start
    path must therefore have the breaker restore the stored trip of every configured connection before it
    relies on this view to let a connection through. Without that, a connection tripped before a restart
    reads ``open`` and stays refused, because no cooldown ever starts, until the breaker restores the trip.
    A start that reaches the breaker's admission restores it (and is itself refused); the trip clears in
    the store only through a successful probe after the cooldown.
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


def daemon_route_refusal(breaker) -> Callable[[Mapping], str | None]:
    """The daemon's own pre-launch refusals of a loaded route entry, as a typed code (Linux only).

    ``breaker`` is the daemon's breaker, or ``NOT_CONSULTED`` from a caller that has none, said on purpose: then
    the breaker's own refusals (a connection or a model it cannot key) are not read. It is not defaulted, so a
    call cannot leave those out by forgetting it, nor by writing None: a breaker passed as None gives a refusal
    that refuses every entry ``source_not_given``, without importing the daemon.

    Where the daemon cannot be imported (it imports on Linux only), this function raises ``ImportError`` for
    any breaker but None; a caller there has no daemon refusal to pass, and passes ``NOT_CONSULTED`` as
    ``route_refusal`` to ``detect`` and to ``router.next_after_limit``."""
    if breaker is None:
        return lambda entry: "source_not_given"
    from recursion_core.daemon.control_adapter import route_unbindable, route_unsupported
    from recursion_core.routes import contract

    keyed_by = None if breaker is NOT_CONSULTED else breaker  # the daemon's own reading of no breaker

    def refusal(entry: Mapping) -> str | None:
        unbindable = route_unbindable(entry, keyed_by)
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
