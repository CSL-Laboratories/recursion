"""Hostile environments for the merged child-env allowlist (CSL-785).

Run from the repository root: ``python3 -m pytest core/recursion_core/child_env/tests/test_env_robustness.py -q``.
No engine, provider or network. Credential-shaped values are fake.

XDG is observed and not changed. ``XDG_RUNTIME_DIR`` is on the allowlist and a relative value
refuses ``value_relative``. ``XDG_CONFIG_HOME`` and ``XDG_DATA_HOME`` are not on the allowlist,
so a relative value is dropped with the rest of the parent environment.
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

from recursion_core.child_env import EnvRefused, build_child_env, policy, spawn
from recursion_core.child_env.tests.cells import owned_tmpdir

_CORE = str(Path(__file__).resolve().parents[3])
_SURROGATE = bytes([0xFF]).decode("utf-8", "surrogateescape")
_LATIN = bytes([0xFF, 0xFE]).decode("utf-8", "surrogateescape")
_SECRET = "fake-parent-secret-not-a-credential"


def _locale_python(kind: str, locale: dict[str, str]) -> str:
    """Run build_child_env and spawn in a fresh interpreter. The script is ASCII."""

    script = r"""
import os, sys
sys.path.insert(0, os.environ["PROBE_CORE"])
from recursion_core.child_env import build_child_env, spawn, EnvRefused
home = os.environ["PROBE_HOME"]
kind = os.environ["PROBE_KIND"]
parent = {"PATH": "/usr/bin:/bin"}
if kind == "unicode":
    parent["LANG"] = "h\u00e9llo"
    parent["USER"] = "caf\u00e9"
else:
    raw = bytes([255]).decode("utf-8", "surrogateescape")
    parent["LANG"] = "C" + raw
    parent["USER"] = raw
try:
    env = build_child_env("codex", home=home, auth_mode="subscription", parent=parent)
except EnvRefused as exc:
    blob = str(exc).encode("ascii", "backslashreplace").decode("ascii")
    leaked = "caf" in blob or "\\xe9" in blob or "\\u00e9" in blob
    print("LEAK" if leaked else "REFUSED", exc.name, exc.cause)
else:
    try:
        proc = spawn(["/bin/true"], env)
        code = proc.wait(timeout=5)
        print("SPAWN", code)
    except EnvRefused as exc:
        print("REFUSED", exc.name, exc.cause)
    except Exception as exc:
        print("CRASH", type(exc).__name__)
