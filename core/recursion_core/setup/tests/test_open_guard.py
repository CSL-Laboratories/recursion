"""The settings-file gate: no-follow refusal, owner and write bits, and the walked open.

Each test was red on 8d2b06d8: the gate opened without ``O_NOFOLLOW`` when the name was
missing, read a file owned by another user or writable by group or world, and opened the
path by name after checking its real path, so an ancestor swapped in between was followed.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.setup import open_guard

_MARKER = "CANARY-GUARD-FAKE-7c1e9a4b-DO-NOT-LEAK"
_OPENED: list = []
_HOOKED = False


def _remember_open(event, args):
    if event == "open" and args:
        try:
            _OPENED.append(args[0])
        except Exception:
            return


def _watch_opens():
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_remember_open)
        _HOOKED = True


def _names(opened):
    return [os.fsdecode(raw) if isinstance(raw, bytes) else str(raw) for raw in opened]


def _write(path, text, mode):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)


class _Layout:
    def _layout(self):
        root = tempfile.TemporaryDirectory(prefix="open-guard-")
        self.addCleanup(root.cleanup)
        home = os.path.join(root.name, "home")
        os.mkdir(home, 0o700)
        os.chmod(home, 0o700)
        settings = os.path.join(home, ".claude", "settings.json")
        _write(settings, '{"model": "' + _MARKER + '"}', 0o600)
        return root.name, home, settings

    def _read(self, path, home):
        return open_guard.read_regular_file(path, root=home, home=home, max_bytes=65536)

    def _refused(self, path, home):
        try:
            data = self._read(path, home)
        except open_guard.GateRefused as refused:
            return refused.reason
        self.assertFalse(_MARKER.encode() in data, "the gate returned the file's bytes")
        self.fail("the gate returned the file")


class NoFollowMissingTests(_Layout, unittest.TestCase):
    """Where ``O_NOFOLLOW`` is missing the gate refuses before any open (simulated Windows has none)."""

    def _without_nofollow(self):
        if hasattr(os, "O_NOFOLLOW"):
            saved = os.O_NOFOLLOW
            del os.O_NOFOLLOW
            self.addCleanup(setattr, os, "O_NOFOLLOW", saved)

    def test_a_missing_o_nofollow_refuses_before_any_open(self):
        _root, home, settings = self._layout()
        self._without_nofollow()
        _watch_opens()
        before = len(_OPENED)
        reason = self._refused(settings, home)
        self.assertEqual(reason, "platform_unsupported")
        opened = _names(_OPENED[before:])
        self.assertFalse(any(name.endswith("settings.json") for name in opened), "the file was opened")


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "geteuid"), "the gate refuses here before the open")
class OwnerAndModeTests(_Layout, unittest.TestCase):
    def test_a_group_or_world_writable_file_is_refused_before_the_open(self):
        _root, home, settings = self._layout()
        _watch_opens()
        for mode in (0o620, 0o602, 0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(settings, mode)
                before = len(_OPENED)
                self.assertEqual(self._refused(settings, home), "writable")
                opened = _names(_OPENED[before:])
                self.assertFalse(any(name.endswith("settings.json") for name in opened), "the file was opened")
        os.chmod(settings, 0o644)
        self.assertTrue(_MARKER.encode() in self._read(settings, home), "a plain file was not read")

    def test_a_file_of_another_owner_is_refused_before_the_open(self):
        _root, home, settings = self._layout()
        _watch_opens()
        before = len(_OPENED)
        other = os.geteuid() + 1
        with mock.patch.object(open_guard.os, "geteuid", return_value=other):
            self.assertEqual(self._refused(settings, home), "not_owner")
        opened = _names(_OPENED[before:])
        self.assertFalse(any(name.endswith("settings.json") for name in opened), "the file was opened")

    def test_owner_and_mode_are_checked_again_on_the_held_descriptor(self):
        _root, home, settings = self._layout()
        real_fstat = os.fstat

        class _Changed:
            def __init__(self, info, field, value):
                self._info, self._field, self._value = info, field, value

            def __getattr__(self, name):
                if name == self._field:
                    return self._value
                return getattr(self._info, name)

        for field, value, reason in (
            ("st_mode", stat.S_IFREG | 0o622, "writable"),
            ("st_uid", os.geteuid() + 1, "not_owner"),
        ):
            def fstat(fd, field=field, value=value):
                info = real_fstat(fd)
                if stat.S_ISREG(info.st_mode):
                    return _Changed(info, field, value)
                return info

            with self.subTest(field=field), mock.patch.object(open_guard.os, "fstat", fstat):
                self.assertEqual(self._refused(settings, home), reason)


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"), "the gate refuses here before the open")
class WalkedOpenTests(_Layout, unittest.TestCase):
    def test_an_ancestor_swapped_after_the_real_path_check_is_not_followed(self):
        # The home's .claude is a symlink out of the home at lstat and at the open, and a real
        # folder inside the home only while the real path is taken. Opening the path by name
        # followed the symlink to the outside file, whose inode lstat had already seen.
        root, home, _settings = self._layout()
        outside = os.path.join(root, "outside")
        _write(os.path.join(outside, "settings.json"), '{"model": "' + _MARKER + '"}', 0o600)
        link = os.path.join(home, ".claude")
        benign = os.path.join(home, ".claude-benign")
        parked = os.path.join(home, ".claude-parked")
        os.rename(link, benign)
        with open(os.path.join(benign, "settings.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        os.symlink(outside, link)
        path = os.path.join(link, "settings.json")
        real_realpath = os.path.realpath
        swaps = []

        def realpath(target, *args, **kwargs):
            if target != path:
                return real_realpath(target, *args, **kwargs)
            os.rename(link, parked)
            os.rename(benign, link)
            try:
                return real_realpath(target, *args, **kwargs)
            finally:
                os.rename(link, benign)
                os.rename(parked, link)
                swaps.append(1)

        with mock.patch.object(open_guard.os.path, "realpath", realpath):
            reason = self._refused(path, home)
        self.assertTrue(swaps, "the real path was not taken")
        self.assertIn(reason, {"unreadable", "aliased"})

    def test_a_plain_file_is_still_read(self):
        _root, home, settings = self._layout()
        self.assertTrue(_MARKER.encode() in self._read(settings, home), "a plain file was not read")


if __name__ == "__main__":
    unittest.main()
