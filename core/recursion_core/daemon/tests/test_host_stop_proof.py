"""A host the daemon ends stays ended.

A host unit carries only the properties the launcher's allow list names; the launcher's own are written after the
caller's; and Stop records a host ended only when the system shows its unit ended and not about to start again.
Stand-in user managers only: no unit is started and no process but this test's threads.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import unittest
import unittest.mock

LINUX = sys.platform.startswith('linux')

if LINUX:
    from recursion_core.daemon import host_units, launcher
    from recursion_core.daemon.tests import test_host_start as hs
    from recursion_core.plugin_security import host_channel as hc
    _Folder = hs._Folder
else:  # the launcher refuses off Linux (test_windows_refusal.py)
    _Folder = unittest.TestCase

UNIT = 'recursion-host-0123456789abcdef.service'
RUNNING = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running', 'Job': ''}
GONE = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'Job': ''}
RESTARTING = {'LoadState': 'loaded', 'ActiveState': 'activating', 'SubState': 'auto-restart', 'Job': ''}
QUEUED = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead', 'Job': '7 start'}
FAILED_KEPT = {'LoadState': 'loaded', 'ActiveState': 'failed', 'SubState': 'failed', 'Job': ''}
# Every kind of property by which the system could start a host again, a name no systemd knows today, and names
# written in another case or with a space.
PLANTED = ('Restart=always', 'Restart=on-failure', 'RestartSec=1', 'RestartSteps=3', 'RestartMaxDelaySec=5',
           'RestartForceExitStatus=1', 'RestartPreventExitStatus=1', 'OnFailure=x.service', 'OnSuccess=x.service',
           'Upholds=x.service', 'Wants=x.service', 'Requires=x.service', 'Requisite=x.service', 'BindsTo=x.service',
           'PartOf=x.service', 'Sockets=x.socket', 'NoPropertyAnySystemdKnowsToday=1', 'restart=always',
           ' Restart=always', 'Restart =always', 'MemoryMax', 'memorymax=1G')
# The launcher's own, last, as the start must write them.
OWN_LAST = ['--property=KillMode=control-group', '--property=Delegate=yes', '--property=KillSignal=SIGINT',
            '--property=TimeoutStopSec=2s', '--property=SendSIGKILL=yes', '--property=RemainAfterExit=no',
            '--property=Restart=no']
SHORT = 0.3   # the Stop bound of the tests that wait it out (a test figure)
POLL = 0.02   # the poll of those tests (a test figure)
SLACK = 2.0   # how much past the bound a Stop of these stand-ins may take (a test figure)


def piped_command(properties):
    manager = launcher.EngineUnitManager()
    manager._verbatim_argv = True
    one, other = socket.socketpair()
    try:
        with unittest.mock.patch.object(launcher.subprocess, 'Popen') as popen:
            manager.start_piped(UNIT, ['python'], one, properties=properties)
        (command,), _ = popen.call_args
        return command[:command.index('--')]
    finally:
        one.close()
        other.close()


class FakeSystem:
    """A stand-in user manager. Its host is a thread on a copy of the host's end (it says hello unless told not to);
    the cgroup kill hangs that end up. What the system shows of a unit: ``running`` before the kill, ``after_kill``
    after it, ``after_stop`` after the system's own stop; ``states`` names a unit's state outright."""

    def __init__(self, test, *, after_kill=GONE, after_stop=GONE, hello=True):
        self.after_kill, self.after_stop, self.hello = after_kill, after_stop, hello
        self.killed, self.stops, self.reads, self.ends, self.started = [], [], [], [], []
        self.states, self.listed, self.fail_reads = {}, [], False
        test.addCleanup(self.hang_up_all)

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= hs.HOSTS_PER_TEST:
            raise AssertionError('this test started more hosts than its count allows')
        self.started.append(unit)
        end = channel.dup()
        self.ends.append((unit, end))

        def host():
            try:
                if self.hello:
                    end.sendall(hc.encode(hs.HELLO))
                while end.recv(1 << 16):
                    pass
            except OSError:
                pass

        threading.Thread(target=host, daemon=True).start()
        return hs.StandInProcess()

    def unit_identity(self, unit):
        return '/stand-in.slice/' + unit, 0

    def kill_cgroup(self, group, unit):
        self.killed.append(unit)
        for name, end in self.ends:
            if name == unit:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        return True

    def unit_state(self, unit, timeout_s):
        self.reads.append(unit)
        if self.fail_reads:
            raise OSError('the read failed')
        if unit in self.states:
            return dict(self.states[unit])
        if unit in self.stops:
            return dict(self.after_stop)
        return dict(self.after_kill if unit in self.killed else RUNNING)

    def stop_unit(self, unit, timeout_s):
        self.stops.append(unit)

    def listed_units(self, pattern, timeout_s):
        return list(self.listed)

    def hang_up_all(self):
        for _, end in self.ends:
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            end.close()


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class AHostUnitTakesOnlyTheAllowedProperties(_Folder):
    def test_every_property_by_which_the_system_could_start_a_host_again_is_refused_before_any_unit(self):
        manager = launcher.EngineUnitManager()
        manager._verbatim_argv = True
        one, other = socket.socketpair()
        self.addCleanup(one.close)
        self.addCleanup(other.close)
        for item in PLANTED:
            name = item.partition('=')[0]
            with self.subTest(property=item):
                with unittest.mock.patch.object(launcher.subprocess, 'Popen') as popen:
                    with self.assertRaises(launcher.PipedUnitPropertyRefused) as caught:
                        manager.start_piped(UNIT, ['python'], one, properties=(item,))
                popen.assert_not_called()
                self.assertEqual(caught.exception.name, name)
                with self.assertRaises(launcher.PipedUnitPropertyRefused):
                    host_units.PluginHostUnits(FakeSystem(self), self.state, hs.StandInCore(), properties=(item,))

    def test_the_refusal_says_what_why_and_the_step(self):
        with self.assertRaises(launcher.PipedUnitPropertyRefused) as caught:
            launcher.check_piped_properties(('OnFailure=x.service',))
        text = str(caught.exception)
        self.assertIn('a plugin host unit cannot carry the property OnFailure: ', text)
        self.assertIn('only the properties the launcher lists may be given to a plugin host, so that a host the '
                      'daemon ends stays ended and nothing else can start it again', text)
        self.assertIn('Take OnFailure out of the properties given to the plugin hosts, or ask for OnFailure to be '
                      'added', text)
        with self.assertRaises(launcher.PipedUnitPropertyRefused) as caught:
            launcher.check_piped_properties(('Restart=always',))
        self.assertIn('the system starts the host again after Stop has killed it', str(caught.exception))
        self.assertNotIn('ask for Restart to be added', str(caught.exception))

    def test_each_property_on_the_list_reaches_the_unit_and_has_its_reason(self):
        names = [name for name, _ in launcher.PIPED_UNIT_ALLOWED]
        self.assertEqual(len(names), len(set(names)))
        for name, reason in launcher.PIPED_UNIT_ALLOWED:
            with self.subTest(name=name):
                self.assertTrue(reason.strip())
                self.assertNotIn(name, launcher.PIPED_UNIT_OWN)
                self.assertIn(f'--property={name}=1', piped_command((f'{name}=1',)))


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class TheLaunchersOwnPropertiesComeLast(unittest.TestCase):
    def test_a_property_that_slipped_past_the_check_still_loses(self):
        slipped = ('Restart=always', 'KillMode=process', 'RemainAfterExit=yes', 'TimeoutStopSec=infinity')
        with unittest.mock.patch.object(launcher, 'check_piped_properties', lambda items: tuple(items)):
            head = piped_command(slipped)
        props = [item for item in head if item.startswith('--property=')]
        self.assertEqual(props[-len(OWN_LAST):], OWN_LAST)
        self.assertEqual(list(launcher.PIPED_UNIT_LAST), [item[len('--property='):] for item in OWN_LAST])
        self.assertLess(head.index('--property=Restart=always'), head.index('--property=Restart=no'))


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class StopProvesTheHostEnded(_Folder):
    def units(self, system, **options):
        options.setdefault('stop_confirm_s', SHORT)
        options.setdefault('stop_confirm_poll_s', POLL)
        units = host_units.PluginHostUnits(system, self.state, hs.StandInCore(), python='/stand-in/python', **options)
        self.addCleanup(units.close)
        return units

    def test_a_host_is_recorded_ended_only_when_the_system_shows_its_unit_gone(self):
        system = FakeSystem(self)
        units = host_units.PluginHostUnits(system, self.state, hs.StandInCore(), python='/stand-in/python')
        self.addCleanup(units.close)
        host = units.start('stand.in', *hs.package(hs.POLITE))
        units.stop('stand.in')
        self.assertEqual(system.killed, [host.unit])
        self.assertIn(host.unit, system.reads)  # read after the kill
        self.assertEqual(system.stops, [])
        self.assertEqual(host.life, 'ended')
        shown = units.status()
        self.assertEqual(shown['plugins']['stand.in']['life'], 'ended')
        self.assertEqual(shown['stop_unconfirmed'], [])
        self.assertEqual(shown['bounds']['plugin_host.stop_confirm_s'], host_units.STOP_CONFIRM_S)

    def assert_unconfirmed(self, after_kill):
        system = FakeSystem(self, after_kill=after_kill, after_stop=after_kill)
        units = self.units(system)
        host = units.start('stand.in', *hs.package(hs.POLITE))
        began = time.monotonic()
        with self.assertLogs(host_units.log, logging.ERROR) as logged:
            units.stop('stand.in')
        took = time.monotonic() - began
        self.assertGreaterEqual(took, SHORT)
        self.assertLess(took, SHORT + SLACK)
        self.assertEqual(system.stops, [host.unit])  # the system's own stop, past the bound
        self.assertEqual(host.life, 'unconfirmed')
        shown = units.status()
        self.assertEqual(shown['plugins']['stand.in']['life'], 'unconfirmed')
        self.assertEqual(shown['plugins']['stand.in']['stops_unconfirmed'], 1)
        (entry,) = shown['stop_unconfirmed']
        for words in (entry['words'], logged.output[0]):
            self.assertIn('the daemon ended the host of stand.in but could not confirm within 0.3 s '
                          '(plugin_host.stop_confirm_s) that the system will not start it again', words)
            self.assertIn('The daemon then asked the system to stop it (' + host.unit + ')', words)
            self.assertIn('Do not trust this host until the status shows it ended', words)
        with self.assertRaises(host_units.HostUnavailable) as caught:
            units.call('stand.in', 'operator', {'command': 'ok'})
        self.assertEqual(caught.exception.reason, 'stopped')
        return system, units, host

    def test_a_unit_the_system_starts_again_is_not_recorded_ended(self):
        system, units, host = self.assert_unconfirmed(RESTARTING)
        self.assertIn('the unit activating (auto-restart)', host.stop_words)

    def test_a_unit_with_a_start_job_queued_is_not_recorded_ended(self):
        _, _, host = self.assert_unconfirmed(QUEUED)
        self.assertIn('with a job queued', host.stop_words)

    def test_a_read_that_fails_is_never_taken_as_ended(self):
        system = FakeSystem(self)
        units = self.units(system)
        host = units.start('stand.in', *hs.package(hs.POLITE))
        system.fail_reads = True
        with self.assertLogs(host_units.log, logging.ERROR):
            units.stop('stand.in')
        self.assertEqual(host.life, 'unconfirmed')
        self.assertIn('the system showed nothing that could be read', host.stop_words)

    def test_a_failed_unit_that_the_system_keeps_is_ended(self):
        system = FakeSystem(self, after_kill=FAILED_KEPT)
        units = self.units(system)
        host = units.start('stand.in', *hs.package(hs.POLITE))
        units.stop('stand.in')
        self.assertEqual((host.life, system.stops), ('ended', []))

    def test_status_reads_an_unconfirmed_host_again_and_says_ended_once_the_system_shows_it(self):
        system = FakeSystem(self, after_kill=RESTARTING, after_stop=RESTARTING)
        units = self.units(system)
        host = units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, logging.ERROR):
            units.stop('stand.in')
        self.assertEqual(units.status()['plugins']['stand.in']['life'], 'unconfirmed')
        system.states[host.unit] = GONE
        shown = units.status()
        self.assertEqual(shown['plugins']['stand.in']['life'], 'ended')
        self.assertEqual(shown['stop_unconfirmed'], [])
        self.assertEqual(shown['plugins']['stand.in']['stops_unconfirmed'], 1)  # it happened, and is still said

    def test_a_host_still_starting_is_proven_ended_too(self):
        system = FakeSystem(self, after_kill=RESTARTING, after_stop=GONE, hello=False)
        units = self.units(system, start_deadline_s=None)
        thread, box = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        deadline = time.monotonic() + hs.CHILD_LIMIT_S
        while not system.started and time.monotonic() < deadline:
            time.sleep(POLL)
        while 'stand.in' not in units._starting and time.monotonic() < deadline:
            time.sleep(POLL)
        with self.assertLogs(host_units.log, logging.ERROR):
            units.stop('stand.in')
        thread.join(hs.CHILD_LIMIT_S)
        self.assertEqual(box['error'].reason, 'stopped')
        (unit,) = system.started
        self.assertEqual(system.stops, [unit])
        self.assertEqual(units.status()['plugins']['stand.in']['life'], 'ended')  # read again: now gone

    def test_close_proves_every_host_within_one_bound(self):
        system = FakeSystem(self, after_kill=RESTARTING, after_stop=RESTARTING)
        units = self.units(system)
        hosts = [units.start(name, *hs.package(hs.POLITE, name)) for name in ('stand.one', 'stand.two')]
        began = time.monotonic()
        with self.assertLogs(host_units.log, logging.ERROR):
            units.close()
        took = time.monotonic() - began
        self.assertLess(took, SHORT * 2)  # one bound for both, never one each
        self.assertEqual(sorted(system.stops), sorted(host.unit for host in hosts))
        self.assertEqual([host.life for host in hosts], ['unconfirmed', 'unconfirmed'])

    def test_the_stop_bound_is_a_number_of_seconds_and_no_limit_is_not_offered(self):
        self.assertEqual(host_units.STOP_CONFIRM_S, 5.0)
        for name in ('stop_confirm_s', 'stop_confirm_poll_s'):
            for bad in (None, 0, -1, True, '5', float('nan'), float('inf'), 10 ** 400):
                with self.subTest(name=name, value=bad):
                    with self.assertRaises(ValueError) as caught:
                        host_units.PluginHostUnits(FakeSystem(self), self.state, hs.StandInCore(), **{name: bad})
                    self.assertIn(name, str(caught.exception))
                    self.assertIn('write a number of seconds above zero', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            host_units.PluginHostUnits(FakeSystem(self), self.state, hs.StandInCore(), stop_confirm_s=None)
        self.assertIn('No limit is not offered here: a Stop with no bound on its proof is no Stop', str(caught.exception))
        units = host_units.PluginHostUnits(FakeSystem(self), self.state, hs.StandInCore(), stop_confirm_s=0.5)
        self.assertEqual(units.status()['bounds']['plugin_host.stop_confirm_s'], 0.5)


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class AHostOfAnEarlierLifeIsStoppedAndReported(_Folder):
    def test_a_host_unit_this_daemon_did_not_start_is_stopped_and_reported_and_its_own_is_left(self):
        system = FakeSystem(self)
        units = host_units.PluginHostUnits(system, self.state, hs.StandInCore(), python='/stand-in/python',
                                           stop_confirm_s=SHORT, stop_confirm_poll_s=POLL)
        self.addCleanup(units.close)
        ours = units.start('stand.in', *hs.package(hs.POLITE)).unit
        stray = 'recursion-host-00000000000000aa.service'
        restarting = 'recursion-host-00000000000000bb.service'
        quiet = 'recursion-host-00000000000000cc.service'
        system.states.update({quiet: GONE, restarting: RESTARTING})
        system.listed = [ours, stray, restarting, quiet, 'builder-other.service', 'recursion-host-x.service']
        system.after_stop = GONE
        with self.assertLogs(host_units.log, logging.ERROR):
            found = units.sweep_strays()
        self.assertEqual(system.stops, [stray, restarting])
        self.assertEqual(len(found), 2)
        self.assertIn(f'a plugin host unit {stray} that this daemon did not start was found with the unit active '
                      '(running); the daemon asked the system to stop it', found[0])
        self.assertIn('found with the unit activating (auto-restart)', found[1])
        self.assertIn('Do not trust that host', found[1])  # the stand-in still shows it restarting
        self.assertEqual(units.status()['strays'], found)
        self.assertNotIn(ours, system.stops)


if __name__ == '__main__':
    unittest.main()
