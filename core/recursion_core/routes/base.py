"""Shared run loop for route adapters (CSL-797, candidate). See README.md."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.host import PLATFORM_UNSUPPORTED, platform_supported
from recursion_core.routes.launch import EngineProcess, Launcher, ReadTimeout

# credentials(connection_id, env_name) -> secret value. Owned by the core's custody layer (A3), injected.
Credentials = Callable[[str, str], str]


class Deadline(Exception):
    """Recursion's own deadline passed. Request-local: never an engine fault (PRODUCT.md S4)."""


class RouteAdapter:
    """One engine behind the route contract. Subclasses build argv/env and drive the protocol."""

    engine = ""

    def __init__(self, launcher: Launcher, clock: Callable[[], float], credentials: Credentials,
                 parent_env: Mapping[str, str], *, wall_clock: Callable[[], float] | None = None) -> None:
        self._launcher, self._clock, self._credentials = launcher, clock, credentials
        self._parent_env = dict(parent_env)  # explicit input; never os.environ
        # ``clock`` may be monotonic. Vendor epochs are translated with ``wall_clock`` (Unix seconds).
        self._wall = wall_clock if wall_clock is not None else time.time

    def vendor_reset(self, epoch: object) -> float | None:
        """Vendor ``resetsAt`` (Unix epoch) as an absolute reading of the breaker clock."""

        return c.vendor_reset(epoch, self._clock, self._wall)

    def run(self, request: dict, deadline_at: float) -> dict:
        rid = request["request_id"]
        if not platform_supported():
            # Lucas, Nov 12 ("Yes, as recommended", decision sheet D9): "Windows refuses uniformly at the
            # shared adapter boundary, before any path or connector is looked at" (R3-7). Nothing is built:
            # no argv, no environment, no credential lookup, and the launcher is never called.
            return c.result(rid, c.LAUNCH_REFUSED, detail=PLATFORM_UNSUPPORTED)
        try:
            argv, env = self.argv(request), self.env(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        # STARTED CHILDREN ARE FENCED ON EVERY WAY OUT. Once the launcher has returned a process, any
        # exception that leaves this method, BaseException included, fences that process first; the same
        # exception object then leaves. The three typed exits below fence with their own reason and answer;
        # everything else is fenced with UNEXPECTED_FENCE and raised. A fence that itself fails never
        # replaces the exception: it is noted on it. What this method cannot cover is an asynchronous
        # exception (a signal handler's) delivered between the launcher's return and the store into
        # ``process``, or inside this handler before the fence has run; the process then exists only in the
        # launcher's own record of the run (daemon/launcher.py publishes it with RunContext.launched).
        process = None
        fenced = False
        try:
            try:
                process = self._launcher(argv, env=env, shell=False)
            except c.RouteError as err:
                # The launcher's own typed refusal (for example the child-environment policy refusing a
                # value). Nothing was started, so this is a refusal before launch, not an engine fault.
                return c.result(rid, err.code, detail=err.detail)
            except OSError as err:
                return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
            events: list[dict] = []
            try:
                return self.drive(process, request, deadline_at, events)
            except Deadline:
                process.fence("deadline")  # fenced for this request only; the breaker never hears of it
                fenced = True
                return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
            except _Protocol as err:
                process.fence("protocol_error")
                fenced = True
                return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
            except OSError as err:
                # A child that exits before the request is written raises BrokenPipeError. That stays a
                # typed result, and the process is fenced, instead of escaping the route boundary.
                process.fence("io_error")
                fenced = True
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
        except BaseException as err:
            # Inline, not a helper: a helper's entry is one more point where an interruption could land
            # before the fence.
            if process is not None and not fenced:
                try:
                    process.fence(UNEXPECTED_FENCE)
                except BaseException as fence_err:  # noqa: BLE001 - the exception that reached here is the one that leaves
                    try:
                        err.add_note(f"fencing the child also failed: {type(fence_err).__name__}")
                    except BaseException:  # noqa: BLE001 - an exception that takes no note still leaves unchanged
                        pass
            raise

    # -- subclass hooks -----------------------------------------------------------------------
    def argv(self, request: dict) -> list[str]:
        raise NotImplementedError

    def env(self, request: dict) -> dict[str, str]:
        raise NotImplementedError

    def drive(self, process: EngineProcess, request: dict, deadline_at: float, events: list[dict]) -> dict:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------------------------
    def credential(self, request: dict, allowed: tuple[str, ...]) -> tuple[str, str] | None:
        """The route's one credential, only when its connection is an API key it selects explicitly."""

        conn = request["route"]["connection"]
        if conn["kind"] != "api_key":
            return None
        name = conn["credential_env"]
        if name not in allowed:
            raise c.RouteError(c.LAUNCH_REFUSED, f"credential_env {name} not allowed for {self.engine}")
        try:
            value = self._credentials(conn["id"], name)
        except KeyError:
            value = ""
        if not isinstance(value, str) or not value:
            raise c.RouteError(c.LAUNCH_REFUSED, f"no credential for {conn['id']}")
        return name, value

    def read_message(self, process: EngineProcess, deadline_at: float) -> dict | None:
        """Next JSON object from stdout, ``None`` at EOF. Raises ``Deadline`` or ``_Protocol``."""

        remaining = deadline_at - self._clock()
        if remaining <= 0:
            raise Deadline()
        try:
            line = process.read_line(remaining)
        except ReadTimeout:
            raise Deadline() from None
        if line is None:
            return None
        try:
            message = json.loads(line)
        except ValueError:
            raise _Protocol("stdout line is not JSON") from None
        if not isinstance(message, dict):
            raise _Protocol("stdout line is not a JSON object")
        return message


class _Protocol(Exception):
    pass


# The fence reason for a started child whose run leaves ``RouteAdapter.run`` by an exception that none of
# the typed exits answers (the same word lane ENGINE-E5's one-shot adapter uses for its own copy of the loop).
UNEXPECTED_FENCE = "unexpected"


def scalar(value: object) -> object:
    """Copy only scalar typed fields into events; never nested model payload."""

    return value if isinstance(value, (str, int, float, bool)) or value is None else None
