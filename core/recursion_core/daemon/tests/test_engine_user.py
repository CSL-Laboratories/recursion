"""Offline tests for the B9 engine-user invariants (Lucas's Nov 8 call).

Structural only: fake `loginctl`/`systemctl` results and temp socket paths. No
systemd, logind, host, credential store or provider is touched. Host evidence for
B9 comes only from the founder rerun on gs-worker-nbg.
"""
import builtins
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon.engine_user import (
    SIGNIN_STORES, SPAWN_PATH, EngineManagerPresent, SessionLaunchRefused,
    check_engine_no_user_manager, refuse_session_launch, signin_without_session_bus)
from recursion_core.daemon.tests.test_daemon import Boot, FakeManager

ENGINE, UID, OPERATOR = 'csl-engine', 1001, 1000


def result(rc, out='', err=''):
    return subprocess.CompletedProcess([], rc, out, err)


class Probe:
    """Fake loginctl/systemctl that records argv and never mutates anything."""
    def __init__(self, linger, active):
        self.answers = {'loginctl': linger, 'systemctl': active}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return self.answers[argv[0]]


class EngineManagerProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, probe, **kw):
        return check_engine_no_user_manager(ENGINE, UID, operator_uid=OPERATOR, call=probe,
                                            runtime_root=self.runtime, **kw)

    def test_linger_no_inactive_no_socket_passes(self):
        probe = Probe(result(0, 'no\n'), result(3, 'inactive\n'))
        self.assertEqual(self.check(probe), {'linger': 'no', 'user_manager': 'inactive',
                                             'manager_socket': 'absent'})

    def test_no_such_user_session_passes(self):
        err = 'Failed to get user: User ID 1001 is not logged in or lingering'
        probe = Probe(result(1, '', err), result(3, 'inactive'))
        self.assertEqual(self.check(probe)['linger'], 'no such user session')

    def test_probe_is_read_only(self):
        probe = Probe(result(0, 'no'), result(3, 'inactive'))
        self.check(probe)
        self.assertEqual(probe.calls, [
            ['loginctl', 'show-user', ENGINE, '--property=Linger', '--value'],
            ['systemctl', 'is-active', f'user@{UID}.service']])
        flat = ' '.join(' '.join(c) for c in probe.calls)
        for verb in ('enable-linger', 'disable-linger', 'terminate', 'stop', 'start',
                     'mask', 'kill'):
            self.assertNotIn(verb, flat.split())

    def test_engine_linger_yes_refused(self):
        with self.assertRaisesRegex(EngineManagerPresent, 'Linger'):
            self.check(Probe(result(0, 'yes'), result(3, 'inactive')))

    def test_unexpected_loginctl_failure_refused(self):
        for linger in (result(1, '', 'Failed to connect to bus'), result(0, ''),
                       result(0, 'unknown'), result(127, '', 'not found')):
            with self.subTest(linger=linger), self.assertRaises(EngineManagerPresent):
                self.check(Probe(linger, result(3, 'inactive')))

    def test_user_manager_not_inactive_refused(self):
        for active in (result(0, 'active'), result(3, 'activating'), result(3, 'failed'),
                       result(3, 'deactivating'), result(4, ''), result(0, 'inactive')):
            with self.subTest(active=active), self.assertRaises(EngineManagerPresent):
                self.check(Probe(result(0, 'no'), active))

    def test_manager_socket_present_refused(self):
        sock = self.runtime / str(UID) / 'systemd' / 'private'
        sock.parent.mkdir(parents=True)
        sock.touch()
        with self.assertRaisesRegex(EngineManagerPresent, 'private'):
            self.check(Probe(result(0, 'no'), result(3, 'inactive')))

    def test_dangling_socket_symlink_refused(self):
        sock = self.runtime / str(UID) / 'systemd' / 'private'
        sock.parent.mkdir(parents=True)
        sock.symlink_to(self.runtime / 'nowhere')
        with self.assertRaises(EngineManagerPresent):
            self.check(Probe(result(0, 'no'), result(3, 'inactive')))

    def test_probe_that_cannot_run_refused(self):
        def boom(argv, **kw):
            raise FileNotFoundError(argv[0])
        with self.assertRaisesRegex(EngineManagerPresent, 'could not run'):
            self.check(boom)

    def test_engine_uid_equal_to_operator_refused(self):
        with self.assertRaisesRegex(EngineManagerPresent, 'operator'):
            check_engine_no_user_manager(ENGINE, OPERATOR, operator_uid=OPERATOR,
                                         call=Probe(result(0, 'no'), result(3, 'inactive')),
                                         runtime_root=self.runtime)

    def test_invalid_user_or_uid_refused(self):
        probe = Probe(result(0, 'no'), result(3, 'inactive'))
        for user, uid in (('-p', UID), ('a b', UID), ('', UID), (ENGINE, 0), (ENGINE, -1),
                          (ENGINE, True), (ENGINE, '1001')):
            with self.subTest(user=user, uid=uid), self.assertRaises(ValueError):
                check_engine_no_user_manager(user, uid, operator_uid=OPERATOR, call=probe,
                                             runtime_root=self.runtime)
        self.assertEqual(probe.calls, [])


