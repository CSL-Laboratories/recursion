"""The OpenCode serve drive on driver.v2's gate (lane ENGINE-E3, the brief's step 5), against fakes only.

No OpenCode binary runs: a spy stands in for the launch path and gives back scripted processes; the abort is a
recorded callable, and ``http_abort`` is checked against a small HTTP server of the test's own on 127.0.0.1. Run by
name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_serve
"""

from __future__ import annotations

import http.server
import inspect
import os
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes.driver import Declaration, DriverInputError, DriverRefused, DriverStateError
from recursion_core.routes.engines import opencode as oc
from recursion_core.routes.launch import ReadTimeout
from recursion_core.subscription_routes import SubscriptionRouteRefused

HERE = Path(__file__).resolve().parent
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/var/lib/recursion/homes/opencode", "OPENAI_API_KEY": "sk-made-up"}
PORT = 41234
URL = "http://127.0.0.1:41234"
READY = "opencode server listening on http://127.0.0.1:41234\n"


class Process:
    """A scripted process: ``lines`` is what read_line gives, in order; a ReadTimeout instance in it is raised."""

    def __init__(self, lines=()) -> None:
        self.lines = list(lines)
        self.sent: list[str] = []
        self.closes = 0
        self.terminated = 0
        self.timeouts: list[float] = []

    def send(self, text: str) -> None:
        assert self.closes == 0, "send after close_stdin"
        self.sent.append(text)

    def close_stdin(self) -> None:
        self.closes += 1

    def read_line(self, timeout: float):
        self.timeouts.append(timeout)
        if not self.lines:
            return None
        line = self.lines.pop(0)
        if isinstance(line, BaseException):
            raise line
        return line

    def terminate(self) -> None:
        self.terminated += 1


class Spy:
    """Spawns the server first (with ``server_lines``), then one Process per turn."""

    def __init__(self, server_lines=(READY,)) -> None:
        self.server_lines = list(server_lines)
        self.spawns: list[tuple[list, dict]] = []
        self.processes: list[Process] = []

    def __call__(self, argv: list, env: dict) -> Process:
        self.spawns.append((list(argv), dict(env)))
        process = Process(self.server_lines if argv[1] == "serve" else ())
        self.processes.append(process)
        return process


class Clock:
    def __init__(self, now: float = 100.0, step: float = 0.0) -> None:
        self.now, self.step = now, step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def session(model="openai/gpt-5.5") -> dict:
    return {"session_id": "ses-e3serve", "run_id": "run-e3serve",
            "route": {"engine": "opencode", "model": model, "connection": {"kind": "api_key", "id": "k1"}}}


def message(turn="turn-1", delivery="dlv-1", text="Run the tests.") -> dict:
    return {"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}


def fixture_session_id(name: str) -> str:
    p = oc.Parser(connection_id="api_key:k1", model=None, wall=lambda: 0.0, clock=lambda: 0.0)
    for line in (HERE / name).read_bytes().splitlines(keepends=True):
        p.feed(line)
    return p.engine_session_id


class Base(unittest.TestCase):
    def make(self, spy=None, **kwargs) -> oc.ServeDriver:
        self.spy = spy or Spy()
        self.aborts: list[tuple[str, str]] = []
        answer = kwargs.pop("abort_answer", True)

        def abort(url, sid):
            self.aborts.append((url, sid))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        kwargs.setdefault("abort", abort)
        kwargs.setdefault("port", lambda: PORT)
        kwargs.setdefault("wall", lambda: 1794614400.0)
        kwargs.setdefault("clock", Clock())
        return oc.ServeDriver(self.spy, **kwargs)

    def replay(self, driver, handle, name: str) -> list[dict]:
        drafts = []
        for line in (HERE / name).read_text("utf-8").splitlines(keepends=True):
            drafts.extend(driver.parse(handle, line))
        return drafts

    def changed(self, driver, handle) -> int:
        anomalies = driver._worker(handle)["turn"]["parser"].anomalies
        return len([a for a in anomalies if a.endswith("conversation_changed")])

    def turn_argv(self, index: int) -> list:
        return self.spy.spawns[index][0]


class PartsTests(unittest.TestCase):
    def test_the_declaration_is_driver_v2s_record(self):
        record = oc.serve_declaration()
        self.assertEqual(record["flags"], {"multi_turn": True, "mid_turn_input": False, "interrupt": True,
                                           "resume": True, "fork": False, "permission_bridge": False,
                                           "typed_limits": True, "usage": True})
        self.assertEqual((record["engine"], record["driver"], record["input_ack"]), ("opencode", 2, False))
        self.assertEqual(Declaration.from_record(record), oc.ServeDriver.declaration)
        record["flags"]["fork"] = True
        self.assertFalse(oc.serve_declaration()["flags"]["fork"])   # a copy each time

    def test_the_server_binds_the_loopback_address_on_the_picked_port(self):
        self.assertEqual(oc.serve_argv(PORT), ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "41234"])
        self.assertEqual(oc.serve_argv(1, binary="/opt/oc/bin/opencode")[0], "/opt/oc/bin/opencode")
        self.assertEqual(oc.serve_argv(65535)[-1], "65535")
        for port in (0, 65536, -1, True, "80", 80.0, None):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    oc.serve_argv(port)
                with self.assertRaises(ValueError):
                    oc.server_url(port)
        for binary in ("", "a\nb", 5, " ", "-x"):
            with self.subTest(binary=binary):
                with self.assertRaises(ValueError):
                    oc.serve_argv(PORT, binary=binary)

    def test_only_the_readiness_line_for_the_picked_port_on_loopback_counts(self):
        self.assertEqual(oc.listening_url(READY, PORT), URL)
        self.assertEqual(oc.listening_url(READY.rstrip("\n"), PORT), URL)
        self.assertEqual(oc.listening_url(READY.rstrip("\n") + "\r\n", PORT), URL)
        for text in ("opencode server listening on http://0.0.0.0:41234\n",
                     "opencode server listening on http://localhost:41234\n",
                     "opencode server listening on http://127.0.0.1:41235\n",
                     "opencode server listening on http://127.0.0.1:041234\n",
                     "opencode server listening on https://127.0.0.1:41234\n",
                     "opencode server listening on http://127.0.0.1:41234/x\n",
                     "note: opencode server listening on http://127.0.0.1:41234\n",
                     "OPENCODE SERVER LISTENING ON http://127.0.0.1:41234\n",
                     None, b"opencode server listening on http://127.0.0.1:41234\n"):
            with self.subTest(text=text):
                self.assertIsNone(oc.listening_url(text, PORT))

    def test_a_turn_attaches_to_the_loopback_server_only(self):
        self.assertEqual(oc.attach_argv(URL, "openai/gpt-5.5"),
                         ["opencode", "run", "--format", "json", "--model", "openai/gpt-5.5", "--attach", URL])
        self.assertEqual(oc.attach_argv(URL, None, session="ses_abc"),
                         ["opencode", "run", "--format", "json", "--attach", URL, "--session", "ses_abc"])
        self.assertEqual(oc.attach_argv("http://127.0.0.1:65535", None)[-1], "http://127.0.0.1:65535")
        for url in ("http://10.0.0.1:41234", "http://localhost:41234", "https://127.0.0.1:41234",
                    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:041234",
                    "http://127.0.0.1", URL + "/", URL + "\n", None):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    oc.attach_argv(url, None)
        for sid in ("", "-x", "--model", "a\nb", "a\tb", "x" * 257, 5, "a" + chr(0xD800)):
            with self.subTest(session=sid):
                with self.assertRaises(ValueError):
                    oc.attach_argv(URL, None, session=sid)
        self.assertEqual(oc.attach_argv(URL, None, session="x" * 256)[-1], "x" * 256)

    def test_a_picked_port_is_a_loopback_port(self):
        self.assertTrue(oc._port_ok(oc.pick_port()))


