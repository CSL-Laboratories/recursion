"""The Task of the Control MCP server (``mcp.task.v1``), derived from a run or from a turn.

A Task is stored nowhere of its own. It is derived each time it is asked for, from the daemon's records
and the event log, so it survives a restart of this server and of the daemon. ``schemas/control/v1/
mcp.task.v1.json`` is the record; ``conformance/rules.py`` (``task_of_run``, ``task_of_turn``) states the
rule as code and ``schemas/control/v1/vectors/task.derivation`` checks it. This module is the server's own
statement of the same rule (the core does not import ``conformance/``); its tests replay every vector.

SOURCE run, for a run started by ``run.start`` without a session (``task_of_run``). ``task_id`` is the
run id; the status comes from the run's view:

* ``starting``, ``running``, ``stop_pending`` and ``unknown``: ``working``, or ``input_required`` while
  an approval is pending (the pending ids are carried). ``unknown`` means no terminal outcome is known,
  not that progress was seen. A Stop that was asked and is not confirmed sets ``cancel_requested``: the
  view says ``stop_pending``, or carries a stop record, or both. The status is not changed by it.
* ``stopped``: ``cancelled``. Only a confirmed Stop is.
* ``ended`` with a result: ``completed`` when the result says ok and carries no code; otherwise
  ``failed`` with the result's code, or ``result_invalid`` when a result that is not ok carries none.
  ``result_ref`` is the run id: the whole result stays in ``run.status``.
* ``ended`` with no result kept: ``failed``, ``result_unavailable``.
* ``start_failed``: ``failed``, ``start_failed``, with the kept ``start_error`` code as the detail, or
  a null detail when none was kept. A reason is never invented.
* ``lost_reboot``: ``failed``, ``lost_reboot``: the run's continuity was lost, not proof of what it did.

A Task of a run has no turn outcome and its ``expires_at`` is null.

SOURCE turn, for a turn of a session (``task_of_turn``). ``task_id`` is the turn id; queued and running
are ``working`` (``input_required`` while an approval is pending for it), ``ended`` is ``completed`` with
the turn's outcome (so done can be told from waiting), ``failed`` is ``failed``, ``interrupted`` and
``stopped`` are ``cancelled``. The result and the expiry are the turn's own, never those of the run the
turns share. ``cancel_requested`` is true while the session's Stop is pending and the turn has not ended.
Cancelling a turn's Task is an interrupt of that turn, not a Stop.

BOTH. ``created_at`` is null: no record or event of this version establishes a creation and its time (a
first ``state.changed`` is an observation of a committed image, a snapshot describes something that
already existed, and a retained tail or a gap is not an origin). ``updated_at`` is the time of the latest
``state.changed`` in the log of the run, or of the turn; null when the log holds none.

What the rule does not decide is the server's: ``status_message`` (a sentence for a person) and
``poll_interval_ms`` (advice on when to ask again; null when none is given). ``RULE_FIELDS`` names the
fields the rule decides; those two are outside it.

A source this module cannot read (an unknown state, a turn that is not in the session) raises
``TaskUnavailable``; a Task is never guessed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

RULE_FIELDS = ("task_id", "source", "session_id", "run_id", "turn_id", "status", "outcome", "cancel_requested",
               "pending_approval_ids", "created_at", "updated_at", "expires_at", "result")
FIELDS = RULE_FIELDS[:8] + ("status_message",) + RULE_FIELDS[8:12] + ("poll_interval_ms", "result")

_LIVE_RUN = ("starting", "running", "stop_pending", "unknown")
_TURN_STATUS = {"queued": "working", "running": "working", "ended": "completed", "failed": "failed",
                "interrupted": "cancelled", "stopped": "cancelled"}


class TaskUnavailable(ValueError):
    """The source cannot be read as a Task; ``reason`` names why (``state``, ``turn_unknown``, ``source``)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


def _latest_change(events: Iterable[Mapping], turn_id: str | None = None) -> str | None:
    times = [e.get("time") for e in events
             if e.get("type") == "state.changed" and (turn_id is None or e.get("turn_id") == turn_id)]
    return times[-1] if times else None


def _ids(pending_approval_ids: Iterable[str]) -> list[str]:
    ids = list(pending_approval_ids)
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise TaskUnavailable("source", "pending approval ids must be distinct non-empty strings")
    return ids


def _finish(task: dict, status_message: str | None, poll_interval_ms: int | None) -> dict:
    if poll_interval_ms is not None and (type(poll_interval_ms) is not int or poll_interval_ms < 100):
        raise ValueError("poll_interval_ms must be an integer of at least 100, or None")
    task["status_message"] = status_message
    task["poll_interval_ms"] = poll_interval_ms
    return {name: task[name] for name in FIELDS}


