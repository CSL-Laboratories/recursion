"""Pi, Hermes and OpenCode routes through ``subscription_routes`` (B3, CSL-786 reused; CSL-797 candidate).

Every launch for these engines goes through ``subscription_routes.guard_then_spawn``, so
``check_route`` refuses a Claude-subscription provider (Q12: the Claude subscription is used only
inside Claude Code) before the launcher is ever called. The refusal comes back through the route
contract as ``subscription_route_refused`` with the policy's typed cause in ``detail``.

The engines' own stdio protocols are not built here: ``driver`` is injected and owns the run once the
guard has passed. No running core wires a driver yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.base import RouteAdapter
from recursion_core.subscription_routes import SAFE_ENV, SubscriptionRouteRefused, guard_then_spawn

# driver(process, request, deadline_at, events) -> contract result
Driver = Callable[..., dict]


class ProfileRouteAdapter(RouteAdapter):
    def __init__(self, engine: str, *args, argv: Callable[[dict], list[str]], driver: Driver,
                 operator_home: str | None = None, **kwargs) -> None:
        if engine not in c.PROFILE_ENGINES:
            raise ValueError(engine)
        super().__init__(*args, **kwargs)
        self.engine, self._argv, self._driver, self._operator_home = engine, argv, driver, operator_home

    def run(self, request: dict, deadline_at: float) -> dict:
        rid, route = request["request_id"], request["route"]
        home = route.get("home")
        conn = route["connection"]
        env = {name: self._parent_env[name] for name in SAFE_ENV if name in self._parent_env and name != "HOME"}
        if isinstance(home, str):
            env["HOME"] = home
        if conn["kind"] == "api_key":
            try:
                value = self._credentials(conn["id"], conn["credential_env"])
            except KeyError:
                value = ""
            if value:
                env[conn["credential_env"]] = value
        try:
            _decision, process = guard_then_spawn(
                self._launcher, self._argv(request), self.engine, dict(route["profile"]), env,
                route_home=home if isinstance(home, str) else "", parent_env=self._parent_env,
                operator_home=self._operator_home,
            )
        except SubscriptionRouteRefused as err:
            return c.result(rid, c.SUBSCRIPTION_ROUTE_REFUSED, detail=err.cause)
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        events: list[dict] = []
        return self._driver(process, request, deadline_at, events)


def profile_adapters(launcher, clock, credentials: Callable[[str, str], str], parent_env: Mapping[str, str], *,
                     argv: Callable[[dict], list[str]], driver: Driver,
                     operator_home: str | None = None) -> dict[str, ProfileRouteAdapter]:
    return {
        engine: ProfileRouteAdapter(engine, launcher, clock, credentials, parent_env, argv=argv, driver=driver,
                                    operator_home=operator_home)
        for engine in c.PROFILE_ENGINES
    }
