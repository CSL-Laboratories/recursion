# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity's one-shot drive: the stream-json parser over the synthetic fixtures, and the adapter.

Run: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.antigravity.test_antigravity_one_shot
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.engines import antigravity as ag
from recursion_core.routes.engines.fixtures.antigravity import schema_check
from recursion_core.routes.launch import BASE_ENV, ReadTimeout

HERE = Path(__file__).resolve().parent
ONE_SHOT = HERE / "one_shot"
CONN = "api_key:conn-A"
MODEL = "gemini-3.6-flash"
FIXTURE_NAMES = sorted(p.name for p in ONE_SHOT.glob("*.jsonl"))


def lines(name: str) -> list[str]:
    return (ONE_SHOT / name).read_text(encoding="utf-8").splitlines()


def parse_all(name: str) -> tuple[ag.OneShotParser, list[dict]]:
    parser = ag.OneShotParser(CONN, MODEL)
    drafts = []
    for line in lines(name):
        drafts += parser.parse(line)
    return parser, drafts


def of_type(drafts: list[dict], kind: str) -> list[dict]:
    return [d["payload"] for d in drafts if d["type"] == kind]


class FixtureLabelTest(unittest.TestCase):
    def test_every_fixture_is_labelled_synthetic(self) -> None:
        self.assertTrue(FIXTURE_NAMES)
        for name in FIXTURE_NAMES:
            self.assertTrue(name.startswith("synthetic-"), name)
            label = json.loads(lines(name)[0])   # a line with no "event": the parser skips it, as agy would
            self.assertIs(label["synthetic"], True, name)
            self.assertNotIn("event", label)
        readme = (HERE / "README.md").read_text(encoding="utf-8")
        for name in FIXTURE_NAMES:
            self.assertIn(name, readme)


class ParserTest(unittest.TestCase):
    def test_every_line_is_an_engine_output_span_of_the_file(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                parser = ag.OneShotParser(CONN, MODEL)
                spans = []
                for line in lines(name):
                    try:
                        spans += of_type(parser.parse(line), "engine.output")
                    except ag.OneShotProtocolError:
                        break
                data = (ONE_SHOT / name).read_bytes()
                for span in spans:
                    self.assertEqual(data[span["offset_start"]:span["offset_end"]].decode("utf-8"), span["text"])
                    self.assertEqual(span["stream"], "stdout")
                self.assertEqual(spans[0]["offset_start"], 0)
                for one, two in zip(spans, spans[1:]):
                    self.assertEqual(one["offset_end"], two["offset_start"])

    def test_offsets_count_utf8_bytes_not_characters(self) -> None:
        line = json.dumps({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE",
                           "step_type": "agent_response", "text_delta": "caf\u00e9 \U0001f600"}}, ensure_ascii=False)
        parser = ag.OneShotParser(CONN, MODEL, offset=10)
        span = of_type(parser.parse(line), "engine.output")[0]
        self.assertEqual(span["offset_start"], 10)
        self.assertEqual(span["offset_end"], 10 + len(line.encode("utf-8")) + 1)
        self.assertNotEqual(len(line.encode("utf-8")), len(line))
        drafts = ag.AcpDrafts(offset=3)
        drafts.on_line(line)
        self.assertEqual(drafts.drafts[0]["payload"]["offset_end"], 3 + len(line.encode("utf-8")) + 1)

    def test_a_successful_run(self) -> None:
        parser, drafts = parse_all("synthetic-ok.jsonl")
        self.assertEqual(parser.conversation_id, "00000000-0000-4000-8000-00000000e5e5")
        self.assertEqual(of_type(drafts, "result"), [{"code": None, "detail": ""}])
        usage = of_type(drafts, "usage")
        self.assertEqual(len(usage), 1)   # the result's total only; per-step usage is not drafted
        u = usage[0]
        self.assertEqual((u["connection_id"], u["model"], u["bucket"], u["span"]), (CONN, MODEL, None, "turn"))
        self.assertEqual(u["input_tokens"], {"value": 1200, "quality": "known", "provenance": "observed"})
        self.assertEqual(u["output_tokens"]["value"], 40)
        self.assertEqual(u["reasoning_tokens"]["value"], 25)
        self.assertEqual(u["cache_read_tokens"]["value"], 800)
        for unknown in ("cache_write_tokens", "cost_usd"):
            self.assertEqual(u[unknown], {"value": None, "quality": "unknown", "provenance": "unavailable"})
        self.assertEqual(parser.code(), (None, ""))

    def test_a_tool_step_gives_one_call_and_one_result(self) -> None:
        _parser, drafts = parse_all("synthetic-tool.jsonl")
        calls, results = of_type(drafts, "tool.call"), of_type(drafts, "tool.result")
        self.assertEqual(len(calls), 1)   # the ACTIVE update of the same step gives nothing
        self.assertEqual(calls[0]["call_id"], "step-1")
        self.assertEqual(calls[0]["name"], "run_command")
        self.assertEqual(calls[0]["input"], {"CommandLine": "echo hi", "Cwd": "/var/tmp/workspace"})
        text = '{"CommandLine":"echo hi","Cwd":"/var/tmp/workspace"}'
        self.assertEqual(calls[0]["input_digest"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(results, [{"call_id": "step-1", "status": "ok", "text": "hi\r\n", "output_ref": None,
                                    "truncated": False}])
        kinds = [d["type"] for d in drafts]
        self.assertLess(kinds.index("tool.call"), kinds.index("tool.result"))

    def test_a_tool_step_is_drafted_once_and_only_when_done(self) -> None:
        def tool(state: str, output: str | None) -> str:
            info = {"name": "run_command", "parameters": {"CommandLine": "ls"}}
            if output is not None:
                info["output"] = output
            return json.dumps({"event": "step_update", "step_update": {
                "step_index": 4, "state": state, "step_type": "tool", "tool_name": "run_command", "tool_info": info}})

        parser = ag.OneShotParser(CONN, MODEL)
        self.assertEqual(of_type(parser.parse(tool("ACTIVE", None)), "tool.call"), [])   # tool_info, still running
        first = parser.parse(tool("DONE", "a\n"))
        again = parser.parse(tool("DONE", "a\n"))
        self.assertEqual(len(of_type(first, "tool.call")), 1)
        self.assertEqual(of_type(again, "tool.call") + of_type(again, "tool.result"), [])

    def test_a_failed_tool_step(self) -> None:
        _parser, drafts = parse_all("synthetic-tool-error.jsonl")
        self.assertEqual(of_type(drafts, "tool.result")[0]["status"], "error")
        self.assertEqual(of_type(drafts, "tool.result")[0]["text"], "the path is outside the workspace")

    def test_a_failed_run_is_an_engine_error_with_the_engines_text_in_the_detail(self) -> None:
        parser, drafts = parse_all("synthetic-error-unknown-model.jsonl")
        code, detail = parser.code()
        self.assertEqual(code, c.ENGINE_ERROR)
        self.assertTrue(detail.startswith("status ERROR: invalid model selection"))
        self.assertIsNone(parser.conversation_id)
        self.assertEqual(of_type(drafts, "usage")[0]["span"], "session_total")   # not one turn

    def test_quota_text_in_a_failed_run_is_a_short_fault_never_a_plan_limit(self) -> None:
        parser, drafts = parse_all("synthetic-error-quota.jsonl")
        self.assertEqual(parser.code()[0], c.SHORT_FAULT)
        self.assertEqual(of_type(drafts, "limit"), [])

    def test_quota_words_in_the_models_answer_change_nothing(self) -> None:
        parser, _drafts = parse_all("synthetic-model-text-sounds-like-quota.jsonl")
        self.assertEqual(parser.code(), (None, ""))

    def test_an_interrupted_run_is_cancelled(self) -> None:
        parser, _drafts = parse_all("synthetic-interrupted.jsonl")
        self.assertEqual(parser.code()[0], ag.CANCELLED)

    def test_unknown_events_and_fields_are_skipped(self) -> None:
        parser, drafts = parse_all("synthetic-unknown-event.jsonl")
        self.assertEqual(parser.code(), (None, ""))
        self.assertEqual(len(of_type(drafts, "engine.output")), 6)   # the label line, five events

    def test_a_stream_without_a_result_has_none(self) -> None:
        parser, drafts = parse_all("synthetic-no-result.jsonl")
        self.assertIsNone(parser.result)
        self.assertEqual(of_type(drafts, "result"), [])

    def test_protocol_errors(self) -> None:
        for bad in ("not json", "[1]", '{"event":"result","result":{"status":"DONE"}}'):
            with self.subTest(line=bad), self.assertRaises(ag.OneShotProtocolError):
                ag.OneShotParser(CONN, MODEL).parse(bad)
        parser, _ = parse_all("synthetic-ok.jsonl")
        with self.assertRaises(ag.OneShotProtocolError):
            parser.parse('{"event":"init"}')   # nothing comes after the result

    def test_thinking_larger_than_output_is_not_taken_as_its_reasoning_part(self) -> None:
        parser = ag.OneShotParser(CONN, MODEL)
        line = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "x", "num_turns": 1,
                           "usage": {"input_tokens": 5, "output_tokens": 3, "thinking_tokens": 9}}})
        usage = of_type(parser.parse(line), "usage")[0]
        self.assertEqual(usage["reasoning_tokens"]["quality"], "unknown")
        self.assertEqual(usage["cache_read_tokens"]["quality"], "unknown")   # absent is unknown, never 0

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_every_draft_fits_driver_v2(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                parser = ag.OneShotParser(CONN, MODEL)
                for line in lines(name):
                    for draft in parser.parse(line):
                        self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)


