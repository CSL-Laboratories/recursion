"""CSL-785: child-environment allowlist and launch boundary in the new core (candidate tests).

Run from the repository root: ``python3 -B -m unittest discover -s engine/child_env/tests -t . -v``.
No engine, provider, network or real credential is used; every secret-shaped value is fake.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.child_env import BASE_NAMES, ENGINE_PROFILES, EnvRefused, ValidatedEnv, build_child_env, spawn
from engine.child_env.tests import cells

FAKE = "fake-value-not-a-credential"


def _home(test: unittest.TestCase) -> str:
    home = tempfile.mkdtemp(prefix="csl785-home-")
    test.addCleanup(shutil.rmtree, home, True)
    return home


@unittest.skipIf(os.name == "nt", "POSIX rules")
class CellTests(unittest.TestCase):
    """C2-37: 15 value cells; C2-38: 14 PATH and 16 NO_PROXY cells; no false refusal; n=5 each."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = {row["cell"]: row for row in cells.run_all(cells.N)}

    def _group(self, *groups: str) -> list[dict]:
        return [row for row in self.rows.values() if row["group"] in groups]

    def _assert_all(self, rows: list[dict], count: int) -> None:
        self.assertEqual(count, len(rows))
        for row in rows:
            with self.subTest(cell=row["cell"]):
                self.assertEqual((cells.N, cells.N), (row["n"], row["pass"]), row)

    def test_fifteen_value_cells_closed(self) -> None:
        self._assert_all(self._group("value"), 15)

    def test_fourteen_path_cells_closed(self) -> None:
        self._assert_all(self._group("path"), 14)

    def test_sixteen_no_proxy_cells_closed(self) -> None:
        self._assert_all(self._group("no_proxy"), 16)

    def test_no_false_refusal(self) -> None:
        self._assert_all(self._group("ordinary", "baseline", "path_ok", "no_proxy_ok"), 3 + 9 + 5 + 6)

    def test_extras_and_c2_40_shapes(self) -> None:
        self._assert_all(self._group("extra", "c2_40"), 8 + 6)

    def test_the_fifteenth_path_cell_is_open_by_default_and_closed_by_the_scan_switch(self) -> None:
        self._assert_all(self._group("path_open", "path_scan"), 2)


