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

# CPython's private handoff map. Absent on a runtime that does not publish it.
_LIMBO_ATTRS = ("_limbo", "_active_limbo_lock")


class _ThreadingHiding:
    """``threading`` stand-in whose named private attributes are absent.

    The real module is left alone, so an OS thread can still be created and parked in the
    real ``_limbo``. The probe's ``getattr`` sees the hidden names as missing.
    """

    def __init__(self, real, hidden):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_hidden", frozenset(hidden))

    def __getattr__(self, name):
        if name in object.__getattribute__(self, "_hidden"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_real"), name)


class _SeamCase(unittest.TestCase):
    def _start_that_interrupts_after_the_os_thread(self):
        """``Thread.start`` that raises ``KeyboardInterrupt`` after the OS thread exists.

        The new thread is parked before ``_bootstrap``, so ``ident`` is unset, ``is_alive`` is
        false, and the target has not entered. The thread is in the real ``_limbo``. ``finish``
        lets that thread run the target.
        """
        started = []
        release = threading.Event()
        real_start = threading.Thread.start
        real_new = threading._start_new_thread

        def start(thread):
            started.append(thread)
            os_ready = threading.Event()

            def delayed(func, args, kwargs=None):
                def wrapped():
                    os_ready.set()
                    if release.wait(5):
                        func(*args)
                return real_new(wrapped, ())

            original_wait = thread._started.wait

            def wait_and_interrupt(timeout=None):
                if not os_ready.wait(2):
                    raise RuntimeError("os thread was not created")
                with threading._active_limbo_lock:
                    handed_off = thread in threading._limbo
                if thread.ident is not None or thread.is_alive() or not handed_off:
                    raise RuntimeError("thread became visible before the interrupt")
                raise KeyboardInterrupt

            thread._started.wait = wait_and_interrupt
            try:
                with patch.object(threading, "_start_new_thread", delayed):
                    real_start(thread)
            finally:
                thread._started.wait = original_wait

        def finish():
            release.set()
            for thread in started:
                if thread._started.wait(2):
                    thread.join(2)

        self.addCleanup(finish)
        return start, started, finish


class RouteStartThreadTests(_SeamCase):
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

    def test_each_missing_limbo_attribute_is_treated_as_published(self) -> None:
        thread = threading.Thread(target=lambda: None, daemon=True)
        missing = []
        for name in _LIMBO_ATTRS:
            hidden = _ThreadingHiding(threading, (name,))
            with patch.object(adapter, "threading", hidden):
                if adapter._os_thread_published(thread) is not True:
                    missing.append(name)
        self.assertEqual(missing, [])

    def test_hidden_limbo_keeps_the_run_in_flight_and_refuses_a_second_start(self) -> None:
        # KeyboardInterrupt from Thread.start after the OS thread exists, while ident is unset
        # and the target has not entered. With the private handoff map hidden, the run must
        # stay reserved: a second start is start_in_flight and does not call Thread.start.
        tmp = tempfile.mkdtemp(prefix="seam-start-hidden-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        routes = {"claude_code": {"route": {"engine": "claude_code"}, "deadline_s": 30.0, "binary": None}}
        calls = []

        class Service:
            def handle(self, request):
                calls.append(request["request_id"])
                return {"status": "ok"}

        starter = adapter.RouteStarter(Service(), routes, None, tmp, start_timeout_s=0.05)
        params = {"run_id": "run-1", "engine": "claude_code", "tenant": "t", "prompt": "p"}
        start, started, finish = self._start_that_interrupts_after_the_os_thread()
        hidden = _ThreadingHiding(threading, _LIMBO_ATTRS)
        with patch.object(adapter, "threading", hidden), patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                starter(params)
            self.assertTrue(starter.active("run-1"), "a handed-off thread must stay start_in_flight")
            self.assertEqual(calls, [])
            self.assertIsNone(started[0].ident)
            self.assertEqual(len(started), 1)
            with self.assertRaises(adapter.StartRefused) as caught:
                starter(params)
            self.assertEqual(caught.exception.code, "start_in_flight")
            self.assertEqual(len(started), 1, "a second start must not call Thread.start")
        finish()
        self.assertEqual(calls, ["run-1"])
        self.assertFalse(starter.active("run-1"))

    def test_the_first_worker_finally_does_not_clear_a_newer_reservation(self) -> None:
        tmp = tempfile.mkdtemp(prefix="seam-start-reserve-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        routes = {"claude_code": {"route": {"engine": "claude_code"}, "deadline_s": 30.0, "binary": None}}
        calls = []

        class Service:
            def handle(self, request):
                calls.append(request["request_id"])
                return {"status": "ok"}

        starter = adapter.RouteStarter(Service(), routes, None, tmp, start_timeout_s=0.05)
        params = {"run_id": "run-1", "engine": "claude_code", "tenant": "t", "prompt": "p"}
        start, started, finish = self._start_that_interrupts_after_the_os_thread()
        hidden = _ThreadingHiding(threading, _LIMBO_ATTRS)
        with patch.object(adapter, "threading", hidden), patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                starter(params)
            self.assertIsNone(started[0].ident)
            self.assertEqual(len(started), 1)
        newer = adapter.RunContext("run-1")
        with starter._lock:
            starter._active["run-1"] = newer
        finish()
        self.assertEqual(calls, ["run-1"])
        with starter._lock:
            self.assertIs(starter._active.get("run-1"), newer)


class OperatorReadThreadTests(_SeamCase):
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

    def _host(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._busy = False
        host._stuck = None
        host._operator_deadline_s = 5.0
        host._operator_turn_wait_s = 0.0
        host.degraded = {}
        host._gil = FakeGilProbe()
        host.health = type("Health", (), {"poison": staticmethod(lambda reason: None)})()
        return host

    def test_each_missing_limbo_attribute_is_treated_as_published(self) -> None:
        thread = threading.Thread(target=lambda: None, daemon=True)
        missing = []
        for name in _LIMBO_ATTRS:
            hidden = _ThreadingHiding(threading, (name,))
            with patch.object(plugins, "threading", hidden):
                if plugins._os_thread_published(thread) is not True:
                    missing.append(name)
        self.assertEqual(missing, [])

    def test_hidden_limbo_keeps_the_operator_busy_and_refuses_a_second_read(self) -> None:
        host = self._host()
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        calls = []

        def handler(got):
            calls.append(got["command"])
            return body

        start, started, finish = self._start_that_interrupts_after_the_os_thread()
        hidden = _ThreadingHiding(threading, _LIMBO_ATTRS)
        with patch.object(plugins, "threading", hidden), patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                host._call_operator("plug", handler, command)
            self.assertTrue(host._busy, "a handed-off operator read must stay busy")
            self.assertEqual(calls, [])
            self.assertIsNone(started[0].ident)
            self.assertEqual(len(started), 1)
            refused = host._call_operator("plug", handler, command)
            self.assertEqual(refused, (503, {
                "error": "unavailable: operator plugin busy with an earlier read; nothing was read"}))
            self.assertEqual(len(started), 1, "a second read must not call Thread.start")
        finish()
        self.assertEqual(calls, ["operator.health.inspect"])
        self.assertFalse(host._busy)

    def test_the_first_operator_finally_does_not_clear_a_newer_read(self) -> None:
        host = self._host()
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        calls = []

        def handler(got):
            calls.append(got["command"])
            return body

        start, started, finish = self._start_that_interrupts_after_the_os_thread()
        hidden = _ThreadingHiding(threading, _LIMBO_ATTRS)
        with patch.object(plugins, "threading", hidden), patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                host._call_operator("plug", handler, command)
            self.assertIsNone(started[0].ident)
            self.assertEqual(len(started), 1)
        newer = object()
        with host._turn:
            host._busy = True
            host._busy_token = newer
        finish()
        self.assertEqual(calls, ["operator.health.inspect"])
        self.assertTrue(host._busy)
        self.assertIs(host._busy_token, newer)


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
