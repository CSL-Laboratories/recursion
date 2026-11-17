"""D-19, the spawn half: a worktree mode start makes the worktree ``place.choose`` already names.

On the tip this file was written against, the provider chooses that place and makes nothing.
The run is refused, ``worktree_path`` stays null, and the unit never starts in a worktree.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.control.hooks import OWNER, Hooks, LaunchContext
from recursion_core.workspace import hooks as WH
from recursion_core.workspace import place as P
from recursion_core.workspace import spawn
from recursion_core.workspace.tests import repos

POSIX = os.name == 'posix'


def _snapshot(root: Path) -> list:
    """Names and contents under ``root``. Link counts are left out: a hardlink changes those, not the bytes."""
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                kind = path.lstat().st_mode
            except OSError:
                rows.append((str(path.relative_to(root)), 'missing'))
                continue
            if stat.S_ISLNK(kind):
                payload = ('link', os.readlink(path))
            elif stat.S_ISREG(kind):
                payload = ('file', hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                payload = ('other', stat.S_IFMT(kind))
            rows.append((str(path.relative_to(root)), payload))
    return rows


class _Ctx:
    def __init__(self, run_id, launch):
        self.run_id = run_id
        self.launch = launch


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the worktree is made on Linux, in a temporary repository')
class SpawnGapTest(unittest.TestCase):
    """``run.start``'s provider, given a project folder, makes the worktree and returns it."""

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-spawn-', dir=os.environ.get('TMPDIR') or None)))
        os.chmod(self.root, 0o700)
        self.addCleanup(self._cleanup)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.repo = self.root / 'repo'
        self.head = repos.make_repo(self.repo, {'README.md': 'hello\n', 'pkg/module.py': 'x = 1\n'})
        (self.repo / 'README.md').write_text('dirty\n')
        held = os.environ.get(WH.STATE_ENV)
        os.environ[WH.STATE_ENV] = str(self.state)
        self.addCleanup(self._restore_env, held)
        self.hooks = Hooks()
        WH.register(self.hooks)

    def _cleanup(self):
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                try:
                    mode = path.lstat().st_mode
                except OSError:
                    continue
                if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                    os.chmod(path, mode | 0o200)
        shutil.rmtree(self.root, ignore_errors=True)

    def _restore_env(self, held):
        if held is None:
            os.environ.pop(WH.STATE_ENV, None)
        else:
            os.environ[WH.STATE_ENV] = held

    def _launch(self, folder, **extra):
        request = {'tenant': 't1', 'prompt': 'x', 'workspace': {'path': str(folder), 'mode': 'worktree', **extra}}
        launch, refusal = self.hooks.launch_snapshot(OWNER, request)
        self.assertIsNone(refusal, refusal)
        return LaunchContext(principal=OWNER, **launch)

    def _freeze_git(self):
        """Take write permission off the source git directory. A linked worktree cannot be registered there."""
        git = self.repo / '.git'
        self._git_before = _snapshot(git)
        for dirpath, dirnames, filenames in os.walk(git):
            os.chmod(dirpath, 0o555)
            for name in filenames:
                path = Path(dirpath) / name
                if not path.is_symlink():
                    os.chmod(path, 0o444)

    def test_a_project_folder_gets_a_worktree_outside_the_repository_and_the_git_directory_is_not_written(self):
        self._freeze_git()
        launch = self._launch(self.repo)
        self.assertIsNone(launch.workspace['worktree_path'])
        ctx = _Ctx('run-spawn', launch)
        provider = WH.Provider(state_dir=self.state)
        folder = Path(provider(ctx))
        self.assertTrue(folder.is_dir(), 'the chosen place was not created')
        self.assertEqual(os.path.realpath(folder), os.path.realpath(self.state / 'worktrees' / 'run-spawn'))
        self.assertNotEqual(os.path.realpath(folder), os.path.realpath(self.repo))
        self.assertFalse(Path(os.path.realpath(self.repo)) in Path(os.path.realpath(folder)).parents)
        self.assertEqual((folder / 'README.md').read_text(), 'dirty\n')
        self.assertEqual((folder / 'pkg' / 'module.py').read_text(), 'x = 1\n')
        self.assertEqual(launch.workspace['worktree_path'], str(folder))
        self.assertEqual(provider(ctx), folder)
        place = provider._places['run-spawn']
        self.assertFalse(place.engine_worktree_switching)
        self.assertFalse(place.writes_git_common_dir)
        self.assertNotEqual(place.mechanism, 'linked')
        self.assertEqual(_snapshot(self.repo / '.git'), self._git_before)
        listed = repos.worktrees(self.repo)
        self.assertEqual(listed, [str(self.repo)])
        again = WH.Provider(state_dir=self.state)
        # A new provider, the same run, the path already on the launch: it returns that folder.
        self.assertEqual(again(ctx), folder)

    def test_a_base_and_a_subdirectory_stay_out_of_the_source_checkout(self):
        first = repos.git(self.repo, 'rev-parse', 'HEAD').decode().strip()
        (self.repo / 'README.md').write_text('second\n')
        second = repos.commit(self.repo, 'second')
        self.assertNotEqual(first, second)
        launch = self._launch(self.repo / 'pkg', base_ref=first)
        ctx = _Ctx('run-base', launch)
        folder = Path(WH.Provider(state_dir=self.state)(ctx))
        self.assertEqual(folder.name, 'pkg')
        self.assertEqual((folder / 'module.py').read_text(), 'x = 1\n')
        self.assertEqual((folder.parent / 'README.md').read_text(), 'hello\n')
        self.assertEqual(repos.git(self.repo, 'rev-parse', 'HEAD').decode().strip(), second)
        self.assertEqual((self.repo / 'README.md').read_text(), 'second\n')
        self.assertEqual(launch.workspace['worktree_path'], str(folder))

    def test_a_linked_worktree_is_only_the_explicit_setting(self):
        launch = self._launch(self.repo)
        ctx = _Ctx('run-link', launch)
        provider = WH.Provider(state_dir=self.state, linked=True)
        folder = Path(provider(ctx))
        self.assertTrue(folder.is_dir())
        self.assertNotEqual(os.path.realpath(folder), os.path.realpath(self.repo))
        listed = repos.worktrees(self.repo)
        self.assertIn(str(folder), listed)
        self.assertEqual(repos.git(self.repo, 'rev-parse', 'HEAD').decode().strip(), self.head)
        # The source checkout keeps its uncommitted byte. The linked tree is the commit.
        self.assertEqual((self.repo / 'README.md').read_text(), 'dirty\n')
        self.assertEqual((folder / 'README.md').read_text(), 'hello\n')
        chosen = provider._places['run-link']
        self.assertEqual(chosen.mechanism, 'linked')
        self.assertTrue(chosen.writes_git_common_dir)
        self.assertFalse(chosen.engine_worktree_switching)

    def test_a_failed_make_keeps_the_place_and_a_second_ask_does_not_choose_again(self):
        launch = self._launch(self.repo)
        ctx = _Ctx('run-retry', launch)
        provider = WH.Provider(state_dir=self.state)
        real = spawn.make
        calls = {'n': 0}

        def flaky(place, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise KeyboardInterrupt
            return real(place, **kwargs)

        with mock.patch('recursion_core.workspace.hooks.choose', wraps=WH.choose) as picked:
            with mock.patch('recursion_core.workspace.hooks.make', side_effect=flaky):
                with self.assertRaises(KeyboardInterrupt):
                    provider(ctx)
                self.assertEqual(picked.call_count, 1)
                folder = provider(ctx)
                self.assertEqual(picked.call_count, 1)
        self.assertTrue(Path(folder).is_dir())
        self.assertEqual(calls['n'], 2)

    def test_no_place_refuses_and_does_not_choose_a_second_time(self):
        launch = self._launch(self.repo)
        ctx = _Ctx('run-none-place', launch)
        provider = WH.Provider(state_dir=self.state)
        with mock.patch('recursion_core.workspace.hooks.choose', return_value=None) as picked:
            with self.assertRaises(RuntimeError):
                provider(ctx)
            with self.assertRaises(RuntimeError):
                provider(ctx)
            self.assertEqual(picked.call_count, 1)
        self.assertFalse((self.state / 'worktrees' / 'run-none-place').exists())

    def test_two_callers_get_one_worktree(self):
        launch = self._launch(self.repo)
        ctx = _Ctx('run-two', launch)
        provider = WH.Provider(state_dir=self.state)
        found = []
        barrier = threading.Barrier(2)

        def ask():
            barrier.wait(timeout=5)
            found.append(provider(ctx))

        threads = [threading.Thread(target=ask), threading.Thread(target=ask)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(found[0], found[1])
        self.assertTrue(Path(found[0]).is_dir())

    def test_run_start_stores_the_worktree_path_on_the_record(self):
        from recursion_core.control.api import ControlAPI
        from recursion_core.control.registry import InMemoryRunRegistry
        from recursion_core.control.tests.fakes import FakeKillTree, run_ids
        from recursion_core.daemon.launcher import RunContext
        seen = {}

        def starter(params):
            ctx = RunContext(params['run_id'])
            seen['folder'] = ctx.hooks.working_directory(ctx)
            return {'cgroup': '/cg/' + params['run_id']}

        registry = InMemoryRunRegistry()
        api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0, new_run_id=run_ids(),
                         hooks=self.hooks)
        answer = api.call('run.start', {
            'tenant': 't1', 'engine': 'codex', 'prompt': 'p',
            'workspace': {'path': str(self.repo), 'mode': 'worktree'}})
        self.assertEqual(answer['code'], 'started', answer)
        recorded = answer['run']['launch']['workspace']['worktree_path']
        self.assertEqual(recorded, str(Path(seen['folder'])))
        self.assertTrue(Path(recorded).is_dir())
        self.assertNotEqual(os.path.realpath(recorded), os.path.realpath(self.repo))
        self.assertEqual(registry.get(answer['run']['run_id'])['launch']['workspace']['worktree_path'], recorded)

    def test_a_failed_start_still_records_the_worktree_it_made(self):
        from recursion_core.control.api import ControlAPI
        from recursion_core.control.registry import InMemoryRunRegistry
        from recursion_core.control.tests.fakes import FakeKillTree, run_ids
        from recursion_core.daemon.launcher import RunContext

        def starter(params):
            ctx = RunContext(params['run_id'])
            ctx.hooks.working_directory(ctx)
            raise RuntimeError('stop-after-folder')

        registry = InMemoryRunRegistry()
        api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0, new_run_id=run_ids(),
                         hooks=self.hooks)
        answer = api.call('run.start', {
            'tenant': 't1', 'engine': 'codex', 'prompt': 'p', 'client_key': 'once',
            'workspace': {'path': str(self.repo), 'mode': 'worktree'}})
        self.assertEqual(answer['code'], 'start_failed', answer)
        stored = registry.get(answer['run_id'])
        path = stored['launch']['workspace']['worktree_path']
        self.assertTrue(path and Path(path).is_dir())
        self.assertEqual(stored['state'], 'start_failed')

    def test_a_stale_running_write_still_stores_the_worktree_path(self):
        """A generation that lost the race is reread. The path is copied onto that fresh row."""
        import copy

        from recursion_core.control.api import ControlAPI
        from recursion_core.control.registry import InMemoryRunRegistry
        from recursion_core.control.tests.fakes import FakeKillTree, run_ids
        from recursion_core.daemon.launcher import RunContext

        class OnceStale(InMemoryRunRegistry):
            def __init__(self):
                super().__init__()
                self.tripped = False

            def put(self, record):
                if not self.tripped and record.get('state') == 'running':
                    self.tripped = True
                    # Another writer lands the row this start read, still without the path.
                    super().put(copy.deepcopy(super().get(record['run_id'])))
                return super().put(record)

        def starter(params):
            ctx = RunContext(params['run_id'])
            ctx.hooks.working_directory(ctx)
            return {'cgroup': '/cg/' + params['run_id']}

        registry = OnceStale()
        api = ControlAPI(registry, FakeKillTree(), starter, clock=lambda: 1000.0, new_run_id=run_ids(),
                         hooks=self.hooks)
        answer = api.call('run.start', {
            'tenant': 't1', 'engine': 'codex', 'prompt': 'p',
            'workspace': {'path': str(self.repo), 'mode': 'worktree'}})
        self.assertEqual(answer['code'], 'started', answer)
        self.assertTrue(registry.tripped)
        recorded = registry.get(answer['run']['run_id'])['launch']['workspace']['worktree_path']
        self.assertTrue(recorded and Path(recorded).is_dir())
        self.assertEqual(answer['run']['launch']['workspace']['worktree_path'], recorded)


