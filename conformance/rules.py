#!/usr/bin/env python3
"""The rules of the control records that a JSON Schema cannot state, written once.

A schema can say that an approval has a digest, some rendered lines and an effect. It cannot say that
the digest is the digest of that effect, or that the lines show that effect and no other. Those rules
are here, as plain functions over plain values, so that the fixtures, the vectors beside the schemas
and any client check the same thing. This file imports the standard library only and nothing from
the product; a client in another language implements the same functions and checks the same vectors
(``schemas/control/v1/vectors``).

What is here:

* ``read_strict``: the strict reading of JSON text that came from an engine (the input of a tool call).
* ``text_of``: the text of a value as RFC 8785 writes it; its UTF-8 bytes are what is hashed. With
  ``display`` on, the same text with the characters no terminal should be handed raw written as escapes.
* ``call_of``, ``call_in`` and ``check_call``: what an engine supplied for one tool call (its tool, its
  input, its directory) and the strict profile it is held to.
* ``canonical_v0``: the restricted canonical JSON ``csl-cj/v0`` that plugin effects and today's
  signatures use.
* ``effect_bytes`` and ``digest_of``: the bytes an approval's digest is taken over, by ``digest_form``.
* ``approval_lines``: the lines every surface prints for an approval (``csl-approval-lines/v0``).
* ``approval_problems``: what does not agree inside one approval record.
* ``transcript_fields`` and ``transcript_problems``: what a transcript line must carry because its
  event says so.
* ``queue_step``: what happens to a queued message, one happening at a time.
* ``task_of_run``, ``task_of_turn`` and ``task_problems``: the Task of a run or of a turn, derived.
* ``ApprovalModel``: approval.open and approval.answer, step by step, with replay.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json

MAX_INT = 2**53 - 1          # the largest whole number every binary64 reader keeps exact
MAX_DEPTH = 64               # of a tool's input
CJ_MAX_DEPTH = 32            # of csl-cj/v0, as plugin_security/canonical.py has it
CALL_MAX = 262144            # UTF-8 bytes of a call's text; a larger call is never drawn and never approved
LINE_PIECE = 4000            # characters of a value on one rendered line; the rest continues on the next
LINES_MAX = 200              # rendered lines of one approval; an effect that needs more is never drawn
LINES_FORM = "csl-approval-lines/v0"
DIGEST_FORMS = {"engine_tool": "rfc8785/v1", "plugin_effect": "csl-cj/v0"}


class Refused(Exception):
    """A value that a rule does not take. ``code`` is one plain word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------------- reading JSON text

def read_strict(text: str):
    """Read JSON text the way the core reads a tool's input: strictly, or not at all.

    Refused, each with its own code: text that is not JSON (``not_json``); an object that names a key
    twice, at any depth (``duplicate_key``); ``NaN`` and the infinities (``not_a_number``); a whole
    number written without a fraction or an exponent that lies outside the range every binary64 reader
    keeps exact (``integer_out_of_range``: such a number is sent as a string, or not at all); a number
    with a fraction or an exponent that is not finite as a binary64 (``number_out_of_range``); half of a
    surrogate pair on its own, in a string or a key (``lone_surrogate``); nesting deeper than 64
    (``too_deep``). The range of a whole number is checked on its digits, before any conversion that
    could lose it. Strings are kept exactly as written: nothing is normalised.
    """
    if not isinstance(text, str):
        raise Refused("not_json")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise Refused("duplicate_key")
            out[key] = value
        return out

    def whole(token):
        if len(token.lstrip("-")) > 16:
            raise Refused("integer_out_of_range")
        number = int(token)
        if not -MAX_INT <= number <= MAX_INT:
            raise Refused("integer_out_of_range")
        return number

    def real(token):
        number = float(token)
        if number != number or number in (float("inf"), float("-inf")):
            raise Refused("number_out_of_range")
        return number

    def constant(token):
        raise Refused("not_a_number")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_int=whole, parse_float=real, parse_constant=constant)
    except Refused:
        raise
    except RecursionError:
        raise Refused("too_deep") from None
    except ValueError:
        raise Refused("not_json") from None
    check_value(value)
    return value


