"""The one accessor of an approval's effect: what is approved, its digest and its lines, from one value.

Lucas decided the form (A22, Nov 13, 2026, about 2:48 AM PST: "I agree with your recommendations, be sure
to mark the decisions for follow up research just in-case they need to be refactored in the future"): an
engine-tool effect is canonicalised with RFC 8785 (``rfc8785/v1``), engine effects only; a plugin effect
keeps ``csl-cj/v0``; the digest form is named in the record. Marked by him for follow-up research, so the
form is kept replaceable here, behind ``bind``: this module is the only code in the core that reads an
effect to digest or draw it, and it produces the bytes, the digest and the rendered lines together from
one private copy of the effect, so they cannot disagree.

The rules are the published ones (SPINE's #368: ``approval.v1``, ``conformance/rules.py`` and the vectors in
``schemas/control/v1/vectors``), written again here because the core imports nothing from
``conformance/``; the vectors judge both. In short:

* An engine's tool call is read strictly (``read_call``): a key named twice at any depth, NaN or an
  infinity, half a surrogate pair, nesting deeper than 64 and an integral value outside
  -(2**53-1)..2**53-1 (however it is spelled) are refused; strings are kept exactly as written. A number
  whose value is integral is bound as the whole number (``1.0`` is ``1``, ``-0.0`` is ``0``): the value
  that is digested and drawn is the value that is dispatched.
* ``rfc8785/v1`` bytes are the UTF-8 bytes of the effect as RFC 8785 writes it: no spaces, keys in the
  order of their UTF-16 code units, numbers as ECMAScript writes them, strings with the short escapes.
* ``csl-approval-lines/v0``: every variable member of the effect is on a line and nothing else is. A line
  of the core's own words is ``trusted``; a line carrying anything an engine or plugin supplied is
  ``untrusted`` and goes through one encoder, which writes as an escape every character a surface must
  not be handed raw. A line holds at most 4,000 characters of its text, the label included; the rest
  continues on lines that start with two spaces. Nothing is ever cut: an effect that needs more than 200
  lines, or a call larger than 262,144 bytes, is refused and no approval is made of it.

PROPOSED, not decided by Lucas (the brief's "what a prompt does when an input is too long to show"): such
an effect is refused with a typed code and never truncated, which is what the published rules say too.
"""

from __future__ import annotations

import copy
import decimal
import hashlib
import json
import re
from dataclasses import dataclass

from recursion_core.approvals.record import effect_problem
from recursion_core.plugin_security.canonical import canonical as _csl_cj_v0
from recursion_core.plugin_security.errors import Refused as _PluginRefused

__all__ = [
    "CALL_MAX", "DIGEST_FORMS", "LINES_FORM", "LINES_MAX", "LINE_MAX", "LINE_PIECE", "MAX_DEPTH", "MAX_INT",
    "Bound", "EffectRefused", "bind", "check_call", "lines_of", "normalised", "read_call", "record_problems",
    "text_of",
]

MAX_INT = 2**53 - 1
MAX_DEPTH = 64
# PROVISIONAL (not decided by Lucas: the size limit of an effect, and what a prompt does when an input is
# too long to show). Built as the brief proposes: past these limits the effect is refused with a typed
# code (too_large, too_many_lines) and never truncated.
CALL_MAX = 262144
LINE_PIECE = 4000
LINE_MAX = 4096
LINES_MAX = 200
LINES_FORM = "csl-approval-lines/v0"
DIGEST_FORMS = {"engine_tool": "rfc8785/v1", "plugin_effect": "csl-cj/v0"}
EFFECT_MEMBERS = {
    "engine_tool": frozenset({"kind", "engine", "session_id", "run_id", "tool", "cwd", "input"}),
    "plugin_effect": frozenset({"schema", "kind", "plugin_id", "run_id", "connection", "scope", "currency",
                                "amount_minor", "payload"}),
}
SCOPE_MEMBERS = frozenset({"account", "venue", "contract_family", "adapter", "environment"})


