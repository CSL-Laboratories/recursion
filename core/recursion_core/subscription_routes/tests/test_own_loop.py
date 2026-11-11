"""2026-11-08 own-loop routes: Copilot on the user's own copilot CLI sign-in, Nous via Hermes's loopback proxy.

Fakes only: no engine, login store, provider, DNS or network is touched.
"""

from __future__ import annotations

import atexit
import ctypes
import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest import mock

import recursion_core.subscription_routes.own_loop as own_loop
from recursion_core.subscription_routes import (
    COPILOT_STRIP_ENV, OwnLoopRouteRefused, check_copilot_own_loop, check_nous_own_loop, nous_proxy_doctor,
)
from recursion_core.child_env import EnvRefused, build_child_env
from recursion_core.subscription_routes.validated_launch import _blocked_secret

FAKE = "fake-not-a-real-token"
BASE = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}
_ROUTE_HOME: str | None = None


def _route_home() -> str:
    """A private directory the Copilot probe can use as the route home."""

    global _ROUTE_HOME
    if _ROUTE_HOME is None:
        path = tempfile.mkdtemp(prefix="csl-probe-home-")
        os.chmod(path, 0o700)
        _ROUTE_HOME = path
        atexit.register(shutil.rmtree, path, True)
    return _ROUTE_HOME


