"""Hermes (upstream Hermes Agent, NousResearch/hermes-agent): the one-shot stream parser, its argv,
and the long-lived gateway owner.

WHAT THIS IS. The parser half of the Hermes one-shot driver: it reads the JSONL stream that
``hermes chat --query-file - --format stream-json`` writes on stdout and turns each line into driver.v2
event drafts (``schemas/control/v1/driver.v2.json``, ``$defs/event_draft``). The parser starts nothing,
writes nothing to an engine, registers nothing and reads no file (one ``HermesStreamParser`` per run's
stream, fed by one reader thread). Registration in the engine table is a later slice.

THE LONG-LIVED OWNER. Chat and ACP are viewers of a profile gateway that keeps the turn after they
exit (a chat viewer can exit 3 while that turn is still running). ``HermesGatewayDrive`` launches
``hermes gateway run --no-supervise --external-supervisor`` on a dedicated profile as the foreground
process of the run's unit, which here is that process's own session. A viewer exit is not Stop.
``route_stop`` answers stopped only after that gateway's work has ended, and pending otherwise.

THE PROTOCOL, as the pinned upstream source writes it (``hermes_cli/stream_json.py``,
``StreamJsonEmitter``, at NousResearch/hermes-agent 2f7e62bad99052055d1db9213b81eaab26174bc4). Every
stdout line is one JSON object with a ``type`` and a ``timestamp`` (Unix milliseconds):

    {"type": "system", "subtype": "init", "model": str, "session_id": str}         first, always
    {"type": "text", "text": str}                                                   an answer delta
    {"type": "tool_use", "name": str, "tool_call_id"?: str, "input"?: object}       a tool started
    {"type": "tool_result", "name": str, "tool_call_id"?: str, "output": str,
     "duration_ms": int, "is_error": bool}                                          a tool finished
    {"type": "result", "session_id": str, "exit_code": int, "text": str,
     "tokens": {"input", "output", "total", "cache_read", "cache_write"},
     "duration_ms": int, "error"?: str, "replay_gap"?: true}                        last, once

``init`` is written before credentials are resolved, so a failed start still gives ``init`` then
``result``. The emitter writes a missing token count as 0 (``data.get(...) or 0``), so a 0 here is NOT
known to be zero. A tool's output is cut at 5000 characters and "..." appended. Diagnostics and the
``session_id:`` line go to stderr.

THE MAPPING (what each line becomes):

    system/init   no draft; the model and the session id are kept
    text          engine.output (stdout): the delta's text, with the line's byte span
    tool_use      tool.call, only when the record carries an ``input`` object that has RFC 8785 text:
                  the engine's call id (or one of ours, below), the name, the SHA-256 of that text,
                  and the input itself when it is small and shallow enough to carry (else null, the
                  digest still the input's). A call whose input is absent, not an object, or has no
                  RFC 8785 text has no truthful digest: its line stays raw (an id-less one still
                  counts as an open call of its name, below)
    tool_result   tool.result: status "error" when is_error, else "ok"; the output as text;
                  truncated when the output is longer than the 5000 characters the emitter keeps
    result        a usage draft (when any count is known), then a result draft (once)
    anything else engine.output (stdout) of the raw line, untrusted, so nothing read is lost: a line
                  that is not one JSON object, holds NaN, Infinity, a number too large for a double
                  (a non-finite float, or a whole number outside ±(2**53 - 1)), a key twice, or a lone
                  UTF-16 surrogate anywhere, has an unknown type, does not have its type's shape, or
                  comes after the result. A line that is empty or only spaces or tabs gives no draft;
                  a vertical tab or a form feed is kept

NO LIMIT IS TYPED. The stream carries no typed limit field: the result record has an exit code and an
error string only. On this path the exit code is 0 (completed), 1 (failed, unfinished, or the start
failed), 2 (an invocation the protocol refuses), 3 (the run detached because a human must answer) or
130 (interrupted). Hermes exits 75 only for a Kanban worker (``hermes_cli/turn_exit.py``,
``turn_exit_code(..., kanban_worker=...)``; the stream-json view passes ``kanban_worker=False``), and
even there 75 also stands for an overloaded provider, a server error or a timeout. So no line ever
becomes a ``limit`` draft, every non-zero exit is ``engine_error`` with the exit code in the detail,
and the declaration's ``typed_limits`` is false. Reading a limit out of the error text would be a
short fault at best, and that is the breaker's call, not the parser's.

CALL IDS. driver.v2 pairs a tool.result with its tool.call by ``call_id`` (1 to 128 characters, no line
break). Hermes names a call when the agent gave it an id. A valid engine id is used as it is. An engine
id that is not a valid call id is replaced by ``e9h-`` and 32 hex digits of its SHA-256, so its result
still pairs. A call with no id gets ``e9n-<n>`` from this parser's own counter. A result with no id
pairs with the open id-less call of its name only when that call is the ONLY one open under that name
(a call that carries an engine id counts as open under that name too) and no result of that name has
arrived while several were open. Otherwise the result gets an id of its own and pairs with nothing,
because a guess would pair the wrong output with a call: once a result arrives while several calls of
a name are open, which call it ended is unknown, so every id-less result of that name pairs with
nothing until all the calls open then, and any started meanwhile, have ended. Then pairing starts
afresh. An id-less call whose line stayed raw is counted as open too, so that its result is never
paired with another call. A line that is not a sound record is counted the same way when it is still
one JSON object of type ``tool_use``, with a name of 1 to 256 characters and no line break, and with
no ``tool_call_id`` (the key absent, or null): it is open under that name with no id, and the name is
uncertain at once. A ``tool_use`` that carries an id is open under that name too, even when its line
stayed raw. The line itself stays raw and mints no id. A line that is not JSON is not counted, and
nothing after the result record is counted.

OFFSETS. ``parse`` takes each line exactly as it was read, newline included; ``offset_start`` and
``offset_end`` are byte offsets (UTF-8) into that stream, the end exclusive. A byte that is not
UTF-8, kept by a reader as a surrogate in U+DC80..U+DCFF, counts as that one byte; any other lone
surrogate counts as the three bytes of its UTF-8 form. A draft's text replaces a lone surrogate with
U+FFFD, so the draft can be stored as UTF-8; the offsets stay those of the stream. stdout and stderr
are counted separately.

THE ARGV. ``one_shot_argv`` gives what a registration with ``default_executable='hermes'`` promises:
argv[0] is the binary the route entry names when composition gave one, else ``hermes``, and nothing
else. The prompt goes on standard input (``--query-file -``), never on the command line, where every
user of the host can read it and its length is bounded; the core writes it and closes stdin, as on
today's one-turn path. ``gateway_argv`` is the long-lived owner: the same binary, then
``gateway run --no-supervise --external-supervisor --profile <name>``. The profile is dedicated to the
run. The shared names ``default`` and ``root`` are refused, and so is a profile directory reached
through a symlink (a new-looking path can alias another gateway).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import threading
import time

from recursion_core.routes.contract import ENGINE_ERROR, ENGINE_EXITED

# RFC 8785 text is SPINE's one implementation (control/methods, ``canonical_text``), the same the
# receipts and the approval bindings use. It is imported when a tool input is first digested, not here:
# ``recursion_core.control`` loads the control API and the registry, which this module must not pull into
# whatever imports the engines.


def _canonical():
    from recursion_core.control.methods import NoCanonicalText, canonical_text
    return canonical_text, NoCanonicalText

ENGINE = 'hermes'
DEFAULT_EXECUTABLE = 'hermes'     # the one command name the engine-table registration will state
ONE_SHOT_ARGS = ('chat', '--query-file', '-', '--format', 'stream-json')
# Foreground owner of the profile gateway. Chat and ACP attach to it; they are not the unit.
GATEWAY_ARGS = ('gateway', 'run', '--no-supervise', '--external-supervisor')
UPSTREAM_PIN = 'NousResearch/hermes-agent@2f7e62bad99052055d1db9213b81eaab26174bc4'

# driver.v2's declaration of the one-shot driver. Usage is read from the result record's typed token
# counts; nothing else is offered: one prompt per process, no interrupt, resume or fork in this driver,
# no permission bridge, and no typed limit (above). input_ack: no input is ever sent into a turn.
DECLARATION = {
    'engine': ENGINE,
    'driver': 2,
    'flags': {'multi_turn': False, 'mid_turn_input': False, 'interrupt': False, 'resume': False,
              'fork': False, 'permission_bridge': False, 'typed_limits': False, 'usage': True},
    'input_ack': False,
}

TOOL_OUTPUT_CAP = 5000            # characters the pinned emitter keeps of a tool's output
INPUT_CARRY_LIMIT = 65536         # bytes of RFC 8785 text above which a tool's input is not carried
# The deepest level inside a tool's input at which it is still carried. The event store refuses a value
# sitting more than 64 levels deep in an event (lane EVENTS' events/db.py, MAX_DEPTH and _check_value, at
# #373), and the input sits 2 levels down (the event, its payload). The digest of a deeper input is still
# taken, up to RFC 8785's own depth.
INPUT_CARRY_DEPTH = 62
ENGINE_SESSION_ID_LIMIT = 256     # driver.v2 handle.engine_session_id
# The last whole number ``canonical_text`` accepts (control/methods MAX_INT). 2**53 itself is a finite
# float and still has no RFC 8785 text, so an int outside this range keeps the line raw.
_MAX_EXACT_INT = 2**53 - 1
_CALL_ID = re.compile(r'[^\r\n]{1,128}')
_NAME = re.compile(r'[^\r\n]{1,256}')
_MODEL = re.compile(r'[^\r\n]{1,256}')
_CONNECTION = re.compile(r'(subscription|api_key|passthrough):[^\r\n]+')
_PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_SHARED_PROFILES = frozenset({'default', 'root'})
_TURN_UNKNOWN = object()
_SURROGATE = re.compile('[%s-%s]' % (chr(0xD800), chr(0xDFFF)))   # a lone surrogate after JSON decoding
_TOKEN_KEYS = (('input_tokens', 'input'), ('output_tokens', 'output'),
               ('cache_read_tokens', 'cache_read'), ('cache_write_tokens', 'cache_write'))
_EXIT_MEANING = {
    1: 'the turn failed or did not finish, or the start failed',
    2: 'Hermes refused the invocation',
    3: 'the run detached: a human must answer before the turn can go on',
    75: 'EX_TEMPFAIL (Hermes uses it for a Kanban worker hitting a rate limit, billing wall, overload, '
        'server error or timeout; not typed as a plan limit here)',
    130: 'the turn was interrupted',
}
_UNKNOWN = {'value': None, 'quality': 'unknown', 'provenance': 'unavailable'}


class HermesParseError(ValueError):
    """A caller error: a line that is not text, or a parser built with a bad connection id."""


class HermesOwnershipRefused(Exception):
    """The gateway was not taken as this run's. A Stop after this is pending, never stopped."""

    def __init__(self, cause: str) -> None:
        self.cause = cause
        super().__init__(cause)


