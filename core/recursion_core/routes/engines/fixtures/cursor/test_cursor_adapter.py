# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The one-shot driver (routes/engines/cursor.py, ``CursorAdapter``) against the fake ``agent``: argv, the
child's environment, the route result for each stream, the billing check, the deadline. No process runs."""

from __future__ import annotations

import hashlib
import json
import unittest
from collections.abc import Mapping

from recursion_core.routes import contract as c
from recursion_core.routes.engines import cursor
from recursion_core.routes.engines.fixtures.cursor import fake_agent
from recursion_core.routes.launch import ReadTimeout

KEY = "made-up-cursor-key-0000"
PARENT = {"PATH": "/usr/bin", "HOME": "/home/someone", "LANG": "C.UTF-8", "CURSOR_API_KEY": "made-up-parent-key",
          "CURSOR_AUTH_TOKEN": "made-up-parent-token", "OPENAI_API_KEY": "made-up-other", "UNLISTED": "x"}
SID = "c0ffee00-0000-4000-8000-0000000000bb"


def request(prompt="Say hello in one sentence.", kind="subscription", model="synthetic-model", **conn):
    connection = {"kind": kind, "id": "cursor-connection", **conn}
    if kind == "api_key":
        connection.setdefault("credential_env", "CURSOR_API_KEY")
    return {"contract": c.CONTRACT_VERSION, "request_id": "r1", "tenant": "t1", "prompt": prompt, "deadline_s": 60.0,
            "route": {"engine": "cursor", "model": model, "connection": connection}}


def credentials(connection_id, env_name):
    if env_name != "CURSOR_API_KEY":
        raise KeyError(env_name)
    return KEY


class LinesLauncher:
    """A launcher whose process gives ``lines`` (or raises ReadTimeout after them, if ``hang``)."""

    def __init__(self, lines, hang=False):
        self.lines, self.hang, self.launches, self.processes = list(lines), hang, [], []

    def __call__(self, argv, *, env, shell=False, **options):
        self.launches.append({"argv": list(argv), "env": dict(env), "shell": shell})
        process = fake_agent.OneShotProcess(self.lines)
        if self.hang:
            read = process.read_line

            def read_line(timeout_s):
                text = read(timeout_s)
                if text is None:
                    raise ReadTimeout()
                return text
            process.read_line = read_line
        self.processes.append(process)
        return process


def line(**fields):
    fields.setdefault("session_id", SID)
    return json.dumps(fields)


def init(source="login"):
    """A system/init line: since the first break-it read of #412 (F5) a run with no init is refused at its
    result, so a test whose run must succeed gives one."""
    return line(type="system", subtype="init", apiKeySource=source, model="m")


def adapter(launcher, clock=lambda: 0.0, binary=None, parent=PARENT):
    return cursor.CursorAdapter(launcher, clock, credentials, parent, binary=binary)