def tool_line(parameters=None, *, name="test_tool", omit=False, index=1) -> str:
    info = {"name": name, "output": "ok"}
    if not omit:
        info["parameters"] = parameters
    return json.dumps({"event": "step_update", "step_update": {"step_index": index, "step_type": "tool",
                                                               "state": "DONE", "tool_info": info}})


class ReviewRoundOneParserTest(unittest.TestCase):
    """Regressions for the two reads of #396 at e8c3a695 that concern the one-shot parser."""

    def test_r5_input_that_is_not_an_object_is_carried_as_null_with_its_own_digest(self) -> None:
        for value in (["synthetic", 1], "text", 7, None):
            with self.subTest(value=value):
                call = of_type(ag.OneShotParser(CONN, MODEL).parse(tool_line(value)), "tool.call")[0]
                self.assertIsNone(call["input"])
                self.assertEqual(call["input_digest"], ag.input_digest(value))
        call = of_type(ag.OneShotParser(CONN, MODEL).parse(tool_line(omit=True)), "tool.call")[0]
        self.assertIsNone(call["input"])
        self.assertEqual(call["input_digest"], ag.input_digest(None))
        self.assertNotEqual(call["input_digest"], ag.input_digest({}))

    def test_r6_parameters_with_no_canonical_text_are_a_protocol_error(self) -> None:
        with self.assertRaises(ag.OneShotProtocolError):
            ag.OneShotParser(CONN, MODEL).parse(tool_line({"large": 9007199254740992}))

    def test_f2_a_line_nested_too_deeply_is_a_protocol_error(self) -> None:
        with self.assertRaises(ag.OneShotProtocolError):
            ag.OneShotParser(CONN, MODEL).parse("[" * 100_000 + "]" * 100_000)

    def test_f3_a_line_that_is_not_utf8_is_a_protocol_error(self) -> None:
        with self.assertRaises(ag.OneShotProtocolError):
            ag.OneShotParser(CONN, MODEL).parse('{"event":"init","x":"\udcff"}')

    def test_a_tool_name_with_a_line_break_gives_no_draft(self) -> None:                     # M12
        for name in ("bad\nname", "bad\rname"):
            with self.subTest(name=name):
                drafts = ag.OneShotParser(CONN, MODEL).parse(tool_line({}, name=name))
                self.assertEqual(of_type(drafts, "tool.call"), [])

    def test_a_negative_token_count_is_unknown(self) -> None:                                # M13
        line = json.dumps({"event": "result", "result": {"status": "SUCCESS", "num_turns": 1,
                                                         "usage": {"input_tokens": -1, "output_tokens": True}}})
        usage = of_type(ag.OneShotParser(CONN, MODEL).parse(line), "usage")[0]
        unknown = {"value": None, "quality": "unknown", "provenance": "unavailable"}
        self.assertEqual(usage["input_tokens"], unknown)
        self.assertEqual(usage["output_tokens"], unknown)

    def test_the_canonical_text_refuses_more_than_sixty_four_levels(self) -> None:           # M14
        value = None
        for _ in range(64):
            value = [value]
        ag.canonical_text(value)
        with self.assertRaises(ag.NoCanonicalText):
            ag.canonical_text([value])

    def test_a_model_that_is_empty_or_looks_like_a_flag_is_refused(self) -> None:             # DeepSeek :125
        for model in ("", "-rf", "--yolo", None):
            with self.subTest(model=model), self.assertRaises(c.RouteError) as ctx:
                ag.one_shot_argv("agy", model)
            self.assertEqual(ctx.exception.code, c.REQUEST_INVALID)


class CanonicalTextTest(unittest.TestCase):
    """The RFC 8785 copy agrees with SPINE's rules on the cases its docstring names."""

    VALUES = (0, -7, 1.0, 0.5, 1e-7, -2.5e-9, 123456789.125, 2**53 - 1, "", "a\u0001b\"\\", [1, [2, {}]],
              {"b": 1, "a": None, "\u00e9": True, "\U0001f600": "x", "\uff01": 3, "B": [0.1]})

    def test_numbers_strings_and_key_order(self) -> None:
        self.assertEqual(ag.canonical_text(1.0), "1")
        self.assertEqual(ag.canonical_text(1e-7), "1e-7")
        self.assertEqual(ag.canonical_text(0.5), "0.5")
        self.assertEqual(ag.canonical_text(123456789.125), "123456789.125")
        with self.assertRaises(ag.NoCanonicalText):     # integral and beyond 2^53 - 1: no text, as SPINE's rule
            ag.canonical_text(1e21)
        # Keys sort by UTF-16 code units, not code points: U+1F600 (surrogates D83D DE00) before U+FF01.
        self.assertEqual(ag.canonical_text({"\uff01": 1, "\U0001f600": 2}), '{"\U0001f600":2,"\uff01":1}')
        self.assertEqual(ag.canonical_text({"b": 1, "a": [True, None, "x\n"]}), '{"a":[true,null,"x\\n"],"b":1}')
        self.assertEqual(ag.canonical_text({"\u00e9": 1, "z": 2}), '{"z":2,"\u00e9":1}')
        for bad in (2**53, float("nan"), "\ud800", {1: 2}):
            with self.subTest(bad=repr(bad)), self.assertRaises(ag.NoCanonicalText):
                ag.canonical_text(bad)

    def test_the_copy_writes_what_spines_function_writes(self) -> None:
        try:
            from recursion_core.control.methods import NoCanonicalText as SpineNoText
            from recursion_core.control.methods import canonical_text as spine
        except ImportError as err:  # pragma: no cover - SPINE's module is on this branch's base
            self.skipTest(f"control.methods: {err}")
        for value in self.VALUES + (1e21, 2**53, float("inf"), "\ud800"):
            with self.subTest(value=repr(value)):
                try:
                    want = spine(value)
                except SpineNoText:
                    with self.assertRaises(ag.NoCanonicalText):
                        ag.canonical_text(value)
                    continue
                self.assertEqual(ag.canonical_text(value), want)


