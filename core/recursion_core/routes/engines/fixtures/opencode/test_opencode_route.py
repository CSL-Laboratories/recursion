"""The one-shot OpenCode drive end to end on today's route path (lane ENGINE-E3, slice 2, local).

``RouteService`` -> ``ConnectionBreaker`` -> ``ProfileRouteAdapter`` (the guard, ``guard_then_spawn``) ->
``opencode.route_argv`` and ``opencode.OneShot``, with a fake launcher whose process replays a SYNTHETIC fixture.
Nothing here edits or replaces the guard, the adapter or the service: they are today's code. Run by name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_route
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.engines import opencode as oc
from recursion_core.routes.base import Deadline
from recursion_core.routes.launch import ReadTimeout
from recursion_core.subscription_routes import SubscriptionRouteRefused
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, request

HERE = Path(__file__).resolve().parent
MODEL = "openai/gpt-5.5"
PROFILE = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
HANG = object()


class RawProcess:
    """An engine process whose standard input is plain text (OpenCode reads the message there), not JSON lines."""

    def __init__(self, lines: list, clock: FakeClock) -> None:
        self.lines, self.clock = list(lines), clock
        self.raise_on_send: BaseException | None = None
        self.sent: list[str] = []
        self.stdin_closed = False
        self.fenced: list[str] = []

    def send(self, line: str) -> None:
        assert not self.stdin_closed, "send after close_stdin"
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(line)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def read_line(self, timeout_s: float):
        assert self.stdin_closed, "read before the prompt was closed"
        if not self.lines:
            return None
        line = self.lines.pop(0)
        if isinstance(line, BaseException):
            raise line
        if line is HANG:
            self.clock.advance(timeout_s)
            raise ReadTimeout()
        return line

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)


class Launcher:
    def __init__(self, clock: FakeClock) -> None:
        self.clock, self.calls, self.processes, self.next = clock, [], [], []

    def __call__(self, argv, **kwargs) -> RawProcess:
        self.calls.append({"argv": list(argv), "env": dict(kwargs["env"]), "shell": kwargs.get("shell")})
        lines = self.next.pop(0)
        if isinstance(lines, str):   # a fixture name: its lines as read_line gives them, without the break
            lines = (HERE / lines).read_text("utf-8").splitlines()
        process = RawProcess(lines, self.clock)
        self.processes.append(process)
        return process


@unittest.skipIf(os.name == "nt", "the guard's route-home checks are POSIX")
class RouteTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="e3-route-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.operator = root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = root / "route-home"
        self.home.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.launcher = Launcher(self.clock)
        parent = {"PATH": "/usr/bin:/bin", "HOME": str(self.operator)}
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: "sk-made-up-" + cid, parent,
                                    argv=oc.route_argv, driver=oc.OneShot(self.clock, wall=lambda: 1794614400.0),
                                    operator_home=str(self.operator))
        self.breaker = ConnectionBreaker(self.clock)
        self.service = RouteService(adapters, self.breaker, self.clock)

    def req(self, conn_id="k1", profile=None, credential_env="OPENAI_API_KEY", **kw):
        return request(engine="opencode", model=kw.pop("model", MODEL), kind="api_key", conn_id=conn_id,
                       credential_env=credential_env, profile=dict(profile or PROFILE), home=str(self.home), **kw)

    def run_fixture(self, name, **kw):
        self.launcher.next.append(name)
        return self.service.handle(self.req(**kw))

    def test_an_api_key_run_passes_the_guard_and_writes_the_prompt_on_standard_input(self):
        res = self.run_fixture("synthetic-v1-success.jsonl", prompt="-v --model claude-agent/x")
        self.assertEqual((res["status"], res["code"], res["text"]), ("ok", None, "The tests pass."))
        (call,) = self.launcher.calls
        self.assertEqual(call["argv"], ["opencode", "run", "--format", "json", "--model", MODEL])
        self.assertEqual(call["env"], {"PATH": "/usr/bin:/bin", "HOME": str(self.home),
                                       "OPENAI_API_KEY": "sk-made-up-k1"})
        self.assertIs(call["shell"], False)
        process = self.launcher.processes[0]
        self.assertEqual((process.sent, process.stdin_closed), (["-v --model claude-agent/x"], True))
        (usage,) = [e for e in res["events"] if e["kind"] == "usage"]
        self.assertEqual(usage["data"]["input_tokens"], {"value": 1200, "quality": "known", "provenance": "observed"})

    def test_a_replayed_429_opens_the_breaker_for_that_connection_only(self):
        for name in ("synthetic-v1-limit-429.jsonl", "synthetic-v2-rate-limit.jsonl"):
            with self.subTest(name):
                self.setUp()
                res = self.run_fixture(name)
                self.assertEqual((res["code"], res["breaker_effect"]), (c.PLAN_LIMIT, "opened"))
                again = self.service.handle(self.req())                 # the same connection: refused, no spawn
                self.assertEqual(again["code"], c.BREAKER_OPEN)
                self.assertEqual(len(self.launcher.calls), 1)
                other = self.run_fixture("synthetic-v1-success.jsonl", conn_id="k2")   # another connection runs
                self.assertEqual((other["code"], len(self.launcher.calls)), (None, 2))

    def test_a_plain_error_does_not_open_it(self):
        for name, code in (("synthetic-v1-error-no-status.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v2-transport-error.jsonl", c.ENGINE_ERROR),
                           ("synthetic-v1-exit-1-no-output.jsonl", c.ENGINE_EXITED)):
            with self.subTest(name):
                self.setUp()
                res = self.run_fixture(name)
                self.assertEqual((res["code"], res["breaker_effect"]), (code, "none"))
                self.assertEqual(self.run_fixture("synthetic-v1-success.jsonl")["code"], None)

    def test_without_an_exit_status_the_last_step_decides(self):
        self.assertEqual(self.run_fixture("synthetic-v1-tool-use.jsonl")["code"], None)    # ends "stop"
        tool_calls_only = (HERE / "synthetic-v1-tool-use.jsonl").read_text("utf-8").splitlines()[:3]
        self.launcher.next.append(tool_calls_only)                                        # ends "tool-calls"
        res = self.service.handle(self.req())
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_EXITED, "no exit status and no finished step"))

    def test_refusals_come_before_any_spawn(self):
        cases = [
            ({"provider": "claude-agent", "auth": "subscription", "credential_env": "ANTHROPIC_API_KEY"},
             "ANTHROPIC_API_KEY", c.SUBSCRIPTION_ROUTE_REFUSED),          # OpenCode's Claude Agent provider
            ({"provider": "google", "auth": "subscription", "credential_env": "OPENAI_API_KEY"},
             "OPENAI_API_KEY", c.SUBSCRIPTION_ROUTE_REFUSED),             # a Google label
            ({"provider": "openai", "auth": "api_key", "credential_env": "CLAUDE_CODE_OAUTH_TOKEN"},
             "CLAUDE_CODE_OAUTH_TOKEN", c.LAUNCH_REFUSED),                # a Claude token for the child
        ]
        for profile, env_name, code in cases:
            with self.subTest(profile):
                res = self.service.handle(self.req(profile=profile, credential_env=env_name))
                self.assertEqual(res["code"], code)
                self.assertEqual(res["breaker_effect"], "none")
        self.assertEqual(self.launcher.calls, [])

    def test_a_hung_engine_is_fenced_at_the_deadline_for_this_request_only(self):
        self.launcher.next.append([HANG])
        res = self.service.handle(self.req(deadline_s=5.0))
        self.assertEqual((res["code"], res["breaker_effect"]), (c.DEADLINE_EXCEEDED, "none"))
        self.assertEqual(self.launcher.processes[0].fenced, ["deadline"])
        self.assertEqual(self.breaker.state("api_key:k1"), "closed")

    def test_an_invalid_model_is_a_typed_refusal_with_nothing_launched_or_booked(self):
        for model in ("gpt", "-x/y", "a b/c", "a/b\n", "openai/"):
            with self.subTest(model):
                res = self.service.handle(self.req(model=model))
                self.assertEqual((res["code"], res["detail"], res["breaker_effect"]),
                                 (c.SUBSCRIPTION_ROUTE_REFUSED, "model_malformed", "none"))
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.breaker.state("api_key:k1"), "closed")
        self.assertEqual(self.run_fixture("synthetic-v1-success.jsonl")["code"], None)   # the connection still runs
        self.assertEqual(oc.route_argv(self.req(), binary="/opt/oc/bin/opencode")[0], "/opt/oc/bin/opencode")
        for bad in ({}, None, {"route": None}):
            with self.subTest(bad=bad):
                with self.assertRaises(c.RouteError) as caught:
                    oc.route_argv(bad)
                self.assertEqual((caught.exception.code, caught.exception.detail), (c.REQUEST_INVALID, "route"))

    def test_a_connection_the_parser_refuses_writes_nothing(self):
        process = RawProcess([], self.clock)
        req = self.req(conn_id="a\nb")
        res = oc.OneShot(self.clock)(process, req, self.clock() + 60, [])
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "route.connection_id"))
        self.assertEqual((process.sent, process.stdin_closed), ([], False))
        self.assertEqual(process.fenced, ["request_invalid"])      # the process had started: it is ended


@unittest.skipIf(os.name == "nt", "the guard's route-home checks are POSIX")
class LaunchedModelTests(unittest.TestCase):
    """F1 of #414's first read: the guard is held to the model that is LAUNCHED, whatever its source."""

    setUp, req, run_fixture = RouteTests.setUp, RouteTests.req, RouteTests.run_fixture   # the same route path

    OPENAI = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}

    def refused(self, cause, **kw):
        res = self.service.handle(self.req(**kw))
        self.assertEqual((res["code"], res["detail"], res["breaker_effect"]),
                         (c.SUBSCRIPTION_ROUTE_REFUSED, cause, "none"))
        self.assertEqual(self.launcher.calls, [])           # nothing reached the launch boundary

    def test_a_claude_provider_in_the_routes_model_is_refused(self):
        for model in ("claude-agent/sonnet", "Claude-Agent/sonnet", "CLAUDE-CODE/x", "claude-agent-sdk/x",
                      "claude_pro/x", "claude/opus", "anthropic-oauth/x"):
            with self.subTest(model):
                self.refused("claude_subscription_provider", model=model)

    def test_a_claude_provider_in_both_the_profile_and_the_route_is_refused(self):
        self.refused("claude_subscription_provider", model="claude-agent/sonnet",
                     profile=dict(self.OPENAI, model="claude-agent/sonnet"))

    def test_the_route_must_launch_the_model_the_profile_names(self):
        for profile_model, route_model in (("openai/gpt-5.5", "claude-agent/sonnet"),
                                           ("openai/gpt-5.5", "openai/gpt-5.4")):
            with self.subTest((profile_model, route_model)):
                self.refused("model_differs_from_profile", model=route_model,
                             profile=dict(self.OPENAI, model=profile_model))
        # A null model (OpenCode's own default) never reaches the adapter: the route contract refuses it first.
        res = self.service.handle(self.req(model=None, profile=dict(self.OPENAI, model="openai/gpt-5.5")))
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "route.model"))
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(oc.route_argv(self.req(model=None)), ["opencode", "run", "--format", "json"])
        with self.assertRaises(SubscriptionRouteRefused) as caught:     # the hook alone refuses it too
            oc.route_argv(self.req(model=None, profile=dict(self.OPENAI, model="openai/gpt-5.5")))
        self.assertEqual(caught.exception.cause, "model_differs_from_profile")

    def test_a_model_that_is_not_a_plain_str_is_refused_however_it_compares(self):
        class Agreeable(str):
            def __eq__(self, other):
                return True

            def __ne__(self, other):
                return False

            __hash__ = str.__hash__

        with self.assertRaises(SubscriptionRouteRefused) as caught:
            oc.held_to_the_guard(Agreeable("anthropic/x"), dict(self.OPENAI, model="openai/gpt-5.5"))
        self.assertEqual(caught.exception.cause, "model_malformed")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            oc.held_to_the_guard("openai/gpt-5.5", dict(self.OPENAI, model=Agreeable("openai/gpt-5.5")))
        self.assertEqual(caught.exception.cause, "model_differs_from_profile")

    def test_a_model_the_profile_does_not_name_is_of_the_profiles_provider(self):
        for model in ("anthropic/claude-x", "google/gemini", "openrouter/claude-agent"):
            with self.subTest(model):
                self.refused("model_provider_mismatch", model=model)

    def test_the_value_the_guard_accepted_is_the_value_launched(self):
        res = self.run_fixture("synthetic-v1-success.jsonl", model="openai/gpt-5.5",
                               profile=dict(self.OPENAI, model="openai/gpt-5.5"))
        self.assertEqual(res["code"], None)
        self.assertEqual([call["argv"][4:] for call in self.launcher.calls], [["--model", "openai/gpt-5.5"]])

    def test_the_driver_path_refuses_a_claude_provider_before_any_spawn(self):
        spawns = []
        driver = oc.OpenCodeDriver(lambda argv, env: spawns.append(argv), wall=lambda: 1.0, clock=lambda: 1.0)
        session = {"session_id": "ses-e3", "run_id": "run-e3",
                   "route": {"engine": "opencode", "model": "Claude-Agent/sonnet",
                             "connection": {"kind": "api_key", "id": "k1"}}}
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            driver.start(session, None, {"PATH": "/usr/bin"})
        self.assertEqual(caught.exception.cause, "claude_subscription_provider")
        self.assertEqual(spawns, [])