class SessionLaunchTests(unittest.TestCase):
    def test_pam_session_wrappers_refused(self):
        for argv in (['su', '-', ENGINE, '-c', 'true'], ['/usr/bin/su', '-l', ENGINE],
                     ['runuser', '-l', ENGINE, '-c', 'true'], ['runuser', '-u', ENGINE, 'x'],
                     ['/usr/sbin/runuser', '--login', ENGINE], ['sudo', '-iu', ENGINE],
                     ['login', '-f', ENGINE], ['machinectl', 'shell', f'{ENGINE}@'],
                     ['pkexec', 'x'], ['doas', 'x'], ['ssh', 'localhost'], ['run0', 'x']):
            with self.subTest(argv=argv), self.assertRaises(SessionLaunchRefused):
                refuse_session_launch(argv)

    def test_systemd_run_pamname_refused(self):
        for argv in (['systemd-run', '-p', 'PAMName=login', '--', '/x'],
                     ['systemd-run', '--property=PAMName=systemd-user', '--', '/x'],
                     ['systemd-run', '-pPAMName=login', '/x']):
            with self.subTest(argv=argv), self.assertRaises(SessionLaunchRefused):
                refuse_session_launch(argv)
        refuse_session_launch(['systemd-run', '--', '/x', 'PAMName=is-just-an-arg'])

    def test_plain_command_allowed(self):
        refuse_session_launch(['/opt/engine/bin/claude', '--print'])
        refuse_session_launch(['/fake'])

    def test_malformed_argv_refused(self):
        for argv in ([], None, [b'su'], ['/x', 3]):
            with self.subTest(argv=argv), self.assertRaises(SessionLaunchRefused):
                refuse_session_launch(argv)

    def test_same_uid_operator_runs_are_not_filtered(self):
        # The guard is for the later cross-uid engine spawn; it does not restrict same-uid operator
        # commands, and the daemon does not claim to enforce engine-manager absence.
        with tempfile.TemporaryDirectory() as temp:
            registry = Registry(Path(temp) / 'state')
            manager = FakeManager()
            daemon = Daemon(registry, manager, Boot())
            try:
                self.assertEqual(daemon.start('one', ['ssh', 'build-host', 'make']).state, 'running')
                self.assertEqual(manager.starts, ['recursion-run-one.service'])
            finally:
                registry.close()
        import recursion_core.daemon as pkg
        source = Path(pkg.__file__).read_text()
        self.assertNotIn('refuse_session_launch', source)
        self.assertNotIn('check_engine_no_user_manager', source)

    def test_recorded_spawn_path_is_same_uid_without_pam(self):
        self.assertIn('systemd-run --user', SPAWN_PATH['path'])
        self.assertIn('CSL-734', SPAWN_PATH['cross_uid_owner'])
        for word in ('su ', 'runuser -l', 'PAMName=login'):
            self.assertNotIn(word, SPAWN_PATH['path'])


class StopScopeTests(unittest.TestCase):
    def test_stop_ends_just_one_run(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Registry(Path(temp) / 'state')
            manager = FakeManager()
            daemon = Daemon(registry, manager, Boot())
            try:
                for run in ('a', 'b', 'c'):
                    daemon.start(run, ['/fake'])
                self.assertEqual(daemon.stop('b').state, 'terminated')
                self.assertEqual(manager.stops, ['recursion-run-b.service'])
                states = {r.run_id: r.state for r in daemon.sweep()}
                self.assertEqual(states, {'a': 'running', 'b': 'terminated', 'c': 'running'})
                # The sweep may re-stop b's leftover unit; it never touches a or c.
                self.assertEqual(set(manager.stops), {'recursion-run-b.service'})
                self.assertEqual(manager.units['recursion-run-a.service'], 'running')
                self.assertEqual(manager.units['recursion-run-c.service'], 'running')
            finally:
                registry.close()


class SignInWithoutBusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_never_reports_signed_in_success(self):
        report = signin_without_session_bus({'HOME': '/home/e', 'PATH': '/usr/bin'}, UID,
                                            self.runtime)
        self.assertEqual(report['verdict'], 'pending')
        for name, row in report['engines'].items():
            self.assertIn('pending founder-attended', row['signed_in_run'])
            self.assertNotIn('pass', row['signed_in_run'].lower())

    def test_never_opens_any_file(self):
        def refuse(*a, **k):
            raise AssertionError('credential store or file opened')
        with patch.object(builtins, 'open', refuse), patch.object(os, 'open', refuse):
            signin_without_session_bus({'HOME': '/home/e'}, UID, self.runtime)

    def test_store_paths_only_with_repository_evidence(self):
        self.assertEqual(SIGNIN_STORES['claude_code']['store_path'], 'unconfirmed')
        self.assertEqual(SIGNIN_STORES['claude_code']['without_session_bus'], 'unconfirmed')
        self.assertIn('CSL-712', SIGNIN_STORES['codex']['store_evidence'])
        self.assertTrue(SIGNIN_STORES['codex']['without_session_bus'].startswith('unconfirmed'))
        for row in SIGNIN_STORES.values():
            self.assertTrue(row['store_path'] == 'unconfirmed' or row['store_evidence'] != '')

    def test_session_bus_in_engine_env_refused(self):
        for name in ('DBUS_SESSION_BUS_ADDRESS', 'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE'):
            with self.subTest(name=name), self.assertRaisesRegex(EngineManagerPresent, name):
                signin_without_session_bus({name: 'unix:path=/run/user/1001/bus'}, UID,
                                           self.runtime)

    def test_reachable_bus_socket_refused(self):
        bus = self.runtime / str(UID) / 'bus'
        bus.parent.mkdir(parents=True)
        bus.touch()
        with self.assertRaisesRegex(EngineManagerPresent, 'bus'):
            signin_without_session_bus({}, UID, self.runtime)

    def test_xdg_runtime_dir_bus_refused(self):
        other = self.runtime / 'other'
        other.mkdir()
        (other / 'bus').touch()
        with self.assertRaises(EngineManagerPresent):
            signin_without_session_bus({'XDG_RUNTIME_DIR': str(other)}, UID, self.runtime)


if __name__ == '__main__':
    unittest.main()
