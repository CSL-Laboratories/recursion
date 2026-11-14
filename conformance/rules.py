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
* ``normalised`` and ``unnormalised``: the bound form of a number, for a value that is already held.
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
* ``DeliveryModel``: what happens to a queued message, one happening at a time, against the binding that
  is committed before anything is written.
* ``task_of_run``, ``task_of_turn`` and ``task_problems``: the Task of a run or of a turn, derived.
* ``ApprovalModel``: approval.open and approval.answer step by step, with replay; the deadline; a change
  of content; the single consumption of an approved effect.
* ``start_intent_text``, ``start_key_of`` and ``start_retry``: the identity of a start request.
* ``cursor_answer``, ``page_problems``, ``transcript_page_problems`` and ``gap_problem``: reading a log
  from a cursor, in the projection that was asked for.
* ``post_image_digest`` and ``input_digest``: the digests an event carries.
"""
from __future__ import annotations

import copy
import decimal
import datetime
import hashlib
import json

MAX_INT = 2**53 - 1          # the largest whole number every binary64 reader keeps exact
MAX_DEPTH = 64               # of a tool's input
CJ_MAX_DEPTH = 32            # of csl-cj/v0, as plugin_security/canonical.py has it
CALL_MAX = 262144            # UTF-8 bytes of a call's text; a larger call is never drawn and never approved
LINE_PIECE = 4000            # characters of text on one rendered line, its label included; the rest continues
LINE_MAX = 4096              # characters of one rendered line, as the record's schema caps it
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
    twice, at any depth (``duplicate_key``); ``NaN`` and the infinities (``not_a_number``); half of a
    surrogate pair on its own, in a string or a key (``lone_surrogate``); nesting deeper than 64
    (``too_deep``). Strings are kept exactly as written: nothing is normalised.

    NUMBERS. Admission does not depend on how a number is spelled, nor on whether the host language
    would call it an integer or a float. An integral value outside ``-MAX_INT .. MAX_INT`` is refused
    (``integer_out_of_range``) whether it is written as digits, with a fraction or with an exponent, and
    the check is made on the exact value, before any conversion that could lose it. A number whose
    conversion to binary64 gives an integral value outside that range is refused the same way. Every
    other number is a finite binary64 (one that is not finite is ``number_out_of_range``). A quantity
    or an identifier that needs more than that is sent as a string.

    Exact numeric range checks are independent of the host Decimal context and never escape as host
    arithmetic exceptions. Every integral value outside the safe range, including 1e1000000, is refused
    as integer_out_of_range.

    THE VALUE THAT IS BOUND IS THE VALUE THAT IS DISPATCHED. What this returns is normalised: a number
    whose value is integral is the whole number, so ``1.0`` is ``1`` and minus zero is ``0``. Writing an
    admitted value as RFC 8785 writes it and reading that text again gives an admitted value with the
    same bytes. ``normalised`` and ``unnormalised`` state the same rule for a value that is already
    held, and ``check_call`` refuses a held call that breaks it.
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

    def number(token):
        try:
            exact = decimal.Decimal(token)
        except decimal.InvalidOperation:        # an exponent beyond what an exact decimal holds
            exact = None
        if exact is not None and not exact.is_finite():     # the same, where the host's context does not trap it
            exact = None
        if exact is not None and exact == exact.to_integral_value():
            # copy_abs, not abs(): abs() rounds in the host's Decimal context and raises its Overflow for 1e1000000.
            if exact.copy_abs() > MAX_INT:
                raise Refused("integer_out_of_range")
            return int(exact)
        value = float(token)
        if value != value or value in (float("inf"), float("-inf")):
            raise Refused("integer_out_of_range" if exact is None else "number_out_of_range")
        if value.is_integer():
            if abs(value) > MAX_INT:
                raise Refused("integer_out_of_range")
            return int(value)
        return value

    def constant(token):
        raise Refused("not_a_number")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_int=number, parse_float=number, parse_constant=constant)
    except Refused:
        raise
    except RecursionError:
        raise Refused("too_deep") from None
    except ValueError:
        raise Refused("not_json") from None
    check_value(value)
    return value


def check_value(value, depth: int = 0) -> None:
    """Refuse a value that ``read_strict`` could not have produced (a record can hold one all the same).

    A number is judged by its VALUE, not by its type in this language: an integral value outside the
    exact range is refused whether it arrives as a whole number or as a float.
    """
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
        if value.is_integer() and abs(value) > MAX_INT:
            raise Refused("integer_out_of_range")
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


def unnormalised(value) -> bool:
    """True when a held value carries a number that is not in its bound form: a float whose value is
    integral (``1.0``, where the bound value is the whole number ``1``) or minus zero (bound as ``0``).

    The text RFC 8785 writes is the same for both spellings, so a digest cannot see the difference; a
    dispatch can. In a language with one number type the first case does not exist and the second is
    ``Object.is(x, -0)``.
    """
    if type(value) is float:
        return value.is_integer()
    if isinstance(value, dict):
        return any(unnormalised(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(unnormalised(item) for item in value)
    return False


def normalised(value):
    """The same value in its bound form: every float whose value is integral and inside the exact range is
    the whole number, and so minus zero is ``0``. Nothing else is touched, and a value outside the profile
    is left for ``check_value`` to refuse. This is what ``read_strict`` does to what it reads; a value that
    reaches the core another way (a changed effect, a record read back from where it is durable) goes
    through this before it is digested, drawn or carried out.
    """
    if type(value) is float and value.is_integer() and abs(value) <= MAX_INT:
        return int(value)
    if isinstance(value, dict):
        return {key: normalised(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalised(item) for item in value]
    return value


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

    Before creating, changing or restoring a bound engine-tool effect, normalize its numeric values
    under this profile. Already-bound-effect validation and consumption must reject a nonnormalized
    effect; successful call-text parsing is not the only enforcement boundary. Digesting, rendering and
    dispatch use that same normalized value. This does not change csl-cj/v0 plugin effects or existing
    signature formats. So a held call that carries an integral float or minus zero is refused here
    (``not_normalised``): its text and its digest would be those of the normalised call, and what would
    be dispatched would not.
    """
    if type(call) is not dict or set(call) != {"cwd", "input", "tool"} or type(call["input"]) is not dict:
        raise Refused("not_a_call")
    tool, cwd = call["tool"], call["cwd"]
    if not isinstance(tool, str) or not 1 <= len(tool) <= 256:
        raise Refused("tool")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise Refused("cwd")
    check_value(call)
    if unnormalised(call):
        raise Refused("not_normalised")
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
    is refused; a later form is a new name. An effect of the first form that holds a number which is
    not normalised has no bytes here (``not_normalised``): the digest is taken over the value that is
    dispatched and over no other.
    """
    if digest_form == "rfc8785/v1":
        check_value(effect)
        if unnormalised(effect):
            raise Refused("not_normalised")
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
def _wrap(text: str, trust: str) -> list:
    """One piece of text as rendered lines. A line holds at most ``LINE_PIECE`` characters of it, the label
    included; the rest continues on following lines that start with two spaces. Nothing is cut."""
    pieces = [text[start:start + LINE_PIECE] for start in range(0, len(text), LINE_PIECE)]
    return [{"text": pieces[0], "trust": trust}] + [{"text": "  " + piece, "trust": trust} for piece in pieces[1:]]


def _field_lines(fields: dict) -> list:
    lines = []
    for key in sorted(fields, key=lambda key: key.encode("utf-16-be", "surrogatepass")):
        lines += _wrap(text_of(key, display=True) + ": " + text_of(fields[key], display=True), "untrusted")
    return lines


def approval_lines(source: str, effect: dict) -> list:
    """The lines every surface prints for an approval, drawn from the bound effect and nothing else.

    EVERY VARIABLE MEMBER OF THE EFFECT IS ON A LINE, the session or log it is bound to included. A
    line that is the core's own words is ``trusted``. A line that carries anything an engine or a
    plugin supplied is ``untrusted``, and ALL such text (the directory and each field of a plugin's
    effect as much as the input) goes through one encoder: it is written as ``text_of`` writes it for
    display, quotes included, so no control character and no character that reorders or hides text
    reaches a surface and two different values never draw the same line; and it is wrapped by
    ``_wrap``, where the label counts toward the length of the line. A line that starts a field
    starts with its label or with a quote; a continuation starts with two spaces. Nothing is ever
    cut: an effect that needs more than ``LINES_MAX`` lines is refused (``too_many_lines``), and no
    approval is made of it.
    """
    def core(text):
        return [{"text": text, "trust": "trusted"}]

    def theirs(label, value):
        return _wrap(label + text_of(value, display=True), "untrusted")

    if source == "engine_tool":
        call = call_in(effect)
        check_call(call)
        lines = (core("Tool call waiting for an answer") + core("engine: " + effect["engine"])
                 + core("session: " + (effect["session_id"] if effect["session_id"] is not None else "none"))
                 + core("run: " + effect["run_id"]) + theirs("tool: ", call["tool"]) + theirs("directory: ", call["cwd"])
                 + core("input fields: %d" % len(call["input"])) + _field_lines(call["input"]))
    elif source == "plugin_effect":
        scope = effect["scope"]
        amount = text_of(effect["amount_minor"], display=True) + " minor units of " + text_of(effect["currency"], display=True)
        lines = (core("Plugin effect waiting for an answer") + theirs("plugin: ", effect["plugin_id"])
                 + theirs("kind: ", effect["kind"]) + theirs("run: ", effect["run_id"])
                 + theirs("connection: ", effect["connection"]) + theirs("account: ", scope["account"])
                 + theirs("venue: ", scope["venue"]) + theirs("contract family: ", scope["contract_family"])
                 + theirs("adapter: ", scope["adapter"]) + theirs("environment: ", scope["environment"])
                 + _wrap("amount: " + amount, "untrusted") + core("payload fields: %d" % len(effect["payload"]))
                 + _field_lines(effect["payload"]))
    else:
        raise Refused("source")
    if len(lines) > LINES_MAX:
        raise Refused("too_many_lines")
    if any(len(line["text"]) > LINE_MAX for line in lines):
        raise Refused("line_too_long")
    return lines


# Every member of an effect is on a line, and nothing else is bound: so an effect has exactly these members.
EFFECT_MEMBERS = {"engine_tool": {"kind", "engine", "session_id", "run_id", "tool", "cwd", "input"},
                  "plugin_effect": {"schema", "kind", "plugin_id", "run_id", "connection", "scope", "currency", "amount_minor",
                                    "payload"}}
SCOPE_MEMBERS = {"account", "venue", "contract_family", "adapter", "environment"}


def approval_problems(record: dict) -> list:
    """What does not agree inside one approval record; an empty list when everything does.

    ``effect``: the effect does not have exactly the members of its source's effect (and, for a plugin's
    effect, its scope exactly the five of a scope). A member more would be bound by the digest and shown
    on no line; a member less has nothing to draw. ``call``: the engine effect's call is not in its
    profile; that includes a call that holds a number which is not normalised (an integral float, minus
    zero), and such a record has no digest and no lines either. ``digest_form``: the form is not the one
    the source uses. ``digest``: the digest is not the digest of the effect. ``rendered``: the lines are
    not the lines of the effect (an effect that cannot be drawn whole has no lines, so any lines are
    wrong). ``envelope``: the record's own run, or for an engine's call its session, is not the one its
    effect is bound to. ``opened_by``: an opening names a revision the request has not reached, or the
    same showing is recorded twice.
    """
    problems = []
    source, effect = record.get("source"), record.get("effect")
    if (not isinstance(effect, dict) or set(effect) != EFFECT_MEMBERS.get(source)
            or (source == "plugin_effect" and (not isinstance(effect["scope"], dict) or set(effect["scope"]) != SCOPE_MEMBERS))):
        problems.append("effect")
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
    if isinstance(effect, dict) and (record.get("run_id") != effect.get("run_id") or (
            source == "engine_tool" and record.get("session_id") != effect.get("session_id"))):
        problems.append("envelope")
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
    (2026-02-31) is refused here, with ``timestamp``.

    The real-instant check validates the seconds spelling as well as the calendar date. A :60 spelling
    is accepted only for an actual UTC leap second supported by the timestamp profile; it is not
    normalized from an arbitrary minute into its successor. Otherwise it is refused as timestamp. THE
    PROFILE OF THESE RECORDS SUPPORTS NO LEAP SECOND: seconds are 00 to 59, so every :60 is refused, on
    a day that had a leap second as on any other. The check stands on its own: the hour is 00 to 23, the
    minute and the second 00 to 59, and the text has exactly this shape, whatever a pattern said before.
    """
    try:
        # The exact spelling, digit for digit: the ten ASCII digits and no other character a language calls one.
        if "".join("0" if ch in "0123456789" else ch for ch in timestamp) != "0000-00-00T00:00:00.000Z":
            raise ValueError(timestamp)
        year, month, date = int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10])
        hour, minute, second, milli = int(timestamp[11:13]), int(timestamp[14:16]), int(timestamp[17:19]), int(timestamp[20:23])
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError(timestamp)
        days = datetime.date(year, month, date).toordinal() - datetime.date(1970, 1, 1).toordinal()
        return ((days * 24 + hour) * 60 + minute) * 60000 + second * 1000 + milli
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
BINDING_KEYS = ("delivery_id", "item_id", "session_id", "run_id", "worker_incarnation", "target_turn_id", "how")
# What a binding's 'how' may be for each 'delivery' an item can ask for.
HOW_OF = {"next_turn": ("new_turn",), "steer": ("mid_turn", "new_turn")}