def _executable(binary: str | None) -> str:
    if binary is None:
        return DEFAULT_EXECUTABLE
    if isinstance(binary, str) and binary.startswith('/') and '\0' not in binary and '\n' not in binary:
        return binary
    raise HermesParseError('binary: an absolute path, or None for the default executable')


def one_shot_argv(binary: str | None = None) -> list[str]:
    """The one-shot command. ``binary`` is the absolute path a route entry names, when it names one."""
    return [_executable(binary), *ONE_SHOT_ARGS]


def gateway_argv(binary: str | None = None, *, profile: str) -> list[str]:
    """The foreground gateway the run's unit must hold. ``profile`` is dedicated to this run."""
    if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None or profile in _SHARED_PROFILES:
        raise HermesOwnershipRefused('profile')
    return [_executable(binary), *GATEWAY_ARGS, '--profile', profile]


def _no_duplicate_keys(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f'key {key!r} twice')
        out[key] = value
    return out


def _load_object(text: str):
    """One JSON object, strictly: no NaN or Infinity, no number too large for a double, no key twice,
    and no lone surrogate in any string or key. None when the text is not that."""
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) and _scalar_values_are_sound(value) else None


def _scalar_values_are_sound(value) -> bool:
    """Every string and key a sequence of Unicode scalar values (``json`` decodes an escaped lone
    surrogate into one, which no UTF-8 store can hold), every whole number inside ±(2**53 - 1), and
    every float finite (``json`` decodes ``NaN``, ``Infinity`` and an overflowing ``1e999`` into
    floats that are not). Walked without recursion."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return False
        elif type(item) is int:
            if item > _MAX_EXACT_INT or item < -_MAX_EXACT_INT:
                return False
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            for key, child in item.items():
                if _SURROGATE.search(key):
                    return False
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _depth(value) -> int:
    """The deepest level any value inside ``value`` sits at, ``value`` itself at 0, as the event store
    counts it (each member one level below its object or array; an empty one adds nothing). Walked
    without recursion."""
    deepest, stack = 0, [(value, 0)]
    while stack:
        item, level = stack.pop()
        deepest = max(deepest, level)
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)
    return deepest


def _count(value) -> dict:
    """A token count from the result record. Only a positive whole number is known: the emitter writes
    0 for a count it was not given, so 0 says nothing."""
    if type(value) is int and value > 0:
        return {'value': value, 'quality': 'known', 'provenance': 'observed'}
    return dict(_UNKNOWN)


def input_digest(value) -> str:
    """SHA-256 (hex) of ``value`` as RFC 8785 writes it. ``NoCanonicalText`` when it has no such text."""
    canonical_text, _ = _canonical()
    return hashlib.sha256(canonical_text(value).encode('utf-8')).hexdigest()


class HermesStreamParser:
    """Turns one Hermes stream-json run into driver.v2 event drafts. One instance per run's stream.

    ``connection_id`` is the route's connection key (``<kind>:<id>``), written into usage drafts.
    Not thread-safe: one reader thread feeds it, in the order the lines were read.
    """

    def __init__(self, connection_id: str) -> None:
        if not isinstance(connection_id, str) or len(connection_id) > 256 or not _CONNECTION.fullmatch(connection_id):
            raise HermesParseError('connection_id')
        self.connection_id = connection_id
        self.model: str | None = None
        self.engine_session_id: str | None = None
        self.exit_code: int | None = None     # the result record's, once it has been read
        self.raw_lines = 0                    # lines given back as raw engine.output
        self.uncarried_inputs = 0             # typed calls whose input was too large or too deep to carry
        self._offsets = {'stdout': 0, 'stderr': 0}
        self._init_seen = False
        self._result_seen = False
        self._minted = 0
        # tool name -> the open calls with no engine id: our id, or None for a call whose line stayed raw
        self._open_unnamed: dict[str, list[str | None]] = {}
        # tool name -> how many calls that carry an engine id are still open
        self._open_named: dict[str, int] = {}
        # names whose id-less results must not be paired until the open calls of that name have ended
        self._ambiguous: set[str] = set()

    # -- the calls ----------------------------------------------------------------------------------
    def parse(self, line: str) -> list[dict]:
        """The drafts for one stdout line, read exactly as written (newline included)."""
        start, end = self._advance('stdout', line)
        text = line.rstrip('\r\n')
        if not text.strip(' \t'):
            return []
        record = _load_object(text)
        drafts = None if record is None or self._result_seen else self._typed(record, start, end)
        if drafts is None:
            self.raw_lines += 1
            if record is None and not self._result_seen:
                self._note_rejected_call(text)
            return [_output('stdout', line, start, end)]
        return drafts

    def parse_stderr(self, line: str) -> list[dict]:
        """stderr carries Hermes's diagnostics: given back as raw engine.output, untrusted."""
        start, end = self._advance('stderr', line)
        return [_output('stderr', line, start, end)] if line else []

    def finish(self, exit_status: int | None) -> list[dict]:
        """At the end of the stream. A run whose stream had no result record gets ``engine_exited``.

        ``exit_status`` is an ``int``, or ``None`` when the process gave none. Any other type is a
        caller error and changes nothing.
        """
        if exit_status is not None and type(exit_status) is not int:
            raise HermesParseError('exit_status')
        if self._result_seen:
            return []
        self._result_seen = True
        how = 'no exit status' if exit_status is None else f'exit status {exit_status}'
        return [{'type': 'result', 'payload': {
            'code': ENGINE_EXITED, 'detail': f'hermes: the stream ended with no result record ({how})'}}]

    @property
    def result_seen(self) -> bool:
        return self._result_seen

    def exit_agrees(self, exit_status: int) -> bool | None:
        """Whether the process's exit status is the one the result record reported; None before a record.

        ``exit_status`` must be an ``int``. A bool or a float is a caller error.
        """
        if type(exit_status) is not int:
            raise HermesParseError('exit_status')
        return None if self.exit_code is None else self.exit_code == exit_status

    # -- one typed record ---------------------------------------------------------------------------
    def _typed(self, record: dict, start: int, end: int):
        kind = record.get('type')
        if kind == 'system' and record.get('subtype') == 'init':
            return self._init(record)
        if kind == 'text':
            text = record.get('text')
            if not isinstance(text, str) or not text:
                return None
            return [_output('stdout', text, start, end)]
        if kind == 'tool_use':
            return self._tool_use(record)
        if kind == 'tool_result':
            return self._tool_result(record)
        if kind == 'result':
            return self._result(record)
        return None

    def _init(self, record: dict):
        model, session = record.get('model'), record.get('session_id')
        if self._init_seen or not isinstance(model, str) or not isinstance(session, str):
            return None   # a second init is not the protocol: kept as a raw line
        self._init_seen = True
        if _MODEL.fullmatch(model):
            self.model = model
        self._take_session(session)
        return []

    def _tool_use(self, record: dict):
        name = record.get('name')
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return None
        engine_id = record.get('tool_call_id')
        if engine_id is not None and not (isinstance(engine_id, str) and engine_id):
            return None
        text = None
        tool_input = record.get('input')
        if isinstance(tool_input, dict):
            try:
                canonical_text, _ = _canonical()
                written = canonical_text(tool_input)
            except Exception:
                written = None
            text = written if isinstance(written, str) else None
        if text is None:
            # No truthful digest: the input is absent, not an object, or has no RFC 8785 text, or the
            # canonical writer failed. The line stays raw. An id-less call is still open under its name,
            # and a call that carries an id is open too, so a later id-less result is not paired by guess.
            if engine_id is None:
                self._open_unnamed.setdefault(name, []).append(None)
            else:
                self._hold_named(name)
            return None
        data = text.encode('utf-8')
        carried = tool_input if len(data) <= INPUT_CARRY_LIMIT and _depth(tool_input) <= INPUT_CARRY_DEPTH else None
        if carried is None:
            self.uncarried_inputs += 1
        if engine_id is None:
            call_id = self._mint()
            self._open_unnamed.setdefault(name, []).append(call_id)
        else:
            call_id = _call_id(engine_id)
            self._hold_named(name)
        return [{'type': 'tool.call', 'payload': {
            'call_id': call_id, 'name': name, 'input_digest': hashlib.sha256(data).hexdigest(), 'input': carried}}]

    def _tool_result(self, record: dict):
        name, output, is_error = record.get('name'), record.get('output'), record.get('is_error')
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(output, str) \
                or type(is_error) is not bool:
            return None
        engine_id = record.get('tool_call_id')
        if engine_id is not None and not (isinstance(engine_id, str) and engine_id):
            return None
        if engine_id is not None:
            call_id = _call_id(engine_id)
            self._release_named(name)
        else:
            call_id = self._end_unnamed(name)
        return [{'type': 'tool.result', 'payload': {
            'call_id': call_id, 'status': 'error' if is_error else 'ok', 'text': output,
            'output_ref': None, 'truncated': len(output) > TOOL_OUTPUT_CAP}}]

    def _result(self, record: dict):
        exit_code, tokens, session = record.get('exit_code'), record.get('tokens'), record.get('session_id')
        text, error = record.get('text', ''), record.get('error')
        if type(exit_code) is not int or not isinstance(tokens, dict) or not isinstance(session, str) \
                or not isinstance(text, str) or (error is not None and not isinstance(error, str)):
            return None
        self._result_seen = True
        self.exit_code = exit_code
        self._take_session(session)
        drafts = []
        usage = self._usage(tokens)
        if usage is not None:
            drafts.append(usage)
        if exit_code == 0:
            payload = {'code': None, 'detail': text}
        else:
            meaning = _EXIT_MEANING.get(exit_code, 'an exit code this parser does not know')
            detail = f'hermes exit {exit_code}: {meaning}'
            if error:
                detail += f'; hermes said: {error}'
            payload = {'code': ENGINE_ERROR, 'detail': detail}
        drafts.append({'type': 'result', 'payload': payload})
        return drafts

    def _usage(self, tokens: dict):
        counts = {field: _count(tokens.get(key)) for field, key in _TOKEN_KEYS}
        if all(c['quality'] == 'unknown' for c in counts.values()):
            return None   # nothing is known: no usage event rather than one made only of unknowns
        # The record's counts are the agent's for the session it ran in; a one-shot run on a new session
        # has one turn, and a running total is never added up, so they are filed as a session total.
        return {'type': 'usage', 'payload': {
            'connection_id': self.connection_id, 'model': self.model, 'bucket': None, 'span': 'session_total',
            'input_tokens': counts['input_tokens'], 'output_tokens': counts['output_tokens'],
            'reasoning_tokens': dict(_UNKNOWN),
            'cache_read_tokens': counts['cache_read_tokens'], 'cache_write_tokens': counts['cache_write_tokens'],
            'cost_usd': dict(_UNKNOWN)}}

    # -- helpers ------------------------------------------------------------------------------------
    def _advance(self, stream: str, line: str) -> tuple[int, int]:
        if not isinstance(line, str):
            raise HermesParseError('line: text, as read')
        start = self._offsets[stream]
        end = start + _utf8_span(line)
        self._offsets[stream] = end
        return start, end

    def _take_session(self, session: str) -> None:
        if session and len(session) <= ENGINE_SESSION_ID_LIMIT and '\n' not in session and '\r' not in session:
            self.engine_session_id = session

    def _mint(self) -> str:
        self._minted += 1
        return f'e9n-{self._minted}'

    def _note_rejected_call(self, text: str) -> None:
        """A tool_use refused before it was a sound record: still open, so its result is not guessed."""
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return
        if not isinstance(value, dict) or value.get('type') != 'tool_use':
            return
        name = value.get('name')
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return
        engine_id = value.get('tool_call_id')
        if engine_id is None:
            self._open_unnamed.setdefault(name, []).append(None)
            self._ambiguous.add(name)
            return
        if isinstance(engine_id, str) and engine_id:
            self._hold_named(name)

    def _hold_named(self, name: str) -> None:
        """One open call of ``name`` that carries an engine id."""
        self._open_named[name] = self._open_named.get(name, 0) + 1

    def _release_named(self, name: str) -> None:
        """That call's result carried the id, so the call is no longer open."""
        count = self._open_named.get(name, 0)
        if count > 1:
            self._open_named[name] = count - 1
        else:
            self._open_named.pop(name, None)
        if not self._open_unnamed.get(name) and name not in self._open_named:
            self._ambiguous.discard(name)

    def _end_unnamed(self, name: str) -> str:
        """The call id for an id-less result of ``name``: its call's, only when that call is beyond doubt."""
        open_calls = self._open_unnamed.get(name, [])
        named_open = name in self._open_named
        if len(open_calls) == 1 and not named_open and name not in self._ambiguous:
            ended = open_calls.pop()
            call_id = ended if ended is not None else self._mint()   # a raw call has no tool.call to pair
        elif open_calls and (len(open_calls) > 1 or named_open or name in self._ambiguous):
            # Several open, or a group already in doubt: one of them ended, which one is unknown.
            self._ambiguous.add(name)
            open_calls.pop()
            call_id = self._mint()
        else:
            call_id = self._mint()
        if not open_calls:
            self._open_unnamed.pop(name, None)
            if not named_open:
                self._ambiguous.discard(name)
        return call_id


