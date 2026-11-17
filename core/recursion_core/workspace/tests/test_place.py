"""Where a run's worktree is put (workspace/place.py).

The rule measures this machine and picks once, at spawn. Each outcome has a test: reflink, hardlink,
copy, an explicit linked worktree, and no place. The measured tests use a real temporary repository.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.workspace import place as P
from recursion_core.workspace.tests import repos

POSIX = os.name == 'posix'
RESERVE = P.REFLINK_RESERVE


def _facts(candidate, *, repo=pathlib.Path('/repo'), common=pathlib.Path('/repo/.git'), same=True,
           reflink=False, hardlink=False, free=1 << 30, work=100, objects=100) -> P.Facts:
    return P.Facts(repository=repo, candidate=pathlib.Path(candidate), same_filesystem=same, reflink=reflink,
                   hardlink=hardlink, free_bytes=free, work_bytes=work, object_bytes=objects,
                   git_common_dir=common)


class PickOutcomes(unittest.TestCase):
    """``pick`` and ``choose`` on facts, with no disk. One test for each way the rule can come out."""

    def test_same_filesystem_with_reflink_and_space_picks_reflink(self) -> None:
        place = P.pick(_facts('/out/run', reflink=True, hardlink=True, free=RESERVE, work=0, objects=0))
        self.assertEqual(place.mechanism, 'reflink')
        self.assertEqual(place.path, pathlib.Path('/out/run'))
        self.assertFalse(place.writes_git_common_dir)
        self.assertIs(place.engine_worktree_switching, False)

    def test_hardlink_when_a_reflink_cannot_be_made(self) -> None:
        # Objects are shared, so the free space only has to hold the work tree. A full copy would not fit.
        free = 100 + RESERVE
        place = P.pick(_facts('/out/run', reflink=False, hardlink=True, free=free, work=100, objects=5000))
        self.assertEqual(place.mechanism, 'hardlink')
        self.assertFalse(place.writes_git_common_dir)
        self.assertIsNone(P.pick(_facts('/out/run', reflink=False, hardlink=True, free=free - 1,
                                        work=100, objects=5000)))

    def test_copy_when_the_filesystem_has_neither_link(self) -> None:
        free = 150 + RESERVE
        place = P.pick(_facts('/out/run', reflink=False, hardlink=False, free=free, work=100, objects=50))
        self.assertEqual(place.mechanism, 'copy')
        self.assertFalse(place.writes_git_common_dir)
        self.assertIsNone(P.pick(_facts('/out/run', free=free - 1, work=100, objects=50)))

    def test_another_filesystem_does_not_use_a_link_even_when_the_flags_say_so(self) -> None:
        place = P.pick(_facts('/out/run', same=False, reflink=True, hardlink=True, free=1 << 30,
                              work=10, objects=10))
        self.assertEqual(place.mechanism, 'copy')
        self.assertFalse(place.same_filesystem)
        self.assertFalse(place.writes_git_common_dir)

    def test_too_little_space_picks_nothing(self) -> None:
        self.assertIsNone(P.pick(_facts('/out/run', reflink=True, hardlink=True, free=RESERVE - 1,
                                        work=0, objects=0)))
        self.assertIsNone(P.pick(_facts('/out/run', free=0, work=0, objects=0)))

    def test_the_default_is_not_a_linked_worktree(self) -> None:
        # Free space holds a checkout, not a second copy of the objects. A linked worktree would fit and
        # would write the git common directory; the default still picks nothing.
        facts = _facts('/out/run', same=False, free=100 + RESERVE, work=100, objects=5000)
        self.assertIsNone(P.pick(facts))
        self.assertIsNone(P.pick(facts, linked=False))

    def test_a_linked_worktree_is_only_the_explicit_setting(self) -> None:
        facts = _facts('/out/run', same=False, free=100 + RESERVE, work=100, objects=5000)
        place = P.pick(facts, linked=True)
        self.assertEqual(place.mechanism, 'linked')
        self.assertTrue(place.writes_git_common_dir)
        self.assertEqual(place.git_common_dir, pathlib.Path('/repo/.git'))
        self.assertIs(place.engine_worktree_switching, False)
        self.assertFalse(place.path.is_relative_to(place.git_common_dir))

    def test_an_explicit_link_with_no_common_directory_picks_nothing(self) -> None:
        facts = _facts('/out/run', common=None, reflink=True, free=1 << 30)
        self.assertIsNone(P.pick(facts, linked=True))
        self.assertEqual(P.pick(facts).mechanism, 'reflink')

    def test_an_explicit_link_without_space_picks_nothing(self) -> None:
        facts = _facts('/out/run', free=100 + RESERVE - 1, work=100, objects=1)
        self.assertIsNone(P.pick(facts, linked=True))

    def test_an_explicit_link_does_not_fall_back_to_another_mechanism(self) -> None:
        copy = _facts('/copy', same=False, common=None, free=1 << 30)
        with mock.patch.object(P, 'measure', return_value=copy):
            self.assertIsNone(P.choose('/repo', ['/copy'], work_bytes=10, object_bytes=10, linked=True))
        with mock.patch.object(P, 'measure', return_value=copy):
            self.assertEqual(P.choose('/repo', ['/copy'], work_bytes=10, object_bytes=10).mechanism, 'copy')

    def test_a_place_inside_the_repository_picks_nothing(self) -> None:
        self.assertIsNone(P.pick(_facts('/repo/sub', reflink=True)))
        self.assertIsNone(P.pick(_facts('/repo', reflink=True)))
        self.assertIsNone(P.pick(_facts('/repo/sub', reflink=True), linked=True))

    def test_a_place_inside_the_git_common_directory_picks_nothing(self) -> None:
        facts = _facts('/git/objects', repo=pathlib.Path('/src'), common=pathlib.Path('/git'), reflink=True)
        self.assertIsNone(P.pick(facts))
        self.assertIsNone(P.pick(facts, linked=True))

    def test_a_place_that_contains_the_repository_picks_nothing(self) -> None:
        self.assertIsNone(P.pick(_facts('/', repo=pathlib.Path('/repo'), reflink=True)))

    def test_the_cheaper_candidate_is_the_one_chosen(self) -> None:
        slow = _facts('/slow', same=False, free=1 << 30, work=10, objects=10)
        fast = _facts('/fast', reflink=True, hardlink=True, free=1 << 30, work=10, objects=10)
        with mock.patch.object(P, 'measure', side_effect=[slow, fast]):
            place = P.choose('/repo', ['/slow', '/fast'], work_bytes=10, object_bytes=10)
        self.assertEqual(place.mechanism, 'reflink')
        self.assertEqual(place.path, pathlib.Path('/fast'))
        self.assertFalse(place.writes_git_common_dir)

    def test_the_first_candidate_wins_when_the_cost_is_equal(self) -> None:
        first = _facts('/a', reflink=True, free=1 << 30)
        second = _facts('/b', reflink=True, free=1 << 30)
        with mock.patch.object(P, 'measure', side_effect=[first, second]):
            place = P.choose('/repo', ['/a', '/b'], work_bytes=100, object_bytes=100)
        self.assertEqual(place.path, pathlib.Path('/a'))

    def test_a_candidate_that_cannot_be_used_is_skipped(self) -> None:
        bad = _facts('/repo/inside', reflink=True)
        good = _facts('/out', reflink=True, free=1 << 30)
        with mock.patch.object(P, 'measure', side_effect=[bad, good]):
            place = P.choose('/repo', ['/repo/inside', '/out'], work_bytes=100, object_bytes=100, linked=True)
        self.assertEqual(place.path, pathlib.Path('/out'))
        self.assertEqual(place.mechanism, 'linked')

    def test_no_candidate_picks_nothing(self) -> None:
        self.assertIsNone(P.choose('/repo', [], work_bytes=0, object_bytes=0))

    def test_the_identity_is_frozen_at_the_choice(self) -> None:
        place = P.pick(_facts('/out/run', reflink=True, free=RESERVE, work=0, objects=0))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            place.mechanism = 'linked'
        with self.assertRaises(dataclasses.FrozenInstanceError):
            place.engine_worktree_switching = True
        again = P.pick(_facts('/out/run', reflink=True, free=RESERVE, work=0, objects=0))
        self.assertEqual(place, again)

    def test_engine_worktree_switching_stays_off(self) -> None:
        self.assertIs(P.ENGINE_WORKTREE_SWITCHING, False)
        with self.assertRaises(ValueError):
            P.Place(path=pathlib.Path('/out'), mechanism='copy', engine_worktree_switching=True,
                    repository=pathlib.Path('/repo'), git_common_dir=pathlib.Path('/repo/.git'),
                    writes_git_common_dir=False, same_filesystem=True, reflink=False, hardlink=False,
                    free_bytes=1, work_bytes=0, object_bytes=0)
        with self.assertRaises(ValueError):
            P.Place(path=pathlib.Path('/out'), mechanism='copy', engine_worktree_switching=False,
                    repository=pathlib.Path('/repo'), git_common_dir=pathlib.Path('/repo/.git'),
                    writes_git_common_dir=True, same_filesystem=True, reflink=False, hardlink=False,
                    free_bytes=1, work_bytes=0, object_bytes=0)

    def test_a_size_that_is_not_a_count_is_rejected(self) -> None:
        for kwargs in ({'work_bytes': -1, 'object_bytes': 0}, {'work_bytes': True, 'object_bytes': 0},
                       {'work_bytes': 0, 'object_bytes': False}, {'work_bytes': 1.5, 'object_bytes': 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    P.choose('/repo', [], **kwargs)

    def test_linked_must_be_exactly_true_or_false(self) -> None:
        with self.assertRaises(TypeError):
            P.pick(_facts('/out'), linked='linked')
        with self.assertRaises(TypeError):
            P.choose('/repo', [], work_bytes=0, object_bytes=0, linked=1)


@unittest.skipUnless(POSIX and repos.HAVE_GIT, 'the place is measured on a POSIX machine with git')
class MeasuredPlace(unittest.TestCase):

    def setUp(self) -> None:
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='ws-place-')))
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = self.root / 'repo'
        repos.make_repo(self.repo, {'README.md': 'hello\n'})
        self.outside = self.root / 'outside'
        self.outside.mkdir()

    def _choose(self, candidate, **kwargs):
        return P.choose(self.repo, [candidate], work_bytes=10, object_bytes=10, **kwargs)

    def test_the_measured_choice_matches_the_facts_and_stays_outside(self) -> None:
        candidate = self.outside / 'run'
        self.assertFalse(candidate.exists())
        facts = P.measure(self.repo, candidate, work_bytes=10, object_bytes=10)
        place = self._choose(candidate)
        chosen = P.pick(facts)
        self.assertEqual(place.mechanism, chosen.mechanism)
        self.assertEqual(place.path, chosen.path)
        self.assertEqual(place.writes_git_common_dir, chosen.writes_git_common_dir)
        self.assertEqual(place.git_common_dir, chosen.git_common_dir)
        self.assertFalse(candidate.exists())
        self.assertEqual(place.path, candidate.resolve())
        self.assertFalse(place.writes_git_common_dir)
        self.assertIs(place.engine_worktree_switching, False)
        self.assertNotEqual(place.mechanism, 'linked')
        self.assertEqual(place.git_common_dir, (self.repo / '.git').resolve())
        self.assertTrue(place.same_filesystem)
        self.assertFalse(place.path.is_relative_to(self.repo))
        self.assertFalse(self.repo.is_relative_to(place.path))
        if facts.reflink:
            self.assertEqual(place.mechanism, 'reflink')
        elif facts.hardlink:
            self.assertEqual(place.mechanism, 'hardlink')
        else:
            self.assertEqual(place.mechanism, 'copy')
        self.assertEqual(self._leftovers(), [])

    def test_the_link_facts_agree_with_a_separate_probe(self) -> None:
        facts = P.measure(self.repo, self.outside / 'run', work_bytes=1, object_bytes=1)
        probe = self.outside / 'probe'
        probe.write_bytes(b'x')
        link = self.outside / 'probe-link'
        try:
            os.link(probe, link)
            hardlink = True
        except OSError:
            hardlink = False
        finally:
            link.unlink(missing_ok=True)
            probe.unlink(missing_ok=True)
        self.assertEqual(facts.hardlink, hardlink)
        self.assertEqual(facts.reflink, P.reflink_possible(self.outside))
        self.assertEqual(facts.same_filesystem, os.stat(self.repo).st_dev == os.stat(self.outside).st_dev)
        self.assertGreater(facts.free_bytes, RESERVE)

    def test_a_linked_worktree_is_outside_and_names_the_common_directory(self) -> None:
        place = self._choose(self.outside / 'run', linked=True)
        self.assertEqual(place.mechanism, 'linked')
        self.assertTrue(place.writes_git_common_dir)
        self.assertEqual(place.git_common_dir, (self.repo / '.git').resolve())
        self.assertFalse(place.path.is_relative_to(self.repo))
        self.assertIs(place.engine_worktree_switching, False)

    def test_a_linked_worktrees_common_directory_is_the_main_one(self) -> None:
        linked = self.root / 'linked'
        repos.git(self.repo, 'worktree', 'add', '-q', '--detach', str(linked), 'HEAD')
        facts = P.measure(linked, self.outside / 'run', work_bytes=10, object_bytes=10)
        self.assertEqual(facts.git_common_dir, (self.repo / '.git').resolve())
        place = P.choose(linked, [self.outside / 'run'], work_bytes=10, object_bytes=10)
        self.assertFalse(place.writes_git_common_dir)
        self.assertEqual(place.git_common_dir, (self.repo / '.git').resolve())

    def test_a_link_into_the_repository_is_not_a_place(self) -> None:
        alias = self.root / 'alias'
        alias.symlink_to(self.repo)
        self.assertIsNone(self._choose(alias))
        self.assertIsNone(self._choose(alias, linked=True))
        self.assertEqual(self._leftovers(), [])

    def test_a_link_to_an_outside_directory_is_that_directory(self) -> None:
        real = self.root / 'real'
        real.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(real)
        place = self._choose(alias / 'run')
        self.assertEqual(place.path, (real / 'run').resolve())
        self.assertFalse((alias / 'run').exists())

    def test_nothing_is_written_inside_the_repository(self) -> None:
        def names() -> list:
            found = []
            for dirpath, dirs, files in os.walk(self.repo):
                for name in dirs + files:
                    found.append(os.path.relpath(os.path.join(dirpath, name), self.repo))
            return sorted(found)

        before = names()
        self._choose(self.outside / 'run')
        self._choose(self.repo / '.git' / 'not-a-place')
        self._choose(self.repo / 'inside')
        self.assertEqual(names(), before)

    def test_a_file_a_relative_path_and_a_missing_repository_are_not_places(self) -> None:
        file_path = self.outside / 'file'
        file_path.write_text('x')
        self.assertIsNone(P.measure(self.repo, file_path, work_bytes=1, object_bytes=1))
        self.assertIsNone(P.measure(self.repo, 'relative', work_bytes=1, object_bytes=1))
        self.assertIsNone(P.measure(self.root / 'missing', self.outside, work_bytes=1, object_bytes=1))
        self.assertIsNone(self._choose(file_path))

    def test_a_size_that_is_not_a_count_is_rejected_before_a_probe(self) -> None:
        with self.assertRaises(ValueError):
            P.measure(self.repo, self.outside, work_bytes=-1, object_bytes=0)
        self.assertEqual(self._leftovers(), [])

    def test_a_link_probe_that_fails_is_not_possible_and_leaves_nothing(self) -> None:
        def refuse(source, destination):
            raise OSError('no hardlink')

        with mock.patch('os.link', refuse):
            facts = P.measure(self.repo, self.outside / 'run', work_bytes=1, object_bytes=1)
        self.assertFalse(facts.hardlink)
        self.assertEqual(self._leftovers(), [])

    def test_an_interruption_during_the_probe_leaves_nothing(self) -> None:
        def interrupt(source, destination):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            with mock.patch('os.link', interrupt):
                P.measure(self.repo, self.outside / 'run', work_bytes=1, object_bytes=1)
        self.assertEqual(self._leftovers(), [])

    def test_a_second_caller_gets_the_same_place(self) -> None:
        found = []
        barrier = threading.Barrier(2)

        def once() -> None:
            barrier.wait()
            found.append(self._choose(self.outside / 'run'))

        threads = [threading.Thread(target=once), threading.Thread(target=once)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIsNotNone(found[0])
        self.assertEqual(found[0].path, found[1].path)
        self.assertEqual(found[0].mechanism, found[1].mechanism)
        self.assertEqual(found[0].writes_git_common_dir, found[1].writes_git_common_dir)
        self.assertEqual(found[0].git_common_dir, found[1].git_common_dir)
        self.assertIs(found[0].engine_worktree_switching, False)
        self.assertFalse(found[0].writes_git_common_dir)
        self.assertEqual(self._leftovers(), [])

    def _leftovers(self) -> list:
        found = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if name.startswith('.recursion-place-'):
                    found.append(os.path.join(dirpath, name))
        return found


if __name__ == '__main__':
    unittest.main()