class DeliveryModel:
    """One queued message, and everything that can happen to it, one happening at a time.

    ``step(happening, facts)`` changes the item, or refuses with a code and changes nothing.

    ``begin`` (facts: the BINDING). The daemon COMMITS, in one write and before anything is written
    toward the engine: the item as ``delivering``, a delivery id that is never used again, and the full
    target binding: the delivery, the item, the log, the run, the worker's incarnation, the turn the
    input is aimed at and how (``new_turn``, whose turn id is allocated before the dispatch, or
    ``mid_turn``). Only this transition owns the write. Only a ``queued`` item can begin
    (``not_queued``), so seeing ``delivering``, retrying a callback or replaying a command never sends
    again. A binding that does not name this item and its log, or is not whole, is refused (``binding``).
    So is one whose ``how`` the item's own ``delivery`` does not allow: a message for the next turn
    (``next_turn``) is never written into the turn in progress, so its ``how`` is ``new_turn``; a
    ``steer`` goes into the turn in progress (``mid_turn``) or, where the driver cannot take input in
    the middle of a turn, begins the next one (``new_turn``).

    ``written`` (facts: delivery_id, and for a write into a turn in progress the turn). The write
    returned. It changes nothing in the item: a crash before the write and a crash after it leave the
    same ``delivering`` item. There is AT MOST ONE write for one binding: a second is refused
    (``already_written``); one that names another delivery, another item or another turn, or names no
    delivery at all, is ``delivery_mismatch``.

    ``accepted`` (facts: the acceptance as the engine's typed event carries it: delivery_id, session_id,
    run_id, turn_id, how). The core checks every one of them against the durable binding. All equal: the
    item is ``delivered`` and carries the turn that took it. Any difference (another delivery, another
    log, another run, another turn, the other ``how``) is ``delivery_mismatch`` and changes nothing.
    Nothing else makes an item ``delivered``.

    ``restart`` (the daemon starts again): an item still ``delivering`` has no acceptance and becomes
    ``outcome_unknown``, whichever ``how`` its binding says. Its delivery outcome is unknown and the
    message may not have been sent; it is not sent again by itself.

    ``turn_ended`` (facts: turn_id). A turn_ended notification carries the ended turn's identity. If an
    item is still delivering and that identity equals its durable binding's target_turn_id, atomically
    quarantine it as outcome_unknown and record the outcome-unknown event, for both new_turn and
    mid_turn deliveries. Ending a different turn changes nothing. A subsequent acceptance is retained
    as evidence and does not promote the quarantined item automatically. A notification that names no
    turn is refused (``turn``) and changes nothing.

    An acceptance that only arrives after the item became ``outcome_unknown`` is refused
    (``not_delivering``) and the item stays as it is, but the acceptance is KEPT, in ``evidence``, for
    a later reconciliation that proves the binding: it is never dropped and never promotes the item
    by itself.

    ``edit`` (facts: text and/or delivery) and ``cancel``: only while ``queued`` (``not_queued``); an
    edit raises the revision. The edited item is made as a whole and checked before it replaces the
    item: an edit that names neither field, a text that is not text or is empty, and a delivery that is
    not one of the two are refused (``edit``) and nothing changes, the revision included.

    Every refusal leaves the item, the binding, the record of the write and the evidence as they were.
    """

    def __init__(self, item: dict) -> None:
        self.item = copy.deepcopy(item)
        self.binding: dict | None = None
        self.written = False
        self.evidence: list = []

    def step(self, happening: str, facts: dict | None = None) -> None:
        facts = facts or {}
        item, state, binding = self.item, self.item["state"], self.binding
        if happening == "begin":
            if state != "queued":
                raise Refused("not_queued")
            if (set(facts) != set(BINDING_KEYS) or facts["item_id"] != item["item_id"]
                    or facts["session_id"] != item["session_id"] or facts["how"] not in HOW_OF.get(item["delivery"], ())
                    or not all(isinstance(facts[key], str) and facts[key] for key in BINDING_KEYS)):
                raise Refused("binding")
            self.binding, self.written = dict(facts), False
            item.update(state="delivering", delivery_id=facts["delivery_id"])
        elif happening == "written":
            if state != "delivering":
                raise Refused("not_delivering")
            if (facts.get("delivery_id") != binding["delivery_id"] or facts.get("item_id", binding["item_id"]) != binding["item_id"]
                    or facts.get("turn_id", binding["target_turn_id"]) != binding["target_turn_id"]):
                raise Refused("delivery_mismatch")
            if self.written:
                raise Refused("already_written")
            self.written = True
        elif happening == "accepted":
            if state != "delivering":
                if state == "outcome_unknown":
                    self.evidence.append(dict(facts))      # kept for reconciliation; never dropped
                raise Refused("not_delivering")
            if any(facts.get(theirs) != binding[ours] for theirs, ours in (
                    ("delivery_id", "delivery_id"), ("session_id", "session_id"), ("run_id", "run_id"),
                    ("turn_id", "target_turn_id"), ("how", "how"))):
                raise Refused("delivery_mismatch")
            item.update(state="delivered", turn_id=binding["target_turn_id"])
        elif happening == "restart":
            if state == "delivering":
                item.update(state="outcome_unknown")
        elif happening == "turn_ended":
            ended = facts.get("turn_id")
            if not isinstance(ended, str) or not ended:
                raise Refused("turn")
            if state == "delivering" and ended == binding["target_turn_id"]:      # whichever 'how' the binding says
                item.update(state="outcome_unknown")
        elif happening in ("edit", "cancel"):
            if state != "queued":
                raise Refused("not_queued")
            if happening == "cancel":
                item.update(state="cancelled")
            else:
                changes = {key: facts[key] for key in ("text", "delivery") if key in facts}
                if (not changes or not isinstance(changes.get("text", "text"), str) or not changes.get("text", "text")
                        or not isinstance(changes.get("delivery", "steer"), str) or changes.get("delivery", "steer") not in HOW_OF):
                    raise Refused("edit")
                item.update(changes, revision=item["revision"] + 1)
        else:
            raise Refused("happening")