class RunTest(unittest.TestCase):
    def run_fixture(self, fixture, req, **kwargs):
        launcher = fake_agent.FakeLauncher(fixture)
        return adapter(launcher, **kwargs).run(req, 10.0), launcher

    def test_a_sign_in_run_gives_the_answer(self):
        result, launcher = self.run_fixture("oneshot/hello.jsonl", request())
        self.assertEqual((result["status"], result["code"], result["text"]), ("ok", None, "Hello! Nice to meet you."))
        argv = json.loads((fake_agent.HERE / "argv.json").read_text(encoding="utf-8"))["oneshot"]["argv"]
        self.assertEqual(launcher.launches[0]["argv"], [*argv, "--", "Say hello in one sentence."])
        self.assertFalse(launcher.launches[0]["shell"])
        process = launcher.processes[0]
        self.assertTrue(process.stdin_closed)
        self.assertEqual((process.sent, process.fenced), ([], []))

    def test_a_sign_in_child_gets_no_cursor_credential_and_nothing_unlisted(self):
        _, launcher = self.run_fixture("oneshot/hello.jsonl", request())
        self.assertEqual(launcher.launches[0]["env"], {"PATH": "/usr/bin", "HOME": "/home/someone", "LANG": "C.UTF-8"})

    def test_an_api_key_child_gets_the_key_from_custody_and_never_in_argv(self):
        result, launcher = self.run_fixture("oneshot/tools.jsonl", request(
            "Read README.md and write NOTES.md with a summary.", kind="api_key"))
        self.assertEqual(result["code"], None)
        env = launcher.launches[0]["env"]
        self.assertEqual(env["CURSOR_API_KEY"], KEY)
        self.assertNotIn("CURSOR_AUTH_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertFalse(any(KEY in arg for arg in launcher.launches[0]["argv"]))
        cursor.check_argv(launcher.launches[0]["argv"])

    def test_every_launch_disables_the_updater_among_its_options(self):
        for fixture, req in (("oneshot/hello.jsonl", request()), ("oneshot/tools.jsonl", request(kind="api_key"))):
            _, launcher = self.run_fixture(fixture, req)
            argv = launcher.launches[0]["argv"]
            self.assertIn("--disable-auto-update", argv[1:argv.index("--")])

    def test_a_prompt_that_looks_like_an_option_is_only_the_prompt(self):
        for prompt in ("-p", "--api-key=made-up", "-HAuthorization: x", "--disable-auto-update", "--"):
            launcher = LinesLauncher([init(), line(type="result", subtype="success", is_error=False, result="ok")])
            result = adapter(launcher).run(request(prompt), 10.0)
            self.assertEqual(result["code"], None, prompt)
            self.assertEqual(launcher.launches[0]["argv"][-2:], ["--", prompt])
            cursor.check_argv(launcher.launches[0]["argv"])

    def test_the_pinned_binary_is_argv0(self):
        _, launcher = self.run_fixture("oneshot/hello.jsonl", request(), binary="/opt/cursor/bin/agent")
        self.assertEqual(launcher.launches[0]["argv"][0], "/opt/cursor/bin/agent")
        cursor.check_argv(launcher.launches[0]["argv"], binary="/opt/cursor/bin/agent")

    def test_a_binary_that_is_not_an_absolute_path_fails_construction(self):
        for binary in ("agent", "./agent", "", 7):
            with self.assertRaises(ValueError):
                adapter(fake_agent.FakeLauncher("oneshot/hello.jsonl"), binary=binary)

    def test_the_class_is_its_own_factory_with_todays_call_shape(self):
        made = cursor.CursorAdapter(fake_agent.FakeLauncher("oneshot/hello.jsonl"), lambda: 0.0, credentials, PARENT,
                                    **{"binary": "/opt/cursor/bin/agent"})
        self.assertEqual(made.engine, "cursor")
        self.assertEqual(made.declaration(), cursor.oneshot_declaration())


class RefusedBeforeLaunchTest(unittest.TestCase):
    def refused(self, req, code, detail_part):
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        result = adapter(launcher).run(req, 10.0)
        self.assertEqual((result["code"], launcher.launches), (code, []))
        self.assertIn(detail_part, result["detail"])

    def test_an_api_key_route_selects_only_cursors_key_name(self):
        self.refused(request(kind="api_key", credential_env="OPENAI_API_KEY"), c.LAUNCH_REFUSED, "not allowed")
        self.refused(request(kind="api_key", credential_env="CURSOR_AUTH_TOKEN"), c.LAUNCH_REFUSED, "not allowed")

    def test_an_api_key_route_with_no_key_in_custody_is_refused(self):
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        made = cursor.CursorAdapter(launcher, lambda: 0.0, lambda conn, name: "", PARENT)
        result = made.run(request(kind="api_key"), 10.0)
        self.assertEqual((result["code"], launcher.launches), (c.LAUNCH_REFUSED, []))

    def test_a_passthrough_route_is_refused(self):
        self.refused(request(kind="passthrough", method="gateway"), c.LAUNCH_REFUSED, "passthrough")

    def test_a_prompt_argv_cannot_carry_is_refused(self):
        self.refused(request("a\x00b"), c.REQUEST_INVALID, "NUL")
        self.refused(request("a\ud800b"), c.REQUEST_INVALID, "surrogate")
        self.refused(request("x" * (cursor.MAX_PROMPT_BYTES + 1)), c.REQUEST_INVALID, "argv limit")
        self.refused(request("\u00e9" * (cursor.MAX_PROMPT_BYTES // 2 + 1)), c.REQUEST_INVALID, "argv limit")

    def test_a_prompt_that_is_not_exactly_a_str_is_refused(self):
        """Break-it read 8 of #388, F8-1, carried to the driver: a ``str`` subclass could show the prompt checks
        one text and execve another (one hides a lone surrogate from them, one's ``encode()`` lies about its
        length), so the prompt must be exactly a ``str``. Nothing is launched."""

        class HiddenSurrogate(str):
            def __iter__(self):
                return iter("ok")

        class LyingLength(str):
            def encode(self, *args, **kwargs):
                return b"x"

        for prompt in (HiddenSurrogate("say " + chr(0xD800)), LyingLength("x" * (cursor.MAX_PROMPT_BYTES + 1))):
            with self.subTest(prompt=type(prompt).__name__):
                self.refused(request(prompt), c.REQUEST_INVALID, "exactly a str")
        launcher = LinesLauncher([init(), line(type="result", subtype="success", is_error=False, result="ok")])
        self.assertEqual(adapter(launcher).run(request(str("say hi")), 10.0)["code"], None)

    def test_the_limit_is_linuxs_one_argument_limit_less_its_nul(self):
        self.assertEqual(cursor.MAX_PROMPT_BYTES, 131072 - 1)   # MAX_ARG_STRLEN, 32 pages of 4 KiB

    def test_a_prompt_at_the_limit_is_launched(self):
        launcher = LinesLauncher([init(), line(type="result", subtype="success", is_error=False, result="ok")])
        result = adapter(launcher).run(request("x" * cursor.MAX_PROMPT_BYTES), 10.0)
        self.assertEqual((result["code"], len(launcher.launches)), (None, 1))

    def test_a_model_that_could_be_an_option_is_refused(self):
        self.refused(request(model="--api-key"), c.LAUNCH_REFUSED, "model_invalid")


class StreamOutcomeTest(unittest.TestCase):
    def test_an_error_result_is_an_engine_error_and_its_text_is_never_read(self):
        result = adapter(fake_agent.FakeLauncher("oneshot/error.jsonl")).run(request("Summarise the repository."), 10.0)
        self.assertEqual((result["code"], result["detail"], result["text"]), (c.ENGINE_ERROR, "subtype:error", ""))
        self.assertIsNone(result["limit"])

    def test_a_stream_with_no_result_is_engine_exited(self):
        result = adapter(fake_agent.FakeLauncher("oneshot/no_result.jsonl")).run(request(kind="api_key"), 10.0)
        self.assertEqual(result["code"], c.ROUTE_CHOICE_REQUIRED)   # its init reports apiKeySource "flag"
        launcher = LinesLauncher([line(type="system", subtype="init", apiKeySource="login", model="m"),
                                  line(type="assistant", message={})])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], c.ENGINE_EXITED)

    def test_a_line_that_is_not_the_engines_stream_is_a_protocol_error_and_fences(self):
        launcher = fake_agent.FakeLauncher("oneshot/hostile.jsonl")
        result = adapter(launcher).run(request("List the files."), 10.0)
        self.assertEqual(result["code"], c.PROTOCOL_ERROR)
        self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])
        for bad in (["not json"], [json.dumps({"type": "result", "subtype": "success", "is_error": False})],
                    [line(type="system", subtype="init", apiKeySource="login", model="m"),
                     line(type="assistant", message={}, session_id="other")]):
            launcher = LinesLauncher(bad)
            self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], c.PROTOCOL_ERROR, bad)
            self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])

    def test_a_result_line_that_is_not_understood_is_a_protocol_error(self):
        launcher = LinesLauncher([line(type="result", subtype="success")])
        result = adapter(launcher).run(request(), 10.0)
        self.assertEqual((result["code"], launcher.processes[0].fenced), (c.PROTOCOL_ERROR, ["protocol_error"]))
        self.assertEqual(result["detail"], "result line not understood")   # the parser's own detail (F6)
        launcher = LinesLauncher([init(), line(type="result", subtype="success", is_error=False, result=3)])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["detail"], "result answer is not text")

    def test_unknown_lines_are_passed_over(self):
        launcher = LinesLauncher([init(), line(type="thinking"), line(type="tool_call", subtype="started", call_id="c1",
                                                              tool_call={"readToolCall": {"args": {"path": "a"}}}),
                                  line(type="result", subtype="success", is_error=False, result="done")])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["text"], "done")


