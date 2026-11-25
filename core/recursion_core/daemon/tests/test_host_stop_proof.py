"""A host the daemon ends stays ended.

A host unit carries only the properties the launcher's allow list names; the launcher's own are written after the
caller's; and Stop records a host ended only when the system shows its unit ended and not about to start again.
Stand-in user managers only: no unit is started and no process but this test's threads.
"""
from __future__ import annotations

import logging
import random
import socket
import subprocess
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
HANG_BOUND = 0.5   # the Stop bound of the test whose user manager answers nothing (a test figure)
HANG_POLL = 0.05   # its poll, the shipped plugin_host.stop_confirm_poll_s (a test figure)
HANG_SLACK = 0.25  # how much past three bounds and one poll that Stop may take: the test's own threads (a test figure)
HANG_COUNTS = (3, 10, 50)  # the counts of hosts that Stop is timed with (a test figure)


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

    def __init__(self, test, *, after_kill=GONE, after_stop=GONE, hello=True, limit=None):
        self.after_kill, self.after_stop, self.hello = after_kill, after_stop, hello
        self.limit = hs.HOSTS_PER_TEST if limit is None else limit  # the hard count on the hosts of one test
        self.killed, self.stops, self.reads, self.ends, self.started = [], [], [], [], []
        self.states, self.listed, self.fail_reads = {}, [], False
        self.stop_requests, self.stop_waits = [], False  # each request of the system's stop; whether it waits it out
        test.addCleanup(self.hang_up_all)

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= self.limit:
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

    def unit_identity(self, unit, timeout_s=None):
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

    def unit_states(self, units, timeout_s):
        """One read of every unit of ``units``, as the user manager's: what ``unit_state`` shows of each, with its
        control group."""
        return {unit: {**self.unit_state(unit, timeout_s), 'ControlGroup': '/stand-in.slice/' + unit}
                for unit in units}

    def stop_units(self, units, timeout_s):
        self.stop_requests.append(list(units))
        self.stops.extend(units)
        if self.stop_waits:  # a manager that does not answer: the request takes its whole bound
            time.sleep(timeout_s)

    def listed_units(self, pattern, timeout_s):
        return list(self.listed)

    def hang_up_all(self):
        for _, end in self.ends:
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            end.close()


class SilentSystem(FakeSystem):
    """A stand-in user manager that answers nothing: every read of the units and the system's stop take their whole
    bound and then time out, as subprocess does."""

    def __init__(self, test, **options):
        super().__init__(test, **options)
        self.state_reads = []  # the units of each read of the system, one list per read
        self.identity_hangs = False  # once set, a read of a unit's control group hangs too

    def unit_identity(self, unit, timeout_s=None):
        if not self.identity_hangs:
            return super().unit_identity(unit, timeout_s)
        self.state_reads.append([unit])
        time.sleep(hs.CHILD_LIMIT_S if timeout_s is None else timeout_s)
        raise subprocess.TimeoutExpired(['systemctl'], timeout_s)

    def unit_state(self, unit, timeout_s):
        self.state_reads.append([unit])
        time.sleep(timeout_s)
        raise subprocess.TimeoutExpired(['systemctl'], timeout_s)

    def unit_states(self, units, timeout_s):
        self.state_reads.append(list(units))
        time.sleep(timeout_s)
        raise subprocess.TimeoutExpired(['systemctl'], timeout_s)

    def stop_units(self, units, timeout_s):
        self.stop_requests.append(list(units))
        time.sleep(timeout_s)
        raise subprocess.TimeoutExpired(['systemctl'], timeout_s)


class HeldLaunch(FakeSystem):
    """A stand-in user manager that holds each start inside the making of its unit until the test lets it go."""

    def __init__(self, test, **options):
        super().__init__(test, **options)
        self.inside, self.go = threading.Event(), threading.Event()
        test.addCleanup(self.go.set)

    def start_piped(self, unit, argv, channel, **options):
        process = super().start_piped(unit, argv, channel, **options)
        self.inside.set()
        self.go.wait(hs.CHILD_LIMIT_S)
        return process