# ----------------------------------------------------------------------------------------- the Task

def _log_times(events: list, entity_id: str, of_turn: str | None = None) -> tuple:
    """created_at and updated_at of the Task of one entity (a run or a turn, named by its id: the ids of
    the two kinds never coincide), from the log as it is retained: the time of the event that
    establishes the entity's creation, and the time of the latest state.changed of the run or the turn;
    each is null where the log holds no such event.

    Task created_at comes from retained creation metadata or an event that establishes creation of that
    entity. Position 1 and absence of an earlier replay.gap are retention checks, not proof of creation.
    In particular, state.changed with reason snapshot describes an entity that already existed when
    logging began and is not its creation event; reconcile likewise does not establish creation. If no
    retained fact establishes the origin, created_at is null. updated_at may still use a later retained
    state change.

    THE EVENT THAT ESTABLISHES CREATION is the entity's FIRST state.changed in the log (its
    ``entity_id`` names it) when its reason is ``commit``: the log saw the record as it was first made.
    That event counts only where the log is whole up to it: the log begins at position 1, every
    position up to the event is there, and no replay.gap stands before it, not even one that stands
    for a single position. An event of another type filed under the run or the turn (an acceptance,
    the turn's start, output) establishes nothing. No
    record of this version carries creation metadata of its own in the timestamp form, so the log is
    the only source here, and a Task whose origin the log does not establish says null.
    """
    mine = [event for event in events if of_turn is None or event.get("turn_id") == of_turn]
    changed = [event["time"] for event in mine if event["type"] == "state.changed"]
    created = None
    for position, event in enumerate(events, 1):
        if event.get("seq") != position or event["type"] == "replay.gap":
            break                       # a retained tail, a hole, or a gap: whatever follows proves no origin
        payload = event["payload"]
        if event["type"] == "state.changed" and payload.get("entity_id") == entity_id:
            if payload.get("reason") == "commit":
                created = event["time"]
            break                       # the first image decides: a snapshot or a reconcile is not a creation
    return created, (changed[-1] if changed else None)


