"""``setup.containment``: the containment setting's file, its reader, its setter and its doctor row (lane INSTALL).

Q14 (Lucas, Nov 15): the route home's place is configurable and its dangers are made clear; Q17 (the
Liaison, Nov 15): the file is read by one function that refuses a file that is not the operator's, is
group or world writable or is reached through a link, answers the default when there is no file, and
never answers the default for an unreadable or untrusted file.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.setup import containment
from recursion_core.setup.containment import ContainmentRefused

INSIDE, OUTSIDE = "inside_operator_home", "outside_operator_home"
_real_lstat, _real_fstat, _real_read = os.lstat, os.fstat, os.read


def _doc(value: str = INSIDE, **extra) -> bytes:
    return json.dumps({"version": 1, "engine_homes": value, **extra}).encode()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "Linux file owners and modes, and a non-root uid")
class ContainmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ct-")
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.realpath(self._tmp.name)
        os.chmod(self.base, 0o700)
        self.config = os.path.join(self.base, "config")
        self.folder = os.path.join(self.config, "recursion")
        self.file = os.path.join(self.folder, "containment.json")
        self.env = {"XDG_CONFIG_HOME": self.config, "HOME": os.path.join(self.base, "home")}
        self.addCleanup(self._reopen)

    def _reopen(self):
        for folder, _, _ in os.walk(self.base):
            if not os.path.islink(folder):
                os.chmod(folder, 0o700)

    def put(self, data: bytes, mode: int = 0o600) -> None:
        os.makedirs(self.folder, 0o700, exist_ok=True)
        with open(self.file, "wb") as handle:
            handle.write(data)
        os.chmod(self.file, mode)

    def refused(self, code: str, call=None) -> ContainmentRefused:
        with self.assertRaises(ContainmentRefused) as caught:
            (call or (lambda: containment.read(self.env)))()
        self.assertEqual(caught.exception.code, code, caught.exception.detail)
        return caught.exception

    # -- reading ---------------------------------------------------------------------------------------

    def test_no_file_is_the_default_said_as_such_and_nothing_is_made(self):
        reading = containment.read(self.env)
        self.assertEqual(reading, containment.Containment(INSIDE, "default", self.file))
        self.assertEqual((reading.allow_inside_operator_home, reading.level), (True, "same_user"))
        self.assertFalse(os.path.lexists(self.config))
        os.mkdir(self.config, 0o700)  # the config folder alone
        self.assertEqual(containment.read(self.env).source, "default")
        os.mkdir(self.folder, 0o700)  # the recursion folder, no file
        self.assertEqual(containment.read(self.env).source, "default")
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_file_is_read_with_its_value_and_level(self):
        for value, allow, level in ((OUTSIDE, False, "engine_user"), (INSIDE, True, "same_user")):
            with self.subTest(value=value):
                self.put(_doc(value))
                reading = containment.read(self.env)
                self.assertEqual(reading, containment.Containment(value, "file", self.file))
                self.assertEqual((reading.allow_inside_operator_home, reading.level), (allow, level))
        for mode in (0o644, 0o640, 0o400):  # readable by others is not a trust problem; writable is
            with self.subTest(mode=oct(mode)):
                os.chmod(self.file, mode)
                self.assertEqual(containment.read(self.env).source, "file")

    def test_the_config_folder_follows_the_daemons_rule(self):
        self.assertEqual(containment.config_folder(self.env), self.folder)
        home = os.path.join(self.base, "home")
        for env in ({"HOME": home}, {"HOME": home, "XDG_CONFIG_HOME": ""}):
            with self.subTest(env=env):
                self.assertEqual(containment.config_folder(env), os.path.join(home, ".config", "recursion"))
        with mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=home)):
            self.assertEqual(containment.config_folder({}), os.path.join(home, ".config", "recursion"))
            self.assertEqual(containment.config_folder({"HOME": ""}), os.path.join(home, ".config", "recursion"))
        with mock.patch("pwd.getpwuid", side_effect=KeyError(os.geteuid())):
            self.refused("containment_untrusted", lambda: containment.read({}))
        for env in ({"XDG_CONFIG_HOME": "relative"}, {"HOME": "relative"}, {"XDG_CONFIG_HOME": "/a\0b"}):
            with self.subTest(env=env):
                error = self.refused("containment_untrusted", lambda: containment.read(env))
                self.assertIn("is not an absolute path", error.detail)

    # -- attacks on the file (Q17: "the file as a link, a FIFO, a directory, another user's, world writable") --

    def test_the_file_as_a_link_is_refused_whether_or_not_it_points_anywhere(self):
        os.makedirs(self.folder, 0o700)
        good = os.path.join(self.base, "good.json")
        with open(good, "wb") as handle:
            handle.write(_doc(OUTSIDE))
        os.chmod(good, 0o600)
        for target in (good, os.path.join(self.base, "missing.json")):
            with self.subTest(target=target):
                os.symlink(target, self.file)
                self.assertEqual(self.refused("containment_untrusted").detail, f"{self.file} is a link")
                os.unlink(self.file)

    def test_the_recursion_folder_as_a_link_or_a_file_is_refused(self):
        real = os.path.join(self.base, "real-folder")
        os.mkdir(real, 0o700)
        with open(os.path.join(real, "containment.json"), "wb") as handle:
            handle.write(_doc())
        os.chmod(os.path.join(real, "containment.json"), 0o600)
        os.mkdir(self.config, 0o700)
        for target in (real, os.path.join(self.base, "missing")):
            with self.subTest(target=target):
                os.symlink(target, self.folder)
                self.assertEqual(self.refused("containment_untrusted").detail, f"{self.folder} is a link")
                os.unlink(self.folder)
        with open(self.folder, "wb"):
            pass
        self.assertEqual(self.refused("containment_untrusted").detail, f"{self.folder} is not a folder")

    def test_a_fifo_a_folder_and_a_socket_in_the_files_place_are_refused_without_blocking(self):
        os.makedirs(self.folder, 0o700)
        os.mkfifo(self.file, 0o600)
        answers = []
        reader = threading.Thread(target=lambda: answers.append(self._code()), daemon=True)
        reader.start()
        reader.join(10)
        self.assertFalse(reader.is_alive(), "the read blocked on a FIFO")
        self.assertEqual(answers, ["containment_untrusted"])
        os.unlink(self.file)
        os.mkdir(self.file, 0o700)
        self.assertIn("not a regular file", self.refused("containment_untrusted").detail)
        os.rmdir(self.file)
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(self.file)
        self.assertIn("not a regular file", self.refused("containment_untrusted").detail)

    def _code(self):
        try:
            containment.read(self.env)
        except ContainmentRefused as exc:
            return exc.code
        return "accepted"

    def test_another_users_file_or_folder_is_refused(self):
        self.put(_doc())
        for target, said in ((self.file, f"{self.file} is not yours"), (self.folder, f"{self.folder} is not yours")):
            ino = _real_lstat(target).st_ino

            def fstat(fd, *args, **kwargs):
                info = _real_fstat(fd, *args, **kwargs)
                if info.st_ino == ino:
                    fields = list(info[:10])
                    fields[4] = info.st_uid + 1
                    return os.stat_result(fields)
                return info
            with self.subTest(target=target), mock.patch("os.fstat", side_effect=fstat):
                self.assertEqual(self.refused("containment_untrusted").detail, said)
        for uid, code in ((os.geteuid() + 1, "containment_untrusted"), (0, None)):  # a folder above: root's is trusted
            def lstat(path, *args, **kwargs):
                info = _real_lstat(path, *args, **kwargs)
                if os.fspath(path) == self.config:
                    fields = list(info[:10])
                    fields[4] = uid
                    return os.stat_result(fields)
                return info
            with self.subTest(above_uid=uid), mock.patch("os.lstat", side_effect=lstat):
                if code:
                    self.assertEqual(self.refused(code).detail, f"{self.config} is owned by another user")
                else:
                    self.assertEqual(containment.read(self.env).source, "file")

    def test_group_or_world_writable_anywhere_on_the_way_is_refused(self):
        self.put(_doc())
        for mode in (0o620, 0o602, 0o660, 0o666, 0o622):
            with self.subTest(file=oct(mode)):
                os.chmod(self.file, mode)
                self.assertEqual(self.refused("containment_untrusted").detail,
                                 f"{self.file} is writable by group or world; `chmod 600` it")
        os.chmod(self.file, 0o600)
        for mode in (0o720, 0o702, 0o770, 0o777):
            with self.subTest(folder=oct(mode)):
                os.chmod(self.folder, mode)
                self.assertIn("writable by group or world", self.refused("containment_untrusted").detail)
        os.chmod(self.folder, 0o755)
        self.assertEqual(containment.read(self.env).source, "file")
        os.chmod(self.folder, 0o700)
        for above in (self.config, self.base):
            for mode in (0o777, 0o720, 0o702):
                with self.subTest(above=above, mode=oct(mode)):
                    os.chmod(above, mode)
                    self.assertEqual(self.refused("containment_untrusted").detail,
                                     f"{above} is writable by group or world and not sticky")
            os.chmod(above, 0o1777)  # sticky, as /tmp
            self.assertEqual(containment.read(self.env).source, "file")
            os.chmod(above, 0o700)

    def test_a_link_above_the_folder_is_followed_and_held_to_the_same_rule(self):
        self.put(_doc(OUTSIDE))
        alias = os.path.join(self.base, "config-alias")
        os.symlink(self.config, alias)
        env = {"XDG_CONFIG_HOME": alias}
        self.assertEqual(containment.read(env).engine_homes, OUTSIDE)  # e.g. ~/.config kept elsewhere
        os.chmod(self.config, 0o777)
        self.refused("containment_untrusted", lambda: containment.read(env))
        os.chmod(self.config, 0o700)

        def lstat(path, *args, **kwargs):
            info = _real_lstat(path, *args, **kwargs)
            if os.fspath(path) == alias:
                fields = list(info[:10])
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info
        with mock.patch("os.lstat", side_effect=lstat):
            error = self.refused("containment_untrusted", lambda: containment.read(env))
        self.assertEqual(error.detail, f"{alias} is a link another user owns")
        os.symlink("loop-b", os.path.join(self.base, "loop-a"))
        os.symlink("loop-a", os.path.join(self.base, "loop-b"))
        error = self.refused("containment_untrusted",
                             lambda: containment.read({"XDG_CONFIG_HOME": os.path.join(self.base, "loop-a")}))
        self.assertIn("more than 40 links", error.detail)
        a_file = os.path.join(self.base, "a-file")
        with open(a_file, "wb"):
            pass
        error = self.refused("containment_untrusted", lambda: containment.read({"XDG_CONFIG_HOME": a_file}))
        self.assertEqual(error.detail, f"{a_file} is not a folder")
        # A folder above that does not exist: no file, so the default.
        self.assertEqual(containment.read({"XDG_CONFIG_HOME": os.path.join(self.base, "no", "such")}).source,
                         "default")

    def test_a_half_written_or_malformed_file_is_invalid_never_the_default(self):
        cases = [
            b"", b"{", _doc()[:-1], b"\xff\xfe", b"\xef\xbb\xbf" + _doc(), b"[]", b'"inside_operator_home"',
            b"null", b'{"version": 1}', b'{"engine_homes": "inside_operator_home"}',
            b'{"version": "1", "engine_homes": "inside_operator_home"}',
            b'{"version": true, "engine_homes": "inside_operator_home"}',
            b'{"version": 1.0, "engine_homes": "inside_operator_home"}',
            b'{"version": 2, "engine_homes": "inside_operator_home"}',
            _doc(extra_choice=True), _doc(**{"Engine_homes": OUTSIDE}),
            b'{"version": 1, "engine_homes": "inside_operator_home", "engine_homes": "outside_operator_home"}',
            b'{"version": 1, "engine_homes": 1}', b'{"version": 1, "engine_homes": "INSIDE_OPERATOR_HOME"}',
            b'{"version": 1, "engine_homes": null}', b'{"version": NaN, "engine_homes": "inside_operator_home"}',
            b"[" * 60000, b'{"version": 1, "engine_homes": "inside_operator_home"}' + b" " * 70000,
        ]
        for data in cases:
            with self.subTest(data=data[:60]):
                self.put(data)
                self.refused("containment_invalid")
        self.put(_doc(**{f"k{n}": n for n in range(9)}))
        detail = self.refused("containment_invalid").detail
        self.assertIn("a newer Recursion may have written it", detail)
        self.assertIn(", ...", detail)
        self.put(b'{"version": 1, "engine_homes": "inside_operator_home", "engine_homes": "inside_operator_home"}')
        self.assertIn("has the key 'engine_homes' twice", self.refused("containment_invalid").detail)

    def test_an_unreadable_file_or_folder_is_refused(self):
        self.put(_doc())
        os.chmod(self.file, 0o000)
        self.assertEqual(self.refused("containment_unreadable").detail, f"{self.file}: PermissionError")
        os.chmod(self.file, 0o600)
        os.chmod(self.folder, 0o300)  # searchable, not readable: cannot be opened
        self.assertEqual(self.refused("containment_unreadable").detail, f"{self.folder}: PermissionError")
        os.chmod(self.folder, 0o700)
        os.chmod(self.config, 0o600)  # not searchable: nothing below can be looked up
        self.refused("containment_unreadable")
        os.chmod(self.config, 0o700)
        with mock.patch("os.read", side_effect=OSError(errno.EIO, "Input/output error")):
            self.assertEqual(self.refused("containment_unreadable").detail, f"{self.file}: OSError")

    # -- the setting changed between two reads (Q17) -----------------------------------------------------

    def test_a_file_rewritten_in_place_while_it_is_read_is_refused(self):
        same_size = _doc(INSIDE) + b" "  # as long as _doc(OUTSIDE): only the bytes differ
        for first, second in ((_doc(OUTSIDE), _doc(INSIDE)), (_doc(OUTSIDE), same_size)):
            with self.subTest(second=second):
                self.put(first)
                done, seen = [], {}

                def rewrite(fd, size):
                    data = _real_read(fd, size)
                    if not done:
                        done.append(True)
                        with open(self.file, "r+b") as handle:  # the same file, rewritten where it is
                            handle.truncate(0)
                            handle.write(second)
                    return data

                def fstat(fd, *args, **kwargs):  # each inode's first size and times, as a coarse clock would show them
                    info = _real_fstat(fd, *args, **kwargs)
                    return seen.setdefault((info.st_dev, info.st_ino), info)
                with mock.patch("os.read", side_effect=rewrite), mock.patch("os.fstat", side_effect=fstat):
                    error = self.refused("containment_changed")
                self.assertEqual(error.detail, f"{self.file} changed while it was read; run the command again")

    def test_a_file_replaced_or_removed_while_it_is_read_is_refused_and_the_next_reading_is_the_new_one(self):
        def replace():
            new = self.file + ".new"
            with open(new, "wb") as handle:
                handle.write(_doc(INSIDE))
            os.chmod(new, 0o600)
            os.rename(new, self.file)  # as write() does: a new file renamed over the old one
        for change, then in ((replace, INSIDE), (lambda: os.unlink(self.file), None)):
            with self.subTest(change=change):
                self.put(_doc(OUTSIDE))
                changed, first = [], {}

                def read(fd, size):
                    if not changed:
                        changed.append(True)
                        change()
                    return _real_read(fd, size)

                def fstat(fd, *args, **kwargs):  # each inode's first times, as a coarse clock would show them
                    info = _real_fstat(fd, *args, **kwargs)
                    return first.setdefault((info.st_dev, info.st_ino), info)
                with mock.patch("os.read", side_effect=read), mock.patch("os.fstat", side_effect=fstat):
                    self.refused("containment_changed")
                reading = containment.read(self.env)
                self.assertEqual((reading.engine_homes, reading.source),
                                 (then or INSIDE, "file" if then else "default"))

    def test_two_readings_around_a_write_each_answer_one_whole_value(self):
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            containment.write(OUTSIDE, self.env)
            first = containment.read(self.env)
            containment.write(INSIDE, self.env)
            second = containment.read(self.env)
        self.assertEqual((first.engine_homes, second.engine_homes), (OUTSIDE, INSIDE))
        self.assertEqual((first.allow_inside_operator_home, second.allow_inside_operator_home), (False, True))

    # -- writing ------------------------------------------------------------------------------------------

    def test_write_makes_owner_only_folders_and_a_0600_file_and_twice_changes_nothing(self):
        old = os.umask(0)
        try:
            reading, changed = containment.write(INSIDE, self.env)
        finally:
            os.umask(old)
        self.assertEqual((reading, changed), (containment.Containment(INSIDE, "file", self.file), True))
        for folder in (self.config, self.folder):
            self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        with open(self.file, "rb") as handle:
            self.assertEqual(handle.read(), b'{"engine_homes": "inside_operator_home", "version": 1}\n')
        before = os.stat(self.file)
        self.assertEqual(containment.write(INSIDE, self.env), (reading, False))
        after = os.stat(self.file)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])
        old = os.umask(0o277)  # a umask that would leave the file unreadable to its owner
        try:
            with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
                containment.write(OUTSIDE, self.env)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(containment.read(self.env).engine_homes, OUTSIDE)

    def test_outside_is_refused_without_an_engine_user_and_nothing_is_made(self):
        error = self.refused("containment_needs_engine_user", lambda: containment.write(OUTSIDE, self.env))
        self.assertEqual(error.detail, "outside_operator_home needs the engine user, which this machine does not have")
        self.assertIsNone(containment.engine_user())
        for value in ("INSIDE_OPERATOR_HOME", "", None, 1, ["inside_operator_home"]):
            with self.subTest(value=value):
                self.refused("containment_invalid", lambda: containment.write(value, self.env))
        self.assertFalse(os.path.lexists(self.config))

    def test_write_refuses_to_replace_a_file_it_cannot_read_and_leaves_it_as_it_was(self):
        other = os.path.join(self.base, "other.json")
        with open(other, "wb") as handle:
            handle.write(_doc(OUTSIDE))
        for name, make, code in (
                ("a link", lambda: os.symlink(other, self.file), "containment_untrusted"),
                ("world writable", lambda: self.put(_doc(OUTSIDE), 0o666), "containment_untrusted"),
                ("a FIFO", lambda: os.mkfifo(self.file, 0o600), "containment_untrusted"),
                ("half written", lambda: self.put(b'{"version": 1, "engine'), "containment_invalid"),
                ("a newer one", lambda: self.put(_doc(OUTSIDE, sandbox="strict")), "containment_invalid")):
            with self.subTest(name=name):
                os.makedirs(self.folder, 0o700, exist_ok=True)
                make()
                before = os.lstat(self.file)
                self.refused(code, lambda: containment.write(INSIDE, self.env))
                after = os.lstat(self.file)
                self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                                 (before.st_ino, before.st_mode, before.st_mtime_ns))
                self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])
                os.unlink(self.file)
        with open(other, "rb") as handle:
            self.assertEqual(handle.read(), _doc(OUTSIDE))  # the link's target was never written

    def test_a_write_that_fails_leaves_the_old_file_and_no_temporary_file(self):
        self.put(_doc(INSIDE))
        real_fsync = os.fsync

        def fsync(fd):
            if os.path.basename(os.readlink(f"/proc/self/fd/{fd}")).startswith(".containment.json."):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=fsync), \
                mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            with self.assertRaises(OSError):
                containment.write(OUTSIDE, self.env)
        self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])
        self.assertEqual(containment.read(self.env).engine_homes, INSIDE)

    # -- doctor ---------------------------------------------------------------------------------------

    def assert_check_shape(self, check: dict) -> None:
        """``doctor_report``'s check, methods.v1.json on SPINE's #368 (59361f0c): a closed object."""
        self.assertEqual(set(check), {"id", "subject", "state", "detail", "fix"})
        self.assertRegex(check["id"], r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
        self.assertIn(check["state"], {"ok", "warn", "fail", "unknown"})
        self.assertIsInstance(check["detail"], str)
        for key in ("subject", "fix"):
            self.assertTrue(check[key] is None or isinstance(check[key], str), key)

    def test_doctor_reports_the_value_and_the_level(self):
        check = containment.doctor_check(self.env)
        self.assert_check_shape(check)
        self.assertEqual((check["id"], check["subject"], check["state"]),
                         ("containment.engine_homes", self.file, "warn"))
        self.assertTrue(check["detail"].startswith(
            "engine_homes is inside_operator_home (the default: no file); level same_user. "), check["detail"])
        self.assertTrue(check["detail"].endswith(containment.OPERATOR_WORDS))
        self.assertIn("outside_operator_home", check["fix"])
        self.put(_doc(OUTSIDE))
        check = containment.doctor_check(self.env)
        self.assert_check_shape(check)
        self.assertEqual(check["state"], "fail")
        self.assertTrue(check["detail"].endswith(
            "level engine_user, which needs the engine user, which this machine does not have"), check["detail"])
        self.assertIn("inside_operator_home", check["fix"])
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            check = containment.doctor_check(self.env)
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["fix"]), ("ok", None))
        self.assertIn("engines run as recursion-engines", check["detail"])
        os.chmod(self.file, 0o666)
        check = containment.doctor_check(self.env)
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["subject"]), ("fail", self.file))
        self.assertIn("containment_untrusted", check["detail"])
        check = containment.doctor_check({"XDG_CONFIG_HOME": "relative"})
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["subject"]), ("fail", None))


class OperatorWordsTests(unittest.TestCase):
    def test_the_words_name_the_danger_and_the_way_out(self):
        words = containment.OPERATOR_WORDS
        for part in ("~/.local/state/recursion/homes/<engine>", "They run as you",
                     "your files, your SSH keys, your browser and other app logins",
                     "cannot stop a program running as you from reaching the rest of your home",
                     "set up a separate engine user"):
            self.assertIn(part, words)
        self.assertTrue(words.isascii())
        self.assertIsNone(re.search(r"\s{2}", words))

    def test_the_module_imports_the_standard_library_only(self):
        import ast
        with open(containment.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, {"__future__", "dataclasses"})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, {"errno", "json", "os", "pwd", "secrets", "stat"})


if __name__ == "__main__":
    unittest.main()