@unittest.skipIf(os.name == "nt", "the guard's route-home checks are POSIX")
class AfterTheLaunchTests(unittest.TestCase):
    """F2 of #414's first read: once the child has started, every way out fences it first."""

    setUp, req, run_fixture = RouteTests.setUp, RouteTests.req, RouteTests.run_fixture   # the same route path

    def test_a_chunk_that_is_not_one_line_of_text_is_a_typed_protocol_error(self):
        for chunk in ("a\nb", "a\nb\n", "\n\n", b"x\n", 7, ["x"]):
            with self.subTest(chunk=chunk):
                self.setUp()
                self.launcher.next.append([chunk])
                res = self.service.handle(self.req())
                self.assertEqual(res["code"], c.PROTOCOL_ERROR)
                self.assertEqual(self.launcher.processes[0].fenced, ["protocol_error"])

    def test_any_other_exception_fences_the_child_and_leaves_unchanged(self):
        for error in (ValueError("v"), TypeError("t"), RuntimeError("r"), KeyError("k"), KeyboardInterrupt(),
                      SystemExit(3)):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.launcher.next.append([error])
                with self.assertRaises(type(error)) as caught:
                    self.service.handle(self.req())
                self.assertIs(caught.exception, error)
                self.assertEqual(self.launcher.processes[0].fenced, ["driver_error"])

    def test_an_exception_while_writing_the_prompt_fences_too(self):
        for error, fenced in ((KeyboardInterrupt(), ["driver_error"]), (BrokenPipeError(), ["io_error"])):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                self.launcher.next.append(["x"])
                launcher = self.launcher

                def spawn(argv, _launcher=launcher, _error=error, **kwargs):
                    process = Launcher.__call__(_launcher, argv, **kwargs)
                    process.raise_on_send = _error
                    return process

                self.service._adapters["opencode"]._launcher = spawn
                if isinstance(error, OSError):
                    self.assertEqual(self.service.handle(self.req())["code"], c.ENGINE_ERROR)
                else:
                    with self.assertRaises(KeyboardInterrupt):
                        self.service.handle(self.req())
                self.assertEqual(self.launcher.processes[0].fenced, fenced)

    def test_lines_split_missing_or_repeated_read_as_lines(self):
        whole = (HERE / "synthetic-v1-success.jsonl").read_text("utf-8").splitlines()
        res = self.run_fixture(whole + whole)                         # repeated: the same answer twice over
        self.assertEqual(res["code"], None)
        res = self.run_fixture([])                                    # nothing at all
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_EXITED, "no exit status and no finished step"))
        res = self.run_fixture([whole[0][:10], whole[0][10:]])       # one line split in two: two bad lines
        self.assertEqual(res["code"], c.ENGINE_EXITED)
        self.assertEqual([p.fenced for p in self.launcher.processes], [["stdout_ended"]] * 3)


