"""Install, list, remove and the registry check refuse typed where POSIX names are missing.

Windows CPython has no ``fcntl``, no ``os.O_NOFOLLOW`` and no ``os.O_DIRECTORY``. There the
install module still imports, the plugin commands refuse ``platform_unsupported`` with the
command's refusal exit and no traceback, and ``check_registry`` reports ``root_unavailable``,
each before any file is opened or made: the form ``daemon/plugins.py`` already gives. No
Windows read path is built here.

Every case runs on every host and none is under a platform skip. On a host that has the names,
a case takes them away for the length of the call; the import and the command line run in a
child interpreter that hides them before the first ``recursion_core`` import.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parents[3]
POSIX_NAMES = ("O_NOFOLLOW", "O_DIRECTORY")
REFUSED = "refused: platform_unsupported\n"

# The child hides what Windows CPython lacks, then imports; with arguments it runs the command line.
_CHILD = """
import os, sys
sys.modules["fcntl"] = None
for name in ("O_NOFOLLOW", "O_DIRECTORY"):
    os.__dict__.pop(name, None)
from recursion_core.plugin_security import install
from recursion_core.plugin_security._entrypoints import main
if sys.argv[1:]:
    sys.exit(main(sys.argv[1:]))
print("imported", install.fcntl is None)
"""


@contextlib.contextmanager
def _platform_without(*names):
    """``os`` and the install module as a platform without ``names`` sees them, for the call."""
    saved = {name: getattr(os, name) for name in POSIX_NAMES if name in names and hasattr(os, name)}
    with contextlib.ExitStack() as stack:
        if "fcntl" in names:
            from recursion_core.plugin_security import install

            stack.enter_context(mock.patch.object(install, "fcntl", None))
        for name in saved:
            delattr(os, name)
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(os, name, value)


@contextlib.contextmanager
def _opens_recorded():
    calls = []
    real = os.open

    def record(path, *args, **kwargs):
        calls.append(path)
        return real(path, *args, **kwargs)

    with mock.patch.object(os, "open", record):
        yield calls


# Past the three names the guard tested at 6dc16a70, the commands also call these.
LATER_NAMES = ("O_CLOEXEC", "geteuid", "fchmod", "dir_fd", "listdir")
# Linux values, set only where a host lacks the name, so that the three first names are present.
_LINUX_FLAGS = {"O_NOFOLLOW": 0o400000, "O_DIRECTORY": 0o200000}


class _Fcntl:
    LOCK_EX, LOCK_NB = 2, 4

    @staticmethod
    def flock(_fd, _flags):
        return None


def _without_dir_fd(real):
    def call(*args, dir_fd=None, **kwargs):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable on this platform")
        return real(*args, **kwargs)

    return call


@contextlib.contextmanager
def _posix_without(name):
    """A POSIX platform that has fcntl, O_NOFOLLOW and O_DIRECTORY but lacks ``name``, for the call."""
    from recursion_core.plugin_security import install

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(os, "name", "posix"))
        if install.fcntl is None:
            stack.enter_context(mock.patch.object(install, "fcntl", _Fcntl()))
        for flag, value in _LINUX_FLAGS.items():
            if not hasattr(os, flag):
                setattr(os, flag, value)
                stack.callback(delattr, os, flag)
        if name == "dir_fd":
            kept = {item for item in os.supports_dir_fd if item.__name__ not in ("open", "stat")}
            stack.enter_context(mock.patch.object(os, "supports_dir_fd", kept))
            stack.enter_context(mock.patch.object(os, "open", _without_dir_fd(os.open)))
            stack.enter_context(mock.patch.object(os, "stat", _without_dir_fd(os.stat)))
        elif name == "listdir":
            real = os.listdir

            def listdir(path="."):
                if isinstance(path, int):
                    raise TypeError("listdir: path should be string, bytes, os.PathLike or None, not int")
                return real(path)

            kept = {item for item in os.supports_fd if item.__name__ != "listdir"}
            stack.enter_context(mock.patch.object(os, "supports_fd", kept))
            stack.enter_context(mock.patch.object(os, "listdir", listdir))
        elif hasattr(os, name):
            value = getattr(os, name)
            delattr(os, name)
            stack.callback(setattr, os, name, value)
        yield


class PlatformRefusalTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="rc-plugin-platform-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.registry = self.tmp / "registry-root"
        (self.registry / "registry").mkdir(parents=True)
        self.config = self.tmp / "config" / "recursion"
        self.state = self.tmp / "state"

    def _child(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(part for part in (str(CORE), env.get("PYTHONPATH", "")) if part)
        scratch = self.tmp / "child-tmp"
        home = self.tmp / "child-home"
        scratch.mkdir(exist_ok=True)
        home.mkdir(exist_ok=True)
        for key in ("TMPDIR", "TEMP", "TMP"):
            env[key] = str(scratch)
        for key in ("HOME", "USERPROFILE"):
            env[key] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / "config")
        env["XDG_STATE_HOME"] = str(home / "state")
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", _CHILD, *args], capture_output=True,
                              text=True, timeout=120, env=env, cwd=str(self.tmp), check=False)

    def _commands(self):
        return {
            "install": ["plugin", "install", "--registry", str(self.registry), "--plugin", "fixture.unsigned",
                        "--config", str(self.config), "--state", str(self.state)],
            "list": ["plugin", "list", "--config", str(self.config), "--state", str(self.state)],
            "remove": ["plugin", "remove", "--plugin", "fixture.unsigned", "--config", str(self.config),
                       "--state", str(self.state)],
        }

    def test_the_install_module_imports_without_fcntl_or_the_open_flags(self):
        proc = self._child()
        self.assertEqual((0, "", "imported True\n"), (proc.returncode, proc.stderr, proc.stdout))

    def test_the_install_command_refuses_platform_unsupported_with_its_refusal_exit_and_no_traceback(self):
        for command, argv in self._commands().items():
            with self.subTest(command=command):
                proc = self._child(*argv)
                self.assertEqual((1, REFUSED, ""), (proc.returncode, proc.stderr, proc.stdout))
                self.assertFalse(self.config.exists())
                self.assertFalse(self.state.exists())

    def test_install_list_and_remove_refuse_before_any_open_when_one_name_is_missing(self):
        from recursion_core.plugin_security._entrypoints import main

        for missing in (("fcntl", *POSIX_NAMES), ("fcntl",), ("O_NOFOLLOW",), ("O_DIRECTORY",)):
            for command, argv in self._commands().items():
                with self.subTest(missing=missing, command=command):
                    out, err = io.StringIO(), io.StringIO()
                    with _platform_without(*missing), _opens_recorded() as opens:
                        status = main(argv, out, err)
                    self.assertEqual((1, REFUSED, ""), (status, err.getvalue(), out.getvalue()))
                    self.assertEqual([], opens)
                    self.assertFalse(self.config.exists())
                    self.assertFalse(self.state.exists())

    def test_install_refusal_is_typed_with_its_code(self):
        from recursion_core.plugin_security.install import InstallRefused, install_plugin

        with _platform_without("fcntl", *POSIX_NAMES), _opens_recorded() as opens:
            with self.assertRaises(InstallRefused) as caught:
                install_plugin(self.config, self.registry, "fixture.unsigned", state=self.state)
        self.assertEqual(("platform_unsupported", ["platform_unsupported"]),
                         (caught.exception.code, caught.exception.codes))
        self.assertEqual([], opens)

    def test_check_registry_refuses_platform_unsupported_before_any_open(self):
        from recursion_core.plugin_security.registry import check_registry

        for missing in (POSIX_NAMES, ("O_NOFOLLOW",), ("O_DIRECTORY",)):
            with self.subTest(missing=missing):
                with _platform_without(*missing), _opens_recorded() as opens:
                    report = check_registry(self.registry)
                self.assertEqual(["root_unavailable"], report.codes())
                self.assertEqual(["."], [item.path for item in report.errors])
                self.assertEqual([], report.checked)
                self.assertEqual([], opens)

    def _valid_registry(self):
        from recursion_core.plugin_security.tests.test_plugin_cli_install import ProfileCase

        ProfileCase.write_registry(self)

    def _clear_profile(self):
        for path in (self.tmp / "config", self.state):
            if path.exists():
                shutil.rmtree(path)

    def test_every_command_refuses_before_any_open_when_a_later_name_is_missing(self):
        from recursion_core.plugin_security._entrypoints import main

        self._valid_registry()
        for missing in LATER_NAMES:
            for command, argv in self._commands().items():
                with self.subTest(missing=missing, command=command):
                    self._clear_profile()
                    out, err = io.StringIO(), io.StringIO()
                    with _posix_without(missing), _opens_recorded() as opens:
                        status = main(argv, out, err)
                    self.assertEqual((1, REFUSED, ""), (status, err.getvalue(), out.getvalue()))
                    self.assertEqual([], opens)
                    self.assertFalse(self.config.exists())
                    self.assertFalse(self.state.exists())

    def test_check_registry_reports_root_unavailable_before_any_open_when_a_read_it_needs_is_missing(self):
        from recursion_core.plugin_security.registry import check_registry

        self._valid_registry()
        for missing in ("O_CLOEXEC", "dir_fd", "listdir"):
            with self.subTest(missing=missing):
                with _posix_without(missing), _opens_recorded() as opens:
                    report = check_registry(self.registry)
                self.assertEqual(["root_unavailable"], report.codes())
                self.assertEqual(["."], [item.path for item in report.errors])
                self.assertEqual([], report.checked)
                self.assertEqual([], opens)

    def test_the_registry_rule_set_holds_only_the_marketplace_codes_for_this_case(self):
        from recursion_core.plugin_security import registry

        self.assertIn("root_unavailable", registry._RULES)
        self.assertNotIn("platform_unsupported", registry._RULES)


if __name__ == "__main__":
    unittest.main()
