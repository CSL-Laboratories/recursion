"""``setup.route_home.check``: the route home the launcher hands an engine as HOME (lane INSTALL, for STOP).

Brief, "Done when": route_home.check refuses a path outside the root, a link and the operator's HOME
(F6's proof). Every case uses a scratch tree; nothing outside it is touched.
"""

from __future__ import annotations

import builtins
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.setup import route_home
from recursion_core.setup.route_home import RouteHomeRefused, check

_real_lstat = os.lstat


def _owned_by(target: str, uid: int | None = None):
    """Patch ``os.lstat`` so that ``target`` alone reports another owner (``uid``, or ours plus one)."""
    def fake(path, *args, **kwargs):
        info = _real_lstat(path, *args, **kwargs)
        if os.fspath(path) == target:
            fields = list(info[:10])
            fields[4] = info.st_uid + 1 if uid is None else uid
            return os.stat_result(fields)
        return info
    return mock.patch("os.lstat", side_effect=fake)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "Linux file owners and modes, and a non-root uid")
class RouteHomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="route-home-")
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.realpath(self._tmp.name)  # the names the walk builds from "/"
        self.state = os.path.join(self.base, "state")
        self.root = os.path.join(self.state, "homes")
        os.makedirs(self.root, 0o700)
        os.chmod(self.state, 0o700)
        self.path = os.path.join(self.root, "claude_code")
        self.addCleanup(self._reopen)
        operator = os.path.join(self.base, "operator")  # stands in for the operator's home
        os.mkdir(operator, 0o700)
        self.env = mock.patch.dict(os.environ, {"HOME": operator})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _reopen(self):
        for folder, _, _ in os.walk(self.base):
            if not os.path.islink(folder):
                os.chmod(folder, 0o700)

    def refused(self, code: str, path, root=None) -> RouteHomeRefused:
        with self.assertRaises(RouteHomeRefused) as caught:
            check(path, self.root if root is None else root)
        self.assertEqual(caught.exception.code, code, caught.exception.detail)
        return caught.exception

    def test_a_route_home_not_made_yet_is_accepted_and_nothing_is_made(self):
        before = sorted(os.listdir(self.root))
        with mock.patch("os.open", side_effect=AssertionError("opened")), \
                mock.patch.object(builtins, "open", side_effect=AssertionError("opened")):
            self.assertIsNone(check(self.path, self.root))
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_an_owner_only_route_home_below_the_root_is_accepted(self):
        os.mkdir(self.path, 0o700)
        check(self.path, self.root)
        check(Path(self.path), Path(self.root))
        check(self.path + "/", self.root + "/./")  # a trailing slash and a "." name read as the same path
        deeper = os.path.join(self.path, "nested")
        os.mkdir(deeper, 0o700)
        check(deeper, self.root)

    def test_a_path_outside_the_root_or_the_root_itself_is_refused(self):
        for path in (self.root, self.state, os.path.join(self.state, "other"), self.root + "x/claude_code", "/"):
            with self.subTest(path=path):
                self.refused("route_home_outside_root", path)

    def test_a_path_that_is_not_a_plain_absolute_string_is_refused(self):
        for path, root in (("homes/claude_code", self.root), (self.path, "state/homes"), (b"/abs/x", self.root),
                           (None, self.root), (self.path, 7), (self.path + "\0x", self.root),
                           (self.path, self.root + "\0"), (os.path.join(self.root, "..", "homes", "x"), self.root),
                           (self.path, os.path.join(self.state, "..", "state", "homes"))):
            with self.subTest(path=path, root=root):
                self.refused("route_home_untrusted", path, root)

    def test_a_link_is_refused_at_the_route_home_on_the_way_and_at_the_root(self):
        elsewhere = os.path.join(self.base, "elsewhere")
        os.mkdir(elsewhere, 0o700)
        os.symlink(elsewhere, self.path)
        self.assertIn("is a link", self.refused("route_home_untrusted", self.path).detail)
        os.unlink(self.path)
        os.symlink(elsewhere, os.path.join(self.root, "via"))
        self.refused("route_home_untrusted", os.path.join(self.root, "via", "claude_code"))
        linked_root = os.path.join(self.state, "linked-homes")
        os.symlink(self.root, linked_root)
        self.refused("route_home_untrusted", os.path.join(linked_root, "claude_code"), linked_root)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_something_that_is_not_a_folder_is_refused(self):
        with open(self.path, "wb"):
            pass
        self.assertIn("not a folder", self.refused("route_home_untrusted", self.path).detail)
        os.unlink(self.path)
        root_file = os.path.join(self.state, "root-file")
        with open(root_file, "wb"):
            pass
        self.refused("route_home_untrusted", os.path.join(root_file, "claude_code"), root_file)

    def test_any_group_or_world_bit_on_the_root_or_below_it_is_refused(self):
        os.makedirs(os.path.join(self.path, "nested"), 0o700)
        for folder in (self.root, self.path):
            for mode in (0o750, 0o705, 0o701, 0o710, 0o740):
                with self.subTest(folder=folder, mode=oct(mode)):
                    os.chmod(folder, mode)
                    error = self.refused("route_home_untrusted", os.path.join(self.path, "nested"))
                    self.assertEqual(error.detail, f"{folder} is open to group or world")
                    os.chmod(folder, 0o700)
        check(os.path.join(self.path, "nested"), self.root)

    def test_another_owner_at_the_root_or_below_it_is_refused(self):
        os.mkdir(self.path, 0o700)
        for folder in (self.root, self.path):
            with self.subTest(folder=folder), _owned_by(folder):
                error = self.refused("route_home_untrusted", self.path)
            self.assertEqual(error.detail, f"{folder} is owned by another user")

    def test_a_folder_that_cannot_be_searched_is_refused_untrusted_not_raised(self):
        os.chmod(self.root, 0o600)  # ours and owner-only, but nothing in it can be looked up
        error = self.refused("route_home_untrusted", self.path)
        self.assertIn("PermissionError", error.detail)
        self.refused("route_home_outside_root", "/x", "/")

    def test_a_missing_root_or_a_missing_folder_on_the_way_is_refused(self):
        gone = os.path.join(self.state, "gone")
        self.refused("route_home_missing", os.path.join(gone, "claude_code"), gone)
        self.refused("route_home_missing", os.path.join(self.root, "missing", "claude_code"))

    def test_the_operators_home_is_refused_as_the_route_home_or_as_the_root(self):
        # The home nested below the root, as $HOME, through a link, and as the password database's.
        home = os.path.join(self.root, "operator-home")
        os.mkdir(home, 0o700)
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.refused("route_home_is_operator_home", home)
        link = os.path.join(self.base, "home-link")
        os.symlink(home, link)
        with mock.patch.dict(os.environ, {"HOME": link}):
            self.refused("route_home_is_operator_home", home)
        with mock.patch.dict(os.environ, {"HOME": ""}), \
                mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=home)):
            self.refused("route_home_is_operator_home", home)
        with mock.patch.dict(os.environ, {"HOME": self.root}):
            error = self.refused("route_home_is_operator_home", self.path)
        self.assertEqual(error.detail, "the root is the operator's home")
        # Inside the operator's home is fine: the daemon's state folder is ~/.local/state/recursion.
        with mock.patch.dict(os.environ, {"HOME": self.state}), \
                mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=self.state)):
            check(self.path, self.root)

    def test_a_folder_above_the_root_another_user_could_change_is_refused(self):
        for folder in (self.base, self.state):
            for mode in (0o777, 0o770, 0o722):
                with self.subTest(folder=folder, mode=oct(mode)):
                    os.chmod(folder, mode)
                    error = self.refused("route_home_untrusted", self.path)
                    self.assertEqual(error.detail, f"{folder} is writable by group or world and not sticky")
            os.chmod(folder, 0o1777)  # as /tmp: only an entry's owner can rename it
            check(self.path, self.root)
            os.chmod(folder, 0o700)
            with self.subTest(folder=folder, case="another owner"), _owned_by(folder):
                error = self.refused("route_home_untrusted", self.path)
            self.assertEqual(error.detail, f"{folder} is owned by another user")
            with _owned_by(folder, uid=0):
                check(self.path, self.root)

    def test_a_link_above_the_root_is_followed_and_held_to_the_same_rule(self):
        absolute = os.path.join(self.base, "abs-state")
        os.symlink(self.state, absolute)
        relative = os.path.join(self.base, "rel-state")
        os.symlink("state", relative)
        for link in (absolute, relative):
            with self.subTest(link=link):
                check(os.path.join(link, "homes", "claude_code"), os.path.join(link, "homes"))
        os.chmod(self.state, 0o777)
        self.refused("route_home_untrusted", os.path.join(absolute, "homes", "claude_code"),
                     os.path.join(absolute, "homes"))
        os.chmod(self.state, 0o700)
        with _owned_by(absolute):
            error = self.refused("route_home_untrusted", os.path.join(absolute, "homes", "claude_code"),
                                 os.path.join(absolute, "homes"))
        self.assertEqual(error.detail, f"{absolute} is a link another user owns")
        os.symlink("loop-2", os.path.join(self.base, "loop-1"))
        os.symlink("loop-1", os.path.join(self.base, "loop-2"))
        loop_root = os.path.join(self.base, "loop-1", "homes")
        error = self.refused("route_home_untrusted", os.path.join(loop_root, "claude_code"), loop_root)
        self.assertEqual(error.detail, "more than 40 links above the root")
        a_file = os.path.join(self.base, "a-file")
        with open(a_file, "wb"):
            pass
        file_root = os.path.join(a_file, "homes")
        error = self.refused("route_home_untrusted", os.path.join(file_root, "claude_code"), file_root)
        self.assertEqual(error.detail, f"{a_file} is not a folder")


class FrozenInterfaceTests(unittest.TestCase):
    """STOP builds against these; a change is a request to lane INSTALL."""

    def test_the_frozen_names_signature_and_refusal(self):
        self.assertEqual(route_home.__all__, ["RouteHomeRefused", "check"])
        parameters = inspect.signature(check).parameters
        self.assertEqual(list(parameters), ["path", "root"])
        for parameter in parameters.values():
            self.assertEqual((parameter.kind, parameter.default),
                             (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty))
        refusal = RouteHomeRefused("route_home_untrusted", "detail")
        self.assertEqual((refusal.code, refusal.detail), ("route_home_untrusted", "detail"))
        self.assertIsInstance(refusal, Exception)
        self.assertEqual(RouteHomeRefused("route_home_missing").detail, "")
        for code in ("route_home_untrusted", "route_home_outside_root", "route_home_is_operator_home",
                     "route_home_missing"):
            self.assertIn(code, route_home.__doc__)

    def test_the_module_imports_the_standard_library_only(self):
        import ast
        with open(route_home.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.module, "__future__")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, {"os", "stat", "pwd"})


if __name__ == "__main__":
    unittest.main()