class OneShotTests(unittest.TestCase):
    """The drive itself, without the route path: the deadline (F4, F10) and the caller's event list."""

    STOP = ('{"type":"step_finish","timestamp":1,"sessionID":"ses_x","part":{"type":"step-finish","reason":"stop",'
            '"tokens":{"input":1}}}')

    class Chatty:
        """A process that gives its lines at once, each one moving the clock on by ``step`` seconds."""

        def __init__(self, lines, clock, step=0.0):
            self.lines, self.clock, self.step = list(lines), clock, step
            self.sent, self.closed, self.fenced = [], False, []

        def send(self, text):
            self.sent.append(text)

        def close_stdin(self):
            self.closed = True

        def read_line(self, timeout_s):
            self.clock.advance(self.step)
            return self.lines.pop(0) if self.lines else None

        def fence(self, reason):
            self.fenced.append(reason)

    def drive(self, lines, step=0.0, deadline=100.0, events=None):
        clock = FakeClock()
        process = self.Chatty(lines, clock, step)
        req = request(engine="opencode", model=MODEL, kind="api_key", conn_id="k1", credential_env="OPENAI_API_KEY",
                      profile=dict(PROFILE), home="/var/lib/recursion/homes/opencode")
        events = [] if events is None else events
        self.process = process
        result = oc.OneShot(clock, wall=lambda: 1794614400.0)(process, req, clock() + deadline, events)
        return result, events, process

    def test_an_engine_that_keeps_talking_past_the_deadline_is_stopped_there(self):
        with self.assertRaises(Deadline):
            self.drive(["x"] * 20, step=10.0, deadline=35.0)
        self.assertEqual(len(self.process.lines), 16)        # four reads, the last one ending at 40 s

    def test_nothing_is_written_once_the_deadline_has_passed(self):
        for deadline in (0.0, -1.0):
            with self.subTest(deadline=deadline):
                with self.assertRaises(Deadline):
                    self.drive([self.STOP], deadline=deadline)
                self.assertEqual((self.process.sent, self.process.closed), ([], False))

    def test_an_empty_read_is_not_the_end_of_the_output(self):
        result, _, _ = self.drive(["", self.STOP])
        self.assertIsNone(result["code"])   # the stopped step after the empty read was read

    def test_the_usage_events_join_the_callers_list(self):
        mine = {"kind": "usage", "data": {}}
        result, events, _ = self.drive([self.STOP], events=[mine])
        self.assertIs(result["events"], events)
        self.assertIs(events[0], mine)
        self.assertEqual(len(events), 2)

    def test_a_request_the_drive_cannot_read_fences_the_process(self):
        clock = FakeClock()
        process = self.Chatty([], clock)
        with self.assertRaises(KeyError):
            oc.OneShot(clock)(process, {"request_id": "r1"}, clock() + 10, [])
        self.assertEqual((process.fenced, process.sent), (["driver_error"], []))

    def test_the_constructor_refuses_what_it_cannot_call(self):
        for args, kwargs in (((None,), {}), (("clock",), {}), ((FakeClock(),), {"wall": None})):
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    oc.OneShot(*args, **kwargs)


