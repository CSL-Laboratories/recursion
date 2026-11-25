"""The daemon's side of a call into a plugin host: what ends a host, and what does not.

* A second start of a plugin ends a host that hangs in a call with no deadline before it waits for the plugin's
  turn; the hanging call ends with ``replaced``, and so does a start still under way.
* The daemon's own time on a message of the host does not count against the call's deadline, and a frame the host
  had already written is taken before a call is said to have timed out.
* A call whose own message the channel refuses fails alone with ``CallRefused``; the host stays and answers the next
  call. An answer of the daemon's that the channel refuses reaches the host as a refusal of its message.

Every host here is a thread of the test on a copy of the host's end of the channel; no unit, no process and no
plugin code is started. Everything lives in one temporary folder of the test's own.
"""
from __future__ import annotations

import socket
import threading
import time
import unittest
import unittest.mock

from recursion_core.control_transport.framing import HEADER
from recursion_core.daemon import host_units
from recursion_core.daemon.tests import test_host_start as hs
from recursion_core.plugin_security import host_channel as hc

DEADLINE_S = 0.5     # the call deadline of the tests on time
SLOW_S = 1.0         # how long the daemon's side takes over one message of the host (a slow core request)
HOST_S = 0.2         # how long the stand-in host itself takes before it answers: well inside DEADLINE_S
HANG_S = 60          # how long a hanging stand-in host waits on its own before it gives up (never reached)
JOIN_S = hs.JOIN_S


def frames(sock, out, stop):
    """Read frames from ``sock`` into ``out`` (decoded messages) until the end of the channel."""
    buffer = b''
    while not stop.is_set():
        chunk = sock.recv(1 << 16)
        if not chunk:
            return
        buffer += chunk
        while len(buffer) >= HEADER.size:
            (length,) = HEADER.unpack_from(buffer)
            if len(buffer) < HEADER.size + length:
                break
            body, buffer = buffer[HEADER.size:HEADER.size + length], buffer[HEADER.size + length:]
            out.append(hc.decode(body, sender='daemon'))
            yield out[-1]


class Hosts:
    """A stand-in user manager: each 'host' is a thread of this test on a copy of the host's end, by mode.

    silent: never says hello. hang: reads the call and never answers.
    answers: answers every call with the number of its host.
    stray_hello: answers a call with a stray hello and, HOST_S later, its result (the reproducer's host).
    core: answers a call with one core request, waits for the daemon's answer to it, takes HOST_S, then its result.
    The cgroup kill hangs up that host's end. At most hs.HOSTS_PER_TEST hosts."""

    def __init__(self, test, modes):
        self.modes, self.started, self.seen, self.ends, self.stop = modes, [], [], [], threading.Event()
        self.called = threading.Event()
        test.addCleanup(self.hang_up_all)

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= hs.HOSTS_PER_TEST:
            raise AssertionError('this test started more hosts than its count allows')
        number = len(self.started) + 1
        mode = self.modes[min(number, len(self.modes)) - 1]
        self.started.append(unit)
        seen = []
        self.seen.append(seen)
        end = channel.dup()
        self.ends.append((unit, end))

        def host():
            try:
                if mode != 'silent':
                    end.sendall(hc.encode(hs.HELLO))
                messages = frames(end, seen, self.stop)
                for message in messages:
                    if message['type'] != 'call':
                        continue
                    self.called.set()
                    if mode == 'hang':
                        self.stop.wait(HANG_S)
                    elif mode == 'answers':
                        end.sendall(hc.encode({'type': 'result', 'id': message['id'], 'value': number}))
                    elif mode == 'stray_hello':
                        end.sendall(hc.encode(hs.HELLO))
                        time.sleep(HOST_S)
                        end.sendall(hc.encode({'type': 'result', 'id': message['id'], 'value': number}))
                    elif mode == 'core':
                        end.sendall(hc.encode({'type': 'core', 'id': 1, 'bytes_b64': hs.b64(b'{}')}))
                        next(messages)  # the daemon's answer to the core request, whatever it is
                        time.sleep(HOST_S)
                        end.sendall(hc.encode({'type': 'result', 'id': message['id'], 'value': number}))
            except (OSError, StopIteration):
                pass

        threading.Thread(target=host, daemon=True).start()
        return hs.StandInProcess()

    def unit_identity(self, unit, timeout_s=None):
        return '/stand-in.slice/' + unit, 0

    def kill_cgroup(self, group, unit):
        for name, end in self.ends:
            if name == unit:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        return True

    def unit_state(self, unit, timeout_s):
        """What the system shows of the stand-in's unit after its kill: gone (the stand-in has no process)."""
        return {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'Job': ''}

    def hang_up_all(self):
        self.stop.set()
        for name, end in self.ends:
            self.kill_cgroup('', name)
            end.close()


