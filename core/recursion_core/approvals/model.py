"""What can happen to one approval request, one happening at a time, as pure transitions over its record.

Every function takes a record and returns a NEW record (and, for a client's call, the answer); none
changes what it was given, and a refusal changes nothing. The store applies them inside the event
store's transaction; the method handlers wrap them; the published lifecycle vectors (#368,
``vectors/approval.lifecycle``) drive them directly. The rules are ``approval.v1``'s, as SPINE's
``conformance/rules.py`` states them (``ApprovalModel``):

* ``revision`` counts changes to the request's CONTENT (effect, lines, deadline); it starts at 1. A
  showing does not change it and neither does an answer. Content changes only while pending.
* approval.answer, in this order: an owner or a device (else ``scope_refused``, the dispatcher's own
  check in production); every field whole (else ``request_invalid``); the record is the shape of an
  approval (else ``record_invalid``, the core's refusal and no answer of the method); at or after
  ``expires_at`` a pending request becomes ``expired`` (no answer, answerer or time) and the answer is
  ``conflict``/``already_resolved``; not pending is ``already_resolved``; another revision is
  ``stale_revision``; no opening by this principal on this surface at the current revision is
  ``prompt_not_shown``; another digest is ``digest_mismatch``. Only then is the answer taken.
* approval.open records (principal, surface, revision, time) once, while pending, and returns the record.
* ``consume`` carries the effect out once: only an approved record that agrees with itself, at exactly
  its revision and digest, and, for an engine's call, with the private binding kept for it, which must
  name this approval, its run and its engine. A plugin's effect has no binding.
"""

from __future__ import annotations

import copy
import re

from recursion_core.approvals import effects
from recursion_core.approvals.effects import EffectRefused
from recursion_core.approvals.record import (STATES, SURFACES, TimestampRefused, approver_problem, binding_problem,
                                             effect_problem, milliseconds, shape_problem)

__all__ = ["ApprovalRefused", "TARGET_MEMBERS", "answer_prompt", "change", "consume", "expire_if_due",
           "list_params_refusal", "new_request", "open_prompt", "params_refusal", "principal_refusal", "restored"]

TARGET_MEMBERS = {"engine_tool": ("kind", "engine", "session_id", "run_id"),
                  "plugin_effect": ("schema", "plugin_id", "run_id")}
_NEEDS = {"approval.open": ("command_id", "approval_id", "surface"),
          "approval.answer": ("command_id", "approval_id", "surface", "expected_revision", "digest", "answer")}


class ApprovalRefused(Exception):
    """The core's own refusal of a happening; ``code`` is one plain word, and ``detail``, when there is one, names
    what it is about (``approval_withdrawn`` names the run). It changes nothing."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _at(at: object) -> int:
    try:
        return milliseconds(at)
    except TimestampRefused:
        raise ApprovalRefused("timestamp") from None


def restored(record: dict) -> dict:
    """A private copy of a record as the core holds it: an engine call's numbers in their bound form."""
    held = copy.deepcopy(record)
    if type(held) is dict and held.get("source") == "engine_tool" and type(held.get("effect")) is dict:
        held["effect"] = effects.normalised(held["effect"])
    return held


def principal_refusal(principal: object):
    """``scope_refused`` unless the caller is an approver exactly as a record keeps one (an owner or a device,
    ``{"kind", "id"}`` and nothing else, its id a token: ``record.approver_problem``); ``None`` when it may
    answer. So what approval.open or approval.answer writes for it never leaves the record unreadable."""
    if approver_problem(principal) is not None:
        return {"code": "scope_refused", "detail": "approve"}
    return None


_APPROVAL_ID = re.compile(r"apr-[A-Za-z0-9_-]{1,60}")
_COMMAND_ID = re.compile(r"[A-Za-z0-9._:-]{8,128}")  # common.v1 command_id
_DIGEST = re.compile(r"[0-9a-f]{64}")
_LOG_ID = re.compile(r"ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64}")
_LIST_PARAMS = ("session_id", "state", "limit", "after")


def command_id_refusal(params: object):
    """``request_invalid``/``command_id`` unless ``params`` carries a command id in common.v1's grammar;
    ``None`` when it does. The registrant derives the receipt's tenant through this, so a malformed id is
    refused before any receipt is looked up or kept."""
    value = params.get("command_id") if type(params) is dict else None
    if type(value) is not str or not _COMMAND_ID.fullmatch(value):
        return {"code": "request_invalid", "detail": "command_id"}
    return None


