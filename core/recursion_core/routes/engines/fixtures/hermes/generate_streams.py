"""Writes the SYNTHETIC Hermes stream-json fixtures in ``streams/`` and their index ``cases.json``.

None of these streams was captured from a real Hermes. Each was written by hand, line by line, in the
shapes the pinned upstream emitter writes (``hermes_cli/stream_json.py``, ``StreamJsonEmitter``, at
NousResearch/hermes-agent 2f7e62bad99052055d1db9213b81eaab26174bc4): the same keys in the same order,
serialised the same way (``json.dumps(obj, ensure_ascii=False)``, then a newline), with a made-up
``timestamp``. Where a case writes something the pinned emitter never writes, its ``what`` says so.
A captured stream from Lucas's attended run will be added beside these, labelled as captured.

Run from the repository root: ``python core/recursion_core/routes/engines/fixtures/hermes/generate_streams.py``.
It rewrites the files; the tests read them and never run this.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIN = 'NousResearch/hermes-agent@2f7e62bad99052055d1db9213b81eaab26174bc4'
T0 = 1794700000000   # a made-up start time, Unix milliseconds


def line(obj: dict, at: int) -> str:
    return json.dumps({**obj, 'timestamp': T0 + at}, ensure_ascii=False) + '\n'


def init(model: str = '', session_id: str = '') -> dict:
    return {'type': 'system', 'subtype': 'init', 'model': model, 'session_id': session_id}


def text(t: str) -> dict:
    return {'type': 'text', 'text': t}


def tool_use(name: str, call_id: str | None = None, args=None) -> dict:
    payload = {'type': 'tool_use', 'name': name}
    if call_id:
        payload['tool_call_id'] = call_id
    if isinstance(args, dict):
        payload['input'] = args
    return payload


def tool_result(name: str, output: str, *, call_id: str | None = None, ms: int = 12, is_error: bool = False) -> dict:
    return {'type': 'tool_result', 'name': name, **({'tool_call_id': call_id} if call_id else {}),
            'output': output if len(output) <= 5000 else output[:5000] + '...',
            'duration_ms': ms, 'is_error': is_error}


def result(session_id: str, exit_code: int, final: str = '', *, input_tokens=0, output_tokens=0,
           cache_read=0, cache_write=0, error: str | None = None, replay_gap: bool = False, ms: int = 2100) -> dict:
    payload = {'type': 'result', 'session_id': session_id, 'exit_code': exit_code, 'text': final,
               'tokens': {'input': input_tokens, 'output': output_tokens,
                          'total': input_tokens + output_tokens, 'cache_read': cache_read,
                          'cache_write': cache_write},
               'duration_ms': ms}
    if error:
        payload['error'] = error
    if replay_gap:
        payload['replay_gap'] = True
    return payload


SID = '20261114_170000_a1b2c3'


def cases() -> list[tuple[str, int, str, list[str]]]:
    """(file, exit status, what, lines)."""
    out = []
    out.append(('s01-text-only.jsonl', 0, 'a completed answer in three deltas, one of them whitespace only', [
        line(init(), 0), line(text('hel'), 100), line(text('\n  '), 101), line(text('lo'), 102),
        line(result(SID, 0, 'hel\n  lo', input_tokens=1824, output_tokens=356), 2100)]))
    long_output = 'x' * 6000
    out.append(('s02-tools.jsonl', 0, 'two parallel calls of one tool, each with its id, ending out of order; '
                'one output cut at 5000 characters', [
        line(init('hermes-test-model', ''), 0),
        line(tool_use('read_file', 'call-a', {'path': 'x'}), 10),
        line(tool_use('read_file', 'call-b', {'path': 'y'}), 11),
        line(tool_result('read_file', 'y', call_id='call-b'), 40),
        line(tool_result('read_file', long_output, call_id='call-a', ms=500), 510),
        line(text('done'), 600),
        line(result(SID, 0, 'done', input_tokens=3000, output_tokens=120, cache_read=2048, cache_write=512), 700)]))
    out.append(('s03-tools-without-ids.jsonl', 0, 'calls with no tool_call_id: one pairs by name; two open '
                'calls of one name leave a result that pairs with nothing; a call whose args are not an '
                'object has no input to digest, so its line stays raw and its result pairs with nothing', [
        line(init(), 0),
        line(tool_use('terminal', None, {'command': 'ls'}), 10),
        line(tool_result('terminal', 'a\nb'), 30),
        line(tool_use('web_search', None, {'query': 'one'}), 40),
        line(tool_use('web_search', None, {'query': 'two'}), 41),
        line(tool_result('web_search', 'first to finish'), 90),
        line(tool_use('patch', None, 'not an object'), 100),
        line(tool_result('patch', 'no such file', is_error=True), 120),
        line(result(SID, 0, 'ok', output_tokens=40), 300)]))
    out.append(('s04-failed.jsonl', 1, 'a turn that failed after some output', [
        line(init(), 0), line(text('partial'), 50),
        line(result(SID, 1, '', input_tokens=3, error='boom'), 400)]))
    out.append(('s05-start-failed.jsonl', 1, 'credentials could not be resolved: init then result, no session, '
                'every count written as 0', [
        line(init(), 0),
        line(result('', 1, '', error='credentials or agent init failed'), 30)]))
    out.append(('s06-interrupted.jsonl', 130, 'the client was interrupted (the work may go on at the Hermes gateway)', [
        line(init(), 0), line(text('work'), 80),
        line(result('', 130, ''), 90)]))
    out.append(('s07-detached-input-required.jsonl', 3, 'one-shot detached because a prompt needs a human '
                '(the turn is left parked at the gateway, not cancelled)', [
        line(init(), 0),
        line(tool_use('terminal', 'call-x', {'command': 'rm -rf build'}), 20),
        line(result(SID, 3, '', error='Input required; detached without cancelling. '
                    'Resume this session interactively.'), 60)]))
    out.append(('s08-exit-75.jsonl', 75, 'NOT WRITTEN BY THE PINNED stream-json PATH (it never exits 75); '
                'here to show that an exit 75 types no limit', [
        line(init(), 0),
        line(result(SID, 75, '', error='rate limited'), 40)]))
    out.append(('s09-replay-gap.jsonl', 0, 'the viewer fell behind: text stops at the gap and the result '
                'carries the whole reply', [
        line(init(), 0), line(text('Hello wor'), 10),
        line(result(SID, 0, 'Hello world, the whole reply.', input_tokens=10, output_tokens=5,
                    replay_gap=True), 900)]))
    garbage = [
        'not json at all\n',
        '[1, 2]\n',
        line({'type': 'reasoning', 'text': 'hmm'}, 1),
        '{"type": "text", "text": NaN, "timestamp": 1794700000002}\n',
        '{"type": "text", "text": "a", "text": "b", "timestamp": 1794700000003}\n',
        # An escaped lone surrogate (json.dumps writes the escape, so the line itself is ASCII).
        json.dumps({'type': 'text', 'text': 'x' + chr(0xD800), 'timestamp': T0 + 3}) + '\n',
        '{"type": "result", "session_id": "s", "exit_code": 0, "text": "x", "tokens": {"output": 1}, '
        '"duration_ms": 1e999, "timestamp": 1794700000003}\n',
        line({'type': 'tool_use'}, 4),
        line(text(''), 5),
        line(init('m', ''), 6),
        line(init('m2', 'other'), 7),
        line(result(SID, 0, 'fine', output_tokens=2), 8),
        line(text('after'), 9),
        line(result(SID, 1, '', error='a second result'), 10),
    ]
    out.append(('s10-not-the-protocol.jsonl', 0, 'lines that are not the protocol (some the pinned emitter '
                'never writes), a second init, and lines after the result', garbage))
    out.append(('s11-cut-off.jsonl', -9, 'the process was killed mid-line: no result record', [
        line(init(), 0), line(text('par'), 10), '{"type": "text", "te']))
    big = {'blob': 'b' * 70000}
    out.append(('s12-ids-and-inputs.jsonl', 0, 'call ids that are not valid call ids or use this parser\'s '
                'prefixes; an input with no RFC 8785 text (its line stays raw); an input too large to carry '
                '(its digest is still the input\'s)', [
        line(init(), 0),
        line(tool_use('t', 'i' * 200, {'n': 1}), 10),
        line(tool_result('t', 'r1', call_id='i' * 200), 11),
        line(tool_use('t', 'e9n-1', {'n': 2}), 12),
        line(tool_result('t', 'r2', call_id='e9n-1'), 13),
        line(tool_use('t', 'a\nb', {'n': 3}), 14),
        line(tool_result('t', 'r3', call_id='a\nb'), 15),
        line(tool_use('t', 'call-big-int', {'n': 9007199254740993}), 16),
        line(tool_use('t', 'call-big', big), 17),
        line(result(SID, 0, ''), 18)]))
    # A CRLF ending is tested in memory: a CR in a committed file fails the repository's whitespace check.
    out.append(('s13-text-outside-ascii.jsonl', 0, 'text outside ASCII, for byte offsets', [
        line(init(), 0), line(text('héllo ✓ 日本'), 5),
        line(result(SID, 0, 'héllo ✓ 日本', output_tokens=7), 9)]))
    out.append(('s14-ambiguous-group-drains.jsonl', 0, 'id-less calls of one name: a result while two are '
                'open puts the group in doubt, a call started meanwhile joins it, no result in it pairs, '
                'and once all have ended the next call pairs again', [
        line(init(), 0),
        line(tool_use('web_search', None, {'query': 'a'}), 10),
        line(tool_use('web_search', None, {'query': 'b'}), 11),
        line(tool_result('web_search', 'one of a or b'), 20),
        line(tool_use('web_search', None, {'query': 'c'}), 21),
        line(tool_result('web_search', 'another'), 30),
        line(tool_result('web_search', 'the last of the three'), 31),
        line(tool_use('web_search', None, {'query': 'd'}), 40),
        line(tool_result('web_search', 'd'), 50),
        line(result(SID, 0, 'ok', output_tokens=9), 60)]))
    return out


def main() -> None:
    streams = HERE / 'streams'
    streams.mkdir(exist_ok=True)
    index = []
    for name, status, what, lines in cases():
        (streams / name).write_bytes(''.join(lines).encode('utf-8'))
        index.append({'stream': f'streams/{name}', 'exit_status': status, 'synthetic': True, 'what': what})
    doc = {'synthetic': True, 'source': f'written by hand in the shapes of hermes_cli/stream_json.py at {PIN}; '
           'not captured', 'cases': index}
    (HERE / 'cases.json').write_bytes((json.dumps(doc, indent=1, ensure_ascii=False) + '\n').encode('utf-8'))


if __name__ == '__main__':
    main()
