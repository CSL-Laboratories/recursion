"""Seams between the pieces merged into the Nov 11 candidate.

Each test names a failure that is visible without a live engine, provider or network.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import gui as daemon_gui
from recursion_core.daemon import plugins
from recursion_core.plugin_security.tests.support import FakeGilProbe
from wsgiref.simple_server import WSGIRequestHandler


class RouteStartThreadTests(unittest.TestCase):
    def test_a_thread_that_does_not_start_does_not_keep_the_run(self) -> None:
        tmp = tempfile.mkdtemp(prefix="seam-start-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        routes = {"claude_code": {"route": {"engine": "claude_code"}, "deadline_s": 30.0, "binary": None}}
        starter = adapter.RouteStarter(None, routes, None, tmp, start_timeout_s=0.01)
        params = {"run_id": "run-1", "engine": "claude_code", "tenant": "t", "prompt": "p"}
        with patch("recursion_core.daemon.control_adapter.threading.Thread.start",
                   side_effect=RuntimeError("cannot start new thread")):
            with self.assertRaises(RuntimeError):
                starter(params)
        self.assertFalse(starter.active("run-1"))
        # A later start of the same run is not stuck on the one that never began.
        with patch("recursion_core.daemon.control_adapter.threading.Thread.start",
                   side_effect=RuntimeError("cannot start new thread")):
            with self.assertRaises(RuntimeError) as again:
                starter(params)
        self.assertNotIsInstance(again.exception, adapter.StartRefused)
        self.assertFalse(starter.active("run-1"))

    def test_an_interrupt_after_the_target_enters_keeps_the_run(self) -> None:
        # Thread.start can raise after the OS thread is inside the target (CPython waits on
        # _started after creating it). Dropping the run there lets a second start overlap.
        tmp = tempfile.mkdtemp(prefix="seam-start-live-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        routes = {"claude_code": {"route": {"engine": "claude_code"}, "deadline_s": 30.0, "binary": None}}
        entered = threading.Event()
        release = threading.Event()
        calls = []
        workers = []

        def stop():
            release.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(2)

        self.addCleanup(stop)

        class Service:
            def handle(self, request):
                calls.append(request["request_id"])
                entered.set()
                release.wait(5)
                return {"status": "ok"}

        starter = adapter.RouteStarter(Service(), routes, None, tmp, start_timeout_s=0.05)
        params = {"run_id": "run-1", "engine": "claude_code", "tenant": "t", "prompt": "p"}
        real_start = threading.Thread.start

        def start_then_interrupt(thread):
            # The Thread object RouteStarter built never publishes ident: the target runs, then
            # start() raises, which is the wait-after-create failure mode.
            worker = threading.Thread(target=thread._target, args=thread._args,
                                      kwargs=thread._kwargs or {}, daemon=True)
            workers.append(worker)
            real_start(worker)
            if not entered.wait(2):
                raise RuntimeError("target did not enter")
            raise KeyboardInterrupt

        with patch("recursion_core.daemon.control_adapter.threading.Thread.start", start_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                starter(params)
        self.assertTrue(starter.active("run-1"))
        with self.assertRaises(adapter.StartRefused) as caught:
            starter(params)
        self.assertEqual(caught.exception.code, "start_in_flight")
        self.assertEqual(calls, ["run-1"])


class OperatorReadThreadTests(unittest.TestCase):
    def test_a_thread_that_does_not_start_does_not_leave_reads_busy(self) -> None:
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._busy = False
        host._stuck = None
        host._operator_deadline_s = 5.0
        host._operator_turn_wait_s = 0.0
        host.degraded = {}
        host._gil = FakeGilProbe()
        host.health = type("Health", (), {"poison": staticmethod(lambda reason: None)})()
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        with patch("recursion_core.daemon.plugins.threading.Thread.start",
                   side_effect=RuntimeError("cannot start new thread")):
            with self.assertRaises(RuntimeError):
                host._call_operator("plug", lambda command: body, command)
        self.assertFalse(host._busy)
        self.assertEqual(host._call_operator("plug", lambda command: body, command), {"body": body})

    def test_an_interrupt_after_the_operator_enters_stays_busy(self) -> None:
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._busy = False
        host._stuck = None
        host._operator_deadline_s = 0.2
        host._operator_turn_wait_s = 0.0
        host.degraded = {}
        host._gil = FakeGilProbe()
        host.health = type("Health", (), {"poison": staticmethod(lambda reason: None)})()
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        entered = threading.Event()
        release = threading.Event()
        calls = []
        workers = []

        def stop():
            release.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(2)

        self.addCleanup(stop)

        def handler(got):
            calls.append(got["command"])
            entered.set()
            release.wait(5)
            return body

        real_start = threading.Thread.start

        def start_then_interrupt(thread):
            worker = threading.Thread(target=thread._target, args=thread._args,
                                      kwargs=thread._kwargs or {}, daemon=True)
            workers.append(worker)
            real_start(worker)
            if not entered.wait(2):
                raise RuntimeError("operator did not enter")
            raise KeyboardInterrupt

        with patch("recursion_core.daemon.plugins.threading.Thread.start", start_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                host._call_operator("plug", handler, command)
        self.assertTrue(host._busy)
        busy = host._call_operator("plug", handler, command)
        self.assertEqual(busy, (503, {
            "error": "unavailable: operator plugin busy with an earlier read; nothing was read"}))
        self.assertEqual(calls, ["operator.health.inspect"])


class StatusFileTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name == "nt" or not hasattr(os, "symlink"):
            self.skipTest("symlink")
        self.tmp = Path(tempfile.mkdtemp(prefix="seam-status-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.run = self.tmp / "runs" / "run-1"
        self.run.mkdir(parents=True)
        self.secret = self.tmp / "secret.json"
        self.secret.write_text('{"text":"LEAKED-SECRET"}\n', encoding="utf-8")
        self.ctrl = adapter.DaemonControl.__new__(adapter.DaemonControl)
        self.ctrl._state = self.tmp
        self.ctrl.starter = type("Starter", (), {"results": {}})()

    def test_stdout_status_does_not_follow_a_symlink(self) -> None:
        os.symlink(self.secret, self.run / "stdout.jsonl")
        got = adapter.DaemonControl._output(self.ctrl, "run-1", False)
        self.assertNotIn("LEAKED-SECRET", json.dumps(got))
        self.assertEqual(got.get("unavailable"), "OSError")

    def test_a_regular_stdout_file_is_still_returned(self) -> None:
        (self.run / "stdout.jsonl").write_text("hello\n", encoding="utf-8")
        got = adapter.DaemonControl._output(self.ctrl, "run-1", False)
        self.assertEqual(got["lines"], ["hello"])
        self.assertTrue(got["complete"])

    def test_result_status_does_not_follow_a_symlink(self) -> None:
        os.symlink(self.secret, self.run / "result.json")
        got = adapter.DaemonControl._result(self.ctrl, "run-1")
        self.assertIsNone(got)

    def test_a_fifo_with_no_writer_does_not_block_the_status_read(self) -> None:
        fifo = self.run / "stdout.jsonl"
        os.mkfifo(fifo)
        done = threading.Event()
        holder = {}

        def read():
            try:
                holder["value"] = adapter._read_regular(fifo)
            except BaseException as exc:  # noqa: BLE001 - the refusal is the result under test
                holder["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        if not done.wait(1.0):
            try:
                writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(writer)
            except OSError:
                pass
            done.wait(1.0)
            self.fail("reading a FIFO with no writer blocked in open")
        self.assertIsInstance(holder.get("error"), OSError)
        self.assertNotIn("value", holder)
        got = adapter.DaemonControl._output(self.ctrl, "run-1", False)
        self.assertEqual(got.get("unavailable"), "OSError")
        self.assertNotIn("lines", got)


class GuiAdmissionTests(unittest.TestCase):
    def _server(self):
        server = daemon_gui._Server(("127.0.0.1", 0), WSGIRequestHandler)
        self.addCleanup(server.server_close)
        return server

    def test_an_evicted_connection_is_not_answered_off_the_books(self) -> None:
        server = self._server()
        for _ in range(daemon_gui.MAX_REQUEST_THREADS):
            server._working[object()] = False
        evicted = object()
        self.assertFalse(server.headers_read(evicted, "GET", "/"))
        self.assertNotIn(evicted, server._working)
        self.assertNotIn(evicted, server._reading)

    def test_a_keep_alive_follow_up_stays_admitted(self) -> None:
        server = self._server()
        alive = object()
        server._working[alive] = False
        self.assertTrue(server.headers_read(alive, "GET", "/v1/routes"))
        self.assertIn(alive, server._working)

    def test_a_connection_still_reading_is_admitted_under_the_cap(self) -> None:
        server = self._server()
        fresh = object()
        server._reading[fresh] = None
        self.assertTrue(server.headers_read(fresh, "GET", "/"))
        self.assertIn(fresh, server._working)
        self.assertNotIn(fresh, server._reading)
