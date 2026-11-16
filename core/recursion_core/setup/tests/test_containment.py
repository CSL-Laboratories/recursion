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
import shutil
import socket
import stat
import tempfile
import threading
import time
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
        self.home = os.path.join(self.base, "home")  # the password database's home for this user, here
        os.mkdir(self.home, 0o700)
        self.config = os.path.join(self.home, ".config")
        self.folder = os.path.join(self.config, "recursion")
        self.file = os.path.join(self.folder, "containment.json")
        self.addCleanup(self._reopen)
        passwd = self.passwd(self.home)
        passwd.start()
        self.addCleanup(passwd.stop)
        # HOME and XDG_CONFIG_HOME name other folders: neither moves the setting's file (Q19).
        env = mock.patch.dict(os.environ, {"HOME": os.path.join(self.base, "env-home"),
                                           "XDG_CONFIG_HOME": os.path.join(self.base, "env-config")})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def passwd(home):
        return mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=home))

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
            (call or containment.read)()
        self.assertEqual(caught.exception.code, code, caught.exception.detail)
        return caught.exception

    # -- reading ---------------------------------------------------------------------------------------

    def test_no_file_is_the_default_said_as_such_and_nothing_is_made(self):
        reading = containment.read()
        self.assertEqual(reading, containment.Containment(INSIDE, "default", self.file))
        self.assertEqual((reading.allow_inside_operator_home, reading.level), (True, "same_user"))
        self.assertFalse(os.path.lexists(self.config))
        os.mkdir(self.config, 0o700)  # the config folder alone
        self.assertEqual(containment.read().source, "default")
        os.mkdir(self.folder, 0o700)  # the recursion folder, no file
        self.assertEqual(containment.read().source, "default")
        self.assertEqual(os.listdir(self.folder), [])

    def test_a_file_is_read_with_its_value_and_level(self):
        for value, allow, level in ((OUTSIDE, False, "engine_user"), (INSIDE, True, "same_user")):
            with self.subTest(value=value):
                self.put(_doc(value))
                reading = containment.read()
                self.assertEqual(reading, containment.Containment(value, "file", self.file))
                self.assertEqual((reading.allow_inside_operator_home, reading.level), (allow, level))
        for mode in (0o644, 0o640, 0o400):  # readable by others is not a trust problem; writable is
            with self.subTest(mode=oct(mode)):
                os.chmod(self.file, mode)
                self.assertEqual(containment.read().source, "file")

    # The Liaison's ruling on Q19 (Nov 15, 6:59 PM): one place only, under the password database's home.
    def test_the_file_is_under_the_password_databases_home_whatever_the_environment_says(self):
        for env in ({}, {"HOME": "/elsewhere"}, {"XDG_CONFIG_HOME": "/xdg"}, {"XDG_CONFIG_HOME": "relative"},
                    {"HOME": "", "XDG_CONFIG_HOME": ""}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(containment.config_folder(), self.folder)
        with self.passwd(self.home + "/./x/.."):  # DeepSeek I5: one spelling of the path, as doctor prints it
            self.assertEqual(containment.config_folder(), self.folder)
        with mock.patch("pwd.getpwuid", side_effect=KeyError(os.geteuid())):
            error = self.refused("containment_untrusted")
        self.assertIn("the password database gives no home for this user", error.detail)
        for home in ("relative", "", "/a\0b"):
            with self.subTest(home=home), self.passwd(home):
                self.assertIn("is not an absolute path", self.refused("containment_untrusted").detail)

    def test_the_shell_and_the_user_manager_read_the_same_file(self):
        shell = {"HOME": self.home, "XDG_CONFIG_HOME": os.path.join(self.base, "shell-config")}
        manager = {"HOME": self.home}  # the user manager's environment need not have the shell's XDG_CONFIG_HOME
        with mock.patch.dict(os.environ, shell, clear=True):
            written, _ = containment.write(INSIDE)
        with mock.patch.dict(os.environ, manager, clear=True):
            read = containment.read()
        self.assertEqual((read, written.path), (written, self.file))
        self.assertFalse(os.path.lexists(os.path.join(self.base, "shell-config")))

    def test_a_file_under_the_xdg_folder_alone_is_not_read_and_doctor_names_it(self):
        xdg = os.path.join(self.base, "xdg")
        stray = os.path.join(xdg, "recursion", "containment.json")
        os.makedirs(os.path.dirname(stray), 0o700)
        with open(stray, "wb") as handle:
            handle.write(_doc(OUTSIDE))
        os.chmod(stray, 0o600)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}):
            self.assertEqual(containment.read(), containment.Containment(INSIDE, "default", self.file))
        checks = containment.doctor_checks({"XDG_CONFIG_HOME": xdg})
        self.assertEqual([check["id"] for check in checks], ["containment.engine_homes", "containment.config_folder"])
        for check in checks:
            self.assert_check_shape(check)
        self.assertEqual((checks[0]["subject"], checks[0]["state"]), (self.file, "warn"))
        self.assertEqual((checks[1]["subject"], checks[1]["state"]), (stray, "warn"))
        self.assertTrue(checks[1]["detail"].startswith(f"{stray} is not read: the containment setting is read only "
                                                       f"from {self.file}"), checks[1]["detail"])
        self.assertIn(stray, checks[1]["fix"])
        os.unlink(stray)  # XDG_CONFIG_HOME names another folder, with no file there: said in a line
        checks = containment.doctor_checks({"XDG_CONFIG_HOME": xdg})
        self.assertEqual((checks[1]["subject"], checks[1]["state"], checks[1]["fix"]),
                         (os.path.join(xdg, "recursion"), "ok", None))
        self.assertIn(f"read only from {self.file}", checks[1]["detail"])
        for env in ({}, {"XDG_CONFIG_HOME": ""}, {"XDG_CONFIG_HOME": self.config},
                    {"XDG_CONFIG_HOME": self.config + "/"}):
            with self.subTest(env=env):  # no other folder named: one line only
                self.assertEqual(len(containment.doctor_checks(env)), 1)
        alias = os.path.join(self.base, "config-alias")
        os.symlink(self.config, alias)
        self.assertEqual(len(containment.doctor_checks({"XDG_CONFIG_HOME": alias})), 1)  # the same folder by a link

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
        # Bound through its folder's descriptor: a short name whatever the folder's path (an absolute temporary
        # path can pass AF_UNIX's limit), and the working folder left alone (an earlier test may have removed it).
        self.bind_socket(server, self.folder, self.file)
        self.assertIn("not a regular file", self.refused("containment_untrusted").detail)

    def bind_socket(self, server, folder, path):
        """Bind ``server`` at ``path`` (in ``folder``) through the folder's descriptor: a short name whatever the
        folder's path (an absolute temporary path can pass AF_UNIX's limit), the working folder left alone.
        /proc/self/fd is Linux's; elsewhere (macOS) ``path`` itself is bound when it fits AF_UNIX's limit there,
        and the test is skipped when it does not (the Liaison's ruling on #382's like test)."""
        held = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, held)
        if os.path.isdir(f"/proc/self/fd/{held}"):
            server.bind(f"/proc/self/fd/{held}/{os.path.basename(path)}")
        elif len(os.fsencode(path)) < 104:
            server.bind(path)
        else:
            self.skipTest(f"no /proc/self/fd here, and the path ({len(os.fsencode(path))} bytes) is past "
                          "AF_UNIX's limit")

    def test_the_socket_fixture_without_proc_self_fd_binds_the_path_or_skips(self):
        # Sol's C2 on #428: the fixture's branch for a system without /proc/self/fd (macOS), never an error.
        os.makedirs(self.folder, 0o700)
        real_isdir = os.path.isdir
        no_proc = mock.patch("os.path.isdir", side_effect=lambda p: not str(p).startswith("/proc/") and real_isdir(p))
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        with no_proc, self.assertRaises(unittest.SkipTest):  # a path past the limit: skipped, with its reason
            self.bind_socket(server, self.folder, os.path.join("/", "x" * 200, "containment.json"))
        if len(os.fsencode(self.file)) >= 104:
            self.skipTest("this run's folder is too long to bind by its own path")
        with no_proc:
            try:
                self.bind_socket(server, self.folder, self.file)
            except unittest.SkipTest as skipped:  # a short path must be bound, not skipped
                self.fail(f"a short path was not bound by its own path: {skipped}")
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.file).st_mode))  # bound by its own path

    def _code(self):
        try:
            containment.read()
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
                    self.assertEqual(containment.read().source, "file")

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
        self.assertEqual(containment.read().source, "file")
        os.chmod(self.folder, 0o700)
        for above in (self.config, self.base):
            for mode in (0o777, 0o720, 0o702):
                with self.subTest(above=above, mode=oct(mode)):
                    os.chmod(above, mode)
                    self.assertEqual(self.refused("containment_untrusted").detail,
                                     f"{above} is writable by group or world and not sticky")
            os.chmod(above, 0o1777)  # sticky, as /tmp
            self.assertEqual(containment.read().source, "file")
            os.chmod(above, 0o700)

    # Review 1 of #428, Codex GPT-6 Astra R1, and the Liaison's ruling on Q21 (7:11 PM): no link is followed at
    # or below the operator's home; above it, a link is followed only when only root can change it. The tests
    # cannot make a file root's, so ``os.lstat`` reports the named paths as root's.
    def owners(self, uids: dict):
        """``os.lstat`` reporting each path in ``uids`` as owned by its uid."""
        uids = {os.fspath(path): uid for path, uid in uids.items()}

        def lstat(path, *args, **kwargs):
            info = _real_lstat(path, *args, **kwargs)
            if os.fspath(path) in uids:
                fields = list(info[:10])
                fields[4] = uids[os.fspath(path)]
                return os.stat_result(fields)
            return info
        return mock.patch("os.lstat", side_effect=lstat)

    def roots(self, *paths):
        return self.owners({path: 0 for path in paths})

    def silverblue(self):
        """``top`` (root's, 0755, as ``/`` is) holds ``sys`` and ``var`` (root's, 0755); ``sys`` holds ``home``, root's
        link to ``../var/home`` (root's, 0755), which holds the operator's home ``u`` with the setting in it:
        Silverblue's ``/home`` -> ``var/home``. The link's ``..`` climbs to ``top``, which must be root's alone too."""
        top = os.path.join(self.base, "top")
        sysdir, var = os.path.join(top, "sys"), os.path.join(top, "var")
        varhome = os.path.join(var, "home")
        for folder in (top, sysdir, var, varhome):
            os.mkdir(folder)
            os.chmod(folder, 0o755)
        user = os.path.join(varhome, "u")
        os.makedirs(os.path.join(user, ".config", "recursion"), 0o700)
        os.chmod(user, 0o700)
        os.chmod(os.path.join(user, ".config"), 0o700)
        with open(os.path.join(user, ".config", "recursion", "containment.json"), "wb") as handle:
            handle.write(_doc(OUTSIDE))
        link = os.path.join(sysdir, "home")
        os.symlink("../var/home", link)
        return os.path.join(link, "u"), link, [sysdir, link, var, varhome, top]

    def test_a_link_only_root_can_change_above_the_home_is_followed(self):
        home, link, owned = self.silverblue()
        with self.roots(*owned), self.passwd(home):
            reading = containment.read()
            self.assertEqual((reading.engine_homes, reading.path),
                             (OUTSIDE, os.path.join(home, ".config", "recursion", "containment.json")))
            self.assertEqual(containment.write(INSIDE)[1], True)
            self.assertEqual(containment.read().engine_homes, INSIDE)
            self.assertEqual(containment.doctor_checks()[0]["state"], "warn")

    def test_a_link_above_the_home_that_anyone_but_root_could_change_is_refused(self):
        home, link, owned = self.silverblue()
        sysdir, var, varhome, top = owned[0], owned[2], owned[3], owned[4]
        cases = [
            ("the operator's link", [sysdir, var, varhome, top], {},
             f"{link} is a link above your home that root does not own"),
            ("another user's link", [sysdir, var, varhome, top], {link: os.geteuid() + 1},
             f"{link} is a link above your home that root does not own"),
            ("in the operator's folder", [link, var, varhome, top], {},
             f"{sysdir}, which holds the link {link}, is not root's"),
            ("a target folder the operator owns", [sysdir, link, var, top], {},
             f"{varhome}, on the way through a link above your home, is not root's"),
            # Sol's C1 on #428: the parent the target's ".." climbs to is on the way, held to the same rule.
            ("the parent the target climbs to, the operator's", [sysdir, link, var, varhome], {},
             f"{top}, on the way through a link above your home, is not root's"),
        ]
        for name, as_root, others, said in cases:
            with self.subTest(case=name), self.owners({**{path: 0 for path in as_root}, **others}), self.passwd(home):
                error = self.refused("containment_untrusted")
            self.assertTrue(error.detail.startswith(said), error.detail)
        # Group or others able to write in the folder holding the link: the folder rule says so first; sticky,
        # which that rule allows, does not count as safe for a link above the home.
        for folder, mode, said in ((sysdir, 0o775, f"{sysdir} is writable by group or world and not sticky"),
                                   (sysdir, 0o757, f"{sysdir} is writable by group or world and not sticky"),
                                   (sysdir, 0o1777, f"{sysdir}, which holds the link {link}, is not root's alone"),
                                   (var, 0o777,
                                    f"{var}, on the way through a link above your home, is not root's alone"),
                                   (varhome, 0o1777, f"{varhome}, on the way through a link above your home, is not "
                                                     "root's alone"),
                                   (top, 0o1777, f"{top}, on the way through a link above your home, is not "
                                                 "root's alone")):
            with self.subTest(folder=folder, mode=oct(mode)), self.roots(*owned), self.passwd(home):
                os.chmod(folder, mode)
                try:
                    error = self.refused("containment_untrusted")
                finally:
                    os.chmod(folder, 0o755)
                self.assertTrue(error.detail.startswith(said), error.detail)
        a_file = os.path.join(top, "a-file")
        with open(a_file, "wb"):
            pass
        os.symlink("../a-file", os.path.join(sysdir, "to-a-file"))
        with self.roots(*owned, os.path.join(sysdir, "to-a-file")), self.passwd(os.path.join(sysdir, "to-a-file", "u")):
            self.assertEqual(self.refused("containment_untrusted").detail, f"{a_file} is not a folder")
        absolute = os.path.join(sysdir, "absolute")
        os.symlink(varhome, absolute)  # an absolute target: walked again from /, each folder held to the rule
        with self.roots(*owned, absolute), self.passwd(os.path.join(absolute, "u")):
            error = self.refused("containment_untrusted")
        self.assertIn("on the way through a link above your home, is not root's alone", error.detail)

    def test_a_chain_longer_than_the_bound_or_a_loop_is_refused_with_its_reason(self):
        home, link, owned = self.silverblue()
        sysdir = owned[0]
        chain = [os.path.join(sysdir, f"l{n}") for n in range(41)]
        for n, path in enumerate(chain):
            os.symlink(f"l{n + 1}" if n + 1 < len(chain) else "../var/home", path)
        with self.roots(*owned, *chain):
            with self.passwd(os.path.join(chain[1], "u")):  # 40 links: the bound itself is followed
                self.assertEqual(containment.read().engine_homes, OUTSIDE)
            with self.passwd(os.path.join(chain[0], "u")):  # 41 links
                self.assertEqual(self.refused("containment_untrusted").detail, "more than 40 links above your home")
        loop = [os.path.join(sysdir, "loop-a"), os.path.join(sysdir, "loop-b")]
        os.symlink("loop-b", loop[0])
        os.symlink("loop-a", loop[1])
        with self.roots(*owned, *loop), self.passwd(os.path.join(loop[0], "u")):
            self.assertEqual(self.refused("containment_untrusted").detail, "more than 40 links above your home")

    def test_every_link_at_or_below_the_home_is_still_refused(self):
        home, link, owned = self.silverblue()
        sysdir, varhome = owned[0], owned[3]
        at = os.path.join(sysdir, "u-link")
        os.symlink("../var/home/u", at)  # the home itself through root's link
        with self.roots(*owned, at), self.passwd(at):
            for name, call in (("read", containment.read), ("write", lambda: containment.write(INSIDE))):
                with self.subTest(at="the home", call=name):
                    self.assertEqual(self.refused("containment_untrusted", call).detail,
                                     f"{at} is a link: the setting is not read through a link")
        self.put(_doc(OUTSIDE))
        elsewhere = os.path.join(self.base, "elsewhere-config")
        os.rename(self.config, elsewhere)
        os.symlink(elsewhere, self.config)  # ~/.config through the operator's own link (a stowed dotfile folder)
        for name, call in (("read", containment.read), ("write", lambda: containment.write(INSIDE)),
                           ("doctor", lambda: containment.doctor_checks()[0])):
            with self.subTest(below=".config", call=name):
                if name == "doctor":
                    check = call()
                    self.assertEqual(check["state"], "fail")
                    self.assertIn(f"{self.config} is a link", check["detail"])
                    continue
                self.assertEqual(self.refused("containment_untrusted", call).detail,
                                 f"{self.config} is a link: the setting is not read through a link")
        with open(os.path.join(elsewhere, "recursion", "containment.json"), "rb") as handle:
            self.assertEqual(handle.read(), _doc(OUTSIDE))  # the write through the link changed nothing
        os.unlink(self.config)
        os.symlink(os.path.join(self.base, "missing-config"), self.config)  # a dangling one is not "no file"
        self.refused("containment_untrusted")
        missing_home = os.path.join(self.base, "missing-home")  # its folder is there, the home is not
        with self.passwd(missing_home):
            self.assertEqual(containment.read().source, "default")
            error = self.refused("containment_unreadable", lambda: containment.write(INSIDE))
        self.assertEqual(error.detail, f"{missing_home}, the operator's home, does not exist")
        self.assertFalse(os.path.lexists(missing_home))
        a_file = os.path.join(self.base, "a-file")
        with open(a_file, "wb"):
            pass
        with self.passwd(a_file):
            error = self.refused("containment_untrusted")
        self.assertEqual(error.detail, f"{a_file} is not a folder")
        # A folder above the home that does not exist: no home, no file, the default; a write makes nothing there.
        with self.passwd(os.path.join(self.base, "no", "such")):
            self.assertEqual(containment.read().source, "default")
            error = self.refused("containment_unreadable", lambda: containment.write(INSIDE))
            self.assertIn("does not exist", error.detail)
        self.assertFalse(os.path.lexists(os.path.join(self.base, "no")))

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
                reading = containment.read()
                self.assertEqual((reading.engine_homes, reading.source),
                                 (then or INSIDE, "file" if then else "default"))

    def test_two_readings_around_a_write_each_answer_one_whole_value(self):
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            containment.write(OUTSIDE)
            first = containment.read()
            containment.write(INSIDE)
            second = containment.read()
        self.assertEqual((first.engine_homes, second.engine_homes), (OUTSIDE, INSIDE))
        self.assertEqual((first.allow_inside_operator_home, second.allow_inside_operator_home), (False, True))

    # Review 1 of #428, Codex GPT-6 Astra R2 and DeepSeek F2: the first fstat of the folder or the file failing
    # was a raw OSError out of read(), a crash in doctor and the wrong code from the command.
    def test_a_metadata_error_is_typed_everywhere(self):
        self.put(_doc(OUTSIDE))
        for target in (self.folder, self.file):
            ino = _real_lstat(target).st_ino

            def fstat(fd, *args, **kwargs):
                info = _real_fstat(fd, *args, **kwargs)
                if info.st_ino == ino:
                    raise OSError(errno.EIO, "Input/output error")
                return info
            with self.subTest(target=target), mock.patch("os.fstat", side_effect=fstat):
                self.assertEqual(self.refused("containment_unreadable").detail, f"{target}: OSError")
                check = containment.doctor_checks()[0]
                self.assertEqual((check["state"], check["subject"]), ("fail", self.file))
                self.assertIn("containment_unreadable", check["detail"])
                self.refused("containment_unreadable", lambda: containment.write(INSIDE))
        real_close, closed = os.close, []

        def close(fd):  # closed, then an error: nothing read is lost at a close
            real_close(fd)
            closed.append(fd)
            raise OSError(errno.EIO, "Input/output error")
        with mock.patch("os.close", side_effect=close):
            self.assertEqual(containment.read().engine_homes, OUTSIDE)
        self.assertEqual(len(closed), 2)  # the file's descriptor and the folder's

    # Review 1 of #428, Codex GPT-6 Astra R3: a change of the file's metadata alone (the same bytes) and the
    # folder's flush after the rename each had no test that their removal turned red.
    def test_a_metadata_change_with_the_same_bytes_while_read_is_refused(self):
        self.put(_doc(OUTSIDE))
        before = _real_lstat(self.file)
        done = []

        def read(fd, size):
            data = _real_read(fd, size)
            if not done:
                done.append(True)
                os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))  # the bytes stay
            return data
        with mock.patch("os.read", side_effect=read):
            self.assertIn("changed while it was read", self.refused("containment_changed").detail)
        self.assertEqual(containment.read().engine_homes, OUTSIDE)

    # Review 2 of #428, DeepSeek F2: a umask that takes the owner's bits left ~/.config at 0500, and write() failed
    # with a raw PermissionError naming the wrong folder, for good.
    def test_folders_made_are_0700_whatever_the_umask_and_one_that_cannot_be_made_is_named(self):
        for umask in (0o200, 0o277, 0o777):
            with self.subTest(umask=oct(umask)):
                old = os.umask(umask)
                try:
                    self.assertEqual(containment.write(INSIDE)[1], True)
                finally:
                    os.umask(old)
                for folder in (self.config, self.folder):
                    self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700, folder)
                self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
                shutil.rmtree(self.config)
        os.mkdir(self.config, 0o500)  # left so by hand, or by an earlier Recursion under such a umask
        try:
            error = self.refused("containment_unreadable", lambda: containment.write(INSIDE))
        finally:
            os.chmod(self.config, 0o700)
        self.assertEqual(error.detail, f"{self.folder} cannot be made in {self.config}: PermissionError; "
                                       f"{self.config} must be writable and searchable by its owner (`chmod 700` it)")
        self.assertEqual(containment.write(INSIDE)[1], True)  # after the `chmod 700` it names, a write works

    # Review 2 of #428, DeepSeek F3: the ctime term of the comparison had no test of its own. A chmod during the
    # read (the bytes, the size and the mtime unchanged) moves only ctime; the pause puts it past the clock's tick.
    def test_a_change_of_ctime_alone_while_read_is_refused(self):
        self.put(_doc(OUTSIDE))
        done = []

        def read(fd, size):
            data = _real_read(fd, size)
            if not done:
                done.append(True)
                time.sleep(0.05)
                os.chmod(self.file, 0o640)
            return data
        with mock.patch("os.read", side_effect=read):
            self.assertIn("changed while it was read", self.refused("containment_changed").detail)
        self.assertEqual(containment.read().engine_homes, OUTSIDE)

    def test_a_change_flushes_the_file_then_renames_then_flushes_the_folder(self):
        calls = []
        real_fsync, real_rename = os.fsync, os.rename

        def fsync(fd):  # named by fstat, not /proc/self/fd (Sol's C2 on #428: not every system has it)
            info = os.fstat(fd)
            calls.append(("fsync", "folder" if stat.S_ISDIR(info.st_mode) else "file", (info.st_dev, info.st_ino)))
            return real_fsync(fd)

        def rename(*args, **kwargs):
            calls.append(("rename", args[1]))
            return real_rename(*args, **kwargs)
        with mock.patch("os.fsync", side_effect=fsync), mock.patch("os.rename", side_effect=rename):
            containment.write(INSIDE)
        self.assertEqual([call[:2] for call in calls], [("fsync", "file"), ("rename", "containment.json"),
                                                        ("fsync", "folder")])
        written, folder = os.stat(self.file), os.stat(self.folder)
        self.assertEqual((calls[0][2], calls[2][2]), ((written.st_dev, written.st_ino), (folder.st_dev, folder.st_ino)))

    # DeepSeek F3: a failed flush of the folder after the rename was a filesystem_error for a change that had
    # happened; it is a typed refusal that says so, and running the command again makes it durable.
    def test_a_folder_that_cannot_be_flushed_after_the_rename_says_the_value_is_in_place(self):
        real_fsync = os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):  # the folder
                raise OSError(errno.EIO, "Input/output error")
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=fsync):
            error = self.refused("containment_not_flushed", lambda: containment.write(INSIDE))
        self.assertTrue(error.detail.startswith(f"{self.file} holds inside_operator_home now, but its folder"))
        self.assertEqual(containment.read(), containment.Containment(INSIDE, "file", self.file))
        flushed = []

        def record(fd):
            info = os.fstat(fd)
            flushed.append((info.st_dev, info.st_ino))
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=record):
            self.assertEqual(containment.write(INSIDE), (containment.Containment(INSIDE, "file", self.file), False))
        folder = os.stat(self.folder)
        self.assertEqual(flushed, [(folder.st_dev, folder.st_ino)])  # it flushes the folder, and writes nothing else

    # DeepSeek F1 (Medium): two writers at once both answered changed with their own value, one of them
    # untrue. Writers take turns on the folder's lock; the second reads what the first wrote.
    def test_two_writers_take_turns_and_each_answer_is_the_files_value_when_given(self):
        entered, release, answers = threading.Event(), threading.Event(), {}
        real_rename = os.rename

        def rename(*args, **kwargs):
            if threading.current_thread().name == "first":
                entered.set()
                release.wait(10)
            return real_rename(*args, **kwargs)

        def writer(name):
            answers[name] = containment.write(INSIDE)
        with mock.patch("os.rename", side_effect=rename):
            first = threading.Thread(target=writer, args=("first",), name="first")
            first.start()
            self.assertTrue(entered.wait(10))
            second = threading.Thread(target=writer, args=("second",), name="second")
            second.start()
            second.join(0.5)
            waited = second.is_alive()  # the second writer waits for the first's turn to end
            release.set()
            first.join(10)
            second.join(10)
        self.assertTrue(waited)
        self.assertEqual(answers["first"], (containment.Containment(INSIDE, "file", self.file), True))
        self.assertEqual(answers["second"], (containment.Containment(INSIDE, "file", self.file), False))

    # DeepSeek I3: a writer killed before its rename left its temporary file; the next writer removes it.
    def test_a_temporary_file_a_killed_writer_left_is_removed_by_the_next_writer(self):
        os.makedirs(self.folder, 0o700)
        left = os.path.join(self.folder, ".containment.json.4242.0123456789abcdef.tmp")
        with open(left, "wb") as handle:
            handle.write(b'{"engine_homes": "inside_')
        containment.write(INSIDE)
        self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])

    # -- writing ------------------------------------------------------------------------------------------

    def test_write_makes_owner_only_folders_and_a_0600_file_and_twice_changes_nothing(self):
        old = os.umask(0)
        try:
            reading, changed = containment.write(INSIDE)
        finally:
            os.umask(old)
        self.assertEqual((reading, changed), (containment.Containment(INSIDE, "file", self.file), True))
        for folder in (self.config, self.folder):
            self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        with open(self.file, "rb") as handle:
            self.assertEqual(handle.read(), b'{"engine_homes": "inside_operator_home", "version": 1}\n')
        before = os.stat(self.file)
        self.assertEqual(containment.write(INSIDE), (reading, False))
        after = os.stat(self.file)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])
        old = os.umask(0o277)  # a umask that would leave the file unreadable to its owner
        try:
            with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
                containment.write(OUTSIDE)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        self.assertEqual(containment.read().engine_homes, OUTSIDE)

    def test_outside_is_refused_without_an_engine_user_and_nothing_is_made(self):
        error = self.refused("containment_needs_engine_user", lambda: containment.write(OUTSIDE))
        self.assertEqual(error.detail, "outside_operator_home needs the engine user, which this machine does not have")
        self.assertIsNone(containment.engine_user())
        for value in ("INSIDE_OPERATOR_HOME", "", None, 1, ["inside_operator_home"]):
            with self.subTest(value=value):
                self.refused("containment_invalid", lambda: containment.write(value))
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
                self.refused(code, lambda: containment.write(INSIDE))
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
            if stat.S_ISREG(os.fstat(fd).st_mode):  # the new file, the only regular file write() flushes
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)
        with mock.patch("os.fsync", side_effect=fsync), \
                mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            with self.assertRaises(OSError):
                containment.write(OUTSIDE)
        self.assertEqual(sorted(os.listdir(self.folder)), ["containment.json"])
        self.assertEqual(containment.read().engine_homes, INSIDE)

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
        check = containment.doctor_checks()[0]
        self.assert_check_shape(check)
        self.assertEqual((check["id"], check["subject"], check["state"]),
                         ("containment.engine_homes", self.file, "warn"))
        self.assertTrue(check["detail"].startswith(
            "engine_homes is inside_operator_home (the default: no file); level same_user. "), check["detail"])
        # DeepSeek F4: no machine has the engine user yet, so no fix is offered that cannot be carried out.
        self.assertTrue(check["detail"].endswith(
            containment.OPERATOR_WORDS + " outside_operator_home needs the engine user, which this machine does "
            "not have."), check["detail"])
        self.assertIsNone(check["fix"])
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            check = containment.doctor_checks()[0]
        self.assertTrue(check["detail"].endswith(containment.OPERATOR_WORDS), check["detail"])
        self.assertEqual(check["fix"], "run `recursiond setup containment outside_operator_home` to keep "
                                       "engines out of your home")
        self.put(_doc(OUTSIDE))
        check = containment.doctor_checks()[0]
        self.assert_check_shape(check)
        self.assertEqual(check["state"], "fail")
        self.assertTrue(check["detail"].endswith(
            "level engine_user, which needs the engine user, which this machine does not have"), check["detail"])
        self.assertIn("inside_operator_home", check["fix"])
        with mock.patch.object(containment, "engine_user", return_value="recursion-engines"):
            check = containment.doctor_checks()[0]
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["fix"]), ("ok", None))
        self.assertIn("engines run as recursion-engines", check["detail"])
        os.chmod(self.file, 0o666)
        check = containment.doctor_checks()[0]
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["subject"]), ("fail", self.file))
        self.assertIn("containment_untrusted", check["detail"])
        with mock.patch("pwd.getpwuid", side_effect=KeyError(os.geteuid())):
            check = containment.doctor_checks()[0]
        self.assert_check_shape(check)
        self.assertEqual((check["state"], check["subject"]), ("fail", None))


class OperatorWordsTests(unittest.TestCase):
    def test_the_words_name_the_danger_and_claim_no_protection(self):
        # The Liaison, Nov 15 4:36 PM: a private home separates configuration and state and is not
        # isolation; the words must say exactly that and must not suggest protection.
        words = containment.OPERATOR_WORDS
        for part in ("Engines run as your user", "~/.local/state/recursion/homes/<engine>",
                     "keeps its settings and sign-ins separate, but does not prevent access to your other "
                     "files, credentials or local services",
                     "an engine can read or change anything your user account can",
                     "your SSH keys and your browser and other app logins", "separate engine user"):
            self.assertIn(part, words)
        for claim in ("protect", "safe", "secure", "isolat", "never", "keeps you", "private to you"):
            self.assertNotIn(claim, words.lower())
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
                    self.assertIn(alias.name, {"errno", "fcntl", "json", "os", "pwd", "secrets", "stat"})


if __name__ == "__main__":
    unittest.main()
