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


if __name__ == '__main__':
    unittest.main()