def copilot(**extra: object) -> dict:
    return {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in", **extra}


def nous(**extra: object) -> dict:
    return {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
            "base_url": "http://127.0.0.1:8645/v1", **extra}


@unittest.skipUnless(hasattr(os, "waitid"), "waitid is not available")
class SigactionAbiTests(unittest.TestCase):
    def setUp(self) -> None:
        if not hasattr(own_loop.os, "waitid"):
            self.skipTest("waitid is not available")

    def test_an_unverified_sigaction_abi_refuses_without_calling_libc(self) -> None:
        def cdll(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("libc")

        real_sizeof = ctypes.sizeof

        def sizeof(obj: object) -> int:
            if obj is ctypes.c_void_p:
                return 4
            return real_sizeof(obj)  # type: ignore[arg-type]

        with mock.patch.object(own_loop.ctypes, "CDLL", side_effect=cdll):
            with mock.patch.object(own_loop.sys, "platform", "unknown"):
                self.assertTrue(own_loop._sigchld_auto_reaps())
            with mock.patch.object(own_loop.platform, "machine", return_value="mips"):
                self.assertTrue(own_loop._sigchld_auto_reaps())
            with mock.patch.object(own_loop.ctypes, "sizeof", side_effect=sizeof):
                self.assertTrue(own_loop._sigchld_auto_reaps())

    def test_a_musl_notes_file_is_not_musl_and_two_architectures_are_ambiguous(self) -> None:
        notes = b"1000-2000 r--p 00000000 00:00 0 /tmp/libc.musl-notes.txt\n"
        named_deleted = (
            b"1000-2000 r--p 00000000 08:01 123 "
            b"/tmp/ld-musl-x86_64.so.1 (deleted)\n"
        )
        both = (
            b"1000-2000 r-xp 00000000 00:00 0 /lib/ld-musl-x86_64.so.1\n"
            b"2000-3000 r-xp 00000000 00:00 0 /lib/ld-musl-aarch64.so.1\n"
        )
        exact = b"1000-2000 r-xp 00000000 00:00 0 /lib/ld-musl-x86_64.so.1\n"
        with mock.patch.object(own_loop.os, "confstr", return_value=None), \
                mock.patch.object(own_loop.sys, "platform", "linux"), \
                mock.patch.object(own_loop.platform, "machine", return_value="x86_64"), \
                mock.patch.object(own_loop.ctypes, "sizeof", return_value=8):
            with mock.patch("builtins.open", mock.mock_open(read_data=notes)):
                self.assertFalse(own_loop._sigaction_abi_supported())
            with mock.patch("builtins.open", mock.mock_open(read_data=named_deleted)):
                self.assertIsNone(own_loop._libc_family())
            with mock.patch("builtins.open", mock.mock_open(read_data=both)):
                self.assertFalse(own_loop._sigaction_abi_supported())
            with mock.patch("builtins.open", mock.mock_open(read_data=exact)):
                self.assertEqual(own_loop._libc_family(), "musl")
                self.assertTrue(own_loop._sigaction_abi_supported())

    def test_native_cases_skip_when_the_sigaction_abi_is_unverified(self) -> None:
        # Only the libc sigaction case skips. A pure mock case still runs.
        native = CopilotTests("test_sa_nocldwait_refuses_before_the_callable")
        pure = CopilotTests("test_pidfd_sender_order_is_stdlib_then_os_then_libc")
        with mock.patch.object(own_loop, "_probe_enforceable", return_value=True), \
                mock.patch.object(own_loop, "_sigaction_abi_supported", return_value=False):
            pure_result = unittest.TestResult()
            pure.run(pure_result)
            self.assertFalse(pure_result.skipped)
            self.assertTrue(pure_result.wasSuccessful())
            native_result = unittest.TestResult()
            native.run(native_result)
            self.assertTrue(native_result.skipped)
            self.assertFalse(native_result.errors)
            # A case that needs a successful probe skips too. It must not error on the real probe.
            stored = CopilotTests("test_stored_login_runs_with_every_token_variable_removed")
            stored_result = unittest.TestResult()
            stored.run(stored_result)
            self.assertTrue(stored_result.skipped)
            self.assertFalse(stored_result.errors)

    def test_an_unlistable_task_dir_is_another_thread(self) -> None:
        with mock.patch.object(own_loop.threading, "enumerate", return_value=[threading.current_thread()]), \
                mock.patch.object(own_loop.sys, "_current_frames", return_value={1: None}), \
                mock.patch.object(own_loop.os.path, "isdir", return_value=True), \
                mock.patch.object(own_loop.os, "listdir", side_effect=PermissionError("task")):
            self.assertTrue(own_loop._other_threads())
        with mock.patch.object(own_loop.threading, "enumerate", return_value=[threading.current_thread()]), \
                mock.patch.object(own_loop.sys, "_current_frames", return_value={1: None}), \
                mock.patch.object(own_loop.os.path, "isdir", return_value=False):
            self.assertFalse(own_loop._other_threads())

    def test_a_failed_mask_restore_refuses_a_login_line(self) -> None:
        with ExitStack() as stack:
            def patch(name: str, **kwargs: object) -> None:
                stack.enter_context(mock.patch.object(own_loop, name, **kwargs))

            patch("_sigchld_auto_reaps", return_value=False)
            patch("_other_threads", return_value=False)
            patch("_read_handshake", return_value=(123, "ok"))
            patch("_leader_state", return_value="running")
            patch("_write_ack")
            patch("_read_status", return_value=(b"T", True, False))
            patch("_stop_probe", return_value=False)
            patch("_reap_probe", return_value=False)
            patch("_close_fd")
            stack.enter_context(mock.patch.object(own_loop.os, "pipe", side_effect=[(10, 11), (12, 13)]))
            stack.enter_context(mock.patch.object(own_loop.os, "set_inheritable"))
            stack.enter_context(mock.patch.object(own_loop.os, "set_blocking"))
            stack.enter_context(mock.patch.object(own_loop.os, "fork", return_value=123))
            stack.enter_context(mock.patch.object(own_loop.os, "pidfd_open", return_value=14))
            stack.enter_context(mock.patch.object(own_loop.selectors, "DefaultSelector"))
            stack.enter_context(mock.patch.object(
                own_loop.signal, "pthread_sigmask", side_effect=[set(), OSError("restore failed")],
            ))
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                own_loop._probe_login_group(lambda: True, 1, None)
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertEqual(caught.exception.detail, "probe unenforceable")

    def test_write_sigaction_skips_before_it_loads_libc(self) -> None:
        def cdll(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("libc")

        with mock.patch.object(own_loop, "_sigaction_abi_supported", return_value=False), \
                mock.patch.object(ctypes, "CDLL", side_effect=cdll):
            with self.assertRaises(unittest.SkipTest):
                _write_sigaction(signal.SIGCHLD, b"\0" * _SIGACTION_BYTES)

    def test_a_pid_waitid_error_is_not_treated_as_reaped(self) -> None:
        with mock.patch.object(own_loop.os, "waitid", side_effect=OSError(errno.EINVAL, "waitid")):
            self.assertEqual(own_loop._pid_state(123456), "unknown")

    def test_a_pidfd_signal_error_still_signals_the_group(self) -> None:
        with mock.patch.object(own_loop, "_pidfd_send_signal", side_effect=OSError(errno.ENOSYS, "pidfd")), \
                mock.patch.object(own_loop, "_leader_state", return_value="zombie"), \
                mock.patch.object(own_loop.os, "killpg") as group, \
                mock.patch.object(own_loop.os, "getpgrp", return_value=20):
            self.assertTrue(own_loop._stop_probe(123456, 123456, 0, 1, 100))
        self.assertTrue(any(call.args[1] == signal.SIGKILL for call in group.call_args_list))


# Cases that never run a successful login probe. Everything else in CopilotTests skips unless
# the platform can both enforce the probe and read the sigaction layout.
_NO_NATIVE_PROBE = frozenset({
    "test_a_raw_thread_refuses_before_the_probe_can_deadlock",
    "test_sigchld_ignored_refuses_before_the_callable",
    "test_pidfd_send_prefers_the_stdlib_over_libc",
    "test_pidfd_sender_order_is_stdlib_then_os_then_libc",
    "test_an_unsafe_home_does_not_leave_a_copilot_directory",
    "test_a_nul_in_home_is_a_typed_refusal",
    "test_a_surrogate_in_home_is_a_typed_refusal",
    "test_an_inherited_or_overridden_surrogate_copilot_home_is_refused",
    "test_check_without_a_route_home_refuses",
    "test_a_held_lock_refuses_before_the_probe_can_deadlock",
    "test_route_may_not_pass_a_token",
    "test_other_methods_and_fields_refuse",
})


class CopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        if not own_loop._probe_enforceable():
            self.skipTest("login probe cannot be enforced on this platform")
        if self._testMethodName not in _NO_NATIVE_PROBE and not own_loop._sigaction_abi_supported():
            self.skipTest("login probe cannot be enforced on this platform")

    def refused(self, cause: str, route: object, env: dict | None = None, probe=lambda: True) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_copilot_own_loop(route, BASE if env is None else env, home=_route_home(), has_copilot_login=probe)
        self.assertEqual(ctx.exception.cause, cause)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_stored_login_runs_with_every_token_variable_removed(self) -> None:
        env = {**BASE, **{n: FAKE for n in COPILOT_STRIP_ENV}, "GH_HOST": "github.com", "GH_CONFIG_DIR": "/x"}
        d = check_copilot_own_loop(copilot(model="gpt-5"), env, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(COPILOT_STRIP_ENV) | {"GH_HOST", "GH_CONFIG_DIR"})
        self.assertIn("COPILOT_API_URL", d.removed_env)
        self.assertNotIn(FAKE, repr(d))

    def test_unrelated_provider_credentials_do_not_reach_the_copilot_child(self) -> None:
        # Devin r4505395174: the stored Copilot sign-in is the only credential this run uses.
        env = {**BASE, "ANTHROPIC_API_KEY": FAKE, "OPENAI_API_KEY": FAKE, "XAI_API_KEY": FAKE,
               "CLAUDE_CODE_OAUTH_TOKEN": FAKE}
        d = check_copilot_own_loop(copilot(), env, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(env) - set(BASE))
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_copilot_model_is_checked_and_carried(self) -> None:
        d = check_copilot_own_loop(copilot(model="gpt-5"), BASE, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(d.model, "gpt-5")
        self.assertEqual(check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True).model, "")
        for bad in ("", "has space", 7, "x" * 129, "line\nbreak"):
            with self.subTest(model=bad):
                self.refused("model_malformed", copilot(model=bad))

    def test_no_stored_login_refuses_and_does_not_fall_back_to_gh(self) -> None:
        self.refused("no_stored_copilot_login", copilot(), {**BASE, "GH_TOKEN": FAKE}, probe=lambda: False)

    def test_missing_or_failing_probe_refuses(self) -> None:
        self.refused("copilot_login_unknown", copilot(), probe=None)

        def boom() -> bool:
            raise OSError("keychain locked")
        self.refused("copilot_login_unknown", copilot(), probe=boom)
        self.refused("no_stored_copilot_login", copilot(), probe=lambda: "yes")

    def test_login_probe_systemexit_refuses(self) -> None:
        # SystemExit is a BaseException, so `except Exception` lets a probe sys.exit escape.
        def exits() -> bool:
            raise SystemExit("do-not-leak-this")

        try:
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=exits)
        except OwnLoopRouteRefused as exc:
            self.assertEqual(exc.cause, "copilot_login_unknown")
            self.assertEqual(exc.detail, "SystemExit")
            self.assertNotIn("do-not-leak-this", str(exc))
        except SystemExit:
            self.fail("SystemExit escaped the login probe")
        else:
            self.fail("probe SystemExit was ignored")

    def test_login_probe_deadline_refuses(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hang() -> bool:
            release.wait(3)
            return True

        started = time.monotonic()
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=hang, probe_deadline=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertIn("deadline", caught.exception.detail)
        kept = check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=0.5)
        self.assertEqual(kept.method, "copilot_cli_sign_in")
        for deadline in (0.05, 0.2):
            with self.subTest(deadline=deadline):
                decision = check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=deadline,
                )
                self.assertEqual(decision.method, "copilot_cli_sign_in")

    @unittest.skipUnless(hasattr(os, "fork") and hasattr(os, "killpg"), "POSIX process-group kill")
    def test_login_probe_deadline_kills_the_process_group(self) -> None:
        # A thread join returns while the probe and anything it forked keep running.
        # The deadline has to SIGKILL the probe's process group, including that child.
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "info")

            def hang() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()} {os.getpgrp()} {sleeper}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                time.sleep(30)
                return True

            def stop_leftovers() -> None:
                if not os.path.exists(info_path):
                    return
                with open(info_path, encoding="ascii") as handle:
                    parts = handle.read().split()
                if len(parts) != 3:
                    return
                for pid in (int(part) for part in (parts[0], parts[2])):
                    if pid == os.getpid():
                        continue
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass

            self.addCleanup(stop_leftovers)
            started = time.monotonic()
            try:
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=hang, probe_deadline=0.8)
            except TypeError:
                self.fail("login probe has no deadline, so a hung probe is left running")
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(caught.exception.cause, "copilot_login_unknown")
            self.assertIn("deadline", caught.exception.detail)
            self.assertTrue(os.path.exists(info_path), "probe did not record its process group")
            with open(info_path, encoding="ascii") as handle:
                probe_pid, pgrp, sleeper = (int(part) for part in handle.read().split())
            self.assertNotEqual(probe_pid, os.getpid())
            self.assertEqual(pgrp, probe_pid)
            self.assertNotEqual(pgrp, os.getpgrp())
            self.assertTrue(_process_not_running(probe_pid))
            self.assertTrue(_process_not_running(sleeper))

    def test_login_probe_with_more_than_1024_open_fds(self) -> None:
        # select() refuses an fd at or above FD_SETSIZE (1024). The pipe has to sit past that.
        # resource is POSIX-only. Importing it at module level breaks the Windows ci:full job,
        # which imports this file while collecting tests.
        if os.name != "posix":
            self.skipTest("POSIX rlimits are required")
        try:
            import resource
        except ImportError:
            self.skipTest("POSIX rlimits are required")
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 1280
        limit = hard if hard != resource.RLIM_INFINITY else target
        if soft < target:
            if limit < target:
                self.skipTest("nofile hard limit is below 1280")
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        opened: list[int] = []
        try:
            while True:
                descriptor = os.open("/dev/null", os.O_RDONLY)
                opened.append(descriptor)
                if descriptor >= 1024:
                    break
                if len(opened) > 4096:
                    self.fail("could not open a descriptor past FD_SETSIZE")
            decision = check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0)
        finally:
            for descriptor in opened:
                os.close(descriptor)
        self.assertEqual(decision.method, "copilot_cli_sign_in")

    def test_a_probe_that_returns_true_kills_the_child_it_forked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "child")

            def probe() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{sleeper}\n")
                return True

            def stop_leftover() -> None:
                if not os.path.exists(info_path):
                    return
                with open(info_path, encoding="ascii") as handle:
                    text = handle.read().strip()
                if not text:
                    return
                pid = int(text)
                if pid == os.getpid():
                    return
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            self.addCleanup(stop_leftover)
            decision = check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
            self.assertEqual(decision.method, "copilot_cli_sign_in")
            with open(info_path, encoding="ascii") as handle:
                sleeper = int(handle.read().strip())
            self.assertNotEqual(sleeper, os.getpid())
            self.assertTrue(_process_not_running(sleeper))

    def test_the_callable_waits_until_the_parent_records_the_group(self) -> None:
        marker = os.path.join(tempfile.mkdtemp(prefix="csl786-ack-"), "ran")
        self.addCleanup(lambda: os.path.isdir(os.path.dirname(marker)) and __import__("shutil").rmtree(os.path.dirname(marker), True))
        leaked: list[bool] = []
        real = own_loop._read_handshake

        def slow(selector, fd, pid, started, deadline):
            pgid, reason = real(selector, fd, pid, started, deadline)
            time.sleep(0.15)
            leaked.append(os.path.exists(marker))
            return pgid, reason

        def probe() -> bool:
            with open(marker, "w", encoding="ascii") as handle:
                handle.write("ran")
            return True

        with mock.patch.object(own_loop, "_read_handshake", slow):
            decision = check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
        self.assertEqual(decision.method, "copilot_cli_sign_in")
        self.assertEqual(leaked, [False])

    def test_an_interrupted_kill_still_reaches_the_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "child")

            def probe() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{sleeper}\n")
                return True

            real = os.killpg
            calls = {"term": 0}

            def flaky(pgid: int, sig: int):
                if sig == signal.SIGTERM and calls["term"] == 0:
                    calls["term"] += 1
                    raise InterruptedError
                return real(pgid, sig)

            self.addCleanup(lambda: _stop_recorded(info_path))
            with mock.patch.object(own_loop.os, "killpg", flaky):
                decision = check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
            self.assertEqual(decision.method, "copilot_cli_sign_in")
            with open(info_path, encoding="ascii") as handle:
                sleeper = int(handle.read().strip())
            self.assertTrue(_process_not_running(sleeper))

    def test_the_worker_is_signalled_before_it_is_reaped(self) -> None:
        order: list[str] = []
        real_wait = os.waitpid
        real_killpg = os.killpg

        def waitpid(pid: int, options: int):
            order.append("wait")
            return real_wait(pid, options)

        def killpg(pgid: int, sig: int):
            if sig != 0:
                order.append("kill")
            return real_killpg(pgid, sig)

        with mock.patch.object(own_loop.os, "waitpid", waitpid), mock.patch.object(own_loop.os, "killpg", killpg):
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0)
        kills = [index for index, item in enumerate(order) if item == "kill"]
        waits = [index for index, item in enumerate(order) if item == "wait"]
        self.assertTrue(kills)
        self.assertTrue(waits)
        # Every group signal finishes before the first reap, not only the first signal.
        self.assertLess(max(kills), min(waits))

    def test_cleanup_is_the_original_process_group_not_the_session(self) -> None:
        # setpgid(0, 0) leaves the original process group and stays in the session. That child is not signalled.
        text = own_loop._stop_probe.__doc__ or ""
        self.assertIn("process group", text)
        self.assertIn("setpgid", text)
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "kids")

            def probe() -> bool:
                ready_r, ready_w = os.pipe()
                stayed = os.fork()
                if stayed == 0:
                    os.close(ready_r)
                    os.close(ready_w)
                    time.sleep(30)
                    os._exit(0)
                escaped = os.fork()
                if escaped == 0:
                    os.close(ready_r)
                    os.setpgid(0, 0)
                    os.write(ready_w, b"1")
                    os.close(ready_w)
                    time.sleep(30)
                    os._exit(0)
                os.close(ready_w)
                os.read(ready_r, 1)
                os.close(ready_r)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{stayed} {escaped}\n")
                return True

            self.addCleanup(lambda: _stop_recorded(info_path))
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
            with open(info_path, encoding="ascii") as handle:
                stayed, escaped = (int(part) for part in handle.read().split())
            self.assertTrue(_process_not_running(stayed))
            self.assertFalse(_process_not_running(escaped))
            os.kill(escaped, signal.SIGKILL)

    def test_a_raw_thread_refuses_before_the_probe_can_deadlock(self) -> None:
        # threading.enumerate misses a thread started with _thread. Fork would copy its lock.
        import _thread
        lock = _thread.allocate_lock()
        started = _thread.allocate_lock()
        release = _thread.allocate_lock()
        lock.acquire()
        started.acquire()
        release.acquire()
        done = _thread.allocate_lock()
        done.acquire()

        def holder() -> None:
            started.release()
            lock.acquire()
            lock.release()
            done.release()

        _thread.start_new_thread(holder, ())
        self.assertTrue(started.acquire(timeout=2))

        def finish() -> None:
            if lock.locked():
                lock.release()
            done.acquire(True, 2)

        self.addCleanup(finish)
        self.addCleanup(release.release)
        ran: list[str] = []

        def probe() -> bool:
            ran.append("ran")
            lock.acquire()
            return True

        started_at = time.monotonic()
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=0.4)
        self.assertEqual(caught.exception.detail, "probe threaded")
        self.assertLess(time.monotonic() - started_at, 0.3)
        self.assertEqual(ran, [])

    def test_interrupt_after_fork_reaps_the_worker_and_closes_its_pipes(self) -> None:
        # The interrupt lands after fork has returned the pid, and before that pidfd is stored.
        # Cleanup signals that identified worker. It does not scan the process list.
        real_fork = os.fork
        before = _open_fds()
        children = _direct_children()

        def fork() -> int:
            return real_fork()

        def pidfd_open(pid: int, flags: int = 0) -> int:
            raise KeyboardInterrupt

        self.addCleanup(_kill_new_children, children)
        with self.assertRaises(KeyboardInterrupt), \
                mock.patch.object(own_loop.os, "fork", fork), \
                mock.patch.object(own_loop.os, "pidfd_open", pidfd_open):
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and _direct_children() - children:
            time.sleep(0.01)
        self.assertEqual(set(), _direct_children() - children)
        self.assertEqual(before, _open_fds())

    def test_interrupt_during_cleanup_still_signals_the_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "child")

            def probe() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{sleeper}\n")
                return True

            real = os.killpg
            seen = {"term": 0}

            def interrupt_term(pgid: int, sig: int):
                if sig == signal.SIGTERM and seen["term"] == 0:
                    seen["term"] += 1
                    raise KeyboardInterrupt
                return real(pgid, sig)

            self.addCleanup(lambda: _stop_recorded(info_path))
            with mock.patch.object(own_loop.os, "killpg", interrupt_term):
                try:
                    check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
                except KeyboardInterrupt:
                    pass
            with open(info_path, encoding="ascii") as handle:
                sleeper = int(handle.read().strip())
            self.assertTrue(_process_not_running(sleeper))

    def test_a_pipe_failure_does_not_kill_an_existing_child(self) -> None:
        # An EMFILE from os.pipe happens before any worker exists. Cleanup must not SIGKILL
        # direct children the caller already had.
        child = os.fork()
        if child == 0:
            time.sleep(60)
            os._exit(0)

        def stop() -> None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass

        self.addCleanup(stop)

        def boom() -> tuple[int, int]:
            raise OSError(errno.EMFILE, "too many open files")

        with mock.patch.object(own_loop.os, "pipe", boom):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0,
                )
        self.assertEqual(caught.exception.detail, "probe failed")
        os.kill(child, 0)

    def test_a_reaping_handler_still_signals_a_same_group_descendant(self) -> None:
        # A SIGCHLD handler that waitpids must not collect the leader before the group is
        # signalled. The descendant shares that group and has to die.
        events: list[str] = []
        real_killpg = os.killpg

        def killpg(pgid: int, sig: int):
            if sig != 0:
                events.append("killpg")
            return real_killpg(pgid, sig)

        def note_reap(signum: int, frame: object) -> None:
            while True:
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return
                if pid == 0:
                    return

        previous = signal.signal(signal.SIGCHLD, note_reap)
        self.addCleanup(signal.signal, signal.SIGCHLD, previous)
        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "child")

            def probe() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{sleeper}\n")
                return True

            self.addCleanup(lambda: _stop_recorded(info_path))
            with mock.patch.object(own_loop.os, "killpg", killpg):
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0,
                )
            with open(info_path, encoding="ascii") as handle:
                sleeper = int(handle.read().strip())
            self.assertIn("killpg", events)
            self.assertTrue(_process_not_running(sleeper))

    def test_sigchld_ignored_refuses_before_the_callable(self) -> None:
        # SIG_IGN auto-reaps even while SIGCHLD is blocked. The callable must not run, and
        # killpg must not see a process-group id the kernel has already freed.
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGCHLD, previous)
        children = _direct_children()
        self.addCleanup(_kill_new_children, children)
        ran: list[str] = []
        killpg_calls: list[int] = []

        def killpg(pgid: int, sig: int) -> None:
            killpg_calls.append(sig)

        with mock.patch.object(own_loop.os, "killpg", killpg):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(),
                    has_copilot_login=lambda: ran.append("ran") or True, probe_deadline=1.0,
                )
        self.assertEqual(caught.exception.detail, "probe unenforceable")
        self.assertEqual(ran, [])
        self.assertEqual(killpg_calls, [])
        self.assertEqual(set(), _direct_children() - children)

    def test_sa_nocldwait_refuses_before_the_callable(self) -> None:
        # libc SA_NOCLDWAIT still reports SIG_DFL from signal.getsignal, and the kernel
        # auto-reaps. The probe must refuse before fork and must not call killpg.
        old = _read_sigaction(signal.SIGCHLD)
        new = bytearray(_SIGACTION_BYTES)
        new[_SA_FLAGS_OFFSET:_SA_FLAGS_OFFSET + 4] = _SA_NOCLDWAIT.to_bytes(4, sys.byteorder)
        _write_sigaction(signal.SIGCHLD, bytes(new))
        self.addCleanup(_write_sigaction, signal.SIGCHLD, old)
        self.assertIs(signal.getsignal(signal.SIGCHLD), signal.SIG_DFL)
        children = _direct_children()
        self.addCleanup(_kill_new_children, children)
        forks: list[int] = []
        killpg_calls: list[int] = []
        real_fork = os.fork

        def fork() -> int:
            forks.append(1)
            return real_fork()

        def killpg(pgid: int, sig: int) -> None:
            killpg_calls.append(sig)

        with mock.patch.object(own_loop.os, "fork", fork), mock.patch.object(own_loop.os, "killpg", killpg):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0,
                )
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertEqual(caught.exception.detail, "probe unenforceable")
        self.assertEqual(forks, [])
        self.assertEqual(killpg_calls, [])
        self.assertEqual(set(), _direct_children() - children)

    def test_pidfd_waitid_einval_before_ack_does_not_run_the_callable(self) -> None:
        # Every P_PIDFD observation is EINVAL, including the one before the ack.
        real_waitid = os.waitid

        def waitid(idtype: int, ident: int, options: int):
            if idtype == os.P_PIDFD:
                raise OSError(errno.EINVAL, "waitid")
            return real_waitid(idtype, ident, options)

        children = _direct_children()
        self.addCleanup(_kill_new_children, children)
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "ran")

            def probe() -> bool:
                with open(marker, "w", encoding="ascii") as handle:
                    handle.write("ran")
                return True

            with mock.patch.object(own_loop.os, "waitid", waitid):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    check_copilot_own_loop(
                        copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0,
                    )
            self.assertEqual(caught.exception.detail, "probe unenforceable")
            self.assertFalse(os.path.exists(marker))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and _direct_children() - children:
            time.sleep(0.01)
        self.assertEqual(set(), _direct_children() - children)

    def test_pidfd_waitid_einval_during_cleanup_signals_the_group(self) -> None:
        # The pre-ack observation is scripted as still running. Every later P_PIDFD
        # observation is EINVAL, whether or not the leader has exited.
        real_waitid = os.waitid
        seen = {"n": 0}

        def waitid(idtype: int, ident: int, options: int):
            if idtype == os.P_PIDFD:
                seen["n"] += 1
                if seen["n"] == 1:
                    return None
                raise OSError(errno.EINVAL, "waitid")
            return real_waitid(idtype, ident, options)

        real_killpg = os.killpg
        signals: list[int] = []

        def killpg(pgid: int, sig: int):
            if sig != 0:
                signals.append(sig)
            return real_killpg(pgid, sig)

        with tempfile.TemporaryDirectory() as tmp:
            info_path = os.path.join(tmp, "child")

            def probe() -> bool:
                sleeper = os.fork()
                if sleeper == 0:
                    time.sleep(30)
                    os._exit(0)
                with open(info_path, "w", encoding="ascii") as handle:
                    handle.write(f"{sleeper}\n")
                return True

            self.addCleanup(lambda: _stop_recorded(info_path))
            with mock.patch.object(own_loop.os, "waitid", waitid), \
                    mock.patch.object(own_loop.os, "killpg", killpg):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    check_copilot_own_loop(
                        copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0,
                    )
            self.assertEqual(caught.exception.detail, "probe unenforceable")
            self.assertGreater(seen["n"], 1)
            self.assertIn(signal.SIGKILL, signals)
            with open(info_path, encoding="ascii") as handle:
                sleeper = int(handle.read().strip())
            self.assertTrue(_process_not_running(sleeper))

    def test_pidfd_open_failure_does_not_signal_a_reaped_pid(self) -> None:
        # The numeric fallback must not run once the worker has already been collected.
        real_fork = os.fork
        killed: list[tuple[int, int]] = []

        def fork() -> int:
            pid = real_fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            return pid

        def pidfd_open(pid: int, flags: int = 0) -> int:
            raise OSError(errno.ENOSYS, "pidfd_open")

        def kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))

        with mock.patch.object(own_loop.os, "fork", fork), \
                mock.patch.object(own_loop.os, "pidfd_open", pidfd_open), \
                mock.patch.object(own_loop.os, "kill", kill):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0,
                )
        self.assertEqual(caught.exception.detail, "probe unenforceable")
        self.assertEqual(killed, [])

    def test_pidfd_open_failure_reaps_the_identified_worker(self) -> None:
        children = _direct_children()
        before = _open_fds()
        self.addCleanup(_kill_new_children, children)

        def pidfd_open(pid: int, flags: int = 0) -> int:
            raise OSError(errno.ENOSYS, "pidfd_open")

        with mock.patch.object(own_loop.os, "pidfd_open", pidfd_open):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(
                    copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0,
                )
        self.assertEqual(caught.exception.detail, "probe unenforceable")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and _direct_children() - children:
            time.sleep(0.01)
        self.assertEqual(set(), _direct_children() - children)
        self.assertEqual(before, _open_fds())

    def test_cleanup_restores_a_mask_the_caller_had_blocked(self) -> None:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, previous)
        check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0)
        self.assertEqual(previous | {signal.SIGINT}, signal.pthread_sigmask(signal.SIG_BLOCK, set()))

    def test_sigint_is_blocked_across_pidfd_acquisition(self) -> None:
        real_open = os.pidfd_open
        seen: list[bool] = []

        def pidfd_open(pid: int, flags: int = 0) -> int:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            seen.append(signal.SIGINT in blocked)
            return real_open(pid, flags)

        before = _open_fds()
        with mock.patch.object(own_loop.os, "pidfd_open", pidfd_open):
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True, probe_deadline=1.0)
        self.assertEqual(seen, [True])
        self.assertEqual(before, _open_fds())

    def test_pidfd_send_prefers_the_stdlib_over_libc(self) -> None:
        seen: list[int] = []

        def fake(pidfd: int, sig: int, *args: object, **kwargs: object) -> None:
            seen.append(sig)

        def libc(pidfd: int, sig: int) -> None:
            raise AssertionError("libc pidfd_send_signal used")

        with mock.patch.object(signal, "pidfd_send_signal", fake), \
                mock.patch.object(own_loop, "_pidfd_send_signal_libc", libc):
            own_loop._pidfd_send_signal(7, signal.SIGTERM)
        self.assertEqual(seen, [signal.SIGTERM])

    def test_pidfd_sender_order_is_stdlib_then_os_then_libc(self) -> None:
        stdlib, posix, libc = object(), object(), object()
        self.assertIs(own_loop._select_pidfd_sender(stdlib, posix, libc), stdlib)
        self.assertIs(own_loop._select_pidfd_sender(None, posix, libc), posix)
        self.assertIs(own_loop._select_pidfd_sender(None, None, libc), libc)
        self.assertIsNone(own_loop._select_pidfd_sender(None, None, None))

    def test_an_unsafe_home_does_not_leave_a_copilot_directory(self) -> None:
        root = tempfile.mkdtemp(prefix="csl-unsafe-home-")
        self.addCleanup(shutil.rmtree, root, True)
        os.chmod(root, 0o700)
        home = os.path.join(root, "open")
        os.mkdir(home, 0o777)
        os.chmod(home, 0o777)
        with self.assertRaises(EnvRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home=home, has_copilot_login=lambda: True)
        self.assertEqual(caught.exception.name, "HOME")
        self.assertEqual(caught.exception.cause, "home_unsafe")
        self.assertFalse(os.path.lexists(os.path.join(home, ".copilot")))

    def test_a_nul_in_home_is_a_typed_refusal(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home="/tmp/bad\0home", has_copilot_login=lambda: True)
        self.assertEqual(caught.exception.name, "HOME")
        self.assertEqual(caught.exception.cause, "home_unsafe")

    def test_a_surrogate_in_home_is_a_typed_refusal(self) -> None:
        # U+DCFF is the surrogateescape spelling of the directory byte 0xff. The directory
        # exists and is private; the surrogate is still refused before any filesystem call.
        root = tempfile.mkdtemp(prefix="csl-surrogate-home-")
        self.addCleanup(shutil.rmtree, root, True)
        os.chmod(root, 0o700)
        raw = os.path.join(os.fsencode(root), b"\xff")
        os.mkdir(raw, 0o700)
        home = os.fsdecode(raw)
        self.assertIn("\udcff", home)
        with self.assertRaises(EnvRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home=home, has_copilot_login=lambda: True)
        self.assertEqual(caught.exception.name, "HOME")
        self.assertEqual(caught.exception.cause, "home_unsafe")
        self.assertFalse(os.path.lexists(os.path.join(raw, b".copilot")))

    def test_an_inherited_or_overridden_surrogate_copilot_home_is_refused(self) -> None:
        root = tempfile.mkdtemp(prefix="csl-copilot-home-")
        self.addCleanup(shutil.rmtree, root, True)
        os.chmod(root, 0o700)
        raw = os.path.join(os.fsencode(root), b"\xff")
        os.mkdir(raw, 0o700)
        copilot_home = os.fsdecode(raw)
        self.assertIn("\udcff", copilot_home)
        parent = {"PATH": "/usr/bin:/bin", "COPILOT_HOME": copilot_home}
        with self.assertRaises(EnvRefused) as inherited:
            build_child_env("copilot", home=_route_home(), auth_mode="subscription", parent=parent)
        self.assertEqual(inherited.exception.name, "COPILOT_HOME")
        self.assertEqual(inherited.exception.cause, "home_unsafe")
        with self.assertRaises(EnvRefused) as overridden:
            build_child_env(
                "copilot", home=_route_home(), auth_mode="subscription",
                parent={"PATH": "/usr/bin:/bin"}, overrides={"COPILOT_HOME": copilot_home},
            )
        self.assertEqual(overridden.exception.name, "COPILOT_HOME")
        self.assertEqual(overridden.exception.cause, "home_unsafe")
        self.assertFalse(os.path.lexists(os.path.join(raw, b".copilot")))

    def test_check_without_a_route_home_refuses(self) -> None:
        # Ambient account A must not approve a later child that selects account B.
        kwargs = {"has" + "_copilot_login": lambda: True}
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(
                copilot(), {"PATH": "/usr/bin:/bin", "HOME": "/home/account-a", "COPILOT_HOME": "/home/account-a/.copilot"},
                **kwargs,
            )
        self.assertEqual(caught.exception.cause, "route_home_missing")

    def test_a_held_lock_refuses_before_the_probe_can_deadlock(self) -> None:
        # Fork copies the lock as held. The holder thread is not in the child, so a probe that
        # acquires the lock never observes a later release and the deadline always fires.
        lock = threading.Lock()
        held = threading.Event()
        release = threading.Event()
        ran = threading.Event()

        def holder() -> None:
            lock.acquire()
            held.set()
            release.wait(5)
            lock.release()

        def probe() -> bool:
            ran.set()
            lock.acquire()
            lock.release()
            return True

        thread = threading.Thread(target=holder)
        thread.start()
        self.addCleanup(release.set)
        self.addCleanup(thread.join, 2)
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=probe, probe_deadline=1.0)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(caught.exception.detail, "probe threaded")
        self.assertFalse(ran.is_set())
        release.set()
        thread.join(2)
        self.assertTrue(lock.acquire(timeout=1))
        lock.release()

    def test_aws_access_key_id_and_gh_pat_do_not_reach_the_copilot_child(self) -> None:
        # Suffix matching catches *_TOKEN and *_API_KEY, not these two names.
        secret = "copilot-child-secret"
        names = ("AWS_ACCESS_KEY_ID", "aws_access_key_id", "GH_PAT", "gh_pat")
        env = {**BASE, **{name: secret for name in names}}
        decision = check_copilot_own_loop(copilot(), env, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(dict(decision.child_env), BASE)
        self.assertNotIn(secret, repr(decision.child_env))
        self.assertTrue(set(names) <= set(decision.removed_env))

    def test_route_may_not_pass_a_token(self) -> None:
        for name in ("token", "github_token", "COPILOT_GITHUB_TOKEN", "api_key", "bearer"):
            with self.subTest(name=name):
                self.refused("recursion_holds_no_token", copilot(**{name: FAKE}))

    def test_other_methods_and_fields_refuse(self) -> None:
        for method in ("gh_cli", "gh", "oauth_app", "github_oauth_app", "", None):
            with self.subTest(method=method):
                self.refused("method_not_allowed", copilot(method=method))
        self.refused("route_unknown_field", copilot(fallback="gh"))
        self.refused("auth_not_subscription", copilot(auth="api_key"))
        self.refused("engine_mismatch", copilot(engine="codex"))
        self.refused("route_malformed", ["copilot"])

    def test_spelling_only_normalisation(self) -> None:
        d = check_copilot_own_loop(copilot(method="Copilot CLI sign-in"), BASE, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(d.method, "copilot_cli_sign_in")


class NousTests(unittest.TestCase):
    def refused(self, cause: str, route: object, env: dict | None = None) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_nous_own_loop(route, BASE if env is None else env)
        self.assertEqual(ctx.exception.cause, cause)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_loopback_proxy_with_nous_adapter_only_runs(self) -> None:
        for url in ("http://127.0.0.1:8645/v1", "http://127.8.9.1:1/", "http://[::1]:8645", "https://127.0.0.1"):
            with self.subTest(url=url):
                d = check_nous_own_loop(nous(base_url=url, adapters=["nous"]), BASE, current_user="u",
                                        interactive_users=["u"])
                self.assertEqual((d.base_url, d.adapters, d.warnings), (url, ("nous",), ()))
        d = check_nous_own_loop(nous(), BASE)
        self.assertEqual(d.adapters, ("nous",))

    def test_child_env_carries_no_provider_credential(self) -> None:
        env = {**BASE, "NOUS_API_KEY": FAKE, "XAI_API_KEY": FAKE, "OPENAI_API_KEY": FAKE,
               "ANTHROPIC_AUTH_TOKEN": FAKE, "HERMES_HOME": "/h", "OPENAI_BASE_URL": "http://evil"}
        d = check_nous_own_loop(nous(), env)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_credential_aliases_and_git_config_bearer_never_reach_the_child(self) -> None:
        # Names the *_API_KEY / *_TOKEN matcher misses, and git's config-injection channel.
        secret = "bearer-not-a-real-token"
        names = (
            "GITHUB_PASSWORD", "github_pat", "GH_PAT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "OPENAI_API_BASE",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE",
            "GIT_CONFIG_PARAMETERS",
        )
        env = {**BASE, **{name: secret for name in names}}
        for check in (
            lambda: check_copilot_own_loop(copilot(), env, home=_route_home(), has_copilot_login=lambda: True),
            lambda: check_nous_own_loop(nous(), env),
        ):
            with self.subTest(check=check):
                decision = check()
                self.assertEqual(dict(decision.child_env), BASE)
                self.assertNotIn(secret, repr(decision.child_env))
                self.assertTrue(set(names) <= set(decision.removed_env))

    def test_proxy_settings_never_reach_the_nous_child(self) -> None:
        # Devin r4505395127: a proxy-aware client would send loopback-bound traffic to the proxy host.
        proxies = {"HTTP_PROXY": "http://proxy.example:3128", "https_proxy": "http://proxy.example:3128",
                   "ALL_PROXY": "socks5://proxy.example:1080", "all_proxy": "socks5://p:1"}
        d = check_nous_own_loop(nous(), {**BASE, **proxies})
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(proxies))

    def test_nous_model_must_stay_on_the_nous_adapter(self) -> None:
        # Devin r4505395083: the model is checked and carried, not left to the caller.
        self.refused("adapter_must_stay_off", nous(model="xai/grok-3"))
        self.refused("nous_adapter_only", nous(model="openai/gpt-5"))
        self.refused("model_malformed", nous(model="has space"))
        for model in ("Hermes-4-405B", "nous/Hermes-4-70B"):
            with self.subTest(model=model):
                self.assertEqual(check_nous_own_loop(nous(model=model), BASE).model, model)
        self.assertEqual(check_nous_own_loop(nous(), BASE).model, "")

    def test_non_loopback_and_names_refuse(self) -> None:
        for url in ("http://10.0.0.5:8645", "http://192.168.1.2", "http://0.0.0.0:8645", "http://[::]:1",
                    "https://portal.nousresearch.com/v1", "http://localhost:8645", "http://127.0.0.1.nip.io"):
            with self.subTest(url=url):
                self.refused("proxy_not_loopback", nous(base_url=url))

    def test_ipv4_mapped_loopback_passes_and_mapped_remote_refuses(self) -> None:
        check_nous_own_loop(nous(base_url="http://[::ffff:127.0.0.1]:8645"), BASE)
        self.refused("proxy_not_loopback", nous(base_url="http://[::ffff:10.0.0.1]:8645"))

    def test_malformed_addresses_refuse(self) -> None:
        self.refused("proxy_address_missing", nous(base_url=""))
        self.refused("proxy_address_missing", nous(base_url=None))
        for url in ("ftp://127.0.0.1", "127.0.0.1:8645", "http://127.0.0.1:99999", "unix:///tmp/s"):
            with self.subTest(url=url):
                self.refused("proxy_address_malformed", nous(base_url=url))
        self.refused("recursion_holds_no_token", nous(base_url=f"http://u:{FAKE}@127.0.0.1:8645"))

    def test_xai_adapter_stays_off_and_only_nous_runs(self) -> None:
        self.refused("adapter_must_stay_off", nous(adapters=["nous", "xai"]))
        self.refused("adapter_must_stay_off", nous(adapters=["XAI"]))
        self.refused("nous_adapter_only", nous(adapters=["openai"]))
        self.refused("nous_adapter_only", nous(adapters=[]))
        self.refused("nous_adapter_only", nous(adapters=["nous", "nous"]))
        self.refused("adapters_malformed", nous(adapters="nous"))

    def test_route_holds_no_nous_token(self) -> None:
        for name in ("api_key", "token", "bearer", "authorization", "portal_credential"):
            with self.subTest(name=name):
                self.refused("recursion_holds_no_token", nous(**{name: FAKE}))
        self.refused("method_not_allowed", nous(method="portal_api_key"))
        self.refused("route_unknown_field", nous(host="127.0.0.1"))

    def test_shared_host_warns(self) -> None:
        d = check_nous_own_loop(nous(), BASE, current_user="u", interactive_users=["u", "bob", "", "alice"])
        self.assertEqual(len(d.warnings), 1)
        self.assertIn("alice, bob", d.warnings[0])

    def test_unknown_host_users_warn(self) -> None:
        # red-team #321 F3: "not checked" must not read as "nobody else is here".
        d = check_nous_own_loop(nous(), BASE)
        self.assertEqual(len(d.warnings), 1)
        self.assertTrue(d.warnings[0].startswith("host users unknown"))
        d = check_nous_own_loop(nous(), BASE, current_user="u", interactive_users=[])
        self.assertEqual(d.warnings, ())


_FAMILY_NAMES = (
    "X_SECRET", "x_password", "SOMETHING_KEY", "X_PAT", "X_CREDENTIALS",
    "AWS_FOO", "AZURE_FOO", "azure_bar", "ARM_FOO", "GOOGLE_FOO", "CLOUDSDK_FOO", "GCLOUD_FOO",
    "SSH_AUTH_SOCK", "SSH_ASKPASS", "GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "KUBECONFIG", "DOCKER_CONFIG",
    "GITHUB_API_URL", "COPILOT_API_URL",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "dyld_library_path",
    "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONSTARTUP", "BASH_ENV", "ENV",
)
_KEPT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/u",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "NODE_EXTRA_CA_CERTS": "/tmp/ca.pem",
}


