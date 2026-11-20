"""Route service: the protocol entry point (CSL-797, candidate). See README.md.

``RouteService.handle(request_dict) -> result_dict`` is the only call a surface or the daemon makes:
validate, ask the connection breaker, run the engine adapter, book the outcome. Every answer is a
contract result with a typed code, so the same tests can drive a later port through JSON alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes import limits as lim
from recursion_core.routes.base import RouteAdapter
from recursion_core.routes.breaker import ConnectionBreaker


class RouteService:
    def __init__(self, adapters: Mapping[str, RouteAdapter], breaker: ConnectionBreaker,
                 clock: Callable[[], float], *, limits: Mapping | None = None, engines: object = None) -> None:
        self._adapters, self._breaker, self._clock = dict(adapters), breaker, clock
        # What the composition registered (seam S3): its engine table when given, otherwise the engines this
        # service has an adapter for, which the daemon builds from that same table. validate_request asks
        # for the names at each call (contract.engine_names); a bad name fails here, at composition.
        self._engines = c.registered_snapshot(engines) if engines is not None else tuple(self._adapters)
        c.engine_names(self._engines)
        # The user's default limit behaviour; a request's own route.limits overrides it. Default: ask.
        self._limits = lim.validate_limits(limits if limits is not None else lim.DEFAULT_LIMITS, engines=self._engines)

    def handle(self, request: object) -> dict:
        rid = request.get("request_id") if isinstance(request, Mapping) else None
        rid = rid if isinstance(rid, str) else ""
        try:
            req = c.validate_request(request, engines=self._engines)
            limits = (lim.validate_limits(req["route"]["limits"], engines=self._engines) if "limits" in req["route"]
                      else self._limits)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        except ValueError as err:
            # A live table can gain a name that is not an engine name after composition. The request
            # answers a typed result. Composition still raises ValueError when the bad name is already
            # present (engine_names). Any other ValueError still leaves.
            if not str(err).startswith("registered engine name "):
                raise
            return c.result(rid, c.REQUEST_INVALID, detail="registered engine name")
        # One absolute deadline for the request, shared by every fallback. A later attempt does not
        # get a fresh deadline_s (11.2: the request stops at the deadline the caller set).
        deadline_at = self._clock() + float(req["deadline_s"])
        res = self._run(req, deadline_at)
        notice = lim.notice(res, req["route"], limits)
        if notice is None:
            return res
        res["limit_notice"] = notice
        if limits["on_limit"] != "fallback":
            return res  # ask / wait: shown to the user; nothing else runs
        trail: list[dict] = []
        for route in limits["fallback"]:  # only the user's own routes, in the user's order
            if self._clock() >= deadline_at:
                break  # no time left for another attempt; the limit result stands
            nxt = dict(req, route=dict(route))
            other = self._run(nxt, deadline_at)
            notice["attempts"].append(lim.describe(route))
            event = c.event("fallback", "recursion.limits", {
                "from": notice["route"]["connection"], "to": c.connection_key(route), "model": route["model"],
                "code": other["code"]})
            if other["code"] not in lim.LIMIT_CODES:
                notice["switched_to"] = lim.describe(route)
                other["events"] = [*trail, event, *other["events"]]
                other["limit_notice"] = notice
                return other
            trail.append(event)
            res["events"].append(event)
        return res

    def _run(self, req: dict, deadline_at: float) -> dict:
        rid = req["request_id"]
        route = req["route"]
        adapter = self._adapters.get(route["engine"])
        if adapter is None:
            return c.result(rid, c.ENGINE_UNKNOWN, detail=f"no adapter for {route['engine']}")
        connection, model, tenant = c.connection_key(route), route["model"], req["tenant"]
        remaining = deadline_at - self._clock()
        if remaining <= 0:
            return c.result(rid, c.DEADLINE_EXCEEDED, detail="recursion deadline")
        # The probe lease is bounded by the time still left (plus grace), so a probe still running
        # inside its deadline is never joined by a second probe on the same connection (11.2, C1-18).
        admit = self._breaker.admit(connection, model, tenant, deadline_s=remaining)
        if not admit["admitted"]:
            return c.result(rid, admit["code"], detail=admit["key"], retry_at=admit["retry_at"])
        try:
            res = adapter.run(req, deadline_at)
        except BaseException:
            self._breaker.record(connection, model, tenant, {"code": c.ENGINE_ERROR}, admit["probe"])
            raise
        res["breaker_effect"] = self._breaker.record(connection, model, tenant, res, admit["probe"])
        return res