def check_value(value, depth: int = 0) -> None:
    """Refuse a value that ``read_strict`` could not have produced (a record can hold one all the same)."""
    if depth > MAX_DEPTH:
        raise Refused("too_deep")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise Refused("lone_surrogate")
    elif type(value) is int:
        if not -MAX_INT <= value <= MAX_INT:
            raise Refused("integer_out_of_range")
    elif type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise Refused("number_out_of_range")
    elif isinstance(value, list):
        for item in value:
            check_value(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise Refused("not_a_json_value")
            check_value(key, depth + 1)
            check_value(item, depth + 1)
    else:
        raise Refused("not_a_json_value")


# ------------------------------------------------------------------------------- the text of a value

def number_text(number) -> str:
    """A number as ECMAScript writes it (RFC 8785, section 3.2.2.3): ``1.0`` is ``1``, ``1e-7`` is ``1e-7``."""
    if type(number) is int:
        return str(number)
    if number == 0:
        return "0"                                  # and so is minus zero
    sign = "-" if number < 0 else ""
    mantissa, _, exponent = repr(abs(number)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits, point = whole + fraction, len(whole) + int(exponent or 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    count = len(digits)
    if count <= point <= 21:
        return sign + digits + "0" * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    power = point - 1
    tail = ("." + digits[1:]) if count > 1 else ""
    return f"{sign}{digits[0]}{tail}e{'+' if power >= 0 else '-'}{abs(power)}"


_SHORT = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}
# Characters a surface is never handed raw, as ranges of code points (first, last). Controls; the soft
# hyphen; the marks and overrides that reorder or hide text; line and paragraph separators; the byte
# order mark; the interlinear and replacement specials; private use; tags and variation selectors; and
# everything from plane 14 up. The list is explicit so that it does not move with a Unicode version.
NOT_SHOWN_RAW = ((0x00, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160),
                 (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x2028, 0x202F), (0x205F, 0x206F),
                 (0x3164, 0x3164), (0xE000, 0xF8FF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0),
                 (0xFFF0, 0xFFFF), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0x10FFFF))


def _raw_on_a_surface(point: int) -> bool:
    return not any(first <= point <= last for first, last in NOT_SHOWN_RAW)


def _escape(point: int) -> str:
    if point >= 0x10000:
        point -= 0x10000
        return "\\u%04x\\u%04x" % (0xD800 + (point >> 10), 0xDC00 + (point & 0x3FF))
    return "\\u%04x" % point


def _string_text(text: str, display: bool) -> str:
    out = ['"']
    for ch in text:
        point = ord(ch)
        if point in _SHORT:
            out.append(_SHORT[point])
        elif point < 0x20:
            out.append("\\u%04x" % point)
        elif display and not _raw_on_a_surface(point):
            out.append(_escape(point))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def text_of(value, display: bool = False) -> str:
    """The text of a value as RFC 8785 writes it: no spaces, keys in the order of their UTF-16 code
    units, numbers as ``number_text`` writes them, strings with the short escapes, lower-case ``\\u``
    escapes for the other characters below U+0020, and every other character as itself. The UTF-8 bytes
    of this text are what a digest of form ``rfc8785/v1`` is taken over.

    With ``display`` the text is the same except that each character of ``NOT_SHOWN_RAW`` is written as
    a ``\\u`` escape (a pair of them above U+FFFF). That is the text a surface prints: a character that
    could reorder, hide or forge part of a line never reaches it raw, and two different values still
    never give the same text, because an escape the engine itself wrote is shown with its backslash
    doubled.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) in (int, float):
        return number_text(value)
    if isinstance(value, str):
        return _string_text(value, display)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(text_of(item, display) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_string_text(key, display) + ":" + text_of(value[key], display) for key in keys) + "}"
    raise Refused("not_a_json_value")


# ------------------------------------------------------------------------------ the call of a tool

def check_call(call) -> None:
    """Refuse a call that is not in the strict profile of digest form ``rfc8785/v1``.

    A call is ``{"cwd": ..., "input": {...}, "tool": ...}`` and nothing else: the tool's name (1 to 256
    characters), the tool's input (an object) and the directory the call would run in (a string, or
    null when the engine does not say). Every value in it is one ``read_strict`` could have produced.
    The UTF-8 bytes of its text are at most ``CALL_MAX``: a larger call is refused, never cut.
    """
    if type(call) is not dict or set(call) != {"cwd", "input", "tool"} or type(call["input"]) is not dict:
        raise Refused("not_a_call")
    tool, cwd = call["tool"], call["cwd"]
    if not isinstance(tool, str) or not 1 <= len(tool) <= 256:
        raise Refused("tool")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise Refused("cwd")
    check_value(call)
    if len(text_of(call).encode("utf-8")) > CALL_MAX:
        raise Refused("too_large")


def call_of(tool, input_text: str, cwd) -> dict:
    """Everything an engine supplied for one tool call, as the value an approval binds.

    ``input_text`` is the tool's input as the engine wrote it; it is read by ``read_strict`` and must be
    an object. The directory and the tool's name are held to the same rules as the input: any text, no
    half surrogate pair, no normalisation.
    """
    value = read_strict(input_text)
    if type(value) is not dict:
        raise Refused("input_not_an_object")
    call = {"cwd": cwd, "input": value, "tool": tool}
    check_call(call)
    return call


def call_in(effect: dict) -> dict:
    """The engine's own part of an engine-tool effect: its ``tool``, its ``cwd`` and its ``input``."""
    return {"cwd": effect["cwd"], "input": effect["input"], "tool": effect["tool"]}