"""
    home = owned_tmpdir("csl785-locale-")
    try:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROBE_CORE": _CORE, "PROBE_HOME": home,
               "PROBE_KIND": kind, "PYTHONSAFEPATH": "1", "PYTHONIOENCODING": "ascii:backslashreplace"}
        env.update(locale)
        ran = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True,
                             timeout=30)
    finally:
        shutil.rmtree(home, True)
    if ran.returncode != 0 and "REFUSED" not in ran.stdout and "SPAWN" not in ran.stdout:
        raise AssertionError(ran.stderr[-500:] or ran.stdout)
    return ran.stdout.strip()


@unittest.skipIf(os.name == "nt", "POSIX environment rules")
class ChildEnvRobustness(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(owned_tmpdir("csl785-robust-"))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for dirpath, dirnames, _filenames in os.walk(self.root):
            os.chmod(dirpath, 0o700)
            for name in dirnames:
                os.chmod(os.path.join(dirpath, name), 0o700)
        shutil.rmtree(self.root, True)

    def _build(self, parent: dict, home: str | None = None, **kwargs):
        if home is None:
            home = str(self.home)
        return build_child_env("codex", home=home, auth_mode="subscription", parent=parent, **kwargs)

    def test_spawn_refuses_unencodable_unicode_under_ascii_locale(self) -> None:
        # LANG=C with UTF-8 mode off: filesystem encoding is ASCII. "é" cannot be encoded,
        # and Popen used to raise UnicodeEncodeError.
        out = _locale_python("unicode", {"LANG": "C", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
        self.assertTrue(out.startswith("REFUSED "), out)
        self.assertIn("value_unencodable", out)
        self.assertNotIn("LEAK", out)

    def test_surrogate_bytes_still_spawn_under_ascii_locale(self) -> None:
        # Non-UTF-8 bytes are surrogates. fsencode keeps them; launch must not raise UnicodeEncodeError.
        out = _locale_python("surrogate", {"LANG": "C", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
        self.assertEqual(out, "SPAWN 0")

    def test_utf8_locale_still_spawns_unicode_values(self) -> None:
        out = _locale_python("unicode", {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        self.assertEqual(out, "SPAWN 0")

    def test_null_no_proxy_refuses_instead_of_valueerror(self) -> None:
        # NO_PROXY is not a bypass when no proxy is set, so a null used to reach Popen and raise ValueError.
        with self.assertRaises(EnvRefused) as caught:
            self._build({"PATH": "/usr/bin:/bin", "NO_PROXY": "localhost\0"})
        self.assertEqual(("NO_PROXY", "value_malformed"), (caught.exception.name, caught.exception.cause))
        self.assertNotIn("localhost", str(caught.exception))

    def test_non_string_parent_key_refuses(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            self._build({"PATH": "/usr/bin:/bin", 1: "x"})  # type: ignore[dict-item]
        self.assertEqual("value_malformed", caught.exception.cause)

    def test_xdg_relative_paths_are_unchanged(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            self._build({"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": "relative/run"})
        self.assertEqual(("XDG_RUNTIME_DIR", "value_relative"), (caught.exception.name, caught.exception.cause))
        env = self._build({"PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": "relative/cfg",
                           "XDG_DATA_HOME": "relative/data", "XDG_STATE_HOME": "relative/state",
                           "XDG_CACHE_HOME": "relative/cache"})
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            self.assertNotIn(name, env)

    def test_hostile_env_matrix(self) -> None:
        base = {"PATH": "/usr/bin:/bin", "LANG": "C"}
        rows: list[tuple[str, str]] = []

        def record(cell: str, fn) -> None:
            try:
                fn()
            except EnvRefused as exc:
                rows.append((cell, "refused:" + exc.cause))
            except Exception as exc:
                rows.append((cell, "CRASH:" + type(exc).__name__))
            else:
                rows.append((cell, "ok"))

        record("clean", lambda: self._build(base))
        record("surrogate-lang", lambda: self._build({**base, "LANG": "C" + _SURROGATE, "USER": _LATIN}))
        record("unicode-lang", lambda: self._build({**base, "LANG": "caf\u00e9", "USER": "h\u00e9llo"}))
        record("empty-home", lambda: self._build(base, home=""))
        record("relative-home", lambda: self._build(base, home="rel/home"))
        record("long-component-home", lambda: self._build(base, home=str(self.root / ("a" * 300))))
        record("secret-dropped", lambda: self._build({**base, "ANTHROPIC_API_KEY": _SECRET, "GITHUB_TOKEN": _SECRET}))

        uni = self.root / "caf\u00e9"
        uni.mkdir(mode=0o700)
        record("unicode-home", lambda: self._build(base, home=str(uni)))

        link = self.root / "linkhome"
        link.symlink_to(self.home, target_is_directory=True)
        record("symlink-home", lambda: self._build(base, home=str(link)))

        readonly = self.root / "ro"
        readonly.mkdir(mode=0o700)
        os.chmod(readonly, 0o500)
        record("readonly-home", lambda: self._build(base, home=str(readonly)))

        deep = self.root / "long"
        deep.mkdir(mode=0o700)
        cursor = deep
        while len(str(cursor)) < 520:
            cursor = cursor / "dd"
            cursor.mkdir(mode=0o700)
        self.assertGreater(len(str(cursor)), 500)
        record("long-path-over-500", lambda: self._build(base, home=str(cursor)))

        env = self._build({**base, "LANG": "C" + _SURROGATE, "ANTHROPIC_API_KEY": _SECRET})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn(_SECRET, env.values())
        proc = spawn(["/bin/true"], env)
        self.assertEqual(0, proc.wait(timeout=5))
        rows.append(("spawn-surrogate-lang", "ok"))

        saved = os.umask(0)
        try:
            policy._TMP.clear()
            record("umask-000-build", lambda: self._build({**base, "TMPDIR": "/tmp"}))
            built = self._build({**base, "TMPDIR": "/tmp"})
            mode = stat.S_IMODE(os.lstat(built["TMPDIR"]).st_mode)
            rows.append(("umask-000-tmpdir-mode", "ok" if mode == 0o700 else f"UNSAFE:{oct(mode)}"))
        finally:
            os.umask(saved)
        saved = os.umask(0o077)
        try:
            policy._TMP.clear()
            built = self._build({**base, "TMPDIR": "/tmp"})
            mode = stat.S_IMODE(os.lstat(built["TMPDIR"]).st_mode)
            rows.append(("umask-077-tmpdir-mode", "ok" if mode == 0o700 else f"UNSAFE:{oct(mode)}"))
        finally:
            os.umask(saved)

        crashes = [row for row in rows if row[1].startswith("CRASH") or row[1].startswith("UNSAFE")]
        self.assertEqual([], crashes, rows)
        expected = {
            "clean": "ok",
            "surrogate-lang": "ok",
            "unicode-lang": "ok",
            "empty-home": "refused:home_unsafe",
            "relative-home": "refused:home_unsafe",
            "long-component-home": "refused:home_unsafe",
            "secret-dropped": "ok",
            "unicode-home": "ok",
            "symlink-home": "refused:home_unsafe",
            "readonly-home": "ok",
            "long-path-over-500": "ok",
            "spawn-surrogate-lang": "ok",
            "umask-000-build": "ok",
            "umask-000-tmpdir-mode": "ok",
            "umask-077-tmpdir-mode": "ok",
        }
        self.assertEqual(expected, dict(rows))


if __name__ == "__main__":
    unittest.main()
