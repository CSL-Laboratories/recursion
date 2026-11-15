"""The owner-only folder and files: modes at every umask, refusals, no following, no hang, no lost save."""
from __future__ import annotations

import os
import stat
import threading
from unittest import mock

from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.tests.support import MARKER, OTHER, TempStoreCase, needs_posix


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@needs_posix
class FirstSaveTest(TempStoreCase):
    def test_reads_create_nothing(self) -> None:
        store = self.store()
        self.assertEqual(store.list().keys, {})
        with self.assertRaises(KeyError) as caught:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertIs(type(caught.exception), KeyError)
        with self.assertRaises(KeyStoreError) as refused:
            store.remove('codex-key')
        self.assertEqual(refused.exception.code, 'key_unknown')
        self.assertEqual(os.listdir(self.base), [])

    def test_first_save_makes_0700_folders_and_0600_files_at_every_umask(self) -> None:
        for umask in (0o000, 0o022, 0o077, 0o277):
            with self.subTest(umask=oct(umask)):
                root = self.base / f'u{umask:o}' / 'recursion' / 'keys'
                old = os.umask(umask)
                try:
                    store = type(self.store())(root, system=self.store().system)
                    self.add(store)
                    self.add(store, key_id='second', value=OTHER)   # a second save needs a writable folder
                finally:
                    os.umask(old)
                for folder in (root.parent.parent, root.parent, root):
                    self.assertEqual(mode(folder), 0o700, folder)
                self.assertEqual(sorted(os.listdir(root)), ['.lock', 'index.json', 'secrets.json'])
                for name in ('.lock', 'index.json', 'secrets.json'):
                    self.assertEqual(mode(root / name), 0o600, name)
                self.assertEqual(store.credential('second', 'CODEX_API_KEY'), OTHER)

    def test_an_existing_parent_is_left_as_it_is(self) -> None:
        parent = self.base / 'config'
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        self.add(self.store())
        self.assertEqual(mode(parent), 0o755)
        self.assertEqual(mode(self.root), 0o700)


@needs_posix
class FolderRefusalTest(TempStoreCase):
    def assert_every_operation_refused(self, code='folder_invalid') -> None:
        store = self.store()
        for call in (lambda: store.list(), lambda: self.add(store), lambda: store.remove('codex-key')):
            with self.assertRaises(KeyStoreError) as caught:
                call()
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, code)

    def test_a_symlink_to_another_owner_only_folder_is_refused_and_not_followed(self) -> None:
        real = self.base / 'elsewhere'
        real.mkdir(mode=0o700)
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(real)
        self.assert_every_operation_refused()
        self.assertEqual(os.listdir(real), [])

    def test_a_dangling_symlink_is_refused_and_nothing_is_made_through_it(self) -> None:
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(self.base / 'nowhere')
        self.assert_every_operation_refused()
        self.assertFalse((self.base / 'nowhere').exists())

    def test_a_file_in_the_folders_place_is_refused(self) -> None:
        self.root.parent.mkdir(parents=True)
        self.root.write_text('not a folder')
        self.assert_every_operation_refused()

    def test_a_folder_of_another_mode_is_refused_and_not_changed(self) -> None:
        for bad in (0o755, 0o750, 0o770, 0o711, 0o500):
            with self.subTest(mode=oct(bad)):
                self.add(self.store())
                self.root.chmod(bad)
                try:
                    self.assert_every_operation_refused()
                    self.assertEqual(mode(self.root), bad)
                finally:
                    self.root.chmod(0o700)
                    self.store().remove('codex-key')

    def test_a_folder_owned_by_someone_else_is_refused(self) -> None:
        self.add(self.store())
        real = os.geteuid()
        with mock.patch('os.geteuid', return_value=real + 1):
            self.assert_every_operation_refused()


