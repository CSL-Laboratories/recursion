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
* ``call_of`` and ``check_call``: the call of an engine tool (its tool, its input, its directory), profile
  ``csl-call/v1``.
* ``canonical_v0``: the restricted canonical JSON ``csl-cj/v0`` that plugin effects and today's
  signatures use.
* ``effect_bytes`` and ``digest_of``: the bytes an approval's digest is taken over, by ``digest_form``.
* ``approval_lines``: the lines every surface prints for an approval (``csl-approval-lines/v0``).
* ``approval_problems``: what does not agree inside one approval record.
* ``ApprovalModel``: approval.open and approval.answer, step by step, with replay.
"""
from __future__ import annotations

import copy
import hashlib
import json

MAX_INT = 2**53 - 1          # the largest whole number every binary64 reader keeps exact
MAX_DEPTH = 64               # of a tool's input
CJ_MAX_DEPTH = 32            # of csl-cj/v0, as plugin_security/canonical.py has it
CALL_MAX = 262144            # UTF-8 bytes of a call's text; a larger call is never drawn and never approved
LINE_PIECE = 4000            # characters of a value on one rendered line; the rest continues on the next
LINES_MAX = 200              # rendered lines of one approval; an effect that needs more is never drawn
CALL_FORM = "csl-call/v1"
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
    """Refuse a call that is not in profile ``csl-call/v1``.

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
        call = effect["call"]
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
            check_call(effect["call"])
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


# ------------------------------------------------------------------- approval.open and approval.answer

class ApprovalModel:
    """approval.open and approval.answer on one request, one call at a time.

    ``revision`` is the revision of the request and of what it shows. It starts at 1 and rises by one at
    every change of the request: an answer, the effect being carried out, a veto, the deadline passing.
    Recording that a surface showed the request is a note about the request, not a change of it, and
    does not raise the revision; the note carries the revision that was shown. So while a request is
    pending and what it shows cannot change, its revision stays 1; if a later version ever lets the
    shown content change, that raises the revision and everyone must look again.

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
                      answer=params["answer"], answered_at=at, revision=record["revision"] + 1)
        return {"code": "ok", "approval": copy.deepcopy(record)}
