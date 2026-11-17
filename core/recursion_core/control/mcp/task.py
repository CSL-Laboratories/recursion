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
``state.changed`` OF that run or turn, null when the log holds none. Which entity a ``state.changed`` is of
is said in its payload (``entity``, ``entity_id``); where it is filed (the ``turn_id`` beside the payload)
says under which turn it was logged, not what changed. So a turn's Task takes an event that is filed under
that turn AND whose payload names that turn: a change of the run or of the session filed under a turn does
not move it. A run's Task takes an event whose payload names that run, wherever it is filed.

What the rule does not decide is the server's: ``status_message`` (a sentence for a person) and
``poll_interval_ms`` (advice on when to ask again; null when none is given). ``RULE_FIELDS`` names the
fields the rule decides; those two are outside it.

A source this module cannot read raises ``TaskUnavailable``, and a Task is never guessed: an unknown state
(``state``), a turn that is not in the session (``turn_unknown``), and (``source``) a member the rule reads
that is absent or not of its shape. This module reads what ``conformance/rules.py`` reads, where it reads
it: a run's ``state`` and ``run_id``; of a result kept, its ``code``, its ``status`` when the code is null,
and its ``detail``; a kept ``start_error``'s ``code``; a session's ``turns``, each turn's ``turn_id`` up to
the one asked for, and its ``state``, ``run_id``, ``outcome``, ``expires_at`` and ``result``; the session's
``session_id``, and its ``state`` while the turn is live; each event's ``type``, and of a ``state.changed``
its ``payload`` (``entity``, then ``entity_id``) and the ``time`` of one that counts. A member present
with the value null is read as null; an absent one is not read as null. Pending approval ids must be
distinct, non-empty strings, as ``mcp.task.v1`` requires (the rule does not check them).
"""

from __future__ import annotations

import json
import re
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


def _code_word(value: object) -> bool:
    """A result code of mcp.task.v1: a code_word (common.v1), not merely any string."""

    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None


def _member(record: Any, key: str, what: str) -> Any:
    """``record[key]``; ``TaskUnavailable("source")`` when ``record`` is not an object or has no ``key``."""

    if not isinstance(record, Mapping) or key not in record:
        raise TaskUnavailable("source", f"{what} has no {key}")
    return record[key]


def _latest_change(events: Iterable[Mapping], entity: str, entity_id: str, of_turn: str | None = None) -> str | None:
    """The time of the latest ``state.changed`` of that run or turn (BOTH, above); null when there is none."""

    times = []
    for event in events:
        if not isinstance(event, Mapping):
            raise TaskUnavailable("source", "an event is not an object")
        if of_turn is not None and event.get("turn_id") != of_turn:
            continue  # filed under another turn, or under none
        if _member(event, "type", "an event") != "state.changed":
            continue
        payload = _member(event, "payload", "a state.changed event")
        if _member(payload, "entity", "a state.changed payload") != entity:
            continue
        if _member(payload, "entity_id", "a state.changed payload") == entity_id:
            times.append(_member(event, "time", "a state.changed event"))
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

    state, run_id = _member(view, "state", "a run's view"), _member(view, "run_id", "a run's view")
    if not isinstance(run_id, str) or not run_id:
        raise TaskUnavailable("source", "run_id")
    pending = _ids(pending_approval_ids)
    result = view.get("result")
    if result is not None and not isinstance(result, Mapping):
        raise TaskUnavailable("source", "a run's result must be an object or null")
    task: dict[str, Any] = {
        "task_id": run_id, "source": "run", "session_id": None, "run_id": run_id, "turn_id": None,
        "outcome": None, "cancel_requested": False, "pending_approval_ids": [], "created_at": None,
        "updated_at": _latest_change(events, "run", run_id), "expires_at": None, "result": None,
    }
    if state in _LIVE_RUN:
        task["status"] = "input_required" if pending else "working"
        task["pending_approval_ids"] = pending
        task["cancel_requested"] = state == "stop_pending" or view.get("stop") is not None
    elif state == "stopped":
        task["status"] = "cancelled"
    elif state == "ended" and result is not None:
        code = _member(result, "code", "a run's result")
        if code is not None and not _code_word(code):
            raise TaskUnavailable("source", "a run's result code is not a code word")
        ok = code is None and _member(result, "status", "a run's result whose code is null") == "ok"
        detail = _member(result, "detail", "a run's result")
        if detail is not None and not isinstance(detail, str):
            raise TaskUnavailable("source", "a run's result detail is not a string")
        task["status"] = "completed" if ok else "failed"
        task["result"] = {"code": None if ok else (code or "result_invalid"), "detail": detail, "result_ref": run_id}
    elif state == "ended":
        task["status"] = "failed"
        task["result"] = {"code": "result_unavailable", "detail": None, "result_ref": None}
    elif state == "start_failed":
        kept = view.get("start_error")  # none kept, or kept empty: the reason is not known, and none is invented
        detail = _member(kept, "code", "a kept start_error") if kept else None
        if detail is not None and not isinstance(detail, str):
            raise TaskUnavailable("source", "a kept start_error code is not a string")
        task["status"] = "failed"
        task["result"] = {"code": "start_failed", "detail": detail, "result_ref": None}
    elif state == "lost_reboot":
        task["status"] = "failed"
        task["result"] = {"code": "lost_reboot", "detail": None, "result_ref": None}
    else:
        raise TaskUnavailable("state", repr(state))
    return _finish(task, run_status_message(view, task), poll_interval_ms)


def task_of_turn(session: Mapping, turn_id: str, events: Iterable[Mapping] = (),
                 pending_approval_ids: Iterable[str] = (), *, poll_interval_ms: int | None = None) -> dict:
    """The Task of one turn of a session (``session.v1``), from the session record and its log."""

    turns = _member(session, "turns", "a session")
    if not isinstance(turns, list):
        raise TaskUnavailable("source", "a session's turns must be a list")
    turn = None
    for each in turns:  # read up to the turn asked for, as the rule does
        if _member(each, "turn_id", "a turn") == turn_id:
            turn = each
            break
    if turn is None:
        raise TaskUnavailable("turn_unknown", str(turn_id))
    state = _member(turn, "state", "a turn")
    status = _TURN_STATUS.get(state) if isinstance(state, str) else None
    if status is None:
        raise TaskUnavailable("state", repr(state))
    pending = _ids(pending_approval_ids)
    live = state in ("queued", "running")
    if live and pending:
        status = "input_required"
    task = {
        "task_id": turn_id, "source": "turn", "session_id": _member(session, "session_id", "a session"),
        "run_id": _member(turn, "run_id", "a turn"), "turn_id": turn_id, "status": status,
        "outcome": _member(turn, "outcome", "a turn"),
        "cancel_requested": live and _member(session, "state", "a session") == "stop_pending",
        "pending_approval_ids": pending if live else [], "created_at": None,
        "updated_at": _latest_change(events, "turn", turn_id, of_turn=turn_id),
        "expires_at": _member(turn, "expires_at", "a turn"), "result": _member(turn, "result", "a turn"),
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
