"""Route service: the protocol entry point (CSL-797, candidate). See README.md.

``RouteService.handle(request_dict) -> result_dict`` is the only call a surface or the daemon makes:
validate, ask the connection breaker, run the engine adapter, book the outcome. Every answer is a
contract result with a typed code, so the same tests can drive a later port through JSON alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.base import RouteAdapter
from recursion_core.routes.breaker import ConnectionBreaker


class RouteService:
    def __init__(self, adapters: Mapping[str, RouteAdapter], breaker: ConnectionBreaker,
                 clock: Callable[[], float]) -> None:
        self._adapters, self._breaker, self._clock = dict(adapters), breaker, clock

    def handle(self, request: object) -> dict:
        rid = request.get("request_id") if isinstance(request, Mapping) else None
        rid = rid if isinstance(rid, str) else ""
        try:
            req = c.validate_request(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        route = req["route"]
        adapter = self._adapters.get(route["engine"])
        if adapter is None:
            return c.result(rid, c.ENGINE_UNKNOWN, detail=f"no adapter for {route['engine']}")
        connection, model, tenant = c.connection_key(route), route["model"], req["tenant"]
        admit = self._breaker.admit(connection, model, tenant)
        if not admit["admitted"]:
            return c.result(rid, admit["code"], detail=admit["key"], retry_at=admit["retry_at"])
        deadline_at = self._clock() + float(req["deadline_s"])
        try:
            res = adapter.run(req, deadline_at)
        except BaseException:
            self._breaker.record(connection, model, tenant, {"code": c.ENGINE_ERROR}, admit["probe"])
            raise
        res["breaker_effect"] = self._breaker.record(connection, model, tenant, res, admit["probe"])
        return res