def _call_id(engine_id: str) -> str:
    if _CALL_ID.fullmatch(engine_id) and not engine_id.startswith(('e9n-', 'e9h-')):
        return engine_id
    return 'e9h-' + hashlib.sha256(engine_id.encode('utf-8', 'surrogatepass')).hexdigest()[:32]


def _utf8_span(line: str) -> int:
    """Bytes of ``line`` as the stream held them.

    A scalar encodes as UTF-8. A surrogate in U+DC80..U+DCFF is one byte that was not UTF-8. Any
    other lone surrogate counts as the three bytes of its UTF-8 form, so the length is always defined.
    """
    try:
        return len(line.encode('utf-8'))
    except UnicodeEncodeError:
        total = 0
        for ch in line:
            point = ord(ch)
            if 0xDC80 <= point <= 0xDCFF:
                total += 1
            else:
                total += len(ch.encode('utf-8', 'surrogatepass'))
        return total


def _storable(text: str) -> str:
    """``text`` with each lone surrogate replaced by U+FFFD, so a UTF-8 store can hold the draft."""
    if _SURROGATE.search(text) is None:
        return text
    return _SURROGATE.sub('\ufffd', text)


def _output(stream: str, text: str, start: int, end: int) -> dict:
    return {'type': 'engine.output', 'payload': {
        'stream': stream, 'text': _storable(text), 'offset_start': start, 'offset_end': end}}


