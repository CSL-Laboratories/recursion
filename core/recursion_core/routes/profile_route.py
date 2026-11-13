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
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol, stop_child
from recursion_core.subscription_routes import ENGINES, SAFE_ENV, SubscriptionRouteRefused, guard_then_spawn

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
            # The route's key goes only under a name subscription_routes lists for this engine and the profile
            # selects; a key custody does not hold refuses here instead of launching keyless (the engine would
            # fall back to whatever login its home holds and bill differently from what the route says).
            name = conn["credential_env"]
            allowed = {n for p in ENGINES[self.engine].providers.values() for n in p.credential_env}
            if name not in allowed:
                return c.result(rid, c.LAUNCH_REFUSED, detail=f"credential_env {name} not allowed for {self.engine}")
            if route["profile"].get("credential_env") != name:
                return c.result(rid, c.LAUNCH_REFUSED, detail="credential_env differs from the profile's")
            try:
                value = self._credentials(conn["id"], name)
            except KeyError:
                value = ""
            if not isinstance(value, str) or not value:
                return c.result(rid, c.LAUNCH_REFUSED, detail=f"no credential for {conn['id']}")
            env[name] = value
        try:
            # Pi and Hermes refuse without the wrapper pin CSL-786 measures. OpenCode leaves it unset.
            _decision, process = guard_then_spawn(
                self._launcher, self._argv(request), self.engine, dict(route["profile"]), env,
                route_home=home if isinstance(home, str) else "", parent_env=self._parent_env,
                operator_home=self._operator_home, wrapper=route.get("wrapper"),
            )
        except SubscriptionRouteRefused as err:
            return c.result(rid, c.SUBSCRIPTION_ROUTE_REFUSED, detail=err.cause)
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        events: list[dict] = []
        try:
            return self._driver(process, request, deadline_at, events)
        except Deadline:
            stop_child(process, "deadline")
            return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
        except _Protocol as err:
            stop_child(process, "protocol_error")
            return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
        except OSError as err:
            stop_child(process, "io_error")
            return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
        except Exception as err:
            # Same post-launch rule as RouteAdapter: fence the child. The detail names the type only.
            stop_child(process, "engine_error")
            return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"unexpected: {type(err).__name__}")


def profile_adapters(launcher, clock, credentials: Callable[[str, str], str], parent_env: Mapping[str, str], *,
                     argv: Callable[[dict], list[str]], driver: Driver,
                     operator_home: str | None = None) -> dict[str, ProfileRouteAdapter]:
    return {
        engine: ProfileRouteAdapter(engine, launcher, clock, credentials, parent_env, argv=argv, driver=driver,
                                    operator_home=operator_home)
        for engine in c.PROFILE_ENGINES
    }
