"""Route service: the protocol entry point (CSL-797, candidate). See README.md.

``RouteService.handle(request_dict) -> result_dict`` is the only call a surface or the daemon makes:
validate, ask the connection breaker, run the engine adapter, book the outcome. Every answer is a
contract result with a typed code, so the same tests can drive a later port through JSON alone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes import limits as lim
from recursion_core.routes.base import RouteAdapter
from recursion_core.routes.breaker import ConnectionBreaker


class RouteService:
    def __init__(self, adapters: Mapping[str, RouteAdapter], breaker: ConnectionBreaker,
                 clock: Callable[[], float], *, limits: Mapping | None = None) -> None:
        self._adapters, self._breaker, self._clock = dict(adapters), breaker, clock
        # The user's default limit behaviour; a request's own route.limits overrides it. Default: ask.
        self._limits = lim.validate_limits(limits if limits is not None else lim.DEFAULT_LIMITS)
        self._gate = threading.Lock()
        self._closed = False
        self._live: set = set()
        self._inflight: set[str] = set()
        for adapter in self._adapters.values():
            adapter._drop_process = self._drop_process
            adapter._launcher = self._wrap_launcher(adapter._launcher)

    def shutdown(self) -> None:
        """Refuse later requests and fence every child this service still has."""

        with self._gate:
            self._closed = True
            live = list(self._live)
        for proc in live:
            try:
                proc.fence("shutdown")
            except Exception:
                pass
            self._drop_process(proc)

    def _wrap_launcher(self, inner):
        def launch(argv, **kwargs):
            with self._gate:
                closed = self._closed
            if closed:
                raise c.RouteError(c.LAUNCH_REFUSED, "shutdown")
            proc = inner(argv, **kwargs)
            with self._gate:
                if self._closed:
                    try:
                        proc.fence("shutdown")
                    except Exception:
                        pass
                    raise c.RouteError(c.LAUNCH_REFUSED, "shutdown")
                self._live.add(proc)
            return proc

        return launch

    def _drop_process(self, proc) -> None:
        with self._gate:
            self._live.discard(proc)

    def handle(self, request: object) -> dict:
        rid = request.get("request_id") if isinstance(request, Mapping) else None
        rid = rid if isinstance(rid, str) else ""
        try:
            req = c.validate_request(request)
            limits = lim.validate_limits(req["route"]["limits"]) if "limits" in req["route"] else self._limits
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        with self._gate:
            if self._closed:
                return c.result(req["request_id"], c.LAUNCH_REFUSED, detail="shutdown")
            if req["request_id"] in self._inflight:
                # A second run of the same id would book a second attempt and launch another child.
                return c.result(req["request_id"], c.LAUNCH_REFUSED, detail="request in flight")
            self._inflight.add(req["request_id"])
        try:
            return self._handle_locked(req, limits)
        finally:
            with self._gate:
                self._inflight.discard(req["request_id"])

    def _handle_locked(self, req: dict, limits: Mapping) -> dict:
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
        # engine:kind:id, not kind:id. Two engines labelled with the same id are different meters.
        connection, model, tenant = c.breaker_key(route), route["model"], req["tenant"]
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