def task_of_run(view: dict, events: list = (), pending_approval_ids: list = ()) -> dict:
    """What the Task of a run started without a session must say, from the run's view and its log.

    The Task has no store of its own: these fields are derived each time, so they survive a restart.
    Starting, running, a Stop pending and an unobservable unit are ``working`` (``input_required``
    while an approval is pending), and a Stop that was asked and is not confirmed sets
    ``cancel_requested`` (the view says ``stop_pending``, or carries a stop record, or both). ``working``
    for an unobservable unit means that no terminal outcome is known, not that progress was observed. ``stopped`` is ``cancelled``: only a confirmed Stop is. A run that ended with
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
    created_at, updated_at = _log_times(list(events), run_id)
    task = {"task_id": run_id, "source": "run", "session_id": None, "run_id": run_id, "turn_id": None,
            "outcome": None, "cancel_requested": False, "pending_approval_ids": [], "expires_at": None,
            "created_at": created_at, "updated_at": updated_at, "result": None}
    if state in ("starting", "running", "stop_pending", "unknown"):
        task["pending_approval_ids"] = list(pending_approval_ids)
        task["status"] = "input_required" if pending_approval_ids else "working"
        # Either sign of a Stop that was asked and is not confirmed: the state, or the stop record.
        task["cancel_requested"] = state == "stop_pending" or view.get("stop") is not None
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
    created_at, updated_at = _log_times(list(events), turn_id, of_turn=turn_id)
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
# The members of an effect that say WHOSE effect it is and what it is bound to. A change of content keeps them.
TARGET_MEMBERS = {"engine_tool": ("kind", "engine", "session_id", "run_id"), "plugin_effect": ("schema", "plugin_id", "run_id")}


class ApprovalModel:
    """One approval request, and everything that can happen to it, one happening at a time.

    ``revision`` counts changes to the request's CONTENT: its effect, its rendered lines, its deadline.
    It starts at 1. A showing does not change it and neither does an answer: the state of the request
    changes, its content does not. An opening carries the revision that was shown, so when the content
    changes, the revision rises and everyone must look again before an answer is taken. Content may
    change ONLY WHILE PENDING; once the request is resolved its effect, its lines and its deadline are
    immutable.

    ``call(principal, method, params, at)`` is approval.open or approval.answer; ``at`` is the server's
    current time. The order of checks for approval.answer: the caller is an owner or a device (else
    scope_refused); a request that lacks a field, or whose answer is neither 'approve' nor 'deny', is
    'request_invalid' naming the field, keeps no receipt and changes nothing; the same command id with
    the same params returns the first answer, and with other
    params is 'conflict' (command_id_reused): such a replay is HISTORICAL, it reports what was answered
    and never carries an effect out again; then, before a fresh answer is taken, the current time is
    compared with expires_at under the same serialisation as the request's state: at or after the
    deadline a pending request becomes 'expired' with no answer, answerer or answer time, and the
    fresh answer is 'conflict' (already_resolved); a request that is no longer pending is 'conflict'
    (already_resolved); a revision that is not the current one is 'conflict' (stale_revision); a caller
    with no opening on that surface at the current revision is 'prompt_not_shown'; a digest that is not
    the request's is 'conflict' (digest_mismatch). Only then is the answer taken.

    ``change(effect=..., expires_at=...)`` is the core changing the content of a pending request: the
    digest and the lines are made again from the effect and the revision rises by one. Refused
    (``resolved``) once the request is no longer pending. Build a proposed approval revision in a
    separate candidate. Validate the complete candidate's shape, canonical digest, rendered lines and
    envelope/effect identity before committing it. A failure is a typed refusal and leaves the prior
    record, revision and binding unchanged. Only a valid candidate replaces the pending record. Changing
    content is not permission to change the target through an inconsistent envelope. The refusals:
    ``nothing_to_change`` (neither an effect nor a deadline); ``effect`` (not an effect of the request's
    source, or one with a member more or less than such an effect has); ``target`` (the new effect is bound to another run, session or engine, or comes from
    another plugin, than the effect it replaces); ``timestamp`` (a deadline that is not a real instant);
    the renderer's own code for an effect that cannot be drawn whole (``too_many_lines``, ``too_large``
    and the like); and, for whatever else does not agree inside the candidate, the first word
    ``approval_problems`` gives for it. The numbers of an engine's call are normalised in the candidate
    before it is digested and drawn.

    ``consume(revision, digest, binding)`` is the core carrying the effect out. It consumes exactly the
    approved ``(approval_id, revision, digest)`` and the private engine binding the daemon keeps for
    it, ONCE: 'approved' becomes 'consumed'. Anything else is refused and changes nothing:
    ``not_approved`` (pending, denied, expired, vetoed), ``already_consumed``, ``approval_mismatch``
    (another revision or digest), ``binding_mismatch`` (not the binding kept for this approval, or a
    binding that does not name this approval's own target), ``record_invalid`` (the record does not
    agree with itself: see below). A cached successful answer never authorises another revision or
    another dispatch.

    THE BINDING. For an engine-tool approval, the private binding's approval_id, run_id and engine must
    equal the approved record's approval_id, the record/effect run_id, and the effect's engine. These
    equalities are checked after recovery as well as on first consumption. A mismatch returns
    binding_mismatch and changes neither approval state nor consumption state. Equality with an
    internally retained binding object alone is insufficient. Consumption still requires the exact
    approved revision and digest and occurs at most once. A PLUGIN'S EFFECT has no pending request of
    an engine, so the daemon keeps no binding for it and none is offered: it is consumed once against
    its approval, revision and digest, and a binding kept or offered for it is ``binding_mismatch``.

    WHAT IS CARRIED OUT IS A RECORD THAT AGREES WITH ITSELF. A model is made from a record (a new
    request, or one read back from where it is durable), and the numbers of an engine's call are
    normalised as it is made, so 1.0 written by another serialiser is the whole number again and minus
    zero is zero before anything is digested, drawn or carried out. Consumption checks the whole record
    once more (``approval_problems``) and refuses one that holds a number which is not normalised, or
    whose digest, lines or envelope are not its effect's (``record_invalid``): successful parsing of a
    call's text is not the only boundary.

    ``binding`` and ``receipts`` are durable beside the record: a model made again from the three (a
    restart) answers and consumes exactly as the first would have.
    """

    def __init__(self, record: dict, binding: dict | None = None, receipts: dict | None = None) -> None:
        self.record = copy.deepcopy(record)
        if self.record.get("source") == "engine_tool" and isinstance(self.record.get("effect"), dict):
            self.record["effect"] = normalised(self.record["effect"])       # creating or restoring: the bound form
        self.binding = copy.deepcopy(binding)
        self.receipts: dict = dict(receipts or {})

    _NEEDS = {"approval.open": ("command_id", "approval_id", "surface"),
              "approval.answer": ("command_id", "approval_id", "surface", "expected_revision", "digest", "answer")}

    def call(self, principal: dict, method: str, params: dict, at: str) -> dict:
        if method not in self._NEEDS:
            raise Refused("method")
        milliseconds(at)                    # the server's own clock; a time that is not a moment is refused whole
        if (not isinstance(principal, dict) or principal.get("kind") not in ("owner", "device")
                or not isinstance(principal.get("id"), str) or not principal["id"]):
            return {"code": "scope_refused", "detail": "approve"}
        # A request that is not whole is refused before any receipt is looked up or kept, and changes nothing. An
        # answer is 'approve' or 'deny' and nothing else: no other word is ever read as either.
        for field in self._NEEDS[method]:
            value = params.get(field) if isinstance(params, dict) else None
            whole = (type(value) is int and value >= 1) if field == "expected_revision" else (isinstance(value, str) and bool(value))
            if not whole or (field == "answer" and value not in ("approve", "deny")):
                return {"code": "request_invalid", "detail": field}
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
        if record["state"] == "pending" and milliseconds(at) >= milliseconds(record["expires_at"]):
            record.update(state="expired")          # nobody answered in time: answer, answerer and time stay null
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

    def change(self, effect: dict | None = None, expires_at: str | None = None) -> None:
        record = self.record
        if record["state"] != "pending":
            raise Refused("resolved")
        if effect is None and expires_at is None:
            raise Refused("nothing_to_change")
        candidate = copy.deepcopy(record)           # nothing below touches the record until the candidate is whole
        source = record["source"]
        if effect is not None:
            if not isinstance(effect, dict):
                raise Refused("effect")
            new = normalised(copy.deepcopy(effect)) if source == "engine_tool" else copy.deepcopy(effect)
            # The target is not content: what the core itself put in the effect stays what it was.
            if any(new.get(member) != record["effect"].get(member) for member in TARGET_MEMBERS.get(source, ())):
                raise Refused("target")
            try:
                lines = approval_lines(source, new)             # the renderer's own refusal, by its code
                candidate.update(effect=new, digest=digest_of(source, new), rendered=lines)
            except (KeyError, TypeError):
                raise Refused("effect") from None
        if expires_at is not None:
            milliseconds(expires_at)                            # 'timestamp' for a deadline that is not a moment
            candidate["expires_at"] = expires_at
        candidate["revision"] = record["revision"] + 1
        problems = approval_problems(candidate)
        if problems:
            raise Refused(problems[0])
        self.record = candidate

    def consume(self, revision: int, digest: str, binding: dict | None) -> None:
        record = self.record
        if record["state"] == "consumed":
            raise Refused("already_consumed")
        if record["state"] != "approved":
            raise Refused("not_approved")
        if approval_problems(record):
            raise Refused("record_invalid")
        if (revision, digest) != (record["revision"], record["digest"]):
            raise Refused("approval_mismatch")
        if record["source"] == "engine_tool":
            kept, effect = self.binding, record["effect"]
            # The binding kept, the binding offered, and each identity member against the approval itself. (The
            # record's own run is its effect's: a record where they differ was refused just above.)
            if (not isinstance(kept, dict) or binding != kept
                    or binding.get("approval_id") != record["approval_id"]
                    or binding.get("run_id") != effect["run_id"]
                    or binding.get("engine") != effect["engine"]):
                raise Refused("binding_mismatch")
        elif self.binding is not None or binding is not None:
            raise Refused("binding_mismatch")       # a plugin's effect has no engine request to bind
        record["state"] = "consumed"


# -------------------------------------------------------------------------- the identity of a start

INTENT_VERSION = 1


def start_intent_text(request: dict, declared) -> str:
    """The exact preimage of a start intent, version 1: the text, as RFC 8785 writes it, of ONE object:
    ``engine`` as the request supplied it, or null when it names none; ``prompt_sha256``, the SHA-256 of
    the prompt's UTF-8 bytes; and each extension that the start path's registry DECLARED and that is
    present and not null in the validated request. An extension nobody declared, one that is absent and
    one that is null are all left out. It is taken from what the client sent, before any registrant
    normalises or routes the request, and never from a registrant's output."""
    intent = {"engine": request.get("engine"),
              "prompt_sha256": hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest()}
    for field in declared:
        if request.get(field) is not None:
            intent[field] = request[field]
    check_value(intent)
    return text_of(intent)