class AllowlistFamilyTests(unittest.TestCase):
    def test_each_secret_family_is_stripped_and_the_base_names_stay(self) -> None:
        secret = "family-not-a-real-secret"
        for name in _FAMILY_NAMES:
            self.assertTrue(own_loop._ambient_secret(name), name)
            self.assertTrue(_blocked_secret(name), name)
        for name in _KEPT:
            self.assertFalse(own_loop._ambient_secret(name), name)
            self.assertFalse(_blocked_secret(name), name)
        # A missing cert under /tmp is not a private file. The probe rebuilds the child env, so the
        # cert has to sit in the route home, where a missing file cannot be planted by another user.
        kept = {**_KEPT, "NODE_EXTRA_CA_CERTS": os.path.join(_route_home(), "ca.pem")}
        env = {**kept, **{name: secret for name in _FAMILY_NAMES}}
        checks = [lambda: check_nous_own_loop(nous(), env)]
        if own_loop._probe_enforceable():
            checks.insert(0, lambda: check_copilot_own_loop(copilot(), env, home=_route_home(), has_copilot_login=lambda: True))
        for check in checks:
            with self.subTest(check=check):
                decision = check()
                self.assertEqual(dict(decision.child_env), kept)
                self.assertNotIn(secret, repr(decision.child_env))
                self.assertTrue(set(_FAMILY_NAMES) <= set(decision.removed_env))
                self.assertIn("COPILOT_API_URL", decision.removed_env)
                self.assertIn("NODE_EXTRA_CA_CERTS", decision.child_env)