class BillingCheckTest(unittest.TestCase):
    def test_a_run_that_reports_another_key_source_is_stopped_and_asks(self):
        cases = (("oneshot/tools.jsonl", "subscription", "env"), ("oneshot/hello.jsonl", "api_key", "login"),
                 ("oneshot/no_result.jsonl", "api_key", "flag"), ("oneshot/no_result.jsonl", "subscription", "flag"))
        for fixture, kind, source in cases:
            with self.subTest(fixture=fixture, kind=kind):
                launcher = fake_agent.FakeLauncher(fixture)
                result = adapter(launcher).run(request(kind=kind), 10.0)
                self.assertEqual(result["code"], c.ROUTE_CHOICE_REQUIRED)
                self.assertIn(f"apiKeySource {source}", result["detail"])
                self.assertEqual(launcher.processes[0].fenced, ["billing_mismatch"])
                self.assertEqual(result["text"], "")

    def test_the_matching_source_runs(self):
        self.assertEqual(adapter(fake_agent.FakeLauncher("oneshot/hello.jsonl")).run(request(), 10.0)["code"], None)
        result = adapter(fake_agent.FakeLauncher("oneshot/tools.jsonl")).run(request(kind="api_key"), 10.0)
        self.assertEqual(result["code"], None)

    def test_a_source_cursor_does_not_document_or_none_is_refused(self):
        """The second break-it read of #412, R3: the README says a run whose init reports a source other than
        the route's is refused; a source Cursor does not document, or none at all, is a meter not known."""
        for kind in ("subscription", "api_key"):
            for fields in ({"apiKeySource": "keychain"}, {"apiKeySource": None}, {}, {"apiKeySource": 3}):
                with self.subTest(kind=kind, fields=fields):
                    launcher = LinesLauncher([line(type="system", subtype="init", model="m", **fields),
                                              line(type="result", subtype="success", is_error=False, result="ok")])
                    result = adapter(launcher).run(request(kind=kind), 10.0)
                    self.assertEqual((result["code"], result["text"]), (c.ROUTE_CHOICE_REQUIRED, ""))
                    self.assertIn("no apiKeySource Cursor documents", result["detail"])
                    self.assertEqual(launcher.processes[0].fenced, ["billing_unknown"])

    def test_the_check_happens_once_at_the_first_init(self):
        launcher = LinesLauncher([line(type="system", subtype="init", apiKeySource="login", model="m"),
                                  line(type="system", subtype="init", apiKeySource="env", model="m"),
                                  line(type="result", subtype="success", is_error=False, result="ok")])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], None)

    def test_a_run_that_reports_no_init_is_stopped_and_asks(self):
        """The first break-it read of #412, F5: with no system/init the run's meter is not known."""
        for kind in ("subscription", "api_key"):
            for last in (line(type="result", subtype="success", is_error=False, result="ok"),
                         line(type="result", subtype="error", is_error=True)):
                with self.subTest(kind=kind, last=last):
                    launcher = LinesLauncher([replay("Say hello in one sentence."), last])
                    result = adapter(launcher).run(request(kind=kind), 10.0)
                    self.assertEqual((result["code"], result["text"]), (c.ROUTE_CHOICE_REQUIRED, ""))
                    self.assertIn("reported no system/init", result["detail"])
                    self.assertEqual(launcher.processes[0].fenced, ["billing_unknown"])

    def test_the_route_is_read_once_for_the_child_and_the_check(self):
        """The first break-it read of #412, F4: a request whose route reads differently the second time
        cannot build one child and check its run against another route."""

        class Flipping(dict):
            def __init__(self, first, later):
                super().__init__(first)
                self.later, self.reads = later, 0

            def __getitem__(self, key):
                if key != "route":
                    return super().__getitem__(key)
                self.reads += 1
                return super().__getitem__(key) if self.reads == 1 else self.later

        flipping = Flipping(request(kind="api_key"), request()["route"])
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")   # its init reports the sign-in
        result = adapter(launcher).run(flipping, 10.0)
        self.assertEqual(flipping.reads, 1)
        self.assertEqual(launcher.launches[0]["env"]["CURSOR_API_KEY"], KEY)
        self.assertEqual(result["code"], c.ROUTE_CHOICE_REQUIRED)
        self.assertIn("api_key route's run reported apiKeySource login", result["detail"])


