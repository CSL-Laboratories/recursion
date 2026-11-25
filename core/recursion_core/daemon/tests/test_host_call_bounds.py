"""What a call meets: a start that waits for the turn, arguments that are not a mapping, and the bound on the
daemon's own time in one call. Every host is a thread of the test (test_host_unit_calls.Hosts or the one below);
no unit, no process and no plugin code is started."""
from __future__ import annotations

import socket
import threading
import time
import unittest

from recursion_core.daemon import host_units
from recursion_core.daemon.tests import test_host_start as hs
from recursion_core.daemon.tests import test_host_unit_calls as tc
from recursion_core.plugin_security import host_channel as hc

CORE_S = 0.2     # the daemon's own time on one core request of the tests (a test figure)
MOST_S = 0.6     # the bound on the daemon's time in those tests (a test figure)
SETTLE_S = 0.3   # how long a test lets a thread reach the turn it waits on (a test figure)
POLL_S = 0.01    # how often a test looks again at what a thread has reached (a test figure)
JOIN_S = hs.JOIN_S


class BusyHosts(tc.Hosts):
    """Hosts that answer a call with core requests without end: each answer of the daemon brings the next."""

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= hs.HOSTS_PER_TEST:
            raise AssertionError('this test started more hosts than its count allows')
        self.started.append(unit)
        seen = []
        self.seen.append(seen)
        end = channel.dup()
        self.ends.append((unit, end))

        def host():
            try:
                end.sendall(hc.encode(hs.HELLO))
                number = 0
                for message in tc.frames(end, seen, self.stop):
                    if message['type'] == 'call' or number:  # the call, then every answer of the daemon's
                        self.called.set()
                        number += 1
                        end.sendall(hc.encode({'type': 'core', 'id': number, 'bytes_b64': hs.b64(b'{}')}))
            except (OSError, StopIteration):
                pass

        threading.Thread(target=host, daemon=True).start()
        return hs.StandInProcess()


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class ACallThatWaitsBehindAStart(tc._Units):
    def test_a_call_waiting_for_the_turn_when_a_start_comes_starts_no_host_and_ends_replaced(self):
        hosts = tc.Hosts(self, ['hang', 'answers'])
        units = self.units(hosts, call_deadline_s=None, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        hanging, first = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'loop'}))
        self.assertTrue(hosts.called.wait(JOIN_S), 'the first host never read the call')
        waiting, second = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'next'}))
        time.sleep(SETTLE_S)  # the second call now waits for the turn the first holds
        starting, start = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        for thread in (hanging, waiting, starting):
            thread.join(JOIN_S)
            self.assertFalse(thread.is_alive())
        self.assertIsInstance(start.get('value'), host_units.HostUnit, start)
        self.assertEqual(len(hosts.started), 2)  # one spawn: the start's; no host of the old package
        self.assertEqual(first['error'].reason, 'replaced')
        if 'error' in second:  # it took the turn before the start
            self.assertEqual(second['error'].reason, 'replaced')
            self.assertEqual(second['error'].detail, host_units.REPLACED_DETAIL)
        else:  # the start took the turn first: its host answered
            self.assertEqual(second['value']['value'], 2)
        self.assertEqual(units.degraded, {})

    def test_a_call_ended_replaced_says_what_happened_and_that_it_may_be_sent_again(self):
        hosts = tc.Hosts(self, ['hang', 'answers'])
        units = self.units(hosts, call_deadline_s=None, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        hanging, call = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'loop'}))
        self.assertTrue(hosts.called.wait(JOIN_S), 'the first host never read the call')
        units.start('stand.in', *hs.package(hs.POLITE))
        hanging.join(JOIN_S)
        error = call['error']
        self.assertEqual(error.reason, 'replaced')
        self.assertEqual(error.detail, 'a new start of the plugin ended the host this call waited on; the call was not '
                                       'answered and may be sent again')
        self.assertIn(error.detail, str(error))


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class ArgumentsThatAreNotAMapping(tc._Units):
    def test_arguments_that_are_not_a_mapping_end_in_call_refused_and_the_host_stays(self):
        hosts = tc.Hosts(self, ['answers'])
        units = self.units(hosts, call_deadline_s=5, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        for args in (5, ['a'], [('command', 'x')], 'command', None):
            with self.subTest(args=args):
                with self.assertRaises(host_units.CallRefused) as caught:
                    units.call('stand.in', 'operator', args)
                self.assertEqual((caught.exception.reason, caught.exception.code), ('call_invalid', 'message_invalid'))
                self.assertIn('the arguments of a call are a mapping of names to values', str(caught.exception))
        self.assertEqual(units.degraded, {})
        self.answered_by(units, hosts, 1)

    def test_the_docstring_of_call_names_the_moved_deadline_its_bound_and_call_refused(self):
        text = ' '.join(host_units.PluginHostUnits.call.__doc__.split())
        self.assertIn("The deadline moves on by the daemon's own time on the host's core requests", text)
        self.assertIn('plugin_host.call_daemon_time_max_s', text)
        self.assertIn('CallRefused (``call_invalid``)', text)


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class TheDaemonsTimeInOneCallHasABound(tc._Units):
    def test_a_host_that_keeps_the_daemon_busy_ends_timed_out_at_the_bound_with_the_row_named(self):
        hosts = BusyHosts(self, ['busy'])
        units = self.units(hosts, tc.SlowCore(took=CORE_S), call_deadline_s=5, start_deadline_s=5,
                           call_daemon_time_max_s=MOST_S)
        units.start('stand.in', *hs.package(hs.POLITE))
        thread, box = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'x'}))
        thread.join(MOST_S + CORE_S + JOIN_S)
        alive = thread.is_alive()
        if alive:
            units.stop('stand.in')  # never left running past the test
            thread.join(JOIN_S)
        self.assertFalse(alive, 'the call went on past its bound on the daemon\'s time')
        self.assertGreaterEqual(box['took'], MOST_S)
        caught = box['error']
        self.assertEqual(caught.reason, 'timed_out')
        self.assertIn("the daemon's own time on this host's requests passed its bound of 0.6 s "
                      '(plugin_host.call_daemon_time_max_s)', caught.detail)
        self.assertEqual(units.degraded, {'stand.in': 1})

    def test_with_no_bound_the_call_goes_on_as_before(self):
        hosts = BusyHosts(self, ['busy'])
        units = self.units(hosts, tc.SlowCore(took=CORE_S), call_deadline_s=5, start_deadline_s=5,
                           call_daemon_time_max_s=None)
        units.start('stand.in', *hs.package(hs.POLITE))
        thread, box = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'x'}))
        thread.join(MOST_S * 3)
        self.assertTrue(thread.is_alive(), box)  # past three times the bound, still in its call
        units.stop('stand.in')
        thread.join(JOIN_S)
        self.assertEqual(box['error'].reason, 'stopped')

    def test_the_row_ships_300_s_takes_none_and_refuses_anything_else(self):
        self.assertEqual(host_units.CALL_DAEMON_TIME_MAX_S, 300.0)
        hosts = tc.Hosts(self, ['answers'])
        for bad in (0, -1, True, '5', float('nan'), float('inf')):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as caught:
                    self.units(hosts, call_daemon_time_max_s=bad)
                self.assertIn('call_daemon_time_max_s', str(caught.exception))
        self.assertEqual(self.units(hosts).bounds()['plugin_host.call_daemon_time_max_s'], 300.0)
        self.assertIsNone(self.units(hosts, call_daemon_time_max_s=None).status()['bounds'][
            'plugin_host.call_daemon_time_max_s'])

    def test_a_call_past_its_own_deadline_names_that_row(self):
        hosts = tc.Hosts(self, ['hang'])
        units = self.units(hosts, call_deadline_s=0.3, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as caught:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual(caught.exception.reason, 'timed_out')
        self.assertIn("by the call's deadline of 0.3 s (plugin_host.call_deadline_s)", caught.exception.detail)


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class ACallReplacedWhileItsOwnStartIsUnderWay(tc._Units):
    def test_it_ends_replaced_with_the_words_that_say_the_call_may_be_sent_again(self):
        hosts = tc.Hosts(self, ['answers', 'silent', 'answers'])
        units = self.units(hosts, call_deadline_s=None, start_deadline_s=None)
        first = units.start('stand.in', *hs.package(hs.POLITE))
        hosts.kill_cgroup('', first.unit)  # the host ends by itself
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as ended:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual(ended.exception.reason, 'ended')
        thread, box = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'y'}))
        deadline = time.monotonic() + JOIN_S
        while 'stand.in' not in units._starting and time.monotonic() < deadline:  # the call starts its own host
            time.sleep(POLL_S)
        self.assertEqual(len(hosts.started), 2)
        units.start('stand.in', *hs.package(hs.POLITE))  # replaces the host the call is starting
        thread.join(JOIN_S)
        self.assertFalse(thread.is_alive())
        error = box.get('error')
        self.assertIsInstance(error, host_units.HostUnavailable, box)
        self.assertEqual((error.reason, error.detail), ('replaced', host_units.REPLACED_DETAIL))
        self.assertIn('the call was not answered and may be sent again', str(error))
        self.answered_by(units, hosts, 3)


if __name__ == '__main__':
    unittest.main()
