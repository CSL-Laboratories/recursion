"""ADV2 red-team: the daemon GUI's O7 reads, its door and its one venue filter, over real loopback HTTP.

PRODUCT.md d84cdd26 (CSLR-D-012) 3.4 S6: Stop is the #916 procedure "on every surface including the
GUI and TUI"; 3.5: "The GUI never grows its own order path or its own Stop"; O7's operator rules:
"Nothing is hidden: refusal, uncertainty, stale and unreconciled states stay visible as text".
The transport under test is the daemon's own ``DaemonControl.gui_transport`` over a real
``PluginHost`` (unsigned plugin, installer grant, SIMULATED GIL probe and verification backend as in
test_adv2_plugins.py) or over a fake plugin host where only the answer matters.
"""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib.parse import urlencode

from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import gui as daemon_gui
from recursion_core.daemon import plugins
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import ACTIONS, ControlClient
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the plugin host and the device door check ownership by uid'
READ = 'operator.positions.inspect'
HUNG_SOURCE = b'''
import threading
GATE = threading.Event()
CALLS = []
def operator(command):
    CALLS.append(command["command"])
    GATE.wait(30)
    return {"api_version": "o7.operator.v0", "command": command["command"], "status_code": "ok", "result": {}}
'''


class StopRecorder:
    """Stands in for the control API's ``call`` behind ``gui_transport``: records each Stop."""

    def __init__(self):
        self.stops = []

    def __call__(self, method, params=None):
        self.stops.append(params)
        return {'code': 'pending', 'run_id': params.get('run_id'), 'state': 'stop_pending'}


def control_over(plugin_host, routes=()):
    """An object with what ``DaemonControl.gui_transport`` reads: ``call``, ``route_records``, ``plugins``."""
    fake = types.SimpleNamespace(call=StopRecorder(), plugins=plugin_host, route_records=lambda: list(routes))
    return fake, (lambda method, path, body: adapter.DaemonControl.gui_transport(fake, method, path, body))


class FakeOperatorHost:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def operator(self, command):
        self.calls.append(command)
        return 200, self.answer


