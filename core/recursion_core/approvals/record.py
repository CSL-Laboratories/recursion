"""The published shape of an approval record (``approval.v1``, SPINE's #368), checked by hand.

The core cannot load ``jsonschema`` (the free-threaded interpreter has none) and does not read the
repository's schema files at run time, so the shape is written out here, rule for rule, and the published
fixtures (``schemas/control/v1/fixtures/{good,bad}/approval.v1``) judge it. ``shape_problem`` returns the
first place a value leaves the shape, or ``None``; ``milliseconds`` reads a timestamp of these records as a
real instant or refuses it.
"""

from __future__ import annotations

import datetime
import re

__all__ = ["APPROVER_KINDS", "STATES", "SURFACES", "TimestampRefused", "binding_problem", "effect_problem",
           "milliseconds", "shape_problem", "summary_of", "summary_problem"]

STATES = ("pending", "approved", "denied", "expired", "consumed", "vetoed")
SURFACES = ("cli", "tui", "page", "phone")
APPROVER_KINDS = ("owner", "device")
_TRUST = ("trusted", "untrusted")

_APPROVAL_ID = re.compile(r"apr-[A-Za-z0-9_-]{1,60}")
_LOG_ID = re.compile(r"ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64}")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TURN_ID = re.compile(r"turn-[A-Za-z0-9_-]{1,59}")
_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_ENGINE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z")
_KIND = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_CURRENCY = re.compile(r"[A-Z]{3}")

RECORD_FIELDS = ("approval_id", "session_id", "run_id", "turn_id", "source", "effect", "digest_form", "digest",
                 "display", "rendered_form", "rendered", "requested_at", "expires_at", "state", "opened_by",
                 "answered_by", "answer", "answered_at", "revision")
SUMMARY_FIELDS = ("approval_id", "session_id", "run_id", "turn_id", "source", "state", "requested_at",
                  "expires_at", "revision")


class TimestampRefused(Exception):
    code = "timestamp"


def milliseconds(timestamp: object) -> int:
    """A record timestamp as milliseconds since 1970, or ``TimestampRefused``.

    Exactly ``YYYY-MM-DDTHH:MM:SS.mmmZ`` in ASCII digits; a date that does not exist, an hour past 23 and
    a minute or second past 59 are refused (the profile has no leap second).
    """
    if type(timestamp) is not str or len(timestamp) != 24 or not _TIMESTAMP.fullmatch(timestamp):
        raise TimestampRefused(str(timestamp)[:40])
    if not timestamp.isascii():
        raise TimestampRefused(timestamp)
    try:
        days = (datetime.date(int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10])).toordinal()
                - datetime.date(1970, 1, 1).toordinal())
    except ValueError:
        raise TimestampRefused(timestamp) from None
    hour, minute, second, milli = (int(timestamp[11:13]), int(timestamp[14:16]), int(timestamp[17:19]),
                                   int(timestamp[20:23]))
    return ((days * 24 + hour) * 60 + minute) * 60000 + second * 1000 + milli


def _no_line_break(value) -> bool:
    return type(value) is str and "\r" not in value and "\n" not in value


def _id(value, pattern) -> bool:
    return _no_line_break(value) and pattern.fullmatch(value) is not None


def _timestamp(value) -> bool:
    """The schema's ``x-instant``: the pattern, and a real instant (no 2026-02-31)."""
    try:
        milliseconds(value)
    except TimestampRefused:
        return False
    return True


def _revision(value) -> bool:
    return type(value) is int and value >= 1


def _approver_problem(value, where: str):
    if type(value) is not dict or set(value) != {"kind", "id"}:
        return where
    if value["kind"] not in APPROVER_KINDS:
        return where + ".kind"
    if not _id(value["id"], _TOKEN):
        return where + ".id"
    return None