# -- the adapter ------------------------------------------------------------------------------------------
class FakeProcess:
    def __init__(self, out_lines, clock) -> None:
        self.out, self.clock = list(out_lines), clock
        self.stdin: list = []
        self.stdin_closed = False
        self.fenced: list = []

    def send(self, line: str) -> None:
        self.stdin.append(line)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def read_line(self, timeout_s: float):
        if self.out:
            return self.out.pop(0)   # a None queued here is the end of the output
        self.clock.now += timeout_s
        raise ReadTimeout()

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Launcher:
    def __init__(self, out_lines=(), clock=None, *, eof=True) -> None:
        self.out_lines, self.clock, self.eof = list(out_lines), clock, eof
        self.calls: list = []
        self.process = None

    def __call__(self, argv, *, env, shell=False, **kwargs):
        self.calls.append({"argv": list(argv), "env": dict(env), "shell": shell})
        self.process = FakeProcess(self.out_lines, self.clock)
        if self.eof:
            self.process.out.append(None)
        return self.process


def request(method="gemini-api-key", kind="api_key", cred="GEMINI_API_KEY", model=MODEL) -> dict:
    connection = {"kind": kind, "id": "conn-A"}
    if cred is not None:
        connection["credential_env"] = cred
    return {"contract": c.CONTRACT_VERSION, "request_id": "r1", "tenant": "t1", "prompt": "say hi",
            "deadline_s": 60.0,
            "route": {"engine": "antigravity", "model": model, "connection": connection, "auth_method": method}}


PARENT = {"PATH": "/usr/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", "GEMINI_API_KEY": "parent-made-up",
          "GOOGLE_API_KEY": "parent-made-up", "GOOGLE_CLOUD_PROJECT": "p", "AGY_ADC_AUTH": "true"}
HOMES: list = []


def prepared(route, method_id) -> str:
    HOMES.append(method_id)
    return f"/var/tmp/route-homes/antigravity-{method_id}"


class FakeBoundary:
    """A stand-in for the launch side's output boundary: it records the values it was told to keep out of
    the capture and hands back the test's launcher. It persists nothing, so there is nothing to mask."""

    def __init__(self, launcher) -> None:
        self.launcher, self.asked = launcher, []

    def __call__(self, values):
        self.asked.append(tuple(values))
        return self.launcher


def adapter(launcher, clock, *, binary=None, home=prepared, boundary=None) -> ag.AntigravityOneShotAdapter:
    return ag.AntigravityOneShotAdapter(launcher, clock, lambda conn_id, name: "made-up-key-for-tests", PARENT,
                                        binary=binary, prepared_home=home,
                                        output_boundary=boundary if boundary is not None else FakeBoundary(launcher))


class AdapterTest(unittest.TestCase):
    def launcher(self, name: str | None = None, clock=None):
        clock = clock or Clock()
        out = lines(name) if name else []
        launcher = Launcher(out, clock)
        return launcher, clock

    def test_the_argv_and_the_one_stdin_line(self) -> None:
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["text"], "hello\n")
        self.assertEqual(launcher.calls[0]["argv"], ["agy", "-p", "--input-format", "stream-json", "--output-format",
                                                     "stream-json", "--model", MODEL, "--disable-slash-commands"])
        self.assertEqual(launcher.process.stdin, ['{"event":"user","message":{"content":"say hi"}}'])
        self.assertTrue(launcher.process.stdin_closed)
        self.assertEqual([e["kind"] for e in res["events"]], ["usage"])
        self.assertEqual(res["events"][0]["data"]["input_tokens"], 1200)

    def test_argv0_is_the_binary_option_when_given_and_agy_otherwise(self) -> None:
        for binary in (None, "/opt/antigravity/bin/agy"):
            with self.subTest(binary=binary):
                launcher, clock = self.launcher("synthetic-ok.jsonl")
                adapter(launcher, clock, binary=binary).run(request(), clock() + 60)
                self.assertEqual(launcher.calls[0]["argv"][0], binary or ag.DEFAULT_EXECUTABLE)

    def test_the_child_environment_has_the_methods_one_key_and_the_updater_off(self) -> None:
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        adapter(launcher, clock).run(request(), clock() + 60)
        env = launcher.calls[0]["env"]
        self.assertEqual(env["GEMINI_API_KEY"], "made-up-key-for-tests")   # custody's, never the parent's
        self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertEqual(env["HOME"], "/var/tmp/route-homes/antigravity-gemini-api-key")
        self.assertEqual(set(env) - set(BASE_ENV), {"GEMINI_API_KEY", "AGY_CLI_DISABLE_AUTO_UPDATE"})

    def test_a_sign_in_method_gets_no_key_from_the_parent(self) -> None:
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        adapter(launcher, clock).run(request("oauth-business", "subscription", None), clock() + 60)
        env = launcher.calls[0]["env"]
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "AGY_ADC_AUTH"):
            self.assertNotIn(name, env)

    def test_the_route_home_is_asked_for_exactly_the_routes_method(self) -> None:
        HOMES.clear()
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        adapter(launcher, clock).run(request("agent-platform", "api_key", "GOOGLE_API_KEY"), clock() + 60)
        self.assertEqual(HOMES, ["agent-platform"])
        self.assertEqual(launcher.calls[0]["env"]["GOOGLE_API_KEY"], "made-up-key-for-tests")

    def test_without_a_prepared_home_every_launch_is_refused_and_nothing_starts(self) -> None:
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        made = ag.one_shot_factory(launcher, clock, lambda *_: "made-up", PARENT)
        res = made.run(request(), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NOT_PREPARED))
        self.assertEqual(launcher.calls, [])

    def test_the_personal_account_refuses_before_anything_starts(self) -> None:
        launcher, clock = self.launcher("synthetic-ok.jsonl")
        res = adapter(launcher, clock).run(request("oauth-personal", "subscription", None), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.PERSONAL_ACCOUNT_OFF))
        self.assertEqual(launcher.calls, [])

    def test_a_route_naming_no_method_or_a_mismatched_one_never_starts(self) -> None:
        cases = [
            (request(None), c.REQUEST_INVALID),
            (request("terminal-login"), c.REQUEST_INVALID),
            (request("gemini-api-key", "subscription", None), c.REQUEST_INVALID),
            (request("gemini-api-key", "api_key", "GOOGLE_API_KEY"), c.LAUNCH_REFUSED),
            (request("agent-platform", "api_key", "GEMINI_API_KEY"), c.LAUNCH_REFUSED),
            (request("oauth-business", "api_key", "GEMINI_API_KEY"), c.REQUEST_INVALID),
        ]
        for req, code in cases:
            with self.subTest(route=req["route"]):
                launcher, clock = self.launcher("synthetic-ok.jsonl")
                res = adapter(launcher, clock).run(req, clock() + 60)
                self.assertEqual(res["code"], code)
                self.assertEqual(launcher.calls, [])

    def test_a_failed_run(self) -> None:
        for name, code in (("synthetic-error-unknown-model.jsonl", c.ENGINE_ERROR),
                           ("synthetic-error-quota.jsonl", c.SHORT_FAULT),
                           ("synthetic-interrupted.jsonl", c.ENGINE_ERROR)):
            with self.subTest(name=name):
                launcher, clock = self.launcher(name)
                res = adapter(launcher, clock).run(request(), clock() + 60)
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], code)
                self.assertEqual(res["text"], "")

    def test_output_ending_before_the_result_is_engine_exited(self) -> None:
        launcher, clock = self.launcher("synthetic-no-result.jsonl")
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["code"], c.ENGINE_EXITED)

    def test_the_deadline_fences_the_run(self) -> None:
        clock = Clock()
        launcher = Launcher(lines("synthetic-no-result.jsonl"), clock, eof=False)
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(launcher.process.fenced, ["deadline"])

    def test_a_line_that_does_not_parse_is_a_protocol_error_and_fences(self) -> None:
        clock = Clock()
        launcher = Launcher(["not json"], clock)
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["code"], c.PROTOCOL_ERROR)
        self.assertEqual(launcher.process.fenced, ["protocol_error"])

    def test_r6_f2_f3_an_unreadable_line_is_a_protocol_error_and_the_run_is_fenced(self) -> None:
        for line in (tool_line({"large": 9007199254740992}), "[" * 100_000 + "]" * 100_000,
                     '{"event":"init","x":"\udcff"}'):
            with self.subTest(line=line[:40]):
                clock = Clock()
                launcher = Launcher([line], clock)
                res = adapter(launcher, clock).run(request(), clock() + 60)
                self.assertEqual(res["code"], c.PROTOCOL_ERROR)
                self.assertEqual(launcher.process.fenced, ["protocol_error"])

    def test_f13_a_request_that_is_not_contract_shaped_is_refused_before_anything_starts(self) -> None:
        good = request()
        no_model = dict(good, route={k: v for k, v in good["route"].items() if k != "model"})
        no_conn_id = dict(good, route=dict(good["route"], connection={"kind": "api_key",
                                                                      "credential_env": "GEMINI_API_KEY"}))
        cases = ["not a request", {}, dict(good, request_id=""), dict(good, prompt=""), dict(good, route="a route"),
                 dict(good, route=dict(good["route"], connection="x")), no_model, no_conn_id]
        for req in cases:
            with self.subTest(req=str(req)[:60]):
                launcher, clock = self.launcher("synthetic-ok.jsonl")
                res = adapter(launcher, clock).run(req, clock() + 60)
                self.assertEqual(res["code"], c.REQUEST_INVALID)
                self.assertEqual(launcher.calls, [])

    def test_the_factory_takes_only_the_binary_option(self) -> None:
        clock = Clock()
        made = ag.one_shot_factory(Launcher(), clock, lambda *_: "", {}, binary="/opt/agy")
        self.assertEqual(made.executable, "/opt/agy")
        self.assertEqual(ag.one_shot_factory(Launcher(), clock, lambda *_: "", {}).executable, "agy")
        with self.assertRaises(TypeError):
            ag.one_shot_factory(Launcher(), clock, lambda *_: "", {}, home="/tmp/x")
        with self.assertRaises(ValueError):
            ag.one_shot_factory(Launcher(), clock, lambda *_: "", {}, binary="agy")


