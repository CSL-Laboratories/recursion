"""The Hermes stream-json parser (``routes/engines/hermes.py``) against the synthetic streams in ``streams/``.

These tests live in the fixtures folder until the path map gives the Hermes lane a test path of its own
(``routes/engines/tests/`` is another lane's). Run by module name:

    PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.hermes.test_hermes_parser

Every stream here is SYNTHETIC (``cases.json``), written in the shapes of the pinned upstream emitter.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from recursion_core.routes.engines import hermes
from recursion_core.routes.engines.hermes import HermesParseError, HermesStreamParser

HERE = Path(__file__).resolve().parent
CONNECTION = 'subscription:hermes-grok'

# event.v1's payload keys for the types a driver may draft (every key required, no other allowed).
DRAFT_KEYS = {
    'engine.output': {'stream', 'text', 'offset_start', 'offset_end'},
    'tool.call': {'call_id', 'name', 'input_digest', 'input'},
    'tool.result': {'call_id', 'status', 'text', 'output_ref', 'truncated'},
    'usage': {'connection_id', 'model', 'bucket', 'span', 'input_tokens', 'output_tokens', 'reasoning_tokens',
              'cache_read_tokens', 'cache_write_tokens', 'cost_usd'},
    'limit': {'connection_id', 'scope', 'kind', 'model', 'bucket', 'reset_at'},
    'result': {'code', 'detail'},
    'input.accepted': {'delivery_id', 'turn_id', 'how'},
}
DECLARATION_FLAGS = ('multi_turn', 'mid_turn_input', 'interrupt', 'resume', 'fork', 'permission_bridge',
                     'typed_limits', 'usage')
CALL_ID = re.compile(r'[^\r\n]{1,128}')
NAME = re.compile(r'[^\r\n]{1,256}')
CODE_WORD = re.compile(r'[a-z][a-z0-9_]{0,63}')
SHA256 = re.compile(r'[0-9a-f]{64}')
UNKNOWN = {'value': None, 'quality': 'unknown', 'provenance': 'unavailable'}


def known(value: int) -> dict:
    return {'value': value, 'quality': 'known', 'provenance': 'observed'}


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def lines_of(name: str) -> list[str]:
    """The stream's lines exactly as a reader gets them: bytes split after each newline, then decoded."""
    data = (HERE / 'streams' / name).read_bytes()
    return [chunk.decode('utf-8') for chunk in data.splitlines(keepends=True)]


def run(name: str, exit_status: int | None = None) -> tuple[HermesStreamParser, list[dict]]:
    parser = HermesStreamParser(CONNECTION)
    drafts = []
    for line in lines_of(name):
        drafts.extend(parser.parse(line))
    drafts.extend(parser.finish(exit_status))
    return parser, drafts


def of_type(drafts: list[dict], kind: str) -> list[dict]:
    return [d['payload'] for d in drafts if d['type'] == kind]


def check_measurement(test: unittest.TestCase, value: dict, *, integer: bool) -> None:
    test.assertEqual(set(value), {'value', 'quality', 'provenance'})
    if value['quality'] == 'unknown':
        test.assertEqual(value, UNKNOWN)
        return
    test.assertIn(value['quality'], ('known', 'lower_bound'))
    test.assertIn(value['provenance'], ('observed', 'modelled', 'inferred'))
    if integer:
        test.assertIs(type(value['value']), int)
    else:
        test.assertIsInstance(value['value'], (int, float))
    test.assertGreater(value['value'], 0, 'a 0 from Hermes is not known to be 0')


