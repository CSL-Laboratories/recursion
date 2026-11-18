"""The workspace registrant: the provider returns a direct folder, and does not make a worktree."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from recursion_core.control.hooks import OWNER, Hooks, LaunchContext
from recursion_core.workspace import hooks as WH
from recursion_core.workspace.place import choose
from recursion_core.workspace.tests import repos
from recursion_core.workspace.validate import WorkspaceInvalid, git_work_tree

POSIX = os.name == 'posix'


class _Ctx:
    def __init__(self, run_id, launch):
        self.run_id = run_id
        self.launch = launch


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the provider checks a real folder, and a repository is made by git')
class ProviderTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-hooks-')))
        self.addCleanup(shutil.rmtree, self.root)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.plain = self.root / 'plain'
        self.plain.mkdir()
        self.repo = self.root / 'repo'
        repos.make_repo(self.repo, {'pkg/module.py': 'x = 1\n'})
        held = os.environ.get(WH.STATE_ENV)
        os.environ[WH.STATE_ENV] = str(self.state)
        self.addCleanup(self._restore, held)
        self.hooks = Hooks()
        WH.register(self.hooks)
        launch, refusal = self.hooks.launch_snapshot(OWNER, {
            'tenant': 't1', 'prompt': 'x', 'workspace': {'path': str(self.plain), 'mode': 'direct'}})
        self.assertIsNone(refusal)
        self.direct = LaunchContext(principal=OWNER, **launch)

    def _restore(self, held):
        if held is None:
            os.environ.pop(WH.STATE_ENV, None)
        else:
            os.environ[WH.STATE_ENV] = held

    def test_a_direct_folder_is_returned_twice_as_the_same_folder(self):
        provider = self.hooks.working_directory
        # The seam's method wraps the provider. Call the provider itself for the folder, and the seam too.
        raw = WH.Provider(state_dir=self.state)
        ctx = _Ctx('run-1', self.direct)
        first = raw(ctx)
        second = raw(ctx)
        self.assertEqual(first, second)
        self.assertEqual(os.path.realpath(first), os.path.realpath(self.plain))
        self.assertEqual(provider(ctx), first)

    def test_no_workspace_returns_none(self):
        launch, refusal = self.hooks.launch_snapshot(OWNER, {'tenant': 't1', 'prompt': 'x'})
        self.assertIsNone(refusal)
        self.assertIsNone(launch['workspace'])
        ctx = _Ctx('run-none', LaunchContext(principal=OWNER, **launch))
        self.assertIsNone(self.hooks.working_directory(ctx))

    def test_a_worktree_place_is_chosen_and_not_made(self):
        launch, refusal = self.hooks.launch_snapshot(OWNER, {
            'tenant': 't1', 'prompt': 'x', 'workspace': {'path': str(self.repo), 'mode': 'worktree'}})
        self.assertIsNone(refusal)
        self.assertEqual(launch['workspace']['mode'], 'worktree')
        self.assertIsNone(launch['workspace']['worktree_path'])
        ctx = _Ctx('run-wt', LaunchContext(principal=OWNER, **launch))
        seen = []

        def _spy(*args, **kwargs):
            place = choose(*args, **kwargs)
            seen.append(place)
            return place

        raw = WH.Provider(state_dir=self.state)
        with mock.patch('recursion_core.workspace.hooks.choose', side_effect=_spy):
            with self.assertRaises(RuntimeError):
                raw(ctx)
        self.assertEqual(len(seen), 1)
        chosen = seen[0]
        self.assertIsNotNone(chosen)
        self.assertFalse(Path(chosen.path).exists(), 'the chosen place was not created')
        self.assertNotEqual(os.path.realpath(chosen.path), os.path.realpath(self.repo))
        with mock.patch('recursion_core.workspace.hooks.choose') as picked:
            with self.assertRaises(RuntimeError):
                raw(ctx)
            picked.assert_not_called()
        self.assertFalse((self.state / 'worktrees' / 'unbuilt').exists())
        with self.assertRaises(Exception):
            self.hooks.working_directory(ctx)

    def test_the_direct_recheck_refuses_a_deleted_folder_the_state_directory_and_an_unsafe_name(self):
        # The snapshot is taken while the folder exists. The provider checks it again on the ask.
        raw = WH.Provider(state_dir=self.state)
        shutil.rmtree(self.plain)
        with self.assertRaises(WorkspaceInvalid) as caught:
            raw(_Ctx('run-gone', self.direct))
        self.assertEqual(caught.exception.reason, 'not_found')

        fresh = WH.Provider(state_dir=self.state)
        state_launch = _Ctx('run-state', _Launch({'mode': 'direct', 'source_path': str(self.state),
                                                  'base_ref': None, 'worktree_path': None}))
        with self.assertRaises(WorkspaceInvalid) as caught:
            fresh(state_launch)
        self.assertEqual(caught.exception.reason, 'state_directory')

        spaced = self.root / 'my project'
        spaced.mkdir()
        spaced_launch = _Ctx('run-space', _Launch({'mode': 'direct', 'source_path': str(spaced),
                                                   'base_ref': None, 'worktree_path': None}))
        with self.assertRaises(WorkspaceInvalid) as caught:
            WH.Provider(state_dir=self.state)(spaced_launch)
        self.assertEqual(caught.exception.reason, 'not_unit_safe')

    def test_choose_is_asked_for_the_measured_bytes_the_unbuilt_candidate_and_linked_false(self):
        launch, refusal = self.hooks.launch_snapshot(OWNER, {
            'tenant': 't1', 'prompt': 'x', 'workspace': {'path': str(self.repo), 'mode': 'worktree'}})
        self.assertIsNone(refusal)
        ctx = _Ctx('run-measured', LaunchContext(principal=OWNER, **launch))
        raw = WH.Provider(state_dir=self.state)
        candidate = self.state / 'worktrees' / 'unbuilt'
        with mock.patch('recursion_core.workspace.hooks.choose') as picked:
            with self.assertRaises(RuntimeError):
                raw(ctx)
            picked.assert_called_once()
            args, kwargs = picked.call_args
            top = git_work_tree(self.repo)
            work, objects = _counted(top)
            self.assertEqual(args[0], top)
            self.assertEqual(list(args[1]), [candidate])
            self.assertEqual(kwargs['work_bytes'], work)
            self.assertEqual(kwargs['object_bytes'], objects)
            self.assertGreater(work, 0)
            self.assertGreater(objects, 0)
            self.assertIs(kwargs['linked'], False)
            self.assertFalse(candidate.exists())
            picked.reset_mock()
            with self.assertRaises(RuntimeError):
                raw(ctx)
            picked.assert_not_called()
        self.assertFalse(candidate.exists())


class _Launch:
    def __init__(self, workspace):
        self.workspace = workspace


def _counted(repository: Path) -> tuple[int, int]:
    """Checkout bytes, and git object bytes, counted by the test rather than by the provider."""
    work = 0
    for dirpath, dirnames, filenames in os.walk(repository, followlinks=False):
        if '.git' in dirnames:
            dirnames.remove('.git')
        for name in filenames:
            try:
                work += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    objects_dir = repository / '.git' / 'objects'
    if not (repository / '.git').is_dir():
        return work, 0
    objects = 0
    if objects_dir.exists():
        for dirpath, _dirnames, filenames in os.walk(objects_dir, followlinks=False):
            for name in filenames:
                try:
                    objects += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    continue
    return work, objects


if __name__ == '__main__':
    unittest.main()