class ReviewRoundTwoParserTest(unittest.TestCase):
    """Regressions for the two reads of #396 at ca153c32 that concern the one-shot parser and adapter."""

    def test_med2_a_step_index_no_64_bit_count_gives_is_a_protocol_error(self) -> None:
        for index in (10**200, 2**63, -1):
            with self.subTest(index=index), self.assertRaises(ag.OneShotProtocolError):
                ag.OneShotParser(CONN, MODEL).parse(tool_line({}, index=index))
        call = of_type(ag.OneShotParser(CONN, MODEL).parse(tool_line({}, index=ag.MAX_STEP_INDEX)), "tool.call")[0]
        self.assertEqual(call["call_id"], f"step-{2**63 - 1}")
        self.assertLessEqual(len(call["call_id"]), 128)

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_med2_the_smallest_and_largest_step_index_fit_driver_v2(self) -> None:
        for index in (0, ag.MAX_STEP_INDEX):
            for draft in ag.OneShotParser(CONN, MODEL).parse(tool_line({}, index=index)):
                self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)

    def test_med2_through_the_adapter_the_run_is_fenced(self) -> None:
        clock = Clock()
        launcher = Launcher([tool_line({}, index=10**200)], clock)
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["code"], c.PROTOCOL_ERROR)
        self.assertEqual(launcher.process.fenced, ["protocol_error"])

    def test_low3_a_prepared_home_that_is_not_an_absolute_path_refuses_and_nothing_starts(self) -> None:
        for home in ("relative/home", "", None, 5):
            with self.subTest(home=home):
                clock = Clock()
                launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
                res = adapter(launcher, clock, home=lambda route, method, home=home: home).run(request(), clock() + 60)
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NOT_PREPARED))
                self.assertEqual(launcher.calls, [])


def step_line(step_index, *, present=True) -> str:
    step = {"step_type": "tool", "state": "DONE", "tool_info": {"name": "t", "parameters": {}, "output": "ok"}}
    if present:
        step["step_index"] = step_index
    return json.dumps({"event": "step_update", "step_update": step})


class ReviewRoundThreeParserTest(unittest.TestCase):
    """Regressions for the two reads of #396 at 6fe2b50d that concern the one-shot parser and adapter."""

    def test_ds_med3_a_prompt_that_is_not_utf8_is_refused_before_anything_starts(self) -> None:
        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        req = dict(request(), prompt="a" + chr(0xD800) + "b")
        res = adapter(launcher, clock).run(req, clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "prompt is not UTF-8"))
        self.assertEqual(launcher.calls, [])
        with self.assertRaises(c.RouteError) as ctx:          # the stdin helper keeps the same rule
            ag.one_shot_stdin("a" + chr(0xD800) + "b")
        self.assertEqual(ctx.exception.code, c.REQUEST_INVALID)
        self.assertEqual(ag.one_shot_stdin("caf" + chr(0xE9)), '{"event":"user","message":{"content":"caf' + chr(0xE9)
                         + '"}}')                             # other text is written as it is

    def test_ds_a_done_tool_step_whose_index_is_not_an_integer_is_a_protocol_error(self) -> None:
        for index in (True, 1.0, "1", None):
            with self.subTest(index=index), self.assertRaises(ag.OneShotProtocolError):
                ag.OneShotParser(CONN, MODEL).parse(step_line(index))
        with self.assertRaises(ag.OneShotProtocolError):
            ag.OneShotParser(CONN, MODEL).parse(step_line(None, present=False))
        clock = Clock()
        launcher = Launcher([step_line(1.0)], clock)
        res = adapter(launcher, clock).run(request(), clock() + 60)
        self.assertEqual(res["code"], c.PROTOCOL_ERROR)
        self.assertEqual(launcher.process.fenced, ["protocol_error"])
        active = json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE"}})
        self.assertEqual(of_type(ag.OneShotParser(CONN, MODEL).parse(active), "tool.call"), [])   # not DONE: skipped

    @unittest.skipUnless(schema_check.AVAILABLE, schema_check.SKIP_REASON)
    def test_a_refused_index_leaves_the_parser_able_to_draft_the_next_step(self) -> None:
        parser = ag.OneShotParser(CONN, MODEL)
        with self.assertRaises(ag.OneShotProtocolError):
            parser.parse(tool_line({}, index=2**63))
        drafts = parser.parse(tool_line({}, index=2**63 - 1))
        self.assertTrue(any(d["type"] == "tool.call" for d in drafts))
        for draft in drafts:
            self.assertTrue(schema_check.fits("driver.v2.json#/$defs/event_draft", draft), draft)

    def test_a_route_home_given_as_bytes_is_refused_before_launch(self) -> None:
        clock = Clock()
        launcher = Launcher([], clock)
        res = adapter(launcher, clock, home=lambda *args: b"/var/tmp/synthetic").run(request(), clock() + 10)
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(launcher.calls, [])


SYNTHETIC_KEY = "r4-made-up-custody-value-0e5"   # invented here; no real key, custody or auth file is read


def echoing_launcher(clock, calls, *, status="SUCCESS", echo=True, name="GEMINI_API_KEY"):
    """A launcher whose child echoes the key it was given in its answer, error text and a tool's output."""
    def launch(argv, *, env, shell=False, **kwargs):
        calls.append({"argv": list(argv), "env": dict(env), "shell": shell})
        said = env.get(name, "") if echo else "hello"
        tool = json.dumps({"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "tool", "state": "DONE",
            "tool_info": {"name": "t", "parameters": {"k": said}, "output": "out " + said}}})
        result = json.dumps({"event": "result", "result": {"status": status, "response": said, "error": said,
                                                           "num_turns": 1}})
        return FakeProcess([tool, result, None], clock)
    return launch


