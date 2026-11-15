"""Hermes (upstream Hermes Agent, NousResearch/hermes-agent): the one-shot stream parser and its argv.

WHAT THIS IS. The parser half of the Hermes one-shot driver: it reads the JSONL stream that
``hermes chat --query-file - --format stream-json`` writes on stdout and turns each line into driver.v2
event drafts (``schemas/control/v1/driver.v2.json``, ``$defs/event_draft``). It starts nothing, writes
nothing to an engine, registers nothing and reads no file. The driver that launches Hermes, and its
registration in the engine table, come in later slices; this module is written so that both can use it
unchanged (one ``HermesStreamParser`` per run's stream, fed by one reader thread).

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
                  that is not one JSON object, holds NaN, Infinity, a number too large for a double, a
                  key twice, or a lone UTF-16 surrogate anywhere, has an unknown type, does not have
                  its type's shape, or comes after the result

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
and no result of that name has arrived while several were open. Otherwise the result gets an id of its
own and pairs with nothing, because a guess would pair the wrong output with a call: once a result
arrives while several calls of a name are open, which call it ended is unknown, so every result of that
name pairs with nothing until all the calls open then, and any started meanwhile, have ended. Then
pairing starts afresh. An id-less call whose line stayed raw is counted as open too, so that its result
is never paired with another call.

OFFSETS. ``parse`` takes each line exactly as it was read, newline included; ``offset_start`` and
``offset_end`` are byte offsets (UTF-8) into that stream, the end exclusive. stdout and stderr are
counted separately.

THE ARGV. ``one_shot_argv`` gives what a registration with ``default_executable='hermes'`` promises:
argv[0] is the binary the route entry names when composition gave one, else ``hermes``, and nothing
else. The prompt goes on standard input (``--query-file -``), never on the command line, where every
user of the host can read it and its length is bounded; the core writes it and closes stdin, as on
today's one-turn path.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

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
_CALL_ID = re.compile(r'[^\r\n]{1,128}')
_NAME = re.compile(r'[^\r\n]{1,256}')
_MODEL = re.compile(r'[^\r\n]{1,256}')
_CONNECTION = re.compile(r'(subscription|api_key|passthrough):[^\r\n]+')
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


def one_shot_argv(binary: str | None = None) -> list[str]:
    """The one-shot command. ``binary`` is the absolute path a route entry names, when it names one."""
    if binary is None:
        executable = DEFAULT_EXECUTABLE
    elif isinstance(binary, str) and binary.startswith('/') and '\0' not in binary and '\n' not in binary:
        executable = binary
    else:
        raise HermesParseError('binary: an absolute path, or None for the default executable')
    return [executable, *ONE_SHOT_ARGS]


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
    surrogate into one, which no UTF-8 store can hold), and every float finite (``json`` decodes
    ``NaN``, ``Infinity`` and an overflowing ``1e999`` into floats that are not). Walked without
    recursion."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
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
        # names whose open id-less calls can no longer be told apart (a result came while several were open)
        self._ambiguous: set[str] = set()

    # -- the calls ----------------------------------------------------------------------------------
    def parse(self, line: str) -> list[dict]:
        """The drafts for one stdout line, read exactly as written (newline included)."""
        start, end = self._advance('stdout', line)
        text = line.rstrip('\r\n')
        if not text.strip():
            return []
        record = _load_object(text)
        drafts = None if record is None or self._result_seen else self._typed(record, start, end)
        if drafts is None:
            self.raw_lines += 1
            return [_output('stdout', line, start, end)]
        return drafts

    def parse_stderr(self, line: str) -> list[dict]:
        """stderr carries Hermes's diagnostics: given back as raw engine.output, untrusted."""
        start, end = self._advance('stderr', line)
        return [_output('stderr', line, start, end)] if line else []

    def finish(self, exit_status: int | None) -> list[dict]:
        """At the end of the stream. A run whose stream had no result record gets ``engine_exited``."""
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
        """Whether the process's exit status is the one the result record reported; None before a record."""
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
            canonical_text, no_text = _canonical()
            try:
                text = canonical_text(tool_input)
            except (no_text, RecursionError):
                text = None
        if text is None:
            # No truthful digest: the input is absent, not an object, or has no RFC 8785 text. The line
            # stays raw. An id-less call is still open under its name, so its result is never paired with
            # another call of that name.
            if engine_id is None:
                self._open_unnamed.setdefault(name, []).append(None)
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
        end = start + len(line.encode('utf-8', 'surrogatepass'))
        self._offsets[stream] = end
        return start, end

    def _take_session(self, session: str) -> None:
        if session and len(session) <= ENGINE_SESSION_ID_LIMIT and '\n' not in session and '\r' not in session:
            self.engine_session_id = session

    def _mint(self) -> str:
        self._minted += 1
        return f'e9n-{self._minted}'

    def _end_unnamed(self, name: str) -> str:
        """The call id for an id-less result of ``name``: its call's, only when that call is beyond doubt."""
        open_calls = self._open_unnamed.get(name, [])
        if len(open_calls) == 1 and name not in self._ambiguous:
            ended = open_calls.pop()
            call_id = ended if ended is not None else self._mint()   # a raw call has no tool.call to pair
        elif open_calls:
            # Several open (or a group already in doubt): one of them ended, which one is unknown.
            self._ambiguous.add(name)
            open_calls.pop()
            call_id = self._mint()
        else:
            call_id = self._mint()
        if not open_calls:
            self._open_unnamed.pop(name, None)
            self._ambiguous.discard(name)
        return call_id


def _call_id(engine_id: str) -> str:
    if _CALL_ID.fullmatch(engine_id) and not engine_id.startswith(('e9n-', 'e9h-')):
        return engine_id
    return 'e9h-' + hashlib.sha256(engine_id.encode('utf-8', 'surrogatepass')).hexdigest()[:32]


def _output(stream: str, text: str, start: int, end: int) -> dict:
    return {'type': 'engine.output', 'payload': {
        'stream': stream, 'text': text, 'offset_start': start, 'offset_end': end}}


def declaration() -> dict:
    """A copy of the driver.v2 declaration."""
    return json.loads(json.dumps(DECLARATION))


__all__ = ['DECLARATION', 'DEFAULT_EXECUTABLE', 'ENGINE', 'HermesParseError', 'HermesStreamParser',
           'ONE_SHOT_ARGS', 'TOOL_OUTPUT_CAP', 'UPSTREAM_PIN', 'declaration', 'input_digest', 'one_shot_argv']
