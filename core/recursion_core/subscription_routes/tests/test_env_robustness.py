"""Hostile environments for the merged subscription routes (CSL-786).

Run from the repository root:
``python3 -m pytest core/recursion_core/subscription_routes/tests/test_env_robustness.py -q``.
No engine, provider or network. Credential-shaped values are fake.

XDG is observed and not changed. A child env that carries ``XDG_CONFIG_HOME`` refuses
``engine_selector_env``. Copilot and Nous own-loop decisions copy a relative ``XDG_*``
and a relative or empty ``HOME`` through; this file records that and does not change it.
Neither module opens SQLite. A route home longer than 500 bytes is a directory path, not a
database path.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest import mock

import recursion_core.subscription_routes.own_loop as own_loop
import recursion_core.subscription_routes.policy as route_policy


def _process_running(pid: int) -> bool:
    """True when ``pid`` is alive and not a zombie."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    end = text.rfind(")")
    if end < 0 or end + 2 >= len(text):
        return True
    return text[end + 2] not in "ZX"
from recursion_core.subscription_routes import (
    HERMES_ADOPT_EXTERNAL_LOGINS,
    OwnLoopRouteRefused,
    SubscriptionRouteRefused,
    WrapperInstall,
    check_copilot_own_loop,
    check_nous_own_loop,
    check_route,
    guard_then_spawn,
    measure_package_pin,
    nous_proxy_doctor,
)

_CORE = str(Path(__file__).resolve().parents[3])
_SECRET = "super-secret-token"
_SURROGATE = bytes([0xFF]).decode("utf-8", "surrogateescape")


def _ascii_python(script: str, locale: dict[str, str], **probe: str) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROBE_CORE": _CORE, "PYTHONSAFEPATH": "1",
           "PYTHONIOENCODING": "ascii:backslashreplace"}
    env.update(locale)
    env.update(probe)
    ran = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    if ran.returncode != 0 and "REFUSED" not in ran.stdout and "SPAWN" not in ran.stdout and "ROUTE" not in ran.stdout:
        raise AssertionError(ran.stderr[-600:] or ran.stdout)
    return ran.stdout.strip()