class EffectRefused(Exception):
    """A value the effect rules do not take. ``code`` is one plain word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- reading an engine's call ------------------------------------------------------------------------------

def _strict_number(token: str):
    try:
        exact = decimal.Decimal(token)
    except decimal.InvalidOperation:
        exact = None
    if exact is not None and not exact.is_finite():
        exact = None
    if exact is not None and exact == exact.to_integral_value():
        if exact.copy_abs() > MAX_INT:  # copy_abs: no rounding in the host's context, no Overflow
            raise EffectRefused("integer_out_of_range")
        return int(exact)
    value = float(token)
    if value != value or value in (float("inf"), float("-inf")):
        raise EffectRefused("integer_out_of_range" if exact is None else "number_out_of_range")
    if value.is_integer():
        if abs(value) > MAX_INT:
            raise EffectRefused("integer_out_of_range")
        return int(value)
    return value


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise EffectRefused("duplicate_key")
        out[key] = value
    return out


def _not_a_number(token):
    raise EffectRefused("not_a_number")


def read_json_strict(text: object):
    """JSON text read strictly, or refused with a typed code; the result is in its bound form."""
    if not isinstance(text, str):
        raise EffectRefused("not_json")
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_int=_strict_number,
                           parse_float=_strict_number, parse_constant=_not_a_number)
    except EffectRefused:
        raise
    except RecursionError:
        raise EffectRefused("too_deep") from None
    except ValueError:
        raise EffectRefused("not_json") from None
    _check_value(value, 0)
    return value


def _check_value(value, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise EffectRefused("too_deep")
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise EffectRefused("lone_surrogate")
    elif type(value) is int:
        if not -MAX_INT <= value <= MAX_INT:
            raise EffectRefused("integer_out_of_range")
    elif type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise EffectRefused("number_out_of_range")
        if value.is_integer() and abs(value) > MAX_INT:
            raise EffectRefused("integer_out_of_range")
    elif type(value) is list:
        for item in value:
            _check_value(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise EffectRefused("not_a_json_value")
            _check_value(key, depth + 1)
            _check_value(item, depth + 1)
    else:
        raise EffectRefused("not_a_json_value")


def _unnormalised(value) -> bool:
    if type(value) is float:
        return value.is_integer()
    if type(value) is dict:
        return any(_unnormalised(item) for item in value.values())
    if type(value) in (list, tuple):
        return any(_unnormalised(item) for item in value)
    return False


def normalised(value):
    """The bound form of a held value: a float whose value is integral and in range is the whole number."""
    if type(value) is float and value.is_integer() and abs(value) <= MAX_INT:
        return int(value)
    if type(value) is dict:
        return {key: normalised(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [normalised(item) for item in value]
    return value


def check_call(call) -> None:
    """Refuse a call ``{"cwd", "input", "tool"}`` outside the strict profile of ``rfc8785/v1``."""
    if type(call) is not dict or set(call) != {"cwd", "input", "tool"} or type(call["input"]) is not dict:
        raise EffectRefused("not_a_call")
    tool, cwd = call["tool"], call["cwd"]
    if type(tool) is not str or not 1 <= len(tool) <= 256:
        raise EffectRefused("tool")
    if cwd is not None and (type(cwd) is not str or not cwd):
        raise EffectRefused("cwd")
    _check_value(call, 0)
    if _unnormalised(call):
        raise EffectRefused("not_normalised")
    if len(text_of(call).encode("utf-8")) > CALL_MAX:
        raise EffectRefused("too_large")


def read_call(tool: object, input_text: object, cwd: object) -> dict:
    """What an engine supplied for one tool call, read strictly: ``{"cwd", "input", "tool"}``."""
    value = read_json_strict(input_text)
    if type(value) is not dict:
        raise EffectRefused("input_not_an_object")
    call = {"cwd": cwd, "input": value, "tool": tool}
    check_call(call)
    return call


# -- RFC 8785 text ------------------------------------------------------------------------------------------

def _number_text(number) -> str:
    """A number as ECMAScript's Number::toString writes it (RFC 8785, 3.2.2.3)."""
    if type(number) is int:
        return str(number)
    if number == 0:
        return "0"  # minus zero too
    sign = "-" if number < 0 else ""
    mantissa, _, exponent = repr(abs(number)).partition("e")  # repr: the shortest text that reads back exactly
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    point = len(whole) + int(exponent or 0)  # where the decimal point sits after the first digit of `digits`
    kept = digits.lstrip("0")
    point -= len(digits) - len(kept)
    digits = kept.rstrip("0") or "0"
    n = len(digits)
    if n <= point <= 21:
        return sign + digits + "0" * (point - n)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    power = point - 1
    return sign + digits[0] + ("." + digits[1:] if n > 1 else "") + "e" + ("+" if power >= 0 else "-") + str(abs(power))