class ReviewRoundFourAdapterTest(unittest.TestCase):
    """Regressions for the three reads of #396 at b94c2cd1 that concern the one-shot adapter and parser
    (Astra E2, Sol R3, DeepSeek F4). Each fails on that head, except the guards marked as such."""

    def test_e2_a_key_route_without_an_output_boundary_is_refused_before_custody_home_or_launch(self) -> None:
        touched = []

        def must_not(*args, **kwargs):
            touched.append(args)
            raise AssertionError("reached without an output boundary")

        made = ag.AntigravityOneShotAdapter(must_not, Clock(), must_not, PARENT, prepared_home=must_not)
        for req in (request(), request("agent-platform", "api_key", "GOOGLE_API_KEY")):
            with self.subTest(method=req["route"]["auth_method"]):
                res = made.run(req, Clock()() + 60)
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.OUTPUT_UNPROTECTED))
                with self.assertRaises(c.RouteError) as ctx:
                    made.env(req)
                self.assertEqual(ctx.exception.detail, ag.OUTPUT_UNPROTECTED)
        self.assertEqual(touched, [])

    def test_e2_a_route_without_a_key_launches_without_a_boundary_with_exactly_its_names(self) -> None:  # guard
        for method, kind in (("oauth-business", "subscription"), ("agent-platform", "passthrough")):
            with self.subTest(method=method, kind=kind):
                clock, calls = Clock(), []

                def custody(*args):
                    raise AssertionError("a route without a key asked custody")

                made = ag.AntigravityOneShotAdapter(echoing_launcher(clock, calls, echo=False), clock, custody, PARENT,
                                                    prepared_home=prepared)
                res = made.run(request(method, kind, None), clock() + 60)
                self.assertEqual(res["code"], None)
                self.assertEqual(calls[0]["env"], {"PATH": "/usr/bin", "LANG": "C.UTF-8", "AGY_CLI_DISABLE_AUTO_UPDATE":
                                                   "true", "HOME": f"/var/tmp/route-homes/antigravity-{method}"})

    def test_r3_a_key_the_child_echoes_never_reaches_the_result_and_the_boundary_is_told_it_first(self) -> None:
        for status in ("SUCCESS", "ERROR"):
            with self.subTest(status=status):
                clock, calls = Clock(), []
                launch = echoing_launcher(clock, calls, status=status)
                boundary = FakeBoundary(launch)
                made = ag.AntigravityOneShotAdapter(launch, clock, lambda conn_id, name: SYNTHETIC_KEY, PARENT,
                                                    prepared_home=prepared, output_boundary=boundary)
                res = made.run(request(), clock() + 60)
                self.assertEqual(boundary.asked, [(SYNTHETIC_KEY,)])          # asked before the launch
                self.assertEqual(calls[0]["env"]["GEMINI_API_KEY"], SYNTHETIC_KEY)
                self.assertNotIn(SYNTHETIC_KEY, json.dumps(res))
                stars = "*" * len(SYNTHETIC_KEY)
                self.assertIn(stars, res["text"] if status == "SUCCESS" else res["detail"])

    def test_r3_a_parser_given_the_key_masks_every_draft_and_keeps_the_byte_offsets(self) -> None:
        for key in (SYNTHETIC_KEY, 'made"up' + chr(0xE9) + "key-0e5"):        # also its JSON-escaped forms
            with self.subTest(key=key):
                lines_ = [json.dumps({"event": "step_update", "step_update": {
                              "step_index": 1, "step_type": "tool", "state": "DONE",
                              "tool_info": {"name": "t", "parameters": {"k": key}, "output": key}}}, ensure_ascii=asc)
                          for asc in (True, False)]
                for line in lines_:
                    plain, masked = ag.OneShotParser(CONN, MODEL, offset=7), ag.OneShotParser(CONN, MODEL, offset=7,
                                                                                               redact=(key,))
                    want = of_type(plain.parse(line), "engine.output")[0]
                    drafts = masked.parse(line)
                    got = of_type(drafts, "engine.output")[0]
                    for form in ag.mask_forms((key,)):
                        self.assertNotIn(form, got["text"])
                    call = of_type(drafts, "tool.call")[0]
                    self.assertNotIn(key, json.dumps(call["input"], ensure_ascii=False))
                    self.assertNotIn(key, of_type(drafts, "tool.result")[0]["text"])
                    self.assertEqual((got["offset_start"], got["offset_end"]),
                                     (want["offset_start"], want["offset_end"]))
                    self.assertEqual(len(got["text"].encode("utf-8")), len(want["text"].encode("utf-8")))

    def test_r3_a_launch_refusal_that_names_the_key_is_masked(self) -> None:
        clock = Clock()

        def refusing(argv, *, env, shell=False, **kwargs):
            raise c.RouteError(c.LAUNCH_REFUSED, "the child environment refused " + env["GEMINI_API_KEY"])

        made = ag.AntigravityOneShotAdapter(refusing, clock, lambda *a: SYNTHETIC_KEY, PARENT, prepared_home=prepared,
                                            output_boundary=FakeBoundary(refusing))
        res = made.run(request(), clock() + 60)
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(res))

    def test_r3_a_line_the_mask_leaves_unreadable_fails_closed(self) -> None:
        with self.assertRaises(ag.OneShotProtocolError):           # the value stands where a number is
            ag.OneShotParser(CONN, MODEL, redact=("12345",)).parse(
                '{"event":"result","result":{"status":"SUCCESS","num_turns":12345}}')
        for bad in ("", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ag.mask_forms((bad,))

    def test_f4_a_route_home_or_key_with_a_nul_is_refused_before_launch(self) -> None:
        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        res = adapter(launcher, clock, home=lambda *a: "/var/tmp/a" + chr(0) + "b").run(
            request("oauth-business", "subscription", None), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NOT_PREPARED))
        made = ag.AntigravityOneShotAdapter(launcher, clock, lambda *a: "made" + chr(0) + "up", PARENT,
                                            prepared_home=prepared, output_boundary=FakeBoundary(launcher))
        res = made.run(request(), clock() + 60)
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertNotIn("made", res["detail"])
        self.assertEqual(launcher.calls, [])


class ReviewRoundFiveAdapterTest(unittest.TestCase):
    """DeepSeek r5 H1 (STRICT): the daemon's own HOME never reaches the child, by any name."""

    def run_with(self, home, parent=None):
        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        made = ag.AntigravityOneShotAdapter(launcher, clock, lambda *a: "made-up", parent or PARENT,
                                            prepared_home=lambda r, m: home)
        return made.run(request("oauth-business", "subscription", None), clock() + 60), launcher

    def test_h1_the_daemons_home_or_a_path_inside_it_is_refused_by_resolved_path(self) -> None:
        import os
        import tempfile
        own = PARENT["HOME"]
        names = [own, own + "/", own + "/./", own + "/sub/..", own + "/.config", "//home//operator"]
        for home in names:
            with self.subTest(home=home):
                res, launcher = self.run_with(home)
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.ROUTE_HOME_IS_DAEMON_HOME))
                self.assertEqual(launcher.calls, [])
        with tempfile.TemporaryDirectory() as tmp:                 # an alias: a symlink to the daemon's HOME
            real = os.path.join(tmp, "real-home")
            os.mkdir(real)
            alias = os.path.join(tmp, "alias")
            os.symlink(real, alias)
            res, launcher = self.run_with(alias, dict(PARENT, HOME=real))
            self.assertEqual(res["detail"], ag.ROUTE_HOME_IS_DAEMON_HOME)
            self.assertEqual(launcher.calls, [])

    def test_h1_a_route_home_elsewhere_still_launches(self) -> None:                      # guard
        for home in ("/var/tmp/route-homes/antigravity-oauth-business", PARENT["HOME"] + "2"):
            with self.subTest(home=home):
                res, launcher = self.run_with(home)
                self.assertIsNone(res["code"])
                self.assertEqual(launcher.calls[0]["env"]["HOME"], home)

    def test_h1_the_daemons_home_is_refused_before_custody_is_asked(self) -> None:       # Sol F2
        asked = []

        def custody(*args):
            asked.append(args)
            return "made-up"

        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        made = ag.AntigravityOneShotAdapter(launcher, clock, custody, PARENT, prepared_home=lambda r, m: PARENT["HOME"],
                                            output_boundary=FakeBoundary(launcher))
        res = made.run(request(), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.ROUTE_HOME_IS_DAEMON_HOME))
        self.assertEqual((asked, launcher.calls), ([], []))

    def test_h1_the_daemons_home_given_through_an_alias_is_resolved_too(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real-home")
            os.mkdir(real)
            alias = os.path.join(tmp, "alias")
            os.symlink(real, alias)
            for given in (alias, real + "/", alias + "/."):
                with self.subTest(given=given):
                    res, launcher = self.run_with(real, dict(PARENT, HOME=given))
                    self.assertEqual(res["detail"], ag.ROUTE_HOME_IS_DAEMON_HOME)
                    self.assertEqual(launcher.calls, [])

    def test_h1_a_daemon_whose_home_is_the_root_refuses_every_route_home(self) -> None:     # round 6
        for home in ("/", "/root", "/etc", "/var/tmp/route-homes/antigravity-oauth-business"):
            with self.subTest(home=home):
                res, launcher = self.run_with(home, dict(PARENT, HOME="/"))
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.ROUTE_HOME_IS_DAEMON_HOME))
                self.assertEqual(launcher.calls, [])