# --------------------------------------------------------------------------- csl-cj/v0 and the digest

def canonical_v0(value) -> bytes:
    """The bytes of ``csl-cj/v0``, the restricted canonical JSON the product signs and digests today.

    Objects with ASCII keys sorted by code point, arrays, ASCII strings, true, false, null, and whole
    numbers within the exact range. No fractions. Anything else is refused.
    """
    def check(item, depth):
        if depth > CJ_MAX_DEPTH:
            raise Refused("noncanonical_value")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if not -MAX_INT <= item <= MAX_INT:
                raise Refused("noncanonical_value")
        elif type(item) is str:
            if not item.isascii():
                raise Refused("noncanonical_value")
        elif type(item) in (list, tuple):
            for inner in item:
                check(inner, depth + 1)
        elif type(item) is dict:
            for key, inner in item.items():
                if type(key) is not str or not key.isascii():
                    raise Refused("noncanonical_value")
                check(inner, depth + 1)
        else:
            raise Refused("noncanonical_value")

    check(value, 0)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def effect_bytes(digest_form: str, effect) -> bytes:
    """The bytes a digest is taken over.

    ``rfc8785/v1``: the UTF-8 bytes of ``text_of(effect)``, the whole effect, the core's own fields and
    the engine's call alike. ``csl-cj/v0``: ``canonical_v0(effect)``. A form that is not one of the two
    is refused; a later form is a new name.
    """
    if digest_form == "rfc8785/v1":
        check_value(effect)
        return text_of(effect).encode("utf-8")
    if digest_form == "csl-cj/v0":
        return canonical_v0(effect)
    raise Refused("digest_form")


def digest_of(source: str, effect) -> str:
    """The digest an approval of ``effect`` carries: SHA-256, lower-case hex, over the form its source uses."""
    if source not in DIGEST_FORMS:
        raise Refused("source")
    return hashlib.sha256(effect_bytes(DIGEST_FORMS[source], effect)).hexdigest()


# ------------------------------------------------------------------------- the lines of an approval