_SHORT_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}
# Code points a surface is never handed raw (first, last), listed explicitly so they do not move with a
# Unicode version: controls, the soft hyphen, marks and overrides that reorder or hide text, line and
# paragraph separators, the byte order mark, the interlinear and replacement specials, private use, tags
# and variation selectors, and everything from plane 14 up. The same list as the published rules.
_NOT_SHOWN_RAW = ((0x00, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160),
                  (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x2028, 0x202F), (0x205F, 0x206F),
                  (0x3164, 0x3164), (0xE000, 0xF8FF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0),
                  (0xFFF0, 0xFFFF), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0x10FFFF))


def _shown_raw(point: int) -> bool:
    return not any(first <= point <= last for first, last in _NOT_SHOWN_RAW)


def _u_escape(point: int) -> str:
    if point >= 0x10000:
        point -= 0x10000
        return "\\u%04x\\u%04x" % (0xD800 + (point >> 10), 0xDC00 + (point & 0x3FF))
    return "\\u%04x" % point


def _string_text(text: str, display: bool) -> str:
    out = ['"']
    for ch in text:
        point = ord(ch)
        if point in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[point])
        elif point < 0x20:
            out.append("\\u%04x" % point)
        elif display and not _shown_raw(point):
            out.append(_u_escape(point))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_order(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def text_of(value, display: bool = False) -> str:
    """A value as RFC 8785 writes it; with ``display``, the characters not shown raw written as escapes."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) in (int, float):
        return _number_text(value)
    if type(value) is str:
        return _string_text(value, display)
    if type(value) in (list, tuple):
        return "[" + ",".join(text_of(item, display) for item in value) + "]"
    if type(value) is dict:
        return "{" + ",".join(_string_text(key, display) + ":" + text_of(value[key], display)
                              for key in sorted(value, key=_utf16_order)) + "}"
    raise EffectRefused("not_a_json_value")


# -- the bytes, the digest, the lines -----------------------------------------------------------------------

def _effect_bytes(digest_form: str, effect) -> bytes:
    if digest_form == "rfc8785/v1":
        _check_value(effect, 0)
        if _unnormalised(effect):
            raise EffectRefused("not_normalised")
        return text_of(effect).encode("utf-8")
    if digest_form == "csl-cj/v0":
        try:
            return _csl_cj_v0(effect)
        except _PluginRefused:
            raise EffectRefused("noncanonical_value") from None
    raise EffectRefused("digest_form")


def _wrap(text: str, trust: str) -> list:
    pieces = [text[start:start + LINE_PIECE] for start in range(0, len(text), LINE_PIECE)] or [""]
    return [{"text": pieces[0], "trust": trust}] + [{"text": "  " + piece, "trust": trust} for piece in pieces[1:]]


def _field_lines(fields: dict) -> list:
    lines = []
    for key in sorted(fields, key=_utf16_order):
        lines += _wrap(text_of(key, display=True) + ": " + text_of(fields[key], display=True), "untrusted")
    return lines


# What may be drawn as the core's own (trusted) words from an engine's effect: a plain id, no space, no line
# break. bind holds the engine, session and run to their exact grammars first; this keeps the drawer safe on
# its own, whoever calls it.
_DRAWN_ID = re.compile(r"[A-Za-z0-9:_-]{1,64}")


def lines_of(source: str, effect: dict) -> list:
    """The rendered lines (``csl-approval-lines/v0``) of a bound effect, or a typed refusal."""
    def core(text):
        return [{"text": text, "trust": "trusted"}]

    def theirs(label, value):
        return _wrap(label + text_of(value, display=True), "untrusted")

    if source == "engine_tool":
        call = {"cwd": effect["cwd"], "input": effect["input"], "tool": effect["tool"]}
        check_call(call)
        session = effect["session_id"] if effect["session_id"] is not None else "none"
        if any(type(value) is not str or not _DRAWN_ID.fullmatch(value)
               for value in (effect["engine"], session, effect["run_id"])):
            raise EffectRefused("effect")
        lines = (core("Tool call waiting for an answer") + core("engine: " + effect["engine"])
                 + core("session: " + session) + core("run: " + effect["run_id"])
                 + theirs("tool: ", call["tool"]) + theirs("directory: ", call["cwd"])
                 + core("input fields: %d" % len(call["input"])) + _field_lines(call["input"]))
    elif source == "plugin_effect":
        scope = effect["scope"]
        amount = (text_of(effect["amount_minor"], display=True) + " minor units of "
                  + text_of(effect["currency"], display=True))
        lines = (core("Plugin effect waiting for an answer") + theirs("plugin: ", effect["plugin_id"])
                 + theirs("kind: ", effect["kind"]) + theirs("run: ", effect["run_id"])
                 + theirs("connection: ", effect["connection"]) + theirs("account: ", scope["account"])
                 + theirs("venue: ", scope["venue"]) + theirs("contract family: ", scope["contract_family"])
                 + theirs("adapter: ", scope["adapter"]) + theirs("environment: ", scope["environment"])
                 + _wrap("amount: " + amount, "untrusted") + core("payload fields: %d" % len(effect["payload"]))
                 + _field_lines(effect["payload"]))
    else:
        raise EffectRefused("source")
    if len(lines) > LINES_MAX:
        raise EffectRefused("too_many_lines")
    if any(len(line["text"]) > LINE_MAX for line in lines):
        raise EffectRefused("line_too_long")
    return lines


def _effect_shape_problem(source: str, effect):
    return effect_problem(source, effect)


def _members_ok(source: str, effect) -> bool:
    if type(effect) is not dict or set(effect) != EFFECT_MEMBERS.get(source):
        return False
    return source != "plugin_effect" or (type(effect["scope"]) is dict and set(effect["scope"]) == SCOPE_MEMBERS)


@dataclass(frozen=True)
class Bound:
    """One effect bound: its private copy, digest form, canonical bytes, digest and rendered lines.

    Produced together by ``bind`` from one copy, so a caller that changes its own dict afterwards changes
    none of them. ``effect`` and ``rendered`` are handed out as fresh copies.
    """

    source: str
    digest_form: str
    canonical: bytes
    digest: str
    _effect: dict
    _rendered: tuple

    @property
    def effect(self) -> dict:
        return copy.deepcopy(self._effect)

    @property
    def rendered(self) -> list:
        return [dict(line) for line in self._rendered]


def bind(source: object, effect: object) -> Bound:
    """Bind an effect: copy it once, normalise it (engine effects), check it, then take the bytes, the
    digest and the lines from that one copy. Refused with a typed code; nothing is half bound."""
    if source not in DIGEST_FORMS:
        raise EffectRefused("source")
    try:
        held = copy.deepcopy(effect)
    except Exception:  # noqa: BLE001 - an object that cannot be copied is not an effect
        raise EffectRefused("effect") from None
    if source == "engine_tool":
        try:
            _check_value(held, 0)
        except RecursionError:
            raise EffectRefused("too_deep") from None
        held = normalised(held)
    # The whole published shape of the source's effect, before anything is drawn: the engine, session and
    # run are drawn as the core's own (trusted) lines, so each must be in its id grammar (no line break,
    # nothing an engine could use to forge a line), and a plugin's effect must be the gate's own object.
    if _effect_shape_problem(source, held) is not None:
        raise EffectRefused("effect")
    digest_form = DIGEST_FORMS[source]
    raw = _effect_bytes(digest_form, held)
    if len(raw) > CALL_MAX:  # a plugin's effect is held to the same cap as an engine's call; never cut
        raise EffectRefused("too_large")
    lines = lines_of(source, held)
    return Bound(source, digest_form, raw, hashlib.sha256(raw).hexdigest(), held, tuple(lines))


def record_problems(record: dict) -> list:
    """What does not agree inside one approval record (an empty list when everything does).

    The published words: ``effect``, ``call``, ``digest_form``, ``digest``, ``rendered``, ``envelope``,
    ``opened_by``, in that order, each at most once.
    """
    problems = []
    source, effect = record.get("source"), record.get("effect")
    if not _members_ok(source, effect):
        problems.append("effect")
    if source == "engine_tool":
        try:
            check_call({"cwd": effect["cwd"], "input": effect["input"], "tool": effect["tool"]})
        except (EffectRefused, KeyError, TypeError):
            problems.append("call")
    if type(source) is not str or record.get("digest_form") != DIGEST_FORMS.get(source):
        problems.append("digest_form")
    try:
        if hashlib.sha256(_effect_bytes(record.get("digest_form"), effect)).hexdigest() != record.get("digest"):
            problems.append("digest")
    except EffectRefused:
        problems.append("digest")
    try:
        if lines_of(source, effect) != record.get("rendered"):
            problems.append("rendered")
    except (EffectRefused, KeyError, TypeError):
        problems.append("rendered")
    if type(effect) is dict and (record.get("run_id") != effect.get("run_id") or (
            source == "engine_tool" and record.get("session_id") != effect.get("session_id"))):
        problems.append("envelope")
    seen = []
    for opening in record.get("opened_by") or ():
        who = (opening.get("principal"), opening.get("surface"), opening.get("revision"))
        if who in seen or type(opening.get("revision")) is not int or opening["revision"] > record.get("revision", 0):
            problems.append("opened_by")
            break
        seen.append(who)
    return problems