def declaration() -> dict:
    """A copy of the driver.v2 declaration."""
    return json.loads(json.dumps(DECLARATION))


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    return flags


def _owned_directory(path: str, cause: str):
    """The lstat of a real directory we own. A symlink is ``cause``, never followed."""
    try:
        info = os.lstat(path)
    except OSError as err:
        raise HermesOwnershipRefused(cause) from err
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise HermesOwnershipRefused(cause)
    return info


def _proc_identity(pid: int):
    """``(state, pgrp, session, start)`` from ``/proc/<pid>/stat``, or None when it cannot be read.

    ``start`` is the kernel start-time field. A later read with a different start time is a
    reused pid, not the process that had this pid before.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        fd = os.open(f'/proc/{pid}/stat', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    cut = data.rfind(b')')
    if cut < 0:
        return None
    rest = data[cut + 2:].split()
    if len(rest) < 20:
        return None
    try:
        pgrp = int(rest[2])
        session = int(rest[3])
        start = int(rest[19])
    except ValueError:
        return None
    return rest[0], pgrp, session, start


def _pid_alive(pid: int):
    """True when ``pid`` is live, False when it is gone or a zombie, None when that is unknown.

    None means a signal-0 probe found the pid and ``/proc/<pid>/stat`` still could not be read.
    That is not death: a caller must not treat the process as ended.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # probe only: signal 0 does not terminate
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    ident = _proc_identity(pid)
    if ident is None:
        return None
    return ident[0] not in (b'Z', b'X')