@unittest.skipIf(os.name == "nt", "the guard's route-home checks are POSIX")
class RoundTwoRouteTests(unittest.TestCase):
    """#414 round 2: astra R1 (the end of stdout is not the end of the process), R6 (an internal CR)."""

    setUp, req, run_fixture = RouteTests.setUp, RouteTests.req, RouteTests.run_fixture   # the same route path

    def test_the_end_of_stdout_hands_the_process_to_cleanup_on_success_and_failure(self):
        for name, code in (("synthetic-v1-success.jsonl", None),
                           ("synthetic-v1-exit-1-no-output.jsonl", c.ENGINE_EXITED),
                           ("synthetic-v1-limit-429.jsonl", c.PLAN_LIMIT)):
            with self.subTest(name):
                self.setUp()
                self.assertEqual(self.run_fixture(name)["code"], code)
                self.assertEqual(self.launcher.processes[0].fenced, ["stdout_ended"])

    def test_a_carriage_return_inside_a_chunk_is_a_protocol_error(self):
        whole = (HERE / "synthetic-v1-success.jsonl").read_text("utf-8").splitlines()
        for chunk in (whole[0] + "\r" + whole[1], "a\rb\r\n", "a\r\rb\n", "a\r\nb\n", "x\r"):
            with self.subTest(chunk=chunk):
                self.setUp()
                self.launcher.next.append([chunk])
                res = self.service.handle(self.req())
                self.assertEqual(res["code"], c.PROTOCOL_ERROR)
                self.assertEqual(self.launcher.processes[0].fenced, ["protocol_error"])

    def test_a_terminal_crlf_is_one_ordinary_line(self):
        whole = (HERE / "synthetic-v1-success.jsonl").read_text("utf-8").splitlines()
        res = self.run_fixture([line + "\r\n" for line in whole])
        self.assertEqual((res["code"], res["text"]), (None, "The tests pass."))

    def test_a_profile_that_is_not_a_mapping_counts_as_empty(self):
        for profile in ("not a mapping", 5, ["openai"]):
            with self.subTest(profile=profile):
                with self.assertRaises(SubscriptionRouteRefused) as caught:
                    oc.held_to_the_guard("openai/x", profile)
                self.assertEqual(caught.exception.cause, "model_provider_mismatch")