class StartTests(Base):
    def test_start_runs_the_server_and_waits_for_its_line(self):
        spy = Spy(["starting\n", "opencode server listening on http://127.0.0.1:41235\n", READY])
        driver = self.make(spy)
        handle = driver.start(session(), None, ENV)
        self.assertEqual(spy.spawns, [(["opencode", "serve", "--hostname", "127.0.0.1", "--port", "41234"], ENV)])
        self.assertEqual(spy.processes[0].terminated, 0)
        self.assertIsNone(handle.engine_session_id)
        self.assertIsNone(handle.live_turn)

    def test_a_server_that_gives_no_readiness_line_is_ended_and_start_refuses(self):
        for lines in ([], ["starting\n"], ["opencode server listening on http://0.0.0.0:41234\n"],
                      [ReadTimeout()], [ReadTimeout(), READY]):
            with self.subTest(lines=lines):
                spy = Spy(lines)
                driver = self.make(spy)
                with self.assertRaises(DriverStateError):
                    driver.start(session(), None, ENV)
                self.assertEqual(len(spy.spawns), 1)
                self.assertEqual(spy.processes[0].terminated, 1)

    def test_the_wait_is_bounded_by_ready_s_on_the_clock(self):
        spy = Spy(["log\n"] * 50 + [READY])
        driver = self.make(spy, clock=Clock(step=1.0), ready_s=5.0)
        with self.assertRaises(DriverStateError):
            driver.start(session(), None, ENV)
        server = spy.processes[0]
        self.assertEqual(server.terminated, 1)
        self.assertEqual(server.timeouts, [4.0, 3.0, 2.0, 1.0])   # each read waits only what is left

    def test_a_read_that_raises_ends_the_server_too(self):
        spy = Spy([KeyboardInterrupt()])
        driver = self.make(spy)
        with self.assertRaises(KeyboardInterrupt):
            driver.start(session(), None, ENV)
        self.assertEqual(spy.processes[0].terminated, 1)

    def test_a_route_the_parser_refuses_starts_nothing(self):
        driver = self.make()
        bad = session()
        bad["route"]["connection"]["id"] = "a\nb"
        with self.assertRaises((ValueError, DriverInputError)):
            driver.start(bad, None, ENV)
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            driver.start(session(model="-x/y"), None, ENV)
        self.assertEqual(caught.exception.cause, "model_malformed")
        self.assertEqual(self.spy.spawns, [])

    def test_a_connection_the_gate_takes_and_the_parser_refuses_starts_nothing(self):
        driver = self.make()
        bad = session()
        bad["route"]["connection"]["id"] = "k" + chr(0xD800)   # one line, so the gate takes it
        with self.assertRaises(ValueError):
            driver.start(bad, None, ENV)
        self.assertEqual(self.spy.spawns, [])

    def test_the_constructor_refuses_what_it_cannot_use(self):
        for kwargs in ({"ready_s": 0}, {"ready_s": float("inf")}, {"ready_s": True}, {"port": 5},
                       {"abort": None}, {"binary": ""}, {"clock": 1.0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    oc.ServeDriver(Spy(), **kwargs)
        with self.assertRaises(ValueError):
            oc.ServeDriver(None)


class TurnTests(Base):
    def test_two_turns_reach_the_same_conversation(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        self.assertEqual(self.turn_argv(1), ["opencode", "run", "--format", "json", "--model", "openai/gpt-5.5",
                                             "--attach", URL])
        self.assertEqual(self.spy.spawns[1][1], ENV)
        first = self.spy.processes[1]
        self.assertEqual((first.sent, first.closes), (["Run the tests."], 1))
        drafts = self.replay(driver, handle, "synthetic-v1-tool-use.jsonl")
        accepted = [d["payload"] for d in drafts if d["type"] == "input.accepted"]
        self.assertEqual(accepted, [{"delivery_id": "dlv-1", "turn_id": "turn-1", "how": "new_turn"}])
        sid = fixture_session_id("synthetic-v1-tool-use.jsonl")
        self.assertEqual(handle.engine_session_id, sid)
        self.assertEqual(self.changed(driver, handle), 0)
        (result,) = driver.finish_turn(handle, 0)
        self.assertEqual(result, {"type": "result", "payload": {"code": None, "detail": ""}})
        self.assertIsNone(handle.live_turn)
        driver.send(handle, message(turn="turn-2", delivery="dlv-2", text="And the docs."))
        self.assertEqual(self.turn_argv(2)[-4:], ["--attach", URL, "--session", sid])
        self.assertEqual(self.spy.processes[2].sent, ["And the docs."])
        drafts = self.replay(driver, handle, "synthetic-v1-tool-use.jsonl")
        accepted = [d["payload"] for d in drafts if d["type"] == "input.accepted"]
        self.assertEqual(accepted, [{"delivery_id": "dlv-2", "turn_id": "turn-2", "how": "new_turn"}])
        self.assertEqual(handle.ignored_acceptances, 0)
        self.assertEqual(self.changed(driver, handle), 0)
        driver.finish_turn(handle, 0)
        self.assertEqual(len([s for s in self.spy.spawns if s[0][1] == "serve"]), 1)   # one server for both turns
        self.assertEqual(self.spy.processes[0].terminated, 0)

    def test_the_binary_option_is_argv0_of_the_server_and_of_every_turn(self):
        driver = self.make(binary="/opt/oc/bin/opencode")
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        self.assertEqual([argv[0] for argv, _ in self.spy.spawns], ["/opt/oc/bin/opencode"] * 2)

    def test_only_a_step_start_accepts_the_message(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        for line in ("not json\n", '{"type":"text","timestamp":1,"sessionID":"ses_x","part":{"text":"hi"}}\n'):
            self.assertNotIn("input.accepted", [d["type"] for d in driver.parse(handle, line)])
        start = '{"type":"step_start","timestamp":1,"sessionID":"ses_x","part":{"type":"step-start"}}\n'
        self.assertIn("input.accepted", [d["type"] for d in driver.parse(handle, start)])

    def test_the_launched_model_is_held_to_the_guard_before_anything_starts(self):
        for model, cause in (("Claude-Agent/sonnet", "claude_subscription_provider"),
                             ("claude_max/x", "claude_subscription_provider"), ("gpt", "model_malformed")):
            with self.subTest(model):
                driver = self.make()
                with self.assertRaises(SubscriptionRouteRefused) as caught:
                    driver.start(session(model=model), None, ENV)
                self.assertEqual(caught.exception.cause, cause)
                with self.assertRaises(SubscriptionRouteRefused):
                    driver.resume("ses_abc", session(model=model), None, ENV)
                self.assertEqual(self.spy.spawns, [])

    def test_a_turn_accepts_its_message_once(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        start = '{"type":"step_start","timestamp":1,"sessionID":"ses_x","part":{"type":"step-start"}}\n'
        drafts = driver.parse(handle, start) + driver.parse(handle, start)
        self.assertEqual([d["type"] for d in drafts].count("input.accepted"), 1)
        self.assertEqual(handle.ignored_acceptances, 0)

    def test_a_turn_whose_process_cannot_start_ends_and_the_next_turn_runs(self):
        spy = Spy()
        real = spy.__call__
        failures = [OSError("no such file")]

        def spawn(argv, env):
            if argv[1] == "run" and failures:
                raise failures.pop()
            return real(argv, env)

        driver = self.make(spy)
        driver._spawn = spawn
        handle = driver.start(session(), None, ENV)
        with self.assertRaises(OSError):
            driver.send(handle, message())
        self.assertIsNone(handle.live_turn)
        with self.assertRaises(DriverStateError):
            driver.interrupt(handle, "turn-1")
        driver.send(handle, message(turn="turn-2", delivery="dlv-2"))   # still the first turn that ran
        self.assertEqual(spy.spawns[1][0][-2:], ["--attach", URL])

    def test_a_resumed_handle_passes_the_session_on_its_first_turn(self):
        driver = self.make()
        handle = driver.resume("ses_abc", session(model=None), None, ENV)
        self.assertEqual(handle.engine_session_id, "ses_abc")
        driver.send(handle, message())
        self.assertEqual(self.turn_argv(1), ["opencode", "run", "--format", "json", "--attach", URL,
                                             "--session", "ses_abc"])

    def test_a_resume_with_an_id_that_cannot_be_an_argv_value_starts_nothing(self):
        driver = self.make()
        for sid in ("-x", "a\tb"):
            with self.subTest(sid=sid):
                with self.assertRaises(DriverInputError):
                    driver.resume(sid, session(), None, ENV)
        self.assertEqual(self.spy.spawns, [])

    def test_a_later_turn_without_a_conversation_id_is_refused_and_ended(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        self.assertEqual(driver.finish_turn(handle, None)[0]["payload"]["code"], c.ENGINE_EXITED)
        with self.assertRaises(DriverStateError):
            driver.send(handle, message(turn="turn-2", delivery="dlv-2"))
        self.assertEqual(len(self.spy.spawns), 2)   # the server and the first turn only
        self.assertIsNone(handle.live_turn)

    def test_what_the_serve_drive_lacks_is_refused_with_nothing_started(self):
        driver = self.make()
        with self.assertRaises(DriverRefused):
            driver.fork("ses_abc", "turn-1")
        handle = driver.start(session(), None, ENV)
        with self.assertRaises(DriverRefused) as caught:
            driver.send(handle, {"kind": "approval_answer", "approval_id": "apr-1", "decision": "approve"})
        self.assertEqual(caught.exception.flag, "permission_bridge")
        driver.send(handle, message())
        with self.assertRaises(DriverRefused) as caught:
            driver.send(handle, message(delivery="dlv-2"))
        self.assertEqual(caught.exception.flag, "mid_turn_input")
        self.assertEqual(len(self.spy.spawns), 2)
        self.assertEqual(self.spy.processes[1].sent, ["Run the tests."])

    def test_a_typed_429_in_a_turn_is_a_limit_for_that_connection_and_the_turns_result(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        drafts = self.replay(driver, handle, "synthetic-v1-limit-429.jsonl")
        (limit,) = [d["payload"] for d in drafts if d["type"] == "limit"]
        self.assertEqual((limit["connection_id"], limit["reset_at"]), ("api_key:k1", "2026-11-14T00:02:00.000Z"))
        self.assertEqual(driver.finish_turn(handle, 1)[0]["payload"]["code"], c.PLAN_LIMIT)

    def test_each_turn_has_its_own_parser(self):
        driver = self.make()
        handle = driver.resume("ses_abc", session(), None, ENV)
        driver.send(handle, message())
        self.replay(driver, handle, "synthetic-v1-limit-429.jsonl")
        self.assertEqual(driver.finish_turn(handle, 1)[0]["payload"]["code"], c.PLAN_LIMIT)
        driver.send(handle, message(turn="turn-2", delivery="dlv-2"))
        self.assertEqual(driver.finish_turn(handle, 0)[0]["payload"]["code"], None)   # the old limit stays behind

    def test_a_turn_that_reports_another_conversation_is_recorded(self):
        driver = self.make()
        handle = driver.resume("ses_other", session(), None, ENV)
        driver.send(handle, message())
        self.replay(driver, handle, "synthetic-v1-tool-use.jsonl")
        self.assertEqual(handle.engine_session_id, "ses_other")
        self.assertEqual(self.changed(driver, handle), 1)

    def test_parse_and_finish_without_a_turn_are_refused(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        with self.assertRaises(DriverStateError):
            driver.parse(handle, '{"type":"text"}\n')
        with self.assertRaises(DriverStateError):
            driver.finish_turn(handle, 0)

    def test_an_exit_code_the_parser_refuses_leaves_the_turn_in_progress(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        for bad in ("0", True, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(DriverInputError):
                    driver.finish_turn(handle, bad)
                self.assertEqual(handle.live_turn, "turn-1")
        driver.finish_turn(handle, 0)
        self.assertIsNone(handle.live_turn)


class InterruptAndCloseTests(Base):
    def started_turn(self, **kwargs):
        driver = self.make(**kwargs)
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        return driver, handle

    def test_an_interrupt_asks_the_server_to_abort_the_session(self):
        driver, handle = self.started_turn()
        self.replay(driver, handle, "synthetic-v1-tool-use.jsonl")
        driver.interrupt(handle, "turn-1")
        self.assertEqual(self.aborts, [(URL, handle.engine_session_id)])
        self.assertEqual(self.spy.processes[1].terminated, 0)
        self.assertEqual(handle.live_turn, "turn-1")          # the turn ends through finish_turn
        driver.finish_turn(handle, 0)
        self.assertIsNone(handle.live_turn)

    def test_without_an_answer_from_the_server_the_turns_process_is_ended(self):
        for answer in (False, None, 1, OSError("refused"), ValueError("x")):
            with self.subTest(answer=answer):
                driver, handle = self.started_turn(abort_answer=answer)
                self.replay(driver, handle, "synthetic-v1-tool-use.jsonl")
                driver.interrupt(handle, "turn-1")
                self.assertEqual(len(self.aborts), 1)
                self.assertEqual(self.spy.processes[1].terminated, 1)
                self.assertEqual(self.spy.processes[0].terminated, 0)

    def test_before_any_conversation_id_the_turns_process_is_ended_without_an_ask(self):
        driver, handle = self.started_turn()
        driver.interrupt(handle, "turn-1")
        self.assertEqual(self.aborts, [])
        self.assertEqual(self.spy.processes[1].terminated, 1)

    def test_an_interrupt_of_another_turn_is_refused_by_the_gate(self):
        driver, handle = self.started_turn()
        with self.assertRaises(DriverStateError):
            driver.interrupt(handle, "turn-9")
        self.assertEqual((self.aborts, self.spy.processes[1].terminated), ([], 0))

    def test_a_close_between_turns_stops_the_server(self):
        driver, handle = self.started_turn()
        driver.finish_turn(handle, 0)
        driver.send(handle, {"kind": "close"})
        self.assertEqual(self.spy.processes[0].terminated, 1)
        with self.assertRaises(DriverStateError):
            driver.finish_turn(handle, 0)

    def test_a_close_during_a_turn_stops_the_server_once_the_turn_is_finished(self):
        driver, handle = self.started_turn()
        driver.send(handle, {"kind": "close"})
        self.assertEqual(self.spy.processes[0].terminated, 0)
        self.assertEqual(self.spy.processes[1].closes, 1)
        driver.finish_turn(handle, 0)
        self.assertEqual((self.spy.processes[0].terminated, self.spy.processes[1].terminated), (1, 0))

    def test_release_stops_both_processes_once(self):
        driver, handle = self.started_turn()
        driver.release(handle)
        driver.release(handle)
        self.assertEqual((self.spy.processes[0].terminated, self.spy.processes[1].terminated), (1, 1))
        with self.assertRaises(DriverStateError):
            driver.parse(handle, '{"type":"text"}\n')

    def test_two_handles_have_two_servers(self):
        driver = self.make()
        one = driver.start(session(), None, ENV)
        two = driver.start(session(), None, ENV)
        driver.release(one)
        self.assertEqual((self.spy.processes[0].terminated, self.spy.processes[1].terminated), (1, 0))
        driver.send(two, message())
        self.assertEqual(self.turn_argv(2)[-2:], ["--attach", URL])


class Recorder(http.server.BaseHTTPRequestHandler):
    status = 200
    seen: list = []

    def do_POST(self):  # noqa: N802 (the handler's own name)
        length = int(self.headers.get("Content-Length") or 0)
        self.seen.append((self.command, self.path, self.rfile.read(length)))
        self.send_response(self.status)
        if 300 <= self.status < 400:
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/followed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST  # noqa: N815 (a followed redirect would arrive as a GET)

    def log_message(self, *args):
        pass


class HttpAbortTests(unittest.TestCase):
    def serve(self, status: int) -> str:
        handler = type("H", (Recorder,), {"status": status, "seen": []})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        self.handler = handler
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_abort_is_a_post_to_the_sessions_abort_path(self):
        url = self.serve(200)
        self.assertTrue(oc.http_abort(url, "ses_a/b c"))
        self.assertEqual(self.handler.seen, [("POST", "/session/ses_a%2Fb%20c/abort", b"")])

    def test_an_answer_that_is_not_2xx_is_no_abort(self):
        for status in (404, 500):
            with self.subTest(status=status):
                self.assertFalse(oc.http_abort(self.serve(status), "ses_x"))

    def test_a_redirect_is_not_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertFalse(oc.http_abort(self.serve(status), "ses_x"))
                self.assertEqual([seen[:2] for seen in self.handler.seen], [("POST", "/session/ses_x/abort")])

    def test_nothing_listening_is_no_abort(self):
        url = self.serve(200)
        port = int(url.rsplit(":", 1)[1])
        self.doCleanups()   # the server is gone; its port is closed
        self.assertFalse(oc.http_abort(f"http://127.0.0.1:{port}", "ses_x", timeout=5.0))

    def test_only_the_loopback_server_is_asked(self):
        for url in ("http://10.0.0.1:80", "http://localhost:80", "https://127.0.0.1:80", None):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    oc.http_abort(url, "ses_x")
        with self.assertRaises(ValueError):
            oc.http_abort("http://127.0.0.1:80", "-x")


def step_start(sid: str, part_type: str = "step-start") -> str:
    return ('{"type":"step_start","timestamp":1,"sessionID":"%s","part":{"type":"%s"}}\n' % (sid, part_type))


class OnceRaisingLock:
    """A lock whose first entry raises ``KeyboardInterrupt``: an interruption landing exactly there."""

    def __init__(self) -> None:
        self.lock, self.armed = threading.Lock(), True

    def __enter__(self):
        if self.armed:
            self.armed = False
            raise KeyboardInterrupt("one interruption at the first entry")
        return self.lock.__enter__()

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


class HookLock:
    """The driver's lock, counting entries: before entry ``n`` it calls ``hooks[n]`` once (a call landing there)."""

    def __init__(self, lock, hooks: dict) -> None:
        self.lock, self.hooks, self.entries = lock, dict(hooks), 0

    def __enter__(self):
        self.entries += 1
        hook = self.hooks.pop(self.entries, None)
        if hook is not None:
            hook()
        return self.lock.__enter__()

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


class ReleaseAndRaceTests(Base):
    """#421's first round: release is terminal and owns what it stops; nothing is left running or reserved."""

    def started_turn(self, **kwargs):
        driver = self.make(**kwargs)
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        return driver, handle

    def test_a_release_while_a_turns_spawn_runs_terminates_the_late_process_unwritten(self):
        entered, go = threading.Event(), threading.Event()
        spy = Spy()

        def spawn(argv, env):
            process = spy(argv, env)
            if argv[1] == "run":
                entered.set()
                if not go.wait(30):   # a watchdog only: the test sets it
                    raise AssertionError("the test never let the spawn return")
            return process

        driver = self.make(spy)
        driver._spawn = spawn
        handle = driver.start(session(), None, ENV)
        errors = []

        def send():
            try:
                driver.send(handle, message())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertTrue(entered.wait(30))
            driver.release(handle)   # does not wait for the spawn
            self.assertEqual(spy.processes[0].terminated, 1)
            self.assertIn(id(handle), driver._workers)   # still owed the process the spawn will give back
        finally:
            go.set()
            thread.join(30)
        self.assertFalse(thread.is_alive())
        server, turn = spy.processes
        self.assertEqual((turn.terminated, turn.sent, turn.closes), (1, [], 0))
        self.assertEqual([type(e) for e in errors], [DriverStateError])
        self.assertEqual(driver._workers, {})
        self.assertIsNone(handle.live_turn)
        driver.release(handle)
        self.assertEqual((server.terminated, turn.terminated), (1, 1))

    def test_a_released_handle_refuses_every_later_call(self):
        driver, handle = self.started_turn()
        driver.release(handle)
        self.assertIsNone(handle.live_turn)   # not left busy: the next message is refused as released
        for call in (lambda: driver.send(handle, message(turn="turn-2", delivery="dlv-2")),
                     lambda: driver.parse(handle, '{"type":"text"}\n'), lambda: driver.finish_turn(handle, 0),
                     lambda: driver.interrupt(handle, "turn-3"), lambda: driver.send(handle, {"kind": "close"})):
            with self.assertRaises(DriverStateError):
                call()
            self.assertIsNone(handle.live_turn)
        self.assertEqual(len(self.spy.spawns), 2)
        self.assertEqual([p.terminated for p in self.spy.processes], [1, 1])

    def test_a_release_between_the_registration_and_the_write_leaves_the_prompt_unwritten(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver._lock = HookLock(driver._lock, {4: lambda: driver.release(handle)})   # the 4th entry in send
        with self.assertRaises(DriverStateError):
            driver.send(handle, message())
        server, turn = self.spy.processes
        self.assertEqual((turn.sent, turn.terminated, server.terminated), ([], 1, 1))
        self.assertIsNone(handle.live_turn)
        self.assertEqual(driver._workers, {})

    def test_a_release_just_before_the_spawn_starts_nothing(self):
        driver = self.make()
        handle = driver.start(session(), None, ENV)
        driver._lock = HookLock(driver._lock, {2: lambda: driver.release(handle)})   # the 2nd entry in send
        with self.assertRaises(DriverStateError):
            driver.send(handle, message())
        self.assertEqual(len(self.spy.spawns), 1)   # the server only
        self.assertEqual(self.spy.processes[0].terminated, 1)
        self.assertIsNone(handle.live_turn)
        self.assertEqual(driver._workers, {})

    def test_an_interruption_in_a_termination_keeps_every_process_not_yet_ended_owed(self):
        driver, handle = self.started_turn()
        server, turn = self.spy.processes
        real, cuts = turn.terminate, [KeyboardInterrupt("cut short")]

        def terminate():
            if cuts:
                raise cuts.pop()
            real()

        turn.terminate = terminate
        with self.assertRaises(KeyboardInterrupt):
            driver.release(handle)
        self.assertEqual((turn.terminated, server.terminated), (0, 0))   # the server was not reached
        driver.release(handle)
        self.assertEqual((turn.terminated, server.terminated), (1, 1))
        self.assertEqual(driver._workers, {})

    def test_one_termination_that_raises_leaves_the_other_tried_and_itself_owed(self):
        for failing in (0, 1):   # the server's, then the turn's process
            with self.subTest(failing=failing):
                driver, handle = self.started_turn()
                processes = self.spy.processes
                victim, real = processes[failing], processes[failing].terminate
                failures = [OSError("synthetic: cannot terminate")]

                def terminate(victim=victim, real=real, failures=failures):
                    if failures:
                        raise failures.pop()
                    real()

                victim.terminate = terminate
                with self.assertRaises(OSError):
                    driver.release(handle)
                self.assertEqual([p.terminated for p in processes], [0 if p is victim else 1 for p in processes])
                self.assertIn(id(handle), driver._workers)
                for call, args in ((driver.send, (message(turn="turn-2", delivery="dlv-2"),)),
                                   (driver.parse, ('{"type":"text"}\n',)), (driver.finish_turn, (0,)),
                                   (driver.send, ({"kind": "close"},))):
                    with self.assertRaises(DriverStateError):
                        call(handle, *args)
                self.assertEqual([p.terminated for p in processes], [0 if p is victim else 1 for p in processes])
                driver.release(handle)   # tries the owed one again, and only it
                self.assertEqual([p.terminated for p in processes], [1, 1])
                self.assertEqual(driver._workers, {})

    def test_an_interruption_at_the_handles_registration_terminates_the_server(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                driver = self.make()
                driver._lock = OnceRaisingLock()
                with self.assertRaises(KeyboardInterrupt):
                    if resume:
                        driver.resume("ses_abc", session(), None, ENV)
                    else:
                        driver.start(session(), None, ENV)
                self.assertEqual(self.spy.processes[0].terminated, 1)
                self.assertEqual(driver._workers, {})

    def test_a_server_whose_termination_fails_at_start_leaves_the_first_error(self):
        spy = Spy([])
        driver = self.make(spy)
        failed = []

        def spawn(argv, env):
            process = spy(argv, env)

            def terminate():
                failed.append(1)
                raise OSError("synthetic: cannot terminate")

            process.terminate = terminate
            return process

        driver._spawn = spawn
        with self.assertRaises(DriverStateError) as caught:
            driver.start(session(), None, ENV)
        self.assertEqual(failed, [1])
        self.assertTrue(any("cannot terminate" in note for note in caught.exception.__notes__))

    def test_a_reported_session_id_a_later_turn_cannot_pass_is_not_adopted(self):
        for sid in ("-invalid-session", ".."):
            with self.subTest(sid=sid):
                driver, handle = self.started_turn()
                drafts = driver.parse(handle, step_start(sid))
                self.assertIsNone(handle.engine_session_id)
                anomalies = driver._worker(handle)["turn"]["parser"].anomalies
                unusable = [a for a in anomalies if a.endswith("session_id_unusable")]
                self.assertEqual(unusable, ["line 1: session_id_unusable"])
                self.assertIn("input.accepted", [d["type"] for d in drafts])   # the engine did take the message
                driver.finish_turn(handle, 0)
                with self.assertRaises(DriverStateError):
                    driver.send(handle, message(turn="turn-2", delivery="dlv-2"))
                self.assertIsNone(handle.live_turn)   # the refused turn is ended: the next call is not busy
                with self.assertRaises(DriverStateError):
                    driver.interrupt(handle, "turn-2")
                self.assertEqual(len(self.spy.spawns), 2)

    def test_a_turn_whose_preparation_raises_ends_and_spawns_nothing(self):
        for raised, expected in ((KeyboardInterrupt("cut short"), KeyboardInterrupt),
                                 (ValueError("session"), DriverStateError)):
            with self.subTest(raised=raised):
                driver = self.make()
                handle = driver.start(session(), None, ENV)
                with mock.patch.object(oc, "attach_argv", side_effect=raised):
                    with self.assertRaises(expected):
                        driver.send(handle, message())
                self.assertIsNone(handle.live_turn)
                self.assertEqual(len(self.spy.spawns), 1)
                driver.send(handle, message(turn="turn-2", delivery="dlv-2"))   # the first turn that runs
                self.assertEqual(self.spy.processes[1].sent, ["Run the tests."])

    def test_an_abort_cut_short_still_terminates_the_turn_and_the_same_exception_leaves(self):
        for raised in (KeyboardInterrupt("stop"), SystemExit(3), GeneratorExit()):
            with self.subTest(raised=type(raised).__name__):
                driver = self.make(abort_answer=raised)
                handle = driver.resume("ses_abc", session(), None, ENV)
                driver.send(handle, message())
                with self.assertRaises(type(raised)) as caught:
                    driver.interrupt(handle, "turn-1")
                self.assertIs(caught.exception, raised)
                self.assertEqual((len(self.aborts), self.spy.processes[1].terminated), (1, 1))
                self.assertEqual(self.spy.processes[0].terminated, 0)

    def test_an_abort_cut_short_whose_termination_also_fails_still_leaves_as_itself(self):
        driver = self.make(abort_answer=KeyboardInterrupt("stop"))
        handle = driver.resume("ses_abc", session(), None, ENV)
        driver.send(handle, message())

        def terminate():
            raise OSError("synthetic: cannot terminate")

        self.spy.processes[1].terminate = terminate
        with self.assertRaises(KeyboardInterrupt) as caught:
            driver.interrupt(handle, "turn-1")
        self.assertTrue(any("cannot terminate" in note for note in caught.exception.__notes__))

    def test_a_finish_between_the_gate_and_the_interrupt_is_a_state_error(self):
        driver, handle = self.started_turn()
        entered, go = threading.Event(), threading.Event()
        real = driver._interrupt
        errors = []

        def paused(h, turn_id):
            entered.set()
            if not go.wait(30):   # a watchdog only: the test sets it
                raise AssertionError("the test never let the interrupt go on")
            return real(h, turn_id)

        driver._interrupt = paused

        def interrupt():
            try:
                driver.interrupt(handle, "turn-1")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=interrupt)
        thread.start()
        try:
            self.assertTrue(entered.wait(30))   # past the gate's check that turn-1 is in progress
            driver.finish_turn(handle, 0)
        finally:
            go.set()
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual([type(e) for e in errors], [DriverStateError])
        self.assertEqual((self.aborts, self.spy.processes[1].terminated), ([], 0))

    def test_two_finishers_of_one_turn_give_one_result(self):
        driver, handle = self.started_turn()
        parser = driver._worker(handle)["turn"]["parser"]
        real, entered, go = parser.end, threading.Event(), threading.Event()
        results, errors = [], []

        def end(exit_code):
            if not entered.is_set():   # only the first finisher waits here
                entered.set()
                if not go.wait(30):   # a watchdog only: the test sets it
                    raise AssertionError("the test never let the finish go on")
            return real(exit_code)

        parser.end = end

        def finish():
            try:
                results.append(driver.finish_turn(handle, 0))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=finish)
        thread.start()
        try:
            self.assertTrue(entered.wait(30))
            with self.assertRaises(DriverStateError):
                driver.finish_turn(handle, 1)
            self.assertEqual(handle.live_turn, "turn-1")
        finally:
            go.set()
            thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual((errors, [r[0]["payload"]["code"] for r in results]), ([], [None]))
        self.assertIsNone(handle.live_turn)
        with self.assertRaises(DriverStateError):
            driver.finish_turn(handle, 0)

    def test_a_finish_interrupted_part_way_completes_the_same_turn_when_called_again(self):
        for after_the_outcome in (False, True):
            with self.subTest(after_the_outcome=after_the_outcome):
                driver, handle = self.started_turn()
                parser = driver._worker(handle)["turn"]["parser"]
                real_end, real_check, cuts = parser.end, oc.check_draft, [KeyboardInterrupt("cut short")]

                def end(exit_code, real_end=real_end, cuts=cuts, after=after_the_outcome):
                    if cuts and not after:
                        raise cuts.pop()
                    return real_end(exit_code)

                def check(draft, real_check=real_check, cuts=cuts, after=after_the_outcome):
                    if cuts and after:
                        raise cuts.pop()
                    return real_check(draft)

                parser.end = end
                with mock.patch.object(oc, "check_draft", check):
                    with self.assertRaises(KeyboardInterrupt):
                        driver.finish_turn(handle, 0)
                    self.assertEqual(handle.live_turn, "turn-1")
                    (result,) = driver.finish_turn(handle, 1)   # the outcome reached first stands
                self.assertEqual(result["payload"]["code"], c.ENGINE_EXITED if not after_the_outcome else None)
                self.assertIsNone(handle.live_turn)

    def test_a_prompt_write_that_raised_is_never_accepted(self):
        spy = Spy()

        def spawn(argv, env):
            process = spy(argv, env)
            if argv[1] == "run":
                def send(text):
                    raise BrokenPipeError("synthetic: the write failed")
                process.send = send
            return process

        driver = self.make(spy)
        driver._spawn = spawn
        handle = driver.start(session(), None, ENV)
        with self.assertRaises(BrokenPipeError):
            driver.send(handle, message())
        drafts = driver.parse(handle, step_start("ses_x"))
        self.assertNotIn("input.accepted", [d["type"] for d in drafts])
        driver.release(handle)
        self.assertEqual([p.terminated for p in spy.processes], [1, 1])

    def test_only_a_well_formed_step_start_accepts_the_message(self):
        driver, handle = self.started_turn()
        self.assertNotIn("input.accepted", [d["type"] for d in driver.parse(handle, step_start("ses_x", "text"))])
        self.assertIn("input.accepted", [d["type"] for d in driver.parse(handle, step_start("ses_x"))])

    def test_a_session_id_of_dots_is_never_an_argv_value_or_a_path(self):
        for sid in (".", ".."):
            with self.subTest(sid=sid):
                with self.assertRaises(ValueError):
                    oc.attach_argv(URL, None, session=sid)
                with self.assertRaises(ValueError):
                    oc.http_abort(URL, sid)
                with self.assertRaises(DriverInputError):
                    self.make().resume(sid, session(), None, ENV)
        self.assertEqual(oc.attach_argv(URL, None, session="...")[-1], "...")


class ProxyTests(unittest.TestCase):
    def test_the_proxy_environment_is_never_used(self):
        target_fixture, proxy_fixture = HttpAbortTests("test_nothing_listening_is_no_abort"), \
            HttpAbortTests("test_nothing_listening_is_no_abort")
        target, proxy = target_fixture.serve(200), proxy_fixture.serve(200)
        self.addCleanup(target_fixture.doCleanups)
        self.addCleanup(proxy_fixture.doCleanups)
        names = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
        with mock.patch.dict(os.environ, {**{n: proxy for n in names}, "no_proxy": "", "NO_PROXY": ""}):
            self.assertTrue(oc.http_abort(target, "ses_x"))
        self.assertEqual(target_fixture.handler.seen, [("POST", "/session/ses_x/abort", b"")])
        self.assertEqual(proxy_fixture.handler.seen, [])


def _cut_each_line(fn):
    """The line numbers of ``fn``'s body after its docstring: a test interrupts once before each of them."""
    lines, first = inspect.getsourcelines(fn)
    # A ``try:`` line is the one instruction of a try statement that its own handler does not cover (CPython 3.12
    # and 3.14): an interruption there is the window before the region, and is not walked.
    body = [first + i for i, text in enumerate(lines)
            if text.strip() and not text.strip().startswith(("#", "def ", "try:"))]
    doc = fn.__doc__ is not None
    if doc:   # skip the docstring's lines
        end = next(i for i, text in enumerate(lines) if i and text.rstrip().endswith('"""'))
        body = [n for n in body if n > first + end]
    return body


def _interrupt_before(fn, lineno):
    """A tracer that raises one KeyboardInterrupt in ``fn`` just before line ``lineno`` runs (bounded: once)."""
    fired = []

    def trace(frame, event, arg):
        if frame.f_code is fn.__code__:
            if event == "line" and frame.f_lineno == lineno and not fired:
                fired.append(lineno)
                raise KeyboardInterrupt("one synthetic interruption")
            return trace
        return None

    return trace, fired


class ServeRoundTwoTests(unittest.TestCase):
    """#421 round 2: astra C1-C7, sol 1-7, DeepSeek r2 N1-N3. Every wait is bounded; every process is a fake."""

    def make(self, spawn=None, **kw):
        spy = spawn or Spy()
        return oc.ServeDriver(spy, port=lambda: PORT, **kw), spy

    def begun(self, **kw):
        driver, spy = self.make(**kw)
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        return driver, spy, handle

    def run_cut(self, fn, lineno, call):
        trace, fired = _interrupt_before(fn, lineno)
        sys.settrace(trace)
        try:
            call()
        except KeyboardInterrupt:
            pass
        finally:
            sys.settrace(None)
        return fired

    # DeepSeek N1 / astra C1 / sol 1: a release after the last check waits for the write, then terminates
    def test_a_release_during_the_write_waits_for_it_and_then_terminates(self):
        driver, spy = self.make()
        handle = driver.start(session(), None, ENV)
        lines, first = inspect.getsourcelines(oc.ServeDriver._send)
        target = first + next(i for i, text in enumerate(lines) if 'process.send(item["text"])' in text)
        entered, go, released, out = threading.Event(), threading.Event(), threading.Event(), []

        def trace(frame, event, arg):
            if frame.f_code is oc.ServeDriver._send.__code__:
                if event == "line" and frame.f_lineno == target:
                    entered.set()
                    go.wait(5)
                return trace
            return None

        def send():
            sys.settrace(trace)
            try:
                driver.send(handle, message())
                out.append("normal")
            except BaseException as err:   # noqa: BLE001 (the kind is the answer)
                out.append(type(err).__name__)
            finally:
                sys.settrace(None)

        sender = threading.Thread(target=send, daemon=True)
        releaser = threading.Thread(target=lambda: (driver.release(handle), released.set()), daemon=True)
        sender.start()
        self.assertTrue(entered.wait(5))
        releaser.start()
        early = released.wait(0.2)
        go.set()
        sender.join(5)
        releaser.join(15)
        self.assertFalse(sender.is_alive() or releaser.is_alive())
        self.assertFalse(early)   # it waited for the write in progress
        self.assertEqual((out, spy.processes[1].sent, [p.terminated for p in spy.processes]),
                         (["normal"], ["Run the tests."], [1, 1]))
        self.assertEqual(driver._workers, {})

    # astra C2 / sol 2: an interruption anywhere in release or in its cleanup loses no process
    def test_one_interruption_anywhere_in_release_or_its_cleanup_loses_no_process(self):
        for fn in (oc.ServeDriver.release, oc.ServeDriver._clean):
            for lineno in _cut_each_line(fn):
                with self.subTest(fn=fn.__name__, line=lineno):
                    driver, spy, handle = self.begun()
                    fired = self.run_cut(fn, lineno, lambda d=driver, h=handle: d.release(h))
                    driver.release(handle)
                    if fired:
                        self.assertTrue(all(p.terminated >= 1 for p in spy.processes))
                        self.assertEqual(driver._workers, {})
                        self.assertIsNone(handle.live_turn)

    # astra C3 / sol 3 / DeepSeek N4: the server is owned from its spawn; a failed termination keeps an owner
    def test_a_start_that_fails_after_the_spawn_terminates_its_server_or_keeps_it_owed(self):
        for lineno in _cut_each_line(oc.ServeDriver._start):
            for resume in (False, True):
                with self.subTest(line=lineno, resume=resume):
                    driver, spy = self.make()
                    call = (lambda d=driver: d.resume("ses_old", session(), None, ENV)) if resume \
                        else (lambda d=driver: d.start(session(), None, ENV))
                    fired = self.run_cut(oc.ServeDriver._start, lineno, call)
                    if fired and spy.processes:
                        self.assertEqual(spy.processes[0].terminated, 1)
                        self.assertEqual(driver._workers, {})
        server = Process([])   # no readiness line, and a termination that fails once
        failures = [OSError("synthetic: not terminated")]

        def terminate():
            if failures:
                raise failures.pop()
            server.terminated += 1

        server.terminate = terminate
        driver, _ = self.make(lambda argv, env: server)
        with self.assertRaises(DriverStateError) as caught:
            driver.start(session(), None, ENV)
        self.assertTrue(caught.exception.__notes__)
        (worker,) = driver._workers.values()   # the failed cleanup keeps a reachable owner
        self.assertEqual(worker["live"], [server])
        driver._clean(worker)
        self.assertEqual((server.terminated, driver._workers), (1, {}))

    def test_an_interruption_at_the_registration_leaves_nothing_started(self):
        driver, spy = self.make()

        class Cut(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                raise KeyboardInterrupt("registered, then interrupted")

        driver._workers = Cut()
        with self.assertRaises(KeyboardInterrupt):
            driver.start(session(), None, ENV)
        self.assertTrue(all(p.terminated == 1 for p in spy.processes))
        self.assertEqual(dict(driver._workers), {})

    # astra C4 / sol 6: a finish interrupted before the gate's transition can be asked again
    def test_one_interruption_anywhere_in_finish_turn_leaves_it_retryable(self):
        for lineno in _cut_each_line(oc.ServeDriver.finish_turn):
            with self.subTest(line=lineno):
                driver, spy, handle = self.begun()
                fired = self.run_cut(oc.ServeDriver.finish_turn, lineno,
                                     lambda d=driver, h=handle: d.finish_turn(h, 0))
                if fired and handle.live_turn is not None:
                    self.assertIsNone(driver.finish_turn(handle, 0)[0]["payload"]["code"])
                self.assertIsNone(handle.live_turn)
                driver.release(handle)

    # astra C5 / sol 5 / DeepSeek N2: the original exception survives a cleanup that raises BaseException
    def test_the_original_exception_survives_a_cleanup_that_raises(self):
        for cleanup in (OSError("synthetic"), SystemExit("synthetic cleanup cancellation")):
            with self.subTest(cleanup=type(cleanup).__name__):
                original = KeyboardInterrupt("synthetic abort cancellation")

                def abort(url, sid, original=original):
                    raise original

                driver, spy = self.make(abort=abort)
                handle = driver.resume("ses_old", session(), None, ENV)
                driver.send(handle, message())

                def terminate(cleanup=cleanup):
                    raise cleanup

                real, spy.processes[1].terminate = spy.processes[1].terminate, terminate
                with self.assertRaises(KeyboardInterrupt) as caught:
                    driver.interrupt(handle, "turn-1")
                self.assertIs(caught.exception, original)
                self.assertTrue(original.__notes__)
                spy.processes[1].terminate = real
                driver.release(handle)
                self.assertEqual(driver._workers, {})

    # astra C6 / sol 5 / DeepSeek N3: a late process whose cleanup fails: DriverStateError, noted, still owed
    def test_a_late_cleanup_failure_is_noted_on_the_state_refusal(self):
        server, turn, holder = Process([READY]), Process(), {}
        failures = [OSError("synthetic: not terminated")]

        def terminate():
            if failures:
                raise failures.pop()
            turn.terminated += 1

        turn.terminate = terminate

        def spawn(argv, env):
            if argv[1] == "serve":
                return server
            holder["driver"].release(holder["handle"])   # released while the turn's spawn runs
            return turn

        driver, _ = self.make(spawn)
        holder["driver"], holder["handle"] = driver, driver.start(session(), None, ENV)
        with self.assertRaises(DriverStateError) as caught:
            driver.send(holder["handle"], message())
        self.assertTrue(caught.exception.__notes__)
        self.assertEqual(turn.sent, [])
        self.assertIn(id(holder["handle"]), driver._workers)
        driver.release(holder["handle"])
        self.assertEqual((server.terminated, turn.terminated, driver._workers), (1, 1, {}))

    # sol 4 / astra C7: an interruption anywhere in a turn's start leaves no live turn without a process
    def test_one_interruption_anywhere_in_send_leaves_no_turn_without_its_process(self):
        for lineno in _cut_each_line(oc.ServeDriver._send):
            with self.subTest(line=lineno):
                driver, spy = self.make()
                handle = driver.start(session(), None, ENV)
                fired = self.run_cut(oc.ServeDriver._send, lineno, lambda d=driver, h=handle: d.send(h, message()))
                if not fired:
                    driver.release(handle)
                    continue
                if handle.live_turn is not None:
                    self.assertIsNotNone(driver._worker(handle)["turn"]["process"])
                driver.release(handle)
                self.assertTrue(all(p.terminated == 1 for p in spy.processes))
                self.assertEqual(driver._workers, {})

    # astra C7 / sol 7: two cleaners at once keep a failing process reachable
    def test_two_cleaners_keep_a_failing_process_reachable(self):
        driver, spy, handle = self.begun()
        entered, go, errors, calls = threading.Event(), threading.Event(), [], []
        turn = spy.processes[1]
        real = turn.terminate

        def terminate():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                go.wait(5)
                raise OSError("synthetic: not terminated")
            real()

        turn.terminate = terminate

        def release():
            try:
                driver.release(handle)
            except BaseException as err:   # noqa: BLE001 (the kind is the answer)
                errors.append(type(err).__name__)

        first, second = threading.Thread(target=release, daemon=True), threading.Thread(target=release, daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        second.join(5)
        go.set()
        first.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, ["OSError"])
        driver.release(handle)
        self.assertEqual(([p.terminated for p in spy.processes], driver._workers), ([1, 1], {}))


class ServeReviewThreadTests(unittest.TestCase):
    """The Devin review threads on #421, and #414's two for the serve drive. Every wait is bounded."""

    def test_a_malformed_abort_reply_is_no_abort(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(5)
        self.addCleanup(srv.close)

        def answer():
            conn, _ = srv.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"no status line at all\r\n\r\n")

        replier = threading.Thread(target=answer, daemon=True)
        replier.start()
        self.assertIs(oc.http_abort(f"http://127.0.0.1:{srv.getsockname()[1]}", "ses_abc"), False)
        replier.join(5)

    def test_a_process_appended_while_a_cleaner_removes_another_stays_owned(self):
        driver = oc.ServeDriver(Spy(), port=lambda: PORT)
        handle = driver.start(session(), None, ENV)
        driver.send(handle, message())
        worker = driver._workers[id(handle)]
        late = Process()

        class Racing(list):   # the turn spawn's unlocked append lands after the cleaner read, before it writes
            fired = False

            def land(self):
                if not Racing.fired:
                    Racing.fired = True
                    list.append(self, late)

            def __setitem__(self, key, value):
                self.land()
                list.__setitem__(self, key, value)

            def __delitem__(self, key):
                self.land()
                list.__delitem__(self, key)

        worker["live"] = Racing(worker["live"])
        driver.finish_turn(handle, 0)          # takes the turn's own process off the list
        self.assertTrue(any(p is late for p in worker["live"]))
        driver.release(handle)
        self.assertEqual((late.terminated, driver._workers), (1, {}))

    def test_a_start_the_serve_drive_would_not_run_as_asked_is_refused(self):
        workspace = {"path": "/repo", "mode": "worktree", "base_ref": "main", "worktree_path": "/repo-wt"}
        codex = session()
        codex["route"] = dict(codex["route"], engine="codex")
        for start, ws, field in ((codex, None, "route.engine"), (session(), workspace, "workspace")):
            with self.subTest(field=field):
                spy = Spy()
                with self.assertRaises(DriverInputError) as caught:
                    oc.ServeDriver(spy, port=lambda: PORT).start(start, ws, ENV)
                self.assertEqual((caught.exception.field, spy.processes), (field, []))


if __name__ == "__main__":
    unittest.main()
