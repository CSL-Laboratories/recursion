"""Pi, Hermes and OpenCode routes through ``subscription_routes`` (B3, CSL-786 reused; CSL-797 candidate).

Every launch for these engines goes through ``subscription_routes.guard_then_spawn``, so
``check_route`` refuses a Claude-subscription provider (Q12: the Claude subscription is used only
inside Claude Code) before the launcher is ever called. The refusal comes back through the route
contract as ``subscription_route_refused`` with the policy's typed cause in ``detail``.

``allow_inside_operator_home`` carries lane INSTALL's containment setting as a bool (True for its value
``inside_operator_home``). Nothing in this tree reads that setting, and the launcher keeps its own home: the
caller that builds this adapter passes the bool. False, the default, refuses a route home inside the
operator's home (``route_home_is_operator_home``); True accepts there only the engine's own home where the
launcher keeps it (``subscription_routes.check_route``).

The engines' own stdio protocols are not built here: ``driver`` is injected and owns the run once the
guard has passed. No running core wires a driver yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol
from recursion_core.subscription_routes import ENGINES, SAFE_ENV, SubscriptionRouteRefused, guard_then_spawn

# driver(process, request, deadline_at, events) -> contract result
Driver = Callable[..., dict]

# The fence reason for a started child whose run leaves ``ProfileRouteAdapter.run`` by an exception that none of
# the typed exits answers. This and the three names below are the same words and the same rule as lane
# DRIVER-CC's draft #420 gives ``routes/base.py`` (its head 5beaa221), which is not on master yet; this module keeps
# its own copies until it is, and then imports them.
UNEXPECTED_FENCE = "unexpected"

# The attribute an exception leaving ``ProfileRouteAdapter.run`` carries when no fence of the started child
# succeeded: a tuple of the fence failures, oldest first. Its presence means the child may still be running.
CLEANUP_FAILURES = "cleanup_failures"
RUNNING_NOTE = "no fence of the started child succeeded: it may still be running"


class ProfileRouteAdapter(RouteAdapter):
    def __init__(self, engine: str, *args, argv: Callable[[dict], list[str]], driver: Driver,
                 operator_home: str | None = None, allow_inside_operator_home: bool = False, **kwargs) -> None:
        if engine not in c.PROFILE_ENGINES:
            raise ValueError(engine)
        super().__init__(*args, **kwargs)
        self.engine, self._argv, self._driver, self._operator_home = engine, argv, driver, operator_home
        self._allow_inside_operator_home = allow_inside_operator_home

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
        # STARTED CHILDREN ARE FENCED ON EVERY WAY OUT OF THIS METHOD (the rule of lane DRIVER-CC's #420 for
        # RouteAdapter.run, at its head 5beaa221; this override of ``run`` owns its own cleanup). Once the guard has
        # returned a process:
        # - A typed exit (deadline, protocol error, engine io) fences with its own reason and answers.
        # - Any other exception, BaseException included, fences with UNEXPECTED_FENCE, and the same exception
        #   object leaves.
        # - If a typed exit's fence raises, the ORIGINAL typed exception leaves instead of the answer; the fence is
        #   tried once more as UNEXPECTED_FENCE, and the first failure is noted on the original.
        # - A fence failure never replaces the exception that leaves. When no fence attempt succeeded, the
        #   exception that leaves carries the failures in CLEANUP_FAILURES (and a note): the child may still be
        #   running, so a caller that finds the attribute must not treat the exit as clean.
        # What this method cannot cover is an asynchronous exception (a signal handler's, or a MemoryError)
        # delivered between the guard's return and the store into ``process``, or inside the outer handler before
        # its fence has run; the process then exists only in the launcher's own record of the run. One delivered
        # after a typed fence succeeded and before ``fenced`` is set fences twice, which is harmless; setting the
        # flag first would leave a child unfenced instead. A driver that fences its own child and then raises is
        # fenced once more as UNEXPECTED_FENCE: ``fenced`` knows only this method's own fences.
        process = None
        fenced = False
        try:
            try:
                # Pi and Hermes refuse without the wrapper pin CSL-786 measures. OpenCode leaves it unset.
                _decision, process = guard_then_spawn(
                    self._launcher, self._argv(request), self.engine, dict(route["profile"]), env,
                    route_home=home if isinstance(home, str) else "", parent_env=self._parent_env,
                    operator_home=self._operator_home, wrapper=route.get("wrapper"),
                    allow_inside_operator_home=self._allow_inside_operator_home,
                )
            except SubscriptionRouteRefused as err:
                return c.result(rid, c.SUBSCRIPTION_ROUTE_REFUSED, detail=err.cause)
            except c.RouteError as err:
                # The launcher raised before a process was stored. Nothing was started, so the
                # typed refusal is a result, the same as RouteAdapter.run.
                return c.result(rid, err.code, detail=err.detail)
            except OSError as err:
                return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
            events: list[dict] = []
            try:
                return self._driver(process, request, deadline_at, events)
            except Deadline as err:
                done = _typed_fence(process, "deadline", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
            except _Protocol as err:
                done = _typed_fence(process, "protocol_error", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
            except OSError as err:
                done = _typed_fence(process, "io_error", err)
                fenced = True
                if not done:
                    raise
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
        except BaseException as err:
            # Inline, not a helper: a helper's entry is one more point where an interruption could land
            # before the fence.
            if process is not None and not fenced:
                try:
                    process.fence(UNEXPECTED_FENCE)
                except BaseException as fence_err:  # noqa: BLE001 - the exception that reached here is the one that leaves
                    _record_cleanup_failure(err, fence_err, still_running=True)
            raise


def _record_cleanup_failure(err: BaseException, fence_err: BaseException, *, still_running: bool) -> None:
    """Note ``fence_err`` on ``err``; when ``still_running``, also add it to ``err``'s CLEANUP_FAILURES. Never
    raises: whatever the note or the attribute raises is dropped, so ``err`` leaves unchanged otherwise.
    (A copy of #420's helper in routes/base.py.)"""

    try:
        if still_running:
            setattr(err, CLEANUP_FAILURES, (*getattr(err, CLEANUP_FAILURES, ()), fence_err))
    except BaseException:  # noqa: BLE001 - an exception that takes no attribute still leaves
        pass
    try:
        err.add_note(f"fencing the child also failed: {type(fence_err).__name__}")
        if still_running and RUNNING_NOTE not in err.__notes__:
            err.add_note(RUNNING_NOTE)
    except BaseException:  # noqa: BLE001 - an exception that takes no note still leaves unchanged
        pass


def _typed_fence(process, reason: str, err: BaseException) -> bool:
    """A typed exit's fence. True when it succeeded. Otherwise the fence is tried once more as
    UNEXPECTED_FENCE, every failure is recorded on ``err`` (``_record_cleanup_failure``), and False is
    returned so the caller raises ``err`` itself. (A copy of #420's helper in routes/base.py.)"""

    try:
        process.fence(reason)
        return True
    except BaseException as first:  # noqa: BLE001 - recorded on the typed exception, which then leaves
        failed = first
    try:
        process.fence(UNEXPECTED_FENCE)
    except BaseException as second:  # noqa: BLE001 - recorded on the typed exception, which then leaves
        _record_cleanup_failure(err, failed, still_running=True)
        _record_cleanup_failure(err, second, still_running=True)
        return False
    _record_cleanup_failure(err, failed, still_running=False)
    return False

def profile_adapters(launcher, clock, credentials: Callable[[str, str], str], parent_env: Mapping[str, str], *,
                     argv: Callable[[dict], list[str]], driver: Driver,
                     operator_home: str | None = None,
                     allow_inside_operator_home: bool = False) -> dict[str, ProfileRouteAdapter]:
    return {
        engine: ProfileRouteAdapter(engine, launcher, clock, credentials, parent_env, argv=argv, driver=driver,
                                    operator_home=operator_home,
                                    allow_inside_operator_home=allow_inside_operator_home)
        for engine in c.PROFILE_ENGINES
    }