_UNENCODABLE = r"""
import os, sys
sys.path.insert(0, os.environ["PROBE_CORE"])
from recursion_core.subscription_routes import (
    check_route, check_copilot_own_loop, OwnLoopRouteRefused, SubscriptionRouteRefused,
)
home = os.environ["PROBE_HOME"]
op = os.environ["PROBE_OP"]
lang = "h\u00e9llo"
try:
    check_route(
        "opencode",
        {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY", "model": "anthropic/m"},
        {"PATH": "/usr/bin:/bin", "HOME": home, "LANG": lang, "ANTHROPIC_API_KEY": "fake-key"},
        route_home=home, parent_env={"HOME": op}, operator_home=op,
    )
except SubscriptionRouteRefused as exc:
    blob = str(exc).encode("ascii", "backslashreplace").decode("ascii")
    print("LEAK" if "caf" in blob or "\\xe9" in blob else "REFUSED", exc.cause)
else:
    print("ROUTE_OK")
try:
    check_copilot_own_loop(
        {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
        {"PATH": "/usr/bin", "HOME": home, "LANG": lang},
        has_copilot_login=lambda: True,
    )
except OwnLoopRouteRefused as exc:
    blob = str(exc).encode("ascii", "backslashreplace").decode("ascii")
    print("LEAK" if "caf" in blob or "\\xe9" in blob else "REFUSED", exc.cause)
else:
    print("COPILOT_OK")
"""


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class SubscriptionRobustness(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="csl786-robust-"))
        self.addCleanup(self._cleanup)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = self.root / "route-home"
        self.home.mkdir(mode=0o700)
        self.install = self.root / "pkg"
        self.install.mkdir(mode=0o700)
        (self.install / "launcher.js").write_text("launcher\n", encoding="ascii")
        (self.install / "lib.js").write_text("lib\n", encoding="ascii")
        launcher = str(self.install / "launcher.js")
        self.wrapper = WrapperInstall(launcher, str(self.install), measure_package_pin(launcher, str(self.install)))
        self.launcher_path = launcher
        self.calls = mock.Mock()

    def _cleanup(self) -> None:
        for dirpath, dirnames, _filenames in os.walk(self.root):
            os.chmod(dirpath, 0o700)
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, True)

    def _profile(self) -> dict:
        return {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY",
                "model": "anthropic/m", HERMES_ADOPT_EXTERNAL_LOGINS: False}

    def _env(self, home: Path | None = None, **extra: str) -> dict:
        target = home or self.home
        return {"PATH": "/usr/bin:/bin", "HOME": str(target), "ANTHROPIC_API_KEY": "fake-key", **extra}

    def _guard(self, home: Path | None = None, **extra: str):
        target = home or self.home
        return guard_then_spawn(
            self.calls, [self.launcher_path, "run", "anthropic/m"], "hermes", self._profile(), self._env(target, **extra),
            route_home=str(target), parent_env={"HOME": str(self.operator), "PATH": "/usr/bin:/bin"},
            operator_home=str(self.operator), wrapper=self.wrapper,
        )

    def test_readonly_hermes_home_refuses(self) -> None:
        os.chmod(self.home, 0o500)
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            self._guard()
        self.assertEqual(("hermes", "hermes_config_blocks_adopt_override"), (caught.exception.engine, caught.exception.cause))
        self.calls.assert_not_called()
        self.assertFalse((self.home / ".hermes").exists())

    def test_proxy_userinfo_is_absent_from_refusal_and_doctor(self) -> None:
        url = f"ftp://user:{_SECRET}@127.0.0.1:9"
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": url}
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(route, {"PATH": "/usr/bin", "HOME": str(self.home)})
        self.assertEqual("recursion_holds_no_token", caught.exception.cause)
        self.assertNotIn(_SECRET, str(caught.exception))
        self.assertNotIn(_SECRET, caught.exception.detail)
        doctor = nous_proxy_doctor(url)
        self.assertEqual((False, "ftp://127.0.0.1:9", "recursion_holds_no_token"),
                         (doctor["ok"], doctor["address"], doctor["cause"]))
        self.assertNotIn(_SECRET, str(doctor))
        http = nous_proxy_doctor(f"http://user:{_SECRET}@10.1.2.3:9")
        self.assertEqual("http://10.1.2.3:9", http["address"])
        self.assertNotIn(_SECRET, str(http))

    def test_screened_parent_env_is_a_snapshot(self) -> None:
        # A mapping that changes between .items() calls, and a login callback that mutates the
        # caller's dict, must not put a NUL into the child after the screen has passed.
        class _Flip(Mapping[str, str]):
            def __init__(self) -> None:
                self.reads = 0

            def __getitem__(self, key: str) -> str:
                return dict(self.items())[key]

            def __iter__(self) -> Iterator[str]:
                return iter(dict(self.items()))

            def __len__(self) -> int:
                return 3

            def items(self):  # type: ignore[override]
                self.reads += 1
                lang = "C" if self.reads == 1 else "C\0"
                return iter((("PATH", "/usr/bin"), ("HOME", str(self_home)), ("LANG", lang)))

        self_home = str(self.home)
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": "http://127.0.0.1:9"}
        decision = check_nous_own_loop(route, _Flip())
        self.assertEqual("C", dict(decision.child_env).get("LANG"))
        self.assertNotIn("\0", "".join(dict(decision.child_env).values()))
        original = {"PATH": "/usr/bin", "HOME": str(self.home), "LANG": "C"}

        def mutate() -> bool:
            original["LANG"] = "C\0"
            return True

        copilot = check_copilot_own_loop(
            {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
            original, has_copilot_login=mutate,
        )
        self.assertEqual("C", dict(copilot.child_env).get("LANG"))
        self.assertNotIn("\0", "".join(dict(copilot.child_env).values()))

    def test_doctor_reports_canonical_origin_only(self) -> None:
        secret = "CANARYSECRET"
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy"}
        malformed = f"ftp:user:{secret}@127.0.0.1:9"
        doctor = nous_proxy_doctor(malformed)
        self.assertNotIn(secret, str(doctor))
        self.assertEqual("", doctor["address"])
        queried = f"http://127.0.0.1:9/?token={secret}"
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop({**route, "base_url": queried}, {"PATH": "/usr/bin", "HOME": str(self.home)})
        self.assertEqual("recursion_holds_no_token", caught.exception.cause)
        self.assertNotIn(secret, str(caught.exception))
        queried_doctor = nous_proxy_doctor(queried)
        self.assertEqual((False, "http://127.0.0.1:9"), (queried_doctor["ok"], queried_doctor["address"]))
        self.assertNotIn(secret, str(queried_doctor))
        encoded = f"http://user%3A{secret}%40127.0.0.1:9"
        with self.assertRaises(OwnLoopRouteRefused) as encoded_caught:
            check_nous_own_loop({**route, "base_url": encoded}, {"PATH": "/usr/bin", "HOME": str(self.home)})
        self.assertNotIn(secret, str(encoded_caught.exception))
        self.assertNotIn("%3A", str(encoded_caught.exception))
        encoded_doctor = nous_proxy_doctor(encoded)
        self.assertEqual("", encoded_doctor["address"])
        self.assertNotIn(secret, str(encoded_doctor))
        self.assertNotIn("%3A", encoded_doctor["address"])

    def test_equals_in_env_name_refuses(self) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop(
                {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": "http://127.0.0.1:9"},
                {"PATH": "/usr/bin", "HOME": str(self.home), "BAD=NAME": "value"},
            )
        self.assertEqual("parent_env_malformed", caught.exception.cause)
        self.assertNotIn("BAD=NAME", str(caught.exception))

    def test_empty_env_name_is_malformed_on_both_routes(self) -> None:
        parent = {"PATH": "/usr/bin", "HOME": str(self.home), "": "val"}
        forks: list[int] = []
        real_fork = os.fork

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                forks.append(pid)
            return pid

        try:
            with self.assertRaises(OwnLoopRouteRefused) as nous_caught:
                check_nous_own_loop(
                    {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                     "base_url": "http://127.0.0.1:9"},
                    parent,
                )
            self.assertEqual("parent_env_malformed", nous_caught.exception.cause)
            self.assertNotIn("val", str(nous_caught.exception))
            with mock.patch.object(own_loop.os, "fork", tracking_fork):
                with self.assertRaises(OwnLoopRouteRefused) as copilot_caught:
                    check_copilot_own_loop(
                        {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                        parent,
                        has_copilot_login=lambda: True,
                    )
            self.assertEqual("parent_env_malformed", copilot_caught.exception.cause)
            self.assertEqual([], forks)
        finally:
            for pid in forks:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_hermes_config_read_eio_refuses(self) -> None:
        hermes = self.home / ".hermes"
        hermes.mkdir(mode=0o700)
        config = hermes / "config.yaml"
        config.write_bytes(b"auth:\n  adopt_external_logins: false\n")
        os.chmod(config, 0o600)

        def fail_read(fd: int, size: int) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch("recursion_core.subscription_routes.policy.os.read", side_effect=fail_read):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                self._guard()
        self.assertEqual("hermes_config_blocks_adopt_override", caught.exception.cause)
        self.calls.assert_not_called()

    def test_non_string_env_key_refuses(self) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                {"PATH": "/usr/bin", 1: "x"},  # type: ignore[dict-item]
                has_copilot_login=lambda: True,
            )
        self.assertEqual("parent_env_malformed", caught.exception.cause)

    def test_unencodable_text_refuses_under_ascii_locale(self) -> None:
        out = _ascii_python(
            _UNENCODABLE,
            {"LANG": "C", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"},
            PROBE_HOME=str(self.home), PROBE_OP=str(self.operator),
        )
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(["REFUSED value_unencodable", "REFUSED env_unencodable"], lines, out)

    def test_utf8_locale_still_accepts_unicode_lang(self) -> None:
        out = _ascii_python(
            _UNENCODABLE,
            {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            PROBE_HOME=str(self.home), PROBE_OP=str(self.operator),
        )
        self.assertEqual("ROUTE_OK\nCOPILOT_OK", out)

    def test_xdg_relative_paths_are_unchanged(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("opencode", {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"},
                        self._env(XDG_CONFIG_HOME="relative/cfg", XDG_DATA_HOME="relative/data"),
                        route_home=str(self.home), parent_env={"HOME": str(self.operator)},
                        operator_home=str(self.operator))
        self.assertEqual("engine_selector_env", caught.exception.cause)
        decision = check_copilot_own_loop(
            {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
            {"PATH": "/usr/bin", "HOME": "rel/home", "XDG_CONFIG_HOME": "rel/cfg", "XDG_DATA_HOME": "rel/data",
             "XDG_RUNTIME_DIR": "rel/run", "GITHUB_TOKEN": _SECRET},
            has_copilot_login=lambda: True,
        )
        child = dict(decision.child_env)
        self.assertEqual("rel/home", child["HOME"])
        self.assertEqual("rel/cfg", child["XDG_CONFIG_HOME"])
        self.assertEqual("rel/data", child["XDG_DATA_HOME"])
        self.assertEqual("rel/run", child["XDG_RUNTIME_DIR"])
        self.assertNotIn("GITHUB_TOKEN", child)
        self.assertNotIn(_SECRET, child.values())

    def test_hostile_env_matrix(self) -> None:
        rows: list[tuple[str, str]] = []
        profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY", "model": "anthropic/m"}

        def record(cell: str, fn) -> None:
            try:
                fn()
            except (SubscriptionRouteRefused, OwnLoopRouteRefused) as exc:
                rows.append((cell, "refused:" + exc.cause))
            except Exception as exc:
                rows.append((cell, "CRASH:" + type(exc).__name__))
            else:
                rows.append((cell, "ok"))

        def route(env, home, parent=None):
            return check_route("opencode", profile, env, route_home=str(home),
                               parent_env=parent if parent is not None else {"HOME": str(self.operator)},
                               operator_home=str(self.operator))

        record("clean", lambda: route(self._env(), self.home))
        record("surrogate-lang", lambda: route(self._env(LANG="C" + _SURROGATE), self.home))
        record("surrogate-key", lambda: route(self._env(ANTHROPIC_API_KEY="fake" + _SURROGATE), self.home))
        record("empty-route-home", lambda: route(self._env(), ""))
        record("relative-route-home", lambda: route({"PATH": "/bin", "HOME": "rel", "ANTHROPIC_API_KEY": "fake-key"}, "rel"))
        record("parent-xdg-relative", lambda: route(self._env(), self.home, {"HOME": str(self.operator),
               "XDG_CONFIG_HOME": "rel/cfg", "XDG_DATA_HOME": "rel/data"}))
        record("null-lang", lambda: route(self._env(LANG="C\0"), self.home))

        link = self.root / "linkhome"
        link.symlink_to(self.home, target_is_directory=True)
        record("symlink-home", lambda: route(self._env(link), link))

        uni = self.root / "caf\u00e9"
        uni.mkdir(mode=0o700)
        record("unicode-home", lambda: route(self._env(uni, LANG="caf\u00e9"), uni))

        deep = self.root / "long"
        deep.mkdir(mode=0o700)
        cursor = deep
        while len(str(cursor)) < 520:
            cursor = cursor / "dd"
            cursor.mkdir(mode=0o700)
        self.assertGreater(len(str(cursor)), 500)
        record("long-path-over-500", lambda: route(self._env(cursor), cursor))

        record("nous-empty-home", lambda: check_nous_own_loop(
            {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": "http://127.0.0.1:9"},
            {"PATH": "/usr/bin", "HOME": "", "LANG": "C", "NOUS_API_KEY": _SECRET}))

        saved = os.umask(0)
        try:
            fresh = self.root / "umask-home"
            fresh.mkdir(mode=0o700)
            os.chmod(fresh, 0o700)
            self._guard(fresh)
            directory = stat.S_IMODE((fresh / ".hermes").lstat().st_mode)
            config = stat.S_IMODE((fresh / ".hermes" / "config.yaml").lstat().st_mode)
            rows.append(("umask-000-modes", "ok" if (directory, config) == (0o700, 0o600) else f"UNSAFE:{oct(directory)},{oct(config)}"))
        finally:
            os.umask(saved)

        crashes = [row for row in rows if row[1].startswith("CRASH") or row[1].startswith("UNSAFE")]
        self.assertEqual([], crashes, rows)
        expected = {
            "clean": "ok",
            "surrogate-lang": "ok",
            "surrogate-key": "ok",
            "empty-route-home": "refused:route_home_missing",
            "relative-route-home": "refused:route_home_missing",
            "parent-xdg-relative": "ok",
            "null-lang": "refused:child_env_malformed",
            "symlink-home": "refused:route_home_not_private",
            "unicode-home": "ok",
            "long-path-over-500": "ok",
            "nous-empty-home": "ok",
            "umask-000-modes": "ok",
        }
        self.assertEqual(expected, dict(rows))
        nous = check_nous_own_loop(
            {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": "http://127.0.0.1:9"},
            {"PATH": "/usr/bin", "HOME": "", "LANG": "C", "NOUS_API_KEY": _SECRET},
        )
        self.assertEqual("", dict(nous.child_env)["HOME"])
        self.assertNotIn(_SECRET, dict(nous.child_env).values())

    def test_login_probe_sees_only_the_screened_child(self) -> None:
        marker = self.root / "probe-seen"
        def probe() -> bool:
            marker.write_text(
                repr((os.environ.get("GITHUB_TOKEN"), os.environ.get("LANG"), os.environ.get("AMBIENT_ONLY"))),
                encoding="ascii",
            )
            return True
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ambient-secret", "AMBIENT_ONLY": "ambient-only"}, clear=False):
            decision = check_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                {"PATH": "/usr/bin", "HOME": str(self.home), "LANG": "C"},
                has_copilot_login=probe,
            )
            self.assertEqual("ambient-secret", os.environ["GITHUB_TOKEN"])
        self.assertEqual("(None, 'C', None)", marker.read_text(encoding="ascii"))
        self.assertNotIn("GITHUB_TOKEN", decision.child_env)
        self.assertNotIn("AMBIENT_ONLY", decision.child_env)
        self.assertEqual("C", decision.child_env["LANG"])

    def test_login_probe_does_not_share_the_process_environment(self) -> None:
        # Another live thread makes fork unsafe: the child would copy one thread and could
        # deadlock on a lock the other thread holds. Refuse before fork, and leave this
        # process's environment untouched.
        environ = {"MARK": "original"}
        forks: list[int] = []
        real_fork = os.fork

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                forks.append(pid)
            return pid

        gate = threading.Event()

        def hold() -> None:
            gate.wait(2)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            with mock.patch.object(own_loop.os, "environ", environ), mock.patch.object(
                own_loop.os, "fork", tracking_fork
            ):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    own_loop._probe_in_screened_env(lambda: True, {"MARK": "probe"})
                self.assertEqual("copilot_login_unknown", caught.exception.cause)
                self.assertEqual("probe threaded", caught.exception.detail)
                self.assertEqual([], forks)
                self.assertEqual("original", own_loop.os.environ["MARK"])
                own_loop.os.environ["MARK"] = "concurrent"
                self.assertEqual("concurrent", own_loop.os.environ["MARK"])
        finally:
            gate.set()
            thread.join(2)
            for pid in forks:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_login_probe_hang_is_killed_at_the_deadline(self) -> None:
        child_pid = self.root / "hung-pid"

        def probe() -> bool:
            child_pid.write_text(str(os.getpid()), encoding="ascii")
            time.sleep(2)
            return True

        started = time.monotonic()
        try:
            with mock.patch.object(own_loop, "_PROBE_DEADLINE_S", 0.2, create=True):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    own_loop._probe_in_screened_env(probe, {"PATH": "/usr/bin"})
            self.assertEqual("copilot_login_unknown", caught.exception.cause)
            self.assertEqual("probe timed out", caught.exception.detail)
            self.assertLess(time.monotonic() - started, 1.0)
            pid = int(child_pid.read_text(encoding="ascii"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text(encoding="ascii")), 9)
                except (OSError, ValueError):
                    pass

    def test_login_probe_eof_stays_inside_the_deadline(self) -> None:
        opened: list[tuple[int, int]] = []
        children: list[int] = []
        real_pipe = os.pipe
        real_fork = os.fork

        def tracking_pipe() -> tuple[int, int]:
            pair = real_pipe()
            opened.append(pair)
            return pair

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                children.append(pid)
            return pid

        def probe() -> bool:
            os.close(opened[0][1])
            time.sleep(0.6)
            return True

        started = time.monotonic()
        try:
            with mock.patch.object(own_loop.os, "pipe", tracking_pipe), mock.patch.object(
                own_loop.os, "fork", tracking_fork
            ), mock.patch.object(own_loop, "_PROBE_DEADLINE_S", 0.1):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    own_loop._probe_in_screened_env(probe, {"PATH": "/usr/bin"})
            self.assertEqual("copilot_login_unknown", caught.exception.cause)
            self.assertLess(time.monotonic() - started, 0.4)
            for pid in children:
                self.assertFalse(_process_running(pid))
        finally:
            for read_fd, write_fd in opened:
                for fd in (read_fd, write_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for pid in children:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_login_probe_interrupt_during_reap_kills_and_reaps(self) -> None:
        children: list[int] = []
        real_fork = os.fork
        real_wait = os.waitpid
        waits = {"n": 0}

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                children.append(pid)
            return pid

        def raising_wait(pid: int, flags: int) -> tuple[int, int]:
            waits["n"] += 1
            if waits["n"] == 1:
                raise KeyboardInterrupt
            return real_wait(pid, flags)

        def slow() -> bool:
            time.sleep(2)
            return True

        try:
            with mock.patch.object(own_loop.os, "fork", tracking_fork), mock.patch.object(
                own_loop.os, "waitpid", raising_wait
            ), mock.patch.object(own_loop, "_read_all", lambda *_args: b""):
                started = time.monotonic()
                with self.assertRaises(KeyboardInterrupt):
                    own_loop._probe_in_screened_env(slow, {"PATH": "/usr/bin"})
                self.assertLess(time.monotonic() - started, 0.8)
            self.assertTrue(children)
            pid = children[0]
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            for pid in children:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_reap_does_not_signal_after_the_leader_is_collected(self) -> None:
        events: list[str] = []
        calls = {"n": 0}

        def waitpid(pid: int, flags: int) -> tuple[int, int]:
            calls["n"] += 1
            if calls["n"] == 1:
                events.append("reaped")
                raise KeyboardInterrupt
            raise ChildProcessError

        with mock.patch.object(own_loop, "_kill_probe_group", side_effect=lambda pid: events.append("signal")), \
                mock.patch.object(own_loop, "_live_group_members", return_value=[]), \
                mock.patch.object(own_loop.os, "waitpid", side_effect=waitpid):
            with self.assertRaises(KeyboardInterrupt):
                own_loop._reap_probe_child(123, time.monotonic() + 1)
        self.assertEqual(["signal", "reaped"], events)

    def test_reap_does_not_kill_a_member_pid_after_the_deadline(self) -> None:
        signals: list[int] = []
        with mock.patch.object(own_loop, "_kill_probe_group"), \
                mock.patch.object(own_loop, "_live_group_members", return_value=[123, 456]), \
                mock.patch.object(own_loop.os, "kill", side_effect=lambda pid, sig: signals.append(pid)), \
                mock.patch.object(own_loop.os, "waitpid", return_value=(123, 0)):
            own_loop._reap_probe_child(123, time.monotonic() - 1)
        self.assertEqual([], signals)

    def test_reap_after_the_deadline_does_not_block(self) -> None:
        waits: list[int] = []

        def waitpid(pid: int, flags: int) -> tuple[int, int]:
            waits.append(flags)
            return (0, 0) if flags == os.WNOHANG else (pid, 0)

        with mock.patch.object(own_loop, "_kill_probe_group"), \
                mock.patch.object(own_loop, "_live_group_members", return_value=[]), \
                mock.patch.object(own_loop.os, "waitpid", side_effect=waitpid), \
                mock.patch.object(own_loop, "_REAP_GRACE_S", 0.05, create=True):
            own_loop._reap_probe_child(123, time.monotonic() - 1)
        self.assertNotIn(0, waits)
        self.assertTrue(waits)

    def test_unreaped_probe_leader_is_unenforceable(self) -> None:
        children: list[int] = []
        real_fork = os.fork

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                children.append(pid)
            return pid

        def never_reap(pid: int, flags: int) -> tuple[int, int]:
            return (0, 0)

        try:
            with mock.patch.object(own_loop.os, "fork", tracking_fork), mock.patch.object(
                own_loop.os, "waitpid", never_reap
            ), mock.patch.object(own_loop, "_PROBE_DEADLINE_S", 0.05), mock.patch.object(
                own_loop, "_REAP_GRACE_S", 0.05, create=True
            ):
                with self.assertRaises(OwnLoopRouteRefused) as caught:
                    own_loop._probe_in_screened_env(lambda: time.sleep(2) or True, {"PATH": "/usr/bin"})
            self.assertEqual("probe unenforceable", caught.exception.detail)
        finally:
            for pid in children:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_probe_kills_helpers_on_every_result(self) -> None:
        markers = {
            "yes": self.root / "helper-yes",
            "no": self.root / "helper-no",
            "boom": self.root / "helper-boom",
        }

        def start(name: str) -> None:
            pid = os.fork()
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            markers[name].write_text(str(pid), encoding="ascii")

        def yes() -> bool:
            start("yes")
            return True

        def no() -> bool:
            start("no")
            return False

        def boom() -> bool:
            start("boom")
            raise OSError("locked")

        leftovers: list[int] = []
        try:
            self.assertTrue(own_loop._probe_in_screened_env(yes, {"PATH": "/usr/bin"}))
            self.assertFalse(own_loop._probe_in_screened_env(no, {"PATH": "/usr/bin"}))
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                own_loop._probe_in_screened_env(boom, {"PATH": "/usr/bin"})
            self.assertEqual("OSError", caught.exception.detail)
            for marker in markers.values():
                leftovers.append(int(marker.read_text(encoding="ascii")))
            for pid in leftovers:
                self.assertFalse(_process_running(pid))
        finally:
            for pid in leftovers:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    def test_probe_refuses_when_the_process_group_cannot_be_set(self) -> None:
        called: list[bool] = []

        def probe() -> bool:
            called.append(True)
            return True

        with mock.patch.object(own_loop.os, "setpgid", side_effect=PermissionError):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                own_loop._probe_in_screened_env(probe, {"PATH": "/usr/bin"})
        self.assertEqual("copilot_login_unknown", caught.exception.cause)
        self.assertEqual("probe unenforceable", caught.exception.detail)
        self.assertEqual([], called)

    def test_interrupt_while_arming_the_probe_closes_the_pipe(self) -> None:
        before = set(os.listdir("/proc/self/fd"))
        with mock.patch.object(own_loop.time, "monotonic", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                own_loop._probe_in_screened_env(lambda: True, {"PATH": "/usr/bin"})
        self.assertEqual(set(), set(os.listdir("/proc/self/fd")) - before)

    def test_login_probe_interrupt_kills_the_child(self) -> None:
        def slow() -> bool:
            time.sleep(2)
            return True

        def interrupt(_fd: int, _limit: int, _deadline: float | None = None) -> bytes:
            raise KeyboardInterrupt

        started = time.monotonic()
        with mock.patch.object(own_loop, "_read_all", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                own_loop._probe_in_screened_env(slow, {"PATH": "/usr/bin"})
        self.assertLess(time.monotonic() - started, 0.8)

    def test_probe_system_exit_is_a_typed_refusal(self) -> None:
        def exited() -> bool:
            sys.exit(1)

        class SystemExit(Exception):
            pass

        def named() -> bool:
            raise SystemExit("no")

        def interrupted() -> bool:
            raise KeyboardInterrupt

        route = {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"}
        parent = {"PATH": "/usr/bin", "HOME": str(self.home)}
        for probe in (exited, named):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(route, parent, has_copilot_login=probe)
            self.assertEqual("copilot_login_unknown", caught.exception.cause)
            self.assertEqual("SystemExit", caught.exception.detail)
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(route, parent, has_copilot_login=interrupted)
        self.assertEqual("KeyboardInterrupt", caught.exception.detail)

    def test_login_probe_closes_fds_when_close_is_interrupted(self) -> None:
        opened: list[tuple[int, int]] = []
        children: list[int] = []
        real_pipe = os.pipe
        real_fork = os.fork

        def tracking_pipe() -> tuple[int, int]:
            pair = real_pipe()
            opened.append(pair)
            return pair

        def tracking_fork() -> int:
            pid = real_fork()
            if pid > 0:
                children.append(pid)
            return pid

        def interrupting_close(_fd: int) -> None:
            raise KeyboardInterrupt

        try:
            with mock.patch.object(own_loop.os, "pipe", tracking_pipe), mock.patch.object(
                own_loop.os, "fork", tracking_fork
            ), mock.patch.object(own_loop.os, "close", interrupting_close):
                with self.assertRaises(KeyboardInterrupt):
                    own_loop._probe_in_screened_env(lambda: True, {"PATH": "/usr/bin"})
            for read_fd, _write_fd in opened:
                with self.assertRaises(OSError):
                    os.fstat(read_fd)
            for pid in children:
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    continue
                self.fail(f"probe child {pid} was not reaped (waitpid returned {reaped})")
        finally:
            for read_fd, write_fd in opened:
                for fd in (read_fd, write_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for pid in children:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_child_env_rejects_equals_and_empty_names(self) -> None:
        for name in ("BAD=NAME", ""):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                route_policy._copy_child_env("opencode", {name: "val"})
            self.assertEqual("child_env_malformed", caught.exception.cause)
            self.assertNotIn("BAD=NAME", str(caught.exception))
            self.assertNotIn("val", str(caught.exception))

    def test_cwd_path_and_bytes_are_screened(self) -> None:
        base = dict(
            launcher=self.calls, argv=[self.launcher_path, "run", "anthropic/m"], engine="hermes",
            profile=self._profile(), child_env=self._env(), route_home=str(self.home),
            parent_env={"HOME": str(self.operator), "PATH": "/usr/bin:/bin"},
            operator_home=str(self.operator), wrapper=self.wrapper,
        )
        cases = (
            Path(str(self.home) + "\0"),
            os.fsencode(str(self.home)) + b"\0",
            Path(str(self.home) + "\ud800"),
        )
        for cwd in cases:
            self.calls.reset_mock()
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                guard_then_spawn(**base, cwd=cwd)
            self.assertIn(caught.exception.cause, ("argv_malformed", "value_unencodable"))
            self.assertNotIn("\x00", str(caught.exception))
            self.assertNotIn("\\ud800", str(caught.exception).lower())
            self.calls.assert_not_called()
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_then_spawn(**base, cwd=7)
        self.assertEqual("argv_malformed", caught.exception.cause)
        self.calls.assert_not_called()

        class _Flip:
            def __init__(self) -> None:
                self.reads = 0

            def __fspath__(self) -> str:
                self.reads += 1
                return str(self_home) if self.reads == 1 else str(self_home) + "\0bad"

        self_home = self.home
        guard_then_spawn(**base, cwd=_Flip())
        self.assertEqual(str(self.home), self.calls.call_args.kwargs["cwd"])
        guard_then_spawn(**base, cwd=os.fsencode(str(self.home)))
        self.assertEqual(os.fsencode(str(self.home)), self.calls.call_args.kwargs["cwd"])
        buffer = bytearray(os.fsencode(str(self.home)))
        guard_then_spawn(**base, cwd=buffer)
        self.assertEqual(bytes(buffer), self.calls.call_args.kwargs["cwd"])
        self.assertIsInstance(self.calls.call_args.kwargs["cwd"], bytes)
        buffer.extend(b"\0mutated")
        self.assertNotIn(b"\0", self.calls.call_args.kwargs["cwd"])

    def test_dropped_unencodable_value_does_not_refuse(self) -> None:
        home = str(self.home)
        out = _ascii_python(
            _DROPPED_UNENCODABLE,
            {"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"},
            PROBE_HOME=home,
        )
        self.assertEqual("COPILOT_OK\nNOUS_OK", out)


_DROPPED_UNENCODABLE = r"""
import os, sys
sys.path.insert(0, os.environ["PROBE_CORE"])
from recursion_core.subscription_routes import check_copilot_own_loop, check_nous_own_loop
home = os.environ["PROBE_HOME"]
parent = {"PATH": "/usr/bin", "HOME": home, "LANG": "C", "GITHUB_TOKEN": "caf\u00e9-dropped"}
decision = check_copilot_own_loop(
    {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
    parent, has_copilot_login=lambda: True,
)
assert "GITHUB_TOKEN" not in decision.child_env
assert decision.child_env["LANG"] == "C"
nous = check_nous_own_loop(
    {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": "http://127.0.0.1:9"},
    parent,
)
assert "GITHUB_TOKEN" not in nous.child_env
print("COPILOT_OK")
print("NOUS_OK")
"""


if __name__ == "__main__":
    unittest.main()