class SlowCore:
    """The core service with one slow answer: each request takes SLOW_S of the daemon's own time."""

    def __init__(self, answer=b'{}', took=SLOW_S):
        self.answer, self.took = answer, took

    def client_for(self, plugin_id):
        def answer(request):
            time.sleep(self.took)
            return self.answer
        return answer


class _Units(hs._Folder):
    def units(self, hosts, core=None, **options):
        units = host_units.PluginHostUnits(hosts, self.state, core or hs.StandInCore(), python='/stand-in/python',
                                           **options)
        self.addCleanup(units.close)
        return units

    def answered_by(self, units, hosts, number):
        """The next call is answered, by host ``number`` of this test, and nothing is marked degraded."""
        answer = units.call('stand.in', 'operator', {'command': 'next'})
        self.assertEqual((answer['type'], answer['value']), ('result', number))
        self.assertEqual(units.degraded, {})
        self.assertEqual(len(hosts.started), number)


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class AStartReplacesAHostThatHangs(_Units):
    def test_a_second_start_ends_a_host_that_hangs_in_a_call_with_no_deadline_before_it_waits(self):
        hosts = Hosts(self, ['hang', 'answers'])
        units = self.units(hosts, call_deadline_s=None, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        hanging, call = hs.in_thread(lambda: units.call('stand.in', 'operator', {'command': 'loop'}))
        self.assertTrue(hosts.called.wait(JOIN_S), 'the first host never read the call')
        second, start = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        second.join(JOIN_S)
        self.assertFalse(second.is_alive(), 'the second start waited on the call that hangs')
        self.assertIsInstance(start.get('value'), host_units.HostUnit, start)
        hanging.join(JOIN_S)
        self.assertFalse(hanging.is_alive(), 'the call into the replaced host did not end')
        self.assertIsInstance(call.get('error'), host_units.HostUnavailable, call)
        self.assertEqual(call['error'].reason, 'replaced')
        self.assertEqual(len(hosts.started), 2)
        self.answered_by(units, hosts, 2)

    def test_a_second_start_ends_a_start_still_under_way_with_no_start_deadline(self):
        hosts = Hosts(self, ['silent', 'answers'])
        units = self.units(hosts, call_deadline_s=None, start_deadline_s=None)
        first, start_a = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        deadline = time.monotonic() + JOIN_S
        while not hosts.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(hosts.started, 'the first start never started its host')
        second, start_b = hs.in_thread(lambda: units.start('stand.in', *hs.package(hs.POLITE)))
        second.join(JOIN_S)
        self.assertFalse(second.is_alive(), 'the second start waited on a start still under way')
        self.assertIsInstance(start_b.get('value'), host_units.HostUnit, start_b)
        first.join(JOIN_S)
        self.assertFalse(first.is_alive(), 'the replaced start did not end')
        self.assertIsInstance(start_a.get('error'), host_units.HostUnavailable, start_a)
        self.assertEqual(start_a['error'].reason, 'replaced')
        self.answered_by(units, hosts, 2)


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class TheDaemonsOwnTimeIsNotTheHosts(_Units):
    def test_the_reproducers_slow_stand_in_a_result_in_time_is_returned(self):
        hosts = Hosts(self, ['stray_hello'])
        units = self.units(hosts, call_deadline_s=DEADLINE_S, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        real_receive = hc.DaemonEnd.receive

        def slow(end, message):
            if type(message) is dict and message.get('type') == 'hello':
                time.sleep(SLOW_S)
            return real_receive(end, message)

        with unittest.mock.patch.object(hc.DaemonEnd, 'receive', slow):
            answer = units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual((answer['type'], answer['value']), ('result', 1))
        self.assertEqual(units.degraded, {})
        self.assertEqual(len(hosts.started), 1)
        self.answered_by(units, hosts, 1)

    def test_a_slow_core_request_does_not_count_against_the_hosts_deadline(self):
        hosts = Hosts(self, ['core'])
        units = self.units(hosts, SlowCore(), call_deadline_s=DEADLINE_S, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        began = time.monotonic()
        answer = units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual((answer['type'], answer['value']), ('result', 1))
        self.assertGreaterEqual(time.monotonic() - began, SLOW_S + HOST_S)  # past the deadline, by the core's time
        self.assertEqual(units.degraded, {})
        self.assertEqual(len(hosts.started), 1)

    def test_the_hosts_own_time_still_counts(self):
        hosts = Hosts(self, ['hang', 'answers'])
        units = self.units(hosts, call_deadline_s=DEADLINE_S, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as raised:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual(raised.exception.reason, 'timed_out')
        self.assertEqual(units.degraded, {'stand.in': 1})


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class AFrameAlreadyWrittenIsTakenBeforeATimeout(unittest.TestCase):
    def unit(self):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return host_units.HostUnit('stand.in', 'recursion-host-0000000000000000.service', ours,
                                   hs.StandInProcess(), None), theirs

    def test_a_frame_on_the_channel_when_the_deadline_has_passed_is_returned(self):
        unit, theirs = self.unit()
        result = {'type': 'result', 'id': 1, 'value': 7}
        theirs.sendall(hc.encode(hs.HELLO) + hc.encode(result))
        self.assertEqual(unit.receive(time.monotonic() - 1), hs.HELLO)
        self.assertEqual(unit.receive(time.monotonic() - 1), result)
        with self.assertRaises(TimeoutError):
            unit.receive(time.monotonic() - 1)

    def test_a_frame_only_partly_written_by_the_deadline_is_a_timeout(self):
        unit, theirs = self.unit()
        theirs.sendall(hc.encode(hs.HELLO)[:-1])
        with self.assertRaises(TimeoutError):
            unit.receive(time.monotonic() - 1)

    def test_the_end_of_the_channel_read_after_the_deadline_is_the_end(self):
        unit, theirs = self.unit()
        theirs.shutdown(socket.SHUT_WR)
        self.assertIsNone(unit.receive(time.monotonic() - 1))


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class ACallTheChannelRefusesFailsAlone(_Units):
    def assert_refused_alone(self, units, hosts, args, code):
        with self.assertRaises(host_units.CallRefused) as raised:
            units.call('stand.in', 'operator', args)
        refused = raised.exception
        self.assertEqual((refused.plugin_id, refused.reason, refused.code), ('stand.in', 'call_invalid', code))
        self.assertIn(code, hc.REFUSAL_CODES)
        for words in ('stand.in: call_invalid: the call was not sent, because the host channel refuses it',
                      'The host is still running: correct the call and send it again'):
            self.assertIn(words, str(refused))
        self.assertEqual(units.degraded, {})
        self.assertFalse([m for m in hosts.seen[0] if m['type'] == 'call'], 'the refused call reached the host')
        self.answered_by(units, hosts, 1)  # the SAME host answers the next call

    def test_an_argument_name_the_channel_refuses_fails_that_call_and_the_host_stays(self):
        hosts = Hosts(self, ['answers'])
        units = self.units(hosts, call_deadline_s=DEADLINE_S, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        self.assert_refused_alone(units, hosts, {'BAD KEY': 1}, 'message_invalid')

    def test_an_argument_that_cannot_be_written_as_json_fails_that_call_and_the_host_stays(self):
        hosts = Hosts(self, ['answers'])
        units = self.units(hosts, call_deadline_s=DEADLINE_S, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        self.assert_refused_alone(units, hosts, {'command': {1, 2}}, 'message_invalid')

    def test_an_answer_of_the_daemons_that_the_channel_refuses_reaches_the_host_as_a_refusal(self):
        hosts = Hosts(self, ['core'])
        too_large = SlowCore(answer=b'x' * hc.MAX_FRAME_BYTES, took=0)
        units = self.units(hosts, too_large, call_deadline_s=5, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        answer = units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual((answer['type'], answer['value']), ('result', 1))
        refusals = [m for m in hosts.seen[0] if m['type'] == 'refusal']
        self.assertEqual([(m['id'], m['code']) for m in refusals], [(1, 'message_invalid')])
        self.assertIn('frame_too_large', refusals[0]['detail'])
        self.assertEqual(units.degraded, {})
        self.assertEqual(len(hosts.started), 1)


if __name__ == '__main__':
    unittest.main()
