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


HEADER = '[core]\n\trepositoryformatversion = 0\n'

# What git 2.43 made of a boolean's value (``core.bare``, ``extensions.preciousObjects``): True, False, or None
# where it refused the value.
BOOLEANS = {'true': True, 'TRUE': True, 'yes': True, 'on': True, '1': True, '2': True, '-1': True, '0x10': True,
            '0X1f': True, '1k': True, '1K': True, '1m': True, '1g': True, '010': True, '0b1': True, '+1': True,
            '017777777777': True, '0x7fffffff': True, '2147483647': True, '-2147483647': True, '"\\t1"': True,
            '"\\n1"': True, '"true"': True, '"tr"ue': True, 'true # a comment': True, 'true;c': True,
            '': False, 'false': False, 'no': False, 'off': False, '0': False, '00': False, '-0': False, '0k': False,
            'maybe': None, 'y': None, 'tru': None, '2g': None, '08': None, '0x': None, '2147483648': None,
            '-2147483648': None, '0x80000000': None, '020000000000': None, '1 k': None, '"1 "': None, '"yes "': None,
            '1kb': None, '1e3': None, '"\\b1"': None, '- 1': None, '+-1': None, '1t': None, 'tr\\ue': None,
            '" true"': None, '9' * 5000: None, '1' * 4400: None, '0' * 5000: False, '-' + '0' * 5000 + '1': True,
            '0' * 5000 + 'k': False, '0' + '7' * 5000: None}

# Config files, and what git 2.43 made of each, asked from the folder they name: that top taken, the setting
# ignored, or the file refused. <W> is the folder as a quoted value, <W1> and <W2> its two halves, <RAW> its path
# unquoted; the file ``extra`` beside the config names it too.
CONFIG_TEXTS = (
    (HEADER + '\tworktree = <W>\n', 'taken'),
    (HEADER + '[core] worktree = <W>\n', 'taken'),
    (HEADER + '[core "x"]\n\tworktree = <W>\n', 'ignored'),
    (HEADER + '[core.x]\n\tworktree = <W>\n', 'ignored'),
    ('[CORE]\n\tRepositoryFormatVersion = 0\n\tWorkTree = <W>\n', 'taken'),
    ((HEADER + '\tworktree = <W>\n').replace('\n', '\r\n'), 'taken'),
    ((HEADER + '\tfoo\n\tworktree = <W>\n').replace('\n', '\r\n'), 'taken'),
    (HEADER + '\tworktree = <W1>\\\r\n<W2>\r\n', 'taken'),
    (b'\xef\xbb\xbf' + (HEADER + '\tworktree = <W>\n').encode(), 'taken'),
    (b'\xef\xbb' + (HEADER + '\tworktree = <W>\n').encode(), 'refused'),
    (HEADER + '\tworktree = <W1>\\\n<W2>\n', 'taken'),
    (HEADER + '\tworktree = <W>\\q\n', 'refused'),
    (HEADER + '\tworktree = "<RAW>\n', 'refused'),
    (HEADER + '!!!\n\tworktree = <W>\n', 'refused'),
    ('worktree = <W>\n' + HEADER, 'ignored'),
    (HEADER + '# worktree = /x\n; worktree = /x\n\tworktree = <W> ; a comment\n', 'taken'),
    (HEADER + '\tfoo\n\tworktree = <W>\n', 'taken'),
    (HEADER + '[]\n', 'refused'),
    (HEADER + '\t1worktree = x\n', 'refused'),
    (HEADER + '\twork_tree = x\n', 'refused'),
    (HEADER + '\twork-tree = x\n[co-re]\n\tx = 1\n[core]\n\tworktree = <W>\n', 'taken'),
    (HEADER + 'worktree=<W>\n', 'taken'),
    (HEADER + '\tworktree\t=\t<W>\t\n', 'taken'),
    (HEADER + '[ core ]\n', 'refused'),
    (HEADER + '[core ]\n', 'refused'),
    (HEADER + '[core "x" ]\n', 'refused'),
    (HEADER + '[core\n', 'refused'),
    (HEADER + '[co_re]\n', 'refused'),
    (HEADER + '[core "a\\"b"]\n\tx = 1\n[core]\n\tworktree = <W>\n', 'taken'),
    (HEADER + '[core "a\n"]\n', 'refused'),
    (HEADER + '[core "x"\n\ty = 1\n[core]\n\tworktree = <W>\n', 'refused'),
    (HEADER + '\tworktree = <W>\n[core x"]\n', 'refused'),
    (HEADER + '[ "x"]\n\ty = 1\n[core]\n\tworktree = <W>\n', 'taken'),
    (HEADER + '[core] = x\n', 'refused'),
    (HEADER + '\tworktree <W>\n', 'refused'),
    (HEADER + '\twork\x00tree = x\n', 'refused'),
    (HEADER + '\tworktree = <W>\x00junk\n', 'taken'),
    (HEADER + '\tworktree = <W>', 'taken'),
    (HEADER + '\tworktree = <W>\r', 'taken'),
    (HEADER + '\tworktree = <W>\\', 'taken'),
    (HEADER + '[core', 'refused'),
    (HEADER + '\x0c\tworktree = <W>\n', 'refused'),
    (HEADER + '\twork\xe9tree = x\n', 'refused'),
    (HEADER + '\tworktree = /nonexistent-recursion-place\n\tworktree = <W>\n', 'taken'),
    (HEADER + '[include]\n\tpath = extra\n', 'ignored'),
    (HEADER + '[includeIf "gitdir:/"]\n\tpath = extra\n', 'ignored'),
    # white space outside quotes is one ASCII space a character (the third reads); inside quotes it is kept
    (HEADER + '\tworktree = <TOP>/na\tmed\n', 'taken'),
    (HEADER + '\tworktree = <TOP>/na\rmed\n', 'taken'),
    (HEADER + '\tworktree = <TOP>/na \tmed\n', 'refused'),
    (HEADER + '\tworktree = "<TOP>/na\tmed"\n', 'refused'),
    (HEADER + '\tworktree = "<TOP>/na"\t"med"\n', 'taken'),
    (HEADER + '\tworktree =\t <TOP>/na med \t\n', 'taken'),
    # the third reads' coverage witnesses
    (HEADER + '[core   "x"]\n\tworktree = <W>\n', 'ignored'),
    (HEADER + '[core ""]\n\tworktree = <W>\n', 'ignored'),
    (HEADER + '\tworktree = <W>\n#worktree = /nonexistent-recursion-place\n', 'taken'),
    (b'\xefAB' + (HEADER + '\tworktree = <W>\n').encode(), 'refused'),
    (b'\xef', 'refused'),
    (b'\xef\xbb', 'refused'),
    (b'\xef\xbbX', 'refused'),
)