def plugin_ids(count):
    """``count`` plugin ids of letters only."""
    letters = 'abcdefghijklmnopqrstuvwxyz'
    return [f'stand.h{letters[number // 26]}{letters[number % 26]}' for number in range(count)]


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

    def test_the_bounds_the_real_unit_tests_give_every_host_are_taken(self):
        # The real-unit tests (test_host_units.GUARD) give every host these; a gate with no user manager skips those
        # tests, so this one holds the list to them without a user manager.
        from recursion_core.daemon.tests import test_host_units as hu
        self.assertEqual(launcher.check_piped_properties(hu.GUARD), hu.GUARD)
        host_units.PluginHostUnits(FakeSystem(self), self.state, hs.StandInCore(), properties=hu.GUARD)
        head = piped_command(hu.GUARD)
        for item in hu.GUARD:
            self.assertIn('--property=' + item, head)
        self.assertLess(head.index('--property=RuntimeMaxSec=120'), head.index('--property=Restart=no'))

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

    def test_the_daemons_stop_asks_one_system_stop_for_every_unconfirmed_host_so_three_bounds_at_most(self):
        system = FakeSystem(self)
        units = self.units(system)
        hosts = [units.start(name, *hs.package(hs.POLITE, name)) for name in ('stand.one', 'stand.two', 'stand.six')]
        system.fail_reads = system.stop_waits = True  # the manager answers nothing, and its stop takes its bound
        began = time.monotonic()
        with self.assertLogs(host_units.log, logging.ERROR):
            units.close()
        took = time.monotonic() - began
        self.assertEqual([sorted(request) for request in system.stop_requests], [sorted(h.unit for h in hosts)])
        self.assertLess(took, SHORT * 3)  # the proof, the one stop and the last reads: never a bound per host
        self.assertEqual([host.life for host in hosts], ['unconfirmed'] * 3)
        for host in hosts:
            self.assertIn('The daemon then asked the system to stop it (' + host.unit + ')', host.stop_words)

    def test_the_daemons_stop_waits_three_bounds_and_one_poll_at_most_whatever_the_count_of_hosts(self):
        took = {}
        for count in HANG_COUNTS:
            with self.subTest(hosts=count):
                system = SilentSystem(self, limit=count)
                units = self.units(system, stop_confirm_s=HANG_BOUND, stop_confirm_poll_s=HANG_POLL)
                hosts = [units.start(name, *hs.package(hs.POLITE, name)) for name in plugin_ids(count)]
                began = time.monotonic()
                with self.assertLogs(host_units.log, logging.ERROR):
                    units.close()
                took[count] = time.monotonic() - began
                self.assertLess(took[count], 3 * HANG_BOUND + HANG_POLL + HANG_SLACK, took)
                self.assertEqual([sorted(request) for request in system.stop_requests],
                                 [sorted(host.unit for host in hosts)])  # one system stop for all of them
                self.assertEqual({host.life for host in hosts}, {'unconfirmed'})
                for host in hosts:
                    self.assertIn('the system showed nothing that could be read', host.stop_words)
                    self.assertIn('The daemon then asked the system to stop it, which failed (TimeoutExpired)',
                                  host.stop_words)
        print(f'\nSTOP-TIMES bound={HANG_BOUND} poll={HANG_POLL} ' + ' '.join(f'{count}:{value:.2f}s'
                                                                           for count, value in took.items()))

    def test_hosts_still_starting_add_no_bound_per_host_to_the_daemons_stop(self):
        took = {}
        for count in HANG_COUNTS:
            with self.subTest(starting=count):
                system = SilentSystem(self, limit=count, hello=False)
                units = self.units(system, stop_confirm_s=HANG_BOUND, stop_confirm_poll_s=HANG_POLL,
                                   start_deadline_s=None)
                names = plugin_ids(count)
                starts = [hs.in_thread(lambda name=name: units.start(name, *hs.package(hs.POLITE, name)))
                          for name in names]
                deadline = time.monotonic() + hs.CHILD_LIMIT_S
                while len(units._starting) < count and time.monotonic() < deadline:
                    time.sleep(HANG_POLL)
                self.assertEqual(len(units._starting), count)  # no control group known of any of them yet
                system.identity_hangs = True
                began = time.monotonic()
                with self.assertLogs(host_units.log, logging.ERROR):
                    units.close()
                took[count] = time.monotonic() - began
                self.assertLess(took[count], 3 * HANG_BOUND + HANG_POLL + HANG_SLACK, took)
                for thread, box in starts:
                    thread.join(hs.CHILD_LIMIT_S)
                    self.assertEqual(box['error'].reason, 'stopped')
                self.assertEqual([sorted(request) for request in system.stop_requests], [sorted(system.started)])
        print(f'\nSTARTING-STOP-TIMES bound={HANG_BOUND} poll={HANG_POLL} ' + ' '.join(
            f'{count}:{value:.2f}s' for count, value in took.items()))

    def test_a_start_that_begins_after_the_daemons_stop_is_refused_and_one_inside_it_ends_its_host(self):
        system = HeldLaunch(self)
        units = self.units(system)
        thread, box = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        self.assertTrue(system.inside.wait(hs.CHILD_LIMIT_S))
        units.close()  # the system is making the unit of stand.in: the daemon has not recorded that host yet
        with self.assertRaises(host_units.HostUnavailable) as refused:
            units.start('stand.two', *hs.package(hs.POLITE, 'stand.two'))
        self.assertEqual((refused.exception.reason, refused.exception.detail), ('stopped', 'the daemon is closing'))
        (unit,) = system.started  # the refused start made no unit
        system.go.set()
        thread.join(hs.CHILD_LIMIT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box['error'].reason, 'stopped')
        self.assertEqual(system.killed, [unit])  # its own start ended it, by the kill
        self.assertIsNone(units.unit_of('stand.in'))
        self.assertEqual(units.status()['plugins']['stand.in']['life'], 'ended')  # and proved it

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