class RoundTwoDeadlineTests(unittest.TestCase):
    """#414 round 2: astra R2, the deadline is checked again after every read, the end of output included."""

    STOP, Chatty, drive = OneShotTests.STOP, OneShotTests.Chatty, OneShotTests.drive

    def test_a_read_that_returns_the_end_late_is_past_the_deadline(self):
        with self.assertRaises(Deadline):
            self.drive([self.STOP], step=3.0, deadline=5.0)        # the stop at 3 s, the end of output at 6 s
        with self.assertRaises(Deadline):
            self.drive([self.STOP, self.STOP], step=3.0, deadline=5.0)   # a line, not the end, read at 6 s
        result, _, _ = self.drive([self.STOP], step=2.0, deadline=5.0)    # the end at 4 s: inside it
        self.assertIsNone(result["code"])
        self.assertEqual(self.process.fenced, ["stdout_ended"])


class ReadTimeoutTests(unittest.TestCase):
    """A read is never asked to wait for a time that is already gone (the check before every read)."""

    def test_a_clock_that_moved_between_reads_stops_before_the_next_read(self):
        state = {"now": 0.0, "step": 0.0}   # still until the first read returns; then 9 s, moving 1.5 s a reading
        waits = []

        def clock():
            value = state["now"]
            state["now"] += state["step"]
            return value

        class Process:
            sent, fenced = [], []

            def send(self, text):
                self.sent.append(text)

            def close_stdin(self):
                pass

            def read_line(self, timeout_s):
                # A BOUND: this fake would feed for ever, so a drive that no longer stops at the late read (the
                # after-read check removed) fails here at once instead of growing without end (Nov 15, 2026: such
                # a mutant passed the 6 GB ceiling five times, its Parser.anomalies growing a line a read).
                if len(waits) >= 3:
                    raise AssertionError("read past the test's budget of three reads")
                waits.append(timeout_s)
                state["now"], state["step"] = 9.0, 1.5
                return "x\n"

            def fence(self, reason):
                self.fenced.append(reason)

        req = request(engine="opencode", model=MODEL, kind="api_key", conn_id="k1", credential_env="OPENAI_API_KEY",
                      profile=dict(PROFILE), home="/var/lib/recursion/homes/opencode")
        with self.assertRaises(Deadline):
            oc.OneShot(clock, wall=lambda: 1794614400.0)(Process(), req, 10.0, [])
        self.assertEqual(waits, [10.0])


if __name__ == "__main__":
    unittest.main()
