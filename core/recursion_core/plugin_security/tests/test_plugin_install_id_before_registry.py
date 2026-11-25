"""An install refuses a reserved or malformed id, and a version that is not text, before it reads the registry.

The id and the version come from the caller alone. Their checks need nothing from the registry or from a git
source, so they run first: a refused id costs no read of a registry that may be hostile, huge or endless, and
a registry that is broken cannot hide the true reason the id was refused. Each case below replaces
``check_registry`` and the git fetch with stand-ins that record every call and raise when called, and records
every ``os.open`` of a path under the registry root. A valid id still reads the registry first, and the other
commands that take an id refuse a malformed one before any read as well.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest import mock

from recursion_core.plugin_security import install as inst
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, ProfileCase

# Each kind of id that can never be a plugin id: (what it is, the id).
MALFORMED = (
    ("empty", ""),
    ("one letter", "a"),
    ("upper case", "Fixture.unsigned"),
    ("a leading digit", "1fixture"),
    ("a leading dot", ".fixture"),
    ("a dot", "."),
    ("two dots", ".."),
    ("a slash", "fixture/unsigned"),
    ("a backslash", "fixture\\unsigned"),
    ("a space", "fixture unsigned"),
    ("the host's own name", "host"),
    ("longer than an id may be", "f" * 65),
    ("not text", None),
    ("a number", 5),
    ("bytes", b"fixture.unsigned"),
    ("a list", ["fixture.unsigned"]),
)
NOT_TEXT_VERSIONS = (("a number", 1), ("bytes", b"1.0.0"), ("a list", ["1.0.0"]))


class RegistryRead(RuntimeError):
    """Raised by the stand-ins: any call of them is a read the install did not need."""


@contextlib.contextmanager
def registry_watched(root):
    """Stand in for the registry check and the git fetch; record every call and every open under ``root``."""
    seen = {"check_registry": [], "fetch": [], "opens": []}
    real_open = os.open
    prefix = os.fspath(root)

    def hostile_check(where, *args, **kwargs):
        seen["check_registry"].append(os.fspath(where))
        raise RegistryRead("the registry was read")

    def hostile_fetch(*args, **kwargs):
        seen["fetch"].append(args)
        raise RegistryRead("a git source was read")

    def recording_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(prefix):
            seen["opens"].append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    with mock.patch.object(inst, "check_registry", hostile_check), \
            mock.patch.object(inst, "fetch_plugin_source", hostile_fetch), \
            mock.patch("os.open", recording_open):
        yield seen


@unittest.skipUnless(LINUX, LINUX_ONLY)
class IdBeforeRegistryTests(ProfileCase):
    def refused_unread(self, plugin_id, code, **kwargs):
        with registry_watched(self.registry) as seen:
            with self.assertRaises(inst.InstallRefused) as caught:
                inst.install_plugin(self.config, self.registry, plugin_id, state=self.state, **kwargs)
        self.assertEqual(code, caught.exception.code, plugin_id)
        self.assertEqual([], seen["check_registry"], plugin_id)
        self.assertEqual([], seen["fetch"], plugin_id)
        self.assertEqual([], seen["opens"], plugin_id)
        self.assertFalse(self.config.exists(), plugin_id)
        self.assertFalse(self.state.exists(), plugin_id)
        return caught.exception

    def test_a_reserved_id_is_refused_with_no_read_of_the_registry(self):
        self.write_registry()
        for reserved in sorted(inst._STORE_RESERVED_IDS):
            with self.subTest(reserved=reserved):
                self.refused_unread(reserved, "plugin_id_reserved")

    def test_a_reserved_id_with_a_git_source_reads_neither(self):
        self.write_registry()
        self.refused_unread("install.json", "plugin_id_reserved", git_repository=str(self.tmp / "bare.git"))

    def test_the_reserved_refusal_says_what_stopped_it_and_what_to_do(self):
        refusal = self.refused_unread("install.json", "plugin_id_reserved")
        self.assertTrue(refusal.show_detail)
        self.assertEqual(
            "The name install.json is kept for the install's own files and is never a plugin id. "
            "Pick another plugin id.", refusal.detail)

    def test_each_kind_of_malformed_id_is_refused_with_no_read_of_the_registry(self):
        self.write_registry()
        for kind, plugin_id in MALFORMED:
            with self.subTest(kind=kind):
                self.refused_unread(plugin_id, "plugin_not_in_registry")
                self.refused_unread(plugin_id, "plugin_not_in_registry", git_repository=str(self.tmp / "bare.git"))

    def test_a_version_that_is_not_text_is_refused_with_no_read_of_the_registry(self):
        self.write_registry()
        for kind, version in NOT_TEXT_VERSIONS:
            with self.subTest(kind=kind):
                self.refused_unread("fixture.unsigned", "release_not_in_registry", version=version)

    def test_with_no_registry_at_all_the_reserved_id_is_still_the_reason(self):
        # Both are wrong: the id is reserved and the registry root is not there. The id's code wins.
        missing = self.tmp / "no-registry"
        with self.assertRaises(inst.InstallRefused) as caught:
            inst.install_plugin(self.config, missing, "install.json", state=self.state)
        self.assertEqual("plugin_id_reserved", caught.exception.code)
        with self.assertRaises(inst.InstallRefused) as caught:
            inst.install_plugin(self.config, missing, "Not An Id", state=self.state)
        self.assertEqual("plugin_not_in_registry", caught.exception.code)
        self.assertFalse(self.config.exists())

    def test_a_valid_id_still_reads_the_registry_first_and_refuses_as_before_when_it_is_bad(self):
        calls = []
        real = inst.check_registry

        def counted(root):
            calls.append(os.fspath(root))
            return real(root)

        with mock.patch.object(inst, "check_registry", counted):
            with self.assertRaises(inst.InstallRefused) as caught:
                inst.install_plugin(self.config, self.tmp / "no-registry", "fixture.unsigned", state=self.state)
        self.assertEqual("root_unavailable", caught.exception.code)
        self.assertEqual(1, len(calls))
        self.assertFalse(self.config.exists())

    def test_a_valid_id_on_a_good_registry_installs_as_before(self):
        self.write_registry()
        result = inst.install_plugin(self.config, self.registry, "fixture.unsigned", state=self.state)
        self.assertEqual(1, result["generation"])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class OtherCommandsBeforeAnyReadTests(ProfileCase):
    """The commands that take an id refuse a malformed one before the lock, the profile or the registry."""

    def test_each_command_refuses_a_malformed_id_before_any_read(self):
        commands = (
            ("remove", lambda pid: inst.remove_plugin(self.config, pid, state=self.state), inst.InstallRefused),
            ("enable", lambda pid: inst.enable_plugin(self.config, pid, state=self.state), inst.InstallRefused),
            ("disable", lambda pid: inst.disable_plugin(self.config, pid, state=self.state), inst.InstallRefused),
            ("unpin", lambda pid: inst.unpin_plugin(self.config, pid, state=self.state), inst.NothingChanged),
            ("quarantine accept", lambda pid: inst.quarantine_accept(self.config, pid, state=self.state),
             inst.NothingChanged),
            ("quarantine drop", lambda pid: inst.quarantine_drop(self.config, pid, state=self.state),
             inst.NothingChanged),
        )
        for name, call, refusal in commands:
            for kind, plugin_id in MALFORMED + (("reserved", "install.json"),):
                with self.subTest(command=name, kind=kind):
                    with registry_watched(self.registry) as seen, \
                            mock.patch.object(inst, "_lock", side_effect=RegistryRead("the lock was taken")):
                        with self.assertRaises(refusal):
                            call(plugin_id)
                    self.assertEqual([], seen["check_registry"])
                    self.assertEqual([], seen["fetch"])
                    self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
