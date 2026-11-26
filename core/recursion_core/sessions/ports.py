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
    ``input_ack`` decides whether steering is offered at all: whether it is offered where an engine gives no
    typed acceptance is Lucas's open question (questions.md Q7), so until he answers, a steer is refused on such
    an engine, mid-turn and next-turn alike, and a steer already queued is held (``service.py``).

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
    ``order.resume`` is the engine's own session id to continue, or None for a fresh conversation. The order
    also carries what the launcher must hold the run to and is not this lane's to enforce: the session's
    ``tenant``, the ``principal`` that started it and its ``budget`` (lane QUOTA's admission). Returns when
    the input was handed to the engine; the run may be registered after that (the Stop below allows for it).
    Raises ``UnitRefused`` when the unit DEFINITELY did not start and nothing was written; any other exception
    is an outcome nobody can vouch for: the unit may run, and its run may be registered late or not at all, so
    the session holds its Stop until a Stop answers ``stopped`` or ``ended`` (``service.py``).

    ``send(order: SendOrder)``: write ``order.text`` under ``order.delivery_id`` to the live worker of
    ``order.run_id``, incarnation ``order.worker_incarnation``, as a new turn or into the turn in progress
    (``order.how``). Same returns and raises as ``launch``.

    ``stop(run_id)``: today's Stop of one run; returns its stop.result.v1 answer, or None when no run of that id
    is registered. A run that was registered once is answered (``stopped``, ``ended`` or ``pending``) from then
    on, never None. A Stop is safe to ask again (an emergency Stop never needs fresh state). Only a well-formed
    answer that confirms the Stop settles a session (``records.stop_confirmed``). None is taken as "nothing of
    that run runs" only when, at the Stop's claim, no launch of the run was pending, in flight, returned or
    attempted with an unknown outcome: a launch inside ``launch`` may register its run after the Stop looked, and
    one that returned or raised may have started a unit whose run is registered late, so then the Stop is asked
    again (``service.py``).

The engine side reports back through the service (``Sessions.accepted``, ``turn_finished``,
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
    tenant: str
    principal: dict
    budget: dict


@dataclass(frozen=True)
class SendOrder:
    session_id: str
    turn_id: str
    run_id: str
    worker_incarnation: str
    delivery_id: str
    text: str
    how: str


class Drivers(Protocol):
    def declaration(self, engine: str) -> dict | None: ...


class Routes(Protocol):
    def resolve(self, principal, params: dict): ...


class Units(Protocol):
    def launch(self, order: LaunchOrder) -> None: ...

    def send(self, order: SendOrder) -> None: ...

    def stop(self, run_id: str) -> dict | None: ...


@dataclass(frozen=True)
class Ports:
    drivers: Drivers
    routes: Routes
    units: Units
