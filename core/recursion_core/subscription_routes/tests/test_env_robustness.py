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

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual((False, "", "recursion_holds_no_token"), (doctor["ok"], doctor["address"], doctor["cause"]))
        self.assertNotIn(_SECRET, str(doctor))
        http = nous_proxy_doctor(f"http://user:{_SECRET}@10.1.2.3:9")
        self.assertEqual("", http["address"])
        self.assertNotIn(_SECRET, str(http))

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


if __name__ == "__main__":
    unittest.main()