KEY5 = "r5-made-up-custody-value-2c9"   # invented here; no real key, custody or auth file is read
# A key with slashes (an escaped slash spells it) and one beyond ASCII and the BMP (a surrogate pair spells it).
KEYS5 = (KEY5, "r5/made/up-value", "r5-caf" + chr(0xE9) + "-" + chr(0x1F600) + "-value")


def u_escape(ch: str, digits: str = "%04x") -> str:
    data = ch.encode("utf-16-be", "surrogatepass")
    return "".join("\\u" + digits % int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))


def spellings(value: str) -> dict:
    """Valid JSON spellings of ``value`` that no serializer writes: the reads' witnesses and their relatives."""
    return {
        "first character escaped": u_escape(value[0]) + json.dumps(value[1:])[1:-1],
        "every character escaped": "".join(u_escape(ch) for ch in value),
        "upper-case hex": "".join(u_escape(ch, "%04X") for ch in value),
        "last character escaped": json.dumps(value[:-1])[1:-1] + u_escape(value[-1]),
        "slashes escaped": json.dumps(value)[1:-1].replace("/", "\\/"),
    }


TEMPLATES5 = (
    '{"event":"result","result":{"status":"SUCCESS","response":"said SPELLED","num_turns":1}}',
    '{"event":"result","result":{"status":"ERROR","error":"said SPELLED"}}',
    '{"event":"step_update","step_update":{"step_index":1,"step_type":"tool","state":"DONE",'
    '"tool_info":{"name":"t","parameters":{"k":"x SPELLED y","SPELLED":1},"output":"SPELLED"}}}',
)


def holds(value, key: str) -> bool:
    return key in json.dumps(value, ensure_ascii=False)


def unwrap(value):
    """``value`` with every string that is JSON text read once more (and a JSON escape in a string read)."""
    if isinstance(value, dict):
        return {unwrap(k): unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            try:
                return json.loads('"' + value + '"')
            except ValueError:
                return value
    return value


class ReviewRoundFiveMaskTest(unittest.TestCase):
    """Regressions for the round-5 reads of #396 at 69a1b194 that concern the mask (Astra 1 and Sol F1,
    STRICT; DeepSeek L2) and the one-shot helpers (DeepSeek O2, O3). Each fails on that head."""

    def test_r5_every_json_spelling_of_the_key_is_masked_in_every_draft_with_its_offsets(self) -> None:
        for key in KEYS5:
            for name, spelled in spellings(key).items():
                for template in TEMPLATES5:
                    line = template.replace("SPELLED", spelled)
                    with self.subTest(key=key.encode("unicode_escape"), spelling=name, line=template[:40]):
                        self.assertTrue(holds(json.loads(line), key))        # the spelling is the key
                        plain = ag.OneShotParser(CONN, MODEL, offset=5)
                        parser = ag.OneShotParser(CONN, MODEL, offset=5, redact=(key,))
                        want = of_type(plain.parse(line), "engine.output")[0]
                        drafts = parser.parse(line)
                        for draft in drafts:
                            if draft["type"] == "engine.output":
                                got = draft["payload"]
                                self.assertFalse(holds(json.loads(got["text"]), key))   # still JSON, no key
                                self.assertEqual((got["offset_start"], got["offset_end"]),
                                                 (want["offset_start"], want["offset_end"]))
                            else:
                                self.assertFalse(holds(draft, key), draft["type"])
                        self.assertFalse(holds(parser.code(), key))
                        self.assertFalse(holds(parser.result, key))

    def test_r5_a_decoded_spelling_never_reaches_the_result_through_either_boundary(self) -> None:
        for status in ("SUCCESS", "ERROR"):
            for masking in (False, True):
                with self.subTest(status=status, capture_masks=masking):
                    clock, calls, captured = Clock(), [], []

                    def launch(argv, *, env, shell=False, **kwargs):
                        calls.append(dict(env))
                        said = spellings(env["GEMINI_API_KEY"])["first character escaped"]
                        out = [template.replace("SPELLED", said) for template in TEMPLATES5[2:]]
                        out.append(TEMPLATES5[0 if status == "SUCCESS" else 1].replace("SPELLED", said))
                        return FakeProcess(out + [None], clock)

                    def capturing(values):
                        def masked_launch(argv, *, env, shell=False, **kwargs):
                            process = launch(argv, env=env, shell=shell)
                            # the documented capture: each line masked before it is kept or read
                            process.out = [None if line is None else ag.mask(line, ag.mask_forms(values))
                                           for line in process.out]
                            captured.extend(line for line in process.out if line is not None)
                            return process
                        return masked_launch

                    boundary = capturing if masking else FakeBoundary(launch)
                    made = ag.AntigravityOneShotAdapter(launch, clock, lambda *a: KEY5, PARENT, prepared_home=prepared,
                                                        output_boundary=boundary)
                    res = made.run(request(), clock() + 60)
                    self.assertEqual(calls[0]["GEMINI_API_KEY"], KEY5)
                    self.assertFalse(holds(res, KEY5))
                    self.assertEqual(res["code"], None if status == "SUCCESS" else c.ENGINE_ERROR)
                    for line in captured:
                        self.assertFalse(holds(json.loads(line), KEY5))

    def test_r5_a_masked_line_keeps_its_byte_length_and_stays_json(self) -> None:
        for key in KEYS5:
            forms = ag.mask_forms((key,))
            for spelled in spellings(key).values():
                for template in TEMPLATES5:
                    line = template.replace("SPELLED", spelled)
                    masked = ag.mask(line, forms)
                    self.assertEqual(len(masked.encode("utf-8")), len(line.encode("utf-8")))
                    self.assertFalse(holds(json.loads(masked), key))
                    self.assertEqual(ag.mask(masked, forms), masked)              # masking again changes nothing

    def test_r5_a_number_that_spells_the_key_fails_closed(self) -> None:
        for number in ("1.2345e4", "12345.0", "123450e-1", "[0, 1.2345e4]", "{\"a\": [1.2345E+4]}"):
            with self.subTest(number=number), self.assertRaises(ag.OneShotProtocolError):
                ag.OneShotParser(CONN, MODEL, redact=("12345",)).parse(
                    '{"event":"result","result":{"status":"SUCCESS","num_turns":' + number + '}}')
        drafts = ag.OneShotParser(CONN, MODEL, redact=("12345",)).parse(            # guard: other numbers read
            '{"event":"result","result":{"status":"SUCCESS","num_turns":1}}')
        self.assertEqual(of_type(drafts, "result"), [{"code": None, "detail": ""}])

    def test_l2_a_key_made_only_of_asterisks_is_masked(self) -> None:
        for key in ("*", "**", "***"):
            with self.subTest(key=key):
                line = '{"event":"result","result":{"status":"ERROR","error":"the key KEY"}}'.replace("KEY", key)
                parser = ag.OneShotParser(CONN, MODEL, redact=(key,))
                drafts = parser.parse(line)
                self.assertNotIn(key, of_type(drafts, "engine.output")[0]["text"])
                self.assertNotIn(key, parser.code()[1])
                self.assertEqual(len(of_type(drafts, "engine.output")[0]["text"]), len(line) + 1)
                acp_drafts = ag.AcpDrafts(redact=(key,))
                acp_drafts.on_line(line)
                self.assertNotIn(key, acp_drafts.drafts[0]["payload"]["text"])
        line = '{"event":"result","result":{"status":"ERROR","error":"the key a*b"}}'   # guard: not uniform
        self.assertEqual(ag.OneShotParser(CONN, MODEL, redact=("a*b",)).parse(line)[-1]["payload"]["detail"],
                         "status ERROR: the key ***")

    def test_r5_masking_repeats_until_no_spelling_is_left(self) -> None:
        # masking "b" turns "ab" into "a*", which spells the other value: a second round masks that too
        forms = ag.mask_forms(("a*", "b"))
        self.assertEqual(ag.mask("x ab y", forms), "x ** y")
        self.assertEqual(ag.mask("x a\\u0062 y", forms), "x ******* y")

    def test_r5_json_written_inside_a_json_string_is_read_to_any_depth(self) -> None:
        for key in KEYS5:
            for depth in (1, 2, 3):
                inner = {"k": key}
                for _ in range(depth):
                    inner = json.dumps(inner)       # JSON text carried in a string, depth times over
                for spelled in (inner, inner.replace(json.dumps(key[0])[1:-1], u_escape(key[0]), 1)):
                    line = json.dumps({"event": "step_update", "step_update": {
                        "step_index": 1, "step_type": "tool", "state": "DONE",
                        "tool_info": {"name": "t", "parameters": {"p": spelled}, "output": spelled}}})
                    with self.subTest(key=key.encode("unicode_escape"), depth=depth, line=line[:60]):
                        drafts = ag.OneShotParser(CONN, MODEL, redact=(key,)).parse(line)
                        for draft in drafts:
                            value = draft
                            for _ in range(depth + 2):          # read every string as JSON as far as it goes
                                self.assertFalse(holds(value, key), draft["type"])
                                value = unwrap(value)

    def test_r5_a_boundary_refusal_that_names_the_key_is_masked(self) -> None:
        clock = Clock()

        def refusing(values):
            raise c.RouteError(c.LAUNCH_REFUSED, "no capture for " + spellings(values[0])["every character escaped"]
                               + " or " + values[0])

        made = ag.AntigravityOneShotAdapter(Launcher([], clock), clock, lambda *a: KEY5, PARENT,
                                            prepared_home=prepared, output_boundary=refusing)
        res = made.run(request(), clock() + 60)
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertFalse(holds(res, KEY5))
        self.assertFalse(holds(unwrap(res), KEY5))

    def test_r5_a_round_that_changes_nothing_keeps_nothing(self) -> None:
        from unittest import mock
        line = '{"event":"result","result":{"status":"ERROR","error":"the key *"}}'
        with mock.patch.object(ag, "_fill", lambda forms: "*"):     # a fill the value is made of
            masked = ag.mask(line, ("*",))
        self.assertEqual(masked, "*" * len(line))

    def test_o2_one_shot_stdin_refuses_a_prompt_that_is_not_a_str(self) -> None:
        for prompt in (5, None, b"say hi", ["say hi"]):
            with self.subTest(prompt=prompt), self.assertRaises(c.RouteError) as ctx:
                ag.one_shot_stdin(prompt)
            self.assertEqual(ctx.exception.code, c.REQUEST_INVALID)

    def test_o3_an_oserror_from_the_route_home_loader_or_the_boundary_is_a_refused_launch(self) -> None:
        def failing(*args):
            raise OSError("synthetic")

        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        made = ag.AntigravityOneShotAdapter(launcher, clock, lambda *a: KEY5, PARENT, prepared_home=failing,
                                            output_boundary=FakeBoundary(launcher))
        res = made.run(request(), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.NOT_PREPARED))
        made = ag.AntigravityOneShotAdapter(launcher, clock, lambda *a: KEY5, PARENT, prepared_home=prepared,
                                            output_boundary=failing)
        res = made.run(request(), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.OUTPUT_UNPROTECTED))
        self.assertEqual(launcher.calls, [])

    def test_f2_ended_ids_at_their_length_limits_and_with_other_characters(self) -> None:   # Astra's follow-up
        from recursion_core.routes.engines.fixtures.antigravity import acp
        out = acp.TurnOutcome(1, None, "end_turn", False, False, None, None, None, "", "hi", 0)
        for turn, delivery in (("turn-" + "a" * 60, "dlv-x"), ("turn-x", "dlv-" + "a" * 61),
                               ("turn-" + chr(0xE9), "dlv-x"), ("turn-x", "dlv-" + chr(10)), ("turn-x", "dlv-a b"),
                               ("turn-x", "dlv-x" + chr(10))):
            with self.subTest(turn=turn, delivery=delivery):
                drafts = ag.AcpDrafts()
                with self.assertRaises(TypeError):
                    drafts.ended(out, turn, delivery)
                self.assertEqual(drafts.drafts, [])
        self.assertEqual(len(ag.AcpDrafts().ended(out, "turn-" + "a" * 59, "dlv-" + "a" * 60)), 2)


