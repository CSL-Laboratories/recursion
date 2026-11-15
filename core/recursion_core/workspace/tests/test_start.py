"""The workspace's part of a start (workspace/start.py): the pure S6 normaliser and its registration.

The normaliser is called as the seam calls it, through ``Hooks.launch_snapshot`` on hooks the lane registered
on (control/hooks.py, lane SPINE's shared fake of the seams), and directly. What it answers is held to the
published shapes with lane SPINE's standard-library reader (``published.fits``).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from recursion_core.control import hooks as H
from recursion_core.workspace import start as S
from recursion_core.workspace.tests import repos
from recursion_core.workspace.tests.published import fits

POSIX = os.name == 'posix'


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the workspace checks are POSIX, and the repository is made by git')
class NormalizerTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-start-')))
        self.addCleanup(shutil.rmtree, self.root)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.plain = self.root / 'plain'
        self.plain.mkdir()
        if S.git_work_tree(self.plain) is not None:
            self.skipTest('the temporary folder is itself inside a git work tree')
        self.repo = self.root / 'repo'
        repos.make_repo(self.repo, {'pkg/module.py': 'x = 1\n'})
        self.normalize = S.Normalizer(state_dir=self.state)

    def ask(self, workspace, normalize=None, principal=H.OWNER):
        request = {'tenant': 't1', 'prompt': 'add a line', 'workspace': workspace}
        return (normalize or self.normalize)(principal, request)

    def launch(self, mode, source, base_ref=None) -> dict:
        return {'workspace': {'mode': mode, 'source_path': str(source), 'base_ref': base_ref, 'worktree_path': None}}

    # -- no workspace ---------------------------------------------------------------------------------
    def test_a_start_with_no_workspace_answers_an_empty_part(self) -> None:
        self.assertEqual(self.normalize(H.OWNER, {'tenant': 't1', 'prompt': 'x'}), {})
        self.assertEqual(self.ask(None), {})

    # -- the snapshot ---------------------------------------------------------------------------------
    def test_a_folder_in_no_repository_is_direct(self) -> None:
        self.assertEqual(self.ask({'path': str(self.plain)}), self.launch('direct', self.plain))

    def test_a_folder_in_a_repository_is_a_worktree_by_default(self) -> None:
        self.assertEqual(self.ask({'path': str(self.repo)}), self.launch('worktree', self.repo))
        self.assertEqual(self.ask({'path': str(self.repo / 'pkg')}), self.launch('worktree', self.repo / 'pkg'))

    def test_a_mode_given_is_kept(self) -> None:
        self.assertEqual(self.ask({'path': str(self.repo), 'mode': 'direct'}), self.launch('direct', self.repo))
        self.assertEqual(self.ask({'path': str(self.repo), 'mode': 'worktree'}), self.launch('worktree', self.repo))
        self.assertEqual(self.ask({'path': str(self.plain), 'mode': 'direct'}), self.launch('direct', self.plain))

    def test_a_configured_default_mode_applies_when_none_is_given(self) -> None:
        direct = S.Normalizer(state_dir=self.state, default_mode='direct')
        self.assertEqual(self.ask({'path': str(self.repo)}, direct), self.launch('direct', self.repo))
        worktree = S.Normalizer(state_dir=self.state, default_mode='worktree')
        self.assertEqual(self.ask({'path': str(self.repo)}, worktree), self.launch('worktree', self.repo))
        self.assertEqual(self.ask({'path': str(self.plain)}, worktree), H.Refusal('workspace_invalid', 'not_a_repository'))
        with self.assertRaises(ValueError):
            S.Normalizer(default_mode='sometimes')

    def test_worktree_mode_asked_of_a_folder_in_no_repository_is_refused(self) -> None:
        self.assertEqual(self.ask({'path': str(self.plain), 'mode': 'worktree'}),
                         H.Refusal('workspace_invalid', 'not_a_repository'))

    def test_the_folder_is_recorded_resolved(self) -> None:
        link = self.root / 'link-to-repo'
        link.symlink_to(self.repo)
        self.assertEqual(self.ask({'path': str(link)}), self.launch('worktree', self.repo))
        self.assertEqual(self.ask({'path': str(self.plain) + '/'}), self.launch('direct', self.plain))

    def test_the_mode_follows_what_git_itself_would_find(self) -> None:
        linked = self.root / 'linked-admin'
        repos.make_repo(linked)
        (linked / '.git').rename(self.root / 'admin')
        (linked / '.git').symlink_to(self.root / 'admin', target_is_directory=True)
        self.assertEqual(self.ask({'path': str(linked)}), self.launch('worktree', linked))
        marker = self.root / 'marker-only'
        (marker / '.git').mkdir(parents=True)
        (marker / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
        self.assertEqual(self.ask({'path': str(marker)}), self.launch('direct', marker))
        self.assertEqual(self.ask({'path': str(marker), 'mode': 'worktree'}),
                         H.Refusal('workspace_invalid', 'not_a_repository'))

    def test_direct_mode_reads_no_git_metadata(self) -> None:
        def no_git(folder):
            raise AssertionError('direct mode read git metadata')
        with mock.patch.object(S, 'git_work_tree', no_git):
            self.assertEqual(self.ask({'path': str(self.repo), 'mode': 'direct'}), self.launch('direct', self.repo))
            direct = S.Normalizer(state_dir=self.state, default_mode='direct')
            self.assertEqual(self.ask({'path': str(self.repo)}, direct), self.launch('direct', self.repo))

    # -- the folder a run works in --------------------------------------------------------------------
    def test_a_worktree_source_may_hold_any_character_the_folder_a_run_works_in_may_not(self) -> None:
        for name in ('project with spaces', 'caf\u00e9'):
            source = self.root / name
            repos.make_repo(source)
            for value in ({'path': str(source)}, {'path': str(source), 'mode': 'worktree'}):
                self.assertEqual(self.ask(value), self.launch('worktree', source), value)
            self.assertEqual(self.ask({'path': str(source), 'mode': 'direct'}),
                             H.Refusal('workspace_invalid', 'not_unit_safe'), name)
        plain = self.root / 'plain folder'
        plain.mkdir()
        self.assertEqual(self.ask({'path': str(plain)}), H.Refusal('workspace_invalid', 'not_unit_safe'))

    def test_in_worktree_mode_the_run_works_in_the_folders_place_in_its_worktree(self) -> None:
        (self.repo / 'sub dir').mkdir()
        self.assertEqual(self.ask({'path': str(self.repo / 'sub dir')}), H.Refusal('workspace_invalid', 'not_unit_safe'))
        self.assertEqual(self.ask({'path': str(self.repo / 'sub dir'), 'mode': 'direct'}),
                         H.Refusal('workspace_invalid', 'not_unit_safe'))
        self.assertEqual(self.ask({'path': str(self.repo / 'pkg')}), self.launch('worktree', self.repo / 'pkg'))

    def test_in_worktree_mode_the_state_directory_is_part_of_the_folder_a_run_works_in(self) -> None:
        state = self.root / 'state dir'
        state.mkdir()
        normalize = S.Normalizer(state_dir=state)
        self.assertEqual(self.ask({'path': str(self.repo)}, normalize), H.Refusal('workspace_invalid', 'not_unit_safe'))
        self.assertEqual(self.ask({'path': str(self.repo), 'mode': 'direct'}, normalize), self.launch('direct', self.repo))
        self.assertEqual(self.ask({'path': str(self.plain)}, normalize), self.launch('direct', self.plain))

    def test_a_base_ref_is_kept_as_given(self) -> None:
        self.assertEqual(self.ask({'path': str(self.repo), 'base_ref': 'origin/main~2'}),
                         self.launch('worktree', self.repo, 'origin/main~2'))
        self.assertEqual(self.ask({'path': str(self.plain), 'base_ref': 'HEAD'}), self.launch('direct', self.plain, 'HEAD'))

    def test_a_base_ref_git_would_misread_is_refused(self) -> None:
        for base_ref in ('--upload-pack=x', 'a..b', 'main branch', 'x@{1}'):
            self.assertEqual(self.ask({'path': str(self.repo), 'base_ref': base_ref}),
                             H.Refusal('workspace_invalid', 'base_ref_invalid'), base_ref)

    # -- refusals -------------------------------------------------------------------------------------
    def test_a_value_that_is_not_a_workspace_request_is_request_invalid(self) -> None:
        for value in ('/a/folder', ['/a'], 5, True, {}, {'mode': 'direct'}, {'path': 5}, {'path': ''}, {'path': None},
                      {'path': str(self.plain), 'extra': 1}, {'path': str(self.plain), 'mode': 'other'},
                      {'path': str(self.plain), 'mode': None}, {'path': str(self.plain), 'base_ref': ''},
                      {'path': str(self.plain), 'base_ref': 7}, {'path': str(self.plain), 'base_ref': None},
                      {'path': str(self.plain), 'worktree_path': '/x'}, {'path': str(self.plain), 'source_path': '/x'}):
            self.assertEqual(self.ask(value), H.Refusal('request_invalid', 'workspace'), repr(value))

    def test_the_request_shapes_refused_are_the_ones_the_published_schema_refuses(self) -> None:
        if fits is None:
            self.skipTest('lane SPINE\'s conformance reader and schemas are not in this tree')
        ref = 'methods.v1.json#/$defs/workspace_request'
        for value in ({'path': '/a'}, {'path': '/a', 'mode': 'direct'}, {'path': '/a', 'mode': 'worktree', 'base_ref': 'main'}):
            self.assertTrue(fits(ref, value), value)
        for value in ('/a', {}, {'path': 5}, {'path': ''}, {'path': '/a', 'extra': 1}, {'path': '/a', 'mode': 'other'},
                      {'path': '/a', 'mode': None}, {'path': '/a', 'base_ref': ''}, {'path': '/a', 'base_ref': None}):
            self.assertFalse(fits(ref, value), value)
            self.assertEqual(self.ask(value), H.Refusal('request_invalid', 'workspace'), value)

    def test_a_folder_that_breaks_a_rule_is_workspace_invalid_with_its_reason(self) -> None:
        (self.root / 'file').write_text('x')
        cases = [('relative', 'not_absolute'), (str(self.root / 'missing'), 'not_found'),
                 (str(self.root / 'file'), 'not_a_directory'), (str(self.state), 'state_directory'),
                 (str(self.repo / '..' / 'repo'), 'dot_dot')]
        if os.geteuid() != 0:
            cases.append(('/usr', 'not_owned'))
        for path, reason in cases:
            self.assertEqual(self.ask({'path': path}), H.Refusal('workspace_invalid', reason), path)

    def test_a_protected_folder_is_refused_like_the_state_directory(self) -> None:
        runtime = self.root / 'runtime'
        (runtime / 'x').mkdir(parents=True)
        normalize = S.Normalizer(state_dir=self.state, protected=(runtime,))
        self.assertEqual(self.ask({'path': str(runtime / 'x')}, normalize), H.Refusal('workspace_invalid', 'state_directory'))
        self.assertEqual(self.ask({'path': str(runtime / 'x')}), self.launch('direct', runtime / 'x'))

    def test_the_state_directory_left_out_is_the_daemons(self) -> None:
        state = self.root / 'xdg' / 'recursion'
        state.mkdir(parents=True)
        normalize = S.Normalizer()
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'xdg')}):
            self.assertEqual(S.default_state_dir(), state)
            self.assertEqual(self.ask({'path': str(state)}, normalize), H.Refusal('workspace_invalid', 'state_directory'))
            self.assertEqual(self.ask({'path': str(self.plain)}, normalize), self.launch('direct', self.plain))
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': ''}):
            self.assertEqual(S.default_state_dir(), pathlib.Path.home() / '.local/state' / 'recursion')

    def test_the_default_state_directory_is_the_one_the_daemon_makes(self) -> None:
        source = pathlib.Path(H.__file__).resolve().parents[1] / 'daemon' / '__main__.py'
        if not source.is_file():
            self.skipTest('the daemon is not in this tree')
        self.assertIn("state = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'recursion'",
                      source.read_text(encoding='utf-8'))

    def test_every_principal_is_held_to_the_same_rules(self) -> None:
        for principal in (H.OWNER, H.DEVICE, H.Principal('plugin', 'acme.tools'), H.Principal('session', 'ses-1')):
            self.assertEqual(self.ask({'path': str(self.plain)}, principal=principal), self.launch('direct', self.plain))
            self.assertEqual(self.ask({'path': str(self.state)}, principal=principal),
                             H.Refusal('workspace_invalid', 'state_directory'))

    def test_what_it_answers_is_a_published_launch_workspace(self) -> None:
        if fits is None:
            self.skipTest('lane SPINE\'s conformance reader and schemas are not in this tree')
        for value in ({'path': str(self.plain)}, {'path': str(self.repo)}, {'path': str(self.repo), 'base_ref': 'main~1'},
                      {'path': str(self.repo), 'mode': 'direct'}):
            part = self.ask(value)
            self.assertTrue(fits('run.record.v1.json#/$defs/launch_workspace', part['workspace']), part)

    # -- purity ---------------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Every name under the test's folder with what a change would touch."""
        seen = {}
        for folder, names, files in os.walk(self.root):
            for name in names + files:
                path = os.path.join(folder, name)
                info = os.lstat(path)
                seen[path] = (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return seen

    def test_it_creates_writes_and_runs_nothing(self) -> None:
        before = self.snapshot()

        def no_process(*args, **kwargs):
            raise AssertionError('the normaliser started a process')

        with mock.patch.object(subprocess, 'Popen', no_process), mock.patch.object(os, 'fork', no_process), \
                mock.patch.object(os, 'posix_spawn', no_process), mock.patch.object(os, 'system', no_process):
            for value in ({'path': str(self.repo)}, {'path': str(self.repo), 'mode': 'worktree', 'base_ref': 'main'},
                          {'path': str(self.plain)}, {'path': str(self.plain), 'mode': 'worktree'},
                          {'path': str(self.state)}, {'path': str(self.root / 'missing')}):
                self.ask(value)
        self.assertEqual(self.snapshot(), before)


@unittest.skipUnless(POSIX, 'the workspace checks are POSIX: the daemon runs on Linux')
class RegistrationTest(unittest.TestCase):
    """The registrant through lane SPINE's hooks, as the seam runs it for a new start."""

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-register-')))
        self.addCleanup(shutil.rmtree, self.root)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.folder = self.root / 'folder'
        self.folder.mkdir()
        if S.git_work_tree(self.folder) is not None:
            self.skipTest('the temporary folder is itself inside a git work tree')
        self.hooks = H.Hooks()
        S.register(self.hooks, normalizer=S.Normalizer(state_dir=self.state))
        self.hooks.freeze()

    def test_it_declares_the_field_and_the_key_workspace_and_nothing_else(self) -> None:
        self.assertTrue(self.hooks.start_on)
        self.assertEqual(self.hooks.declared_fields(), frozenset({'workspace'}))
        self.assertEqual(self.hooks.start_order(), ('workspace',))
        self.assertEqual(self.hooks._start['workspace'][1], ('workspace',))
        self.assertFalse(self.hooks.engine_may_be_chosen)

    def test_the_snapshot_carries_the_workspace(self) -> None:
        launch, refusal = self.hooks.launch_snapshot(H.OWNER, {'tenant': 't1', 'prompt': 'x', 'engine': 'codex',
                                                               'workspace': {'path': str(self.folder)}})
        self.assertIsNone(refusal)
        self.assertEqual(launch['workspace'], {'mode': 'direct', 'source_path': str(self.folder), 'base_ref': None,
                                               'worktree_path': None})
        self.assertEqual(set(launch), set(H.LAUNCH_KEYS))

    def test_with_no_workspace_the_snapshot_has_none(self) -> None:
        launch, refusal = self.hooks.launch_snapshot(H.OWNER, {'tenant': 't1', 'prompt': 'x', 'engine': 'codex'})
        self.assertIsNone(refusal)
        self.assertIsNone(launch['workspace'])

    def test_a_refusal_reaches_the_answer_with_its_code_and_reason(self) -> None:
        launch, refusal = self.hooks.launch_snapshot(H.OWNER, {'tenant': 't1', 'prompt': 'x',
                                                               'workspace': {'path': str(self.state)}})
        self.assertIsNone(launch)
        self.assertEqual(refusal, {'code': 'workspace_invalid', 'detail': 'state_directory'})
        launch, refusal = self.hooks.launch_snapshot(H.OWNER, {'tenant': 't1', 'prompt': 'x', 'workspace': 'nope'})
        self.assertEqual(refusal, {'code': 'request_invalid', 'detail': 'workspace'})

    def test_it_runs_after_routing_and_beside_another_lane(self) -> None:
        order = []

        def routing(principal, request):
            order.append('routing')
            return {'route_entry': None}

        def quota(principal, request):
            order.append('quota')
            return {'budget': {'cap_usd': 0}}

        hooks = H.Hooks()
        hooks.start_registrant('quota', (), ('budget',), quota)
        S.register(hooks, normalizer=S.Normalizer(state_dir=self.state))
        hooks.start_registrant('routing', (), ('route_entry',), routing, chooses_engine=True)
        hooks.freeze()
        launch, refusal = hooks.launch_snapshot(H.OWNER, {'tenant': 't1', 'prompt': 'x',
                                                          'workspace': {'path': str(self.folder)}})
        self.assertIsNone(refusal)
        self.assertEqual(order, ['routing', 'quota'])
        self.assertEqual(hooks.start_order(), ('routing', 'workspace', 'quota'))
        self.assertEqual(launch['workspace']['source_path'], str(self.folder))

    def test_registering_twice_fails_composition(self) -> None:
        hooks = H.Hooks()
        S.register(hooks)
        with self.assertRaises(H.HookError):
            S.register(hooks)

    def test_the_package_registers_nothing_with_a_composition_yet(self) -> None:
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('recursion_core.workspace'))
        self.assertIsNone(importlib.util.find_spec('recursion_core.workspace.hooks'))
        self.assertIsNone(H._load_workspace_hooks())


if __name__ == '__main__':
    unittest.main()