def params_refusal(method: str, params: object):
    """``request_invalid`` naming the first field that is not whole, or the first member the method does not
    take; ``None`` when the request is whole. Ids and digests are held to their published grammars."""
    for field in _NEEDS[method]:
        value = params.get(field) if type(params) is dict else None
        whole = (type(value) is int and value >= 1) if field == "expected_revision" else (type(value) is str and bool(value))
        if not whole or (field == "answer" and value not in ("approve", "deny")) \
                or (field == "surface" and value not in SURFACES) \
                or (field == "command_id" and not _COMMAND_ID.fullmatch(value)) \
                or (field == "approval_id" and not _APPROVAL_ID.fullmatch(value)) \
                or (field == "digest" and not _DIGEST.fullmatch(value)):
            return {"code": "request_invalid", "detail": field}
    for field in sorted(params):
        if field not in _NEEDS[method]:
            return {"code": "request_invalid", "detail": field}
    return None


def list_params_refusal(params: object):
    """approval.list's parameters: ``session_id`` (a log id), ``state``, ``limit`` (1 to 1000), ``after`` (an
    approval id), all optional, nothing else."""
    if type(params) is not dict:
        return {"code": "request_invalid", "detail": "params"}
    for field in sorted(params):
        value = params[field]
        if field not in _LIST_PARAMS \
                or (field == "session_id" and not (type(value) is str and _LOG_ID.fullmatch(value))) \
                or (field == "state" and value not in STATES) \
                or (field == "limit" and not (type(value) is int and 1 <= value <= 1000)) \
                or (field == "after" and not (type(value) is str and _APPROVAL_ID.fullmatch(value))):
            return {"code": "request_invalid", "detail": field}
    return None


def _require_shape(record: dict) -> None:
    if shape_problem(record) is not None:
        raise ApprovalRefused("record_invalid")


def _opened(record: dict, principal: dict, surface: str) -> bool:
    return any((item["principal"], item["surface"], item["revision"]) == (principal, surface, record["revision"])
               for item in record["opened_by"])


def open_prompt(record: dict, principal: dict, params: dict, at: str) -> tuple:
    """approval.open: record that this principal saw the current revision on this surface."""
    _at(at)
    _require_shape(record)
    record = restored(record)
    if params.get("approval_id") != record["approval_id"]:
        return record, {"code": "approval_unknown", "approval_id": params.get("approval_id")}
    if record["state"] == "pending" and not _opened(record, principal, params["surface"]):
        record["opened_by"].append({"principal": copy.deepcopy(principal), "surface": params["surface"],
                                    "revision": record["revision"], "at": at})
    return record, {"code": "ok", "approval": copy.deepcopy(record)}


def expire_if_due(record: dict, at: str):
    """The record expired at its deadline (a new record), or ``None`` when it is not a pending one past it."""
    if record["state"] == "pending" and _at(at) >= _at(record["expires_at"]):
        record = restored(record)
        record["state"] = "expired"
        return record
    return None


def answer_prompt(record: dict, principal: dict, params: dict, at: str) -> tuple:
    """approval.answer: the first valid answer wins; every refusal is typed and changes nothing."""
    _at(at)
    _require_shape(record)
    record = restored(record)
    if params.get("approval_id") != record["approval_id"]:
        return record, {"code": "approval_unknown", "approval_id": params.get("approval_id")}
    refusal = {"approval_id": record["approval_id"]}
    expired = expire_if_due(record, at)
    if expired is not None:
        record = expired
    if record["state"] != "pending":
        return record, {"code": "conflict", "detail": "already_resolved", **refusal}
    if params["expected_revision"] != record["revision"]:
        return record, {"code": "conflict", "detail": "stale_revision", "current_revision": record["revision"],
                        **refusal}
    if not _opened(record, principal, params["surface"]):
        return record, {"code": "prompt_not_shown", **refusal}
    if params["digest"] != record["digest"]:
        return record, {"code": "conflict", "detail": "digest_mismatch", **refusal}
    record.update(state="approved" if params["answer"] == "approve" else "denied",
                  answered_by=copy.deepcopy(principal), answer=params["answer"], answered_at=at)
    return record, {"code": "ok", "approval": copy.deepcopy(record)}


