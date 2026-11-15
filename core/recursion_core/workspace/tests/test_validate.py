"""The checks on a folder a run is asked to work in (workspace/validate.py).

Each rule has a folder that breaks it and nothing else; the folder that breaks none comes back resolved. The
folders are made under the test's temporary folder; ``/usr`` stands for a folder another user owns.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.workspace import validate as V
from recursion_core.workspace.tests import repos

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

    def test_any_character_is_taken_here_the_unit_rule_is_for_the_folder_a_run_works_in(self) -> None:
        for name in ('with space', 'quote"d', 'semi;colon', 'd\u00e9j\u00e0'):
            folder = self.root / name
            folder.mkdir()
            self.assertEqual(V.validate(str(folder), state_dir=self.state), folder)
            self.assertFalse(V.unit_safe(folder), name)
            self.assertFalse(V.unit_safe(str(folder)), name)
        self.assertTrue(V.unit_safe(self.work))
        self.assertTrue(V.unit_safe('/home/dev/a-b_c.d/e+f@g,h'))

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


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the workspace checks are POSIX, and these repositories are made by git')
class GitWorkTreeTest(unittest.TestCase):
    """Real repositories, made by git. Every answer expected here is the one git 2.43 gave for the same layout
    (``git rev-parse --is-inside-work-tree --show-toplevel``)."""

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-git-')))
        self.addCleanup(self._remove)
        if V.git_work_tree(self.root) is not None:
            self.skipTest('the temporary folder is itself inside a git work tree')

    def _remove(self) -> None:
        for folder, names, _ in os.walk(self.root):
            for name in names:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        shutil.rmtree(self.root)

    def repo(self, name: str) -> pathlib.Path:
        top = self.root / name
        repos.make_repo(top)
        return top

    def test_a_repository_and_a_folder_inside_it_are_in_its_work_tree(self) -> None:
        top = self.repo('repo')
        (top / 'pkg' / 'deep').mkdir(parents=True)
        self.assertEqual(V.git_work_tree(top), top)
        self.assertEqual(V.git_work_tree(top / 'pkg' / 'deep'), top)

    def test_a_linked_worktree_is_its_own_work_tree_through_its_common_directory(self) -> None:
        top = self.repo('main')
        linked = self.root / 'linked'
        repos.git(top, 'worktree', 'add', '-q', '--detach', str(linked), 'HEAD')
        self.assertEqual(V.git_work_tree(linked), linked)
        common = top / '.git' / 'worktrees' / 'linked' / 'commondir'
        common.write_bytes(b'')                     # git: "failed to read"
        self.assertIsNone(V.git_work_tree(linked))
        common.unlink()                             # git: "not a git repository": no objects or refs of its own
        self.assertIsNone(V.git_work_tree(linked))

    def test_a_git_file_names_the_git_directory_and_a_relative_name_is_its_folders(self) -> None:
        tree = self.root / 'tree'
        tree.mkdir()
        repos.git(tree, 'init', '-q', '--separate-git-dir', str(self.root / 'admin'))
        self.assertEqual(V.git_work_tree(tree), tree)
        (tree / '.git').write_bytes(b'gitdir: ../admin\r\n')
        self.assertEqual(V.git_work_tree(tree), tree)
        name = b'gitdir: ../admin'
        (tree / '.git').write_bytes(name + b'\n' * ((1 << 20) - len(name)))       # 1 MiB exactly: git reads it
        self.assertEqual(V.git_work_tree(tree), tree)
        (tree / '.git').write_bytes(name + b'\n' * ((1 << 20) - len(name) + 1))   # a byte more: "too large"
        self.assertIsNone(V.git_work_tree(tree))

    def test_a_dot_git_link_counts_as_what_it_leads_to(self) -> None:
        top = self.repo('repo')
        (top / '.git').rename(self.root / 'admin')
        (top / '.git').symlink_to(self.root / 'admin', target_is_directory=True)
        self.assertEqual(V.git_work_tree(top), top)
        tree = self.root / 'tree'
        tree.mkdir()
        (self.root / 'gitfile').write_bytes(b'gitdir: ' + bytes(self.root / 'admin') + b'\n')
        (tree / '.git').symlink_to(self.root / 'gitfile')
        self.assertEqual(V.git_work_tree(tree), tree)

    def test_a_git_file_with_no_path_in_a_bare_repository_is_refused(self) -> None:
        bare = self.root / 'bare.git'
        bare.mkdir()
        repos.git(bare, 'init', '-q', '--bare')
        (bare / '.git').write_bytes(b'gitdir: \n')       # git: "no path in gitfile"
        self.assertIsNone(V.git_work_tree(bare))

    def test_inside_a_git_directory_is_in_no_work_tree(self) -> None:
        top = self.repo('repo')
        self.assertIsNone(V.git_work_tree(top / '.git'))
        self.assertIsNone(V.git_work_tree(top / '.git' / 'objects'))
        bare = self.root / 'bare.git'
        bare.mkdir()
        repos.git(bare, 'init', '-q', '--bare')
        self.assertIsNone(V.git_work_tree(bare))
        self.assertIsNone(V.git_work_tree(bare / 'refs'))

    @staticmethod
    def _not_a_git_directory(kind: str, marker: pathlib.Path, gone: pathlib.Path) -> None:
        if kind == 'head-only':
            marker.mkdir()
            (marker / 'HEAD').write_text('ref: refs/heads/main\n')
        elif kind == 'empty-folder':
            marker.mkdir()
        elif kind == 'dangling-link':
            marker.symlink_to(gone)
        else:
            os.mkfifo(marker)

    def test_a_dot_git_that_is_no_git_directory_is_passed_over(self) -> None:
        outer = self.repo('outer')
        for kind in ('head-only', 'empty-folder', 'dangling-link', 'pipe'):
            inside, alone = outer / kind, self.root / ('alone-' + kind)
            for folder in (inside, alone):
                folder.mkdir()
                self._not_a_git_directory(kind, folder / '.git', self.root / 'gone')
            self.assertEqual(V.git_work_tree(inside), outer, kind)
            self.assertIsNone(V.git_work_tree(alone), kind)

    def test_a_dot_git_file_git_refuses_ends_the_search(self) -> None:
        outer = self.repo('outer')
        admin = bytes(self.repo('admin') / '.git')
        (outer / 'plain').mkdir()
        refused = {'malformed': b'not a gitfile\n', 'empty': b'', 'no-path': b'gitdir: \n',
                   'no-space': b'gitdir:' + admin + b'\n', 'upper-case': b'GITDIR: ' + admin + b'\n',
                   'nothing-there': b'gitdir: ../missing\n',
                   'no-git-directory': b'gitdir: ../plain\n', 'too-large': b'gitdir: ' + admin + b'\n' * (1 << 20)}
        for name, content in refused.items():
            folder = outer / name
            folder.mkdir()
            (folder / '.git').write_bytes(content)
            self.assertIsNone(V.git_work_tree(folder), name)
        taken = outer / 'taken'
        taken.mkdir()
        (taken / '.git').write_bytes(b'gitdir: ' + admin + b'\n')
        self.assertEqual(V.git_work_tree(taken), taken)

    def test_a_git_directory_has_a_head_objects_and_refs_git_accepts(self) -> None:
        heads = {b'ref: refs/heads/main\n': True, b'ref:\t refs/heads/main\n': True,
                 b'0123456789abcdef0123456789ABCDEF01234567\n': True, b'ref:\x0brefs/heads/main\n': False,
                 b'ref: heads/main\n': False, b'garbage\n': False, b'0123456789abcdef\n': False}
        for number, (head, taken) in enumerate(heads.items()):
            top = self.repo(f'head-{number}')
            (top / '.git' / 'HEAD').write_bytes(head)
            self.assertEqual(V.git_work_tree(top), top if taken else None, head)
        top = self.repo('head-link')
        for target, taken in (('refs/heads/main', True), ('heads/main', False)):
            (top / '.git' / 'HEAD').unlink()
            (top / '.git' / 'HEAD').symlink_to(target)
            self.assertEqual(V.git_work_tree(top), top if taken else None, target)
        for missing in ('objects', 'refs'):
            top = self.repo('no-' + missing)
            shutil.rmtree(top / '.git' / missing)
            self.assertIsNone(V.git_work_tree(top), missing)
        top = self.repo('common')
        (top / '.git' / 'commondir').write_bytes(b'\n')      # names the git directory itself: git takes it
        self.assertEqual(V.git_work_tree(top), top)
        (top / '.git' / 'commondir').write_bytes(b'')        # git: "failed to read"
        self.assertIsNone(V.git_work_tree(top))

    @unittest.skipIf(POSIX and os.geteuid() == 0, 'root enters every folder')
    def test_objects_and_refs_must_be_folders_that_may_be_entered(self) -> None:
        for shut in ('objects', 'refs'):
            top = self.repo('shut-' + shut)
            os.chmod(top / '.git' / shut, 0o600)
            self.assertIsNone(V.git_work_tree(top), shut)

    def test_a_pipe_where_git_reads_a_file_answers_at_once(self) -> None:
        top = self.root / 'piped'
        (top / '.git').mkdir(parents=True)
        os.mkfifo(top / '.git' / 'HEAD')
        answers = []
        reader = threading.Thread(target=lambda: answers.append(V.git_work_tree(top)), daemon=True)
        reader.start()
        reader.join(20)
        self.assertFalse(reader.is_alive(), 'reading the pipe waited for a writer')
        self.assertEqual(answers, [None])

    def test_a_folder_in_no_repository_is_in_none(self) -> None:
        plain = self.root / 'plain'
        plain.mkdir()
        self.assertIsNone(V.git_work_tree(plain))

    def test_the_nearest_repository_wins(self) -> None:
        outer = self.repo('outer')
        inner = outer / 'vendor' / 'inner'
        repos.make_repo(inner)
        self.assertEqual(V.git_work_tree(inner / '.'), inner)
        self.assertEqual(V.git_work_tree(outer / 'vendor'), outer)

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
                     'a/b/c/d', 'HEAD~0000001', 'main~12345678901^2'):
            self.assertTrue(V.valid_base_ref(text), text)

    def test_what_git_would_read_otherwise_or_refuse_is_refused(self) -> None:
        for text in (None, 5, '', '-x', '--upload-pack=evil', 'a..b', 'a b', 'a:b', 'a?b', 'a*b', 'a[b', 'a\\b',
                     '@', 'x@{1}', '/a', 'a/', 'a//b', '.a', 'a/.b', 'a.lock', 'a/b.lock', 'a.', 'caf\u00e9',
                     'x' * 257, '~1', '^', 'main~x', 'HEAD^{tree}', 'a\tb', 'a\x7fb', 'main\n'):
            self.assertFalse(V.valid_base_ref(text), repr(text))


if __name__ == '__main__':
    unittest.main()