RACE_ROUNDS = 2000  # rounds of each race of a start inside the making of its unit with the daemon's Stop (a test figure)
RACE_HOLD_S = 0.004  # the longest the stand-in's making of a unit takes in those races (a test figure)
RACE_LEAD_S = 0.003  # the longest the race waits before its Stop begins (a test figure)


class RacingSystem(FakeSystem):
    """A stand-in user manager whose making of a unit takes a random while, up to ``hold``, after the host exists, so a
    Stop can begin while a start is inside it. A host is a thread that lives until the kill or the end of its channel,
    as a real host does; ``inside`` is set once a host exists."""

    def __init__(self, test, **options):
        super().__init__(test, limit=10 ** 6, **options)
        self.hold, self.inside, self.lock, self.threads = 0.0, threading.Event(), threading.Lock(), {}

    def start_piped(self, unit, argv, channel, **options):
        end = channel.dup()
        with self.lock:
            self.started.append(unit)
            self.ends.append((unit, end))

        def host():
            try:
                end.sendall(hc.encode(hs.HELLO))
                while end.recv(1 << 16):
                    pass
            except OSError:
                pass
            end.close()

        thread = threading.Thread(target=host, daemon=True)
        self.threads[unit] = thread
        thread.start()
        self.inside.set()
        if self.hold:
            time.sleep(random.uniform(0, self.hold))
        return hs.StandInProcess()

    def own_fault(self, unit):
        """The host of ``unit`` ends by its own fault: its end of the channel shuts, with no kill."""
        for name, end in list(self.ends):
            if name == unit:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def alive(self, unit):
        thread = self.threads.get(unit)
        return thread is not None and thread.is_alive()


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class AStartInsideTheMakingOfItsUnitIsSeenByTheStop(_Folder):
    """A start whose unit the system is making as a Stop begins: the Stop waits for it within its bound and proves the
    host it records, or names it; never a host alive when the Stop returns that the Stop neither ended nor named."""

    def units(self, system, **options):
        options.setdefault('stop_confirm_s', SHORT)
        options.setdefault('stop_confirm_poll_s', POLL)
        units = host_units.PluginHostUnits(system, self.state, hs.StandInCore(), python='/stand-in/python', **options)
        self.addCleanup(units.close)
        return units

    def race(self, through_a_call):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        system = RacingSystem(self)
        unseen, handed = [], []
        for number in range(RACE_ROUNDS):
            system.hold = 0.0
            units = host_units.PluginHostUnits(system, self.state, hs.StandInCore(), python='/stand-in/python',
                                               stop_confirm_s=SHORT, stop_confirm_poll_s=POLL)
            first = len(system.started)
            units.start('stand.run', *hs.package(hs.POLITE, 'stand.run'))
            system.inside.clear()
            if through_a_call:  # its host ends by its own fault, and the next call starts a new one
                system.own_fault(units.unit_of('stand.run').unit)
                with self.assertRaises(host_units.HostUnavailable):
                    units.call('stand.run', 'operator', {'command': 'one'})
                system.inside.clear()
                system.hold = RACE_HOLD_S
                work = hs.in_thread(lambda: units.call('stand.run', 'operator', {'command': 'two'}))
            else:
                system.hold = RACE_HOLD_S
                work = hs.in_thread(lambda: units.start('stand.late', *hs.package(hs.POLITE, 'stand.late')))
            if number % 2:
                system.inside.wait(hs.JOIN_S)
            time.sleep(random.uniform(0, RACE_LEAD_S))
            named = units.close()
            made = system.started[first:]
            for unit in made:
                if system.alive(unit) and unit not in system.killed and not named:
                    unseen.append(unit)  # alive when the Stop returned, neither ended by it nor named
            thread, box = work
            thread.join(hs.JOIN_S)
            self.assertFalse(thread.is_alive())
            if 'value' in box and not through_a_call and box['value'].unit not in system.killed:
                handed.append(box['value'].unit)  # a host given to its caller that the Stop did not end
            system.hang_up_all()
            system.ends.clear()
        return unseen, handed

    def test_the_plain_race_of_a_start_with_the_daemons_stop_leaves_no_host_alive_and_unnamed(self):
        unseen, handed = self.race(through_a_call=False)
        print(f'\nRACE start rounds={RACE_ROUNDS} alive and unnamed at the Stop\'s return: {len(unseen)}; '
              f'handed to a caller: {len(handed)}')
        self.assertEqual(unseen, [])
        self.assertEqual(handed, [])

    def test_the_race_of_a_calls_new_host_with_the_daemons_stop_leaves_no_host_alive_and_unnamed(self):
        unseen, _ = self.race(through_a_call=True)
        print(f'\nRACE call rounds={RACE_ROUNDS} alive and unnamed at the Stop\'s return: {len(unseen)}')
        self.assertEqual(unseen, [])

    def test_a_start_that_records_its_host_within_the_bound_is_ended_and_proven_by_the_stop(self):
        system = HeldLaunch(self)
        units = self.units(system)
        thread, box = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        self.assertTrue(system.inside.wait(hs.CHILD_LIMIT_S))
        letting_go = hs.in_thread(lambda: (time.sleep(SHORT / 3), system.go.set()))
        began = time.monotonic()
        named = units.close()
        took = time.monotonic() - began
        (unit,) = system.started
        self.assertEqual(named, [])
        self.assertIn(unit, system.killed)  # ended before the Stop returned
        self.assertEqual(units.status()['plugins']['stand.in']['life'], 'ended')  # and proven by it
        self.assertEqual(units.status()['starts_still_making'], [])
        self.assertLess(took, SHORT + POLL + HANG_SLACK)
        letting_go[0].join(hs.JOIN_S)
        thread.join(hs.JOIN_S)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('value', box)
        self.assertEqual(box['error'].reason, 'stopped')
        self.assertIsNone(units.unit_of('stand.in'))

    def test_a_start_that_reaches_the_making_of_its_unit_after_the_daemons_stop_began_makes_no_unit(self):
        system = FakeSystem(self)
        units = self.units(system)
        inside, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)
        real = host_units.secrets.token_hex

        def held(size):  # the start is past its first look at the Stop and has not reached the making of its unit
            inside.set()
            go.wait(hs.CHILD_LIMIT_S)
            return real(size)
        with unittest.mock.patch.object(host_units.secrets, 'token_hex', held):
            thread, box = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
            self.assertTrue(inside.wait(hs.CHILD_LIMIT_S))
            self.assertEqual(units.close(), [])  # no start was making a unit
            go.set()
            thread.join(hs.CHILD_LIMIT_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual((box['error'].reason, box['error'].detail), ('stopped', 'the daemon is closing'))
        self.assertEqual(system.started, [])  # no unit was made

    def assert_named_then_ended(self, stop):
        system = HeldLaunch(self)
        units = self.units(system)
        thread, box = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        self.assertTrue(system.inside.wait(hs.CHILD_LIMIT_S))
        began = time.monotonic()
        with self.assertLogs(host_units.log, logging.ERROR) as logged:
            named = stop(units)
        took = time.monotonic() - began
        self.assertGreaterEqual(took, SHORT)  # it waited for the start, within its bound
        self.assertLess(took, 3 * SHORT + POLL + HANG_SLACK)  # and never past its promised bounds
        (words,) = named
        self.assertIn('a start of stand.in was making its unit as a Stop began and had not recorded its host within '
                      '0.3 s (plugin_host.stop_confirm_s), so that Stop could not end it: its host may still appear, '
                      'and that start ends it and proves it as soon as it records it. Do not take it as stopped until '
                      'the status shows that host ended', words)
        self.assertIn(words, logged.output[0])
        self.assertEqual(units.status()['starts_still_making'], [{'plugin': 'stand.in', 'words': words}])
        (unit,) = system.started
        self.assertNotIn(unit, system.killed)  # not seen by the Stop: named, not called stopped
        system.go.set()
        thread.join(hs.CHILD_LIMIT_S)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('value', box)  # the host it made is handed to no caller
        self.assertEqual(box['error'].reason, 'stopped')
        self.assertEqual(system.killed, [unit])  # its own start ended it, by the kill
        self.assertIsNone(units.unit_of('stand.in'))
        shown = units.status()
        self.assertEqual(shown['plugins']['stand.in']['life'], 'ended')  # and proved it
        self.assertEqual(shown['starts_still_making'], [])  # the name goes once the host's own record says ended

    def test_a_start_still_making_its_unit_when_the_daemons_stop_bound_ends_is_named_and_ends_its_host(self):
        self.assert_named_then_ended(lambda units: units.close())

    def test_a_start_still_making_its_unit_when_a_plugins_stop_bound_ends_is_named_and_ends_its_host(self):
        self.assert_named_then_ended(lambda units: units.stop('stand.in'))


if __name__ == '__main__':
    unittest.main()