def check_draft(test: unittest.TestCase, draft: dict) -> None:
    """event.v1's rules for one draft, as far as this parser can break them."""
    test.assertEqual(set(draft), {'type', 'payload'})
    kind, p = draft['type'], draft['payload']
    test.assertIn(kind, DRAFT_KEYS)
    test.assertEqual(set(p), DRAFT_KEYS[kind], kind)
    if kind == 'engine.output':
        test.assertIn(p['stream'], ('stdout', 'stderr'))
        test.assertIsInstance(p['text'], str)
        test.assertIs(type(p['offset_start']), int)
        test.assertIs(type(p['offset_end']), int)
        test.assertLessEqual(0, p['offset_start'])
        test.assertLessEqual(p['offset_start'], p['offset_end'])
    elif kind == 'tool.call':
        test.assertTrue(CALL_ID.fullmatch(p['call_id']))
        test.assertTrue(NAME.fullmatch(p['name']))
        test.assertTrue(SHA256.fullmatch(p['input_digest']))
        test.assertTrue(p['input'] is None or isinstance(p['input'], dict))
        if p['input'] is not None:
            test.assertEqual(p['input_digest'], hermes.input_digest(p['input']))
    elif kind == 'tool.result':
        test.assertTrue(CALL_ID.fullmatch(p['call_id']))
        test.assertIn(p['status'], ('ok', 'error', 'denied', 'cancelled'))
        test.assertIsInstance(p['text'], str)   # this parser always carries the text
        test.assertIsNone(p['output_ref'])
        test.assertIs(type(p['truncated']), bool)
    elif kind == 'usage':
        test.assertEqual(p['connection_id'], CONNECTION)
        test.assertIn(p['span'], ('call', 'turn', 'session_total'))
        test.assertTrue(p['model'] is None or (isinstance(p['model'], str) and p['model']))
        test.assertIsNone(p['bucket'])
        for name in ('input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens'):
            check_measurement(test, p[name], integer=True)
        check_measurement(test, p['cost_usd'], integer=False)
    elif kind == 'result':
        test.assertTrue(p['code'] is None or CODE_WORD.fullmatch(p['code']))
        test.assertIsInstance(p['detail'], str)
    else:
        test.fail(f'the Hermes parser drafted {kind}')


class FixtureIndexTest(unittest.TestCase):
    def test_every_stream_is_indexed_and_marked_synthetic(self):
        doc = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
        self.assertIs(doc['synthetic'], True)
        self.assertIn('not captured', doc['source'])
        listed = {case['stream'] for case in doc['cases']}
        on_disk = {f'streams/{p.name}' for p in (HERE / 'streams').iterdir()}
        self.assertEqual(listed, on_disk)
        for case in doc['cases']:
            self.assertIs(case['synthetic'], True, case['stream'])
            self.assertIs(type(case['exit_status']), int)
            self.assertTrue(case['what'])

    def test_every_draft_of_every_stream_has_event_v1s_shape_and_none_is_a_limit(self):
        doc = json.loads((HERE / 'cases.json').read_text(encoding='utf-8'))
        for case in doc['cases']:
            name = case['stream'].split('/', 1)[1]
            with self.subTest(stream=name):
                parser, drafts = run(name, case['exit_status'])
                self.assertTrue(drafts)
                for draft in drafts:
                    check_draft(self, draft)
                self.assertEqual(of_type(drafts, 'limit'), [])
                self.assertEqual(len(of_type(drafts, 'result')), 1, 'exactly one result per run')
                after = drafts[[d['type'] for d in drafts].index('result') + 1:]
                self.assertEqual({d['type'] for d in after} - {'engine.output'}, set(),
                                 'after the result only raw lines')

    def test_stdout_offsets_tile_each_stream_without_gap_or_overlap(self):
        for path in sorted((HERE / 'streams').iterdir()):
            with self.subTest(stream=path.name):
                parser = HermesStreamParser(CONNECTION)
                position = 0
                for line in lines_of(path.name):
                    for draft in parser.parse(line):
                        if draft['type'] == 'engine.output':
                            self.assertEqual(draft['payload']['offset_start'], position)
                            self.assertEqual(draft['payload']['offset_end'], position + len(line.encode('utf-8')))
                    position += len(line.encode('utf-8'))
                self.assertEqual(position, path.stat().st_size)


