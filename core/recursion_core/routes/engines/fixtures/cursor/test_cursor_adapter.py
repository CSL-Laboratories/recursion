# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The one-shot driver (routes/engines/cursor.py, ``CursorAdapter``) against the fake ``agent``: argv, the
child's environment, the route result for each stream, the billing check, the deadline. No process runs."""

from __future__ import annotations

import json
import unittest

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
            launcher = LinesLauncher([line(type="result", subtype="success", is_error=False, result="ok")])
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
        launcher = LinesLauncher([line(type="result", subtype="success", is_error=False, result="ok")])
        self.assertEqual(adapter(launcher).run(request(str("say hi")), 10.0)["code"], None)

    def test_the_limit_is_linuxs_one_argument_limit_less_its_nul(self):
        self.assertEqual(cursor.MAX_PROMPT_BYTES, 131072 - 1)   # MAX_ARG_STRLEN, 32 pages of 4 KiB

    def test_a_prompt_at_the_limit_is_launched(self):
        launcher = LinesLauncher([line(type="result", subtype="success", is_error=False, result="ok")])
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

    def test_unknown_lines_are_passed_over(self):
        launcher = LinesLauncher([line(type="thinking"), line(type="tool_call", subtype="started", call_id="c1",
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

    def test_a_source_cursor_does_not_document_is_not_refused(self):
        launcher = LinesLauncher([line(type="system", subtype="init", apiKeySource="keychain", model="m"),
                                  line(type="result", subtype="success", is_error=False, result="ok")])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], None)

    def test_the_check_happens_once_at_the_first_init(self):
        launcher = LinesLauncher([line(type="system", subtype="init", apiKeySource="login", model="m"),
                                  line(type="system", subtype="init", apiKeySource="env", model="m"),
                                  line(type="result", subtype="success", is_error=False, result="ok")])
        self.assertEqual(adapter(launcher).run(request(), 10.0)["code"], None)


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


if __name__ == "__main__":
    unittest.main()