def change(record: dict, effect: object = None, expires_at: object = None) -> dict:
    """The core changes a pending request's content: a new candidate, checked whole, or a typed refusal."""
    if type(record) is not dict or record.get("state") not in STATES:
        raise ApprovalRefused("record_invalid")
    if record["state"] != "pending":
        raise ApprovalRefused("resolved")
    _require_shape(record)
    if effect is None and expires_at is None:
        raise ApprovalRefused("nothing_to_change")
    candidate = restored(record)
    source = record["source"]
    if effect is not None:
        if type(effect) is not dict:
            raise ApprovalRefused("effect")
        new = effects.normalised(copy.deepcopy(effect)) if source == "engine_tool" else copy.deepcopy(effect)
        if effect_problem(source, new) is not None:
            raise ApprovalRefused("effect")
        if any(new.get(member) != candidate["effect"].get(member) for member in TARGET_MEMBERS[source]):
            raise ApprovalRefused("target")
        try:
            effects.lines_of(source, new)  # the renderer's own refusal first, by its code
            bound = effects.bind(source, new)
        except EffectRefused as refused:
            raise ApprovalRefused(refused.code) from None
        except (KeyError, TypeError):
            raise ApprovalRefused("effect") from None
        candidate.update(effect=bound.effect, digest=bound.digest, rendered=bound.rendered)
    if expires_at is not None:
        _at(expires_at)
        candidate["expires_at"] = expires_at
    candidate["revision"] = record["revision"] + 1
    _require_shape(candidate)
    problems = effects.record_problems(candidate)
    if problems:
        raise ApprovalRefused(problems[0])
    return candidate


def consume(record: dict, kept: object, revision: object, digest: object, binding: object) -> dict:
    """Carry an approved effect out once: the record becomes ``consumed``, or a typed refusal.

    ``record`` is the record as the core holds it (``restored``): an engine call's numbers in their bound
    form. A record that still holds an integral float or minus zero does not agree with itself and is
    ``record_invalid``: what would be carried out would not be what was digested.
    """
    _require_shape(record)
    if record["state"] == "consumed":
        raise ApprovalRefused("already_consumed")
    if record["state"] != "approved":
        raise ApprovalRefused("not_approved")
    if effects.record_problems(record):
        raise ApprovalRefused("record_invalid")
    held = copy.deepcopy(record)
    if (revision, digest) != (record["revision"], record["digest"]):
        raise ApprovalRefused("approval_mismatch")
    if record["source"] == "engine_tool":
        effect = record["effect"]
        if (type(kept) is not dict or binding != kept or binding_problem(kept) is not None
                or binding.get("approval_id") != record["approval_id"]
                or binding.get("run_id") != effect["run_id"] or binding.get("engine") != effect["engine"]):
            raise ApprovalRefused("binding_mismatch")
    elif kept is not None or binding is not None:
        raise ApprovalRefused("binding_mismatch")
    held["state"] = "consumed"
    return held


def new_request(*, approval_id: str, source: str, effect: dict, session_id, run_id, turn_id,
                requested_at: str, expires_at: str, display: list = ()) -> dict:
    """A pending record for a newly bound effect, checked whole before it exists."""
    try:
        bound = effects.bind(source, effect)
    except EffectRefused as refused:
        raise ApprovalRefused(refused.code) from None
    _at(requested_at)
    if _at(expires_at) <= _at(requested_at):
        raise ApprovalRefused("timestamp")
    record = {
        "approval_id": approval_id, "session_id": session_id, "run_id": run_id, "turn_id": turn_id,
        "source": source, "effect": bound.effect, "digest_form": bound.digest_form, "digest": bound.digest,
        "display": [dict(item) for item in display], "rendered_form": effects.LINES_FORM, "rendered": bound.rendered,
        "requested_at": requested_at, "expires_at": expires_at, "state": "pending", "opened_by": [],
        "answered_by": None, "answer": None, "answered_at": None, "revision": 1,
    }
    _require_shape(record)
    problems = effects.record_problems(record)
    if problems:
        raise ApprovalRefused(problems[0])
    return record
