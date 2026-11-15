# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""What the session layer needs from outside the store, as interfaces. Nothing in this lane implements them.

They are the joins to other lanes, written down so that this slice can be tested against fakes and so that
each lane can see exactly what it would be asked for. None of them is wired: there is no adapter to a real
driver, launcher or Stop in this slice (the start note: "No wiring"). The adapters come with the slice that
stands on lane DRIVER-CC's handle service, lane STOP's Stop and lane ROUTING's chooser.

``Drivers.declaration(engine)``
    The engine's driver.v2 declaration (#368): ``{"engine", "driver", "flags", "input_ack"}``, or None when no
    driver serves that engine. The flags decide, BEFORE any side effect, how a turn is carried: a worker that
    takes a second message (``multi_turn``), input in the middle of a turn (``mid_turn_input``), ``resume``.
    ``input_ack`` is read by nobody here: whether steering is offered where an engine gives no typed
    acceptance is Lucas's open question, and this lane builds no fallback for it.

``Routes.resolve(principal, params)``
    The route a new session is billed through, as session.v1 writes it (``{"engine", "model", "connection"}``),
    or a refusal ``(code, detail)``. Choosing a route is lane ROUTING's; a session only records the choice. It is
    asked inside the command's transaction, so it must be pure: it reads configuration and touches nothing
    (the rule a start registrant's ``normalize`` already follows).

``Units``
    The units of a session. Each call is made by the lane's service, on its own thread, OUTSIDE every
    transaction, after the work's attempt was committed.

    ``launch(order: LaunchOrder)``: start a NEW run under ``order.run_id`` (allocated by this lane before the
    dispatch) whose input is ``order.text``, written under ``order.delivery_id`` and aimed at ``order.turn_id``;
    ``order.resume`` is the engine's own session id to continue, or None for a fresh conversation. Returns when
    the input was handed to the engine. Raises ``UnitRefused`` when the unit DEFINITELY did not start and
    nothing was written; any other exception is an outcome nobody can vouch for.

    ``send(order: SendOrder)``: write ``order.text`` under ``order.delivery_id`` to the live worker of
    ``order.run_id``, incarnation ``order.worker_incarnation``, as a new turn or into the turn in progress
    (``order.how``). Same returns and raises as ``launch``.

    ``interrupt(order: InterruptOrder)``: the driver's own cancel of the turn ``order.turn_id`` in the live worker of
    ``order.run_id``, incarnation ``order.worker_incarnation`` (driver.v2: ``interrupt(turn_id)``, only where the
    driver declares ``interrupt``). Returns when the cancel was handed to the engine; that the turn WAS interrupted
    is reported back later (``Sessions.turn_interrupted``). Raises ``UnitRefused`` when the driver definitely did
    not send it (the turn is not the one in progress, say); any other exception is an outcome nobody can vouch for.

    ``stop(run_id)``: today's Stop of one run; returns its stop.result.v1 answer, or None when no run of that id
    was ever registered. A Stop is safe to ask again (an emergency Stop never needs fresh state).

The engine side reports back through the service (``Sessions.accepted``, ``turn_finished``, ``turn_interrupted``,
``engine_session_reported``); in this slice the tests make those calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class UnitRefused(Exception):
    """The unit definitely did not start, or the worker definitely did not take the write: nothing was written."""

    def __init__(self, code: str, detail: str = '') -> None:
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class LaunchOrder:
    session_id: str
    turn_id: str
    run_id: str
    worker_incarnation: str
    delivery_id: str
    text: str
    route: dict
    workspace: dict | None
    resume: str | None


@dataclass(frozen=True)
class SendOrder:
    session_id: str
    turn_id: str
    run_id: str
    worker_incarnation: str
    delivery_id: str
    text: str
    how: str


@dataclass(frozen=True)
class InterruptOrder:
    session_id: str
    turn_id: str
    run_id: str
    worker_incarnation: str


class Drivers(Protocol):
    def declaration(self, engine: str) -> dict | None: ...


class Routes(Protocol):
    def resolve(self, principal, params: dict): ...


class Units(Protocol):
    def launch(self, order: LaunchOrder) -> None: ...

    def send(self, order: SendOrder) -> None: ...

    def interrupt(self, order: InterruptOrder) -> None: ...

    def stop(self, run_id: str) -> dict | None: ...


@dataclass(frozen=True)
class Ports:
    drivers: Drivers
    routes: Routes
    units: Units