def _group_populated(pgid: int) -> bool:
    """True when any live process is in the session ``pgid`` names, or ``/proc`` cannot be listed.

    ``pgid`` is the launch group's id. The child is a new session, so that id is the session id
    too. A member that has moved to another group of the same session still counts. A missing
    listing is unknown, so the caller must not answer stopped. A pid whose stat disappeared
    between the listing and the read is skipped.
    """
    try:
        names = os.listdir('/proc')
    except OSError:
        return True
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return True
    for name in names:
        if not name.isdigit():
            continue
        ident = _proc_identity(int(name))
        if ident is None:
            continue
        state, _pgrp, session, _start = ident
        if state in (b'Z', b'X'):
            continue
        if session == pgid:
            return True
    return False


class HermesGatewayDrive:
    """Launches and owns ``hermes gateway run`` inside the run's unit.

    The unit is the gateway process's session. Stop signals the launch group (SIGINT, then
    SIGKILL after ``timeout``, the same order as the #916 unit properties) and answers stopped
    only when that session has no live member and this profile's turn record does not name one.
    A member in another group of the same session keeps the answer pending: this drive does not
    signal that group. A chat viewer exiting 3 is not that answer. A second caller receives the
    same confirmation with ``repeat`` true.
    """

    def __init__(self, binary: str | None, *, profile: str, home: str, run_id: str, timeout: float = 2.0) -> None:
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise HermesOwnershipRefused('run_id')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout <= 0:
            raise HermesOwnershipRefused('timeout')
        if not isinstance(home, str) or not home.startswith('/') or any(ch in home for ch in '\0\n\r'):
            raise HermesOwnershipRefused('home')
        self.argv = gateway_argv(binary, profile=profile)
        self.profile = profile
        self.home = home
        self.run_id = run_id
        self.timeout = float(timeout)
        self.pgid: int | None = None
        self._proc: subprocess.Popen | None = None
        # One record per dedicated profile. Two profiles on one home do not share it.
        self._turn_path = os.path.join(home, 'profiles', profile, '.gateway-turn')
        self._launch_start: int | None = None
        self._identity: tuple | None = None
        self._launched = False
        self._confirmed = False
        self._lock = threading.Lock()

    @property
    def confirmed(self) -> bool:
        with self._lock:
            return self._confirmed

    def launch(self) -> None:
        """Start the gateway in its own session. Refuses a profile this run did not just create."""
        with self._lock:
            if self._launched or self._proc is not None:
                raise HermesOwnershipRefused('already_launched')
            self._prepare_profile()
            env = {'PATH': '/usr/bin:/bin', 'HOME': self.home, 'HERMES_HOME': self.home,
                   'RECURSION_E9_GATEWAY_TURN': self._turn_path}
            try:
                self._proc = subprocess.Popen(
                    self.argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, cwd=self.home, start_new_session=True)
            except OSError:
                self._proc = None
                raise
            self.pgid = self._proc.pid
            self._capture_launch_identity()
            # KeyboardInterrupt here leaves pgid set and _launched false. stop() signals
            # that group and clears it, so a later launch is not stuck behind the child.
            if not self._wait_until_owned():
                self._signal(signal.SIGKILL)
                self._reap()
                self.pgid = None
                self._proc = None
                self._launch_start = None
                raise HermesOwnershipRefused('gateway_exited')
            self._launched = True

    def viewer_exit(self) -> int:
        """Run the chat viewer. Its exit, including 3, does not end the owned gateway and is not Stop."""
        if not self._launched:
            raise HermesOwnershipRefused('not_launched')
        argv = [self.argv[0], *ONE_SHOT_ARGS]
        env = {'PATH': '/usr/bin:/bin', 'HOME': self.home, 'HERMES_HOME': self.home,
               'RECURSION_E9_GATEWAY_TURN': self._turn_path}
        done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=self.timeout + 5, check=False)
        return done.returncode

    def stop(self) -> dict:
        """End the owned gateway's work. Pending whenever that work is not shown to have ended."""
        with self._lock:
            try:
                return self._stop_locked()
            except KeyboardInterrupt:
                raise
            except Exception as err:
                name = type(err).__name__
                if not name.isidentifier() or len(name) > 128:
                    name = 'Exception'
                return self._pending('cleanup_error:' + name)

    def turn_alive(self) -> bool:
        """True when the turn record names a process that is still alive."""
        with self._lock:
            return self._turn_blocks()

    def member_alive(self) -> bool:
        """True when the gateway session still has a live member."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            return self.pgid is not None and _group_populated(self.pgid)

    def _prepare_profile(self) -> None:
        info = _owned_directory(self.home, 'home')
        if info.st_mode & 0o077:
            raise HermesOwnershipRefused('home_writable')
        parent = os.path.join(self.home, 'profiles')
        if not os.path.lexists(parent):
            try:
                os.mkdir(parent, 0o700)
            except FileExistsError:
                pass
        _owned_directory(parent, 'profile_alias')
        if stat.S_IMODE(_owned_directory(parent, 'profile_alias').st_mode) & 0o077:
            raise HermesOwnershipRefused('profile_alias')
        dest = os.path.join(parent, self.profile)
        if os.path.lexists(dest):
            try:
                st = os.lstat(dest)
            except OSError as err:
                raise HermesOwnershipRefused('profile_alias') from err
            if stat.S_ISLNK(st.st_mode):
                raise HermesOwnershipRefused('profile_alias')
            raise HermesOwnershipRefused('profile_not_dedicated')
        os.mkdir(dest, 0o700)
        fd = os.open(dest, _directory_flags())
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise HermesOwnershipRefused('profile_owner')
        self._identity = (st.st_dev, st.st_ino, st.st_uid)

    def _profile_still_ours(self) -> bool:
        if self._identity is None:
            return False
        dest = os.path.join(self.home, 'profiles', self.profile)
        try:
            info = os.lstat(dest)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
        try:
            fd = os.open(dest, _directory_flags())
        except OSError:
            return False
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        return (st.st_dev, st.st_ino, st.st_uid) == self._identity

    def _stop_locked(self) -> dict:
        if self._confirmed and not self._blocks():
            return self._stopped(True)
        if self.pgid is None:
            return self._pending('cgroup_unknown')
        if self._launched and not self._profile_still_ours():
            self._end_group()
            return self._pending('cgroup_changed')
        self._signal(signal.SIGINT)
        if not self._wait_dead(self.timeout):
            self._signal(signal.SIGKILL)
            if not self._wait_dead(self.timeout):
                return self._pending('member_alive' if self._blocks() else 'confirm_timeout')
        if self._blocks():
            return self._pending('member_alive')
        if not self._launched:
            # Popen returned and ownership did not. The child is gone; do not leave it blocking launch.
            self._reap()
            self.pgid = None
            self._proc = None
            self._launch_start = None
            return self._pending('cgroup_unknown')
        self._confirmed = True
        return self._stopped(False)

    def _wait_until_owned(self) -> bool:
        """True when the process we started is still the foreground leader of its session.

        A turn record that names that pid can confirm it early. The record is not required:
        a process that never writes one, and is still in the foreground at the deadline, is
        the owned unit.
        """
        deadline = time.monotonic() + self.timeout
        while True:
            if self._proc is None or self._proc.poll() is not None:
                return False
            record = self._read_turn()
            if isinstance(record, dict) and record.get('alive') is True and record.get('pid') == self.pgid \
                    and self._foreground_leader():
                return True
            if time.monotonic() >= deadline:
                return self._foreground_leader()
            time.sleep(0.02)

    def _wait_dead(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._blocks():
                return True
            time.sleep(0.02)
        return not self._blocks()

    def _blocks(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self.pgid is not None and _group_populated(self.pgid):
            return True
        return self._turn_blocks()

    def _turn_blocks(self) -> bool:
        record = self._read_turn()
        if record is None:
            return False
        if record is _TURN_UNKNOWN:
            return True
        if record.get('alive') is not True:
            return False
        pid = record.get('pid')
        if not isinstance(pid, int) or isinstance(pid, bool):
            return True
        alive = _pid_alive(pid)
        if alive is None:
            return True
        if not alive:
            return False
        # A live pid from another profile's session is not this drive's member.
        if self.pgid is None:
            return True
        ident = _proc_identity(pid)
        if ident is None:
            return True
        return ident[2] == self.pgid

    def _read_turn(self):
        path = self._turn_path
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        except OSError:
            return _TURN_UNKNOWN
        try:
            chunks = []
            while True:
                piece = os.read(fd, 65536)
                if not piece:
                    break
                chunks.append(piece)
                if sum(len(item) for item in chunks) > 65536:
                    return _TURN_UNKNOWN
        finally:
            os.close(fd)
        try:
            value = json.loads(b''.join(chunks).decode('utf-8'))
        except (UnicodeError, ValueError):
            return _TURN_UNKNOWN
        return value if isinstance(value, dict) else _TURN_UNKNOWN

    def _capture_launch_identity(self) -> None:
        """Remember the launch leader's start time, so a reused pgid is not signalled later."""
        self._launch_start = None
        if self.pgid is None:
            return
        deadline = time.monotonic() + 0.2
        while True:
            ident = _proc_identity(self.pgid)
            if ident is not None and ident[0] not in (b'Z', b'X'):
                self._launch_start = ident[3]
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)

    def _foreground_leader(self) -> bool:
        """True when the process we started is still the foreground leader of its session."""
        proc = self._proc
        if proc is None or self.pgid is None or proc.poll() is not None or proc.pid != self.pgid:
            return False
        ident = _proc_identity(self.pgid)
        if ident is None:
            return True
        state, pgrp, session, _start = ident
        if state in (b'Z', b'X'):
            return False
        return pgrp == self.pgid and session == self.pgid

    def _signal(self, sig: int) -> None:
        """Signal the launch group when its leader is still the process started at launch.

        Other groups in the session are not signalled. A pgid whose start time is missing
        or different is not this process, so it is not signalled either.
        """
        if self.pgid is None or self._launch_start is None:
            return
        ident = _proc_identity(self.pgid)
        if ident is None or ident[3] != self._launch_start:
            return
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            return

    def _end_group(self) -> None:
        self._signal(signal.SIGKILL)
        self._reap()

    def _reap(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return

    def _pending(self, reason: str) -> dict:
        return {'code': 'pending', 'run_id': self.run_id, 'state': 'pending', 'reason': reason}

    def _stopped(self, repeat: bool) -> dict:
        return {'code': 'stopped', 'run_id': self.run_id, 'state': 'stopped', 'repeat': repeat}


def route_stop(drive: HermesGatewayDrive) -> dict:
    """The Stop answer a Hermes route may give. It is never stopped while the gateway's work is alive."""
    if not isinstance(drive, HermesGatewayDrive):
        raise HermesOwnershipRefused('drive')
    return drive.stop()


__all__ = ['DECLARATION', 'DEFAULT_EXECUTABLE', 'ENGINE', 'GATEWAY_ARGS', 'HermesGatewayDrive',
           'HermesOwnershipRefused', 'HermesParseError', 'HermesStreamParser', 'ONE_SHOT_ARGS',
           'TOOL_OUTPUT_CAP', 'UPSTREAM_PIN', 'declaration', 'gateway_argv', 'input_digest',
           'one_shot_argv', 'route_stop']