def run_status_message(view: Mapping, task: Mapping) -> str | None:
    """The server's sentence for a Task of a run: what is pending, if anything."""

    if task["status"] == "input_required":
        return "waiting for an approval on a surface that draws approvals"
    if task["cancel_requested"]:
        stop = view.get("stop")
        reason = stop.get("reason") if isinstance(stop, Mapping) else None
        return f"stop pending: {reason}" if isinstance(reason, str) and reason else "stop pending"
    return None


def task_of_run(view: Mapping, events: Iterable[Mapping] = (), pending_approval_ids: Iterable[str] = (), *,
                poll_interval_ms: int | None = None) -> dict:
    """The Task of a run started without a session, from its view (``run.view.v1``) and its log."""

    try:
        state, run_id = view["state"], view["run_id"]
    except (KeyError, TypeError):
        raise TaskUnavailable("source", "a run's view needs state and run_id") from None
    if not isinstance(run_id, str) or not run_id:
        raise TaskUnavailable("source", "run_id")
    pending = _ids(pending_approval_ids)
    result = view.get("result")
    if result is not None and not isinstance(result, Mapping):
        raise TaskUnavailable("source", "a run's result must be an object or null")
    task: dict[str, Any] = {
        "task_id": run_id, "source": "run", "session_id": None, "run_id": run_id, "turn_id": None,
        "outcome": None, "cancel_requested": False, "pending_approval_ids": [], "created_at": None,
        "updated_at": _latest_change(events), "expires_at": None, "result": None,
    }
    if state in _LIVE_RUN:
        task["status"] = "input_required" if pending else "working"
        task["pending_approval_ids"] = pending
        task["cancel_requested"] = state == "stop_pending" or view.get("stop") is not None
    elif state == "stopped":
        task["status"] = "cancelled"
    elif state == "ended" and result is not None:
        code = result.get("code")
        if code is None and result.get("status") == "ok":
            task["status"] = "completed"
            task["result"] = {"code": None, "detail": result.get("detail"), "result_ref": run_id}
        else:
            task["status"] = "failed"
            task["result"] = {"code": code or "result_invalid", "detail": result.get("detail"), "result_ref": run_id}
    elif state == "ended":
        task["status"] = "failed"
        task["result"] = {"code": "result_unavailable", "detail": None, "result_ref": None}
    elif state == "start_failed":
        kept = view.get("start_error")
        task["status"] = "failed"
        task["result"] = {"code": "start_failed", "detail": kept.get("code") if isinstance(kept, Mapping) else None,
                          "result_ref": None}
    elif state == "lost_reboot":
        task["status"] = "failed"
        task["result"] = {"code": "lost_reboot", "detail": None, "result_ref": None}
    else:
        raise TaskUnavailable("state", repr(state))
    return _finish(task, run_status_message(view, task), poll_interval_ms)


def task_of_turn(session: Mapping, turn_id: str, events: Iterable[Mapping] = (),
                 pending_approval_ids: Iterable[str] = (), *, poll_interval_ms: int | None = None) -> dict:
    """The Task of one turn of a session (``session.v1``), from the session record and its log."""

    turns = session.get("turns") if isinstance(session, Mapping) else None
    if not isinstance(turns, list):
        raise TaskUnavailable("source", "a session needs its turns")
    turn = next((t for t in turns if isinstance(t, Mapping) and t.get("turn_id") == turn_id), None)
    if turn is None:
        raise TaskUnavailable("turn_unknown", str(turn_id))
    status = _TURN_STATUS.get(turn.get("state"))
    if status is None:
        raise TaskUnavailable("state", repr(turn.get("state")))
    pending = _ids(pending_approval_ids)
    live = turn["state"] in ("queued", "running")
    if live and pending:
        status = "input_required"
    task = {
        "task_id": turn_id, "source": "turn", "session_id": session.get("session_id"), "run_id": turn.get("run_id"),
        "turn_id": turn_id, "status": status, "outcome": turn.get("outcome"),
        "cancel_requested": live and session.get("state") == "stop_pending",
        "pending_approval_ids": pending if live else [], "created_at": None,
        "updated_at": _latest_change(events, turn_id), "expires_at": turn.get("expires_at"), "result": turn.get("result"),
    }
    message = None
    if status == "input_required":
        message = "waiting for an approval on a surface that draws approvals"
    elif task["cancel_requested"]:
        message = "stop pending"
    return _finish(task, message, poll_interval_ms)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def differences(task: Mapping, other: Mapping) -> list[str]:
    """The rule's fields on which two Tasks differ, compared as JSON values (``true`` is not ``1``), sorted.
    ``status_message`` and ``poll_interval_ms`` are the server's and are not compared."""

    return sorted(name for name in RULE_FIELDS if _json(task.get(name)) != _json(other.get(name)))
