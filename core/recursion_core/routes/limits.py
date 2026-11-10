"""What happens when a route hits a limit: shown, user-configurable, never a silent switch (Nov 8 briefing).

Lucas's Nov 8 briefing: "when a subscription hits a limit, show it clearly and let the user switch. Model
or provider per request, what happens at a limit, and fallback order are all user-configurable. The
default is show and ask, never a silent switch."

* The model, engine and connection are already chosen per request (``route``).
* ``route.limits`` (or the user's default passed to ``RouteService``) sets ``on_limit``:

  - ``ask`` (default): nothing else runs; the result carries a ``limit_notice`` with the connection,
    scope, reset time and the user's choices (wait, each configured fallback route, cancel).
  - ``wait``: nothing else runs; the notice says the request waits for ``reset_at`` (the surface or
    daemon re-submits then; no scheduler lives here).
  - ``fallback``: the service tries the user's own ``fallback`` routes in the user's order. Every
    switch is visible: a ``fallback`` event and ``limit_notice.switched_to`` on the result.

* A fallback route is only ever one the user listed. Nothing here picks a route, model or provider
  by itself, and an empty fallback list with ``on_limit = "fallback"`` is refused as invalid.
"""

from __future__ import annotations

from collections.abc import Mapping

from recursion_core.routes import contract as c

ON_LIMIT = ("ask", "wait", "fallback")
DEFAULT_LIMITS = {"on_limit": "ask", "fallback": []}
MAX_FALLBACK = 8
# Results that mean "this connection is at a limit now": a typed limit, a breaker still open on it, or
# its half-open probe still running (the connection has not shown it recovered). SHORT_FAULT is one model
# for one tenant for a minute (grill Q23), not a subscription limit, so it is not offered as a switch
# point, including the breaker's refusal while that short fence holds.
LIMIT_CODES = (c.PLAN_LIMIT, c.BREAKER_OPEN, c.PROBE_IN_FLIGHT)
_ROUTE_KEYS = {"engine", "model", "connection", "profile", "home", "bare", "api_key_helper", "billing_ack", "wrapper"}


def validate_limits(limits: object) -> dict:
    """Return a normalised copy or raise ``RouteError(REQUEST_INVALID)``."""

    if not isinstance(limits, Mapping) or set(limits) - {"on_limit", "fallback"}:
        raise c.RouteError(c.REQUEST_INVALID, "route.limits")
    on_limit = limits.get("on_limit", "ask")
    fallback = limits.get("fallback", [])
    if on_limit not in ON_LIMIT:
        raise c.RouteError(c.REQUEST_INVALID, "route.limits.on_limit")
    if not isinstance(fallback, list) or len(fallback) > MAX_FALLBACK:
        raise c.RouteError(c.REQUEST_INVALID, "route.limits.fallback")
    if on_limit == "fallback" and not fallback:
        raise c.RouteError(c.REQUEST_INVALID, "route.limits.fallback is empty: nothing the user chose to switch to")
    routes = []
    for entry in fallback:
        if not isinstance(entry, Mapping) or set(entry) - _ROUTE_KEYS or not valid_wrapper(entry.get("wrapper")):
            raise c.RouteError(c.REQUEST_INVALID, "route.limits.fallback entry")
        probe = {"contract": c.CONTRACT_VERSION, "request_id": "limits", "tenant": "limits", "prompt": "-",
                 "deadline_s": 1.0, "route": dict(entry)}
        routes.append(c.validate_request(probe)["route"])  # same rules as a primary route
    return {"on_limit": on_limit, "fallback": routes}


def describe(route: Mapping) -> dict:
    """What the user sees for a route: engine, model and connection key. Never a credential value."""

    return {"engine": route["engine"], "model": route["model"], "connection": c.connection_key(route)}


def notice(result: Mapping, route: Mapping, limits: Mapping) -> dict | None:
    """The visible limit notice for a result, or None when the result is not a limit."""

    if result.get("code") not in LIMIT_CODES:
        return None
    signal = result.get("limit") or {}
    vendor = signal.get("reset_at") if isinstance(signal, Mapping) else None
    hold = result.get("retry_at")
    # Wait for the breaker hold. A vendor reset that is earlier than the open window is still on
    # the signal; advertising it would tell the user to retry while the breaker still refuses.
    reset_at = hold if isinstance(hold, (int, float)) and not isinstance(hold, bool) else vendor
    here = describe(route)
    choices = [{"action": "wait", "until": reset_at}]
    choices += [{"action": "switch", "route": describe(r)} for r in limits["fallback"]]
    choices.append({"action": "cancel"})
    return {
        "message": f"{here['connection']} ({here['engine']}, {here['model']}) is at a usage limit",
        "route": here,
        "code": result["code"],
        "scope": signal.get("scope", _refusal_scope(result)),
        "source": signal.get("source", result.get("detail", "")),
        "reset_at": reset_at,
        "on_limit": limits["on_limit"],
        "choices": choices,
        "switched_to": None,
        "attempts": [here],
    }


def valid_wrapper(value: object) -> bool:
    """A fallback may carry the Pi/Hermes pin: the in-process object, or the same fields as JSON."""

    if value is None:
        return True
    if isinstance(value, Mapping):
        pin = value.get("pin")
        files = pin.get("package_files") if isinstance(pin, Mapping) else None
        return (
            isinstance(value.get("launcher_path"), str) and bool(value["launcher_path"])
            and isinstance(value.get("install_root"), str) and bool(value["install_root"])
            and isinstance(pin, Mapping)
            and isinstance(pin.get("launcher_sha256"), str) and bool(pin["launcher_sha256"])
            and isinstance(files, list)
            and all(
                isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(part, str) and part for part in item)
                for item in files
            )
        )
    pin = getattr(value, "pin", None)
    return (
        isinstance(getattr(value, "launcher_path", None), str) and bool(value.launcher_path)
        and isinstance(getattr(value, "install_root", None), str) and bool(value.install_root)
        and isinstance(getattr(pin, "launcher_sha256", None), str) and bool(pin.launcher_sha256)
        and isinstance(getattr(pin, "package_files", None), tuple)
    )


def _refusal_scope(result: Mapping) -> str:
    """A breaker refusal names the key it hit in ``detail``: a model bucket key is model scope."""

    detail = result.get("detail")
    return "model" if isinstance(detail, str) and "#model:" in detail else "connection"