def start_key_of(request: dict, declared) -> dict | None:
    """The ``start_key`` a start stores, or None for a request with no ``client_key`` (it stores no identity).

    ``declared`` is None when no lane has registered on the start path: the identity is then the legacy
    three members and nothing else, exactly as today. Otherwise it is the collection of declared
    extension fields (the sessions lane's ``session_id`` and ``turn_id`` are declared, internal ones),
    and the key also carries ``intent_version`` and ``intent_sha256``, the SHA-256 of the UTF-8 bytes of
    ``start_intent_text``.
    """
    key = request.get("client_key")
    if not isinstance(key, str):
        return None
    start_key = {"client_key": key, "engine": request.get("engine"),
                 "prompt_sha256": hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest()}
    if declared is not None:
        start_key["intent_version"] = INTENT_VERSION
        start_key["intent_sha256"] = hashlib.sha256(start_intent_text(request, declared).encode("utf-8")).hexdigest()
    return start_key


def start_retry(stored: dict, request: dict, declared) -> str:
    """How a keyed request compares with the ``start_key`` already stored under its tenant and key:
    ``"retry"`` or ``"client_key_reused"``. The lookup comes before any registrant runs.

    A stored key with an intent: an equal ``intent_sha256`` is a retry, anything else is refused. A
    stored LEGACY key (no intent): a request with no declared extension present is compared on the three
    legacy members, as today, and one with a declared extension present is refused, because what the
    first request launched cannot be established. A stored key with an intent, read where the start
    path is off (``declared`` is None), never equals a legacy key: refused, and nothing is launched.
    """
    mine = start_key_of(request, declared)
    if "intent_version" in stored:
        return "retry" if mine.get("intent_sha256") == stored["intent_sha256"] else "client_key_reused"
    legacy = {key: mine[key] for key in ("client_key", "engine", "prompt_sha256")}
    present = declared is not None and any(request.get(field) is not None for field in declared)
    return "retry" if legacy == stored and not present else "client_key_reused"