class DeclarationTest(unittest.TestCase):
    def test_the_declaration_is_driver_v2s_root_record_with_usage_only(self):
        record = hermes.declaration()
        self.assertEqual(set(record), {'engine', 'driver', 'flags', 'input_ack'})
        self.assertEqual(record['engine'], 'hermes')
        self.assertIs(type(record['driver']), int)
        self.assertEqual(record['driver'], 2)
        self.assertEqual(tuple(record['flags']), DECLARATION_FLAGS)
        self.assertTrue(all(type(v) is bool for v in record['flags'].values()))
        self.assertEqual([k for k, v in record['flags'].items() if v], ['usage'])
        self.assertIs(record['input_ack'], False)
        record['flags']['usage'] = False
        self.assertIs(hermes.DECLARATION['flags']['usage'], True, 'declaration() gives a copy')

    def test_typed_limits_is_false_because_the_stream_has_no_typed_limit(self):
        self.assertIs(hermes.DECLARATION['flags']['typed_limits'], False)


class OneShotArgvTest(unittest.TestCase):
    def test_argv_zero_is_the_default_executable_and_the_prompt_is_not_on_the_command_line(self):
        self.assertEqual(hermes.DEFAULT_EXECUTABLE, 'hermes')
        self.assertEqual(hermes.one_shot_argv(), ['hermes', 'chat', '--query-file', '-', '--format', 'stream-json'])

    def test_argv_zero_is_the_binary_composition_gave_and_nothing_else(self):
        self.assertEqual(hermes.one_shot_argv('/opt/hermes/bin/hermes')[0], '/opt/hermes/bin/hermes')
        self.assertEqual(hermes.one_shot_argv('/opt/hermes/bin/hermes')[1:], list(hermes.ONE_SHOT_ARGS))

    def test_a_binary_that_is_not_an_absolute_path_is_refused(self):
        for bad in ('hermes', 'bin/hermes', '', '/a\0b', '/a\nb', b'/usr/bin/hermes', 7):
            with self.subTest(binary=bad), self.assertRaises(HermesParseError):
                hermes.one_shot_argv(bad)

    def test_no_kanban_unattended_or_approval_bypass_flag_is_passed(self):
        argv = hermes.one_shot_argv()
        for flag in ('-z', '--oneshot', '--yolo', '-q', '--query', '--tui'):
            self.assertNotIn(flag, argv)


class ConstructionTest(unittest.TestCase):
    def test_the_connection_id_has_common_v1s_form(self):
        for good in ('subscription:x', 'api_key:hermes-1', 'passthrough:a b', 'api_key:' + 'k' * 248):
            self.assertEqual(HermesStreamParser(good).connection_id, good)
        for bad in ('', 'hermes', 'oauth:x', 'subscription:', 'api_key:a\nb', 'api_key:a\rb',
                    'api_key:' + 'k' * 249, None, 7):
            with self.subTest(connection_id=bad), self.assertRaises(HermesParseError):
                HermesStreamParser(bad)

    def test_a_line_that_is_not_text_is_a_caller_error(self):
        parser = HermesStreamParser(CONNECTION)
        for bad in (b'{"type": "text"}\n', None, 3):
            with self.subTest(line=bad), self.assertRaises(HermesParseError):
                parser.parse(bad)


