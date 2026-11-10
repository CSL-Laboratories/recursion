"""2026-11-08 own-loop routes: Copilot on the user's own copilot CLI sign-in, Nous via Hermes's loopback proxy.

Fakes only: no engine, login store, provider, DNS or network is touched.
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import recursion_core.subscription_routes.own_loop as own_loop
from recursion_core.subscription_routes import (
    COPILOT_STRIP_ENV, OwnLoopRouteRefused, check_copilot_own_loop, check_nous_own_loop, nous_proxy_doctor,
)
from recursion_core.subscription_routes.validated_launch import _blocked_secret

FAKE = "fake-not-a-real-token"
BASE = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}


def copilot(**extra: object) -> dict:
    return {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in", **extra}


def nous(**extra: object) -> dict:
    return {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
            "base_url": "http://127.0.0.1:8645/v1", **extra}


class CopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        if not own_loop._probe_enforceable():
            self.skipTest("login probe cannot be enforced on this platform")

    def refused(self, cause: str, route: object, env: dict | None = None, probe=lambda: True) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_copilot_own_loop(route, BASE if env is None else env, has_copilot_login=probe)
        self.assertEqual(ctx.exception.cause, cause)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_stored_login_runs_with_every_token_variable_removed(self) -> None:
        env = {**BASE, **{n: FAKE for n in COPILOT_STRIP_ENV}, "GH_HOST": "github.com", "GH_CONFIG_DIR": "/x"}
        d = check_copilot_own_loop(copilot(model="gpt-5"), env, has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(COPILOT_STRIP_ENV) | {"GH_HOST", "GH_CONFIG_DIR"})
        self.assertIn("COPILOT_API_URL", d.removed_env)
        self.assertNotIn(FAKE, repr(d))

    def test_unrelated_provider_credentials_do_not_reach_the_copilot_child(self) -> None:
        # Devin r4505395174: the stored Copilot sign-in is the only credential this run uses.
        env = {**BASE, "ANTHROPIC_API_KEY": FAKE, "OPENAI_API_KEY": FAKE, "XAI_API_KEY": FAKE,
               "CLAUDE_CODE_OAUTH_TOKEN": FAKE}
        d = check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(env) - set(BASE))
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_copilot_model_is_checked_and_carried(self) -> None:
        d = check_copilot_own_loop(copilot(model="gpt-5"), BASE, has_copilot_login=lambda: True)
        self.assertEqual(d.model, "gpt-5")
        self.assertEqual(check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True).model, "")
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
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=exits)
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
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=hang, probe_deadline=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertIn("deadline", caught.exception.detail)
        kept = check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True, probe_deadline=0.5)
        self.assertEqual(kept.method, "copilot_cli_sign_in")
        for deadline in (0.05, 0.2):
            with self.subTest(deadline=deadline):
                decision = check_copilot_own_loop(
                    copilot(), BASE, has_copilot_login=lambda: True, probe_deadline=deadline,
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
                    check_copilot_own_loop(copilot(), BASE, has_copilot_login=hang, probe_deadline=0.8)
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
            decision = check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True, probe_deadline=1.0)
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
            decision = check_copilot_own_loop(copilot(), BASE, has_copilot_login=probe, probe_deadline=1.0)
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
            decision = check_copilot_own_loop(copilot(), BASE, has_copilot_login=probe, probe_deadline=1.0)
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
                decision = check_copilot_own_loop(copilot(), BASE, has_copilot_login=probe, probe_deadline=1.0)
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
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True, probe_deadline=1.0)
        self.assertIn("kill", order)
        self.assertIn("wait", order)
        self.assertLess(order.index("kill"), order.index("wait"))

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
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=probe, probe_deadline=1.0)
            with open(info_path, encoding="ascii") as handle:
                stayed, escaped = (int(part) for part in handle.read().split())
            self.assertTrue(_process_not_running(stayed))
            self.assertFalse(_process_not_running(escaped))
            os.kill(escaped, signal.SIGKILL)

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
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=probe, probe_deadline=1.0)
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
        decision = check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True)
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
        d = check_copilot_own_loop(copilot(method="Copilot CLI sign-in"), BASE, has_copilot_login=lambda: True)
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
            lambda: check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True),
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
        env = {**_KEPT, **{name: secret for name in _FAMILY_NAMES}}
        checks = [lambda: check_nous_own_loop(nous(), env)]
        if own_loop._probe_enforceable():
            checks.insert(0, lambda: check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True))
        for check in checks:
            with self.subTest(check=check):
                decision = check()
                self.assertEqual(dict(decision.child_env), _KEPT)
                self.assertNotIn(secret, repr(decision.child_env))
                self.assertTrue(set(_FAMILY_NAMES) <= set(decision.removed_env))
                self.assertIn("COPILOT_API_URL", decision.removed_env)
                self.assertIn("NODE_EXTRA_CA_CERTS", decision.child_env)


class UnenforceableProbeTests(unittest.TestCase):
    def test_windows_probe_refuses_instead_of_starting_a_thread(self) -> None:
        with mock.patch.object(own_loop, "_probe_enforceable", return_value=False):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True)
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