# ------------------------------------------------------------------------------- a page of a log

def cursor_answer(cursor: dict, log: dict | None, epoch: int) -> dict:
    """What a read is answered before any event is looked at. ``log`` is None for a log that does not
    exist, else ``{"high_water": <the last position of the log, 0 when it is empty>}``; ``epoch`` is the
    daemon's.

    A first read uses epoch 1 and seq 0: epoch 1 is the bootstrap token, not a claim that the client saw
    the log before. An unknown log is ``session_unknown``. A cursor from a FUTURE epoch is
    ``request_invalid`` (cursor_epoch_ahead). A seq beyond the high-water mark is ``request_invalid``
    (cursor_ahead); a seq AT the mark is a valid empty read, or a follow that waits. An older epoch
    reads on along the same sequence and the answer carries the current epoch with ``epoch_advanced``
    true; an equal epoch gives false. ``epoch_advanced`` is true exactly when a successful continuation
    replaces an older requested epoch with the current one; it is not an instruction to start again
    from zero.
    """
    if log is None:
        return {"code": "session_unknown", "session_id": cursor["session_id"]}
    if cursor["epoch"] > epoch:
        return {"code": "request_invalid", "detail": "cursor_epoch_ahead"}
    if cursor["seq"] > log["high_water"]:
        return {"code": "request_invalid", "detail": "cursor_ahead"}
    return {"code": "ok", "epoch_advanced": cursor["epoch"] < epoch}