@needs_posix
class FileRefusalTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.add(self.store())

    def assert_refused(self, code='file_invalid') -> None:
        store = self.store()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, code)
        with self.assertRaises(KeyStoreError) as caught:
            store.list() if code != 'owner_file_invalid' else store.remove('codex-key')
        self.assertEqual(caught.exception.code, code)

    def test_an_index_of_another_mode_is_refused(self) -> None:
        for bad in (0o644, 0o640, 0o604, 0o400, 0o700):
            with self.subTest(mode=oct(bad)):
                (self.root / 'index.json').chmod(bad)
                self.assert_refused()
        (self.root / 'index.json').chmod(0o600)
        self.assertEqual(self.store().credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_an_owner_file_of_another_mode_is_refused(self) -> None:
        (self.root / 'secrets.json').chmod(0o644)
        with self.assertRaises(KeyRefused) as refused:
            self.store().credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'file_invalid')

    def test_a_symlinked_index_is_refused_and_not_followed(self) -> None:
        target = self.base / 'index-copy.json'
        os.replace(self.root / 'index.json', target)
        target.chmod(0o600)
        (self.root / 'index.json').symlink_to(target)
        self.assert_refused()

    def test_a_hard_linked_owner_file_is_refused(self) -> None:
        os.link(self.root / 'secrets.json', self.base / 'second-name')
        with self.assertRaises(KeyRefused) as refused:
            self.store().credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'file_invalid')

    def test_a_fifo_in_the_index_place_is_refused_without_blocking(self) -> None:
        os.unlink(self.root / 'index.json')
        os.mkfifo(self.root / 'index.json', 0o600)
        self.assert_refused()           # an open that blocked would never get here

    def test_a_folder_in_a_files_place_is_refused(self) -> None:
        os.unlink(self.root / 'secrets.json')
        os.mkdir(self.root / 'secrets.json', 0o700)
        with self.assertRaises(KeyRefused) as refused:
            self.store().credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'file_invalid')

    def test_files_owned_by_someone_else_are_refused(self) -> None:
        # Only the named file looks like another user's: the folder and the lock stay ours, so it is the file's
        # own owner check that refuses, not the lock's.
        real = os.geteuid()
        original_fstat = os.fstat
        for name, code in (('index.json', 'file_invalid'), ('secrets.json', 'file_invalid')):
            with self.subTest(file=name):
                target = os.stat(self.root / name).st_ino

                def fstat(fd, target=target):
                    st = original_fstat(fd)
                    if st.st_ino == target:
                        values = list(st)
                        values[4] = real + 1
                        return os.stat_result(values)
                    return st

                with mock.patch('os.fstat', fstat), self.assertRaises(KeyRefused) as refused:
                    self.store().credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, code)
                self.assertIn('not owned by this user', str(refused.exception) + self._last_detail(name))
        self.assertEqual(self.store().credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def _last_detail(self, name: str) -> str:
        """The detail a direct read of ``name`` gives under the same fake (KeyRefused carries only the code)."""
        from recursion_core.keys.files import open_folder

        real = os.geteuid()
        original_fstat = os.fstat
        target = os.stat(self.root / name).st_ino

        def fstat(fd):
            st = original_fstat(fd)
            if st.st_ino == target:
                values = list(st)
                values[4] = real + 1
                return os.stat_result(values)
            return st

        with mock.patch('os.fstat', fstat), open_folder(self.root, create=False) as folder:
            try:
                folder.read(name, 1 << 22)
            except KeyStoreError as exc:
                return str(exc)
        return ''

    def test_a_lock_wider_than_0600_is_refused(self) -> None:
        (self.root / '.lock').chmod(0o644)
        with self.assertRaises(KeyStoreError) as caught:
            self.add(self.store(), key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'file_invalid')

    def test_a_lock_narrower_than_0600_is_put_back(self) -> None:
        (self.root / '.lock').chmod(0o400)
        self.add(self.store(), key_id='second', value=OTHER)
        self.assertEqual(mode(self.root / '.lock'), 0o600)


@needs_posix
class WriteTest(TempStoreCase):
    def test_simultaneous_saves_lose_no_key(self) -> None:
        for rounds in range(5):
            with self.subTest(round=rounds):
                root = self.base / f'r{rounds}' / 'keys'
                ids = [f'key-{n}' for n in range(8)]
                barrier = threading.Barrier(len(ids))
                errors = []

                def save(key_id, root=root, barrier=barrier, errors=errors):
                    try:
                        store = type(self.store())(root, system=self.store().system)
                        barrier.wait()
                        store.add(key_id, 'openai', 'CODEX_API_KEY', f'{MARKER}-{key_id}', metered_spend_allowed=True,
                                  cap_usd=1)
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        errors.append(exc)

                threads = [threading.Thread(target=save, args=(kid,)) for kid in ids]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                store = type(self.store())(root, system=self.store().system)
                self.assertEqual(sorted(store.list().keys), ids)
                for kid in ids:
                    self.assertEqual(store.credential(kid, 'CODEX_API_KEY'), f'{MARKER}-{kid}')

    def test_every_write_syncs_the_file_and_then_the_folder(self) -> None:
        store = self.store()
        self.add(store)
        synced = []
        real_fsync = os.fsync

        def fsync(fd):
            synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
            real_fsync(fd)

        with mock.patch('os.fsync', fsync):
            self.add(store, key_id='second', value=OTHER)         # the intent, the owner-only file, the index
        self.assertEqual(synced, [stat.S_IFREG, stat.S_IFDIR] * 3)

    def test_no_temp_file_is_left_after_saves_and_removals(self) -> None:
        store = self.store()
        self.add(store)
        self.add(store, key_id='second', value=OTHER)
        store.remove('codex-key')
        self.assertEqual(sorted(os.listdir(self.root)), ['.lock', 'index.json', 'secrets.json'])

    def test_a_failed_write_leaves_no_temp_file_and_the_old_index(self) -> None:
        store = self.store()
        self.add(store)
        before = (self.root / 'index.json').read_bytes()
        with mock.patch('os.replace', side_effect=OSError(28, 'No space left on device')), self.assertRaises(OSError):
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.root)), ['.lock', 'index.json', 'secrets.json'])

    def test_no_descriptor_leaks_on_any_path(self) -> None:
        if not os.path.isdir('/proc/self/fd'):
            self.skipTest('needs /proc/self/fd to count descriptors')
        store = self.store()
        self.add(store)
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(50):
            store.credential('codex-key', 'CODEX_API_KEY')
            store.list()
            for call in (lambda: store.credential('nobody', 'X'), lambda: self.add(store),
                         lambda: store.remove('nobody')):
                try:
                    call()
                except (KeyError, KeyStoreError):
                    pass
            (self.root / 'index.json').chmod(0o644)
            for call in (lambda: store.credential('codex-key', 'CODEX_API_KEY'), lambda: store.list()):
                try:
                    call()
                except (KeyError, KeyStoreError):
                    pass
            (self.root / 'index.json').chmod(0o600)
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
