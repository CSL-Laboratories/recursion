"""E21, and a plugin directory name that must not forge a log record.

``test_a_symlinked_revocation_store_must_not_be_opened`` pins a symlink whose target is mode 0666.
The mode check alone refuses that target, so it does not tell ``lstat`` from ``stat``.
``test_a_symlink_to_an_owned_0600_store_is_refused`` is the case where they differ: ``stat`` follows
the link and loads, and the check must not.
``test_a_swap_to_a_symlink_between_the_check_and_the_open_does_not_load`` pins that the open uses
the inode that was checked. Bounded rounds, run on the free-threaded interpreter with the GIL off.
``test_two_hosts_creating_the_store_together_both_load`` pins two creators of one new store.
``test_plugin_paths_must_not_inject_log_records`` pins that a newline in a plugin directory name
cannot forge a second log record.
Fixture bytes are generated here.
"""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import create_revocation_store
from recursion_core.plugin_security import manifest as manifest_mod


class GilOff:
    def gil_enabled(self):
        return False


class PluginRegressions(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='sweep-s2b-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def host(self, source, **options):
        config = self.root / 'config'
        fx.write_install(config, grants=[fx.grant_for(source)])
        fx.write_plugin(config / 'plugins' / 'fixture', source)
        options.setdefault('gil_probe', GilOff())
        host = plugins.PluginHost(config, self.root / 'state', verification_available=lambda: True, **options)
        self.addCleanup(host.close)
        return host

    def test_a_symlinked_revocation_store_must_not_be_opened(self):
        os.chmod(self.root, 0o711)
        shared = self.root / 'shared'
        shared.mkdir(mode=0o777)
        os.chmod(shared, 0o777)
        target = shared / 'other-revocations.sqlite3'
        create_revocation_store(str(target))
        os.chmod(target, 0o666)
        state_dir = self.root / 'state' / 'plugin-security'
        state_dir.mkdir(parents=True, mode=0o700)
        (state_dir / 'revoked.sqlite3').symlink_to(target)
        host = self.host(b'VALUE = 1\n')
        self.assertEqual(host.records[0]['state'], 'refused', 'loaded through a symlinked revocation store')
        self.assertEqual(host.records[0]['code'], 'revocation_store_untrusted')

    def test_a_symlink_to_an_owned_0600_store_is_refused(self):
        private = self.root / 'private'
        private.mkdir(mode=0o700)
        target = private / 'real.sqlite3'
        create_revocation_store(str(target))
        os.chmod(target, 0o600)
        state_dir = self.root / 'state' / 'plugin-security'
        state_dir.mkdir(parents=True, mode=0o700)
        (state_dir / 'revoked.sqlite3').symlink_to(target)
        host = self.host(b'VALUE = 1\n')
        self.assertEqual(host.records[0]['state'], 'refused', 'loaded through a symlink to a 0600 store')
        self.assertEqual(host.records[0]['code'], 'revocation_store_untrusted')

    def test_two_hosts_creating_the_store_together_both_load(self):
        source = b'VALUE = 1\n'
        config = self.root / 'config'
        fx.write_install(config, grants=[fx.grant_for(source)])
        fx.write_plugin(config / 'plugins' / 'fixture', source)
        refused = []
        for _ in range(8):
            state = tempfile.mkdtemp(prefix='s2b-pair-', dir=self.root)
            os.chmod(state, 0o700)
            barrier = threading.Barrier(2)
            found = []
            errors = []
            guard = threading.Lock()

            def run():
                try:
                    barrier.wait(5)
                    host = plugins.PluginHost(config, state, gil_probe=GilOff(),
                                               verification_available=lambda: True)
                    try:
                        row = host.records[0]
                        with guard:
                            found.append((row['state'], row['code']))
                    finally:
                        host.close()
                except BaseException as exc:  # noqa: BLE001 - reported on the test thread
                    with guard:
                        errors.append(repr(exc))

            threads = [threading.Thread(target=run), threading.Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            refused.extend(code for state, code in found if state != 'loaded')
        self.assertEqual(refused, [], 'a second host creating the store was refused every plugin')

    def test_a_swap_to_a_symlink_between_the_check_and_the_open_does_not_load(self):
        """Bounded rounds on the free-threaded interpreter. The swap sits in the hash that runs after the check and before the open."""
        source = b'VALUE = 1\n'
        leaks = 0
        rounds = 8
        keys = {f'k{i:05d}': 'ab' * 32 for i in range(32)}
        for _ in range(rounds):
            folder = Path(tempfile.mkdtemp(prefix='s2b-race-', dir=self.root))
            os.chmod(folder, 0o700)
            config = folder / 'config'
            fx.write_install(config, trusted=keys, grants=[fx.grant_for(source)])
            fx.write_plugin(config / 'plugins' / 'fixture', source)
            evil_dir = folder / 'elsewhere'
            evil_dir.mkdir(mode=0o700)
            evil = evil_dir / 'elsewhere.sqlite3'
            create_revocation_store(str(evil))
            os.chmod(evil, 0o600)
            store = folder / 'state' / 'plugin-security' / 'revoked.sqlite3'
            done = {}
            swapped = {'done': False}
            real_key_id = manifest_mod.key_id

            def wrapped(value, store=store, evil=evil, swapped=swapped):
                # TrustStore hashes every key after the path check returns and before it opens
                # the store. Replacing the path here is that gap, on the load thread, so a
                # round cannot miss it.
                if not swapped['done']:
                    swapped['done'] = True
                    if store.exists() or store.is_symlink():
                        store.unlink()
                        store.symlink_to(evil)
                return real_key_id(value)

            def load(folder=folder, config=config, done=done, wrapped=wrapped):
                with mock.patch.object(manifest_mod, 'key_id', wrapped):
                    host = plugins.PluginHost(config, folder / 'state', gil_probe=GilOff(),
                                               verification_available=lambda: True)
                    try:
                        row = host.records[0]
                        opened = None
                        trust = getattr(host, '_trust', None)
                        db = None if trust is None else getattr(trust, '_db', None)
                        if db is not None:
                            opened = db.execute('PRAGMA database_list').fetchone()[2]
                        done['state'] = row['state']
                        done['code'] = row['code']
                        done['opened'] = opened
                    finally:
                        host.close()

            thread = threading.Thread(target=load)
            thread.start()
            thread.join(60)
            self.assertFalse(thread.is_alive(), 'the load thread did not finish')
            opened = done.get('opened') or ''
            if done.get('state') == 'loaded' and 'elsewhere.sqlite3' in str(opened):
                leaks += 1
        self.assertEqual(leaks, 0, f'{leaks} of {rounds} rounds loaded through a symlinked store')

    def test_plugin_paths_must_not_inject_log_records(self):
        source = self.root / 'fixture\nSYNTHETIC-FORGED-LOG-LINE'
        source.mkdir(mode=0o700)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as captured:
            host = plugins.PluginHost(self.root / 'config', self.root / 'state',
                                      plugin_dirs=(source,), gil_probe=GilOff(), verification_available=lambda: True)
        self.addCleanup(host.close)
        self.assertEqual(host.records[0]['code'], 'plugin_file_missing')
        self.assertNotIn('\nSYNTHETIC-FORGED-LOG-LINE', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