class MechanismTest(unittest.TestCase):
    """Each mechanism, asked directly, leaves the source git directory's bytes where they were."""

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-mech-', dir=os.environ.get('TMPDIR') or None)))
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / 'repo'
        repos.make_repo(self.repo, {'README.md': 'hello\n'})
        self.before = _snapshot(self.repo / '.git')

    def _place(self, name: str, mechanism: str, *, writes: bool = False) -> P.Place:
        return P.Place(path=self.root / name, mechanism=mechanism, engine_worktree_switching=False,
                       repository=self.repo.resolve(), git_common_dir=(self.repo / '.git').resolve(),
                       writes_git_common_dir=writes, same_filesystem=True, reflink=mechanism == 'reflink',
                       hardlink=mechanism == 'hardlink', free_bytes=1 << 40, work_bytes=10, object_bytes=10)

    def _object(self) -> Path | None:
        for dirpath, _dirs, names in os.walk(self.repo / '.git' / 'objects'):
            for name in names:
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink() and len(path.parent.name) == 2:
                    return path
        return None

    def test_copy_writes_new_bytes_and_hardlink_shares_the_object(self):
        copied = spawn.make(self._place('copied', 'copy'))
        self.assertEqual((copied / 'README.md').read_text(), 'hello\n')
        self.assertEqual(_snapshot(self.repo / '.git'), self.before)
        obj = self._object()
        self.assertIsNotNone(obj)
        before_links = obj.stat().st_nlink
        linked = spawn.make(self._place('linked-objects', 'hardlink'))
        self.assertEqual((linked / 'README.md').read_text(), 'hello\n')
        self.assertGreater(obj.stat().st_nlink, before_links)
        twin = None
        for dirpath, _dirs, names in os.walk(linked / '.git' / 'objects'):
            for name in names:
                path = Path(dirpath) / name
                if path.is_file() and path.stat().st_ino == obj.stat().st_ino:
                    twin = path
        self.assertIsNotNone(twin)
        work_src, work_dst = self.repo / 'README.md', linked / 'README.md'
        self.assertNotEqual(work_src.stat().st_ino, work_dst.stat().st_ino)
        self.assertEqual(_snapshot(self.repo / '.git'), self.before)
        again = spawn.make(self._place('linked-objects', 'hardlink'))
        self.assertEqual(again, linked)
        self.assertEqual(obj.stat().st_nlink, before_links + 1)

    def test_reflink_shares_extents_when_the_filesystem_can(self):
        if not P.reflink_possible(self.root):
            self.skipTest('this filesystem cannot reflink')
        made = spawn.make(self._place('reflinked', 'reflink'))
        self.assertEqual((made / 'README.md').read_text(), 'hello\n')
        self.assertNotEqual((self.repo / 'README.md').stat().st_ino, (made / 'README.md').stat().st_ino)
        self.assertEqual(_snapshot(self.repo / '.git'), self.before)

    def test_a_place_inside_the_repository_is_not_removed(self):
        inside = self.repo / 'inside'
        inside.mkdir()
        (inside / 'keep.txt').write_text('keep\n')
        place = self._place('unused', 'copy')
        # The constructed path is replaced: the candidate is inside the repository.
        place = P.Place(path=inside, mechanism='copy', engine_worktree_switching=False,
                        repository=self.repo.resolve(), git_common_dir=(self.repo / '.git').resolve(),
                        writes_git_common_dir=False, same_filesystem=True, reflink=False, hardlink=False,
                        free_bytes=1 << 40, work_bytes=10, object_bytes=10)
        with self.assertRaises(spawn.SpawnFailed) as caught:
            spawn.make(place)
        self.assertEqual(caught.exception.reason, 'place_overlaps_repository')
        self.assertEqual((inside / 'keep.txt').read_text(), 'keep\n')

    def test_a_foreign_directory_at_the_place_is_left_in_place(self):
        dest = self.root / 'occupied'
        dest.mkdir()
        (dest / 'keep.txt').write_text('keep\n')
        with self.assertRaises(spawn.SpawnFailed) as caught:
            spawn.make(self._place('occupied', 'copy'))
        self.assertEqual(caught.exception.reason, 'place_not_empty')
        self.assertEqual((dest / 'keep.txt').read_text(), 'keep\n')

    def test_an_interruption_after_the_tree_is_finished_leaves_it(self):
        place = self._place('kept', 'copy')
        real = spawn._independent

        def after(chosen, dest, base_ref):
            real(chosen, dest, base_ref)
            raise KeyboardInterrupt

        with mock.patch('recursion_core.workspace.spawn._independent', side_effect=after):
            with self.assertRaises(KeyboardInterrupt):
                spawn.make(place)
        self.assertTrue((place.path / '.git' / 'recursion-spawn-complete').is_file())
        self.assertEqual((place.path / 'README.md').read_text(), 'hello\n')
        self.assertEqual(spawn.make(place), place.path)


if __name__ == '__main__':
    unittest.main()