def _field_lines(fields: dict) -> list:
    lines = []
    for key in sorted(fields, key=lambda key: key.encode("utf-16-be", "surrogatepass")):
        text = text_of(key, display=True) + ": " + text_of(fields[key], display=True)
        pieces = [text[start:start + LINE_PIECE] for start in range(0, len(text), LINE_PIECE)]
        lines.append(pieces[0])
        lines.extend("  " + piece for piece in pieces[1:])
    return [{"text": line, "trust": "untrusted"} for line in lines]


def approval_lines(source: str, effect: dict) -> list:
    """The lines every surface prints for an approval, drawn from the bound effect and nothing else.

    A line that is the core's own words is ``trusted``. A line that carries anything an engine or a
    plugin supplied is ``untrusted``, and that text is always written as ``text_of`` writes it for
    display, quotes included: no control character and no character that reorders or hides text reaches
    a surface, and two different values never draw the same line. Every field of the effect is shown.
    A value longer than ``LINE_PIECE`` characters continues on following lines that start with two
    spaces (a line that starts a field starts with a quote). Nothing is ever cut: an effect that needs
    more than ``LINES_MAX`` lines is refused.
    """
    def core(text):
        return {"text": text, "trust": "trusted"}

    def theirs(label, value):
        return {"text": label + text_of(value, display=True), "trust": "untrusted"}

    if source == "engine_tool":
        call = call_in(effect)
        check_call(call)
        lines = [core("Tool call waiting for an answer"),
                 core("engine: " + effect["engine"]),
                 core("run: " + effect["run_id"]),
                 theirs("tool: ", call["tool"]),
                 theirs("directory: ", call["cwd"]),
                 core("input fields: %d" % len(call["input"]))]
        lines += _field_lines(call["input"])
    elif source == "plugin_effect":
        scope = effect["scope"]
        amount = text_of(effect["amount_minor"], display=True) + " minor units of " + text_of(effect["currency"], display=True)
        lines = [core("Plugin effect waiting for an answer"),
                 theirs("plugin: ", effect["plugin_id"]),
                 theirs("kind: ", effect["kind"]),
                 theirs("run: ", effect["run_id"]),
                 theirs("connection: ", effect["connection"]),
                 theirs("account: ", scope["account"]),
                 theirs("venue: ", scope["venue"]),
                 theirs("contract family: ", scope["contract_family"]),
                 theirs("adapter: ", scope["adapter"]),
                 theirs("environment: ", scope["environment"]),
                 {"text": "amount: " + amount, "trust": "untrusted"},
                 core("payload fields: %d" % len(effect["payload"]))]
        lines += _field_lines(effect["payload"])
    else:
        raise Refused("source")
    if len(lines) > LINES_MAX:
        raise Refused("too_many_lines")
    return lines


def approval_problems(record: dict) -> list:
    """What does not agree inside one approval record; an empty list when everything does.

    ``call``: the engine effect's call is not in its profile. ``digest_form``: the form is not the one
    the source uses. ``digest``: the digest is not the digest of the effect. ``rendered``: the lines are
    not the lines of the effect. ``opened_by``: an opening names a revision the request has not reached,
    or the same showing is recorded twice.
    """
    problems = []
    source, effect = record.get("source"), record.get("effect")
    if source == "engine_tool":
        try:
            check_call(call_in(effect))
        except (Refused, KeyError, TypeError):
            problems.append("call")
    if not isinstance(source, str) or record.get("digest_form") != DIGEST_FORMS.get(source):
        problems.append("digest_form")
    try:
        if hashlib.sha256(effect_bytes(record.get("digest_form"), effect)).hexdigest() != record.get("digest"):
            problems.append("digest")
    except Refused:
        problems.append("digest")
    try:
        if approval_lines(source, effect) != record.get("rendered"):
            problems.append("rendered")
    except (Refused, KeyError, TypeError):
        problems.append("rendered")
    seen = []
    for opening in record.get("opened_by") or ():
        who = (opening.get("principal"), opening.get("surface"), opening.get("revision"))
        if who in seen or not isinstance(opening.get("revision"), int) or opening["revision"] > record.get("revision", 0):
            problems.append("opened_by")
            break
        seen.append(who)
    return problems


