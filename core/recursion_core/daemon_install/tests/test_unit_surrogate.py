"""A ``\\u`` surrogate in an Environment word. No systemctl; files under a temporary home.

systemd 255's ``cunescape_one`` decodes ``\\u`` without ``unichar_is_valid``, then the assignment
check drops that one word and keeps reading the line. Stopping the line here dropped a later
``XDG_STATE_HOME`` and ``upgrade`` rewrote the unit.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest

from recursion_core.daemon_install import (
    UNIT_NAME,
    InstallRefused,
    Roots,
    daemon_exec_args,
    install,
    render_unit,
    upgrade,
)
from recursion_core.daemon_install.unit import state_elsewhere

PY = "/opt/recursion/venv/bin/python3.14t"
POSIX_PATHS = os.path.isabs(PY)
EXEC = daemon_exec_args(PY) if POSIX_PATHS else ()

# The watch's second example, word for word.
_LINE = "A=1 B=\\uD800 XDG_STATE_HOME=/srv/elsewhere"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _tree(root: str) -> dict[str, tuple]:
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                found[path] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[path] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                with open(path, "rb") as handle:
                    found[path] = ("file", stat.S_IMODE(info.st_mode), handle.read())
    return found


@unittest.skipUnless(os.name == "posix", "the installer is Linux-only")
@unittest.skipUnless(POSIX_PATHS, "this host does not take the unit's interpreter path as absolute")
class SurrogateWordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        self.unit_dir = os.path.join(self.home, ".config", "systemd", "user")
        self.unit = os.path.join(self.unit_dir, UNIT_NAME)
        self.state = os.path.join(self.home, ".local", "state", "recursion")
        os.makedirs(self.state, 0o700)
        with open(os.path.join(self.state, "runs.sqlite3"), "wb") as handle:
            handle.write(b"rows-v1")

    def test_a_u_surrogate_drops_that_word_and_a_later_state_folder_stays(self) -> None:
        # Environment=A=1 B=\uD800 XDG_STATE_HOME=/srv/elsewhere
        # The surrogate word is not an assignment. The elsewhere word is, so upgrade must leave the unit.
        os.makedirs(self.unit_dir, 0o700)
        text = f"[Service]\nEnvironment={_LINE}\nExecStart=/old\n"
        with open(self.unit, "w", encoding="utf-8") as handle:
            handle.write(text)
        why = state_elsewhere(text.encode(), self.home)
        self.assertIsNotNone(why)
        self.assertIn("sets XDG_STATE_HOME=/srv/elsewhere", why)
        self.assertNotIn("cannot be read", why)
        again = state_elsewhere(text.encode(), self.home)
        self.assertEqual(again, why)
        before = _tree(self.home)
        with self.assertRaises(InstallRefused) as caught:
            upgrade(self.roots, EXEC)
        self.assertEqual(caught.exception.kind, "unit_state_elsewhere")
        self.assertIn("sets XDG_STATE_HOME=/srv/elsewhere", caught.exception.detail)
        self.assertEqual(_tree(self.home), before)
        self.assertEqual(_read(self.unit), text)
        self.assertNotEqual(_read(self.unit), render_unit(EXEC))
        with self.assertRaises(InstallRefused):
            upgrade(self.roots, EXEC)


@unittest.skipIf(hasattr(os, "geteuid"), "this host has os.geteuid; the refusal is what a platform without it answers")
class NoGeteuidRefusalTests(unittest.TestCase):
    def test_install_and_upgrade_refuse_in_one_line_that_names_the_platform(self) -> None:
        # The owner check cannot be made. install and upgrade answer a refusal and leave the state folder.
        tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(tmp.cleanup)
        home = os.path.join(tmp.name, "home")
        os.mkdir(home, 0o700)
        state = os.path.join(home, ".local", "state", "recursion")
        os.makedirs(state, 0o700)
        with open(os.path.join(state, "runs.sqlite3"), "wb") as handle:
            handle.write(b"rows-v1")
        before = _tree(home)
        roots = Roots(home=home)
        expected = f"the per-user installer needs os.geteuid, which {sys.platform} does not provide"
        for name, call in (("install", install), ("upgrade", upgrade)):
            with self.subTest(name=name):
                with self.assertRaises(InstallRefused) as caught:
                    call(roots, EXEC)
                self.assertEqual(caught.exception.kind, "platform_unsupported")
                self.assertEqual(caught.exception.detail, expected)
                self.assertNotIn("\n", caught.exception.detail)
                self.assertNotIn("\n", str(caught.exception))
                self.assertIn(sys.platform, caught.exception.detail)
        self.assertEqual(_tree(home), before)


class InstallWithoutGeteuidTests(unittest.TestCase):
    def test_install_refuses_platform_unsupported_and_creates_no_breaker_store_or_custody_root(self) -> None:
        # NoGeteuidRefusalTests runs only where os.geteuid is already absent. This host has it, so the
        # attribute is taken away for the call and put back. install must refuse before it makes the
        # breaker store or the route homes' custody root.
        tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(tmp.cleanup)
        home = os.path.join(tmp.name, "home")
        os.mkdir(home, 0o700)
        state = os.path.join(home, ".local", "state", "recursion")
        breaker = os.path.join(state, "plugin-security", "breaker.sqlite3")
        custody = os.path.join(state, "homes")
        before = _tree(home)
        roots = Roots(home=home)
        saved = getattr(os, "geteuid", None)
        try:
            if saved is not None:
                del os.geteuid
            with self.assertRaises(InstallRefused) as caught:
                install(roots, EXEC)
        finally:
            if saved is not None:
                os.geteuid = saved
        self.assertEqual(caught.exception.kind, "platform_unsupported")
        self.assertFalse(os.path.lexists(breaker))
        self.assertFalse(os.path.lexists(custody))
        self.assertEqual(_tree(home), before)


if __name__ == "__main__":
    unittest.main()
