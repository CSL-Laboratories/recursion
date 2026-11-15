"""Which backend a host offers, and the helper programs' environment and PATH."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.system import Completed, System, detect
from recursion_core.keys.tests.support import MARKER, FakeSystem, busctl, needs_posix


def dbus_send(owner=False, activatable=False, kwallet=False):
    """A stand-in ``dbus-send --print-reply``: ``ListNames`` and ``ListActivatableNames`` as it prints them."""
    def run(argv, input):
        names = ['org.freedesktop.DBus']
        listing = argv[-1].endswith('.ListNames')
        if (listing and owner) or (not listing and activatable):
            names.append('org.freedesktop.secrets')
        if listing and kwallet:
            names.append('org.kde.kwalletd5')
        body = ''.join(f'      string "{name}"\n' for name in names)
        return Completed(0, f'method return time=1 sender=x\n   array [\n{body}   ]\n'.encode(), b'')
    return run


def failing(argv, input):
    return Completed(1, b'', b'Failed to connect to bus\n')


class DetectTest(unittest.TestCase):
    def detect(self, **kw):
        return detect(FakeSystem(**kw))

    def test_macos_and_windows_always_have_a_keychain(self) -> None:
        self.assertEqual(self.detect(platform='darwin'), 'macos_keychain')
        self.assertEqual(self.detect(platform='win32'), 'windows_credential')

    def test_no_session_bus_means_the_owner_file(self) -> None:
        self.assertEqual(self.detect(tools={'secret-tool': failing, 'busctl': busctl(owner=True)}), 'owner_file')

    def test_a_secret_service_on_the_bus_is_used(self) -> None:
        bus = 'unix:path=/fake/bus'
        for tools in ({'busctl': busctl(owner=True)}, {'busctl': busctl(activatable=True)},
                      {'busctl': failing, 'dbus-send': dbus_send(owner=True)},
                      {'dbus-send': dbus_send(activatable=True)}):
            with self.subTest(tools=sorted(tools)):
                self.assertEqual(self.detect(tools={**tools, 'secret-tool': failing}, bus=bus), 'secret_service')

    def test_a_bus_without_a_secret_service_means_the_owner_file(self) -> None:
        bus = 'unix:path=/fake/bus'
        self.assertEqual(self.detect(tools={'busctl': busctl()}, bus=bus), 'owner_file')
        self.assertEqual(self.detect(tools={'dbus-send': dbus_send()}, bus=bus), 'owner_file')

    def test_kwallet_without_its_secret_service_interface_is_refused_not_answered_with_the_file(self) -> None:
        bus = 'unix:path=/fake/bus'
        for tools in ({'busctl': busctl(kwallet=True)}, {'dbus-send': dbus_send(kwallet=True)}):
            with self.subTest(tools=sorted(tools)), self.assertRaises(KeyStoreError) as caught:
                self.detect(tools={**tools, 'secret-tool': failing}, bus=bus)
            self.assertEqual(caught.exception.code, 'keychain_unsupported')
        self.assertEqual(self.detect(tools={'busctl': busctl(owner=True, kwallet=True), 'secret-tool': failing},
                                     bus=bus), 'secret_service')

    def test_a_listing_that_does_not_add_up_is_refused(self) -> None:
        def wrong_count(argv, input):
            return Completed(0, b'as 3 "org.freedesktop.DBus" "org.freedesktop.secrets"', b'')

        with self.assertRaises(KeyStoreError) as caught:
            self.detect(tools={'busctl': wrong_count, 'secret-tool': failing}, bus='unix:path=/fake/bus')
        self.assertEqual(caught.exception.code, 'keychain_undetermined')

    def test_a_keychain_without_secret_tool_is_refused_not_answered_with_the_file(self) -> None:
        with self.assertRaises(KeyStoreError) as caught:
            self.detect(tools={'busctl': busctl(owner=True)}, bus='unix:path=/fake/bus')
        self.assertEqual(caught.exception.code, 'keychain_tool_missing')

    def test_a_bus_nobody_can_ask_is_refused(self) -> None:
        for tools in ({}, {'busctl': failing}, {'busctl': failing, 'dbus-send': failing},
                      {'busctl': lambda a, i: Completed(0, b'something else\n', b'')}):
            with self.subTest(tools=sorted(tools)):
                with self.assertRaises(KeyStoreError) as caught:
                    self.detect(tools={**tools, 'secret-tool': failing}, bus='unix:path=/fake/bus')
                self.assertEqual(caught.exception.code, 'keychain_undetermined')


@needs_posix
class HelperEnvironmentTest(unittest.TestCase):
    def test_helpers_get_a_small_environment_with_no_key_in_it(self) -> None:
        environ = {'PATH': '/usr/bin:relative/bin::/bin', 'HOME': '/home/op', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/b',
                   'DISPLAY': ':0', 'ANTHROPIC_API_KEY': MARKER, 'CODEX_API_KEY': MARKER, 'LD_PRELOAD': '/evil.so',
                   'PYTHONPATH': '/x', 'LANG': 'de_DE.UTF-8'}
        env = System(environ, platform='linux').helper_env()
        self.assertEqual(env, {'PATH': '/usr/bin:/bin', 'HOME': '/home/op', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/b',
                               'DISPLAY': ':0', 'LC_ALL': 'C', 'LANG': 'C'})

    def test_the_runtime_bus_socket_is_the_session_bus_when_no_address_is_set(self) -> None:
        import socket

        with tempfile.TemporaryDirectory() as runtime:
            system = System({'XDG_RUNTIME_DIR': runtime, 'PATH': '/bin'}, platform='linux')
            self.assertIsNone(system.session_bus())
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.bind(os.path.join(runtime, 'bus'))
                self.assertEqual(system.session_bus(), f'unix:path={runtime}/bus')
                self.assertEqual(system.helper_env()['DBUS_SESSION_BUS_ADDRESS'], f'unix:path={runtime}/bus')
            finally:
                sock.close()

    def test_a_program_in_a_relative_path_entry_is_never_found(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            local = Path(work, 'bin')
            local.mkdir()
            tool = local / 'secret-tool'
            tool.write_text('#!/bin/sh\nexit 0\n')
            tool.chmod(0o700)
            old = os.getcwd()
            os.chdir(work)
            try:
                self.assertIsNone(System({'PATH': 'bin:.:'}, platform='linux').which('secret-tool'))
                self.assertEqual(System({'PATH': str(local)}, platform='linux').which('secret-tool'), str(tool))
            finally:
                os.chdir(old)

    def test_a_helper_runs_from_the_root_folder_with_its_own_environment(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            probe = Path(work, 'probe')
            probe.write_text('#!/bin/sh\npwd\nenv\n')
            probe.chmod(0o700)
            done = System({'PATH': '/usr/bin:/bin', 'HOME': '/h', 'CODEX_API_KEY': MARKER}, platform='linux').run(
                [str(probe)], timeout=60)
        lines = done.stdout.decode().splitlines()
        self.assertEqual(lines[0], '/')
        self.assertNotIn(MARKER, done.stdout.decode())
        self.assertIn('LC_ALL=C', lines)
