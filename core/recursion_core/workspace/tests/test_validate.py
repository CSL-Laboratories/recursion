"""The checks on a folder a run is asked to work in (workspace/validate.py).

Each rule has a folder that breaks it and nothing else; the folder that breaks none comes back resolved. The
folders are made under the test's temporary folder; ``/usr`` stands for a folder another user owns.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from recursion_core.workspace import validate as V

POSIX = os.name == 'posix'


@unittest.skipUnless(POSIX, 'the workspace checks are POSIX: the daemon runs on Linux')
class ValidateTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-validate-')))
        self.addCleanup(self._remove)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()

    def _remove(self) -> None:
        for folder, names, _ in os.walk(self.root):
            for name in names:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        shutil.rmtree(self.root)

    def refused(self, path, reason, **options) -> None:
        options.setdefault('state_dir', self.state)
        with self.assertRaises(V.WorkspaceInvalid) as caught:
            V.validate(path, **options)
        self.assertEqual(caught.exception.reason, reason)

    def test_an_operator_folder_comes_back_resolved(self) -> None:
        self.assertEqual(V.validate(str(self.work), state_dir=self.state), self.work)
        self.assertEqual(V.validate(str(self.work) + '/', state_dir=self.state), self.work)
        self.assertEqual(V.validate(str(self.root / '.' / 'work'), state_dir=self.state), self.work)

    def test_a_link_to_another_operator_folder_answers_the_folder_it_leads_to(self) -> None:
        other = self.root / 'other'
        other.mkdir()
        link = self.work / 'there'
        link.symlink_to(other)
        self.assertEqual(V.validate(str(link), state_dir=self.state), other)

    def test_a_path_that_is_not_text_is_refused(self) -> None:
        for value in (None, '', 5, b'/tmp', str(self.work) + '\x00', str(self.work) + '\n', '/a\rb'):
            self.refused(value, 'not_text')

    def test_a_relative_path_is_refused(self) -> None:
        self.refused('work', 'not_absolute')
        self.refused('./work', 'not_absolute')

    def test_a_dot_dot_part_is_refused_even_when_it_leads_back(self) -> None:
        self.refused(str(self.work / '..' / 'work'), 'dot_dot')

    def test_nothing_there_is_refused(self) -> None:
        self.refused(str(self.root / 'missing'), 'not_found')
        (self.root / 'file').write_text('x')
        self.refused(str(self.root / 'file' / 'below'), 'not_found')
        (self.work / 'dangling').symlink_to(self.root / 'gone')
        self.refused(str(self.work / 'dangling'), 'not_found')

    def test_a_loop_of_links_is_refused(self) -> None:
        (self.root / 'a').symlink_to(self.root / 'b')
        (self.root / 'b').symlink_to(self.root / 'a')
        self.refused(str(self.root / 'a'), 'link_loop')

    @unittest.skipIf(POSIX and os.geteuid() == 0, 'root reads every folder')
    def test_a_path_through_a_folder_the_operator_cannot_enter_is_refused(self) -> None:
        shut = self.root / 'shut'
        (shut / 'inner').mkdir(parents=True)
        os.chmod(shut, 0)
        self.refused(str(shut / 'inner'), 'unreadable')

    def test_what_is_not_a_folder_is_refused(self) -> None:
        (self.root / 'file').write_text('x')
        self.refused(str(self.root / 'file'), 'not_a_directory')
        os.mkfifo(self.root / 'pipe')
        self.refused(str(self.root / 'pipe'), 'not_a_directory')
        (self.work / 'to-file').symlink_to(self.root / 'file')
        self.refused(str(self.work / 'to-file'), 'not_a_directory')

    @unittest.skipIf(POSIX and os.geteuid() == 0, 'root owns /usr')
    def test_a_folder_another_user_owns_is_refused(self) -> None:
        self.assertNotEqual(os.stat('/usr').st_uid, os.geteuid())
        self.refused('/usr', 'not_owned')

    @unittest.skipIf(POSIX and os.geteuid() == 0, 'root owns /usr')
    def test_a_link_out_of_the_operators_files_is_refused(self) -> None:
        (self.work / 'out').symlink_to('/usr')
        self.refused(str(self.work / 'out'), 'not_owned')

    def test_the_operator_is_the_uid_given(self) -> None:
        self.refused(str(self.work), 'not_owned', uid=os.geteuid() + 1)
        self.assertEqual(V.validate(str(self.work), state_dir=self.state, uid=os.geteuid()), self.work)

    def test_a_folder_its_owner_may_not_list_or_enter_is_refused(self) -> None:
        for mode in (0o300, 0o600, 0o000):
            os.chmod(self.work, mode)
            self.refused(str(self.work), 'not_accessible')
        os.chmod(self.work, 0o500)
        self.assertEqual(V.validate(str(self.work), state_dir=self.state), self.work)

    def test_the_state_directory_and_what_is_inside_it_are_refused(self) -> None:
        inner = self.state / 'runs' / 'run-1'
        inner.mkdir(parents=True)
        self.refused(str(self.state), 'state_directory')
        self.refused(str(inner), 'state_directory')
        (self.work / 'into-state').symlink_to(inner)
        self.refused(str(self.work / 'into-state'), 'state_directory')

    def test_a_state_directory_named_through_a_link_is_refused_all_the_same(self) -> None:
        (self.root / 'state-link').symlink_to(self.state)
        self.refused(str(self.state), 'state_directory', state_dir=self.root / 'state-link')

    def test_a_folder_beside_the_state_directory_with_a_longer_name_is_not_inside_it(self) -> None:
        beside = self.root / 'state2'
        beside.mkdir()
        self.assertEqual(V.validate(str(beside), state_dir=self.state), beside)

    def test_a_protected_folder_is_refused_like_the_state_directory(self) -> None:
        runtime = self.root / 'runtime'
        (runtime / 'x').mkdir(parents=True)
        self.refused(str(runtime / 'x'), 'state_directory', protected=(runtime,))
        self.assertEqual(V.validate(str(runtime / 'x'), state_dir=self.state), runtime / 'x')

    def test_no_state_directory_refuses_none(self) -> None:
        self.assertEqual(V.validate(str(self.state), state_dir=None), self.state)

    def test_a_folder_the_launcher_could_not_name_in_a_unit_is_refused(self) -> None:
        for name in ('with space', 'quote"d', 'semi;colon', 'd\u00e9j\u00e0'):
            folder = self.root / name
            folder.mkdir()
            self.refused(str(folder), 'not_unit_safe')
        (self.work / 'safe').symlink_to(self.root / 'with space')
        self.refused(str(self.work / 'safe'), 'not_unit_safe')

    def test_the_unit_safe_characters_are_the_launchers(self) -> None:
        try:
            from recursion_core.daemon import launcher
        except ImportError as exc:  # pragma: no cover - the daemon is Linux only
            self.skipTest(f'the daemon cannot be imported here: {exc}')
        self.assertEqual(V.UNIT_PATH_SAFE, launcher._PATH_SAFE)

    def test_every_reason_is_a_code_word(self) -> None:
        for reason in V.REASONS:
            self.assertRegex(reason, r'^[a-z][a-z0-9_]{0,63}$')
        with self.assertRaises(ValueError):
            V.WorkspaceInvalid('anything_else')


@unittest.skipUnless(POSIX, 'the workspace checks are POSIX: the daemon runs on Linux')
class GitWorkTreeTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-git-')))
        self.addCleanup(shutil.rmtree, self.root)

    def repo(self, name: str) -> pathlib.Path:
        top = self.root / name
        (top / '.git').mkdir(parents=True)
        (top / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
        return top

    def test_a_repository_and_a_folder_inside_it_are_in_its_work_tree(self) -> None:
        top = self.repo('repo')
        (top / 'pkg' / 'deep').mkdir(parents=True)
        self.assertEqual(V.git_work_tree(top), top)
        self.assertEqual(V.git_work_tree(top / 'pkg' / 'deep'), top)

    def test_a_linked_worktree_has_a_git_file(self) -> None:
        tree = self.root / 'linked'
        tree.mkdir()
        (tree / '.git').write_text('gitdir: /elsewhere/.git/worktrees/linked\n')
        self.assertEqual(V.git_work_tree(tree), tree)

    def test_a_folder_in_no_repository_is_in_none(self) -> None:
        plain = self.root / 'plain'
        plain.mkdir()
        if V.git_work_tree(self.root) is not None:
            self.skipTest('the temporary folder is itself inside a git work tree')
        self.assertIsNone(V.git_work_tree(plain))

    def test_a_dot_git_that_is_not_a_repository_is_passed_over(self) -> None:
        if V.git_work_tree(self.root) is not None:
            self.skipTest('the temporary folder is itself inside a git work tree')
        for name, make in (('empty-dir', lambda marker: marker.mkdir()),
                           ('other-file', lambda marker: marker.write_text('not a git file\n'))):
            top = self.root / name
            top.mkdir()
            make(top / '.git')
            self.assertIsNone(V.git_work_tree(top), name)

    def test_the_nearest_repository_wins(self) -> None:
        outer = self.repo('outer')
        inner = outer / 'vendor' / 'inner'
        (inner / '.git').mkdir(parents=True)
        (inner / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
        self.assertEqual(V.git_work_tree(inner / '.'), inner)

    def test_the_search_stops_at_a_mount(self) -> None:
        top = self.repo('mounted')
        below = top / 'below'
        below.mkdir()
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if pathlib.Path(path) == top:
                fields = list(info)
                fields[2] = info.st_dev + 1        # st_dev: the repository is on another device
                return os.stat_result(fields)
            return info

        with mock.patch.object(V.os, 'stat', stat):
            self.assertIsNone(V.git_work_tree(below))
        self.assertEqual(V.git_work_tree(below), top)

    def test_a_folder_that_is_gone_is_in_none(self) -> None:
        self.assertIsNone(V.git_work_tree(self.root / 'missing'))


class BaseRefTest(unittest.TestCase):

    def test_ref_names_and_commit_ids_with_steps_are_taken(self) -> None:
        for text in ('main', 'HEAD', 'refs/heads/main', 'origin/main', 'v1.0', 'feature/x-y_z', 'release-2026.11',
                     'HEAD~2', 'main^', 'main^2~3', 'HEAD~', 'abc1234', '1234567', 'a' * 40, '0' * 64, 'x{y}',
                     'a/b/c/d'):
            self.assertTrue(V.valid_base_ref(text), text)

    def test_what_git_would_read_otherwise_or_refuse_is_refused(self) -> None:
        for text in (None, 5, '', '-x', '--upload-pack=evil', 'a..b', 'a b', 'a:b', 'a?b', 'a*b', 'a[b', 'a\\b',
                     '@', 'x@{1}', '/a', 'a/', 'a//b', '.a', 'a/.b', 'a.lock', 'a/b.lock', 'a.', 'caf\u00e9',
                     'x' * 257, '~1', '^', 'main~x', 'HEAD^{tree}', 'a\tb', 'a\x7fb', 'main\n'):
            self.assertFalse(V.valid_base_ref(text), repr(text))


if __name__ == '__main__':
    unittest.main()