# ------------------------------------------------------------------------------------ the transcript

def milliseconds(timestamp: str) -> int:
    """A timestamp of these records as milliseconds since the start of 1970, or refuse.

    The pattern in the schemas takes any day from 01 to 31 in any month. A date that does not exist
    (2026-02-31) is refused here, with ``timestamp``. A second of 60 is read as the minute's last
    second plus one.
    """
    try:
        day, clock = timestamp[:-1].split("T")
        year, month, date = (int(part) for part in day.split("-"))
        hour, minute, rest = clock.split(":")
        second, milli = rest.split(".")
        days = datetime.date(year, month, date).toordinal() - datetime.date(1970, 1, 1).toordinal()
        return ((days * 24 + int(hour)) * 60 + int(minute)) * 60000 + int(second) * 1000 + int(milli)
    except (ValueError, AttributeError, TypeError):
        raise Refused("timestamp") from None


_STATE_FROM = {"turn.ended": "outcome", "approval.answered": "state", "stop.result": "code", "steer.queued": "state",
               "interrupt": "outcome"}
_TEXT_FROM = {"engine.output": "text", "turn.started": "text", "tool.result": "text", "result": "detail"}


def _plain(text) -> bool:
    return text is None or all(ch in "\t\n" or " " <= ch <= "~" for ch in text)


def transcript_fields(events: list) -> list:
    """What each line of a transcript must carry because its event says so: one dict for each event of a
    log, in order, the log's first event first.

    ``seq``, ``event_type``, ``source`` and ``trust`` are the event's. ``time_rel_ms`` is the event's
    time minus the time of the log's first event, in milliseconds, and never less than the line before
    it: when the clock steps back, a line keeps the value of the line before, so the time of a
    transcript never runs backwards and is never negative. ``state`` is the state word the event
    reports: for state.changed the entity's state in the post-image (for a run the stored record's, or
    the daemon row's when there is no stored record); for turn.ended its outcome; for approval.answered
    its state; for stop.result its code; for steer.queued its state; for interrupt its outcome; null
    for every other type.

    ``text`` is given only where the text is the event's own: the payload's text for engine.output,
    turn.started and tool.result, the detail of a result, and the rendered lines of an
    approval.requested joined by line feeds. Text that came from an engine goes through the core's
    sanitizer, whose rule is not part of these records, so this function gives the text of such an
    event only when it is plain (printable ASCII, tabs and line feeds), which a sanitizer leaves as it
    is. For every other type the text is a sentence of the core's renderer and is not given here; nor
    are ``speech`` and ``focus``.
    """
    lines, first, last = [], None, 0
    for event in events:
        at = milliseconds(event["time"])
        first = at if first is None else first
        last = max(last, at - first)
        kind, payload = event["type"], event["payload"]
        if kind == "state.changed":
            image = payload["post_image"]
            if payload["entity"] == "run":
                image = image["control_record"] or image["daemon_row"]
            state = image["state"]
        else:
            state = payload[_STATE_FROM[kind]] if kind in _STATE_FROM else None
        line = {"seq": event["seq"], "time_rel_ms": last, "event_type": kind, "source": event["source"],
                "trust": event["trust"], "state": state}
        if kind == "approval.requested":
            line["text"] = "\n".join(item["text"] for item in payload["rendered"])
        elif kind in _TEXT_FROM and _plain(payload[_TEXT_FROM[kind]]):
            line["text"] = payload[_TEXT_FROM[kind]]
        lines.append(line)
    return lines