class GuiCase(unittest.TestCase):
    def get(self, gui, path, headers=None, timeout=5):
        conn = HTTPConnection(*gui.address, timeout=timeout)
        try:
            conn.request('GET', path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def post(self, gui, path, form, headers=None, timeout=5):
        body = urlencode(form)
        hdrs = {'Origin': 'http://127.0.0.1:%d' % gui.address[1], 'Content-Type': 'application/x-www-form-urlencoded'}
        hdrs.update(headers or {})
        conn = HTTPConnection(*gui.address, timeout=timeout)
        try:
            conn.request('POST', path, body=body, headers=hdrs)
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2HungPluginTests(GuiCase):
    """ADV2-3: an operator plugin that never answers."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-gui-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        config = self.tmp / 'config' / 'recursion'
        fx.write_install(config, grants=[fx.grant_for(HUNG_SOURCE)])
        fx.write_plugin(config / 'plugins' / 'hung', HUNG_SOURCE)
        self.host = plugins.PluginHost(config, self.tmp / 'state', gil_probe=FakeGilProbe(),
                                       verification_available=lambda: True)
        self.addCleanup(self.host.close)
        self.module = self.host.loader._active['fixture.operator'].module
        self.addCleanup(self.module.GATE.set)  # always let the plugin go, so no thread outlives the test

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)
        return predicate()

    def test_a_second_read_is_not_held_forever_behind_a_hung_plugin(self):
        first = threading.Thread(target=self.host.operator, args=({'command': READ, 'request_id': 'r1'},), daemon=True)
        first.start()
        self.assertTrue(self.wait_for(lambda: self.module.CALLS))
        answers = []
        second = threading.Thread(target=lambda: answers.append(self.host.operator({'command': 'operator.health.inspect'})),
                                  daemon=True)
        second.start()
        second.join(3)
        self.assertFalse(second.is_alive(), 'the second O7 read is still waiting on the hung plugin after 3 s')

    def test_a_timed_out_call_answers_504_marks_the_plugin_degraded_and_no_second_call_is_made(self):
        host = plugins.PluginHost(self.tmp / 'config' / 'recursion', self.tmp / 'state', gil_probe=FakeGilProbe(),
                                  verification_available=lambda: True, operator_deadline_s=0.3)
        self.addCleanup(host.close)
        module = host.loader._active['fixture.operator'].module
        self.addCleanup(module.GATE.set)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            status, body = host.operator({'command': READ})
        self.assertEqual((status, body), (504, {'error': plugins.OPERATOR_TIMED_OUT}))
        started = time.monotonic()
        status, body = host.operator({'command': READ})
        self.assertLess(time.monotonic() - started, 0.2, 'a read into a stuck plugin is answered at once')
        self.assertEqual(status, 504)
        self.assertIn('no new call was made', body['error'])
        self.assertEqual(module.CALLS, [READ], 'no second call into the stuck plugin')
        self.assertIn('a call has still not returned', host.inventory()['plugins'][0]['degraded'])
        self.assertIn('degraded', host.route_records()[0]['blocker'])
        with self.assertLogs('recursion_core.daemon.plugins', 'WARNING'):
            module.GATE.set()
            self.assertTrue(self.wait_for(lambda: host._stuck is None))
        self.assertEqual(host.operator({'command': READ})[0], 200)
        self.assertEqual(host.inventory()['plugins'][0]['degraded'], 'operator() passed its 0.3 s deadline 1 time(s)')

    def test_gui_stop_still_answers_while_the_operator_plugin_hangs(self):
        fake, transport = control_over(self.host)
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            readers = [threading.Thread(target=self._read_quietly, args=(gui,), daemon=True)
                       for _ in range(daemon_gui.MAX_REQUEST_THREADS)]
            for reader in readers:
                reader.start()
            self.assertTrue(self.wait_for(lambda: len(gui.server._working) == daemon_gui.MAX_REQUEST_THREADS),
                            'the readers did not all reach the plugin')
            try:
                status, page = self.post(gui, '/stop', {'run_id': 'run-1', 'confirm': 'explicit'}, timeout=5)
                outcome = (status, 'stop_pending' in page)
            except OSError as exc:  # the GUI shut the connection: every thread is waiting on the plugin
                outcome = ('refused', type(exc).__name__)
            finally:
                self.module.GATE.set()
                for reader in readers:
                    reader.join(10)
        self.assertEqual(outcome, (200, True), f'GUI Stop while the operator plugin hangs: {outcome}')
        self.assertEqual(fake.call.stops, [{'run_id': 'run-1'}])

    def test_stops_reserved_threads_answer_only_stop(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class BlockedHost:
            def operator(self, command):
                release.wait(30)
                return 200, {'status_code': 'ok', 'result': {}}

        fake, transport = control_over(BlockedHost())
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            readers = [threading.Thread(target=self._read_quietly, args=(gui,), daemon=True)
                       for _ in range(daemon_gui.MAX_REQUEST_THREADS)]
            for reader in readers:
                reader.start()
            try:
                self.assertTrue(self.wait_for(lambda: len(gui.server._working) == daemon_gui.MAX_REQUEST_THREADS))
                status, _ = self.get(gui, '/read?command=' + READ)
                self.assertEqual(status, 503)  # a read is not answered on Stop's reserve
                status, page = self.post(gui, '/stop', {'run_id': 'run-2', 'confirm': 'explicit'})
                self.assertEqual((status, 'stop_pending' in page), (200, True))
            finally:
                release.set()
                for reader in readers:
                    reader.join(10)
        self.assertEqual(fake.call.stops, [{'run_id': 'run-2'}])

    def test_evicted_buffered_connections_cannot_bypass_max_request_threads(self):
        import socket
        gate = threading.Event()
        self.addCleanup(gate.set)
        lock = threading.Lock()
        active = [0]
        peak = [0]

        class CountingHost:
            def operator(self, command):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                gate.wait(10)
                with lock:
                    active[0] -= 1
                return 200, {'status_code': 'ok', 'result': {}}

        _, transport = control_over(CountingHost())
        total = daemon_gui.MAX_REQUEST_THREADS + 6
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            port = gui.address[1]
            orig = gui.server.headers_read
            seen = [0]
            arrived = threading.Event()

            def sync_headers(req, method, path):
                with lock:
                    seen[0] += 1
                    if seen[0] >= total:
                        arrived.set()
                arrived.wait(2.0)
                return orig(req, method, path)

            gui.server.headers_read = sync_headers
            threads = []
            for _ in range(total):
                def hit():
                    try:
                        s = socket.create_connection(('127.0.0.1', port), timeout=5)
                        s.sendall(f'GET /read?command={READ} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
                        s.recv(4096)
                        s.close()
                    except OSError:
                        pass
                t = threading.Thread(target=hit, daemon=True)
                t.start()
                threads.append(t)
                time.sleep(0.01)
            self.wait_for(lambda: seen[0] >= total, seconds=3)
            time.sleep(0.2)
            observed_peak = peak[0]
            gate.set()
            for t in threads:
                t.join(5)
        self.assertLessEqual(observed_peak, daemon_gui.MAX_REQUEST_THREADS)

    def test_a_read_thread_that_cannot_be_started_does_not_leave_the_plugin_host_busy(self):
        """The host is marked busy before the thread that makes the read is started. When that thread
        cannot be started the call raises, and the mark has to be cleared: the next read is served,
        not refused as busy for good."""
        from unittest import mock
        self.module.GATE.set()  # the plugin answers at once; nothing here waits on it
        self.host._operator_turn_wait_s = 0.05  # how long a read waits for its turn before it answers busy
        with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError('cannot start new thread')):
            with self.assertRaises(RuntimeError):
                self.host.operator({'command': READ})
        self.assertEqual(self.module.CALLS, [], 'the read whose thread never started reached the plugin')
        status, body = self.host.operator({'command': READ})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.module.CALLS, [READ])

    def _read_quietly(self, gui):
        try:
            self.get(gui, '/read?command=' + READ, timeout=40)
        except OSError:
            pass

    def test_control_gui_stop_answers_when_the_plugin_is_idle(self):
        fake, transport = control_over(self.host)
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            status, page = self.post(gui, '/stop', {'run_id': 'run-1', 'confirm': 'explicit'})
        self.assertEqual(status, 200)
        self.assertIn('stop_pending', page)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2EvictedStopTests(GuiCase):
    """A connection still among the readers when their room is full is closed to make way for a new
    one. Its request may already be in the daemon's hands by then. A read on it is dropped (the test
    above this class); a Stop that has arrived is carried out, and counted like any other Stop."""

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)
        return predicate()

    def stop_bytes(self, port, sent, length=None):
        """A ``POST /stop`` whose body is ``sent``; ``length`` is the Content-Length when it says more."""
        length = len(sent) if length is None else length
        return (f'POST /stop HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n'
                f'Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {length}\r\n\r\n').encode() + sent

    def evict_a_held_stop(self, gui, payload):
        """Send ``payload``, hold its thread where its headers have just been read, and open quiet
        connections until that connection is the one evicted. Then let the thread go on and wait
        until it is done with the request."""
        import socket
        port = gui.address[1]
        real = gui.server.headers_read
        at_gate, go, seen = threading.Event(), threading.Event(), {}
        self.addCleanup(go.set)

        def held(request, method, path):
            if method != 'POST' or 'request' in seen:
                return real(request, method, path)
            seen['request'] = request
            at_gate.set()
            go.wait(30)
            seen['answer'] = real(request, method, path)
            return seen['answer']

        gui.server.headers_read = held
        client = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.addCleanup(client.close)
        client.sendall(payload)
        self.assertTrue(at_gate.wait(10), 'the Stop never reached headers_read')
        for _ in range(4 * daemon_gui.MAX_REQUEST_THREADS):
            if seen['request'] not in gui.server._reading:
                break
            quiet = socket.create_connection(('127.0.0.1', port), timeout=5)  # connects and sends nothing
            self.addCleanup(quiet.close)
            time.sleep(0.02)
        self.assertNotIn(seen['request'], gui.server._reading, 'the Stop was never evicted: nothing was tested')
        go.set()
        # Taken or not, the thread is done once headers_read has answered and the request is not being worked on.
        self.assertTrue(self.wait_for(lambda: 'answer' in seen and seen['request'] not in gui.server._working),
                        'the thread of the evicted connection did not finish')

    def test_a_stop_that_had_arrived_is_carried_out_when_its_connection_is_evicted(self):
        for busy in (False, True):
            with self.subTest(every_request_thread_is_answering=busy):
                release = threading.Event()
                self.addCleanup(release.set)

                class BlockedHost:
                    def operator(self, command):
                        release.wait(30)
                        return 200, {'status_code': 'ok', 'result': {}}

                def read_quietly(gui):
                    try:
                        self.get(gui, '/read?command=' + READ, timeout=40)
                    except OSError:
                        pass

                fake, transport = control_over(BlockedHost())
                with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
                    readers = [threading.Thread(target=read_quietly, args=(gui,), daemon=True)
                               for _ in range(daemon_gui.MAX_REQUEST_THREADS if busy else 0)]
                    for reader in readers:
                        reader.start()
                    try:
                        self.assertTrue(self.wait_for(lambda: len(gui.server._working) == len(readers)))
                        body = b'run_id=run-evicted&confirm=explicit'
                        self.evict_a_held_stop(gui, self.stop_bytes(gui.address[1], body))
                        self.assertEqual(fake.call.stops, [{'run_id': 'run-evicted'}],
                                         'a Stop the daemon had received was dropped with its connection')
                    finally:
                        release.set()
                        for reader in readers:
                            reader.join(10)

    def test_evicted_stops_are_counted_under_the_allowance_stop_has(self):
        import socket
        gate, lock = threading.Event(), threading.Lock()
        self.addCleanup(gate.set)
        active, peak, taken = [0], [0], [0]

        def call(method, params=None):
            with lock:
                active[0] += 1
                taken[0] += 1
                peak[0] = max(peak[0], active[0])
            gate.wait(20)
            with lock:
                active[0] -= 1
            return {'code': 'pending', 'run_id': (params or {}).get('run_id'), 'state': 'stop_pending'}

        fake = types.SimpleNamespace(call=call, plugins=FakeOperatorHost({}), route_records=lambda: [])
        allowance = daemon_gui.MAX_REQUEST_THREADS + daemon_gui.STOP_RESERVED_THREADS
        connections = allowance + 12  # so the oldest ones are evicted with their Stop already read
        with LoopbackGUI(ControlClient(lambda method, path, body: adapter.DaemonControl.gui_transport(
                fake, method, path, body)), lambda env: True, port=0) as gui:
            port = gui.address[1]
            real = gui.server.headers_read
            go, arrived = threading.Event(), [0]
            self.addCleanup(go.set)

            def all_together(request, method, path):
                with lock:
                    arrived[0] += 1
                go.wait(30)
                return real(request, method, path)

            gui.server.headers_read = all_together
            for number in range(connections):
                client = socket.create_connection(('127.0.0.1', port), timeout=5)
                self.addCleanup(client.close)
                client.sendall(self.stop_bytes(port, b'run_id=run-%d&confirm=explicit' % number))
                # one at a time: every Stop is in the daemon's hands before a later connection evicts it
                self.assertTrue(self.wait_for(lambda: arrived[0] > number), 'a Stop never reached headers_read')
            go.set()
            self.assertTrue(self.wait_for(lambda: taken[0] >= allowance),
                            'fewer Stops were carried out than Stop has room for: evicted ones were dropped')
            time.sleep(0.3)  # a Stop taken past the allowance would be inside the control API by now
            observed = peak[0]
            gate.set()
        self.assertEqual(observed, allowance)

    def test_a_stop_cut_short_by_the_eviction_names_no_run(self):
        whole = b'confirm=explicit&run_id=run-1234567890'
        sent = whole[:-8]  # ends inside the run id: "...run_id=run-12" is a well-formed Stop of another run
        fake, transport = control_over(FakeOperatorHost({}))
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            self.evict_a_held_stop(gui, self.stop_bytes(gui.address[1], sent, length=len(whole)))
        self.assertEqual(fake.call.stops, [], 'a Stop whose body had not all arrived was carried out')


class Adv2ContentLengthTests(GuiCase):
    """A request says once how long its body is. With two Content-Length headers the server read the
    first, so a shorter first header made a prefix of the body the whole form: "...run_id=run-12" out
    of "...run_id=run-1234567890", with the page's own length check satisfied. A request that names
    its length more than once is refused before the page sees it."""

    WHOLE = b'confirm=explicit&run_id=run-1234567890'

    def post_raw(self, gui, lengths, body):
        """POST /stop with one Content-Length header per entry of ``lengths``; the status line that came back."""
        import socket
        port = gui.address[1]
        head = (f'POST /stop HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n'
                'Content-Type: application/x-www-form-urlencoded\r\n'
                + ''.join(f'Content-Length: {length}\r\n' for length in lengths) + '\r\n').encode()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
            client.sendall(head + body)
            client.shutdown(socket.SHUT_WR)
            answer = b''
            while chunk := client.recv(65536):
                answer += chunk
        return answer.split(b'\r\n', 1)[0].decode()

    def test_a_request_with_more_than_one_content_length_is_refused(self):
        whole = len(self.WHOLE)
        fake, transport = control_over(FakeOperatorHost({}))
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            # The shorter one first (it named run-12), the longer one first, and the same one twice.
            for lengths in ((whole - 8, whole), (whole, whole - 8), (whole, whole)):
                with self.subTest(content_lengths=lengths):
                    status = self.post_raw(gui, lengths, self.WHOLE)
                    self.assertEqual(status.split()[1], '400', status)
                    self.assertEqual(fake.call.stops, [], 'a request with two lengths was acted on')
            # One length, for contrast: the same bytes are a Stop of the run they name.
            self.assertEqual(self.post_raw(gui, (whole,), self.WHOLE).split()[1], '200')
            self.assertEqual(fake.call.stops, [{'run_id': 'run-1234567890'}])
            # A refused request holds no place among the readers or the answering threads afterwards.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (gui.server._reading or gui.server._working):
                time.sleep(0.02)
            self.assertEqual((len(gui.server._reading), len(gui.server._working)), (0, 0))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2DoorTests(GuiCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-door-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.door = adapter.DeviceDoor(self.tmp / 'rt')
        self.addCleanup(self.door.close)
        self.token = self.door.path.read_text().strip()
        self.op = FakeOperatorHost({'status_code': 'ok', 'result': {}})
        self.fake, transport = control_over(self.op)
        self.client = ControlClient(transport)

    def test_every_path_needs_the_device_token_and_the_host_and_origin(self):
        with LoopbackGUI(self.client, self.door, port=0) as gui:
            host = '127.0.0.1:%d' % gui.address[1]
            paths = ['/', '/read?command=' + READ, '/panel?id=x', '/nope']
            for path in paths:
                self.assertEqual(self.get(gui, path)[0], 401, path)
                self.assertEqual(self.get(gui, path + ('&' if '?' in path else '?') + 'token=' + self.token)[0], 401)
                self.assertEqual(self.get(gui, path, {'Authorization': 'Bearer ' + self.token[:-1] + 'x'})[0], 401)
                self.assertEqual(self.get(gui, path, {'Authorization': 'Bearer ' + self.token, 'Host': 'localhost'})[0], 403)
                self.assertEqual(self.get(gui, path, {'Authorization': 'Bearer ' + self.token,
                                                      'Origin': 'http://evil.example'})[0], 403)
                self.assertEqual(self.get(gui, path, {'Cookie': 'recursion_device=' + self.token,
                                                      'Sec-Fetch-Site': 'cross-site'})[0], 403)
            for path, form in (('/stop', {'run_id': 'run-1', 'confirm': 'explicit'}),
                               ('/action', {'command': ACTIONS[0], 'confirm': 'explicit'})):
                self.assertEqual(self.post(gui, path, form)[0], 401)
                self.assertEqual(self.post(gui, path, form, {'Authorization': 'Bearer ' + self.token,
                                                             'Origin': 'null'})[0], 403)
                self.assertEqual(self.post(gui, path, form, {'Authorization': 'Bearer ' + self.token,
                                                             'Origin': 'http://%s, http://evil' % host})[0], 403)
            self.assertEqual(self.fake.call.stops, [])
            self.assertEqual(self.op.calls, [])
            ok = self.get(gui, '/read?command=' + READ, {'Authorization': 'Bearer ' + self.token})
            self.assertEqual(ok[0], 200)
            self.assertEqual(len(self.op.calls), 1)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2RenderingTests(GuiCase):
    PANEL = {'id': 'adv2.kalshi', 'title': 'Kalshi <script>alert(1)</script>', 'reads': [READ], 'venue': 'kalshi'}

    def gui_for(self, answer, routes=()):
        op = FakeOperatorHost(answer)
        _, transport = control_over(op, routes)
        return LoopbackGUI(ControlClient(transport), lambda env: True, plugins=[{'panels': [self.PANEL]}], port=0)

    def test_html_in_panel_titles_route_rows_and_answers_is_escaped(self):
        rows = [{'id': 'plugin:x', 'class': 'plugin', 'sign_in': '<img src=x onerror=alert(2)>', 'configured': False,
                 'maturity': 'beta', 'beta': True, 'bars': {}, 'blocker': 'refused: <script>alert(3)</script>'},
                {'id': '<b>bad id</b>', 'class': '<i>c</i>', 'sign_in': '', 'configured': True, 'maturity': 'beta',
                 'beta': True, 'bars': {}, 'blocker': ''}]
        answer = {'status_code': '<script>alert(4)</script>', 'result': {'note': '</pre><script>alert(5)</script>'}}
        with self.gui_for(answer, rows) as gui:
            pages = [self.get(gui, '/')[1], self.get(gui, '/panel?id=adv2.kalshi')[1], self.get(gui, '/read?command=' + READ)[1]]
        for page in pages:
            self.assertNotIn('<script', page)
            self.assertNotIn('<img', page)
            self.assertNotIn('<b>bad', page)

    def test_venue_filter_withholds_every_lookalike_label(self):
        own = {'venue': 'kalshi', 'symbol': 'OWN-ROW', 'legs': [{'venue': 'other', 'symbol': 'LEAK-NESTED'}]}
        rows = [own, {'venue': 'KALSHI', 'symbol': 'LEAK-CASE'}, {'venue': 'kalshі', 'symbol': 'LEAK-HOMOGLYPH'},
                {'venue': None, 'symbol': 'LEAK-NONE'}, {'venue': ['kalshi'], 'symbol': 'LEAK-LIST'},
                {'venue': {'id': 'kalshi'}, 'symbol': 'LEAK-DICT'}, {'symbol': 'LEAK-NOLABEL'},
                {'venue': 'kalshi ', 'symbol': 'LEAK-SPACE'}, {'venue': 'kalshi\x00', 'symbol': 'LEAK-NUL'},
                'LEAK-SCALAR', ['LEAK-INNER-LIST']]
        answer = {'status_code': 'ok', 'result': {'positions': rows, 'total': 'LEAK-TOTAL',
                                                   'kalshi_extra': {'symbol': 'LEAK-PREFIX'}}}
        with self.gui_for(answer) as gui:
            status, page = self.get(gui, '/panel?id=adv2.kalshi')
        self.assertEqual(status, 200)
        self.assertIn('OWN-ROW', page)
        self.assertIn('Withheld', page)
        for leak in ('LEAK-NESTED', 'LEAK-CASE', 'LEAK-HOMOGLYPH', 'LEAK-NONE', 'LEAK-LIST', 'LEAK-DICT', 'LEAK-NOLABEL',
                     'LEAK-SPACE', 'LEAK-NUL', 'LEAK-SCALAR', 'LEAK-INNER-LIST', 'LEAK-TOTAL', 'LEAK-PREFIX'):
            self.assertNotIn(leak, page)

    def test_venue_filter_on_a_top_level_label(self):
        for answer in ({'venue': 'other', 'status_code': 'ok', 'result': {'symbol': 'LEAK-TOP'}},
                       {'venue': 'kalshi', 'status_code': 'ok', 'result': {'rows': [{'venue': 'other', 'symbol': 'LEAK-UNDER-OWN'}]}}):
            with self.gui_for(answer) as gui:
                page = self.get(gui, '/panel?id=adv2.kalshi')[1]
            self.assertNotIn('LEAK-', page)

    def test_venue_panel_does_not_print_unfiltered_status_text(self):
        answer = {'status_code': 'LEAK-OTHER-VENUE-ROW qty=2', 'result': {'positions': [{'venue': 'other', 'symbol': 'x'}]}}
        with self.gui_for(answer) as gui:
            page = self.get(gui, '/panel?id=adv2.kalshi')[1]
        self.assertNotIn('LEAK-OTHER-VENUE-ROW', page)
        self.assertIn("withheld by this panel&#x27;s venue filter", page)
        for status in ('ok', 'refused'):  # O7's fixed status strings stay visible on a venue panel (3.5)
            answer = {'status_code': status, 'result': {'positions': [{'venue': 'other', 'symbol': 'x'}]}}
            with self.gui_for(answer) as gui:
                page = self.get(gui, '/panel?id=adv2.kalshi')[1]
            self.assertIn('<strong>%s</strong>' % status, page)

    def test_a_huge_answer_is_rendered_whole(self):
        # No size bound on an O7 answer: observed, not a refusal. 2 MB in, more than 2 MB out.
        answer = {'status_code': 'ok', 'result': {'blob': 'A' * 2_000_000}}
        with self.gui_for(answer) as gui:
            status, page = self.get(gui, '/read?command=' + READ, timeout=30)
        self.assertEqual(status, 200)
        self.assertGreater(len(page), 2_000_000)

    # ADV2-9 (owner call Nov 11 2:20am PST): a typed refusal from the daemon reads "Not sent".
    def test_a_refused_intervention_does_not_read_as_maybe_sent(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)  # only operator()'s ACTIONS branch runs
        _, transport = control_over(host)
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            status, page = self.post(gui, '/action', {'command': ACTIONS[0], 'confirm': 'explicit'})
        self.assertEqual(status, 200)
        self.assertIn('<p role="status">Not sent: refused by the daemon (unavailable: operator interventions are '
                      'not wired to the daemon; nothing was sent); run Stop is available</p>', page)
        self.assertEqual(page.count('run Stop is available'), 1)  # the GUI says it; the daemon's reason does not
        self.assertIn('<title>Not sent', page)
        self.assertNotIn('may have reached', page)
        self.assertNotIn('outcome: unknown', page)
        self.assertNotIn('outcome unknown', page)

    def test_a_transport_failure_on_an_intervention_still_reads_outcome_unknown(self):
        sent = []

        def transport(method, path, body):
            sent.append(path)
            raise ConnectionResetError('the answer never came back')
        with LoopbackGUI(ControlClient(transport), lambda env: True, port=0) as gui:
            status, page = self.post(gui, '/action', {'command': ACTIONS[0], 'confirm': 'explicit'})
        self.assertEqual((status, sent), (200, ['/v1/operator']))
        self.assertIn('Intervention outcome: unknown', page)
        self.assertIn('may have reached', page)
        self.assertNotIn('Not sent', page)
        self.assertNotIn('refused by the daemon', page)


if __name__ == '__main__':
    unittest.main()
