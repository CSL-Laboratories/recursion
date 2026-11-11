"""The composed daemon: control API over the real socket, real launcher, real registry (CSL-738).

Everything here is the production code path except the service manager: ``ProcessUnitManager``
starts the unit's argv as a plain child process instead of asking ``systemd-run``, so the
trampoline, the FIFO environment, the route adapter, the control API, the durable registry, the
Unix socket, the CLI and the GUI door all run for real with the stand-in engine. The systemd
calls themselves are checked as argv here and against a real user manager by
``evidence_e2e.py`` (and by ``RealUserManagerTest`` where one is running).
"""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control import cli
from recursion_core.control.client import SocketAPI
from recursion_core.control.registry import EMPTY, POPULATED, TIMEOUT, UNKNOWN, StaleGeneration, StopUnattested
from recursion_core.control_transport import TransportBusy
from recursion_core.daemon import Daemon, Registry, Run, UnitNotLoaded
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import engine_exec, launcher
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.launch import ReadTimeout
from recursion_core.routes.tests.fakes import FakeClock, request

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the composed daemon needs pidfd, FIFO stdio, AF_UNIX peer credentials and systemd '
              'user units; on Windows the launcher, compose_control and the daemon entry point refuse '
              'PlatformUnsupported (platform_unsupported) and the CLI answers control_api_unavailable; '
              'covered by WindowsRefusalTests in daemon/tests and control/tests')
STANDIN = Path(launcher.__file__).with_name('evidence_standin_engine.py')
BOOT = 'boot-1'


