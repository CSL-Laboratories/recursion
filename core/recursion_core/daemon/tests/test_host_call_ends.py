"""How a call ends: a host ended by Stop during a call that is answered, a frame cut short at the end of the channel,
and a start inside a call that fails. Every host is a thread of the test; no unit, no process, no plugin code."""
from __future__ import annotations

import socket
import subprocess
import threading
import time
import unittest

from recursion_core.control_transport.framing import HEADER
from recursion_core.daemon import host_units
from recursion_core.daemon.tests import test_host_start as hs
from recursion_core.daemon.tests import test_host_unit_calls as tc
from recursion_core.plugin_security import host_channel as hc

JOIN_S = hs.JOIN_S
CALL_S = 0.5     # the call deadline of the stalled-read test (a test figure)
STOP_S = 0.2     # its Stop bound (a test figure)
STALL_S = 3.0    # how long the stalled stand-in's read of a unit takes when nothing bounds it (a test figure)
SLACK_S = 1.0    # how far past the call's deadline that call may end (a test figure)


class StalledIdentity(tc.Hosts):
    """Hosts whose user manager stalls on every read of a unit's identity but the first host's: a read with a bound
    takes the whole bound and then times out, as subprocess does; a read with none takes STALL_S."""

    def __init__(self, test, modes):
        super().__init__(test, modes)
        self.bounds = []

    def unit_identity(self, unit, timeout_s=None):
        if unit == self.started[0]:
            return super().unit_identity(unit)
        self.bounds.append(timeout_s)
        if timeout_s is None:
            time.sleep(STALL_S)
            return super().unit_identity(unit)
        time.sleep(min(max(timeout_s, 0.0), STALL_S))
        raise subprocess.TimeoutExpired(['systemctl'], timeout_s)


class ScriptedHosts(tc.Hosts):
    """Hosts whose answer to a call is ``script(end, message)``. The kill leaves the channel as it is (the bytes the
    host wrote before it stay readable, as after a real kill); ``hang_up_all`` ends every channel."""

    def __init__(self, test, script):
        super().__init__(test, ['scripted'])
        self.script = script

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= hs.HOSTS_PER_TEST:
            raise AssertionError('this test started more hosts than its count allows')
        self.started.append(unit)
        seen = []
        end = channel.dup()
        self.ends.append((unit, end))

        def host():
            try:
                end.sendall(hc.encode(hs.HELLO))
                for message in tc.frames(end, seen, self.stop):
                    if message['type'] == 'call':
                        self.called.set()
                        self.script(end, message)
            except (OSError, StopIteration):
                pass

        threading.Thread(target=host, daemon=True).start()
        return hs.StandInProcess()

    def kill_cgroup(self, group, unit):
        self.killed = getattr(self, 'killed', []) + [unit]
        return True


class HookCore:
    """The core service whose answer to a core request first runs ``hook``."""

    def __init__(self, hook):
        self.hook = hook

    def client_for(self, plugin_id):
        def answer(request):
            self.hook()
            return b'{}'
        return answer


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class ACallAnsweredAfterItsHostWasEnded(tc._Units):
    def test_the_daemons_stop_during_a_call_that_is_answered_leaves_no_channel_open(self):
        def script(end, message):  # a core request, then at once the result
            end.sendall(hc.encode({'type': 'core', 'id': 1, 'bytes_b64': hs.b64(b'{}')}))
            end.sendall(hc.encode({'type': 'result', 'id': message['id'], 'value': 1}))

        hosts = ScriptedHosts(self, script)
        box = {}
        units = self.units(hosts, HookCore(lambda: box.setdefault('units').close()), call_deadline_s=5,
                           start_deadline_s=5)
        box['units'] = units
        host = units.start('stand.in', *hs.package(hs.POLITE))
        answer = units.call('stand.in', 'operator', {'command': 'x'})  # the core request runs the daemon's Stop
        self.assertEqual((answer['type'], answer['value']), ('result', 1))
        self.assertEqual(hosts.killed, [host.unit])
        self.assertEqual(host._sock.fileno(), -1)  # the call closed the channel before it returned
        self.assertEqual(units._ended, [])


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class AFrameCutShort(tc._Units):
    def test_bytes_left_at_the_end_of_the_channel_are_frame_truncated_and_the_end_says_so(self):
        def script(end, message):  # a header for 100 bytes, 10 bytes, then the end of the channel
            end.sendall(HEADER.pack(100) + b'0123456789')
            end.shutdown(socket.SHUT_RDWR)

        hosts = ScriptedHosts(self, script)
        units = self.units(hosts, call_deadline_s=5, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as caught:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual(caught.exception.reason, 'ended')
        self.assertIn('message_invalid', caught.exception.detail)
        self.assertIn(f'frame_truncated: the channel ended {HEADER.size + 10} bytes into a frame',
                      caught.exception.detail)

    def test_a_whole_frame_then_the_end_is_still_the_plain_end(self):
        def script(end, message):
            end.shutdown(socket.SHUT_RDWR)

        hosts = ScriptedHosts(self, script)
        units = self.units(hosts, call_deadline_s=5, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as caught:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual((caught.exception.reason, caught.exception.detail), ('ended', ''))


@unittest.skipUnless(hs.LINUX, 'plugin host units are Linux only')
class AStartInsideACallThatFails(tc._Units):
    def test_a_silent_replacement_within_the_calls_deadline_counts_toward_degraded(self):
        hosts = tc.Hosts(self, ['hang', 'silent'])
        units = self.units(hosts, call_deadline_s=0.3, start_deadline_s=5)
        units.start('stand.in', *hs.package(hs.POLITE))
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable) as first:
                units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual((first.exception.reason, units.degraded), ('timed_out', {'stand.in': 1}))
        with self.assertRaises(host_units.HostUnavailable) as second:
            units.call('stand.in', 'operator', {'command': 'x'})
        self.assertEqual(second.exception.reason, 'not_started')
        self.assertEqual(units.degraded, {'stand.in': 2})

    def test_a_stalled_read_of_the_units_identity_ends_within_the_calls_deadline(self):
        hosts = StalledIdentity(self, ['answers', 'answers'])
        units = self.units(hosts, call_deadline_s=CALL_S, start_deadline_s=None, stop_confirm_s=STOP_S)
        first = units.start('stand.in', *hs.package(hs.POLITE))
        hosts.kill_cgroup('', first.unit)  # the host ends by itself
        with self.assertLogs(host_units.log, 'ERROR'):
            with self.assertRaises(host_units.HostUnavailable):
                units.call('stand.in', 'operator', {'command': 'x'})
        began = time.monotonic()
        with self.assertRaises(host_units.HostUnavailable) as caught:
            units.call('stand.in', 'operator', {'command': 'x'})  # starts a host; its identity read stalls
        took = time.monotonic() - began
        self.assertLess(took, CALL_S + SLACK_S)
        self.assertEqual(caught.exception.reason, 'not_started')
        self.assertIn("the system did not show the unit's control group", caught.exception.detail)
        self.assertIn('plugin_host.call_deadline_s', caught.exception.detail)
        self.assertGreater(hosts.bounds[0], 0)
        self.assertLessEqual(hosts.bounds[0], CALL_S)  # what was left of the call's deadline, no figure of its own
        self.assertEqual(units.degraded, {'stand.in': 2})
        self.assertIsNone(units.unit_of('stand.in'))


if __name__ == '__main__':
    unittest.main()