class DeadlineTest(unittest.TestCase):
    def test_a_read_that_times_out_is_the_deadline_and_fences(self):
        launcher = LinesLauncher([line(type="system", subtype="init", apiKeySource="login", model="m")], hang=True)
        result = adapter(launcher).run(request(), 10.0)
        self.assertEqual((result["code"], launcher.processes[0].fenced), (c.DEADLINE_EXCEEDED, ["deadline"]))

    def test_a_deadline_already_past_reads_nothing(self):
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        result = adapter(launcher, clock=lambda: 11.0).run(request(), 10.0)
        self.assertEqual(result["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(len(fake_agent.fixture_lines("oneshot/hello.jsonl")), len(launcher.processes[0]._out))

    def test_a_deadline_reached_exactly_reads_nothing(self):
        """The first break-it read of #412, F6 (M14): no time left is the deadline, not one more read."""
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        result = adapter(launcher, clock=lambda: 10.0).run(request(), 10.0)
        self.assertEqual((result["code"], launcher.processes[0].fenced), (c.DEADLINE_EXCEEDED, ["deadline"]))
        self.assertEqual(len(fake_agent.fixture_lines("oneshot/hello.jsonl")), len(launcher.processes[0]._out))


SOURCES = {"input.accepted": "cursor.user", "tool.call": "cursor.tool_call.started",
           "tool.result": "cursor.tool_call.completed", "result": "cursor.result"}


def expected_events(fixture, request_id="r1"):
    """The route events a whole run of ``fixture`` must carry, written here and not taken from the module: the
    fixture's expected drafts in order, the input's ids named from the request id, a nested value (a tool's
    input) not copied."""
    want = json.loads((fake_agent.HERE / fixture.replace(".jsonl", ".expected.json")).read_text(encoding="utf-8"))
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]
    out = []
    for draft in want["drafts"]:
        data = {k: (None if isinstance(v, (dict, list)) else v) for k, v in draft["payload"].items()}
        if draft["type"] == "input.accepted":
            data.update(delivery_id="dlv-" + digest, turn_id="turn-" + digest)
        out.append({"kind": draft["type"], "source": SOURCES[draft["type"]], "data": data})
    return out


def replay(text):
    return line(type="user", message={"role": "user", "content": [{"type": "text", "text": text}]})


class EventsTest(unittest.TestCase):
    """The first break-it read of #412, F1: every draft the parser gives reaches the route result, in order,
    the replay of the input included (``input.accepted``)."""

    def test_a_whole_tool_stream_reaches_the_result_in_order(self):
        result = adapter(fake_agent.FakeLauncher("oneshot/tools.jsonl")).run(
            request("Read README.md and write NOTES.md with a summary.", kind="api_key"), 10.0)
        self.assertEqual(result["code"], None)
        self.assertEqual(result["events"], expected_events("oneshot/tools.jsonl"))
        self.assertEqual([e["kind"] for e in result["events"]],
                         ["input.accepted"] + ["tool.call", "tool.result"] * 5 + ["result"])
        self.assertIsNone(result["events"][1]["data"]["input"])   # nested model payload: the digest only
        self.assertEqual(len(result["events"][1]["data"]["input_digest"]), 64)

    def test_the_input_is_accepted_from_its_replay_on_each_outcome(self):
        for fixture, req in (("oneshot/hello.jsonl", request()),
                             ("oneshot/error.jsonl", request("Summarise the repository.")),
                             ("oneshot/partial.jsonl", request("Count to two."))):
            with self.subTest(fixture=fixture):
                result = adapter(fake_agent.FakeLauncher(fixture)).run(req, 10.0)
                self.assertEqual(result["events"], expected_events(fixture))
        self.assertEqual(cursor.input_ids("r1"), (expected_events("oneshot/hello.jsonl")[0]["data"]["delivery_id"],
                                                  expected_events("oneshot/hello.jsonl")[0]["data"]["turn_id"]))

    def test_a_run_that_ends_early_keeps_the_events_it_had(self):
        before = [init(), replay("hi"), line(type="tool_call", subtype="started", call_id="c1",
                                             tool_call={"readToolCall": {"args": {"path": "a"}}})]
        result = adapter(LinesLauncher(before)).run(request("hi"), 10.0)   # end of file before a result
        self.assertEqual((result["code"], [e["kind"] for e in result["events"]]),
                         (c.ENGINE_EXITED, ["input.accepted", "tool.call"]))
        result = adapter(LinesLauncher(before, hang=True)).run(request("hi"), 10.0)   # the deadline
        self.assertEqual((result["code"], [e["kind"] for e in result["events"]]),
                         (c.DEADLINE_EXCEEDED, ["input.accepted", "tool.call"]))
        result = adapter(LinesLauncher(before + ["not json"])).run(request("hi"), 10.0)   # a protocol error
        self.assertEqual((result["code"], [e["kind"] for e in result["events"]]),
                         (c.PROTOCOL_ERROR, ["input.accepted", "tool.call"]))

    def test_lines_missing_repeated_or_out_of_order(self):
        ok = line(type="result", subtype="success", is_error=False, result="ok")
        started = line(type="tool_call", subtype="started", call_id="c1",
                       tool_call={"readToolCall": {"args": {"path": "a"}}})
        done = line(type="tool_call", subtype="completed", call_id="c1",
                    tool_call={"readToolCall": {"args": {"path": "a"}, "result": {"success": {"content": "x"}}}})
        cases = (
            ([init(), ok], ["result"]),                                    # no replay: nothing accepted
            ([init(), replay("other"), ok], ["result"]),                   # a replay of another text
            ([init(), replay("hi"), replay("hi"), ok], ["input.accepted", "result"]),   # replayed twice
            ([init(), done, started, ok], ["tool.call", "result"]),        # completed before started
            ([init(), started, started, done, done, ok], ["tool.call", "tool.result", "result"]),
            ([init(), ok, replay("hi"), started], ["result"]),             # lines after the result: not read
        )
        for lines, kinds in cases:
            with self.subTest(kinds=kinds):
                result = adapter(LinesLauncher(lines)).run(request("hi"), 10.0)
                self.assertEqual((result["code"], [e["kind"] for e in result["events"]]), (None, kinds))


class RaisingProcess(fake_agent.OneShotProcess):
    """Gives ``lines``, then raises ``error`` from ``read_line``; its fence raises ``fence_error`` if given."""

    def __init__(self, lines, error, fence_error=None):
        super().__init__(lines)
        self.error, self.fence_error = error, fence_error

    def read_line(self, timeout_s):
        if self._out:
            return super().read_line(timeout_s)
        raise self.error

    def fence(self, reason):
        super().fence(reason)
        if self.fence_error is not None:
            raise self.fence_error


def launched(process, **kwargs):
    return adapter(lambda argv, *, env, shell=False, **options: process, **kwargs)


class FencingTest(unittest.TestCase):
    """The first break-it read of #412, F2 (STRICT, fencing): once the child has started, every way out of
    the drive fences it first, and the original exception is what leaves."""

    def test_every_kind_of_exception_after_the_launch_fences_the_child(self):
        for error in (TypeError("x"), MemoryError(), KeyboardInterrupt(), RuntimeError("x"), ValueError("x"),
                      SystemExit(3), AssertionError("x"), KeyError("x"), RecursionError("x")):
            with self.subTest(error=type(error).__name__):
                process = RaisingProcess([init(), replay("Say hello in one sentence.")], error)
                with self.assertRaises(type(error)) as caught:
                    launched(process).run(request(), 10.0)
                self.assertIs(caught.exception, error)
                self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])

    def test_a_line_that_is_not_text_fences_the_child(self):
        for value in (b'{"type": "result"}', 3, ["x"]):
            with self.subTest(value=value):
                process = fake_agent.OneShotProcess([value])
                with self.assertRaises(TypeError):
                    launched(process).run(request(), 10.0)
                self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])

    def test_an_exception_from_the_clock_or_stdin_fences_the_child(self):
        def clock():
            raise RuntimeError("clock")
        process = fake_agent.OneShotProcess([init()])
        with self.assertRaises(RuntimeError):
            launched(process, clock=clock).run(request(), 10.0)
        self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])
        process = fake_agent.OneShotProcess([init()])
        process.close_stdin = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            launched(process).run(request(), 10.0)
        self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])

    def test_a_fence_that_fails_does_not_replace_the_error(self):
        error = MemoryError()
        process = RaisingProcess([], error, fence_error=RuntimeError("fence"))
        with self.assertRaises(MemoryError) as caught:
            launched(process).run(request(), 10.0)
        self.assertIs(caught.exception, error)
        self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])
        self.assertIn("fencing the child also failed: RuntimeError", caught.exception.__notes__)

    def test_a_failed_note_keeps_the_original_exception(self):
        """Astra R1: a diagnostic note that cannot be attached must not replace the driver exception."""
        original = RuntimeError("synthetic original")
        original.__notes__ = "not-a-list"
        process = RaisingProcess([init()], original, fence_error=RuntimeError("synthetic fence failure"))
        with self.assertRaises(RuntimeError) as caught:
            launched(process).run(request(), 10.0)
        self.assertIs(caught.exception, original)
        self.assertEqual(process.fenced, [cursor.DRIVER_FENCE])

    def test_the_typed_ways_out_are_fenced_once_by_the_route_boundary(self):
        for error, code, reason in ((BrokenPipeError(), c.ENGINE_ERROR, "io_error"),
                                    (ReadTimeout(), c.DEADLINE_EXCEEDED, "deadline")):
            with self.subTest(error=type(error).__name__):
                process = RaisingProcess([init()], error)
                self.assertEqual(launched(process).run(request(), 10.0)["code"], code)
                self.assertEqual(process.fenced, [reason])