def transcript_problems(events: list, lines: list) -> list:
    """Where the lines are not the projection of the events: ``"<line number>:<field>"`` for each field a
    line carries that is not what its event says, ``"count"`` when there is not one line for each event,
    ``"refused:<code>"`` when an event's time is not a moment that exists."""
    try:
        fields = transcript_fields(events)
    except Refused as refusal:
        return ["refused:" + refusal.code]
    problems = [] if len(lines) == len(fields) else ["count"]
    for number, (line, wanted) in enumerate(zip(lines, fields), 1):
        problems += [f"{number}:{key}" for key, value in wanted.items() if line.get(key) != value]
    return problems


# ------------------------------------------------------------------------- the delivery of a queue item

def queue_step(item: dict, happening: str, facts: dict | None = None) -> dict:
    """One thing that happens to a queued message, and the item after it; or refuse.

    ``begin`` (facts: delivery_id): the daemon COMMITS its intent to deliver, before anything is written
    toward the engine: the item becomes ``delivering`` under that delivery id and can no longer be
    edited or withdrawn. Only a ``queued`` item can begin (``not_queued``), so nothing is ever sent a
    second time by itself. ``written`` (facts: delivery_id): the write toward the engine returned. It
    changes NOTHING in the item: a crash before the write and a crash after it leave the same
    ``delivering`` item, which is the point. Only a ``delivering`` item is written
    (``not_delivering``), under its own delivery id (``delivery_mismatch``). ``accepted`` (facts:
    delivery_id, turn_id): the engine's own typed acceptance, an ``input.accepted`` event, names the
    delivery: the item becomes ``delivered`` and carries the turn that took it. Nothing else makes an
    item ``delivered``. An acceptance of an item that is not ``delivering`` changes nothing
    (``not_delivering``), and neither does one that names another delivery (``delivery_mismatch``).
    ``restart`` (the daemon starts again) and ``turn_ended`` (the turn the item was written into
    ended): an item still ``delivering`` has no acceptance and becomes ``outcome_unknown``, which is
    shown as sent and not confirmed; any other item is unchanged. ``edit`` (facts: text and/or
    delivery) and ``cancel``: only while ``queued`` (``not_queued``); an edit raises the revision.
    """
    facts = facts or {}
    after = copy.deepcopy(item)
    state = item["state"]
    if happening == "begin":
        if state != "queued":
            raise Refused("not_queued")
        after.update(state="delivering", delivery_id=facts["delivery_id"])
    elif happening in ("written", "accepted"):
        if state != "delivering":
            raise Refused("not_delivering")
        if facts["delivery_id"] != item["delivery_id"]:
            raise Refused("delivery_mismatch")
        if happening == "accepted":
            after.update(state="delivered", turn_id=facts["turn_id"])
    elif happening in ("restart", "turn_ended"):
        if state == "delivering":
            after.update(state="outcome_unknown")
    elif happening in ("edit", "cancel"):
        if state != "queued":
            raise Refused("not_queued")
        if happening == "cancel":
            after.update(state="cancelled")
        else:
            after.update({key: facts[key] for key in ("text", "delivery") if key in facts}, revision=item["revision"] + 1)
    else:
        raise Refused("happening")
    return after


# ----------------------------------------------------------------------------------------- the Task

def _log_times(events: list, of_turn: str | None = None) -> tuple:
    """created_at and updated_at as the core logged them: the time of the first event, and of the latest
    state.changed; null where the log holds none."""
    mine = [event for event in events if of_turn is None or event.get("turn_id") == of_turn]
    changed = [event["time"] for event in mine if event["type"] == "state.changed"]
    return (mine[0]["time"] if mine else None), (changed[-1] if changed else None)


