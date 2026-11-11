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
from recursion_core.routes import limits as lim
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol
from recursion_core.subscription_routes import (
    ENGINES, PackagePin, SAFE_ENV, SubscriptionRouteRefused, WrapperInstall, guard_then_spawn,
)

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
            # A JSON request carries the pin as a dict; the guard still wants the measured object.
            _decision, process = guard_then_spawn(
                self._launcher, self._argv(request), self.engine, dict(route["profile"]), env,
                route_home=home if isinstance(home, str) else "", parent_env=self._parent_env,
                operator_home=self._operator_home, wrapper=_coerce_wrapper(route.get("wrapper")),
            )
        except SubscriptionRouteRefused as err:
            return c.result(rid, c.SUBSCRIPTION_ROUTE_REFUSED, detail=err.cause)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        events: list[dict] = []
        try:
            try:
                return self._driver(process, request, deadline_at, events)
            except Deadline as err:
                self._abort(process, "deadline", err)
                return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
            except _Protocol as err:
                self._abort(process, "protocol_error", err)
                return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
            except OSError as err:
                self._abort(process, "io_error", err)
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
            except Exception as err:
                self._abort(process, "internal", err)
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=type(err).__name__)
            except BaseException as err:
                self._abort(process, "cancelled", err)
                raise
        finally:
            drop = getattr(self, "_drop_process", None)
            if drop is not None:
                drop(process)


def _coerce_wrapper(wrapper: object) -> object:
    """Accept the in-process pin or the JSON shape of the same fields.

    A wrapper that was sent but is not that shape is ``request_invalid``. An absent wrapper is
    left absent so the guard can still say ``wrapper_pin_missing``.
    """

    if wrapper is None or isinstance(wrapper, WrapperInstall):
        return wrapper
    if not lim.valid_wrapper(wrapper):
        raise c.RouteError(c.REQUEST_INVALID, "route.wrapper")
    pin = wrapper["pin"]
    files = tuple((path, digest) for path, digest in pin["package_files"])
    return WrapperInstall(wrapper["launcher_path"], wrapper["install_root"], PackagePin(pin["launcher_sha256"], files))


def profile_adapters(launcher, clock, credentials: Callable[[str, str], str], parent_env: Mapping[str, str], *,
                     argv: Callable[[dict], list[str]], driver: Driver,
                     operator_home: str | None = None) -> dict[str, ProfileRouteAdapter]:
    return {
        engine: ProfileRouteAdapter(engine, launcher, clock, credentials, parent_env, argv=argv, driver=driver,
                                    operator_home=operator_home)
        for engine in c.PROFILE_ENGINES
    }