class ReviewRoundSixTest(unittest.TestCase):
    """Regressions for the round-6 reads of #396 at c44c0b49 (Sol R6-1; DeepSeek MED, MED STRICT, LOW F3, LOW
    F4). Each fails on that head."""

    def test_r6_a_line_built_to_peel_one_escape_layer_at_a_time_costs_bounded_work(self) -> None:
        from unittest import mock
        key = "r6-made-up-custody-value-742c"
        line = json.dumps({"event": "result", "result": {"status": "SUCCESS",
                                                         "response": chr(92) + "u005c" * 3200 + "u0041"}})
        calls = []
        real = ag._decode
        with mock.patch.object(ag, "_decode", lambda text: calls.append(len(text)) or real(text)):
            masked = ag.mask(line, ag.mask_forms((key,)))
        self.assertLessEqual(len(calls), ag.MASK_DEPTH * ag.MASK_ROUNDS)
        self.assertLessEqual(len(calls), ag.MASK_DEPTH + 1)          # one round: the layers are never all read
        self.assertEqual(masked, "*" * len(line.encode("utf-8")))  # kept nothing: fails closed
        with self.assertRaises(ag.OneShotProtocolError):
            ag.OneShotParser(CONN, MODEL, redact=(key,)).parse(line)

    def test_r6_masking_stops_after_its_rounds_and_keeps_nothing(self) -> None:
        from unittest import mock
        # each round masks one more "a*" (the fill completes it): 21 rounds would be needed; MASK_ROUNDS run
        text = "a" * 20 + "b" + chr(92) + "n"
        calls = []
        real = ag._decode
        with mock.patch.object(ag, "_decode", lambda s: calls.append(1) or real(s)):
            masked = ag.mask(text, ag.mask_forms(("a*", "b")))
        self.assertLessEqual(len(calls), ag.MASK_ROUNDS)
        self.assertEqual(masked, "*" * len(text))

    def test_r6_nesting_a_serializer_writes_is_still_read_and_masked(self) -> None:   # guard
        key = "r6-made-up-custody-value-742c"
        inner = {"k": key}
        for _ in range(4):
            inner = json.dumps(inner)
        line = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": inner}})
        masked = ag.mask(line, ag.mask_forms((key,)))
        self.assertNotIn(key, json.dumps(json.loads(masked), ensure_ascii=False))
        value = json.loads(masked)["result"]["response"]
        for _ in range(4):
            value = json.loads(value)
        self.assertEqual(set(value), {"k"})
        self.assertNotIn(key, value["k"])

    def test_r6_a_run_whose_line_is_read_past_the_deadline_never_returns_success(self) -> None:
        clock = Clock()
        deadline = clock() + 10

        class Slow(FakeProcess):
            def read_line(self, timeout_s):
                line = super().read_line(timeout_s)
                clock.now = deadline + 1          # the line's reading and masking took the rest of the time
                return line

        made = ag.AntigravityOneShotAdapter(None, clock, lambda *a: "made-up", PARENT, prepared_home=prepared,
                                            output_boundary=lambda values: lambda *a, **k: process)
        process = Slow([json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "hi"}})], clock)
        res = made.run(request(), deadline)
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(process.fenced, ["deadline"])

    def test_r6_a_parent_environment_with_no_usable_home_refuses_before_custody(self) -> None:
        asked = []
        parents = {"no HOME": {k: v for k, v in PARENT.items() if k != "HOME"}, "empty": dict(PARENT, HOME=""),
                   "relative": dict(PARENT, HOME="home/operator"), "a NUL": dict(PARENT, HOME="/home/op" + chr(0)),
                   "not a str": dict(PARENT, HOME=b"/home/operator")}
        for name, parent in parents.items():
            with self.subTest(parent=name):
                clock = Clock()
                launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
                made = ag.AntigravityOneShotAdapter(launcher, clock, lambda *a: asked.append(a) or "made-up", parent,
                                                    prepared_home=prepared, output_boundary=FakeBoundary(launcher))
                res = made.run(request(), clock() + 60)
                self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.DAEMON_HOME_UNKNOWN))
                self.assertEqual(launcher.calls, [])
        self.assertEqual(asked, [])

    def test_r6_a_custody_callback_that_raises_oserror_is_a_refused_launch(self) -> None:
        def custody(*args):
            raise OSError("synthetic: the keychain is locked")

        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        made = ag.AntigravityOneShotAdapter(launcher, clock, custody, PARENT, prepared_home=prepared,
                                            output_boundary=FakeBoundary(launcher))
        res = made.run(request(), clock() + 60)
        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, ag.CREDENTIAL_UNAVAILABLE))
        self.assertEqual(launcher.calls, [])


