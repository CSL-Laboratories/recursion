"""The macOS LaunchAgent plist and the launchctl plan. No launchctl is started; files stay under a temporary home.

The product module plans ``bootstrap`` and ``bootout`` and does not run them. A test passes a callable
that stands in for ``launchctl print`` when uninstall must know whether the agent is loaded.
"""

from __future__ import annotations

import os
import plistlib
import stat
import tempfile
import unittest

from recursion_core.daemon_install import (
    MARKER,
    STATE_HOME,
    InstallRefused,
    Roots,
    daemon_exec_args,
    owned_paths,
)
from recursion_core.daemon_install.launchd import (
    LABEL,
    install_agent,
    leftovers_agent,
    plan_commands_darwin,
    render_plist,
    uninstall_agent,
    upgrade_agent,
)

PY = "/opt/recursion/venv/bin/python3.14t"
PY_NEXT = "/opt/recursion/venv-2/bin/python3.14t"
POSIX_PATHS = os.path.isabs(PY)
EXEC = daemon_exec_args(PY) if POSIX_PATHS else ()
EXEC_NEXT = daemon_exec_args(PY_NEXT) if POSIX_PATHS else ()
UID = 501


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


class _Launchctl:
    """Records every argv and answers ``print`` with an exit status. Any other subcommand is a failure."""

    def __init__(self, code: int = 1) -> None:
        self.code = code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> int:
        self.calls.append(tuple(argv))
        if tuple(argv[:2]) != ("launchctl", "print"):
            raise AssertionError(argv)
        return self.code