def gap_problem(event: dict) -> bool:
    """True when a replay.gap event is not a span: ``from`` is at most ``to``, and the event's own seq is ``to``."""
    payload = event["payload"]
    return not (1 <= payload["from"] <= payload["to"] == event["seq"])


PROJECTION_DEFAULT = "raw"      # what a read that names no projection asks for: the events as they were logged


def page_problems(params: dict, page: dict) -> list:
    """Where an answered page is not the answer to the request it was asked with; empty when it is.

    ``params`` is the request's (its ``cursor``, and its ``projection`` when it names one); ``page`` is
    the 'ok' answer of events.read or events.follow.

    A successful events.read or events.follow response uses the projection explicitly requested by the
    caller. Validate that equality before checking the returned page. When projection is omitted, the
    method must document one default and test it. The default is ``raw`` (``PROJECTION_DEFAULT``). A
    page in another projection than the one asked for is ``projection``, and nothing else is looked at:
    it is not the answer to this request.

    The cursor's session_id names the one log asked for. Every event or line returned belongs to it
    (``log``). Only positions strictly after ``cursor.seq`` are returned, in order, and none is skipped:
    each position is an event or a line of its own, or lies inside a replay.gap that starts at the next
    missing position and whose own seq is the last position it stands for (``order``; ``gap`` for a
    span that runs backwards or carries the wrong seq). A projection does not silently filter positions.
    The answer's cursor is the last position represented, or the request's own position for an empty
    page, and its log never changes (``cursor``). Its epoch is the request's or a later one, and
    ``epoch_advanced`` says exactly whether it is later (``epoch``).

    A transcript line carries neither its log nor the first position of a gap, so a page of lines can
    only be checked for order here. ``transcript_page_problems`` is the producer's side of it.
    """
    cursor = params["cursor"]
    if page["projection"] != params.get("projection", PROJECTION_DEFAULT):
        return ["projection"]
    problems = []
    raw = page["projection"] == "raw"
    items = page["events"] if raw else page["lines"]
    position = cursor["seq"]
    for item in items:
        gap = (item["type"] if raw else item["event_type"]) == "replay.gap"
        if raw and item["session_id"] != cursor["session_id"] and "log" not in problems:
            problems.append("log")
        if raw and gap:
            if gap_problem(item) and "gap" not in problems:
                problems.append("gap")
            ok = item["payload"]["from"] == position + 1 and item["seq"] >= position + 1
        elif gap:
            ok = item["seq"] >= position + 1        # a line carries the span's last position, not its first
        else:
            ok = item["seq"] == position + 1
        if not ok and "order" not in problems:
            problems.append("order")
        position = max(position, item["seq"]) if isinstance(item["seq"], int) else position
    answered = page["cursor"]
    if answered["session_id"] != cursor["session_id"] or answered["seq"] != position:
        problems.append("cursor")
    if answered["epoch"] < cursor["epoch"] or page["epoch_advanced"] != (answered["epoch"] > cursor["epoch"]):
        problems.append("epoch")
    return problems