def task_of_run(view: dict, events: list = (), pending_approval_ids: list = ()) -> dict:
    """What the Task of a run started without a session must say, from the run's view and its log.

    The Task has no store of its own: these fields are derived each time, so they survive a restart.
    Starting, running, a Stop pending and an unobservable unit are ``working`` (``input_required``
    while an approval is pending), and a Stop that was asked and is not confirmed sets
    ``cancel_requested``. ``stopped`` is ``cancelled``: only a confirmed Stop is. A run that ended with
    a result is ``completed`` when the result carries no code and says ok, and ``failed`` with the
    result's code when it carries one. (The route contract writes ok exactly when there is no code. A
    result that breaks that is never ``completed``: one that says ok and carries a code is ``failed``
    with that code, and one that says error and carries none is ``failed`` with the code
    ``result_invalid``.) The Task's ``result_ref`` is the run id, and the whole result stays in run.status. A run
    that ended with NO result kept is ``failed`` with the code ``result_unavailable``. A start that
    never launched is ``failed`` with the code ``start_failed``; its detail is the code of the record's
    ``start_error`` when the record carries one, and null when it does not (the reason is not durable
    until the lane that owns Stop writes it). A run lost at a reboot is ``failed`` with the code
    ``lost_reboot``. A Task of a run has no turn outcome and does not expire.
    """
    state, run_id, result = view["state"], view["run_id"], view.get("result")
    created_at, updated_at = _log_times(list(events))
    task = {"task_id": run_id, "source": "run", "session_id": None, "run_id": run_id, "turn_id": None,
            "outcome": None, "cancel_requested": False, "pending_approval_ids": [], "expires_at": None,
            "created_at": created_at, "updated_at": updated_at, "result": None}
    if state in ("starting", "running", "stop_pending", "unknown"):
        task["pending_approval_ids"] = list(pending_approval_ids)
        task["status"] = "input_required" if pending_approval_ids else "working"
        task["cancel_requested"] = view.get("stop") is not None
    elif state == "stopped":
        task["status"] = "cancelled"
    elif state == "ended" and result is not None:
        ok = result["code"] is None and result["status"] == "ok"
        task["status"] = "completed" if ok else "failed"
        task["result"] = {"code": None if ok else (result["code"] or "result_invalid"), "detail": result["detail"],
                          "result_ref": run_id}
    elif state == "ended":
        task["status"] = "failed"
        task["result"] = {"code": "result_unavailable", "detail": None, "result_ref": None}
    elif state == "start_failed":
        kept = view.get("start_error")
        task["status"] = "failed"
        task["result"] = {"code": "start_failed", "detail": kept["code"] if kept else None, "result_ref": None}
    elif state == "lost_reboot":
        task["status"] = "failed"
        task["result"] = {"code": "lost_reboot", "detail": None, "result_ref": None}
    else:
        raise Refused("state")
    return task


def task_of_turn(session: dict, turn_id: str, events: list = (), pending_approval_ids: list = ()) -> dict:
    """What the Task of a session's turn must say, from the session record and its log.

    It is never derived from the latest result of a run that several turns share: the result is the
    turn's own and so is the expiry. Queued and running are ``working`` (``input_required`` while an
    approval is pending for it); ``ended`` is ``completed``, with the turn's outcome so that a caller
    can tell done from waiting; ``failed`` is ``failed``; ``interrupted`` and ``stopped`` are
    ``cancelled``. Cancelling a turn's Task is an interrupt of that turn, not a Stop: the worker may
    live on. ``cancel_requested`` is true while the session's Stop is pending and the turn has not ended.
    """
    turn = next((item for item in session["turns"] if item["turn_id"] == turn_id), None)
    if turn is None:
        raise Refused("turn_unknown")
    created_at, updated_at = _log_times(list(events), of_turn=turn_id)
    live = turn["state"] in ("queued", "running")
    status = {"queued": "working", "running": "working", "ended": "completed", "failed": "failed",
              "interrupted": "cancelled", "stopped": "cancelled"}[turn["state"]]
    if live and pending_approval_ids:
        status = "input_required"
    return {"task_id": turn_id, "source": "turn", "session_id": session["session_id"], "run_id": turn["run_id"],
            "turn_id": turn_id, "status": status, "outcome": turn["outcome"],
            "cancel_requested": live and session["state"] == "stop_pending",
            "pending_approval_ids": list(pending_approval_ids) if live else [], "expires_at": turn["expires_at"],
            "created_at": created_at, "updated_at": updated_at, "result": turn["result"]}