class StreamTest(unittest.TestCase):
    def test_s01_text_deltas_are_engine_output_and_the_result_gives_usage_then_result(self):
        parser, drafts = run('s01-text-only.jsonl', 0)
        self.assertEqual([d['type'] for d in drafts], ['engine.output'] * 3 + ['usage', 'result'])
        self.assertEqual(''.join(p['text'] for p in of_type(drafts, 'engine.output')), 'hel\n  lo')
        usage = of_type(drafts, 'usage')[0]
        self.assertEqual(usage['span'], 'session_total')
        self.assertIsNone(usage['model'], 'init named no model')
        self.assertEqual(usage['input_tokens'], known(1824))
        self.assertEqual(usage['output_tokens'], known(356))
        for name in ('reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cost_usd'):
            self.assertEqual(usage[name], UNKNOWN, name)
        self.assertEqual(of_type(drafts, 'result'), [{'code': None, 'detail': 'hel\n  lo'}])
        self.assertEqual(parser.engine_session_id, '20261114_170000_a1b2c3')
        self.assertEqual(parser.exit_code, 0)
        self.assertIs(parser.exit_agrees(0), True)
        self.assertIs(parser.exit_agrees(1), False)
        self.assertEqual(parser.raw_lines, 0)

    def test_s02_parallel_calls_pair_by_their_own_ids_and_a_cut_output_is_truncated(self):
        parser, drafts = run('s02-tools.jsonl', 0)
        calls, results = of_type(drafts, 'tool.call'), of_type(drafts, 'tool.result')
        self.assertEqual(calls, [
            {'call_id': 'call-a', 'name': 'read_file', 'input_digest': digest_of('{"path":"x"}'), 'input': {'path': 'x'}},
            {'call_id': 'call-b', 'name': 'read_file', 'input_digest': digest_of('{"path":"y"}'), 'input': {'path': 'y'}},
        ])
        self.assertEqual([(r['call_id'], r['status'], r['truncated']) for r in results],
                         [('call-b', 'ok', False), ('call-a', 'ok', True)])
        self.assertEqual(results[0]['text'], 'y')
        self.assertEqual(len(results[1]['text']), hermes.TOOL_OUTPUT_CAP + 3)
        usage = of_type(drafts, 'usage')[0]
        self.assertEqual(usage['model'], 'hermes-test-model')
        self.assertEqual((usage['cache_read_tokens'], usage['cache_write_tokens']), (known(2048), known(512)))

    def test_truncated_is_exactly_longer_than_the_emitters_cap(self):
        for length, cut in ((0, False), (hermes.TOOL_OUTPUT_CAP, False), (hermes.TOOL_OUTPUT_CAP + 1, True),
                            (hermes.TOOL_OUTPUT_CAP + 3, True)):
            parser = HermesStreamParser(CONNECTION)
            line = json.dumps({'type': 'tool_result', 'name': 't', 'tool_call_id': 'c', 'output': 'o' * length,
                               'duration_ms': 1, 'is_error': False}) + '\n'
            with self.subTest(length=length):
                self.assertIs(of_type(parser.parse(line), 'tool.result')[0]['truncated'], cut)

    def test_s03_calls_without_ids_pair_only_when_one_call_of_that_name_is_open(self):
        parser, drafts = run('s03-tools-without-ids.jsonl', 0)
        calls, results = of_type(drafts, 'tool.call'), of_type(drafts, 'tool.result')
        self.assertEqual([(c['call_id'], c['name']) for c in calls],
                         [('e9n-1', 'terminal'), ('e9n-2', 'web_search'), ('e9n-3', 'web_search'), ('e9n-5', 'patch')])
        self.assertEqual([(r['call_id'], r['status']) for r in results],
                         [('e9n-1', 'ok'), ('e9n-4', 'ok'), ('e9n-5', 'error')])
        self.assertNotIn(results[1]['call_id'], {c['call_id'] for c in calls}, 'an ambiguous result pairs with nothing')
        patch = calls[3]
        self.assertIsNone(patch['input'], 'args that are not an object carry no input')
        self.assertEqual(patch['input_digest'], digest_of('null'))
        usage = of_type(drafts, 'usage')[0]
        self.assertEqual(usage['input_tokens'], UNKNOWN, 'a 0 is not known to be 0')
        self.assertEqual(usage['output_tokens'], known(40))

    def test_a_result_with_no_id_pairs_with_the_one_open_call_of_its_name_even_after_others_closed(self):
        parser = HermesStreamParser(CONNECTION)
        use = json.dumps({'type': 'tool_use', 'name': 'n'}) + '\n'
        done = json.dumps({'type': 'tool_result', 'name': 'n', 'output': '', 'duration_ms': 0, 'is_error': False}) + '\n'
        ids = [of_type(parser.parse(line), kind)[0]['call_id'] for line, kind in
               ((use, 'tool.call'), (done, 'tool.result'), (use, 'tool.call'), (done, 'tool.result'), (done, 'tool.result'))]
        self.assertEqual(ids, ['e9n-1', 'e9n-1', 'e9n-2', 'e9n-2', 'e9n-3'])

    def test_s04_a_failed_turn_is_engine_error_with_hermes_words_in_the_detail(self):
        parser, drafts = run('s04-failed.jsonl', 1)
        self.assertEqual(of_type(drafts, 'result'), [{
            'code': 'engine_error',
            'detail': 'hermes exit 1: the turn failed or did not finish, or the start failed; hermes said: boom'}])
        self.assertEqual(of_type(drafts, 'usage')[0]['input_tokens'], known(3))

    def test_s05_a_failed_start_gives_a_result_and_no_usage_and_no_session(self):
        parser, drafts = run('s05-start-failed.jsonl', 1)
        self.assertEqual([d['type'] for d in drafts], ['result'])
        self.assertEqual(drafts[0]['payload']['code'], 'engine_error')
        self.assertIn('credentials or agent init failed', drafts[0]['payload']['detail'])
        self.assertIsNone(parser.engine_session_id)

    def test_s06_s07_s08_other_exits_are_engine_errors_and_exit_75_is_not_a_limit(self):
        expected = {
            's06-interrupted.jsonl': (130, 'hermes exit 130: the turn was interrupted'),
            's07-detached-input-required.jsonl': (3, 'hermes exit 3: the run detached: a human must answer before '
                                                     'the turn can go on; hermes said: Input required; detached '
                                                     'without cancelling. Resume this session interactively.'),
            's08-exit-75.jsonl': (75, 'hermes exit 75: EX_TEMPFAIL (Hermes uses it for a Kanban worker hitting a '
                                      'rate limit, billing wall, overload, server error or timeout; not typed as a '
                                      'plan limit here); hermes said: rate limited'),
        }
        for name, (status, detail) in expected.items():
            with self.subTest(stream=name):
                parser, drafts = run(name, status)
                self.assertEqual(of_type(drafts, 'result'), [{'code': 'engine_error', 'detail': detail}])
                self.assertEqual(of_type(drafts, 'limit'), [])
                self.assertEqual(parser.exit_code, status)

    def test_an_exit_code_the_parser_does_not_know_is_still_an_engine_error(self):
        parser = HermesStreamParser(CONNECTION)
        line = json.dumps({'type': 'result', 'session_id': 's', 'exit_code': 9, 'text': '', 'tokens': {}}) + '\n'
        self.assertEqual(parser.parse(line), [{'type': 'result', 'payload': {
            'code': 'engine_error', 'detail': 'hermes exit 9: an exit code this parser does not know'}}])

    def test_s09_after_a_replay_gap_the_result_carries_the_whole_reply(self):
        parser, drafts = run('s09-replay-gap.jsonl', 0)
        self.assertEqual(of_type(drafts, 'engine.output')[0]['text'], 'Hello wor')
        self.assertEqual(of_type(drafts, 'result'), [{'code': None, 'detail': 'Hello world, the whole reply.'}])

    def test_s10_lines_that_are_not_the_protocol_are_kept_raw_and_nothing_is_typed_after_the_result(self):
        lines = lines_of('s10-not-the-protocol.jsonl')
        parser, drafts = run('s10-not-the-protocol.jsonl', 0)
        raw = [i for i, line in enumerate(lines) if i not in (7, 9)]   # the first init and the first result
        self.assertEqual(parser.raw_lines, len(raw))
        outputs = of_type(drafts, 'engine.output')
        self.assertEqual([o['text'] for o in outputs], [lines[i] for i in raw])
        self.assertEqual(of_type(drafts, 'result'), [{'code': None, 'detail': 'fine'}])
        self.assertEqual(parser.model, 'm', 'a second init changes nothing')
        self.assertEqual(parser.engine_session_id, '20261114_170000_a1b2c3')
        self.assertEqual(parser.exit_code, 0)

    def test_s11_a_stream_cut_off_mid_line_ends_engine_exited_once(self):
        parser = HermesStreamParser(CONNECTION)
        drafts = []
        for line in lines_of('s11-cut-off.jsonl'):
            drafts.extend(parser.parse(line))
        self.assertEqual(of_type(drafts, 'engine.output')[-1]['text'], '{"type": "text", "te')
        self.assertIsNone(parser.exit_agrees(-9))
        self.assertEqual(parser.finish(-9), [{'type': 'result', 'payload': {
            'code': 'engine_exited', 'detail': 'hermes: the stream ended with no result record (exit status -9)'}}])
        self.assertEqual(parser.finish(-9), [])
        late = '{"type": "result", "session_id": "", "exit_code": 0, "text": "", "tokens": {}}\n'
        size = (HERE / 'streams' / 's11-cut-off.jsonl').stat().st_size
        self.assertEqual(parser.parse(late), [{'type': 'engine.output', 'payload': {
            'stream': 'stdout', 'text': late, 'offset_start': size, 'offset_end': size + len(late)}}],
            'a result after the end is not typed')

    def test_finish_without_an_exit_status_says_so(self):
        self.assertIn('(no exit status)', HermesStreamParser(CONNECTION).finish(None)[0]['payload']['detail'])

    def test_s12_engine_ids_that_are_not_call_ids_are_hashed_and_still_pair(self):
        parser, drafts = run('s12-ids-and-inputs.jsonl', 0)
        calls, results = of_type(drafts, 'tool.call'), of_type(drafts, 'tool.result')
        for engine_id, call, result in zip(('i' * 200, 'e9n-1', 'a\nb'), calls, results):
            with self.subTest(engine_id=engine_id[:12]):
                self.assertEqual(call['call_id'], 'e9h-' + digest_of(engine_id)[:32])
                self.assertEqual(result['call_id'], call['call_id'])

    def test_s12_an_input_with_no_canonical_text_or_too_large_is_not_carried(self):
        parser, drafts = run('s12-ids-and-inputs.jsonl', 0)
        by_id = {c['call_id']: c for c in of_type(drafts, 'tool.call')}
        big_int, big = by_id['call-big-int'], by_id['call-big']
        self.assertIsNone(big_int['input'])
        self.assertEqual(big_int['input_digest'], digest_of('null'), 'no RFC 8785 text: the digest is of what is carried')
        self.assertIsNone(big['input'])
        self.assertEqual(big['input_digest'], digest_of('{"blob":"' + 'b' * 70000 + '"}'),
                         'too large to carry: the digest is still of the input')
        self.assertEqual(parser.uncarried_inputs, 2)
        self.assertEqual(of_type(drafts, 'usage'), [], 'every count was 0: no usage draft')

    def test_an_input_at_the_carry_limit_is_carried(self):
        parser = HermesStreamParser(CONNECTION)
        filler = hermes.INPUT_CARRY_LIMIT - len('{"k":""}')
        for size, carried in ((filler, True), (filler + 1, False)):
            line = json.dumps({'type': 'tool_use', 'name': 't', 'tool_call_id': f'c{size}', 'input': {'k': 'v' * size}})
            with self.subTest(size=size):
                call = of_type(parser.parse(line + '\n'), 'tool.call')[0]
                self.assertIs(call['input'] is not None, carried)

    def test_s13_offsets_count_bytes_not_characters(self):
        lines = lines_of('s13-text-outside-ascii.jsonl')
        parser, drafts = run('s13-text-outside-ascii.jsonl', 0)
        output = of_type(drafts, 'engine.output')[0]
        self.assertEqual(output['text'], 'héllo ✓ 日本')
        start = len(lines[0].encode('utf-8'))
        self.assertEqual((output['offset_start'], output['offset_end']), (start, start + len(lines[1].encode('utf-8'))))
        self.assertGreater(len(lines[1].encode('utf-8')), len(lines[1]))

    def test_a_crlf_line_ending_is_counted_and_the_record_still_parses(self):
        parser = HermesStreamParser(CONNECTION)
        line = '{"type": "text", "text": "hi"}\r\n'
        self.assertEqual(parser.parse(line), [{'type': 'engine.output', 'payload': {
            'stream': 'stdout', 'text': 'hi', 'offset_start': 0, 'offset_end': len(line)}}])

    def test_a_blank_line_gives_nothing_but_moves_the_offset(self):
        parser = HermesStreamParser(CONNECTION)
        self.assertEqual(parser.parse('\n'), [])
        self.assertEqual(parser.parse('  \n'), [])
        self.assertEqual(parser.parse('{"type": "text", "text": "x"}\n')[0]['payload']['offset_start'], 4)

    def test_stderr_is_raw_engine_output_with_its_own_offsets(self):
        parser = HermesStreamParser(CONNECTION)
        parser.parse('{"type": "text", "text": "x"}\n')
        self.assertEqual(parser.parse_stderr('session_id: s\n'), [{'type': 'engine.output', 'payload': {
            'stream': 'stderr', 'text': 'session_id: s\n', 'offset_start': 0, 'offset_end': 14}}])
        self.assertEqual(parser.parse_stderr('é\n')[0]['payload']['offset_start'], 14)
        self.assertEqual(parser.parse_stderr(''), [])

    def test_a_record_missing_a_required_field_is_raw(self):
        records = [
            {'type': 'system', 'subtype': 'init', 'model': ''},
            {'type': 'system', 'subtype': 'other', 'model': '', 'session_id': ''},
            {'type': 'text', 'text': 7},
            {'type': 'tool_use', 'name': ''},
            {'type': 'tool_use', 'name': 'x\ny'},
            {'type': 'tool_use', 'name': 'n' * 257},
            {'type': 'tool_use', 'name': 't', 'tool_call_id': 5},
            {'type': 'tool_use', 'name': 't', 'tool_call_id': ''},
            {'type': 'tool_result', 'name': 't', 'output': 'o', 'is_error': 'no'},
            {'type': 'tool_result', 'name': 't', 'output': None, 'is_error': False},
            {'type': 'tool_result', 'name': 't', 'tool_call_id': 3, 'output': '', 'is_error': False},
            {'type': 'result', 'session_id': 's', 'exit_code': '0', 'text': '', 'tokens': {}},
            {'type': 'result', 'session_id': 's', 'exit_code': True, 'text': '', 'tokens': {}},
            {'type': 'result', 'session_id': 's', 'exit_code': 0, 'text': '', 'tokens': []},
            {'type': 'result', 'session_id': None, 'exit_code': 0, 'text': '', 'tokens': {}},
            {'type': 'result', 'session_id': 's', 'exit_code': 0, 'text': 3, 'tokens': {}},
            {'type': 'result', 'session_id': 's', 'exit_code': 1, 'text': '', 'tokens': {}, 'error': 3},
        ]
        for record in records:
            parser = HermesStreamParser(CONNECTION)
            line = json.dumps(record) + '\n'
            with self.subTest(record=record):
                self.assertEqual(parser.parse(line), [{'type': 'engine.output', 'payload': {
                    'stream': 'stdout', 'text': line, 'offset_start': 0, 'offset_end': len(line.encode('utf-8'))}}])
                self.assertFalse(parser.result_seen)

    def test_nan_or_infinity_anywhere_makes_the_line_raw_even_in_a_field_the_parser_does_not_read(self):
        for constant in ('NaN', 'Infinity', '-Infinity'):
            parser = HermesStreamParser(CONNECTION)
            line = ('{"type": "result", "session_id": "s", "exit_code": 0, "text": "", "tokens": {"output": 1}, '
                    f'"duration_ms": {constant}}}\n')
            with self.subTest(constant=constant):
                self.assertEqual([d['type'] for d in parser.parse(line)], ['engine.output'])
                self.assertFalse(parser.result_seen)

    def test_an_input_that_is_present_but_not_an_object_is_not_carried(self):
        for value in ([1, 2], 'text', 3, True):
            parser = HermesStreamParser(CONNECTION)
            line = json.dumps({'type': 'tool_use', 'name': 't', 'tool_call_id': 'c', 'input': value}) + '\n'
            with self.subTest(input=value):
                call = of_type(parser.parse(line), 'tool.call')[0]
                self.assertIsNone(call['input'])
                self.assertEqual(call['input_digest'], digest_of('null'))

    def test_a_count_is_known_only_when_it_is_a_positive_whole_number(self):
        for value, expected in ((5, known(5)), (0, UNKNOWN), (-1, UNKNOWN), (2.0, UNKNOWN), (True, UNKNOWN),
                                ('5', UNKNOWN), (None, UNKNOWN)):
            parser = HermesStreamParser(CONNECTION)
            line = json.dumps({'type': 'result', 'session_id': 's', 'exit_code': 0, 'text': '',
                               'tokens': {'input': value, 'output': 1}}) + '\n'
            with self.subTest(value=value):
                self.assertEqual(of_type(parser.parse(line), 'usage')[0]['input_tokens'], expected)

    def test_a_model_or_session_id_with_a_line_break_or_too_long_is_not_kept(self):
        parser = HermesStreamParser(CONNECTION)
        parser.parse(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'a\nb', 'session_id': 's' * 257}) + '\n')
        self.assertIsNone(parser.model)
        self.assertIsNone(parser.engine_session_id)
        parser.parse(json.dumps({'type': 'result', 'session_id': 'x\ry', 'exit_code': 0, 'text': '',
                                 'tokens': {'output': 1}}) + '\n')
        self.assertIsNone(parser.engine_session_id)
        for session in ('x\ny', 'x\n'):
            other = HermesStreamParser(CONNECTION)
            other.parse(json.dumps({'type': 'system', 'subtype': 'init', 'model': '', 'session_id': session}) + '\n')
            with self.subTest(session=session):
                self.assertIsNone(other.engine_session_id)

    def test_deeply_nested_json_is_raw_not_a_crash(self):
        parser = HermesStreamParser(CONNECTION)
        line = '[' * 100000 + ']' * 100000 + '\n'
        self.assertEqual(parser.parse(line)[0]['type'], 'engine.output')
        deep = json.dumps({'type': 'tool_use', 'name': 't', 'tool_call_id': 'c', 'input': {'a': [[[[[[]]]]]] * 1}})
        nested = {'v': 0}
        for _ in range(80):
            nested = {'v': nested}
        call = of_type(parser.parse(json.dumps({'type': 'tool_use', 'name': 't', 'tool_call_id': 'd',
                                                'input': nested}) + '\n'), 'tool.call')[0]
        self.assertIsNone(call['input'], 'deeper than RFC 8785 text is made for here: not carried')
        self.assertEqual(of_type(parser.parse(deep + '\n'), 'tool.call')[0]['input'], {'a': [[[[[[]]]]]]})


if __name__ == '__main__':
    unittest.main()
