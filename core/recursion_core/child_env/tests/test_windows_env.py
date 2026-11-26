"""Windows child environment: an allow-list, asserted through the simulated-Windows harness.

On a POSIX host ``WindowsChildEnvTests`` is skipped and ``SimulatedWindowsEnvTests`` runs this
file again in a child whose ``os.name`` is ``nt`` (``test_simulated_windows.py``). On a Windows
host the class runs directly. Nothing is spawned. Every secret-shaped value is fake.

The route home's ``APPDATA`` and ``LOCALAPPDATA`` are the Windows locations of those variables
(``AppData/Roaming`` and ``AppData/Local`` under the route home). ``os.path.join`` supplies the
separator, so the simulated harness (posixpath) and a Windows host (ntpath) each use their own.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from recursion_core.child_env import ChildRoute, EnvRefused, build_child_env, policy, spawn

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"
CANARY = "fake-not-a-key"
OAUTH = "fake-oauth-not-a-token"
ROUTE_KEY = "fake-user-entered-key-not-a-credential"
HOME = "/route/home"
INHERITED = (
    "SystemRoot",
    "windir",
    "PATHEXT",
    "ComSpec",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def _parent() -> dict[str, str]:
    """A Windows-shaped parent. One spelling of each name. The canary must not reach the child."""

    return {
        "systemroot": r"C:\Windows",
        "windir": r"C:\Windows",
        "PATHEXT": ".COM;.EXE;.BAT",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "TEMP": r"C:\Temp",
        "TMP": r"C:\Temp",
        "NUMBER_OF_PROCESSORS": "4",
        "processor_architecture": "AMD64",
        "PATH": r"C:\Windows\System32",
        "ANTHROPIC_API_KEY": CANARY,
        "HOME": "/home/operator",
        "USERPROFILE": r"C:\Users\someone",
        "APPDATA": r"C:\Users\someone\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\someone\AppData\Local",
        "CLAUDE_CODE_OAUTH_TOKEN": OAUTH,
        "HTTPS_PROXY": "http://127.0.0.1:9",
    }


def _profile_paths(home: str = HOME) -> dict[str, str]:
    return {
        "USERPROFILE": home,
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
    }


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsChildEnvTests(unittest.TestCase):
    def test_allow_list_drops_the_canary_and_sets_the_route_home(self) -> None:
        parent = _parent()
        for engine in ("codex", "claude_code"):
            with self.subTest(engine=engine):
                env = build_child_env(engine, home=HOME, auth_mode="subscription", parent=parent)
                self.assertEqual(set(INHERITED) | set(_profile_paths()), set(env))
                self.assertEqual(r"C:\Windows", env["SystemRoot"])
                self.assertEqual(r"C:\Windows", env["windir"])
                self.assertEqual(".COM;.EXE;.BAT", env["PATHEXT"])
                self.assertEqual(r"C:\Windows\System32\cmd.exe", env["ComSpec"])
                self.assertEqual(r"C:\Temp", env["TEMP"])
                self.assertEqual(r"C:\Temp", env["TMP"])
                self.assertEqual("4", env["NUMBER_OF_PROCESSORS"])
                self.assertEqual("AMD64", env["PROCESSOR_ARCHITECTURE"])
                self.assertEqual(_profile_paths(), {name: env[name] for name in _profile_paths()})
                self.assertNotIn(CANARY, env.to_dict().values())
                self.assertNotIn(OAUTH, env.to_dict().values())
                self.assertNotIn(r"C:\Users\someone", env.to_dict().values())
                self.assertNotIn("PATH", env)
                self.assertNotIn("HOME", env)
                self.assertNotIn(CANARY, repr(env))
                self.assertEqual(
                    ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HOME", "HTTPS_PROXY", "PATH"),
                    env.dropped,
                )
                self.assertEqual(engine, env.engine)
                self.assertEqual("subscription", env.auth_mode)

    def test_two_spellings_of_one_name_are_refused(self) -> None:
        parent = _parent()
        parent["Path"] = r"C:\Other"
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent)
        self.assertEqual(("PATH", "env_name_ambiguous"), (caught.exception.name, caught.exception.cause))
        self.assertNotIn(CANARY, str(caught.exception))
        # The same rule for an allow-listed name, not only for Path.
        parent = _parent()
        parent["Temp"] = r"C:\OtherTemp"
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=HOME, auth_mode="subscription", parent=parent)
        self.assertEqual(("TEMP", "env_name_ambiguous"), (caught.exception.name, caught.exception.cause))

    def test_spawn_still_refuses_platform_unsupported(self) -> None:
        env = build_child_env("codex", home=HOME, auth_mode="subscription", parent=_parent())
        calls: list = []
        with self.assertRaises(EnvRefused) as caught:
            spawn([sys.executable, "-c", "pass"], env, popen=lambda *args, **kwargs: calls.append(args))
        self.assertEqual((None, "platform_unsupported"), (caught.exception.name, caught.exception.cause))
        self.assertEqual([], calls)

    def test_a_route_key_is_the_routes_and_the_parent_canary_stays_out(self) -> None:
        parent = _parent()
        parent["CODEX_API_KEY"] = CANARY
        for engine, key_name in (("claude_code", "ANTHROPIC_API_KEY"), ("codex", "CODEX_API_KEY")):
            with self.subTest(engine=engine):
                env = build_child_env(
                    engine, home=HOME, auth_mode="api_key", route=ChildRoute(api_key=ROUTE_KEY), parent=parent,
                )
                self.assertEqual(ROUTE_KEY, env[key_name])
                self.assertNotIn(CANARY, env.to_dict().values())
                self.assertIn("ANTHROPIC_API_KEY", env.dropped)
                self.assertIn(key_name, env.dropped)
                self.assertNotIn(ROUTE_KEY, repr(env))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=HOME, auth_mode="api_key", parent=parent)
        self.assertEqual(("ANTHROPIC_API_KEY", "route_key_missing"), (caught.exception.name, caught.exception.cause))

    def test_a_cloud_provider_switch_comes_from_the_route(self) -> None:
        parent = _parent()
        parent["CLAUDE_CODE_USE_BEDROCK"] = "0"
        env = build_child_env(
            "claude_code", home=HOME, auth_mode="cloud_provider",
            route=ChildRoute(provider_switches=("CLAUDE_CODE_USE_BEDROCK",)), parent=parent,
        )
        self.assertEqual("1", env["CLAUDE_CODE_USE_BEDROCK"])
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", env.dropped)
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "codex", home=HOME, auth_mode="cloud_provider",
                route=ChildRoute(provider_switches=("CLAUDE_CODE_USE_BEDROCK",)), parent=parent,
            )
        self.assertEqual((None, "auth_mode_unsupported"), (caught.exception.name, caught.exception.cause))

    def test_refusals_that_are_not_an_unchecked_environment(self) -> None:
        parent = _parent()
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("not-an-engine", home=HOME, auth_mode="subscription", parent=parent)
        self.assertEqual((None, "unknown_engine"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, parent=parent)
        self.assertEqual((None, "auth_mode_missing"), (caught.exception.name, caught.exception.cause))
        for home in ("not-absolute", "", "/route/../etc", "/"):
            with self.subTest(home=home), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex", home=home, auth_mode="subscription", parent=parent)
            self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        bad = _parent()
        bad["TEMP"] = "C:\\Temp\0hidden"
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=bad)
        self.assertEqual(("TEMP", "value_malformed"), (caught.exception.name, caught.exception.cause))

    def test_overrides_cannot_widen_the_allow_list(self) -> None:
        parent = _parent()
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent,
                            overrides={"ANTHROPIC_API_KEY": ROUTE_KEY})
        self.assertEqual(("ANTHROPIC_API_KEY", "override_denied"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent,
                            overrides={"PATH": r"C:\Windows"})
        self.assertEqual(("PATH", "override_denied"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent,
                            overrides={"USERPROFILE": "/other"})
        self.assertEqual(("USERPROFILE", "override_denied"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent,
                            overrides={"LD_PRELOAD": "/mock/not-loaded.so"})
        self.assertEqual(("LD_PRELOAD", "override_not_allowed"), (caught.exception.name, caught.exception.cause))
        env = build_child_env("codex", home=HOME, auth_mode="subscription", parent=parent,
                               overrides={"comspec": r"C:\Windows\System32\cmd.exe"})
        self.assertEqual(r"C:\Windows\System32\cmd.exe", env["ComSpec"])
        self.assertNotIn(CANARY, env.to_dict().values())

    def test_a_second_call_does_not_keep_the_first_parents_values(self) -> None:
        first = build_child_env("codex", home=HOME, auth_mode="subscription", parent=_parent())
        other = _parent()
        other["systemroot"] = r"D:\Windows"
        second = build_child_env("claude_code", home="/route/other", auth_mode="subscription", parent=other)
        self.assertEqual(r"C:\Windows", first["SystemRoot"])
        self.assertEqual(r"D:\Windows", second["SystemRoot"])
        self.assertEqual(HOME, first["USERPROFILE"])
        self.assertEqual("/route/other", second["USERPROFILE"])
        self.assertNotIn(CANARY, second.to_dict().values())

    def test_owner_checks_stay_unavailable(self) -> None:
        # The allow-list does not turn the POSIX owner helpers on. spawn still reads this as unsupported.
        self.assertFalse(policy._posix_supported())
        self.assertIsNone(policy._posix_euid())


@unittest.skipIf(os.name == "nt", "simulation for POSIX hosts; a Windows host runs WindowsChildEnvTests")
class SimulatedWindowsEnvTests(unittest.TestCase):
    def test_the_suite_passes_under_the_simulated_windows_harness(self) -> None:
        from recursion_core.child_env.tests.test_simulated_windows import simulate

        result = simulate(str(Path(__file__).resolve()))
        bad = [f"{kind}: {row['test']}: {row['exception']}"
               for kind in ("errors", "failures") for row in result[kind]]
        self.assertEqual([], bad, f"{result['run']} run, {result['skipped']} skipped")
        # Nine WindowsChildEnvTests methods. The driver class itself is skipped in the child.
        self.assertGreaterEqual(result["run"], 8)
        for name in (
            "WindowsChildEnvTests.test_allow_list_drops_the_canary_and_sets_the_route_home",
            "WindowsChildEnvTests.test_two_spellings_of_one_name_are_refused",
            "WindowsChildEnvTests.test_spawn_still_refuses_platform_unsupported",
        ):
            self.assertNotIn(name, result["skipped_tests"])


if __name__ == "__main__":
    unittest.main()