class UnenforceableProbeTests(unittest.TestCase):
    def test_windows_probe_refuses_instead_of_starting_a_thread(self) -> None:
        with mock.patch.object(own_loop, "_probe_enforceable", return_value=False):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(copilot(), BASE, home=_route_home(), has_copilot_login=lambda: True)
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertEqual(caught.exception.detail, "probe unenforceable")
        self.assertNotIn("thread", caught.exception.detail)


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_address_and_never_raises(self) -> None:
        self.assertEqual(nous_proxy_doctor("http://127.0.0.1:8645/v1", current_user="u", interactive_users=["u"]),
                         {"ok": True, "address": "http://127.0.0.1:8645", "cause": "", "warnings": ()})
        self.assertEqual(nous_proxy_doctor("https://127.0.0.1")["address"], "https://127.0.0.1:443")
        self.assertEqual(nous_proxy_doctor("http://[::1]:8645/v1")["address"], "http://[::1]:8645")
        bad = nous_proxy_doctor("http://10.1.2.3:8645")
        self.assertEqual((bad["ok"], bad["address"], bad["cause"]), (False, "", "proxy_not_loopback"))
        self.assertNotIn("10.1.2.3", str(bad))
        self.assertEqual(nous_proxy_doctor(None)["cause"], "proxy_address_missing")
        shared = nous_proxy_doctor("http://127.0.0.1:1", current_user="u", interactive_users=["u", "eve"])
        self.assertTrue(shared["ok"])
        self.assertIn("eve", shared["warnings"][0])

    def test_doctor_does_not_echo_proxy_userinfo(self) -> None:
        secret = "userinfo-secret"
        leaked = (
            f"http://user:{secret}@127.0.0.1:8645/v1",
            f"http://user:{secret}@[::1]:8645/v1",
            f"ftp://user:{secret}@127.0.0.1/x",
        )
        for url in leaked:
            with self.subTest(url=url.split("@", 1)[0]):
                report = nous_proxy_doctor(url)
                self.assertFalse(report["ok"])
                self.assertNotIn(secret, str(report))
                self.assertNotIn("@", report["address"].split("://", 1)[-1])
                try:
                    check_nous_own_loop(nous(base_url=url), BASE)
                except OwnLoopRouteRefused as exc:
                    self.assertNotIn(secret, str(exc))
                    self.assertNotIn(secret, exc.detail)
                    if url.startswith("ftp://"):
                        self.assertEqual(exc.cause, report["cause"])
                        self.assertEqual("recursion_holds_no_token", exc.cause)
                else:
                    self.fail("credential URL was accepted")

    def test_query_and_fragment_secrets_are_redacted_and_refused(self) -> None:
        # Userinfo is stripped from the doctor line. A secret in the query or fragment was copied through.
        secret = "query-fragment-secret"
        urls = (
            f"http://127.0.0.1:8645/v1?token={secret}",
            f"http://127.0.0.1:8645/v1?x=1&access_token={secret}",
            f"http://[::1]:8645/v1#{secret}",
            f"http://127.0.0.1:8645/v1?ok=1#{secret}",
            f"ftp://127.0.0.1/x?password={secret}",
            f"http://user:pass@127.0.0.1:8645/v1?token={secret}",
            f"http://10.1.2.3:8645/v1?token={secret}#more-{secret}",
        )
        for url in urls:
            with self.subTest(url=url.split(secret)[0]):
                report = nous_proxy_doctor(url)
                self.assertFalse(report["ok"])
                self.assertNotIn(secret, str(report))
                self.assertNotIn("?", report["address"])
                self.assertNotIn("#", report["address"])
                try:
                    check_nous_own_loop(nous(base_url=url), BASE)
                except OwnLoopRouteRefused as exc:
                    self.assertEqual(exc.cause, "recursion_holds_no_token")
                    self.assertNotIn(secret, str(exc))
                    self.assertNotIn(secret, exc.detail)
                else:
                    self.fail("query or fragment secret was accepted")

    def test_doctor_address_is_the_origin_and_refusal_text_is_fixed(self) -> None:
        # The port parser's ValueError used to echo the token "canary". The scheme check used to echo the raw URL.
        canary = "canary"
        report = nous_proxy_doctor(f"http://127.0.0.1:{canary}/v1")
        self.assertFalse(report["ok"])
        self.assertEqual(report["address"], "")
        self.assertNotIn(canary, str(report))
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(nous(base_url=f"http://127.0.0.1:{canary}/v1"), BASE)
        self.assertEqual(caught.exception.detail, "proxy address")
        self.assertNotIn(canary, str(caught.exception))
        self.assertNotIn(canary, caught.exception.detail)

        path_secret = "path-secret"
        report = nous_proxy_doctor(f"ftp://127.0.0.1/{path_secret}")
        self.assertNotIn(path_secret, str(report))
        self.assertNotIn("/" + path_secret, report["address"])
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(nous(base_url=f"ftp://127.0.0.1/{path_secret}"), BASE)
        self.assertEqual(caught.exception.detail, "proxy address")
        self.assertNotIn(path_secret, str(caught.exception))

        control_secret = "control-secret"
        raw = f"http://127.0.0.1:8645/\n{control_secret}"
        report = nous_proxy_doctor(raw)
        self.assertFalse(report["ok"])
        self.assertEqual(report["address"], "")
        self.assertNotIn(control_secret, str(report))
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(nous(base_url=raw), BASE)
        self.assertEqual(caught.exception.cause, "proxy_address_malformed")
        self.assertEqual(caught.exception.detail, "proxy address")
        self.assertNotIn(control_secret, str(caught.exception))

    def test_doctor_does_not_echo_host_scheme_or_zone_text(self) -> None:
        canary = "canary"
        cases = (
            f"http://{canary}.invalid:8645/v1",
            f"{canary}://127.0.0.1:8645/v1",
            f"http://[::1%{canary}]:8645/v1",
        )
        for url in cases:
            with self.subTest(url=url.split(canary)[0]):
                report = nous_proxy_doctor(url)
                self.assertFalse(report["ok"])
                self.assertEqual(report["address"], "")
                self.assertNotIn(canary, str(report))
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    check_nous_own_loop(nous(base_url=url), BASE)
                self.assertNotIn(canary, str(caught.exception))
                self.assertNotIn(canary, caught.exception.detail)
        padded = "\nhttp://127.0.0.1:8645/v1"
        report = nous_proxy_doctor(padded)
        self.assertFalse(report["ok"])
        self.assertEqual(report["address"], "")
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(nous(base_url=padded), BASE)
        self.assertEqual(caught.exception.cause, "proxy_address_malformed")

    def test_a_bare_query_or_fragment_is_accepted_and_the_decision_keeps_the_path(self) -> None:
        for url in ("http://127.0.0.1:8645/v1?", "http://127.0.0.1:8645/v1#", "http://127.0.0.1:8645/v1?#"):
            with self.subTest(url=url):
                decision = check_nous_own_loop(nous(base_url=url), BASE)
                self.assertEqual(decision.base_url, url)
                report = nous_proxy_doctor(url)
                self.assertTrue(report["ok"])
                self.assertEqual(report["address"], "http://127.0.0.1:8645")
                self.assertNotIn("/v1", report["address"])
                self.assertNotIn("?", report["address"])
                self.assertNotIn("#", report["address"])

    def test_doctor_warns_when_host_users_unknown(self) -> None:
        for url in ("http://127.0.0.1:8645/v1", "http://10.1.2.3:8645"):
            got = nous_proxy_doctor(url)
            self.assertEqual(len(got["warnings"]), 1)
            self.assertTrue(got["warnings"][0].startswith("host users unknown"))


