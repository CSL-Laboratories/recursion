"""Shared run loop for route adapters (CSL-797, candidate). See README.md."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping

from recursion_core.routes import contract as c
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
        try:
            argv, env = self.argv(request), self.env(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        try:
            process = self._launcher(argv, env=env, shell=False)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        # Spawn can outlast the caller's deadline. Do not write stdin after it has passed.
        # The check sits inside the fence/drop try so a passed deadline still drops the handle,
        # and a fence that raises stays tracked through _fence_for / _keep_process.
        events: list[dict] = []
        try:
            if self._clock() >= deadline_at:
                fence_err = self._fence_for(process, "deadline")
                if fence_err is not None:
                    raise fence_err
                return c.result(rid, c.DEADLINE_EXCEEDED, detail="recursion deadline")
            try:
                return self.drive(process, request, deadline_at, events)
            except Deadline as err:
                self._abort(process, "deadline", err)  # fenced for this request only; the breaker never hears of it
                return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
            except _Protocol as err:
                self._abort(process, "protocol_error", err)
                return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))
            except OSError as err:
                # A child that exits before the request is written raises BrokenPipeError. That stays a
                # typed result, and the process is fenced, instead of escaping the route boundary.
                self._abort(process, "io_error", err)
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
            except Exception as err:
                # A malformed envelope or a driver bug must not escape as an untyped exception, and
                # must not leave the child running. The detail is the type name, never the message.
                self._abort(process, "internal", err)
                return c.result(rid, c.ENGINE_ERROR, events=events, detail=type(err).__name__)
            except BaseException as err:
                self._abort(process, "cancelled", err)
                raise
        finally:
            drop = getattr(self, "_drop_process", None)
            if drop is not None:
                drop(process)

    def _fence_for(self, process, reason: str) -> BaseException | None:
        """Fence the child. On failure, keep it tracked and return the fence error."""

        try:
            process.fence(reason)
        except BaseException as err:
            keep = getattr(self, "_keep_process", None)
            if keep is not None:
                keep(process)
            return err
        return None

    def _abort(self, process, reason: str, err: BaseException) -> None:
        """Fence after a driver failure. A fence error stays chained under that failure."""

        fence_err = self._fence_for(process, reason)
        if fence_err is not None:
            raise err from fence_err

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


def scalar(value: object) -> object:
    """Copy only scalar typed fields into events; never nested model payload."""

    return value if isinstance(value, (str, int, float, bool)) or value is None else None
