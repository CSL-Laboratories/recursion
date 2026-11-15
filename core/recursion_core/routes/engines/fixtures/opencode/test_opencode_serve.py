"""The OpenCode serve drive on driver.v2's gate (lane ENGINE-E3, the brief's step 5), against fakes only.

No OpenCode binary runs: a spy stands in for the launch path and gives back scripted processes; the abort is a
recorded callable, and ``http_abort`` is checked against a small HTTP server of the test's own on 127.0.0.1. Run by
name:

    PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_serve
"""

from __future__ import annotations

import http.server
import threading
import unittest
from pathlib import Path

from recursion_core.routes import contract as c
from recursion_core.routes.driver import Declaration, DriverInputError, DriverRefused, DriverStateError
from recursion_core.routes.engines import opencode as oc
from recursion_core.routes.launch import ReadTimeout

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
        for binary in ("", "a\nb", 5):
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
        with self.assertRaises((ValueError, DriverInputError)):
            driver.start(session(model="-x/y"), None, ENV)
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
        with self.assertRaises(ValueError):
            driver.finish_turn(handle, "0")
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


if __name__ == "__main__":
    unittest.main()