@unittest.skipUnless(POSIX_PATHS, "the interpreter path is absolute only on this host's rules")
class RenderTests(unittest.TestCase):
    def test_the_plist_round_trips_with_the_agent_keys(self) -> None:
        raw = render_plist(EXEC, LABEL)
        again = render_plist(EXEC, LABEL)
        self.assertEqual(raw, again)
        doc = plistlib.loads(raw)
        self.assertIs(doc["AbandonProcessGroup"], True)
        self.assertEqual(doc["ProcessType"], "Standard")
        self.assertNotEqual(doc["ProcessType"], "Background")
        self.assertIs(doc["RunAtLoad"], True)
        self.assertEqual(doc["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(doc["Label"], LABEL)
        self.assertEqual(doc["RecursionInstaller"], MARKER)
        self.assertEqual(doc["ProgramArguments"], [PY, "-I", "-X", "gil=0", "-m", "recursion_core.daemon"])
        self.assertEqual(tuple(doc["ProgramArguments"]), EXEC)  # this path has no %, so unescaped is the vector
        env = doc["EnvironmentVariables"]
        self.assertEqual(set(env), {"XDG_STATE_HOME", "PATH"})
        self.assertEqual(env["XDG_STATE_HOME"], "~/.local/state")
        self.assertEqual(env["PATH"].split(":"), ["/opt/recursion/venv/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        self.assertNotEqual(env["PATH"], os.environ.get("PATH"))
        self.assertEqual(doc["StandardOutPath"], "~/Library/Logs/Recursion/daemon.log")
        self.assertEqual(doc["StandardErrorPath"], "~/Library/Logs/Recursion/daemon.err.log")
        # Nothing but the named variables is carried, so a credential in the environment cannot reach the plist.
        self.assertNotIn(b"PYTHONPATH", raw)
        self.assertNotIn(b"SECRET", raw)
        self.assertNotIn(b"TOKEN", raw)

    def test_program_arguments_are_daemon_exec_args_with_systemd_percents_undone(self) -> None:
        path = "/opt/release%Q/bin/python3.14t"
        args = daemon_exec_args(path)
        self.assertEqual(args[0], "/opt/release%%Q/bin/python3.14t")
        doc = plistlib.loads(render_plist(args, LABEL))
        self.assertEqual(doc["ProgramArguments"], [path, "-I", "-X", "gil=0", "-m", "recursion_core.daemon"])
        self.assertEqual(doc["EnvironmentVariables"]["PATH"].split(":")[0], "/opt/release%Q/bin")

    def test_a_label_with_a_slash_or_a_dotdot_is_refused(self) -> None:
        for bad in ("a/b", "..", "../x", "x/..", "foo/../../etc", "a..b", "", "has space", "récursion", None, 3):
            with self.subTest(bad=bad):
                with self.assertRaises(InstallRefused) as caught:
                    render_plist(EXEC, bad)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.kind, "label_invalid")

    def test_exec_args_follow_the_unit_vector_rules(self) -> None:
        for bad in ((), ["not-a-tuple"], ("python3",), ("/bin/python3", "has space"), ("/opt/a/../bin/python3",)):
            with self.subTest(bad=bad):
                with self.assertRaises(InstallRefused) as caught:
                    render_plist(bad, LABEL)  # type: ignore[arg-type]
                self.assertIn(caught.exception.kind, {"exec_args", "exec_arg"})

    def test_the_module_names_no_process_and_no_stop_signal(self) -> None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launchd.py")
        text = _read(path).decode("utf-8")
        for needle in ("subprocess", "os.kill", "killpg", "SIGKILL", "signal."):
            self.assertNotIn(needle, text)


class CommandTests(unittest.TestCase):
    def test_plan_argv_is_bootstrap_and_bootout_and_an_unknown_action_is_refused(self) -> None:
        plist = "/Users/me/Library/LaunchAgents/recursiond.plist"
        self.assertEqual(
            plan_commands_darwin("install", UID, LABEL, plist=plist),
            (("launchctl", "bootstrap", "gui/501", plist),),
        )
        self.assertEqual(
            plan_commands_darwin("uninstall", UID, LABEL),
            (("launchctl", "bootout", "gui/501/recursiond"),),
        )
        self.assertEqual(
            plan_commands_darwin("upgrade", UID, LABEL, plist=plist),
            (
                ("launchctl", "bootout", "gui/501/recursiond"),
                ("launchctl", "bootstrap", "gui/501", plist),
            ),
        )
        # No home is in the signature: the contract path is the one the order names, with a tilde.
        self.assertEqual(
            plan_commands_darwin("install", UID, LABEL),
            (("launchctl", "bootstrap", "gui/501", "~/Library/LaunchAgents/recursiond.plist"),),
        )
        for action in ("purge", "stop", "kill", ""):
            with self.subTest(action=action):
                with self.assertRaises(InstallRefused) as caught:
                    plan_commands_darwin(action, UID, LABEL)
                self.assertEqual(caught.exception.kind, "action")
        for uid in (-1, True, "501", None, 1.5):
            with self.subTest(uid=uid):
                with self.assertRaises(InstallRefused) as caught:
                    plan_commands_darwin("install", uid, LABEL)  # type: ignore[arg-type]
                self.assertEqual(caught.exception.kind, "uid")
        with self.assertRaises(InstallRefused) as caught:
            plan_commands_darwin("install", UID, "a/b")
        self.assertEqual(caught.exception.kind, "label_invalid")


@unittest.skipUnless(os.name == "posix", "the installer writes a home, which these tests do on POSIX")
class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="launchd-")
        self.addCleanup(self._tmp.cleanup)
        made = os.path.join(self._tmp.name, "home")
        os.mkdir(made, 0o700)
        self.roots = Roots(home=made)
        self.paths = owned_paths(self.roots)
        self.home = self.paths.home
        self.plist = os.path.join(self.home, "Library", "LaunchAgents", LABEL + ".plist")
        self.state = os.path.join(self.home, ".local", "state", "recursion")
        os.makedirs(self.state, 0o700)
        self.runs = os.path.join(self.state, "runs.sqlite3")
        with open(self.runs, "wb") as handle:
            handle.write(b"rows-v1")
        self.homes = os.path.join(self.state, "homes")
        self.state_home = os.path.join(self.home, *STATE_HOME.split("/"))

    def _install(self, exec_args: tuple[str, ...] = EXEC):
        return install_agent(self.roots, exec_args, UID, LABEL)

    def test_install_is_idempotent_and_the_second_caller_changes_nothing(self) -> None:
        runs_before = _read(self.runs)
        plan = self._install()
        self.assertEqual((plan.action, plan.changed, plan.unit), ("install", True, self.plist))
        self.assertEqual(plan.commands, (("launchctl", "bootstrap", "gui/501", self.plist),))
        self.assertEqual(plan.stores[0][0], "breaker_store")
        self.assertTrue(plan.stores[0][2])
        self.assertEqual(plan.stores[1][0], "revocation_store")
        blob = _read(self.plist)
        self.assertEqual(blob, render_plist(EXEC, LABEL, state_home=self.state_home))
        doc = plistlib.loads(blob)
        self.assertEqual(doc["EnvironmentVariables"]["XDG_STATE_HOME"], self.state_home)
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.lstat(self.homes).st_mode), 0o700)
        self.assertEqual(_read(self.runs), runs_before)
        self.assertEqual(leftovers_agent(self.roots, LABEL), (self.plist,))
        once_plist = blob
        again = self._install()
        self.assertEqual((again.action, again.changed), ("install", False))
        self.assertEqual(_read(self.plist), once_plist)
        self.assertFalse(again.stores[0][2])
        self.assertEqual(_read(self.runs), runs_before)

    def test_a_different_plist_is_refused_and_left_in_place(self) -> None:
        self._install()
        other = b"not our plist\n"
        with open(self.plist, "wb") as handle:
            handle.write(other)
        os.chmod(self.plist, 0o644)
        before = _read(self.plist)
        with self.assertRaises(InstallRefused) as caught:
            self._install()
        self.assertEqual(caught.exception.kind, "plist_differs")
        self.assertEqual(_read(self.plist), before)

    def test_a_plist_another_user_could_write_is_refused(self) -> None:
        self._install()
        os.chmod(self.plist, 0o666)
        before = _read(self.plist)
        with self.assertRaises(InstallRefused) as caught:
            self._install()
        self.assertEqual(caught.exception.kind, "plist_untrusted")
        self.assertEqual(_read(self.plist), before)
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o666)

    def test_uninstall_of_a_foreign_plist_is_refused_and_the_file_is_untouched(self) -> None:
        agents = os.path.dirname(self.plist)
        os.makedirs(agents, 0o700)
        foreign = b"someone else's plist\n"
        with open(self.plist, "wb") as handle:
            handle.write(foreign)
        os.chmod(self.plist, 0o644)
        inode = os.stat(self.plist).st_ino
        fake = _Launchctl(code=1)
        with self.assertRaises(InstallRefused) as caught:
            uninstall_agent(self.roots, UID, LABEL, launchctl=fake)
        self.assertEqual(caught.exception.kind, "plist_not_ours")
        self.assertEqual(_read(self.plist), foreign)
        self.assertEqual(os.stat(self.plist).st_ino, inode)
        self.assertEqual(fake.calls, [("launchctl", "print", "gui/501/recursiond")])

    def test_a_loaded_agent_is_refused_and_the_plist_stays(self) -> None:
        self._install()
        before = _read(self.plist)
        inode = os.stat(self.plist).st_ino
        fake = _Launchctl(code=0)
        with self.assertRaises(InstallRefused) as caught:
            uninstall_agent(self.roots, UID, LABEL, launchctl=fake)
        self.assertEqual(caught.exception.kind, "agent_loaded")
        self.assertEqual(_read(self.plist), before)
        self.assertEqual(os.stat(self.plist).st_ino, inode)
        self.assertEqual(fake.calls, [("launchctl", "print", "gui/501/recursiond")])
        self.assertEqual(leftovers_agent(self.roots), (self.plist,))

    def test_uninstall_without_a_print_answer_refuses_and_leaves_the_file(self) -> None:
        self._install()
        before = _read(self.plist)
        with self.assertRaises(InstallRefused) as caught:
            uninstall_agent(self.roots, UID, LABEL)
        self.assertEqual(caught.exception.kind, "agent_loaded")
        self.assertEqual(_read(self.plist), before)

    def test_a_print_that_raises_refuses_and_leaves_the_file(self) -> None:
        self._install()
        before = _read(self.plist)

        def boom(_argv: tuple[str, ...]) -> int:
            raise OSError("launchctl is not here")

        with self.assertRaises(InstallRefused) as caught:
            uninstall_agent(self.roots, UID, LABEL, launchctl=boom)
        self.assertEqual(caught.exception.kind, "agent_loaded")
        self.assertEqual(_read(self.plist), before)

    def test_uninstall_removes_only_our_plist_and_keeps_the_run_registry(self) -> None:
        self._install()
        os.mkdir(os.path.join(self.homes, "claude_code"), 0o700)
        runs = _read(self.runs)
        fake = _Launchctl(code=1)
        plan = uninstall_agent(self.roots, UID, LABEL, launchctl=fake)
        self.assertEqual((plan.action, plan.changed), ("uninstall", True))
        self.assertEqual(plan.commands, (("launchctl", "bootout", "gui/501/recursiond"),))
        self.assertFalse(os.path.lexists(self.plist))
        self.assertEqual(fake.calls, [("launchctl", "print", "gui/501/recursiond")])
        self.assertEqual(_read(self.runs), runs)
        self.assertEqual(os.listdir(self.homes), ["claude_code"])
        self.assertEqual(leftovers_agent(self.roots, LABEL), ())
        # A second caller, after the plist is gone and the agent is not loaded, changes nothing.
        again = uninstall_agent(self.roots, UID, LABEL, launchctl=_Launchctl(code=1))
        self.assertEqual((again.action, again.changed), ("uninstall", False))
        self.assertEqual(_read(self.runs), runs)

    def test_upgrade_rewrites_the_program_and_keeps_the_registry(self) -> None:
        self._install()
        runs = _read(self.runs)
        plan = upgrade_agent(self.roots, EXEC_NEXT, UID, LABEL)
        self.assertEqual((plan.action, plan.changed), ("upgrade", True))
        self.assertEqual(
            plan.commands,
            (
                ("launchctl", "bootout", "gui/501/recursiond"),
                ("launchctl", "bootstrap", "gui/501", self.plist),
            ),
        )
        doc = plistlib.loads(_read(self.plist))
        self.assertEqual(doc["ProgramArguments"][0], PY_NEXT)
        self.assertEqual(doc["EnvironmentVariables"]["XDG_STATE_HOME"], self.state_home)
        self.assertEqual(_read(self.runs), runs)
        again = upgrade_agent(self.roots, EXEC_NEXT, UID, LABEL)
        self.assertFalse(again.changed)
        self.assertEqual(_read(self.runs), runs)

    def test_upgrade_rewrites_a_plist_others_could_write(self) -> None:
        self._install()
        os.chmod(self.plist, 0o666)
        plan = upgrade_agent(self.roots, EXEC, UID, LABEL)
        self.assertTrue(plan.changed)
        self.assertEqual(stat.S_IMODE(os.stat(self.plist).st_mode), 0o644)
        self.assertEqual(_read(self.plist), render_plist(EXEC, LABEL, state_home=self.state_home))

    def test_upgrade_of_a_missing_plist_is_refused(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            upgrade_agent(self.roots, EXEC, UID, LABEL)
        self.assertEqual(caught.exception.kind, "plist_missing")
        self.assertFalse(os.path.lexists(self.plist))
        self.assertFalse(os.path.isdir(os.path.join(self.state, "plugin-security")))

    def test_upgrade_refuses_a_plist_whose_state_is_elsewhere_and_leaves_it(self) -> None:
        agents = os.path.dirname(self.plist)
        os.makedirs(agents, 0o700)
        blob = plistlib.dumps(
            {
                "RecursionInstaller": MARKER,
                "Label": LABEL,
                "EnvironmentVariables": {"XDG_STATE_HOME": "/somewhere/else", "PATH": "/usr/bin:/bin"},
            }
        )
        with open(self.plist, "wb") as handle:
            handle.write(blob)
        os.chmod(self.plist, 0o644)
        with self.assertRaises(InstallRefused) as caught:
            upgrade_agent(self.roots, EXEC, UID, LABEL)
        self.assertEqual(caught.exception.kind, "plist_state_elsewhere")
        self.assertEqual(_read(self.plist), blob)
        self.assertFalse(os.path.isdir(os.path.join(self.state, "plugin-security")))

    def test_a_bad_label_writes_nothing(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            install_agent(self.roots, EXEC, UID, "../escape")
        self.assertEqual(caught.exception.kind, "label_invalid")
        self.assertFalse(os.path.isdir(os.path.join(self.home, "Library")))
        self.assertEqual(_read(self.runs), b"rows-v1")


if __name__ == "__main__":
    unittest.main()