def task_problems(wanted: dict, task: dict) -> list:
    """The fields of a Task that are not what its source says, by name. What the rule does not decide
    (the message for a person, the advice on when to ask again) is not compared."""
    return [key for key, value in wanted.items() if task.get(key) != value]


# ------------------------------------------------------------------- approval.open and approval.answer

class ApprovalModel:
    """approval.open and approval.answer on one request, one call at a time.

    ``revision`` counts changes to the request's CONTENT: its effect, its rendered lines, its deadline.
    It starts at 1. A showing does not change it and neither does an answer: the state of the request
    changes, its content does not. An opening carries the revision that was shown, so if the content
    ever changes (a deadline moved, say), the revision rises and everyone must look again before an
    answer is taken.

    The order of checks for approval.answer: the caller is an owner or a device (else scope_refused);
    the same command id with the same params returns the first answer, and with other params is
    'conflict' (command_id_reused); a request that is no longer pending is 'conflict'
    (already_resolved); a revision that is not the current one is 'conflict' (stale_revision); a caller
    with no opening on that surface at the current revision is 'prompt_not_shown'; a digest that is not
    the request's is 'conflict' (digest_mismatch). Only then is the answer taken.
    """

    def __init__(self, record: dict) -> None:
        self.record = copy.deepcopy(record)
        self.receipts: dict = {}

    def call(self, principal: dict, method: str, params: dict, at: str) -> dict:
        if principal.get("kind") not in ("owner", "device"):
            return {"code": "scope_refused", "detail": "approve"}
        receipt_key = (method, params.get("command_id"))
        payload = text_of({key: value for key, value in params.items() if key != "command_id"})
        if receipt_key in self.receipts:
            first_payload, first_answer = self.receipts[receipt_key]
            if first_payload == payload:
                return copy.deepcopy(first_answer)
            return {"code": "conflict", "detail": "command_id_reused"}
        answer = self._open(principal, params, at) if method == "approval.open" else self._answer(principal, params, at)
        self.receipts[receipt_key] = (payload, copy.deepcopy(answer))
        return answer

    def _open(self, principal: dict, params: dict, at: str) -> dict:
        record = self.record
        if params.get("approval_id") != record["approval_id"]:
            return {"code": "approval_unknown", "approval_id": params.get("approval_id")}
        shown = {"principal": principal, "surface": params["surface"], "revision": record["revision"], "at": at}
        already = any((o["principal"], o["surface"], o["revision"]) == (principal, params["surface"], record["revision"])
                      for o in record["opened_by"])
        if record["state"] == "pending" and not already:
            record["opened_by"].append(shown)
        return {"code": "ok", "approval": copy.deepcopy(record)}

    def _answer(self, principal: dict, params: dict, at: str) -> dict:
        record = self.record
        if params.get("approval_id") != record["approval_id"]:
            return {"code": "approval_unknown", "approval_id": params.get("approval_id")}
        refusal = {"approval_id": record["approval_id"]}
        if record["state"] != "pending":
            return {"code": "conflict", "detail": "already_resolved", **refusal}
        if params["expected_revision"] != record["revision"]:
            return {"code": "conflict", "detail": "stale_revision", "current_revision": record["revision"], **refusal}
        if not any((o["principal"], o["surface"], o["revision"]) == (principal, params["surface"], record["revision"])
                   for o in record["opened_by"]):
            return {"code": "prompt_not_shown", **refusal}
        if params["digest"] != record["digest"]:
            return {"code": "conflict", "detail": "digest_mismatch", **refusal}
        record.update(state="approved" if params["answer"] == "approve" else "denied", answered_by=principal,
                      answer=params["answer"], answered_at=at)
        return {"code": "ok", "approval": copy.deepcopy(record)}
