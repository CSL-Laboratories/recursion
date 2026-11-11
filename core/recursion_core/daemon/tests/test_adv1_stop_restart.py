"""ADV1 red team of the Stop path, restart recovery and the launcher (CSL-738 candidate at 0beb782f).

The service manager is faked two ways: ``ScriptedManager`` keeps the real ``SystemdUserManager``
observation and attestation code and only scripts what ``systemctl`` prints (and how it exits),
over a fake cgroup tree on disk; the composed tests use ``ProcessUnitManager`` from
``test_composition``. Tests that expose a defect are ``expectedFailure`` with an ``ADV1-<n>`` reason.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import StopUnattested
from recursion_core.daemon import Daemon, Registry, RUN_ID
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import engine_exec, launcher
from recursion_core.daemon.tests.test_adv1_helpers import BOOT, ComposedBase, ProcessUnitManager, wait_for

LINUX = sys.platform.startswith('linux')
UNIT = 'recursion-run-run-x.service'
GROUP = '/app.slice/' + UNIT


class ScriptedManager(launcher.EngineUnitManager):
    """Real observation/attestation code; ``systemctl`` output and exit status are scripted."""

    def __init__(self, cgroup_root, show, stop='ok'):
        super().__init__(cgroup_root=cgroup_root)
        self.show, self.stop_mode, self.calls = show, stop, []

    def _call(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ['systemctl', '--user', 'stop']:
            mode = self.stop_mode
            if mode == 'ok':
                return ''
            if mode == 'hang':
                raise subprocess.TimeoutExpired(argv, 15)
            if mode == 'not_loaded':
                raise subprocess.CalledProcessError(5, argv, '', f'Failed to stop {argv[-1]}: Unit {argv[-1]} not loaded.\n')
            if isinstance(mode, int):
                raise subprocess.CalledProcessError(mode, argv, '', 'odd failure\n')
            if mode == 'oserror':
                raise FileNotFoundError(2, 'systemctl')
        if argv[:3] == ['systemctl', '--user', 'show']:
            show = self.show() if callable(self.show) else self.show
            if isinstance(show, BaseException):
                raise show
            return show
        if 'list-units' in argv:
            return ''
        raise AssertionError(argv)


def show(**props):
    base = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running', 'ControlGroup': GROUP,
            'Job': '', 'InvocationID': 'a' * 32, 'ExecMainStartTimestampMonotonic': '5', 'MainPID': '0'}
    base.update(props)
    return ''.join(f'{k}={v}\n' for k, v in base.items())


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class StopTruthTest(unittest.TestCase):
    """Stop answers ``stopped`` only on proof. Every case starts from a recorded running run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-stop-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.root = self.tmp / 'cg'
        self.victims = []

    def cgroup(self, path, pids=(), populated=None):
        directory = self.root / path.lstrip('/')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'cgroup.procs').write_text(''.join(f'{p}\n' for p in pids))
        if populated is None:
            populated = 1 if pids else 0
        (directory / 'cgroup.events').write_text(f'populated {populated}\nfrozen 0\n')
        return directory

    def victim(self):
        proc = subprocess.Popen(['sleep', '60'])
        self.victims.append(proc)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        return proc

    def api(self, manager):
        daemon = Daemon(Registry(self.tmp / 'state'), manager, boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        registry = adapter.DurableRunRegistry(daemon.registry, processes=adapter.pid_alive(manager))
        registry.put({'run_id': 'run-x', 'tenant': 't', 'engine': 'claude_code', 'state': 'running',
                      'cgroup': GROUP, 'started_at': 1.0, 'stop': None, 'start_key': None})
        api = ControlAPI(registry, launcher.UnitKillTree(daemon, poll_s=0.01), lambda p: {}, time.time,
                         confirm_timeout_s=0.3)
        return api, daemon

    def assertNotStopped(self, answer):
        self.assertEqual(answer['code'], 'pending', answer)
        self.assertNotEqual(answer.get('state'), 'stopped', answer)

    def forgotten(self):
        """A manager that no longer knows the unit: stop says "not loaded", show says not-found."""
        return ScriptedManager(self.root, show(LoadState='not-found', ActiveState='inactive', SubState='dead',
                                               ControlGroup='', InvocationID='', MainPID='0',
                                               ExecMainStartTimestampMonotonic='0'), stop='not_loaded')

    def test_unit_forgotten_by_the_manager_while_its_cgroup_still_holds_a_process_is_not_stopped(self):
        """ADV1-1 (fixed): "not loaded" + "not-found" is not an end while the recorded cgroup holds a process."""
        proc = self.victim()
        self.cgroup(GROUP, [proc.pid])
        api, daemon = self.api(self.forgotten())
        answer = api.call('run.stop', {'run_id': 'run-x'})
        self.assertIsNone(proc.poll(), 'the process in the recorded cgroup is alive')
        self.assertNotStopped(answer)
        self.assertEqual(answer.get('reason'), 'cgroup_populated', answer)
        row = daemon.registry.get('run-x')
        self.assertEqual((row.state, row.detail), ('stopping', 'unit gone; the recorded cgroup still holds a process'))
        # A child cgroup of the recorded one counts too, and so does a walk that cannot be finished.
        self.cgroup(GROUP, [])
        self.cgroup(GROUP + '/sub', [proc.pid])
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))
        os.symlink(self.root, self.root / GROUP.lstrip('/') / 'loop')
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))

    def test_unit_forgotten_by_the_manager_is_stopped_only_from_its_recorded_cgroup(self):
        """ADV1-1 (fixed): the attestation comes from the recorded cgroup being absent or empty."""
        for name in ('absent', 'empty', 'dead_pid'):
            with self.subTest(case=name):
                if name == 'empty':
                    self.cgroup(GROUP, [])
                elif name == 'dead_pid':
                    proc = self.victim()
                    proc.kill()
                    proc.wait()
                    self.cgroup(GROUP, [proc.pid], populated=0)
                api, daemon = self.api(self.forgotten())
                answer = api.call('run.stop', {'run_id': 'run-x'})
                self.assertEqual((answer['code'], answer['state']), ('stopped', 'stopped'), answer)
                self.assertEqual(daemon.registry.get('run-x').state, 'terminated')
                daemon.close()
                shutil.rmtree(self.tmp / 'state')
                shutil.rmtree(self.root, True)

    def test_a_live_recorded_cgroup_is_killed_through_the_cgroup_and_stays_pending_until_empty(self):
        """ADV1-1 (fixed): the kill goes to the recorded cgroup's cgroup.kill; only an empty re-read is an end."""
        proc = self.victim()
        directory = self.cgroup(GROUP, [proc.pid])
        (directory / 'cgroup.kill').write_text('')  # a plain file here: the write lands, nothing dies
        manager = self.forgotten()
        manager.recorded_kill_wait_s = 0.2
        api, _ = self.api(manager)
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))
        self.assertEqual((directory / 'cgroup.kill').read_text(), '1')
        self.assertIsNone(proc.poll())
        # Without cgroup.kill the pids are signalled one by one, but only a pid /proc places in that cgroup:
        # this one is in the test's own cgroup, so it is left alone and the Stop stays pending.
        (directory / 'cgroup.kill').unlink()
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))
        self.assertIsNone(proc.poll())
        self.assertFalse(manager._kill_member(proc.pid, GROUP))
        # Once the cgroup reads empty the same Stop is attested.
        proc.kill()
        proc.wait()
        self.cgroup(GROUP, [])
        self.assertEqual(api.call('run.stop', {'run_id': 'run-x'})['code'], 'stopped')

    def test_a_recorded_cgroup_that_does_not_name_the_unit_is_never_walked_or_killed(self):
        manager = self.forgotten()
        for group in ('/app.slice/other.service', '/app.slice/../' + UNIT, 'app.slice/' + UNIT, '', None,
                      '/app.slice/' + UNIT + '/child'):
            self.assertIsNone(manager.recorded_cgroup_clear(UNIT, group), group)
        self.assertIsNone(manager.recorded_cgroup_clear('sshd.service', '/system.slice/sshd.service'))

    def test_systemctl_that_hangs_fails_or_is_missing_never_yields_stopped(self):
        for mode in ('hang', 1, 3, 143, 'oserror'):
            with self.subTest(mode=mode):
                proc = self.victim()
                self.cgroup(GROUP, [proc.pid])
                manager = ScriptedManager(self.root, show(MainPID=str(proc.pid)), stop=mode)
                api, daemon = self.api(manager)
                self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))
                self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))  # a repeat only re-checks
                self.assertTrue(daemon.registry.get('run-x').stop_requested)  # the durable intent is kept
                shutil.rmtree(self.tmp / 'state')
                shutil.rmtree(self.root)

    def test_not_loaded_without_systemds_exact_words_is_not_attested(self):
        proc = self.victim()
        self.cgroup(GROUP, [proc.pid])
        manager = ScriptedManager(self.root, show(LoadState='not-found', ActiveState='inactive', ControlGroup='',
                                                  MainPID='0'), stop=5)  # exit 5, other text
        api, _ = self.api(manager)
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))

    def test_a_stale_empty_cgroup_does_not_attest_a_live_tree(self):
        cases = {}
        proc = self.victim()
        # Unit cgroup reads empty, but a delegated child cgroup holds the live process.
        cases['child_cgroup'] = lambda: (self.cgroup(GROUP, [], populated=1), self.cgroup(GROUP + '/sub', [proc.pid]),
                                         show(ActiveState='deactivating', SubState='stop-sigterm'))
        # Unit cgroup reads empty and unpopulated, but systemd still names a live main pid.
        cases['main_pid_alive'] = lambda: (self.cgroup(GROUP, []), None,
                                           show(ActiveState='deactivating', MainPID=str(proc.pid)))
        # systemd names a cgroup outside the unit path: refused, not read.
        cases['traversal'] = lambda: (None, None, show(ControlGroup='/app.slice/../escape'))
        # Unreadable show output.
        cases['garbage'] = lambda: (None, None, 'no equals sign here\n')
        cases['timeout_on_show'] = lambda: (None, None, subprocess.TimeoutExpired(['systemctl'], 15))
        # The unit is gone, but its main pid is still alive.
        cases['not_found_main_alive'] = lambda: (None, None, show(LoadState='not-found', ActiveState='inactive',
                                                                 ControlGroup='', MainPID=str(proc.pid)))
        # cgroup pruned, unit inactive, but a start job is still queued (never ran).
        cases['queued_job'] = lambda: (None, None, show(ActiveState='inactive', SubState='dead', ControlGroup='',
                                                       Job='42', InvocationID='', ExecMainStartTimestampMonotonic='0'))
        for name, build in cases.items():
            with self.subTest(case=name):
                _, _, text = build()
                manager = ScriptedManager(self.root, text, stop='ok')
                api, _ = self.api(manager)
                self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))
                self.assertIsNone(proc.poll())
                shutil.rmtree(self.tmp / 'state')
                shutil.rmtree(self.root, True)

    def test_a_symlinked_or_looping_cgroup_tree_is_not_a_proof(self):
        proc = self.victim()
        unit_dir = self.cgroup(GROUP, [], populated=0)
        elsewhere = self.cgroup('/elsewhere', [proc.pid])
        os.symlink(elsewhere, unit_dir / 'child')
        manager = ScriptedManager(self.root, show(ActiveState='deactivating'), stop='ok')
        api, _ = self.api(manager)
        self.assertNotStopped(api.call('run.stop', {'run_id': 'run-x'}))

    def test_stop_of_a_lost_reboot_run_stays_pending(self):
        manager = ScriptedManager(self.root, show(), stop='ok')
        daemon = Daemon(Registry(self.tmp / 'state'), manager, boot_id=lambda: 'boot-2')
        self.addCleanup(daemon.close)
        daemon.registry.add('run-x', BOOT)
        registry = adapter.DurableRunRegistry(daemon.registry, processes=adapter.pid_alive(manager))
        registry.put({'run_id': 'run-x', 'tenant': 't', 'engine': 'claude_code', 'state': 'running',
                      'cgroup': GROUP, 'started_at': 1.0, 'stop': None, 'start_key': None})
        api = ControlAPI(registry, launcher.UnitKillTree(daemon, poll_s=0.01), lambda p: {}, time.time,
                         confirm_timeout_s=0.3)
        answer = api.call('run.stop', {'run_id': 'run-x'})
        self.assertEqual((answer['code'], answer.get('reason')), ('pending', 'confirm_unknown'), answer)
        self.assertFalse([c for c in manager.calls if c[:3] == ['systemctl', '--user', 'stop']],
                         'documents: a lost_reboot run is never sent a systemctl stop')

    def test_kill_tree_refuses_a_cgroup_that_names_no_run_unit(self):
        kill = launcher.UnitKillTree(mock.Mock())
        for bad in ('/app.slice/recursion-run-x.service/../../user.slice', '/app.slice/recursion-run-a b.service',
                    '/app.slice/recursion-run-%s.service' % ('x' * 65), '/app.slice/sshd.service',
                    '/app.slice/recursion-run-x.service\n', 'relative/recursion-run-x.service'):
            with self.assertRaises(ValueError, msg=bad):
                kill.request_stop(bad)


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class ComposedStopTest(ComposedBase):
    def test_stop_before_the_cgroup_is_recorded_is_pending_then_carried_out(self):
        units = self.units
        release = threading.Event()

        class SlowStart(ProcessUnitManager):
            def start(self, unit, argv):
                release.wait(20)
                super().start(unit, argv)

        _, composed = self.boot(manager=SlowStart(units))
        api = self.api(composed)
        started = {}
        thread = threading.Thread(target=lambda: started.update(api.call(
            'run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0'})))
        thread.start()
        self.assertTrue(wait_for(lambda: composed.control.registry.list(), 10))
        run_id = composed.control.registry.list()[0]['run_id']
        early = api.call('run.stop', {'run_id': run_id})
        self.assertEqual((early['code'], early.get('reason')), ('pending', 'cgroup_unknown'), early)
        release.set()
        thread.join(60)
        self.assertEqual(started['code'], 'started', started)
        self.assertEqual(started['stop']['code'], 'stopped', started)
        self.assertEqual(api.call('run.status', {'run_id': run_id})['run']['state'], 'stopped')
        self.assertIsNotNone(units.procs[f'recursion-run-{run_id}.service'].poll())

    def test_concurrent_double_stop_both_answer_stopped_and_the_tree_is_gone(self):
        _, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(self.api(composed).call('run.stop', {'run_id': run_id})))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual([a['code'] for a in answers], ['stopped'] * 4, answers)
        self.assertEqual(sum(1 for a in answers if a.get('repeat') is False), 1, answers)
        self.assertIsNotNone(self.units.procs[f'recursion-run-{run_id}.service'].poll())

    def test_prompt_never_reaches_argv_whatever_it_contains(self):
        _, composed = self.boot()
        api = self.api(composed)
        prompt = '--dangerously-skip-permissions\n-p $(id) `id` ${HOME} %n \x00 helper=0 ticks=1'
        run_id = self.start(api, prompt=prompt)
        self.assertTrue(wait_for(lambda: any(l.get('subtype') == 'init' for l in self.lines(run_id)), 15))
        init = next(l for l in self.lines(run_id) if l.get('subtype') == 'init')
        self.assertEqual(init['argv'], ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                                        '--verbose', '--model', 'stand-in-model'])
        started = self.units.started[-1]['argv']
        self.assertFalse([a for a in started if 'dangerously' in a or '$(id)' in a or '\x00' in a], started)

    def test_run_id_paths_are_never_followed_out_of_the_run_directory(self):
        _, composed = self.boot()
        secret = self.tmp / 'outside.jsonl'
        secret.write_text('{"leak": true}\n')
        for bad in ('../outside', '..', 'a/../../outside', '/etc/passwd', 'run-x/../../x'):
            self.assertIsNone(composed.control._output(bad, live=False), bad)
            self.assertIsNone(composed.control._result(bad), bad)
            with self.assertRaises(adapter.StartRefused):
                composed.control.starter({'run_id': bad, 'engine': 'claude_code', 'tenant': 't', 'prompt': 'p'})
        self.assertFalse(RUN_ID.fullmatch('run-x/../y'))

    def test_a_routes_symlink_or_foreign_file_starts_nothing(self):
        config = self.tmp / 'config' / 'recursion'
        config.mkdir(parents=True)
        real = self.tmp / 'routes-real.json'
        real.write_text(json.dumps({'routes': {'claude_code': {
            'model': 'm', 'connection': {'kind': 'subscription', 'id': 'x'}, 'deadline_s': 5}}}))
        os.chmod(real, 0o600)
        (config / 'routes.json').symlink_to(real)
        with self.assertRaises(adapter.RoutesUnreadable):
            adapter.load_routes(config / 'routes.json')
        self.routes = None  # let compose read the file
        daemon = Daemon(Registry(self.state), self.manager(), boot_id=lambda: BOOT)
        with self.assertLogs(adapter.log, 'ERROR'):
            composed = adapter.compose_control(daemon, self.state, environ=self.environ)
        self.open.append((daemon, composed))
        answer = self.api(composed).call('run.start', {'tenant': 't', 'engine': 'claude_code', 'prompt': 'p'})
        self.assertEqual((answer['code'], answer['start_error']['code']), ('start_failed', 'route_not_configured'))
        self.assertEqual(self.units.started, [])


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class RestartTest(ComposedBase):
    def test_a_torn_or_misshapen_control_record_keeps_the_daemon_down(self):
        # Documents ADV1-7 (INFO): one bad row from any same-uid writer refuses the whole start, which also
        # removes the socket and the GUI Stop for every other run until an operator repairs the file.
        _, composed = self.boot()
        survivor = self.start(self.api(composed))
        self.shutdown()
        for bad, error in (('{torn', ValueError), ('[1, 2]', AttributeError),
                           (json.dumps({'run_id': 'run-y', 'state': 'stopped', 'cgroup': '/x'}), StopUnattested)):
            with self.subTest(bad=bad):
                store = Registry(self.state)
                store.control_put('run-y', bad)
                store.close()
                with self.assertRaises(error):
                    self.boot()
                self.open.clear()
                self.assertFalse((self.runtime / 'recursion' / 'control.sock').exists())
        self.assertIsNone(self.units.procs[f'recursion-run-{survivor}.service'].poll(), 'the run is still live')

    @unittest.expectedFailure  # ADV1-7
    def test_a_record_with_a_non_string_run_id_does_not_silently_disable_list_and_reconcile(self):
        """ADV1-7: the daemon starts on such a row, then run.list answers registry_unavailable and every
        reconcile pass logs "nothing reconciled" (sorting int and str ids raises), with no other signal."""
        _, composed = self.boot()
        survivor = self.start(self.api(composed))
        self.shutdown()
        store = Registry(self.state)
        store.control_put('run-y', '{"run_id": 5, "state": "running"}')
        store.close()
        try:
            _, composed = self.boot()
        except Exception:  # refusing to start, like the other untrusted rows, is an acceptable answer
            return
        listed = self.api(composed).call('run.list')
        with self.assertLogs(adapter.log, 'ERROR') as logged:
            composed.control.reconcile()
            adapter.log.error('marker')
        self.assertEqual(listed['code'], 'ok', listed)
        self.assertIn(survivor, [r['run_id'] for r in listed['runs']])
        self.assertEqual(len(logged.output), 1, logged.output)

    def test_repeated_reconcile_passes_are_idempotent_for_settled_records(self):
        daemon, composed = self.boot()
        api = self.api(composed)
        live = self.start(api)
        stopped = self.start(api)
        self.assertEqual(api.call('run.stop', {'run_id': stopped})['code'], 'stopped')
        failed = api.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': 'p'})
        self.assertEqual(failed['code'], 'start_failed')
        before = {r['run_id']: r['generation'] for r in composed.control.registry.list()}
        for _ in range(5):
            daemon.sweep()
            composed.control.reconcile()
        after = {r['run_id']: r['generation'] for r in composed.control.registry.list()}
        self.assertEqual(before, after)
        self.assertIsNone(self.units.procs[f'recursion-run-{live}.service'].poll())

    @unittest.expectedFailure  # ADV1-6
    def test_a_stop_that_can_never_confirm_is_not_rewritten_on_every_sweep(self):
        """ADV1-6: a pending Stop is re-sent from reconcile() on every sweep; each pass writes a new generation
        (a full registry-file rewrite) and, for an unobservable unit, waits the whole confirm timeout."""
        daemon, composed = self.boot(confirm_timeout_s=0.3)
        api = self.api(composed)
        run_id = self.start(api)
        unit = f'recursion-run-{run_id}.service'
        proc = self.units.procs.pop(unit)
        self.addCleanup(lambda: (proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL), proc.wait()))
        manager = daemon.manager
        with mock.patch.object(manager, 'stop', side_effect=subprocess.TimeoutExpired(['systemctl'], 15)):
            self.assertEqual(api.call('run.stop', {'run_id': run_id})['code'], 'pending')
            first = composed.control.registry.get(run_id)['generation']
            started = time.monotonic()
            for _ in range(5):
                daemon.sweep()
                composed.control.reconcile()
            spent = time.monotonic() - started
        later = composed.control.registry.get(run_id)['generation']
        self.assertEqual(later, first, f'{later - first} new generations in 5 sweeps, {spent:.1f} s in reconcile')

    @unittest.expectedFailure  # ADV1-8
    def test_reconcile_racing_a_start_does_not_mark_it_failed(self):
        """ADV1-8: reconcile() in the window between the API's launch-intent write and RouteStarter
        registering the run writes start_failed for a start that then succeeds (record flaps)."""
        daemon, composed = self.boot()
        api = self.api(composed)
        starter = composed.control.starter
        original = type(starter).__call__
        seen = []
        gate = threading.Event()

        def slow(self_, params):
            gate.set()
            time.sleep(0.5)
            return original(self_, params)

        def reconcile_in_window():
            gate.wait(10)
            composed.control.reconcile()
            seen.extend(r['state'] for r in composed.control.registry.list())

        with mock.patch.object(type(starter), '__call__', slow):
            racer = threading.Thread(target=reconcile_in_window)
            racer.start()
            answer = api.call('run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0'})
            racer.join(30)
        self.assertEqual(answer['code'], 'started', answer)
        run_id = answer['run']['run_id']
        self.assertEqual(api.call('run.status', {'run_id': run_id})['run']['state'], 'running')
        self.assertNotIn('start_failed', seen, 'the record flapped starting -> start_failed -> running')


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class LauncherAttackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-launch-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)

    def test_argv_nul_shell_and_options_are_refused_before_any_unit(self):
        daemon, manager = mock.Mock(), mock.Mock()
        launch = launcher.ProductionLauncher('claude_code', daemon, manager, self.tmp)
        ctx = launcher.RunContext('run-1')
        with launcher.bound_run(ctx):
            for argv, kwargs in ((['/bin/claude', 'a\x00b'], {}), ([], {}), ('claude', {}), ([b'x'], {}),
                                 (['/bin/claude'], {'shell': True}), (['/bin/claude'], {'cwd': '/'}),
                                 (['/bin/claude'], {'preexec_fn': print})):
                with self.assertRaises(Exception) as caught:
                    launch(argv, env={'PATH': '/usr/bin'}, **kwargs)
                self.assertEqual(type(caught.exception).__name__, 'RouteError', argv)
        daemon.start.assert_not_called()
        manager.prepare.assert_not_called()

    def test_hostile_environment_names_never_reach_the_engine(self):
        launch = launcher.ProductionLauncher('claude_code', mock.Mock(), mock.Mock(), self.tmp)
        hostile = {'LD_PRELOAD': '/tmp/x.so', 'LD_LIBRARY_PATH': '/tmp', 'NODE_OPTIONS': '--require /tmp/x.js',
                   'BASH_ENV': '/tmp/x', 'PYTHONPATH': '/tmp', 'GIT_SSH_COMMAND': 'sh', 'ENV': '/tmp/x',
                   'PROMPT_COMMAND': 'id', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
                   'SYSTEMD_EXEC_PID': '1', 'NOTIFY_SOCKET': '/run/user/1000/systemd/notify'}
        for name, value in hostile.items():
            with self.subTest(name=name):
                try:
                    got = launch.child_environment({'PATH': '/usr/bin:/bin', name: value}).to_dict()
                except Exception as exc:  # a refusal is fine too
                    self.assertEqual(type(exc).__name__, 'RouteError')
                    continue
                self.assertNotIn(name, got)

    def test_the_engine_is_told_where_the_runtime_directory_is(self):
        # Documents ADV1-10 (INFO): B1 carries XDG_RUNTIME_DIR, so the engine is handed the directory of the
        # control socket and of the user manager it can use to leave its unit (ADV1-2). Dropping it would not
        # be a barrier (the path is /run/user/<uid>), so this is recorded, not marked as a defect.
        launch = launcher.ProductionLauncher('claude_code', mock.Mock(), mock.Mock(), self.tmp)
        got = launch.child_environment({'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': '/run/user/1000'}).to_dict()
        self.assertEqual(got.get('XDG_RUNTIME_DIR'), '/run/user/1000')

    def test_env_fifo_handoff_cannot_tell_who_read_the_environment(self):
        # Documents ADV1-9 (INFO): the launcher's only check is that the FIFO drained. Any same-uid process
        # that opens runs/<id>/env.fifo first (an engine of another run) gets the environment, and can write
        # one of its own for engine_exec. The 0700 state directory is the only barrier.
        fifo = self.tmp / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        payload = json.dumps({'HOME': '/h', 'PATH': '/usr/bin'}).encode()
        os.write(fd, b'%d\n' % len(payload) + payload)
        stolen = engine_exec.read_env(str(fifo))  # the thief
        launcher.ProductionLauncher('claude_code', mock.Mock(), mock.Mock(), self.tmp, start_timeout_s=1)._wait_env_read(fd)
        self.assertEqual(stolen['HOME'], '/h')
        forged = json.dumps({'PATH': '/usr/bin', 'LD_PRELOAD': '/tmp/evil.so'}).encode()
        os.write(fd, b'%d\n' % len(forged) + forged)
        self.assertEqual(engine_exec.read_env(str(fifo))['LD_PRELOAD'], '/tmp/evil.so')  # what engine_exec would exec with

    def test_an_endless_stdout_line_is_bounded_on_read(self):
        out = self.tmp / 'stdout.jsonl'
        with open(out, 'wb') as handle:
            handle.write(b'x' * (launcher.MAX_LINE_BYTES + 70000))
        child = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        process = launcher.UnitEngineProcess(mock.Mock(), 'run-1', None, out, os.pidfd_open(child.pid))
        self.addCleanup(process.close)
        with self.assertRaises(OSError):
            process.read_line(30)
        control = adapter.DaemonControl(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(results={}), self.tmp)
        (self.tmp / 'runs' / 'run-1').mkdir(parents=True)
        os.rename(out, self.tmp / 'runs' / 'run-1' / 'stdout.jsonl')
        tail = control._output('run-1', live=True)
        self.assertEqual((tail['lines'], tail['complete']), ([], False))
        self.assertGreater(tail['bytes'], launcher.MAX_LINE_BYTES)


if __name__ == '__main__':
    unittest.main()