class LaunchBoundaryTest(unittest.TestCase):
    """The first break-it read of #412, F3 (STRICT, guard): ``check_argv`` holds the exact argv handed to the
    launch, before anything starts."""

    def test_a_changed_argv_is_refused_at_the_launch(self):
        class Changed(cursor.CursorAdapter):
            def argv(self, request):
                return self.change(super().argv(request))

        changes = ((lambda a: a[:3] + ["--force"] + a[3:], "full_access_not_acknowledged:--force"),
                   (lambda a: a[:3] + ["--api-key", "made-up"] + a[3:], "credential_in_argv:--api-key"),
                   (lambda a: [a[0]] + a[2:], "updater_not_disabled"),
                   (lambda a: ["/usr/bin/other"] + a[1:], "executable_mismatch"),
                   (lambda a: a[:-1] + ["x" * cursor.MAX_ARG_BYTES] * 9, "argv_invalid"),
                   (lambda a: tuple(a[:3]) + ("--api-key", "made-up"), "credential_in_argv:--api-key"))
        for change, detail in changes:
            with self.subTest(detail=detail):
                launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
                made = Changed(launcher, lambda: 0.0, credentials, PARENT)
                made.change = change
                result = made.run(request(), 10.0)
                self.assertEqual((result["code"], result["detail"], launcher.launches),
                                 (c.LAUNCH_REFUSED, detail, []))

    def test_a_request_id_or_deadline_the_drive_cannot_read_launches_nothing(self):
        """Found by the lane's own attack on this answer: both started a child, then failed in the drive."""
        for req, deadline, detail in ((dict(request(), request_id=7), 10.0, "request_id: not text"),
                                      (dict(request(), request_id="r" + chr(0xD800)), 10.0, "request_id: not text"),
                                      (dict(request(), request_id="r" + chr(0xDCFF) + "x"), 10.0,
                                       "request_id: not text"),
                                      (request(), 10 ** 400, "deadline_at: not a finite number"),
                                      (request(), -(10 ** 400), "deadline_at: not a finite number"),
                                      (dict(request(), request_id=""), 10.0, "request_id: not text"),
                                      (request(), "10", "deadline_at: not a finite number"),
                                      (request(), None, "deadline_at: not a finite number"),
                                      (request(), True, "deadline_at: not a finite number"),
                                      (request(), float("nan"), "deadline_at: not a finite number"),
                                      (request(), float("inf"), "deadline_at: not a finite number")):
            with self.subTest(detail=detail, deadline=deadline):
                launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
                result = adapter(launcher).run(req, deadline)
                self.assertEqual((result["code"], result["detail"], launcher.launches),
                                 (c.REQUEST_INVALID, detail, []))
        self.assertEqual(adapter(fake_agent.FakeLauncher("oneshot/hello.jsonl")).run(request(), 10)["code"], None)

    def test_every_request_field_the_run_reads_with_an_unusable_value_starts_no_child(self):
        """The second break-it read of #412, R1, as a rule of ``run``: everything taken from the request is
        worked out before the launch. Each field the run reads, given a value of the right type that cannot be
        used (text UTF-8 cannot write, empty, too long, an option, a huge int, NaN, inf) or missing, is refused
        with a typed code, nothing escapes, and no child starts."""
        bad = chr(0xD800)
        cases = [
            (("request_id",), "r" + bad), (("request_id",), ""),
            (("prompt",), "a" + bad), (("prompt",), ""), (("prompt",), "a\x00"),
            (("prompt",), "x" * (cursor.MAX_PROMPT_BYTES + 1)),
            (("route", "model"), "m" + bad), (("route", "model"), ""), (("route", "model"), "-m"),
            (("route", "model"), "a\nb"), (("route", "model"), "m" * 200000),
            (("route", "connection", "kind"), "api_key" + bad), (("route", "connection", "kind"), ""),
            (("route", "connection", "kind"), "API_KEY"),
            (("route", "connection", "credential_env"), "CURSOR_API_KEY" + bad),
            (("route", "connection", "credential_env"), ""),
            (("route", "connection", "credential_env"), "OPENAI_API_KEY"),
            (("route", "connection", "id"), "x" + bad), (("route", "connection", "id"), ""),
        ]
        missing = [("request_id",), ("prompt",), ("route",), ("route", "model"), ("route", "connection"),
                   ("route", "connection", "kind"), ("route", "connection", "credential_env"),
                   ("route", "connection", "id")]

        def edited(path, value=None, drop=False):
            req = json.loads(json.dumps(request(kind="api_key")))
            target = req
            for key in path[:-1]:
                target = target[key]
            if drop:
                del target[path[-1]]
            else:
                target[path[-1]] = value
            return req

        runs = [(f"{'.'.join(p)}={v!r:.24}", edited(p, v), 10.0) for p, v in cases]
        runs += [(f"{'.'.join(p)} missing", edited(p, drop=True), 10.0) for p in missing]
        runs += [(f"deadline_at={d!r}", request(kind="api_key"), d)
                 for d in (10 ** 400, -(10 ** 400), float("nan"), float("inf"), float("-inf"), True, "10", None)]
        for label, req, deadline in runs:
            with self.subTest(label):
                launcher = fake_agent.FakeLauncher("oneshot/tools.jsonl")
                result = adapter(launcher).run(req, deadline)
                self.assertIn(result["code"], (c.REQUEST_INVALID, c.LAUNCH_REFUSED))
                self.assertEqual(launcher.launches, [])
        # The fields the run does not read reach nothing, whatever they hold.
        req = dict(request(), contract=object(), tenant="t" + bad, deadline_s=float("nan"))
        req["route"] = dict(req["route"], engine="not-cursor")
        self.assertEqual(adapter(fake_agent.FakeLauncher("oneshot/hello.jsonl")).run(req, 10.0)["code"], None)

    def test_an_empty_prompt_is_the_prompts_own_refusal(self):
        """The second break-it read of #412, R4: every prompt defect answers ``request_invalid``."""
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        result = adapter(launcher).run(request(""), 10.0)
        self.assertEqual((result["code"], result["detail"], launcher.launches),
                         (c.REQUEST_INVALID, "prompt: empty", []))

    def test_nothing_reads_the_request_after_the_launch(self):
        """The second break-it read of #412, R1: the drive is handed what was worked out before the launch, not
        the request; the caller's request can be emptied once the child starts and the run is the same."""
        seen = []

        class Watching(cursor.CursorAdapter):
            def drive(self, process, request, deadline_at, events):
                seen.append(request)
                return super().drive(process, request, deadline_at, events)

        req = request()

        def launcher(argv, *, env, shell=False, **options):
            req.clear()
            return fake_agent.OneShotProcess(fake_agent.fixture_lines("oneshot/hello.jsonl"))

        result = Watching(launcher, lambda: 0.0, credentials, PARENT).run(req, 10.0)
        self.assertEqual((result["code"], [e["kind"] for e in result["events"]]), (None, ["input.accepted", "result"]))
        self.assertNotIsInstance(seen[0], Mapping)
        with self.assertRaises(KeyError):
            seen[0]["prompt"]

    def test_only_a_closure_holds_compositions_launcher(self):
        """The second break-it read of #412, R8: no attribute of the adapter is the raw launcher."""
        launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
        made = adapter(launcher)
        self.assertFalse([name for name, value in vars(made).items() if value is launcher])
        with self.assertRaises(c.RouteError):
            made._launcher(["agent", "--disable-auto-update", "-p", "--force"], env={})
        self.assertEqual(launcher.launches, [])

    def test_a_model_over_the_bounds_is_refused_not_launched(self):
        for size in (140000, 1100000):
            with self.subTest(size=size):
                launcher = fake_agent.FakeLauncher("oneshot/hello.jsonl")
                result = adapter(launcher).run(request(model="m" * size), 10.0)
                self.assertEqual((result["code"], result["detail"], launcher.launches),
                                 (c.LAUNCH_REFUSED, "argv_invalid", []))

    def test_the_launch_gets_the_very_argv_that_was_checked(self):
        seen = []
        checked = cursor.check_argv

        def spy(argv, **kwargs):
            seen.append(("checked", argv))
            return checked(argv, **kwargs)

        def launcher(argv, *, env, shell=False, **options):
            seen.append(("launched", argv))
            return fake_agent.OneShotProcess(fake_agent.fixture_lines("oneshot/hello.jsonl"))

        cursor.check_argv = spy
        self.addCleanup(setattr, cursor, "check_argv", checked)
        self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], None)
        self.assertEqual([step for step, _ in seen], ["checked", "launched"])
        self.assertIs(seen[0][1], seen[1][1])

    def test_a_mapping_the_run_cannot_read_is_refused_before_launch(self):
        """Astra R2 and DeepSeek round 3 F1: an ordinary failure while reading or copying the request, its
        route or its connection is a typed refusal and starts no child. The connection kind is judged as
        plain text before it is compared. KeyboardInterrupt from the mapping still leaves ``run``."""

        class BadMapping(Mapping):
            def __init__(self, data, operation):
                self.data, self.operation = data, operation

            def __len__(self):
                return len(self.data)

            def __iter__(self):
                if self.operation == "iterate":
                    raise ValueError("synthetic iteration failure")
                return iter(self.data)

            def __getitem__(self, key):
                if self.operation == "lookup":
                    raise ValueError("synthetic lookup failure")
                return self.data[key]

        class BadEquality(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                raise ValueError("synthetic comparison failure")

        class KeysRaise(Mapping):
            def __init__(self):
                self.data = request(kind="api_key")

            def __iter__(self):
                return iter(self.data)

            def __len__(self):
                return len(self.data)

            def __getitem__(self, key):
                return self.data[key]

            def keys(self):
                raise RuntimeError("hostile keys")

        class InterruptOnId(Mapping):
            def __iter__(self):
                return iter(["request_id"])

            def __len__(self):
                return 1

            def __getitem__(self, key):
                raise KeyboardInterrupt("hostile getitem")

        cases = []
        for level in ("request", "route", "connection"):
            for operation in ("lookup", "iterate"):
                req = request(kind="api_key")
                broken = BadMapping(
                    req if level == "request" else req["route"] if level == "route" else req["route"]["connection"],
                    operation)
                if level == "request":
                    req = broken
                elif level == "route":
                    req["route"] = broken
                else:
                    req["route"]["connection"] = broken
                cases.append((f"{level} {operation}", req, c.REQUEST_INVALID, "ValueError"))
        cases.append(("keys", KeysRaise(), c.REQUEST_INVALID, "RuntimeError"))
        kind_req = request(kind="api_key")
        kind_req["route"]["connection"]["kind"] = BadEquality("api_key")
        cases.append(("kind", kind_req, c.LAUNCH_REFUSED, None))
        for label, req, code, exc_name in cases:
            with self.subTest(label):
                launcher = LinesLauncher([])
                result = adapter(launcher).run(req, 10.0)
                self.assertEqual(result["code"], code)
                self.assertEqual(launcher.launches, [])
                self.assertIsInstance(result["request_id"], str)
                if exc_name is not None:
                    self.assertEqual(result["detail"], f"request: not readable ({exc_name})")
        launcher = LinesLauncher([])
        with self.assertRaises(KeyboardInterrupt):
            adapter(launcher).run(InterruptOnId(), 10.0)
        self.assertEqual(launcher.launches, [])

    def test_a_refusal_answers_the_request_id_it_already_read(self):
        """Astra R3 and DeepSeek round 3 F4: a refusal answers with the id copied while preparing, and does
        not read the caller's request id again."""

        class FlipId(Mapping):
            def __init__(self):
                self.base = request(prompt="")
                self.n = 0

            def __iter__(self):
                return iter(self.base)

            def __len__(self):
                return len(self.base)

            def __getitem__(self, key):
                if key == "request_id":
                    self.n += 1
                    return "first" if self.n == 1 else "second"
                return self.base[key]

        req = FlipId()
        launcher = LinesLauncher([])
        result = adapter(launcher).run(req, 10.0)
        self.assertEqual((result["request_id"], result["code"], req.n, launcher.launches),
                         ("first", c.REQUEST_INVALID, 1, []))

    def test_a_general_mapping_launches_the_first_route(self):
        """Astra R4: a general mapping whose route changes on the next lookup launches the first route."""

        class FlipRoute(Mapping):
            def __init__(self):
                self.base = request(kind="api_key", model="first-model")
                self.other = request(kind="subscription", model="second-model")["route"]
                self.n = 0

            def __len__(self):
                return len(self.base)

            def __iter__(self):
                return iter(self.base)

            def __getitem__(self, key):
                if key == "route":
                    self.n += 1
                    return self.base[key] if self.n == 1 else self.other
                return self.base[key]

        req = FlipRoute()
        launcher = LinesLauncher([init("env"), line(type="result", subtype="success", is_error=False, result="ok")])
        result = adapter(launcher).run(req, 10.0)
        self.assertIsNone(result["code"])
        self.assertEqual(launcher.launches[0]["argv"][-3:], ["first-model", "--", req.base["prompt"]])
        self.assertEqual(launcher.launches[0]["env"]["CURSOR_API_KEY"], KEY)

    def test_a_general_mapping_connection_is_read_once(self):
        """Astra R4: a general mapping connection is snapshotted once; env and the billing check use that kind."""

        class Flip(Mapping):
            def __init__(self):
                self.n = 0

            def __iter__(self):
                return iter(["kind", "id", "credential_env"])

            def __len__(self):
                return 3

            def __getitem__(self, key):
                if key == "kind":
                    self.n += 1
                    return "api_key" if self.n == 1 else "subscription"
                return {"id": "synthetic-connection", "credential_env": "CURSOR_API_KEY"}[key]

        req = request(kind="api_key")
        connection = Flip()
        req["route"]["connection"] = connection
        launcher = LinesLauncher([init("login")])
        result = adapter(launcher).run(req, 10.0)
        self.assertEqual(connection.n, 1)
        self.assertEqual(launcher.launches[0]["env"]["CURSOR_API_KEY"], KEY)
        self.assertEqual(result["code"], c.ROUTE_CHOICE_REQUIRED)

    def test_a_valid_request_id_is_kept_on_a_refusal(self):
        """Astra R4: a usable, distinctive request id is the id of a refusal."""
        req = request(prompt="")
        req["request_id"] = "distinct-refusal-id"
        launcher = LinesLauncher([])
        result = adapter(launcher).run(req, 10.0)
        self.assertEqual((result["request_id"], result["code"], launcher.launches),
                         ("distinct-refusal-id", c.REQUEST_INVALID, []))

    def test_a_valid_request_id_is_kept_on_success(self):
        """Astra R4: a usable, distinctive request id is the id of a successful run."""
        req = request()
        req["request_id"] = "distinct-success-id"
        launcher = LinesLauncher([init(), line(type="result", subtype="success", is_error=False, result="ok")])
        result = adapter(launcher).run(req, 10.0)
        self.assertEqual((result["request_id"], result["code"]), ("distinct-success-id", None))

    def test_a_request_that_is_not_an_object_is_refused(self):
        """DeepSeek round 3 F2: a request that is not a mapping is request_invalid and starts no child."""
        for value in (None, [], "request", 7, object()):
            with self.subTest(value=type(value).__name__):
                launcher = LinesLauncher([])
                result = adapter(launcher).run(value, 10.0)
                self.assertEqual((result["code"], result["detail"], result["request_id"], launcher.launches),
                                 (c.REQUEST_INVALID, "request: not an object", "", []))

    def test_a_refusal_does_not_echo_an_unusable_request_id(self):
        """DeepSeek round 3 F2: a refusal never puts an unusable request id on the result."""

        class Sub(str):
            pass

        cases = (
            (dict(request(), request_id=7), 10.0),
            (dict(request(), request_id="r" + chr(0xD800)), 10.0),
            (dict(request(), request_id=""), 10.0),
            (dict(request(), request_id=None), 10.0),
            (dict(request(), request_id=Sub("r1")), 10.0),
        )
        missing = request()
        del missing["request_id"]
        cases = (*cases, (missing, 10.0))
        for req, deadline in cases:
            with self.subTest(request_id=req.get("request_id")):
                launcher = LinesLauncher([])
                result = adapter(launcher).run(req, deadline)
                self.assertEqual(result["code"], c.REQUEST_INVALID)
                self.assertEqual(result["request_id"], "")
                self.assertEqual(launcher.launches, [])


if __name__ == "__main__":
    unittest.main()
