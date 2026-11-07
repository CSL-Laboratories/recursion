"""Shared run loop for route adapters (CSL-797, candidate). See README.md."""

from __future__ import annotations

import json
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
                 parent_env: Mapping[str, str]) -> None:
        self._launcher, self._clock, self._credentials = launcher, clock, credentials
        self._parent_env = dict(parent_env)  # explicit input; never os.environ

    def run(self, request: dict, deadline_at: float) -> dict:
        rid = request["request_id"]
        try:
            argv, env = self.argv(request), self.env(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        try:
            process = self._launcher(argv, env=env, shell=False)
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        events: list[dict] = []
        try:
            return self.drive(process, request, deadline_at, events)
        except Deadline:
            process.fence("deadline")  # fenced for this request only; the breaker never hears of it
            return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
        except _Protocol as err:
            process.fence("protocol_error")
            return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))

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
