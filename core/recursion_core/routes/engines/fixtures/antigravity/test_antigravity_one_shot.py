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


def adapter(launcher, clock, *, binary=None, home=prepared) -> ag.AntigravityOneShotAdapter:
    return ag.AntigravityOneShotAdapter(launcher, clock, lambda conn_id, name: "made-up-key-for-tests", PARENT,
                                        binary=binary, prepared_home=home)


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


if __name__ == "__main__":
    unittest.main()