def until(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


class Units:
    """Stands in for the user manager's unit table: it outlives a daemon restart."""

    def __init__(self):
        self.procs, self.stubborn, self.started = {}, set(), []


class ProcessUnitManager(launcher.EngineUnitManager):
    """Runs a unit's argv as a child process with the stdio the launcher asked systemd for."""

    def __init__(self, units):
        super().__init__()
        self.units = units

    def start(self, unit, argv):
        with self._pending_lock:
            properties, cwd = self._pending.pop(unit, ((), None))
        props = dict(item.split('=', 1) for item in properties)
        self.units.started.append({'unit': unit, 'argv': list(argv), 'properties': props, 'cwd': cwd})
        stdin = os.open(props['StandardInput'].removeprefix('file:'), os.O_RDONLY | os.O_NONBLOCK)
        os.set_blocking(stdin, True)
        with open(props['StandardOutput'].removeprefix('append:'), 'ab') as out, \
                open(props['StandardError'].removeprefix('append:'), 'ab') as err:
            # The manager's own environment must never reach the engine: engine_exec replaces it.
            proc = subprocess.Popen(argv, stdin=stdin, stdout=out, stderr=err, cwd=cwd, start_new_session=True,
                                    env={'MANAGER_ONLY': 'must-not-reach-the-engine'})
        os.close(stdin)
        self.units.procs[unit] = proc

    def stop(self, unit):
        proc = self.units.procs.get(unit)
        if proc is None:
            raise UnitNotLoaded(unit)
        if unit in self.units.stubborn or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def observe(self, unit):
        proc = self.units.procs.get(unit)
        if proc is None:
            return 'gone'
        return 'running' if proc.poll() is None else 'empty'

    def processes_gone(self, unit):
        proc = self.units.procs.get(unit)
        return proc is None or proc.poll() is not None

    def owned_units(self):
        return set(self.units.procs)

    def unit_identity(self, unit):
        return f'/test.slice/{unit}', self.units.procs[unit].pid

    def open_main_pidfd(self, group, pid):
        return os.pidfd_open(pid)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ComposedDaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-compose-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        (self.tmp / 'bin').mkdir(mode=0o700)
        self.engine = self.tmp / 'bin' / 'claude'
        shutil.copyfile(STANDIN, self.engine)
        os.chmod(self.engine, 0o700)
        try:  # with no user message the stand-in engine prints two lines and exits: an exec probe
            probe = subprocess.run([str(self.engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                   env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            probe = b''
        if b'stand-in engine got no user message' not in probe:
            # Not a product condition: the stand-in engine has to be executable from the temp directory.
            self.skipTest('temp directory cannot execute the stand-in engine (noexec mount or no /usr/bin/python3)')
        self.units = Units()
        self.addCleanup(self.kill_units)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config'),
                        'DAEMON_ONLY_SECRET_TOKEN': 'never-in-a-child'}
        self.routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': 'stand-in-model', 'api_key_helper': 'absent',
                      'connection': {'kind': 'subscription', 'id': 'stand-in-login'}},
            'deadline_s': 120.0, 'binary': str(self.engine)}}
        self.open = []
        self.addCleanup(self.shutdown)

    def kill_units(self):
        for proc in self.units.procs.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()

    def boot(self, **options):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes, **options)
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return daemon, composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    def api(self, composed):
        return SocketAPI(composed.server.path, timeout_s=30)

    def start(self, api, prompt='helper=0 fast=1', key=None):
        params = {'tenant': 't1', 'engine': 'claude_code', 'prompt': prompt}
        if key:
            params['client_key'] = key
        answer = api.call('run.start', params)
        self.assertEqual(answer['code'], 'started', answer)
        return answer['run']['run_id']

    def lines(self, run_id):
        path = self.state / 'runs' / run_id / 'stdout.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # -- socket round trip: start, stream, inspect, Stop ---------------------------------------
    def test_start_stream_status_and_attested_stop_over_the_socket(self):
        _, composed = self.boot()
        api = self.api(composed)
        self.assertEqual(stat.S_IMODE(os.stat(composed.server.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(Path(composed.server.path).parent).st_mode), 0o700)
        run_id = self.start(api, key='k1')
        unit = f'recursion-run-{run_id}.service'
        proc = self.units.procs[unit]
        first = until(lambda: (api.call('run.status', {'run_id': run_id})['run'].get('output') or {}).get('bytes'))
        self.assertTrue(first)
        later = until(lambda: (lambda b: b if b > first else None)(
            api.call('run.status', {'run_id': run_id})['run']['output']['bytes']))
        self.assertGreater(later, first)  # output arrives while the run is live
        status = api.call('run.status', {'run_id': run_id})['run']
        self.assertEqual((status['state'], status['observed']['state'], status['output']['complete']),
                         ('running', 'running', False))
        self.assertTrue(all(json.loads(line)['stand_in_engine'] for line in status['output']['lines']))
        self.assertEqual([r['run_id'] for r in api.call('run.list')['runs']], [run_id])
        self.assertEqual(api.call('run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 fast=1',
                                                'client_key': 'k1'})['duplicate'], True)
        self.assertEqual(len(self.units.started), 1)  # the retry launched nothing

        receipt = api.call('run.stop', {'run_id': run_id})
        self.assertEqual((receipt['code'], receipt['state'], receipt.get('recorded')), ('stopped', 'stopped', None))
        self.assertIsNotNone(proc.poll())  # the receipt came after the process was gone, not before
        self.assertEqual(self.lines(run_id)[-1]['subtype'], 'stand_in_sigint')  # the engine was told to stop
        after = api.call('run.status', {'run_id': run_id})['run']
        self.assertEqual(after['state'], 'stopped')
        self.assertEqual(after['observed']['state'], 'terminated')
        self.assertEqual((after['stop_attestation']['proof'], after['stop_attestation']['cgroup']),
                         ('empty', f'/test.slice/{unit}'))
        self.assertTrue(after['output']['complete'])
        again = api.call('run.stop', {'run_id': run_id})
        self.assertEqual((again['code'], again['repeat']), ('stopped', True))
        self.assertEqual(api.call('run.stop', {'run_id': 'run-none'})['code'], 'run_unknown')
        self.assertEqual(api.call('run.kill', {})['code'], 'method_unknown')

    def test_engine_gets_only_the_policy_environment_and_no_secret_is_in_the_unit_command(self):
        _, composed = self.boot()
        run_id = self.start(self.api(composed))
        init = until(lambda: next((l for l in self.lines(run_id) if l.get('subtype') == 'init'), None))
        home = self.state / 'homes' / 'claude_code'
        self.assertEqual(init['env_names'], ['HOME', 'LANG', 'PATH'])
        self.assertEqual(init['home'], str(home))
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        started = self.units.started[0]
        self.assertEqual(started['argv'][:4], [sys.executable, '-I', '-S', str(launcher.ENGINE_EXEC)])
        self.assertEqual(started['argv'][5:8], ['--', str(self.engine), '-p'])
        self.assertNotIn('never-in-a-child', json.dumps(started, default=str))
        self.assertFalse((self.state / 'runs' / run_id / 'env.fifo').exists())  # removed once it was read
        self.assertEqual(stat.S_IMODE(os.stat(self.state / 'runs' / run_id).st_mode), 0o700)
        self.assertEqual(started['cwd'], home)

    def test_run_that_finishes_reads_ended_with_its_route_result(self):
        daemon, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api, 'helper=0 fast=1 ticks=3')
        done = until(lambda: (lambda r: r if r.get('result') else None)(api.call('run.status', {'run_id': run_id})['run']))
        self.assertEqual((done['result']['status'], done['result']['text']),
                         ('ok', 'stand-in engine finished after 3 ticks'))
        until(lambda: daemon.reconcile(run_id).state == 'terminated')
        ended = api.call('run.status', {'run_id': run_id})['run']
        # Nobody stopped it: it is not "stopped", and it is no longer "running".
        self.assertEqual((ended['state'], ended['recorded_state'], ended['stop_attestation']), ('ended', 'running', None))
        listed = api.call('run.list')['runs'][0]
        self.assertEqual((listed['state'], listed['result']), ('ended', {'status': 'ok', 'code': None, 'detail': ''}))
        self.assertNotIn('output', listed)

    def test_stop_is_pending_until_the_tree_is_attested_gone(self):
        _, composed = self.boot(confirm_timeout_s=0.3)
        api = self.api(composed)
        run_id = self.start(api)
        unit = f'recursion-run-{run_id}.service'
        self.units.stubborn.add(unit)  # the stop request returns but the process stays
        receipt = api.call('run.stop', {'run_id': run_id})
        self.assertEqual((receipt['code'], receipt['state'], receipt['reason']), ('pending', 'pending', 'cgroup_populated'))
        self.assertIsNone(self.units.procs[unit].poll())
        status = api.call('run.status', {'run_id': run_id})['run']
        self.assertEqual((status['state'], status['stop_attestation'], status['observed']['state']),
                         ('stop_pending', None, 'stopping'))
        out = io.StringIO()
        self.assertEqual(cli.main(['stop', run_id], api, out), cli.EXIT_PENDING)
        self.units.stubborn.clear()
        composed.control.reconcile()  # the sweep's follow-up finishes the Stop through the same path
        self.assertEqual(api.call('run.status', {'run_id': run_id})['run']['state'], 'stopped')
        self.assertIsNotNone(self.units.procs[unit].poll())

    def test_start_refusals_are_typed_and_launch_nothing(self):
        self.routes['codex'] = {'route': {'engine': 'codex', 'model': 'm', 'connection': {
            'kind': 'api_key', 'id': 'key-1', 'credential_env': 'CODEX_API_KEY'}}, 'deadline_s': 30.0, 'binary': None}
        _, composed = self.boot()
        api = self.api(composed)
        no_route = api.call('run.start', {'tenant': 't1', 'engine': 'pi', 'prompt': 'x'})
        self.assertEqual((no_route['code'], no_route['start_error']['code']), ('start_failed', 'route_not_configured'))
        no_key = api.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': 'x'})
        self.assertEqual(no_key['start_error'], {'code': 'launch_refused', 'detail': 'no credential for key-1'})
        self.assertEqual(self.units.started, [])
        states = {r['state'] for r in api.call('run.list')['runs']}
        self.assertEqual(states, {'start_failed'})

    # -- CLI -------------------------------------------------------------------------------------
    def run_cli(self, argv, path):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run(argv, lambda: SocketAPI(path, timeout_s=30))
        return code, json.loads(out.getvalue())

    def test_cli_exit_codes_with_the_daemon_up_and_down(self):
        _, composed = self.boot()
        path = composed.server.path
        self.assertEqual(self.run_cli(['list'], path), (0, {'code': 'ok', 'runs': []}))
        code, started = self.run_cli(['start', '--tenant', 't1', '--engine', 'claude_code',
                                      '--prompt', 'helper=0 fast=1'], path)
        self.assertEqual((code, started['code']), (0, 'started'))
        run_id = started['run']['run_id']
        self.assertEqual(self.run_cli(['status', run_id], path)[0], 0)
        self.assertEqual(self.run_cli(['status', 'run-none'], path), (4, {'code': 'run_unknown', 'run_id': 'run-none'}))
        self.assertEqual(self.run_cli(['conditions'], path)[0], 0)
        code, receipt = self.run_cli(['stop', run_id], path)
        self.assertEqual((code, receipt['code']), (0, 'stopped'))
        self.shutdown()  # daemon down: the socket is gone
        code, answer = self.run_cli(['list'], path)
        self.assertEqual((code, answer['code'], answer['delivered']), (1, 'control_api_unavailable', False))
        code, answer = self.run_cli(['stop', run_id], path)
        self.assertEqual((code, answer['code']), (1, 'control_api_unavailable'))  # never "stopped" unanswered
        with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.runtime)}):
            from recursion_core.control import __main__ as entry
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(entry.main(['list']), 1)
            self.assertEqual(json.loads(out.getvalue())['code'], 'control_api_unavailable')

    def test_packaged_entry_point_reaches_the_running_daemon(self):
        _, composed = self.boot()
        done = subprocess.run([sys.executable, '-B', '-m', 'recursion_core.control', 'list'], text=True,
                              capture_output=True, timeout=60, check=False,
                              env={'PYTHONPATH': str(Path(launcher.__file__).parents[2]),
                                   'XDG_RUNTIME_DIR': str(self.runtime), 'PATH': '/usr/bin:/bin'})
        self.assertEqual((done.returncode, json.loads(done.stdout)), (0, {'code': 'ok', 'runs': []}))

    def test_socket_api_reports_what_it_knows_about_delivery(self):
        class Client:
            def __init__(self, error=None, reply=None):
                self.error, self.reply = error, reply

            def request(self, message):
                if self.error:
                    raise self.error
                return self.reply

            def close(self):
                pass

        def api(client):
            return SocketAPI('/nonexistent/control.sock', connect=lambda path, timeout_s: client)

        self.assertEqual(api(Client(TransportBusy('busy'))).call('run.stop', {'run_id': 'r'}),
                         {'code': 'control_api_unavailable', 'delivered': False, 'detail': 'busy'})
        lost = api(Client(TimeoutError())).call('run.stop', {'run_id': 'r'})
        self.assertEqual((lost['code'], lost['delivered']), ('control_api_unavailable', 'unknown'))
        refused = api(Client(reply={'ok': False, 'error': {'code': 'scope_refused'}})).call('run.list')
        self.assertEqual(refused, {'code': 'control_api_unavailable', 'delivered': True, 'detail': 'daemon:scope_refused'})
        self.assertEqual(api(Client(reply={'ok': True, 'result': {'code': 'pending'}})).call('run.stop'),
                         {'code': 'pending'})

    def test_a_peer_inside_a_run_unit_gets_no_control(self):
        _, composed = self.boot()
        with mock.patch.object(adapter, 'peer_is_engine', return_value=True):
            answer = self.api(composed).call('run.list')
        self.assertEqual(answer, {'code': 'control_api_unavailable', 'delivered': True, 'detail': 'daemon:scope_refused'})
        self.assertFalse(adapter.peer_is_engine(os.getpid()))
        for unreadable in (None, 0, -1, 2 ** 22 + 12345):
            self.assertTrue(adapter.peer_is_engine(unreadable))  # fail closed

    # -- GUI -------------------------------------------------------------------------------------
    def gui(self, gui, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', gui.address[1], timeout=30)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_gui_door_is_a_real_device_token_and_stop_is_the_same_stop(self):
        _, composed = self.boot()
        token_file = self.runtime / 'recursion' / 'gui-device-token'
        token = token_file.read_text().strip()
        self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
        self.assertGreaterEqual(len(token), 40)
        run_id = self.start(self.api(composed))
        with LoopbackGUI(composed.client, composed.door, port=0) as gui:
            self.assertEqual(gui.address[0], '127.0.0.1')
            bearer = {'Authorization': 'Bearer ' + token}
            for headers in ({}, {'Authorization': 'Bearer ' + token[:-1]}, {'Authorization': 'Bearer '},
                            {'Cookie': 'recursion_device=wrong'}, {'Authorization': token}):
                status, page = self.gui(gui, 'GET', '/', headers)
                self.assertEqual(status, 401, headers)
                self.assertIn('device_not_authenticated', page)
            status, page = self.gui(gui, 'GET', '/', bearer)
            self.assertEqual(status, 200)
            self.assertIn('Route inventory unavailable: control_api_501:route_inventory_not_implemented', page)
            self.assertEqual(self.gui(gui, 'GET', '/', {'Cookie': 'a=b; recursion_device=' + token})[0], 200)
            origin = 'http://127.0.0.1:%d' % gui.address[1]
            form = {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': origin}
            body = f'run_id={run_id}&confirm=explicit'
            self.assertEqual(self.gui(gui, 'POST', '/stop', form, body)[0], 401)  # no token: no Stop
            self.assertIsNone(self.units.procs[f'recursion-run-{run_id}.service'].poll())
            status, page = self.gui(gui, 'POST', '/stop', {**form, **bearer}, body)
            self.assertEqual(status, 200)
            self.assertIn('Stop state reported by the control API: <strong>stopped</strong>', page)
        self.assertEqual(self.api(composed).call('run.status', {'run_id': run_id})['run']['state'], 'stopped')
        self.shutdown()
        self.assertFalse(token_file.exists())
        self.assertFalse(adapter.DeviceDoor(self.runtime / 'recursion')({'HTTP_AUTHORIZATION': 'Bearer ' + token}))

    # -- restart ---------------------------------------------------------------------------------
    def test_restart_recovers_runs_and_reports_what_happened_while_the_daemon_was_down(self):
        _, composed = self.boot()
        api = self.api(composed)
        survivor = self.start(api, key='survivor')
        ended = self.start(api, key='ended')
        cut_short = self.start(api, key='cut-short')
        stopped = self.start(api, key='stopped')
        self.assertEqual(api.call('run.stop', {'run_id': stopped})['code'], 'stopped')
        self.shutdown()  # the daemon goes away; the units do not
        unit = 'recursion-run-%s.service'
        self.assertIsNone(self.units.procs[unit % survivor].poll())
        os.killpg(self.units.procs[unit % ended].pid, signal.SIGKILL)  # ends with no daemon watching
        self.units.procs[unit % ended].wait()
        # A Stop whose durable intent was written and whose receipt never was: the previous daemon
        # died between Daemon.stop's registry write and the control record.
        registry = Registry(self.state)
        registry.request_stop(cut_short)
        registry.close()

        daemon, composed = self.boot()
        api = self.api(composed)
        runs = {r['run_id']: r for r in api.call('run.list')['runs']}
        self.assertEqual(set(runs), {survivor, ended, cut_short, stopped})
        self.assertEqual((runs[survivor]['state'], runs[survivor]['observed']['state']), ('running', 'running'))
        self.assertEqual((runs[ended]['state'], runs[ended]['recorded_state']), ('ended', 'running'))
        self.assertEqual(runs[stopped]['state'], 'stopped')
        self.assertEqual(runs[stopped]['stop_attestation']['proof'], 'empty')
        # The sweep replayed the Stop, and the control record got its attested receipt.
        self.assertEqual(runs[cut_short]['state'], 'stopped')
        self.assertEqual(runs[cut_short]['stop_attestation']['cgroup'], f'/test.slice/{unit % cut_short}')
        self.assertIsNotNone(self.units.procs[unit % cut_short].poll())
        # Start dedupe survives the restart, and the survivor is still stoppable.
        replay = api.call('run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 fast=1',
                                        'client_key': 'survivor'})
        self.assertEqual((replay['code'], replay['duplicate'], replay['run']['run_id']), ('started', True, survivor))
        self.assertEqual(len(self.units.started), 4)
        self.assertEqual(api.call('run.stop', {'run_id': survivor})['code'], 'stopped')
        self.assertIsNotNone(self.units.procs[unit % survivor].poll())

    def test_restart_settles_a_start_the_previous_daemon_never_finished_recording(self):
        daemon, composed = self.boot()
        registry = composed.control.registry
        intent = {'run_id': 'run-neverlaunched', 'tenant': 't1', 'engine': 'claude_code', 'state': 'starting',
                  'cgroup': None, 'started_at': 1.0, 'stop': None, 'start_key': None}
        registry.put(dict(intent))
        # A launch that reached the unit but not the control record.
        launched = self.start(self.api(composed))
        row = registry.get(launched)
        row.update(state='starting', cgroup=None)
        registry.put(row)
        self.shutdown()
        _, composed = self.boot()
        api = self.api(composed)
        self.assertEqual(api.call('run.status', {'run_id': 'run-neverlaunched'})['run']['state'], 'start_failed')
        adopted = api.call('run.status', {'run_id': launched})['run']
        self.assertEqual((adopted['state'], adopted['cgroup']),
                         ('running', f'/test.slice/recursion-run-{launched}.service'))
        self.assertEqual(api.call('run.stop', {'run_id': launched})['code'], 'stopped')

    def test_unobservable_unit_is_never_reported_running(self):
        daemon, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        unit = f'recursion-run-{run_id}.service'
        proc = self.units.procs.pop(unit)  # the manager no longer knows the unit, and nothing saw it end
        self.addCleanup(lambda: (os.killpg(proc.pid, signal.SIGKILL), proc.wait()))
        daemon.sweep()
        status = api.call('run.status', {'run_id': run_id})['run']
        self.assertEqual((status['state'], status['recorded_state'], status['observed']['state']),
                         ('unknown', 'running', 'unknown'))
        receipt = api.call('run.stop', {'run_id': run_id})
        self.assertEqual(receipt['code'], 'stopped')  # stop answered "not loaded" and the unit is gone: attested
        self.units.procs[unit] = proc

    # -- durable control records -------------------------------------------------------------------
    def test_control_records_are_durable_and_keep_the_compare_and_swap(self):
        store = Registry(self.state)
        self.addCleanup(store.close)
        registry = adapter.DurableRunRegistry(store, processes=lambda pid: False)
        first = registry.put({'run_id': 'run-a', 'tenant': 't', 'engine': 'codex', 'state': 'running',
                              'cgroup': '/x/recursion-run-run-a.service', 'started_at': 1.0, 'stop': None,
                              'start_key': {'client_key': 'k', 'engine': 'codex', 'prompt_sha256': 'h'}})
        self.assertEqual(first['generation'], 1)
        with self.assertRaises(StaleGeneration):
            registry.put(dict(first, generation=0, state='starting'))
        stopped = dict(first, state='stopped', stop_attestation={
            'proof': 'empty', 'cgroup': first['cgroup'], 'cgroup_marker': first['cgroup_marker'],
            'member_ids': [], 'generation': 1})
        registry.put(stopped)
        reloaded = adapter.DurableRunRegistry(store, processes=lambda pid: False)
        self.assertEqual(reloaded.get('run-a')['state'], 'stopped')
        self.assertEqual(reloaded.get('run-a')['generation'], 2)
        self.assertEqual(reloaded.find_start('t', 'k')['run_id'], 'run-a')
        with self.assertRaises(StaleGeneration):  # stopped stays terminal after a restart too
            reloaded.put(dict(reloaded.get('run-a'), state='running'))
        self.assertNotEqual(reloaded._new_marker(), reloaded._new_marker())
        with self.assertRaises(ValueError):
            store.control_put('../escape', '{}')
        self.assertEqual(stat.S_IMODE(os.stat(self.state / 'runs.sqlite3').st_mode), 0o600)

    def test_a_stopped_record_with_no_attestation_is_refused_at_load(self):
        store = Registry(self.state)
        self.addCleanup(store.close)
        store.control_put('run-b', json.dumps({'run_id': 'run-b', 'tenant': 't', 'engine': 'codex', 'state': 'stopped',
                                               'cgroup': '/x/recursion-run-run-b.service', 'generation': 3}))
        with self.assertRaises(StopUnattested):
            adapter.DurableRunRegistry(store)

    def test_a_failed_durable_write_leaves_the_record_unchanged_and_is_typed(self):
        _, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        before = composed.control.registry.get(run_id)
        with mock.patch.object(Registry, 'control_put', side_effect=OSError('disk')):
            receipt = api.call('run.stop', {'run_id': run_id})
        # Cleanup ran and was attested; the receipt says it is not recorded.
        self.assertEqual((receipt['code'], receipt['recorded']), ('stopped', False))
        self.assertEqual(composed.control.registry.get(run_id)['generation'], before['generation'])
        self.assertEqual(api.call('run.stop', {'run_id': run_id}).get('recorded'), None)  # written on the retry
        self.assertEqual(composed.control.registry.get(run_id)['state'], 'stopped')

    # -- route file --------------------------------------------------------------------------------
    def test_route_file_is_read_only_when_it_is_the_operators_and_nothing_is_defaulted(self):
        path = self.tmp / 'routes.json'
        self.assertEqual(adapter.load_routes(path), {})
        entry = {'model': 'm', 'connection': {'kind': 'subscription', 'id': 'login'}, 'deadline_s': 60,
                 'binary': '/opt/bin/claude'}
        path.write_text(json.dumps({'routes': {'claude_code': entry}}))
        os.chmod(path, 0o600)
        routes = adapter.load_routes(path)
        self.assertEqual(routes['claude_code'], {'route': {'model': 'm', 'engine': 'claude_code', 'connection': {
            'kind': 'subscription', 'id': 'login'}}, 'deadline_s': 60.0, 'binary': '/opt/bin/claude'})
        os.chmod(path, 0o666)
        with self.assertRaises(adapter.RoutesUnreadable):
            adapter.load_routes(path)
        os.chmod(path, 0o600)
        for bad in ({'routes': {'pi': entry}}, {'routes': {'codex': dict(entry, deadline_s=0)}},
                    {'routes': {'codex': dict(entry, binary='claude')}}, {'routes': []}, []):
            path.write_text(json.dumps(bad))
            with self.assertRaises(adapter.RoutesUnreadable):
                adapter.load_routes(path)
        path.write_text('{not json')
        with self.assertRaises(adapter.RoutesUnreadable):
            adapter.load_routes(path)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-launch-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)

    def launcher(self, engine='claude_code', daemon=None, manager=None):
        return launcher.ProductionLauncher(engine, daemon or mock.Mock(), manager or mock.Mock(), self.tmp)

    def refusal(self, call):
        with self.assertRaises(c.RouteError) as caught:
            call()
        self.assertEqual(caught.exception.code, c.LAUNCH_REFUSED)
        return caught.exception.detail

    def test_child_environment_is_the_policy_over_the_adapters_environment(self):
        make = self.launcher().child_environment
        home = str(self.tmp / 'homes' / 'claude_code')
        sub = make({'PATH': '/usr/bin:/bin', 'HOME': '/home/operator', 'LANG': 'C', 'CLAUDE_CODE_OAUTH_TOKEN': 'tok'})
        self.assertEqual((sub.auth_mode, sub.to_dict()), ('subscription', {
            'PATH': '/usr/bin:/bin', 'LANG': 'C', 'HOME': home, 'CLAUDE_CODE_OAUTH_TOKEN': 'tok'}))
        key = make({'PATH': '/usr/bin:/bin', 'HOME': '/home/operator', 'ANTHROPIC_API_KEY': 'sk-route'})
        self.assertEqual((key.auth_mode, key['ANTHROPIC_API_KEY'], key['HOME']), ('api_key', 'sk-route', home))
        cloud = make({'PATH': '/usr/bin:/bin', 'CLAUDE_CODE_USE_BEDROCK': '1', 'AWS_REGION': 'us-east-1'})
        self.assertEqual((cloud.auth_mode, cloud['CLAUDE_CODE_USE_BEDROCK']), ('cloud_provider', '1'))
        self.assertNotIn('AWS_REGION', cloud)  # not on the policy's allowlist
        codex = self.launcher('codex').child_environment({'PATH': '/usr/bin:/bin', 'CODEX_API_KEY': 'ck'})
        self.assertEqual((codex.auth_mode, codex['CODEX_API_KEY']), ('api_key', 'ck'))

    def test_child_environment_refusals_are_typed_and_name_no_value(self):
        make = self.launcher().child_environment
        # A credential the route selected that the policy would silently drop: a different sign-in would run.
        gateway = self.refusal(lambda: make({'PATH': '/usr/bin:/bin', 'ANTHROPIC_AUTH_TOKEN': 'gw-secret'}))
        self.assertEqual(gateway, 'child_env:ANTHROPIC_AUTH_TOKEN:not_carried_by_policy')
        openai = self.refusal(lambda: self.launcher('codex').child_environment(
            {'PATH': '/usr/bin:/bin', 'OPENAI_API_KEY': 'ok-secret'}))
        self.assertEqual(openai, 'child_env:OPENAI_API_KEY:not_carried_by_policy')
        self.assertEqual(self.refusal(lambda: make({'PATH': '/usr/bin::/bin'})), 'child_env:PATH:path_empty_entry')
        both = self.refusal(lambda: make({'PATH': '/usr/bin', 'ANTHROPIC_API_KEY': 'k', 'CLAUDE_CODE_USE_VERTEX': '1'}))
        self.assertEqual(both, 'child_env:route:key_and_cloud_provider')
        with self.assertRaises(ValueError):
            self.launcher('pi')

    def test_launch_refuses_without_a_run_and_never_widens_the_launch(self):
        launch = self.launcher()
        self.assertEqual(self.refusal(lambda: launch(['claude'], env={})), 'no_run_context')
        ctx = launcher.RunContext('run-1')
        with launcher.bound_run(ctx):
            self.assertEqual(self.refusal(lambda: launch(['claude'], env={}, shell=True)), 'launch_option_denied')
            self.assertEqual(self.refusal(lambda: launch(['claude'], env={}, cwd='/')), 'launch_option_denied')
            self.assertEqual(self.refusal(lambda: launch('claude -p', env={})), 'argv_malformed')
            self.assertEqual(self.refusal(lambda: launch(['claude'], env={'PATH': '/nonexistent-dir-for-test'})),
                             'child_env:-:argv0_not_found')
            ctx.launches = 1
            self.assertEqual(self.refusal(lambda: launch(['claude'], env={})), 'run_already_launched')
            ctx.launches, ctx.cancelled = 0, True
            self.assertEqual(self.refusal(lambda: launch(['claude'], env={})), 'start_abandoned')
        self.assertEqual(self.refusal(lambda: launch(['claude'], env={})), 'no_run_context')
        launch._daemon.start.assert_not_called()

    def test_a_launcher_refusal_is_a_typed_route_result_not_an_engine_fault(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        service = RouteService({'claude_code': ClaudeCodeAdapter(self.launcher(), clock, lambda *_: '',
                                                                 {'PATH': '/usr/bin:/bin'})}, breaker, clock)
        result = service.handle(request())
        self.assertEqual((result['code'], result['detail'], result['breaker_effect']),
                         (c.LAUNCH_REFUSED, 'no_run_context', 'none'))

    def test_a_systemd_run_that_cannot_pass_argv_verbatim_refuses_the_launch(self):
        # ADV1-11: no --expand-environment=no, no launch. The flag is never dropped to make an old systemd work.
        for version in ('systemd 253 (253.5-1)\n', 'systemd 249\n', '', 'not systemd\n',
                        subprocess.CalledProcessError(1, ['systemd-run']), FileNotFoundError(2, 'systemd-run')):
            with self.subTest(version=version):
                manager = launcher.EngineUnitManager()
                calls = []

                def call(argv, version=version, calls=calls):
                    calls.append(argv)
                    if isinstance(version, BaseException):
                        raise version
                    return version

                manager._call = call
                with self.assertRaises(c.RouteError) as caught:
                    manager.start('recursion-run-run-1.service', ['/opt/claude', '$HOME'])
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 (c.LAUNCH_REFUSED, 'systemd_run_cannot_pass_argv_verbatim'))
                self.assertEqual(calls, [['systemd-run', '--version']])
        manager = launcher.EngineUnitManager()
        manager._call = lambda argv: 'systemd 254 (254.1)\n' if '--version' in argv else ''
        manager.start('recursion-run-run-1.service', ['/opt/claude', '$HOME'])

    def test_unit_command_carries_the_916_stop_properties_and_file_stdio(self):
        manager = launcher.EngineUnitManager()
        calls = []
        manager._call = lambda argv: calls.append(argv) or ('systemd 257 (257.13-1)\n' if '--version' in argv else '')
        run_dir = self.tmp / 'runs' / 'run-1'
        launch = self.launcher(manager=manager)
        manager.prepare('recursion-run-run-1.service', properties=launch.unit_properties(run_dir),
                        working_directory=self.tmp / 'homes' / 'claude_code')
        manager.start('recursion-run-run-1.service', launch.unit_argv('/opt/claude', ['claude', '-p'], run_dir / 'env.fifo'))
        self.assertEqual(calls, [['systemd-run', '--version'], [
            'systemd-run', '--user', '--quiet', '--unit=recursion-run-run-1.service', '--service-type=exec',
            '--expand-environment=no', '--property=KillMode=control-group', '--property=Delegate=yes', '--property=RemainAfterExit=yes',
            '--property=KillSignal=SIGINT', '--property=TimeoutStopSec=2s', '--property=SendSIGKILL=yes',
            f'--property=StandardInput=file:{run_dir}/stdin.fifo',
            f'--property=StandardOutput=append:{run_dir}/stdout.jsonl',
            f'--property=StandardError=append:{run_dir}/stderr.log',
            f'--working-directory={self.tmp}/homes/claude_code', '--',
            sys.executable, '-I', '-S', str(launcher.ENGINE_EXEC), f'{run_dir}/env.fifo', '--', '/opt/claude', '-p']])
        # No PartOf/BindsTo to the daemon, no environment and no PAM session in the unit command.
        self.assertFalse([a for a in calls[1] if 'PartOf' in a or 'BindsTo' in a or 'setenv' in a.lower()
                          or 'Environment' in a or 'PAMName' in a])
        with self.assertRaises(ValueError):
            manager.start('recursion-run-run-2.service', [])
        manager.start('recursion-run-run-3.service', ['/opt/claude'])
        self.assertEqual([call[:2] for call in calls[2:]], [['systemd-run', '--user']])  # the version is read once
        self.assertFalse(launcher._unit_path_ok('/tmp/with space/run'))
        self.assertFalse(launcher._unit_path_ok('/tmp/a/../b'))
        self.assertTrue(launcher._unit_path_ok(str(run_dir)))

    def test_unit_identity_and_pidfd_bind_to_the_units_cgroup(self):
        manager = launcher.EngineUnitManager()
        manager._call = lambda argv: 'ControlGroup=/app.slice/recursion-run-x.service\nMainPID=%d\n' % os.getpid()
        self.assertEqual(manager.unit_identity('recursion-run-x.service'),
                         ('/app.slice/recursion-run-x.service', os.getpid()))
        manager._call = lambda argv: 'ControlGroup=../escape\nMainPID=\n'
        self.assertEqual(manager.unit_identity('recursion-run-x.service'), ('', 0))
        # This process is not in that cgroup: a reused or foreign pid gets no pidfd.
        self.assertIsNone(manager.open_main_pidfd('/app.slice/recursion-run-x.service', os.getpid()))
        self.assertIsNone(manager.open_main_pidfd('', os.getpid()))
        own = Path('/proc/self/cgroup').read_text().splitlines()[0].split(':', 2)[-1]
        fd = manager.open_main_pidfd(own, os.getpid())
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_kill_tree_answers_empty_only_on_an_attested_end(self):
        class FakeDaemon:
            def __init__(self, states):
                self.states, self.stopped, self.seen = list(states), [], []

            def stop(self, run_id):
                self.stopped.append(run_id)

            def reconcile(self, run_id):
                self.seen.append(run_id)
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                return Run(run_id, f'recursion-run-{run_id}.service', True, state, BOOT, '')

        def tree(*states):
            clock = FakeClock(0.0)
            daemon = FakeDaemon(states)
            return daemon, launcher.UnitKillTree(daemon, poll_s=0.5, clock=clock, sleep=clock.advance)

        group = '/user.slice/app.slice/recursion-run-run-7.service'
        daemon, kill = tree('terminated')
        kill.request_stop(group)
        self.assertEqual(daemon.stopped, ['run-7'])
        self.assertEqual(kill.confirm(group, 5.0), EMPTY)
        self.assertEqual(tree('stopping', 'stopping', 'terminated')[1].confirm(group, 5.0), EMPTY)
        self.assertEqual(tree('running')[1].confirm(group, 2.0), POPULATED)
        self.assertEqual(tree('stopping')[1].confirm(group, 2.0), POPULATED)
        self.assertEqual(tree('unknown')[1].confirm(group, 2.0), UNKNOWN)
        self.assertEqual(tree('starting')[1].confirm(group, 2.0), TIMEOUT)
        self.assertEqual(tree('lost_reboot')[1].confirm(group, 2.0), UNKNOWN)  # nothing saw it end
        for bad in ('', 'recursion-run-run-7.service', '/a/../recursion-run-run-7.service', '/a/other.service', None):
            with self.assertRaises(ValueError):
                kill.request_stop(bad)
            with self.assertRaises(ValueError):
                kill.confirm(bad, 1.0)
        self.assertEqual(daemon.stopped, ['run-7'])

    def test_engine_process_reads_lines_until_the_main_process_is_gone(self):
        out = self.tmp / 'stdout.jsonl'
        out.write_bytes(b'')
        os.mkfifo(self.tmp / 'stdin.fifo', 0o600)
        stdin = os.open(self.tmp / 'stdin.fifo', os.O_RDWR | os.O_NONBLOCK)
        child_in = os.open(self.tmp / 'stdin.fifo', os.O_RDONLY | os.O_NONBLOCK)
        os.set_blocking(child_in, True)
        child_out = open(out, 'ab')
        child = subprocess.Popen([sys.executable, '-c', 'import sys, time\n'
                                  'line = sys.stdin.readline()\nprint("got " + line.strip(), flush=True)\n'
                                  'time.sleep(0.3)\nsys.stdout.write("tail-without-newline")'],
                                 stdin=child_in, stdout=child_out)
        os.close(child_in)
        child_out.close()
        self.addCleanup(child.wait)
        daemon = mock.Mock()
        process = launcher.UnitEngineProcess(daemon, 'run-1', stdin, out, os.pidfd_open(child.pid))
        self.addCleanup(process.close)
        with self.assertRaises(ReadTimeout):
            process.read_line(0.1)
        process.send('hello')
        process.close_stdin()
        with self.assertRaises(BrokenPipeError):
            process.send('after close')
        self.assertEqual(process.read_line(10), 'got hello')
        self.assertEqual(process.read_line(10), 'tail-without-newline')
        self.assertIsNone(process.read_line(10))
        process.fence('deadline')
        daemon.stop.assert_called_once_with('run-1')
        daemon.stop.side_effect = OSError('systemctl')
        with self.assertLogs(launcher.log, 'ERROR'):
            process.fence('again')  # a failed fence is logged, never raised into the route result
        self.assertEqual(process.fenced, ['deadline', 'again'])

    def test_engine_exec_reads_exactly_one_environment_and_refuses_anything_else(self):
        fifo = self.tmp / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        payload = json.dumps({'HOME': '/h', 'PATH': '/usr/bin'}).encode()
        os.write(fd, b'%d\n' % len(payload) + payload)
        self.assertEqual(engine_exec.read_env(str(fifo)), {'HOME': '/h', 'PATH': '/usr/bin'})
        for bad in (b'x\n{}', b'999999\n{}', b'2\n[]', b'12\n{"A=B": "1"}', b'8\n{"A": 1}'):
            os.write(fd, bad)
            with self.assertRaises(ValueError):
                engine_exec.read_env(str(fifo))
            try:
                os.read(fd, 65536)  # drain what a refused read left behind
            except BlockingIOError:
                pass
        # End to end: the engine sees only the given environment, and a bad launch runs nothing.
        os.write(fd, b'%d\n' % len(payload) + payload)
        ran = subprocess.run([sys.executable, '-I', '-S', str(launcher.ENGINE_EXEC), str(fifo), '--', '/usr/bin/env'],
                             capture_output=True, text=True, timeout=30, env={'LEAK': '1'}, check=False)
        self.assertEqual(sorted(ran.stdout.split()), ['HOME=/h', 'PATH=/usr/bin'])
        refused = subprocess.run([sys.executable, '-I', '-S', str(launcher.ENGINE_EXEC), str(fifo), '--', 'env'],
                                 capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((refused.returncode, refused.stdout), (127, ''))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class CompositionRootTest(unittest.TestCase):
    """``python -m recursion_core.daemon`` composes its own control adapter unless one is injected."""

    def run_main(self, **kwargs):
        from recursion_core.daemon import __main__ as entry
        with mock.patch.object(entry, 'require_free_threaded'), \
                mock.patch.object(entry, 'check_operator_linger'), \
                mock.patch.object(entry.signal, 'signal'), \
                mock.patch.object(entry, 'UserLock'), \
                mock.patch.object(entry, 'Registry'), \
                mock.patch.object(entry, 'Daemon') as daemon, \
                mock.patch.object(entry, 'LoopbackGUI') as gui, \
                mock.patch.object(entry, 'compose_control') as compose, \
                mock.patch.object(entry.threading, 'Event') as stopped, \
                mock.patch.dict(entry.os.environ, {'RECURSION_ENGINE_USER': ''}):
            stopped.return_value.wait.side_effect = [False, True]
            order = mock.Mock()
            order.attach_mock(daemon.return_value.sweep, 'sweep')
            order.attach_mock(compose, 'compose')
            order.attach_mock(compose.return_value.control.reconcile, 'reconcile')
            order.attach_mock(compose.return_value.close, 'close_control')
            order.attach_mock(daemon.return_value.close, 'close_daemon')
            entry.main(port=0, **kwargs)
            return entry, daemon, gui, compose, stopped, [call[0] for call in order.mock_calls if '()' not in call[0]]

    def test_daemon_serves_the_composed_control_adapter_to_the_gui(self):
        entry, daemon, gui, compose, stopped, order = self.run_main(sweep_seconds=1.5)
        manager = daemon.call_args.args[1]
        self.assertIsInstance(manager, launcher.EngineUnitManager)
        composed = compose.return_value
        gui.assert_called_once_with(composed.client, composed.door, plugins=(), port=0)
        # Sweep first, so recovered runs are observed before any surface can ask; never stop a run on exit.
        self.assertEqual(order, ['sweep', 'compose', 'reconcile', 'sweep', 'reconcile', 'close_control', 'close_daemon'])
        stopped.return_value.wait.assert_called_with(1.5)
        daemon.return_value.stop.assert_not_called()

    def test_an_injected_adapter_replaces_the_composition(self):
        entry, daemon, gui, compose, _, order = self.run_main(client=object(), door=bool)
        compose.assert_not_called()
        self.assertNotIsInstance(daemon.call_args.args[1], launcher.EngineUnitManager)
        self.assertEqual(order, ['sweep', 'sweep', 'close_daemon'])

    def test_a_composition_that_cannot_start_stops_the_daemon_before_any_gui(self):
        from recursion_core.daemon import __main__ as entry
        with mock.patch.object(entry, 'require_free_threaded'), mock.patch.object(entry, 'check_operator_linger'), \
                mock.patch.object(entry.signal, 'signal'), mock.patch.object(entry, 'UserLock'), \
                mock.patch.object(entry, 'Registry'), mock.patch.object(entry, 'Daemon') as daemon, \
                mock.patch.object(entry, 'LoopbackGUI') as gui, \
                mock.patch.object(entry, 'compose_control', side_effect=StopUnattested('run-x')), \
                mock.patch.dict(entry.os.environ, {'RECURSION_ENGINE_USER': ''}):
            with self.assertRaises(StopUnattested):
                entry.main(port=0)
            gui.assert_not_called()
            daemon.return_value.close.assert_called_once()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RealUserManagerTest(unittest.TestCase):
    """The same launch and Stop against a real ``systemd --user`` manager, where one is running."""

    def test_launch_and_916_stop_in_a_real_transient_unit(self):
        try:
            state = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True,
                                   timeout=15, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            state = 'unavailable'
        if state not in ('running', 'degraded'):
            self.skipTest(f'needs a running systemd --user manager (is-system-running: {state or "no answer"}); '
                          'the same path is covered by daemon/evidence_e2e.py (evidence-S12/evidence.md)')
        tmp = Path(tempfile.mkdtemp(prefix='rc-unit-', dir=os.environ.get('XDG_RUNTIME_DIR')))
        self.addCleanup(shutil.rmtree, tmp, True)
        engine = tmp / 'claude'
        shutil.copyfile(STANDIN, engine)
        os.chmod(engine, 0o700)
        manager = launcher.EngineUnitManager()
        daemon = Daemon(Registry(tmp / 'state'), manager)
        self.addCleanup(daemon.close)
        run_id = 'run-test%d' % os.getpid()
        ctx = launcher.RunContext(run_id)
        self.addCleanup(lambda: subprocess.run(['systemctl', '--user', 'stop', f'recursion-run-{run_id}.service'],
                                               capture_output=True, check=False))
        with launcher.bound_run(ctx):
            process = launcher.ProductionLauncher('claude_code', daemon, manager, tmp / 'state')(
                [str(engine), '-p'], env={'PATH': '/usr/bin:/bin'})
        self.addCleanup(process.close)
        process.send(json.dumps({'message': {'content': [{'text': 'fast=1'}]}}))
        process.close_stdin()
        self.assertEqual(json.loads(process.read_line(20))['subtype'], 'init')
        kill = launcher.UnitKillTree(daemon)
        self.assertEqual(kill.confirm(ctx.cgroup, 0.5), POPULATED)
        kill.request_stop(ctx.cgroup)
        self.assertEqual(kill.confirm(ctx.cgroup, 10.0), EMPTY)


if __name__ == '__main__':
    unittest.main()
