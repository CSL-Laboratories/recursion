"""D-19: run.start names a folder, and the run's unit works in that folder.

A start that carries ``workspace`` is checked before a record exists. In direct mode the unit's
working directory is that folder. HOME stays the engine's private home. A worktree is not made here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from recursion_core.control.api import ControlAPI
from recursion_core.control.hooks import compose
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter, run_ids
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher
from recursion_core.daemon.tests.test_composition import (
    BOOT, LINUX, LINUX_ONLY, STANDIN, ProcessUnitManager, Units,
)
from recursion_core.workspace.hooks import STATE_ENV
from recursion_core.workspace.tests import repos

POSIX = os.name == 'posix'


def _real(path) -> str:
    return os.path.realpath(path)


def _hooks(state):
    held = os.environ.get(STATE_ENV)
    os.environ[STATE_ENV] = str(state)
    try:
        return compose()
    finally:
        if held is None:
            os.environ.pop(STATE_ENV, None)
        else:
            os.environ[STATE_ENV] = held


def _api(state):
    from recursion_core.control.registry import InMemoryRunRegistry
    registry = InMemoryRunRegistry()
    starter = FakeStarter()
    api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0, new_run_id=run_ids(),
                     hooks=_hooks(state))
    return api, registry, starter


@unittest.skipUnless(POSIX, 'the launcher prepares a unit only on Linux')
class LauncherFolderTest(unittest.TestCase):
    """The launcher call, with no new argument on ``RunContext``. Today's code prepares the engine home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-folder-', dir=os.environ.get('TMPDIR') or None))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.folder = self.tmp / 'project'
        self.folder.mkdir()
        self.home = self.tmp / 'homes' / 'claude_code'

    def test_prepare_uses_the_named_folder_and_still_makes_the_engine_home(self):
        daemon, manager = mock.Mock(), mock.Mock()
        daemon.start.side_effect = RuntimeError('stop-after-prepare')
        launch = launcher.ProductionLauncher('claude_code', daemon, manager, self.tmp)
        ctx = launcher.RunContext('run-folder')
        ctx.launch = mock.Mock(workspace={
            'mode': 'direct', 'source_path': str(self.folder), 'base_ref': None, 'worktree_path': None})
        ctx.hooks = mock.Mock()
        ctx.hooks.working_directory.return_value = self.folder
        with launcher.bound_run(ctx):
            with self.assertRaises(RuntimeError):
                launch(['/usr/bin/true'], env={'PATH': '/usr/bin:/bin'})
        ctx.hooks.working_directory.assert_called_once()
        cwd = Path(manager.prepare.call_args.kwargs['working_directory'])
        self.assertEqual(cwd, self.folder)
        self.assertNotEqual(cwd, self.home)
        self.assertTrue(self.home.is_dir(), 'HOME is still the engine home')

    def test_with_no_hooks_prepare_still_uses_the_engine_home(self):
        daemon, manager = mock.Mock(), mock.Mock()
        daemon.start.side_effect = RuntimeError('stop-after-prepare')
        launch = launcher.ProductionLauncher('claude_code', daemon, manager, self.tmp)
        ctx = launcher.RunContext('run-plain')
        with launcher.bound_run(ctx):
            with self.assertRaises(RuntimeError):
                launch(['/usr/bin/true'], env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(Path(manager.prepare.call_args.kwargs['working_directory']), self.home)


@unittest.skipUnless(POSIX, 'folder checks read the file system')
class DoorFolderTest(unittest.TestCase):
    """The control door with the composed hooks and a fake starter: the record, not the unit."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='rc-door-', dir=os.environ.get('TMPDIR') or None))
        self.addCleanup(shutil.rmtree, self.root, True)
        os.chmod(self.root, 0o700)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.folder = self.root / 'project'
        self.folder.mkdir()
        self.api, self.registry, self.starter = _api(self.state)

    def _start(self, workspace, **extra):
        params = {'tenant': 't1', 'engine': 'codex', 'prompt': 'p', 'workspace': workspace}
        params.update(extra)
        return self.api.call('run.start', params)

    def test_a_direct_folder_is_recorded_and_a_state_directory_or_another_user_is_refused(self):
        answer = self._start({'path': str(self.folder), 'mode': 'direct'})
        self.assertEqual(answer['code'], 'started', answer)
        recorded = answer['run']['launch']['workspace']
        self.assertEqual(recorded['source_path'], _real(self.folder))
        self.assertEqual(recorded['mode'], 'direct')
        self.assertIsNone(recorded['worktree_path'])
        self.assertEqual(answer['run']['principal'], {'kind': 'owner', 'id': 'local'})
        self.assertEqual(len(self.starter.calls), 1)
        inside = self.state / 'nope'
        inside.mkdir()
        refused = self._start({'path': str(inside), 'mode': 'direct'})
        self.assertEqual((refused.get('code'), refused.get('detail')), ('workspace_invalid', 'state_directory'), refused)
        other = self._start({'path': '/etc', 'mode': 'direct'})
        self.assertEqual((other.get('code'), other.get('detail')), ('workspace_invalid', 'not_owned'), other)
        self.assertEqual(len(self.starter.calls), 1, 'a refused folder must not launch')
        self.assertIsNone(self.registry.get(refused['run_id']) if 'run_id' in refused else None)

    def test_a_parent_of_the_state_directory_is_not_the_state_directory_rule(self):
        # ``validate`` refuses the state directory and every folder inside it. It does not refuse a parent.
        parent = self.root
        answer = self._start({'path': str(parent), 'mode': 'direct'})
        self.assertEqual(answer['code'], 'started', answer)
        self.assertEqual(answer['run']['launch']['workspace']['source_path'], _real(parent))

    def test_a_missing_path_is_refused_before_a_record_and_a_link_is_recorded_as_its_target(self):
        bad = self._start({'mode': 'direct'})
        self.assertEqual((bad.get('code'), bad.get('detail')), ('request_invalid', 'workspace'), bad)
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.starter.calls, [])
        link = self.root / 'link'
        link.symlink_to(self.folder, target_is_directory=True)
        answer = self._start({'path': str(link), 'mode': 'direct'})
        self.assertEqual(answer['run']['launch']['workspace']['source_path'], _real(self.folder))

    def test_the_same_folder_is_one_start_and_another_folder_reuses_the_key(self):
        first = self._start({'path': str(self.folder), 'mode': 'direct'}, client_key='k')
        again = self._start({'path': str(self.folder), 'mode': 'direct'}, client_key='k')
        self.assertEqual((again['code'], again['duplicate'], again['run']['run_id']),
                         ('started', True, first['run']['run_id']))
        self.assertEqual(len(self.starter.calls), 1)
        other = self.root / 'other'
        other.mkdir()
        reused = self._start({'path': str(other), 'mode': 'direct'}, client_key='k')
        self.assertEqual((reused.get('code'), reused.get('detail')), ('request_invalid', 'client_key_reused'), reused)
        self.assertEqual(len(self.starter.calls), 1)

    def test_a_legacy_key_with_no_folder_still_retries_and_a_folder_on_it_is_refused(self):
        prompt = 'p'
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        self.registry.put({'run_id': 'run-legacy', 'tenant': 't1', 'engine': 'codex', 'state': 'running',
                           'cgroup': '/cg/legacy', 'started_at': 1.0, 'stop': None,
                           'start_key': {'client_key': 'legacy', 'engine': 'codex', 'prompt_sha256': digest}})
        plain = self.api.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': prompt, 'client_key': 'legacy'})
        self.assertEqual((plain['code'], plain['duplicate'], plain['run']['run_id']), ('started', True, 'run-legacy'))
        self.assertNotIn('launch', plain['run'])
        self.assertEqual(self.starter.calls, [])
        reused = self._start({'path': str(self.folder), 'mode': 'direct'}, client_key='legacy')
        self.assertEqual((reused.get('code'), reused.get('detail')), ('request_invalid', 'client_key_reused'), reused)

    def test_a_name_the_unit_cannot_carry_is_refused_before_launch(self):
        spaced = self.root / 'my project'
        spaced.mkdir()
        answer = self._start({'path': str(spaced), 'mode': 'direct'})
        self.assertEqual((answer.get('code'), answer.get('detail')), ('workspace_invalid', 'not_unit_safe'), answer)
        self.assertEqual(self.starter.calls, [])

    def test_without_hooks_a_folder_is_ignored(self):
        from recursion_core.control.registry import InMemoryRunRegistry
        registry, starter = InMemoryRunRegistry(), FakeStarter()
        api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0, new_run_id=run_ids())
        answer = api.call('run.start', {'tenant': 't1', 'engine': 'codex', 'prompt': 'p',
                                        'workspace': {'path': str(self.folder), 'mode': 'direct'}})
        self.assertEqual(answer['code'], 'started', answer)
        self.assertNotIn('launch', answer['run'])
        self.assertEqual(len(starter.calls), 1)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ComposedFolderTest(unittest.TestCase):
    """The daemon's ``compose_control`` path. The unit's working directory is the folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-compose-', dir=os.environ.get('TMPDIR') or None))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        (self.tmp / 'bin').mkdir(mode=0o700)
        self.engine = self.tmp / 'bin' / 'claude'
        shutil.copyfile(STANDIN, self.engine)
        os.chmod(self.engine, 0o700)
        try:
            probe = subprocess.run([str(self.engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                   env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            probe = b''
        if b'stand-in engine got no user message' not in probe:
            self.skipTest('temp directory cannot execute the stand-in engine')
        self.units = Units()
        self.addCleanup(self.kill_units)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
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
                    os.killpg(proc.pid, signal_kill())
                except ProcessLookupError:
                    pass
            proc.wait()

    def boot(self):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes)
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    def _start(self, composed, workspace, prompt='helper=0 ticks=1 fast=1', key=None):
        params = {'tenant': 't1', 'engine': 'claude_code', 'prompt': prompt, 'workspace': workspace}
        if key is not None:
            params['client_key'] = key
        return composed.control.call('run.start', params)

    def _init_line(self, run_id):
        path = self.state / 'runs' / run_id / 'stdout.jsonl'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                text = path.read_text().splitlines()
                if text:
                    return json.loads(text[0])
            time.sleep(0.05)
        self.fail('the engine wrote no init line')

    def test_a_direct_folder_is_the_units_working_directory_and_home_stays_the_engine_home(self):
        folder = self.tmp / 'project'
        folder.mkdir()
        composed = self.boot()
        answer = self._start(composed, {'path': str(folder), 'mode': 'direct'})
        self.assertEqual(answer.get('code'), 'started', answer)
        recorded = answer['run']['launch']['workspace']['source_path']
        self.assertEqual(recorded, _real(folder))
        self.assertEqual(_real(self.units.started[-1]['cwd']), _real(folder))
        self.assertNotEqual(_real(self.units.started[-1]['cwd']), _real(self.state / 'homes' / 'claude_code'))
        init = self._init_line(answer['run']['run_id'])
        self.assertEqual(init.get('home'), str(self.state / 'homes' / 'claude_code'))

    def test_a_start_that_names_no_folder_still_uses_the_engine_home(self):
        composed = self.boot()
        answer = composed.control.call('run.start', {
            'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 ticks=1 fast=1'})
        self.assertEqual(answer.get('code'), 'started', answer)
        self.assertIsNone(answer['run']['launch']['workspace'])
        self.assertEqual(_real(self.units.started[-1]['cwd']), _real(self.state / 'homes' / 'claude_code'))

    def test_the_state_directory_and_another_users_folder_launch_nothing(self):
        composed = self.boot()
        inside = self.state / 'not-a-project'
        inside.mkdir()
        before = len(self.units.started)
        state = self._start(composed, {'path': str(inside), 'mode': 'direct'})
        self.assertEqual((state.get('code'), state.get('detail')), ('workspace_invalid', 'state_directory'), state)
        other = self._start(composed, {'path': '/etc', 'mode': 'direct'})
        self.assertEqual((other.get('code'), other.get('detail')), ('workspace_invalid', 'not_owned'), other)
        self.assertEqual(len(self.units.started), before)

    @unittest.skipUnless(repos.HAVE_GIT, 'the repository is made by git')
    def test_a_worktree_is_not_made_and_the_run_does_not_start_in_the_checkout_or_the_home(self):
        repo = self.tmp / 'repo'
        repos.make_repo(repo, {'README.md': 'hello\n'})
        composed = self.boot()
        before = len(self.units.started)
        answer = self._start(composed, {'path': str(repo), 'mode': 'worktree'})
        self.assertEqual(answer.get('code'), 'start_failed', answer)
        self.assertEqual(answer.get('start_error', {}).get('detail'), 'working_directory_failed')
        self.assertEqual(len(self.units.started), before)
        rec = composed.control.registry.get(answer['run_id'])
        self.assertEqual(rec['launch']['workspace']['mode'], 'worktree')
        self.assertIsNone(rec['launch']['workspace']['worktree_path'])
        self.assertEqual(rec['launch']['workspace']['source_path'], _real(repo))
        self.assertFalse((self.state / 'worktrees' / 'unbuilt').exists())


def signal_kill():
    import signal
    return signal.SIGKILL


if __name__ == '__main__':
    unittest.main()