def _engine_effect_problem(effect):
    if type(effect) is not dict or set(effect) != {"kind", "engine", "session_id", "run_id", "tool", "cwd", "input"}:
        return "effect"
    if effect["kind"] != "engine_tool":
        return "effect.kind"
    if not _id(effect["engine"], _ENGINE):
        return "effect.engine"
    if not (effect["session_id"] is None or _id(effect["session_id"], _LOG_ID)):
        return "effect.session_id"
    if not _id(effect["run_id"], _RUN_ID):
        return "effect.run_id"
    if type(effect["tool"]) is not str or not 1 <= len(effect["tool"]) <= 256:
        return "effect.tool"
    if not (effect["cwd"] is None or (type(effect["cwd"]) is str and len(effect["cwd"]) >= 1)):
        return "effect.cwd"
    if type(effect["input"]) is not dict:
        return "effect.input"
    return None


def _plugin_effect_problem(effect):
    fields = {"schema", "kind", "plugin_id", "run_id", "connection", "scope", "currency", "amount_minor", "payload"}
    if type(effect) is not dict or set(effect) != fields:
        return "effect"
    if effect["schema"] != "csl-effect/vNext":
        return "effect.schema"
    if not _id(effect["kind"], _KIND):
        return "effect.kind"
    if not _id(effect["plugin_id"], _PLUGIN_ID):
        return "effect.plugin_id"
    for name in ("run_id", "connection"):
        if type(effect[name]) is not str or not effect[name]:
            return "effect." + name
    scope = effect["scope"]
    if type(scope) is not dict or set(scope) != {"account", "venue", "contract_family", "adapter", "environment"}:
        return "effect.scope"
    for name, value in scope.items():
        if type(value) is not str or not value:
            return "effect.scope." + name
    if not _id(effect["currency"], _CURRENCY):
        return "effect.currency"
    if type(effect["amount_minor"]) is not int or effect["amount_minor"] < 0:
        return "effect.amount_minor"
    if type(effect["payload"]) is not dict:
        return "effect.payload"
    return None


def effect_problem(source: object, effect: object):
    """The first place an effect leaves its source's published effect shape, or ``None``."""
    if source == "engine_tool":
        return _engine_effect_problem(effect)
    if source == "plugin_effect":
        return _plugin_effect_problem(effect)
    return "source"


def _lines_problem(items, where: str, low: int, high: int, fields: tuple, label: bool):
    if type(items) is not list or not low <= len(items) <= high:
        return where
    for number, item in enumerate(items):
        at = "%s[%d]" % (where, number)
        if type(item) is not dict or set(item) != set(fields):
            return at
        if item["trust"] not in _TRUST:
            return at + ".trust"
        if label:
            if type(item["label"]) is not str or not 1 <= len(item["label"]) <= 64:
                return at + ".label"
            if type(item["value"]) is not str or len(item["value"]) > 4096:
                return at + ".value"
        elif not _no_line_break(item["text"]) or len(item["text"]) > 4096:
            return at + ".text"
    return None