class ReviewRoundSevenTest(unittest.TestCase):
    """Regressions for DeepSeek's round-7 confirming read of #396 at 4205e522 (F1, F2 MEDIUM; F3 LOW). Each
    fails on that head."""

    def adapter_and_launcher(self, clock=None):
        clock = clock or Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        return adapter(launcher, clock), launcher, clock

    def test_f1_a_request_the_contract_refuses_is_refused_before_anything_starts(self) -> None:
        cases = {"deadline_s": [None, -1, 0, "x", True, float("nan"), float("inf"), 10**400],
                 "contract": [None, "recursion.route/v9"], "tenant": [None, "", 5]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    made, launcher, clock = self.adapter_and_launcher()
                    req = request()
                    if value is None:
                        del req[field]
                    else:
                        req[field] = value
                    res = made.run(req, clock() + 60)
                    self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, field))
                    self.assertEqual(launcher.calls, [])
        made, launcher, clock = self.adapter_and_launcher()
        req = request()
        req["route"]["connection"]["id"] = ""
        self.assertEqual(made.run(req, clock() + 60)["detail"], "route.connection")
        self.assertEqual(launcher.calls, [])
        made, launcher, clock = self.adapter_and_launcher()                     # guard: the shape the contract has
        self.assertIsNone(made.run(request(), clock() + 60)["code"])

    def test_f2_a_deadline_that_is_not_a_finite_number_is_refused_before_the_launch(self) -> None:
        for bad in (None, "x", float("nan"), float("inf"), True, 10**400):
            with self.subTest(deadline_at=repr(bad)):
                made, launcher, _clock = self.adapter_and_launcher()
                res = made.run(request(), bad)
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "deadline_at"))
                self.assertEqual(launcher.calls, [])

    def test_f2_whatever_escapes_after_the_launch_fences_the_child_first(self) -> None:
        for error in (ValueError, KeyboardInterrupt):
            with self.subTest(error=error.__name__):
                clock = Clock()
                launcher = Launcher(lines("synthetic-ok.jsonl"), clock, eof=False)
                made = adapter(launcher, clock)

                def failing_clock(err=error):
                    raise err("synthetic: a clock that fails once the child runs")

                made._clock = failing_clock
                with self.assertRaises(error):
                    made.run(request(), clock() + 60)
                self.assertEqual(len(launcher.calls), 1)
                self.assertEqual(launcher.process.fenced, ["unexpected"])

    def test_f3_a_values_normalization_variant_is_masked(self) -> None:
        import unicodedata
        key = "r7-caf" + chr(0xE9) + "-made-up"                      # composed (NFC) in custody
        decomposed = unicodedata.normalize("NFD", key)
        self.assertNotEqual(key, decomposed)
        for held, spelled in ((key, decomposed), (decomposed, key)):
            with self.subTest(held=held.encode("unicode_escape")):
                line = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": spelled}},
                                  ensure_ascii=False)
                parser = ag.OneShotParser(CONN, MODEL, redact=(held,))
                drafts = parser.parse(line)
                for value in (key, decomposed):
                    self.assertNotIn(value, json.dumps(drafts, ensure_ascii=False))
                    self.assertNotIn(value, parser.result["response"])


class ReviewRoundEight396Test(unittest.TestCase):
    """DeepSeek's round-8 LOW on #396 (the NFKC and NFKD spellings of a value): the code is left as it is and the
    text says exactly what the mask reads. These pin the text and the reach it describes."""

    def test_the_mask_reads_canonical_spellings_and_says_it_does_not_read_compatibility_ones(self) -> None:
        self.assertEqual(set(ag.mask_forms(("x" + chr(0xB2) + "y",))), {"x" + chr(0xB2) + "y"})    # no NFKC "x2y"
        self.assertEqual(ag.mask(chr(0xFF4B) + "ey", ag.mask_forms(("key",))), chr(0xFF4B) + "ey")  # fullwidth k
        self.assertEqual(set(ag.mask_forms(("A" + chr(0x30A),))), {"A" + chr(0x30A), chr(0xC5)})   # NFC, NFD
        doc = " ".join(ag.mask.__doc__.split())
        self.assertIn("Its compatibility spellings (NFKC, NFKD", doc)
        import pathlib

        here = pathlib.Path(ag.__file__).parent / "fixtures" / "antigravity"
        readme = (here / "README.md").read_text(encoding="utf-8")
        self.assertIn("never its compatibility spellings (NFKC, NFKD)", " ".join(readme.split()))


class _TwoFaced(dict):
    """``get`` answers the check; the stored value is what ``__getitem__`` and ``items`` return."""

    def get(self, key, default=None):
        if key == "request_id":
            return "r-checked"
        if key == "prompt":
            return "prompt-the-check-is-shown"
        return super().get(key, default)


class ReviewRoundFour411OneShotTest(unittest.TestCase):
    """DeepSeek's round-4 F1 and F2 on the one-shot adapter. A refusal detail must stay writable, and the
    request is read once so the id that is echoed is the id that was checked."""

    def test_f1_a_surrogate_in_the_connection_id_never_makes_a_refusal_unwritable(self) -> None:
        bad = "conn-" + chr(0xD800)
        for key in ("", "made-up\x00key"):
            with self.subTest(key=repr(key)):
                clock = Clock()
                launcher = Launcher([], clock)

                def custody(_conn_id, _name, key=key):
                    return key

                made = ag.AntigravityOneShotAdapter(launcher, clock, custody, PARENT, prepared_home=prepared,
                                                    output_boundary=FakeBoundary(launcher))
                req = request()
                req["route"]["connection"]["id"] = bad
                res = made.run(req, clock() + 60)
                json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertNotIn(chr(0xD800), res["detail"])
                self.assertEqual(launcher.calls, [])

    def test_f2_the_one_shot_request_is_read_once(self) -> None:
        clock = Clock()
        launcher = Launcher(lines("synthetic-ok.jsonl"), clock)
        made = adapter(launcher, clock)
        stored = request()
        stored["request_id"] = "r-stored-" + chr(0xD800)
        stored["prompt"] = "a prompt the check never saw"
        res = made.run(_TwoFaced(stored), clock() + 60)
        json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.assertEqual(res["request_id"], "")
        self.assertEqual(res["detail"], "request_id")
        self.assertEqual(launcher.calls, [])
        self.assertNotIn("a prompt the check never saw", json.dumps(res))


if __name__ == "__main__":
    unittest.main()