def transcript_page_problems(params: dict, raw_page: dict, page: dict) -> list:
    """Where a transcript page is not the projection of the raw page it was made from; empty when it is.

    A transcript page is produced only from a raw page whose log identity, sequence continuity and gap
    bounds have already been validated. A transcript line's missing log and gap-start fields do not
    waive those producer checks.

    ``params`` is the request's, and it asks for the transcript; ``raw_page`` is the raw page of the
    same read (the same cursor, asked raw); ``page`` is the transcript page given for it. ``source``:
    the raw page is not itself a right answer to that cursor (``page_problems`` finds something in it:
    an event of another log, a position skipped, a gap that does not start at the next missing position
    or carries the wrong seq), so no transcript page may be made from it. ``lines``: there is not one
    line for each event of the raw page, in order, with the event's seq and type. ``envelope``: the
    transcript page's cursor, ``more`` or ``epoch_advanced`` is not the raw page's. Then whatever
    ``page_problems`` finds in the transcript page itself.
    """
    asked_raw = dict(params, projection="raw")
    if raw_page.get("projection") != "raw" or page_problems(asked_raw, raw_page):
        return ["source"]
    problems = []
    lines, events = page.get("lines"), raw_page["events"]
    if (not isinstance(lines, list) or len(lines) != len(events)
            or any(line.get("seq") != event["seq"] or line.get("event_type") != event["type"] for line, event in zip(lines, events))):
        problems.append("lines")
    if any(page.get(member) != raw_page.get(member) for member in ("cursor", "more", "epoch_advanced")):
        problems.append("envelope")
    return problems + [found for found in page_problems(params, page) if found not in problems]


# ------------------------------------------------------------------------ digests an event carries

def post_image_digest(post_image) -> str:
    """``post_image_sha256`` of a state.changed event: the SHA-256, lower-case hex, of the UTF-8 bytes of
    the post-image as RFC 8785 writes it (the form named ``rfc8785/v1``, ``text_of``). Two events of one
    entity with the same digest carry the same image; a consumer compares with the LAST image logged
    for that entity, never with every image it has ever seen."""
    check_value(post_image)
    return hashlib.sha256(text_of(post_image).encode("utf-8")).hexdigest()


def input_digest(tool_input) -> str:
    """``input_digest`` of a tool.call event: the same form, over the tool's input."""
    check_value(tool_input)
    return hashlib.sha256(text_of(tool_input).encode("utf-8")).hexdigest()