def shape_problem(record: object):
    """The first place ``record`` leaves ``approval.v1``'s shape, or ``None`` when it fits."""
    if type(record) is not dict or set(record) != set(RECORD_FIELDS):
        return "fields"
    if not _id(record["approval_id"], _APPROVAL_ID):
        return "approval_id"
    if not (record["session_id"] is None or _id(record["session_id"], _LOG_ID)):
        return "session_id"
    if not (record["run_id"] is None or _id(record["run_id"], _RUN_ID)):
        return "run_id"
    if not (record["turn_id"] is None or _id(record["turn_id"], _TURN_ID)):
        return "turn_id"
    source = record["source"]
    if source not in ("engine_tool", "plugin_effect"):
        return "source"
    if record["digest_form"] not in ("rfc8785/v1", "csl-cj/v0"):
        return "digest_form"
    if not _id(record["digest"], _SHA256):
        return "digest"
    problem = (_lines_problem(record["display"], "display", 0, 32, ("label", "value", "trust"), True)
               or _lines_problem(record["rendered"], "rendered", 1, 200, ("text", "trust"), False))
    if problem:
        return problem
    if record["rendered_form"] != "csl-approval-lines/v0":
        return "rendered_form"
    for name in ("requested_at", "expires_at"):
        if not _timestamp(record[name]):
            return name
    if record["state"] not in STATES:
        return "state"
    opened = record["opened_by"]
    if type(opened) is not list:
        return "opened_by"
    for number, item in enumerate(opened):
        at = "opened_by[%d]" % number
        if type(item) is not dict or set(item) != {"principal", "surface", "revision", "at"}:
            return at
        problem = _approver_problem(item["principal"], at + ".principal")
        if problem:
            return problem
        if item["surface"] not in SURFACES:
            return at + ".surface"
        if not _revision(item["revision"]):
            return at + ".revision"
        if not _timestamp(item["at"]):
            return at + ".at"
    if record["answered_by"] is not None:
        problem = _approver_problem(record["answered_by"], "answered_by")
        if problem:
            return problem
    if record["answer"] not in ("approve", "deny", None):
        return "answer"
    if not (record["answered_at"] is None or _timestamp(record["answered_at"])):
        return "answered_at"
    if not _revision(record["revision"]):
        return "revision"
    # allOf: the source decides the effect, the digest form and (an engine's) a run
    if source == "engine_tool":
        problem = _engine_effect_problem(record["effect"])
        if problem:
            return problem
        if type(record["run_id"]) is not str:
            return "run_id"
        if record["digest_form"] != "rfc8785/v1":
            return "digest_form"
    else:
        problem = _plugin_effect_problem(record["effect"])
        if problem:
            return problem
        if record["digest_form"] != "csl-cj/v0":
            return "digest_form"
    # allOf: the state decides the answer
    state = record["state"]
    if state in ("pending", "expired"):
        if (record["answered_by"], record["answer"], record["answered_at"]) != (None, None, None):
            return "answer"
    elif state in ("approved", "consumed"):
        if type(record["answered_by"]) is not dict or record["answer"] != "approve" or type(record["answered_at"]) is not str:
            return "answer"
    else:
        if type(record["answered_by"]) is not dict or record["answer"] != "deny" or type(record["answered_at"]) is not str:
            return "answer"
    return None


def summary_of(record: dict) -> dict:
    return {name: record[name] for name in SUMMARY_FIELDS}


def summary_problem(summary: object):
    if type(summary) is not dict or set(summary) != set(SUMMARY_FIELDS):
        return "fields"
    probe = dict.fromkeys(RECORD_FIELDS)
    probe.update(summary)
    for name, check in (("approval_id", lambda v: _id(v, _APPROVAL_ID)),
                        ("session_id", lambda v: v is None or _id(v, _LOG_ID)),
                        ("run_id", lambda v: v is None or _id(v, _RUN_ID)),
                        ("turn_id", lambda v: v is None or _id(v, _TURN_ID)),
                        ("source", lambda v: v in ("engine_tool", "plugin_effect")),
                        ("state", lambda v: v in STATES), ("requested_at", _timestamp), ("expires_at", _timestamp),
                        ("revision", _revision)):
        if not check(summary[name]):
            return name
    return None


def binding_problem(binding: object):
    """The first place a private binding leaves ``approval.v1#/$defs/binding``, or ``None``."""
    if type(binding) is not dict or set(binding) != {"approval_id", "run_id", "engine", "engine_request_id", "bound_at"}:
        return "fields"
    if not _id(binding["approval_id"], _APPROVAL_ID):
        return "approval_id"
    if not _id(binding["run_id"], _RUN_ID):
        return "run_id"
    if not _id(binding["engine"], _ENGINE):
        return "engine"
    request = binding["engine_request_id"]
    if not _no_line_break(request) or not 1 <= len(request) <= 256:
        return "engine_request_id"
    if not _timestamp(binding["bound_at"]):
        return "bound_at"
    return None
