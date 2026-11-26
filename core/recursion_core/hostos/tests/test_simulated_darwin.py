"""Run the daemon start path the way a Mac does, on a POSIX host.

Each simulation is a child interpreter. Before the daemon is imported it sets
``sys.platform`` to ``darwin`` and deletes ``os.pidfd_open``. ``loginctl`` is not
on the child's ``PATH``. Path syntax, file modes and process handling stay
those of this host. The child is not a Mac, and it does not run ``launchctl``
or read ``XPC_SERVICE_NAME`` from a real launchd.

On today's daemon, ``main`` stops at the operator linger read because that read
runs ``loginctl``. After the start-path hooks, a fake LaunchAgent gets past
that read and ``main`` stops at the engine launcher's typed refusal. The
launcher gate itself is not part of this change.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_FLAG = "--simulate-darwin"
_MARK = "SIMULATED_DARWIN_RESULT "
_SCRATCH = Path("/var/tmp/grok-hostos-dispatch")
_LAUNCH_AGENT = "run as the Recursion LaunchAgent"
_RUNTIME = "runtime directory must be a private 0700 path of at most 60 bytes"
_LAUNCHER = ("platform_unsupported: the engine launcher needs Linux "
             "(FIFO stdio, FIONREAD, pidfd, systemd user units)")
_LABEL = "com.recursion.agent"
HERE = Path(__file__).resolve().parent
CORE = HERE.parents[2]


def _apply() -> None:
    # Cache the host build data first. sysconfig picks its data file from sys.platform,
    # and a darwin name on this host has no such file.
    sysconfig.get_config_vars()
    sys.platform = "darwin"
    if hasattr(os, "pidfd_open"):
        delattr(os, "pidfd_open")


def _probe() -> dict:
    return {"sys.platform": sys.platform, "pidfd_open": hasattr(os, "pidfd_open"),
            "loginctl": shutil.which("loginctl")}


def _run_main() -> dict:
    import recursion_core.daemon.__main__ as entry

    # 3.12 cannot pass the free-threaded gate, so the harness steps past it there.
    # A free-threaded 3.14 child keeps the real gate (it has already passed once).
    try:
        entry.require_free_threaded()
    except RuntimeError:
        entry.require_free_threaded = lambda: None
    exc_type = message = None
    try:
        entry.main()
    except BaseException as exc:  # noqa: BLE001  the start refusal is the result
        exc_type = type(exc).__name__
        message = str(exc)
    return {"exc_type": exc_type, "message": message, "sys.platform": sys.platform,
            "pidfd_open": hasattr(os, "pidfd_open"), "loginctl": shutil.which("loginctl")}


def _child_main(argv: list[str]) -> int:
    _apply()
    out = _probe() if argv[0] == "--probe" else _run_main()
    sys.stdout.write("\n" + _MARK + json.dumps(out) + "\n")
    return 0


def _launchctl(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        '[ "$1" = print ] || exit 2\n'
        "printf '%s = {\\n\\tstate = running\\n}\\n' \"$2\"\n"
    )
    path.chmod(0o700)


def simulate(mode: str, *, launch_agent: bool) -> dict:
    """Run one child under the simulation. ``mode`` is ``--probe`` or ``--main``."""

    _SCRATCH.mkdir(mode=0o700, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="d", dir=str(_SCRATCH)))
    os.chmod(scratch, 0o700)
    if len(os.fsencode(scratch)) > 60:
        shutil.rmtree(scratch, ignore_errors=True)
        raise AssertionError(f"scratch is longer than 60 bytes: {scratch}")
    bins = scratch / "bin"
    bins.mkdir(mode=0o700)
    if launch_agent:
        _launchctl(bins / "launchctl")
    home = scratch / "home"
    state = scratch / "state"
    config = scratch / "config"
    for directory in (home, state, config):
        directory.mkdir(mode=0o700)
    env = {
        "PATH": str(bins),
        "HOME": str(home),
        "USER": "operator",
        "LOGNAME": "operator",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(CORE),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(scratch),
        "XDG_RUNTIME_DIR": str(scratch),
        "XDG_STATE_HOME": str(state),
        "XDG_CONFIG_HOME": str(config),
    }
    if launch_agent:
        env["XPC_SERVICE_NAME"] = _LABEL
    flags = ["-X", "gil=0"] if sysconfig.get_config_var("Py_GIL_DISABLED") else ["-X", "utf8"]
    try:
        proc = subprocess.run(
            [sys.executable, *flags, "-B", str(Path(__file__).resolve()), _FLAG, mode],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(CORE.parent), check=False,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    line = next((item for item in reversed(proc.stdout.splitlines()) if item.startswith(_MARK)), None)
    if line is None:
        raise AssertionError(
            f"simulated-darwin child gave no result (exit {proc.returncode}):\n{proc.stderr[-4000:]}")
    return json.loads(line[len(_MARK):])


@unittest.skipIf(os.name == "nt", "simulation for POSIX hosts; a Windows host runs its own refusal tests")
class SimulatedDarwinTests(unittest.TestCase):
    maxDiff = None

    def test_the_simulation_is_in_force(self) -> None:
        self.assertEqual(
            {"sys.platform": "darwin", "pidfd_open": False, "loginctl": None},
            simulate("--probe", launch_agent=False),
        )

    def test_main_passes_the_start_checks_and_stops_at_the_launcher_refusal(self) -> None:
        # Today's main stops here with the loginctl linger refusal. With a fake LaunchAgent
        # the start checks pass and the unchanged launcher gate refuses.
        result = simulate("--main", launch_agent=True)
        self.assertEqual("darwin", result["sys.platform"])
        self.assertFalse(result["pidfd_open"])
        self.assertIsNone(result["loginctl"])
        self.assertEqual("PlatformUnsupported", result["exc_type"], result)
        self.assertEqual(_LAUNCHER, result["message"])

    def test_main_without_a_launch_agent_names_the_refusal(self) -> None:
        result = simulate("--main", launch_agent=False)
        self.assertEqual("RuntimeError", result["exc_type"], result)
        self.assertEqual(_LAUNCH_AGENT, result["message"])


def _short_dir(prefix: str) -> Path:
    _SCRATCH.mkdir(mode=0o700, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(_SCRATCH)))
    os.chmod(path, 0o700)
    if len(os.fsencode(path)) > 60:
        shutil.rmtree(path, ignore_errors=True)
        raise AssertionError(f"scratch is longer than 60 bytes: {path}")
    return path


@unittest.skipIf(os.name == "nt" or not hasattr(os, "getuid"),
                 "these hooks are exercised from a POSIX host, including the win32 refusal text")
class StartPathHookTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._owned: list[Path] = []

    def tearDown(self) -> None:
        for path in self._owned:
            shutil.rmtree(path, ignore_errors=True)

    def _dir(self, prefix: str) -> str:
        path = _short_dir(prefix)
        self._owned.append(path)
        return str(path)

    def test_this_host_selects_linux_and_keeps_today_s_calls(self) -> None:
        from recursion_core.hostos import LinuxHooks, select

        hooks = select()
        self.assertIsInstance(hooks, LinuxHooks)
        self.assertIsInstance(select("linux"), LinuxHooks)
        self.assertIsInstance(select("linux2"), LinuxHooks)
        # A platform the start path does not name keeps today's calls.
        self.assertIsInstance(select("freebsd"), LinuxHooks)
        missing = "/tmp/" + ("m" * 55)
        self.assertEqual(len(missing), 60)
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(hooks.runtime_dir({"XDG_RUNTIME_DIR": missing}), missing)
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(hooks.runtime_dir({}), f"/run/user/{os.getuid()}")
        self.assertEqual(hooks.runtime_dir({"XDG_RUNTIME_DIR": ""}), f"/run/user/{os.getuid()}")
        seen = {}

        def call(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout="yes\n")

        hooks.check_operator("operator", call, environ={"XPC_SERVICE_NAME": "ignored"})
        self.assertEqual(seen["argv"], ["loginctl", "show-user", "operator", "--property=Linger", "--value"])
        self.assertNotIn("enable-linger", seen["argv"])

    def test_linger_keeps_the_loginctl_read_unless_the_platform_is_darwin(self) -> None:
        import recursion_core.daemon as daemon

        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                seen = {}

                def call(argv, **kwargs):
                    seen["argv"] = list(argv)
                    return subprocess.CompletedProcess(argv, 0, stdout="yes\n")

                with mock.patch.object(daemon.sys, "platform", platform):
                    daemon.check_operator_linger("operator", call)
                self.assertEqual(
                    seen["argv"],
                    ["loginctl", "show-user", "operator", "--property=Linger", "--value"],
                )
                self.assertNotIn("enable-linger", seen["argv"])

    def test_client_socket_directory_stays_on_today_s_linux_path(self) -> None:
        from recursion_core.control.client import socket_directory

        uid = os.getuid()
        self.assertEqual(socket_directory({}), f"/run/user/{uid}/recursion")
        self.assertEqual(socket_directory({"XDG_RUNTIME_DIR": ""}), f"/run/user/{uid}/recursion")
        self.assertEqual(socket_directory({"XDG_RUNTIME_DIR": "/run/user/4242"}),
                         "/run/user/4242/recursion")

    def test_darwin_launch_agent_read_accepts_only_a_running_service(self) -> None:
        from recursion_core.hostos import DarwinHooks, select

        self.assertIsInstance(select("darwin"), DarwinHooks)
        uid = os.getuid()
        service = f"gui/{uid}/{_LABEL}"
        seen = {}

        def call(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[2]} = {{\n\tstate = running\n}}\n")

        select("darwin").check_operator("operator", call, environ={"XPC_SERVICE_NAME": _LABEL})
        self.assertEqual(seen["argv"], ["launchctl", "print", service])
        self.assertEqual(seen["kwargs"]["check"], True)
        self.assertNotIn("operator", seen["argv"])

    def test_darwin_refuses_a_missing_or_idle_launch_agent(self) -> None:
        from recursion_core.hostos import select

        def boom(*_args, **_kwargs):
            raise AssertionError("launchctl ran")

        for environ in ({}, {"XPC_SERVICE_NAME": ""}, {"XPC_SERVICE_NAME": "gui/1/com.recursion.agent"},
                        {"XPC_SERVICE_NAME": "../com.recursion.agent"}, {"XPC_SERVICE_NAME": "com.recursion agent"}):
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(RuntimeError, f"^{_LAUNCH_AGENT}$"):
                    select("darwin").check_operator("operator", boom, environ=environ)

        def failed(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv, stderr="Could not find service")

        def idle(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[2]} = {{\n\tstate = waiting\n}}\n")

        for call in (failed, idle, lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("launchctl"))):
            with self.subTest(call=getattr(call, "__name__", "missing")):
                with self.assertRaisesRegex(RuntimeError, f"^{_LAUNCH_AGENT}$"):
                    select("darwin").check_operator(
                        "operator", call, environ={"XPC_SERVICE_NAME": _LABEL})

    def test_darwin_runtime_directory_is_private_and_at_most_60_bytes(self) -> None:
        from recursion_core.hostos import MAX_RUNTIME_DIR_BYTES, select

        self.assertEqual(60, MAX_RUNTIME_DIR_BYTES)
        path = self._dir("rt")
        os.chmod(path, 0o755)
        got = select("darwin").runtime_dir({"XDG_RUNTIME_DIR": path})
        self.assertEqual(path, got)
        self.assertLessEqual(len(os.fsencode(got)), 60)
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(got).st_mode))
        self.assertEqual(os.geteuid(), os.lstat(got).st_uid)
        self.assertFalse(os.path.islink(got))

    def test_darwin_runtime_directory_refuses_a_long_relative_or_linked_path(self) -> None:
        from recursion_core.hostos import select

        long_path = "/tmp/" + ("z" * 56)
        self.assertEqual(61, len(os.fsencode(long_path)))
        self.assertFalse(os.path.exists(long_path))
        with self.assertRaisesRegex(RuntimeError, f"^{_RUNTIME}$"):
            select("darwin").runtime_dir({"XDG_RUNTIME_DIR": long_path})
        self.assertFalse(os.path.exists(long_path))

        with self.assertRaisesRegex(RuntimeError, f"^{_RUNTIME}$"):
            select("darwin").runtime_dir({"XDG_RUNTIME_DIR": "relative/runtime"})
        with self.assertRaisesRegex(RuntimeError, f"^{_RUNTIME}$"):
            select("darwin").runtime_dir({"XDG_RUNTIME_DIR": "/tmp/../tmp"})

        parent = Path(self._dir("ln"))
        target = parent / "target"
        target.mkdir(mode=0o700)
        link = parent / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, f"^{_RUNTIME}$"):
            select("darwin").runtime_dir({"XDG_RUNTIME_DIR": str(link)})
        self.assertTrue(link.is_symlink())

        occupied = parent / "file"
        occupied.write_text("not a directory")
        with self.assertRaisesRegex(RuntimeError, f"^{_RUNTIME}$"):
            select("darwin").runtime_dir({"XDG_RUNTIME_DIR": str(occupied)})
        self.assertTrue(occupied.is_file())

    def test_darwin_boundary_of_60_bytes_is_accepted(self) -> None:
        from recursion_core.hostos import select

        path = "/tmp/" + f"{os.getpid():07d}" + ("b" * 48)
        self.assertEqual(60, len(os.fsencode(path)))
        os.mkdir(path, 0o700)
        self.addCleanup(os.rmdir, path)
        self.assertEqual(path, select("darwin").runtime_dir({"XDG_RUNTIME_DIR": path}))
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(path).st_mode))

    def test_darwin_default_runtime_is_not_run_user(self) -> None:
        from recursion_core.hostos import select

        path = f"/tmp/recursion-{os.getuid()}"
        existed = os.path.lexists(path)
        try:
            got = select("darwin").runtime_dir({})
            self.assertEqual(path, got)
            self.assertLessEqual(len(os.fsencode(got)), 60)
            self.assertFalse(got.startswith("/run/user/"))
            self.assertEqual(0o700, stat.S_IMODE(os.lstat(got).st_mode))
            self.assertEqual(os.geteuid(), os.lstat(got).st_uid)
        finally:
            if not existed and os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)

    def test_client_socket_directory_follows_the_darwin_runtime(self) -> None:
        import recursion_core.hostos as hostos
        from recursion_core.control.client import socket_directory

        path = self._dir("sk")
        with mock.patch.object(hostos.sys, "platform", "darwin"):
            got = socket_directory({"XDG_RUNTIME_DIR": path})
        self.assertEqual(str(Path(path) / "recursion"), got)
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(path).st_mode))

    def test_win32_hooks_raise_the_daemon_refusal_text(self) -> None:
        from recursion_core.daemon import PlatformUnsupported, require_posix
        from recursion_core.hostos import Win32Hooks, select

        # The same text require_posix raises, including the space where its
        # two string literals meet.
        tree = ast.parse(inspect.getsource(require_posix))
        raised = [node for node in ast.walk(tree) if isinstance(node, ast.Raise)]
        self.assertEqual(1, len(raised))
        expected = ast.literal_eval(raised[0].exc.args[0])
        hooks = select("win32")
        self.assertIsInstance(hooks, Win32Hooks)
        for call in (lambda: hooks.check_operator("op"), lambda: hooks.runtime_dir()):
            with self.subTest(call=call):
                with self.assertRaises(PlatformUnsupported) as caught:
                    call()
                self.assertEqual(expected, str(caught.exception))


if __name__ == "__main__":
    if sys.argv[1:2] == [_FLAG]:
        raise SystemExit(_child_main(sys.argv[2:]))
    unittest.main(verbosity=2)