_SA_NOCLDWAIT = 2
_SIGACTION_BYTES = 256
_SA_FLAGS_OFFSET = 136


def _sigaction_buffer(raw: bytes) -> ctypes.Array:
    if _SIGACTION_BYTES < 128:
        raise AssertionError("sigaction buffer")
    if not own_loop._sigaction_abi_supported():
        raise unittest.SkipTest("sigaction ABI is not verified")
    return ctypes.create_string_buffer(raw.ljust(_SIGACTION_BYTES, b"\0")[:_SIGACTION_BYTES])


def _libc_sigaction(signum: int, new: bytes | None, old: ctypes.Array | None) -> int:
    if not own_loop._sigaction_abi_supported():
        raise unittest.SkipTest("sigaction layout is not verified for this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sigaction.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    libc.sigaction.restype = ctypes.c_int
    return libc.sigaction(signum, _sigaction_buffer(new) if new is not None else None, old)


def _read_sigaction(signum: int) -> bytes:
    old = _sigaction_buffer(b"")
    if _libc_sigaction(signum, None, old) != 0:
        raise OSError("sigaction")
    return bytes(old.raw)


def _write_sigaction(signum: int, raw: bytes) -> None:
    if _libc_sigaction(signum, raw, None) != 0:
        raise OSError("sigaction")


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def _direct_children() -> set[int]:
    me = os.getpid()
    kids: set[int] = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="ascii") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                if int(line.split()[1]) == me:
                    kids.add(int(name))
                break
    return kids


def _kill_new_children(before: set[int]) -> None:
    for pid in _direct_children() - before:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _stop_recorded(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="ascii") as handle:
        parts = handle.read().split()
    for part in parts:
        pid = int(part)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _process_not_running(pid: int) -> bool:
    """True when ``pid`` is gone or a zombie. A sleeping sleeper is still running."""

    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if _process_state(pid).startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def _process_state(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip().split(" ", 1)[0] if out.stdout.strip() else ""


if __name__ == "__main__":
    unittest.main()