class ProfileTests(unittest.TestCase):
    def test_unknown_or_wrapped_engines_refuse(self) -> None:
        home = _home(self)
        for engine in ("claude", "Codex", "env", "sh", "npx", "codex-cli", "", None, "pi", "hermes", "opencode"):
            with self.subTest(engine=engine), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, parent={"PATH": "/usr/bin"})
            self.assertEqual("unknown_engine", caught.exception.cause)

    def test_only_allowlisted_names_reach_either_engine(self) -> None:
        home = _home(self)
        planted = {name: FAKE for name in (
            "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN",
            "ANTHROPIC_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_BASE_URL",
            "CLAUDE_CONFIG_DIR", "GEMINI_API_KEY", "XAI_API_KEY", "CURSOR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "NPM_TOKEN", "DATABASE_PASSWORD", "MY_SERVICE_SECRET",
            "SSH_AUTH_SOCK", "LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH", "ALL_PROXY", "XDG_RUNTIME_DIR",
            "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_SHELL", "CLAUDE_ENV_FILE", "CODEX_CA_CERTIFICATE")}
        parent = {"PATH": "/usr/bin:/bin", "HOME": "/home/operator", "LANG": "C.UTF-8", **planted}
        for engine, profile in ENGINE_PROFILES.items():
            with self.subTest(engine=engine):
                env = build_child_env(engine, home=home, parent=parent)
                self.assertLessEqual(set(env), set(BASE_NAMES))
                self.assertFalse(set(planted) & set(env))
                self.assertFalse(set(profile.scrub) & set(env))
                self.assertEqual(home, env["HOME"])
                self.assertNotIn(FAKE, env.to_dict().values())
                self.assertLessEqual(set(planted), set(env.dropped))
                self.assertNotIn(FAKE, repr(env))

    def test_codex_scrub_names_are_listed(self) -> None:
        self.assertLessEqual({"CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"}, set(ENGINE_PROFILES["codex"].scrub))
        self.assertLessEqual({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}, set(ENGINE_PROFILES["claude_code"].scrub))
        for profile in ENGINE_PROFILES.values():
            self.assertFalse(set(profile.scrub) & set(profile.allowed), profile.engine)

    def test_engine_hosts_differ_per_profile(self) -> None:
        home = _home(self)
        parent = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": ".openai.com"}
        self.assertEqual(".openai.com", build_child_env("claude_code", home=home, parent=parent)["NO_PROXY"])
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=home, parent=parent)
        self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (caught.exception.name, caught.exception.cause))

    def test_overrides_never_carry_credentials(self) -> None:
        home = _home(self)
        for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "MY_TOKEN", "ANTHROPIC_BASE_URL", "CODEX_HOME", "HOME", "PATH"):
            engine = "codex" if name.startswith(("CODEX", "OPENAI")) else "claude_code"
            with self.subTest(name=name), self.assertRaises(EnvRefused) as caught:
                build_child_env(engine, home=home, parent={}, overrides={name: FAKE})
            self.assertEqual("override_denied", caught.exception.cause)
        env = build_child_env("claude_code", home=home, parent={}, overrides={"DISABLE_AUTOUPDATER": "1"})
        self.assertEqual("1", env["DISABLE_AUTOUPDATER"])

    def test_home_must_be_private(self) -> None:
        home = _home(self)
        os.chmod(home, 0o755)
        for value in (home, "relative/home", "/nonexistent-csl785", None):
            with self.subTest(home=value), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=value, parent={})
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))

    def test_validated_env_cannot_be_hand_made(self) -> None:
        with self.assertRaises(TypeError):
            ValidatedEnv({"PATH": "/usr/bin"}, "codex", ())

    def test_windows_refuses_instead_of_passing_values_unchecked(self) -> None:
        with mock.patch("engine.child_env.policy.os.name", "nt"), self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home="C:\\x", parent={})
        self.assertEqual("platform_unsupported", caught.exception.cause)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class LaunchTests(unittest.TestCase):
    """The boundary passes exactly the validated env, never the parent's, and refuses before Popen."""

    def setUp(self) -> None:
        self.home = _home(self)
        self.bin = Path(_home(self))
        self.engine_bin = self.bin / "fake-engine"
        self.engine_bin.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.engine_bin.chmod(0o755)
        self.popen = mock.MagicMock(name="Popen")

    def _env(self, engine: str = "codex", **extra: str) -> ValidatedEnv:
        parent = {"PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "C.UTF-8", **extra}
        return build_child_env(engine, home=self.home, parent=parent)

    def test_popen_gets_exactly_the_validated_env_not_the_parent(self) -> None:
        planted = {"CODEX_API_KEY": FAKE, "CODEX_ACCESS_TOKEN": FAKE, "OPENAI_API_KEY": FAKE, "ANTHROPIC_API_KEY": FAKE}
        with mock.patch.dict(os.environ, planted):
            env = self._env(**planted)
            spawn(["fake-engine", "exec", "--json"], env, cwd=self.home, popen=self.popen)
        self.popen.assert_called_once()
        (argv,), kwargs = self.popen.call_args
        self.assertEqual([str(self.engine_bin), "exec", "--json"], argv)
        self.assertIsNot(kwargs["env"], os.environ)
        self.assertEqual(env.to_dict(), kwargs["env"])
        self.assertFalse(set(planted) & set(kwargs["env"]))
        self.assertEqual((False, True, True), (kwargs["shell"], kwargs["close_fds"], kwargs["start_new_session"]))

    def test_a_plain_mapping_or_os_environ_is_refused(self) -> None:
        for env in (dict(os.environ), os.environ, {"PATH": "/usr/bin"}, None):
            with self.subTest(env=type(env).__name__), self.assertRaises(TypeError):
                spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()

    def test_caller_cannot_replace_env_or_widen_the_launch(self) -> None:
        env = self._env()
        for key in ("env", "shell", "executable", "preexec_fn", "pass_fds", "close_fds"):
            with self.subTest(key=key), self.assertRaises((EnvRefused, TypeError)):  # env= is a TypeError
                spawn(["fake-engine"], env, popen=self.popen, **{key: None})
        self.popen.assert_not_called()

    def test_argv0_resolves_only_on_the_validated_path(self) -> None:
        env = build_child_env("codex", home=self.home, parent={"PATH": "/usr/bin:/bin"})
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin"}), self.assertRaises(EnvRefused) as caught:
            spawn(["fake-engine"], env, popen=self.popen)
        self.assertEqual("argv0_not_found", caught.exception.cause)
        for argv0, cause in (("./fake-engine", "argv0_relative"), ("bin/fake-engine", "argv0_relative")):
            with self.subTest(argv0=argv0), self.assertRaises(EnvRefused) as caught:
                spawn([argv0], env, popen=self.popen)
            self.assertEqual(cause, caught.exception.cause)
        self.popen.assert_not_called()

    def test_an_engine_binary_someone_else_can_replace_is_refused(self) -> None:
        loose = Path(_home(self))
        (loose / "fake-engine").write_text("#!/bin/sh\n", encoding="ascii")
        (loose / "fake-engine").chmod(0o755)
        loose.chmod(0o777)
        env = self._env()
        with self.assertRaises(EnvRefused) as caught:
            spawn([str(loose / "fake-engine")], env, popen=self.popen)
        self.assertEqual("argv0_value_chain_unsafe", caught.exception.cause)
        self.popen.assert_not_called()

    def test_a_refused_environment_never_reaches_popen(self) -> None:
        with self.assertRaises(EnvRefused):
            env = self._env(HTTPS_PROXY="http://u:p@127.0.0.1:9")
            spawn(["fake-engine"], env, popen=self.popen)
        self.popen.assert_not_called()

    def test_real_child_sees_only_the_validated_env(self) -> None:
        """One real local child (/usr/bin/env, no engine): it prints exactly the validated names."""

        if not os.path.exists("/usr/bin/env"):
            self.skipTest("needs /usr/bin/env")
        with mock.patch.dict(os.environ, {"CODEX_API_KEY": FAKE, "OPENAI_API_KEY": FAKE}):
            env = build_child_env("codex", home=self.home, parent={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                                                  "CODEX_API_KEY": FAKE})
            proc = spawn(["/usr/bin/env"], env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = proc.communicate(timeout=10)
        names = {line.split("=", 1)[0] for line in out.decode().splitlines() if "=" in line}
        self.assertEqual(set(env), names)
        self.assertNotIn(FAKE, out.decode())

    def test_route_temp_root_is_private(self) -> None:
        env = self._env(TMPDIR="/tmp")
        st = os.lstat(env["TMPDIR"])
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual((os.geteuid(), 0), (st.st_uid, st.st_mode & 0o077))


if __name__ == "__main__":
    unittest.main(verbosity=2)