# The folder the files of CONFIG_TEXTS name, in the repository's top.
NAMED = 'na med'


def quoted(value) -> str:
    """``value`` as a quoted config value."""
    return '"' + os.fspath(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def filled(text, named):
    """A file of ``CONFIG_TEXTS`` for the folder ``named``."""
    value = quoted(named)
    half = len(value) // 2
    for mark, part in (('<W1>', value[:half]), ('<W2>', value[half:]), ('<W>', value), ('<RAW>', os.fspath(named)),
                       ('<TOP>', os.fspath(named.parent))):
        text = text.replace(mark.encode(), part.encode()) if isinstance(text, bytes) else text.replace(mark, part)
    return text


class _Repositories:
    """A temporary folder for real repositories made by git, removed whatever modes a test left in it."""

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

    @staticmethod
    def config(gitdir: pathlib.Path, text, name: str = 'config') -> None:
        (gitdir / name).write_bytes(text if isinstance(text, bytes) else text.encode('utf-8'))

    quoted = staticmethod(quoted)


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the workspace checks are POSIX, and these repositories are made by git')
class GitWorkTreeTest(_Repositories, unittest.TestCase):
    """Real repositories, made by git. Every answer expected here is the one git 2.43 gave for the same layout
    (``git rev-parse --is-inside-work-tree --show-toplevel``)."""

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
        self.config(bare, HEADER + '\tbare = false\n')   # the same, with no core.bare to refuse it as well
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

    def test_an_unborn_head_linked_into_refs_is_taken(self) -> None:
        top = self.root / 'unborn'
        top.mkdir()
        repos.git(top, 'init', '-q', '-b', 'main')
        (top / '.git' / 'HEAD').unlink()
        (top / '.git' / 'HEAD').symlink_to('refs/heads/main')     # no commit yet, so the link leads nowhere
        self.assertEqual(V.git_work_tree(top), top)

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

    def test_a_folder_named_through_a_link_is_asked_as_it_really_is(self) -> None:
        top = self.repo('repo')
        (top / 'pkg').mkdir()
        (self.root / 'via').symlink_to(top / 'pkg', target_is_directory=True)
        self.assertEqual(V.git_work_tree(self.root / 'via'), top)     # git works from where it really is


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the workspace checks are POSIX, and these repositories are made by git')
class GitConfigTest(_Repositories, unittest.TestCase):
    """The repository's own configuration, read as git 2.43 reads it to find a work tree (``core.bare``,
    ``core.worktree``, the format version, the extensions, ``config.worktree``). Every answer expected here is
    the one git gave for the same layout, except where a comment says otherwise and why."""

    def test_core_bare_true_leaves_no_work_tree(self) -> None:
        top = self.repo('configured')
        (top / 'sub').mkdir()
        repos.git(top, 'config', 'core.bare', 'true')
        self.assertIsNone(V.git_work_tree(top))
        self.assertIsNone(V.git_work_tree(top / 'sub'))
        stored = self.root / 'stored'
        (stored / '.git').mkdir(parents=True)
        repos.git(stored / '.git', 'init', '-q', '--bare')       # a bare repository kept as a .git folder
        self.assertIsNone(V.git_work_tree(stored))
        for text, answer in ((HEADER + '\tbare = false\n', top), (HEADER + '\tbare = true\n\tbare = false\n', top),
                             (HEADER + '\tbare = false\n\tbare = true\n', None),
                             (HEADER + '\tbare = true\n\tworktree = %s\n' % self.quoted(top), None)):
            self.config(top / '.git', text)
            self.assertEqual(V.git_work_tree(top), answer, text)

    def test_core_worktree_names_the_top(self) -> None:
        top = self.repo('repo')
        for sub in ('sub', 'inner/x', 'actual tree/sub', 'other'):
            (top / sub).mkdir(parents=True)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        actual = top / 'actual tree'
        answers = {                         # the value named: {the folder asked: git's answer}
            os.fspath(elsewhere): {top: None, elsewhere: None},
            os.fspath(actual): {actual: actual, actual / 'sub': actual, top: None, top / 'other': None},
            '../inner': {top / 'inner': top / 'inner', top / 'inner' / 'x': top / 'inner', top: None, top / 'sub': None,
                         top / '.git': None},
            '..': {top: top, top / 'sub': top, top / '.git': top, top / '.git' / 'objects': top},
            '.': {top / '.git': top / '.git', top: None},
            os.fspath(top / 'inner') + '/': {top / 'inner' / 'x': top / 'inner'},
            '/': {top / 'sub': pathlib.Path('/')},
        }
        for value, asked in answers.items():
            self.config(top / '.git', HEADER + '\tworktree = %s\n' % self.quoted(value))
            for folder, answer in asked.items():
                self.assertEqual(V.git_work_tree(folder), answer, (value, folder))

    def test_core_worktree_is_resolved_as_git_resolves_it(self) -> None:
        top = self.repo('repo')
        (top / 'real' / 'x').mkdir(parents=True)
        (self.root / 'link').symlink_to(top / 'real')
        (top / 'alias').symlink_to(top / 'real')
        for value in (os.fspath(self.root / 'link'), '../alias'):
            self.config(top / '.git', HEADER + '\tworktree = %s\n' % self.quoted(value))
            self.assertEqual(V.git_work_tree(top / 'real' / 'x'), top / 'real', value)
            self.assertIsNone(V.git_work_tree(top), value)
        store = self.repo('store')                  # a relative name is the git directory's as it really is
        linked = self.root / 'linked'
        linked.mkdir()
        (linked / '.git').symlink_to(store / '.git', target_is_directory=True)
        self.config(store / '.git', HEADER + '\tworktree = ..\n')
        self.assertIsNone(V.git_work_tree(linked))
        self.assertEqual(V.git_work_tree(store), store)
        source = self.root / 'source'               # a separate git directory: the name is its
        (source / 'pkg').mkdir(parents=True)
        repos.git(source, 'init', '-q', '--separate-git-dir', os.fspath(self.root / 'admin'))
        self.config(self.root / 'admin', HEADER + '\tworktree = ../source/pkg\n')
        self.assertEqual(V.git_work_tree(source / 'pkg'), source / 'pkg')
        self.assertIsNone(V.git_work_tree(source))

    def test_a_core_worktree_git_cannot_follow_leaves_none(self) -> None:
        top = self.repo('repo')
        (top / 'sub').mkdir()
        (top / 'file').write_text('x')
        for line in ('worktree = ../nothing', 'worktree =', 'worktree', 'worktree = ../file', 'worktree = nothing/../..',
                     'worktree = %s' % self.quoted(self.root / 'nothing'),
                     'worktree = %s' % self.quoted(self.root / 'no' / 'thing'),
                     'worktree = %s' % self.quoted(top / 'nothing' / '..'),
                     'worktree = %s' % self.quoted(top / 'file')):
            self.config(top / '.git', HEADER + '\t%s\n' % line)
            for folder in (top, top / 'sub', top / '.git'):
                self.assertIsNone(V.git_work_tree(folder), (line, folder))

    def test_inside_a_git_directory_a_named_work_tree_counts(self) -> None:
        work = self.root / 'w'
        bare = work / 'b.git'
        bare.mkdir(parents=True)
        repos.git(bare, 'init', '-q', '--bare')
        self.config(bare, HEADER + '\tbare = false\n\tworktree = %s\n' % self.quoted(work))
        self.assertEqual(V.git_work_tree(bare), work)
        self.assertEqual(V.git_work_tree(bare / 'objects'), work)
        self.assertIsNone(V.git_work_tree(work))           # from the work tree itself no repository is found
        (self.root / 'wt').mkdir()
        self.config(bare, HEADER + '\tbare = false\n\tworktree = %s\n' % self.quoted(self.root / 'wt'))
        self.assertIsNone(V.git_work_tree(bare))

    def test_the_first_repository_found_decides(self) -> None:
        outer = self.repo('outer')
        inner = outer / 'inner'
        repos.make_repo(inner)
        self.config(inner / '.git', HEADER + '\tbare = true\n')
        self.assertIsNone(V.git_work_tree(inner))
        self.assertEqual(V.git_work_tree(outer), outer)

    def test_the_two_settings_count_only_with_a_format_version(self) -> None:
        top = self.repo('repo')
        for text in ('[core]\n\tbare = true\n', '[core]\n\trepositoryformatversion = -1\n\tbare = true\n',
                     '[core]\n\trepositoryformatversion = -2\n\tbare = true\n',
                     '[core]\n\tworktree = /nonexistent-recursion-place\n', ''):
            self.config(top / '.git', text)
            self.assertEqual(V.git_work_tree(top), top, text)
        for version in ('0', '1', '01', '0x1', '"0"', '0 # a comment', ' 0', '0' * 5000, '0' * 5000 + '1'):
            self.config(top / '.git', '[core]\n\trepositoryformatversion = %s\n\tbare = true\n' % version)
            self.assertIsNone(V.git_work_tree(top), version)
        for version, answer in (('1', top), ('2', None), ('1k', None), ('abc', None), ('', None), ('2147483648', None),
                                ('9' * 5000, None), ('0' * 5000, top)):     # a long number is refused, not raised
            self.config(top / '.git', '[core]\n\trepositoryformatversion = %s\n' % version)
            self.assertEqual(V.git_work_tree(top), answer, version)
        self.config(top / '.git', '[core]\n\trepositoryformatversion\n')
        self.assertIsNone(V.git_work_tree(top))

    def test_the_extensions_git_checks(self) -> None:
        top = self.repo('repo')
        answers = {('0', 'objectFormat = sha1'): None, ('1', 'objectFormat = sha1'): top,
                   ('1', 'objectFormat = sha256'): top, ('1', 'objectFormat = md5'): None,
                   ('1', 'objectFormat = SHA1'): None, ('1', 'objectFormat = '): None, ('1', 'objectFormat'): None,
                   ('0', 'noop-v1 = x'): None, ('1', 'noop-v1 = x'): top, ('0', 'noop'): top,
                   ('0', 'partialClone'): None, ('0', 'partialClone = origin'): top, ('1', 'partialClone = '): top,
                   ('0', 'worktreeConfig = maybe'): None, ('0', 'preciousObjects = maybe'): None,
                   ('0', 'preciousObjects = true'): top, ('0', 'unknownExt = 1'): top,
                   # git 2.43 refuses an extension it does not know at version 1. A newer git may know it, so it is
                   # not held against the repository here (the module's text): the provider's git decides.
                   ('1', 'unknownExt = 1'): top, ('1', 'refStorage = files'): top}
        for (version, line), answer in answers.items():
            self.config(top / '.git', '[core]\n\trepositoryformatversion = %s\n[extensions]\n\t%s\n' % (version, line))
            self.assertEqual(V.git_work_tree(top), answer, (version, line))
        for line, answer in (('objectFormat = sha1', top), ('objectFormat = md5', None)):
            self.config(top / '.git', '[extensions]\n\t%s\n' % line)        # no version: values are still read
            self.assertEqual(V.git_work_tree(top), answer, line)

    def test_a_boolean_and_a_number_are_read_as_git_reads_them(self) -> None:
        top = self.repo('repo')
        for value, meaning in BOOLEANS.items():
            self.config(top / '.git', HEADER + '\tbare = %s\n' % value)
            self.assertEqual(V.git_work_tree(top), top if meaning is False else None, value)
            self.config(top / '.git', HEADER + '[extensions]\n\tpreciousObjects = %s\n' % value)
            self.assertEqual(V.git_work_tree(top), None if meaning is None else top, value)
        self.config(top / '.git', HEADER + '\tbare\n')       # a key with no value is true
        self.assertIsNone(V.git_work_tree(top))

    def test_the_config_file_is_parsed_as_git_parses_it(self) -> None:
        top = self.repo('repo')
        named = top / NAMED
        named.mkdir()
        (top / '.git' / 'extra').write_text('[core]\n\tworktree = %s\n' % self.quoted(named))
        for text, answer in CONFIG_TEXTS:
            self.config(top / '.git', filled(text, named))
            expected = {'taken': named, 'ignored': top, 'refused': None}[answer]
            self.assertEqual(V.git_work_tree(named), expected, repr(text))
        hashed = top / 'a # b'
        hashed.mkdir()
        (top / 'a').mkdir()
        self.config(top / '.git', HEADER + '\tworktree = %s # a comment\n' % self.quoted(hashed))
        self.assertEqual(V.git_work_tree(hashed), hashed)
        self.config(top / '.git', HEADER + '\tworktree = %s\n' % hashed)      # unquoted: # begins a comment
        self.assertEqual(V.git_work_tree(top / 'a'), top / 'a')
        self.assertIsNone(V.git_work_tree(hashed))

    def test_a_config_git_cannot_read_refuses_the_repository(self) -> None:
        top = self.repo('repo')
        config = top / '.git' / 'config'
        config.unlink()
        self.assertEqual(V.git_work_tree(top), top)                  # no file: nothing is set
        config.symlink_to(self.root / 'nowhere')
        self.assertEqual(V.git_work_tree(top), top)                  # a dangling link: no file either
        config.unlink()
        (self.root / 'shared').write_text(HEADER + '\tbare = true\n')
        config.symlink_to(self.root / 'shared')
        self.assertIsNone(V.git_work_tree(top))                      # a link to a file is read
        config.unlink()
        config.mkdir()
        self.assertIsNone(V.git_work_tree(top))                      # git: "unable to access"
        config.rmdir()
        os.mkfifo(config)
        answers = []
        reader = threading.Thread(target=lambda: answers.append(V.git_work_tree(top)), daemon=True)
        reader.start()
        reader.join(20)
        self.assertFalse(reader.is_alive(), 'reading the pipe waited for a writer')
        self.assertEqual(answers, [None])                            # git would wait; it is refused here
        config.unlink()
        with mock.patch.object(V, '_CONFIG_MAX', 64):
            self.config(top / '.git', (HEADER + '#' * 64)[:64])
            self.assertEqual(V.git_work_tree(top), top)
            self.config(top / '.git', (HEADER + '#' * 64)[:65])
            self.assertIsNone(V.git_work_tree(top))                  # over the lane's limit: refused

    @unittest.skipIf(POSIX and os.geteuid() == 0, 'root reads every file')
    def test_a_config_that_may_not_be_read_refuses_the_repository(self) -> None:
        top = self.repo('repo')
        os.chmod(top / '.git' / 'config', 0)
        self.assertIsNone(V.git_work_tree(top))                      # git: "unable to access"

    def linked(self, name: str, bare: bool = False) -> tuple:
        """A main repository (bare or not) and a linked worktree of it: (main, linked, its git directory)."""
        main, linked = self.root / name / 'main', self.root / name / 'linked'
        if bare:
            main.mkdir(parents=True)
            repos.git(main, 'init', '-q', '--bare', '-b', 'main')
            scratch = self.root / name / 'scratch'
            repos.make_repo(scratch)
            repos.git(scratch, 'push', '-q', os.fspath(main), 'main')
        else:
            repos.make_repo(main)
        repos.git(main, 'worktree', 'add', '-q', '--detach', os.fspath(linked), 'HEAD')
        (linked / 'sub').mkdir()
        return main, linked, pathlib.Path((linked / '.git').read_text().split('gitdir: ', 1)[1].strip())

    def test_a_linked_worktree_takes_the_common_settings_only_with_worktree_config(self) -> None:
        main, linked, admin = self.linked('plain')
        answers = {                         # the main repository's config: {the folder asked: git's answer}
            HEADER + '\tbare = true\n': {linked: linked, linked / 'sub': linked, main: None},
            HEADER + '\tworktree = %s\n' % self.quoted(self.root): {linked: linked, main: self.root},
            HEADER + '\tbare = maybe\n': {linked: None},             # git reads the value it then ignores
            HEADER + '\tworktree\n': {linked: None},
            HEADER + '\tworktree =\n': {linked: linked},
        }
        for text, asked in answers.items():
            self.config(main / '.git', text)
            for folder, answer in asked.items():
                self.assertEqual(V.git_work_tree(folder), answer, (text, folder))
        self.config(main / '.git', HEADER + '[extensions]\n\tworktreeConfig = true\n')
        self.config(main / '.git', '[core]\n\tbare = true\n', 'config.worktree')     # the main worktree's own
        self.assertEqual(V.git_work_tree(linked), linked)
        self.assertIsNone(V.git_work_tree(main))
        self.config(admin, '[core]\n\tworktree = %s\n' % self.quoted(linked / 'sub'), 'config.worktree')
        self.assertIsNone(V.git_work_tree(linked))
        self.assertEqual(V.git_work_tree(linked / 'sub'), linked / 'sub')
        for text in ('[core\n', '[core]\n\tbare = maybe\n', '[core]\n\tworktree\n'):
            self.config(admin, text, 'config.worktree')              # no common core.bare to hide the refusal
            self.assertIsNone(V.git_work_tree(linked), text)
        (admin / 'config.worktree').unlink()
        (admin / 'config.worktree').mkdir()
        self.assertIsNone(V.git_work_tree(linked))                   # git: "unable to access"

    def test_a_linked_worktree_of_a_bare_repository(self) -> None:
        main, linked, admin = self.linked('bare', bare=True)
        self.assertEqual(V.git_work_tree(linked), linked)
        self.assertEqual(V.git_work_tree(linked / 'sub'), linked)
        self.assertIsNone(V.git_work_tree(main))
        self.config(main, HEADER + '\tbare = true\n[extensions]\n\tworktreeConfig = true\n')
        self.assertIsNone(V.git_work_tree(linked))                   # the common core.bare now counts
        for text, answer in (('[core]\n\tbare = false\n', linked), ('[core\n', None), ('[core]\n\tbare = maybe\n', None),
                             ('[extensions]\n\tobjectFormat = md5\n[core]\n\trepositoryformatversion = 7\n\tbare = false\n', linked)):
            self.config(admin, text, 'config.worktree')
            self.assertEqual(V.git_work_tree(linked), answer, text)
        (admin / 'config.worktree').unlink()
        (admin / 'config.worktree').mkdir()
        self.assertIsNone(V.git_work_tree(linked))                   # git: "unable to access"
        (admin / 'config.worktree').rmdir()
        (admin / 'config.worktree').symlink_to(self.root / 'nowhere')
        self.assertIsNone(V.git_work_tree(linked))                   # no file: the common core.bare stands
        self.config(main, '[core]\n\tbare = true\n[extensions]\n\tworktreeConfig = true\n')
        self.assertEqual(V.git_work_tree(linked), linked)            # no version: neither counts
        (admin / 'config.worktree').unlink()
        self.config(main, HEADER + '\tbare = true\n')
        self.config(admin, '[extensions]\n\tworktreeConfig = true\n[core]\n\tbare = true\n', 'config.worktree')
        self.assertEqual(V.git_work_tree(linked), linked)            # only the common config names extensions


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
