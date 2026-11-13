"""Seams between the pieces merged into the Nov 11 candidate.

Each test names a failure that is visible without a live engine, provider or network.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
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

    def _start_that_leaves_limbo_before_the_probe(self):
        """Park before ``_bootstrap``, raise ``KeyboardInterrupt`` from ``_started.wait``, then let
        that OS thread run past ``del _limbo[self]`` before the probe acquires the limbo lock.

        ``ident`` and ``is_alive`` are still unset when ``start`` raises. The probe's first take of
        the limbo lock is what releases the parked thread, and it is held back until the thread has
        left ``_limbo`` with ``ident`` and ``_started`` set. A probe that read those before the lock
        then sees an empty limbo and reports that no OS thread exists.
        """
        if not hasattr(threading, "_limbo") or not hasattr(threading, "_active_limbo_lock"):
            self.fail("this runtime has no thread limbo map; the gap cannot be scheduled")
        started = []
        release = threading.Event()
        real_start = threading.Thread.start
        real_new = threading._start_new_thread
        real_lock = threading._active_limbo_lock
        probe_thread = threading.current_thread()
        state = {"armed": False, "injected": False, "left": False, "error": None}

        class _LimboLock:
            def acquire(self, blocking=True, timeout=-1):
                if (state["armed"] and not state["injected"]
                        and threading.current_thread() is probe_thread):
                    state["injected"] = True
                    release.set()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if not real_lock.acquire(True, 0.05):
                            continue
                        try:
                            thread = started[0]
                            left = (thread not in threading._limbo and thread.ident is not None
                                    and thread._started.is_set())
                        finally:
                            real_lock.release()
                        if left:
                            state["left"] = True
                            break
                    else:
                        state["error"] = "parked thread did not leave limbo before the probe lock"
                if timeout == -1:
                    return real_lock.acquire(blocking)
                return real_lock.acquire(blocking, timeout)

            def release(self):
                real_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_exc):
                self.release()

        proxy = _LimboLock()

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
                with real_lock:
                    handed_off = thread in threading._limbo
                if thread.ident is not None or thread.is_alive() or not handed_off:
                    raise RuntimeError("thread became visible before the interrupt")
                state["armed"] = True
                raise KeyboardInterrupt

            thread._started.wait = wait_and_interrupt
            try:
                with patch.object(threading, "_start_new_thread", delayed):
                    real_start(thread)
            finally:
                thread._started.wait = original_wait

        def finish():
            release.set()
            if threading._active_limbo_lock is proxy:
                threading._active_limbo_lock = real_lock
            for thread in started:
                thread.join(2)

        threading._active_limbo_lock = proxy
        self.addCleanup(finish)
        return start, started, finish, state


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

    def test_a_thread_that_leaves_limbo_before_the_probe_does_not_overlap(self) -> None:
        # ident is unset when start() raises, and the OS thread leaves _limbo before the probe
        # takes the limbo lock. The run stays reserved: one Thread.start, and the handler does
        # not overlap a second start.
        tmp = tempfile.mkdtemp(prefix="seam-start-limbo-gap-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        routes = {"claude_code": {"route": {"engine": "claude_code"}, "deadline_s": 30.0, "binary": None}}
        calls = []
        release_handler = threading.Event()
        entered = threading.Event()
        self.addCleanup(release_handler.set)

        class Service:
            def handle(self, request):
                calls.append(request["request_id"])
                entered.set()
                release_handler.wait(5)
                return {"status": "ok"}

        starter = adapter.RouteStarter(Service(), routes, None, tmp, start_timeout_s=0.05)
        params = {"run_id": "run-1", "engine": "claude_code", "tenant": "t", "prompt": "p"}
        start, started, finish, state = self._start_that_leaves_limbo_before_the_probe()
        with patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                starter(params)
            self.assertTrue(state["left"] and state["injected"], state["error"])
            self.assertIsNotNone(started[0].ident)
            with threading._active_limbo_lock:
                self.assertNotIn(started[0], threading._limbo)
            self.assertTrue(entered.wait(2), "the handed-off worker did not reach the handler")
            second = None
            try:
                starter(params)
            except adapter.StartRefused as exc:
                second = exc.code
            except BaseException as exc:  # noqa: BLE001 - the overlapping start's exception is the result
                second = type(exc).__name__
            self.assertEqual(
                (len(started), second, calls, starter.active("run-1")),
                (1, "start_in_flight", ["run-1"], True))
        release_handler.set()
        finish()
        self.assertEqual(calls, ["run-1"])
        self.assertFalse(starter.active("run-1"))


class OperatorReadThreadTests(_SeamCase):
    def test_a_thread_that_does_not_start_does_not_leave_reads_busy(self) -> None:
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        host._turn = threading.Condition()
        host._busy = False
        host._stuck = None
        host._stuck_token = None
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
        host._stuck_token = None
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
        host._stuck_token = None
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

    def test_a_thread_that_leaves_limbo_before_the_probe_does_not_overlap(self) -> None:
        # Same gap as the route start: the OS thread leaves _limbo before the probe takes the
        # limbo lock, while ident was unset at the interrupt. One Thread.start, no second read.
        host = self._host()
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        calls = []
        release_handler = threading.Event()
        entered = threading.Event()
        self.addCleanup(release_handler.set)

        def handler(got):
            calls.append(got["command"])
            entered.set()
            release_handler.wait(5)
            return body

        start, started, finish, state = self._start_that_leaves_limbo_before_the_probe()
        busy = (503, {"error": "unavailable: operator plugin busy with an earlier read; nothing was read"})
        with patch.object(threading.Thread, "start", start):
            with self.assertRaises(KeyboardInterrupt):
                host._call_operator("plug", handler, command)
            self.assertTrue(state["left"] and state["injected"], state["error"])
            self.assertIsNotNone(started[0].ident)
            with threading._active_limbo_lock:
                self.assertNotIn(started[0], threading._limbo)
            self.assertTrue(entered.wait(2), "the handed-off worker did not reach the handler")
            second = None
            try:
                second = host._call_operator("plug", handler, command)
            except BaseException as exc:  # noqa: BLE001 - the overlapping read's exception is the result
                second = type(exc).__name__
            self.assertEqual((len(started), second, calls, host._busy), (1, busy, ["operator.health.inspect"], True))
        release_handler.set()
        finish()
        self.assertEqual(calls, ["operator.health.inspect"])
        self.assertFalse(host._busy)

    def test_a_stale_worker_does_not_clear_a_newer_stuck_mark(self) -> None:
        # The first handler is still blocked when a later call becomes the stuck one. The first
        # worker's finally must not clear that newer mark (it used to match on plugin id alone,
        # and the log then said reads go to the plugin again).
        host = self._host()
        host._operator_deadline_s = 0.15
        body = {"api_version": "o7.operator.v0", "status_code": "ok"}
        command = {"command": "operator.health.inspect"}
        release_first, release_second = threading.Event(), threading.Event()
        first_in, second_in = threading.Event(), threading.Event()
        self.addCleanup(release_first.set)
        self.addCleanup(release_second.set)
        idents = []
        count = {"n": 0}

        def handler(_got):
            idents.append(threading.current_thread())
            n = count["n"]
            count["n"] = n + 1
            if n == 0:
                first_in.set()
                release_first.wait(5)
            else:
                second_in.set()
                release_second.wait(5)
            return body

        holder = {}

        def first():
            try:
                holder["first"] = host._call_operator("plug", handler, command)
            except BaseException as exc:  # noqa: BLE001 - recorded so the assertion can see it
                holder["exc"] = exc

        caller = threading.Thread(target=first, daemon=True)
        caller.start()
        self.assertTrue(first_in.wait(2), "the first handler did not enter")
        caller.join(2)
        self.assertNotIn("exc", holder)
        self.assertEqual(holder["first"][0], 504)
        self.assertEqual(host._stuck, "plug")
        # The first OS thread is still inside the handler. Drop the reservation the way a false
        # "no thread" rollback does, so a second call can run and take the stuck mark.
        with host._turn:
            host._busy = False
            host._busy_token = None
            host._stuck = None
            host._stuck_token = None
        with self.assertLogs("recursion_core.daemon.plugins", level="ERROR"):
            second = host._call_operator("plug", handler, command)
        self.assertEqual(second[0], 504)
        self.assertTrue(second_in.is_set())
        self.assertEqual(host._stuck, "plug")
        release_first.set()
        idents[0].join(2)
        self.assertFalse(idents[0].is_alive())
        self.assertEqual(host._stuck, "plug", "a stale worker cleared the newer stuck mark")
        self.assertTrue(idents[1].is_alive(), "the newer handler is still blocked")
        self.assertIn("a call has still not returned", host._degraded_fields("plug")["degraded"])
        release_second.set()
        idents[1].join(2)
        self.assertFalse(host._busy)
        self.assertIsNone(host._stuck)


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
