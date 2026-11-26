"""A signed version 1 package loads only when its folder holds exactly what its manifest lists, unchanged.

Through the loader and through the daemon's plugin host, with real files. Real Ed25519 signatures where
the interpreter has ``cryptography``; elsewhere the signature check is a stand-in (``manifest.verify`` and
``backend_available`` patched to True, as ``PackagePresenceTests`` does), which shows the package checks
and says nothing about Ed25519. Every case first shows that no plugin code ran before a refusal.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid

import recursion_core
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.daemon.tests.test_plugins import LINUX, LINUX_ONLY, MISSING, HostCase
from recursion_core.plugin_security import CoreHealth, PluginLoader, Refused, ReservedEffects, TrustStore
from recursion_core.plugin_security import loader as loader_module
from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import FakeGilProbe, Key
from recursion_core.plugin_security.tests.test_manifest_v1 import v1

DIGEST = loader_module.PACKAGE_FILE_DIGEST_MISMATCH_TEXT
UNLISTED = loader_module.PACKAGE_FILE_UNLISTED_TEXT
UNTRUSTED = loader_module.PACKAGE_FILE_UNTRUSTED_TEXT
CORE = str(Path(recursion_core.__file__).resolve().parents[1])

# Run in a child interpreter started with -B, so importing Recursion writes nothing; the child then lets the
# interpreter write compiled files again and loads the package, whose entry imports its listed helper.
NO_CACHE_CHILD = '''
import sys
from pathlib import Path
from unittest import mock
from recursion_core.plugin_security import CoreHealth, PluginLoader, ReservedEffects, TrustStore
from recursion_core.plugin_security.tests.support import FakeGilProbe
package, signature, developer, stand_in = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == "1"
if stand_in:
    mock.patch("recursion_core.plugin_security.manifest.verify", return_value=True).start()
sys.dont_write_bytecode = False
loader = PluginLoader(TrustStore({"dev": developer}), {}, ReservedEffects(), CoreHealth(), FakeGilProbe())
loaded = loader.load((package / "manifest.json").read_bytes(), signature, (package / "plugin.py").read_bytes(),
                     package_dir=package)
print(loaded.trust, sys.dont_write_bytecode, ",".join(sorted(p.name for p in package.iterdir())))
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class SignedPackageContentTests(HostCase):
    def setUp(self):
        super().setUp()
        self.key = Key() if backend_available() else None
        self.developer = self.key.public if self.key is not None else fx.FAKE_DEVELOPER_KEY
        if self.key is None:
            self.enterContext(mock.patch.object(plugins, 'backend_available', return_value=True))
            self.enterContext(mock.patch('recursion_core.plugin_security.manifest.verify', return_value=True))
        self.package = self.plugins / 'fixture'
        self.marker = self.tmp / 'plugin-code-ran'
        # A module name of this case alone: the entry imports it from its own folder, as a multi-file plugin does.
        self.module = 'pkg854_' + uuid.uuid4().hex[:12]
        self.helper_file = self.module + '.py'
        self.helper = ('VALUE = "signed"\nwith open(%r, "a") as out:\n    out.write("helper\\n")\n'
                       % str(self.marker)).encode()
        self.entry = ('import sys\nsys.path.insert(0, %r)\nimport %s\nVALUE = %s.VALUE\n'
                      % (str(self.package), self.module, self.module)).encode()
        self.addCleanup(self.forget)
        self.raw = v1(self.developer, source=self.entry, plugin_id='fixture.content', permissions=(),
                      files={'plugin.py': sha(self.entry), self.helper_file: sha(self.helper),
                             'lib/__init__.py': sha(b''), 'lib/data.txt': sha(b'listed data\n')})
        self.signature = self.sign(self.raw)
        fx.write_install(self.config, trusted={'dev': self.developer})
        fx.write_plugin(self.package, self.entry, manifest=self.raw, signature=self.signature)
        self.write(self.helper_file, self.helper)
        (self.package / 'lib').mkdir(mode=0o700)
        self.write('lib/__init__.py', b'')
        self.write('lib/data.txt', b'listed data\n')

    def sign(self, raw):
        return self.key.sign(raw) if self.key is not None else 'test signature'

    def forget(self):
        sys.modules.pop(self.module, None)
        while str(self.package) in sys.path:
            sys.path.remove(str(self.package))

    def write(self, rel, data, mode=0o600):
        path = self.package / rel
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def loader(self, trusted=True):
        return PluginLoader(TrustStore({'dev': self.developer} if trusted else {}), {}, ReservedEffects(),
                            CoreHealth(), FakeGilProbe())

    def load(self, trusted=True):
        return self.loader(trusted).load(self.raw, self.signature, self.entry, package_dir=self.package)

    def refused(self):
        """The loader's refusal, then the daemon's for the same folder; no plugin code ran for either."""
        with self.assertRaises(Refused) as ctx:
            self.load()
        self.assertFalse(self.marker.exists(), 'plugin code ran before the loader refused')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            record, = self.host().records
        self.assertFalse(self.marker.exists(), 'plugin code ran before the daemon refused')
        self.assertEqual((record['state'], record['code'], record['detail']),
                         ('refused', ctx.exception.code, ctx.exception.detail))
        return ctx.exception

    # -- what loads ---------------------------------------------------------------------------------------------
    def test_an_untouched_package_loads_signed_and_the_record_says_what_was_checked(self):
        loaded = self.load()
        self.assertEqual((loaded.trust, loaded.package_verified, loaded.module.VALUE), ('signed', True, 'signed'))
        self.forget()
        self.marker.unlink()
        record, = self.host().records
        self.assertEqual((record['state'], record['trust'], record['verified']),
                         ('loaded', 'signed', plugins.VERIFIED_PACKAGE))
        self.assertEqual(self.marker.read_text(), 'helper\n')

    def test_only_the_signature_envelope_is_exempt(self):
        self.assertEqual(loader_module.PACKAGE_ENVELOPE, ('manifest.json', 'manifest.sig'))
        self.assertEqual(sorted(p.name for p in self.package.iterdir()),
                         sorted(['lib', 'manifest.json', 'manifest.sig', 'plugin.py', self.helper_file]))
        self.assertEqual(self.load().trust, 'signed')

    # -- a listed file changed ----------------------------------------------------------------------------------
    def test_a_listed_module_changed_after_signing_is_refused_naming_it(self):
        self.write(self.helper_file, self.helper.replace(b'"signed"', b'"changed"'))
        exc = self.refused()
        self.assertEqual(str(exc), 'package_file_digest_mismatch: ' + DIGEST % self.helper_file)

    def test_a_listed_data_file_in_a_folder_changed_after_signing_is_refused(self):
        self.write('lib/data.txt', b'other data\n')
        exc = self.refused()
        self.assertEqual((exc.code, exc.detail), ('package_file_digest_mismatch', DIGEST % 'lib/data.txt'))

    def test_a_missing_file_is_still_named_before_a_changed_one(self):
        self.write('lib/data.txt', b'other data\n')
        (self.package / self.helper_file).unlink()
        self.assertEqual(self.refused().detail, MISSING % self.helper_file)

    def test_the_loader_reads_the_entry_on_disk_too(self):
        self.write('plugin.py', self.entry + b'# changed on disk\n')
        with self.assertRaises(Refused) as ctx:
            self.load()  # the entry bytes handed to the loader are the signed ones
        self.assertEqual(str(ctx.exception), 'package_file_digest_mismatch: ' + DIGEST % 'plugin.py')
        self.assertFalse(self.marker.exists())

    # -- something unlisted ---------------------------------------------------------------------------------------
    def test_an_unlisted_package_beside_a_listed_module_is_refused_and_never_imported(self):
        shadow = self.package / self.module
        shadow.mkdir(mode=0o700)
        (shadow / '__init__.py').write_bytes(('with open(%r, "a") as out:\n    out.write("shadow\\n")\n'
                                              % str(self.marker)).encode())
        exc = self.refused()
        self.assertEqual((exc.code, exc.detail), ('package_file_unlisted', UNLISTED % (self.module + '/')))

    def test_an_unlisted_file_is_refused(self):
        self.write('extra.py', b'VALUE = 1\n')
        self.assertEqual(str(self.refused()), 'package_file_unlisted: ' + UNLISTED % 'extra.py')

    def test_an_unlisted_file_in_a_listed_folder_is_refused(self):
        self.write('lib/extra.py', b'VALUE = 1\n')
        self.assertEqual(self.refused().detail, UNLISTED % 'lib/extra.py')

    def test_a_folder_of_compiled_files_is_not_exempt(self):
        (self.package / '__pycache__').mkdir(mode=0o700)
        self.write('__pycache__/%s.cpython-312.pyc' % self.module, b'compiled')
        self.assertEqual(self.refused().detail, UNLISTED % '__pycache__/')

    def test_a_dot_file_is_not_exempt(self):
        self.write('.hidden', b'')
        self.assertEqual(self.refused().detail, UNLISTED % '.hidden')

    def test_an_unlisted_link_is_refused(self):
        (self.package / 'elsewhere').symlink_to(self.tmp, target_is_directory=True)
        self.assertEqual(self.refused().detail, UNLISTED % 'elsewhere')

    # -- owner and mode -----------------------------------------------------------------------------------------
    def test_a_listed_file_others_can_write_is_refused_even_unchanged(self):
        for mode in (0o666, 0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.package / self.helper_file, mode)
                self.assertEqual(str(self.refused()), 'package_file_untrusted: ' + UNTRUSTED % self.helper_file)

    def test_a_folder_on_a_listed_path_others_can_write_is_refused(self):
        os.chmod(self.package / 'lib', 0o777)
        self.assertEqual(self.refused().detail, UNTRUSTED % 'lib/')

    def test_a_package_of_another_user_is_refused_by_the_loader(self):
        # The loader alone: the daemon refuses a folder of another user before it reads a plugin file.
        with mock.patch.object(loader_module.os, 'geteuid', return_value=os.geteuid() + 1):
            with self.assertRaises(Refused) as ctx:
                self.load()
        self.assertEqual(str(ctx.exception), "package_file_untrusted: " + UNTRUSTED % "the plugin's folder")
        self.assertFalse(self.marker.exists())

    # -- no link and no FIFO, even after the presence check -------------------------------------------------------
    def test_a_listed_file_swapped_for_a_link_after_the_presence_check_is_not_followed(self):
        outside = self.tmp / 'outside.py'
        outside.write_bytes(self.helper)  # the same bytes: only following the link would accept it
        os.chmod(outside, 0o600)
        target = self.package / self.helper_file
        target.unlink()
        target.symlink_to(outside)
        with mock.patch.object(loader_module, '_package_file_present', return_value=True):
            with self.assertRaises(Refused) as ctx:
                self.load()
        self.assertEqual(str(ctx.exception), 'package_file_missing: ' + MISSING % self.helper_file)
        self.assertFalse(self.marker.exists())

    def test_a_fifo_in_the_place_of_a_listed_file_is_refused_without_waiting(self):
        (self.package / 'lib' / 'data.txt').unlink()
        os.mkfifo(self.package / 'lib' / 'data.txt', 0o600)
        with mock.patch.object(loader_module, '_package_file_present', return_value=True):
            with self.assertRaises(Refused) as ctx:
                self.load()
        self.assertEqual(str(ctx.exception), 'package_file_missing: ' + MISSING % 'lib/data.txt')

    # -- the doctor, the entry guard, the interpreter's compiled files --------------------------------------------
    def test_the_doctor_names_the_same_refusal_and_runs_nothing(self):
        self.write(self.helper_file, self.helper.replace(b'"signed"', b'"changed"'))
        findings = self.loader().doctor(self.raw, self.signature, self.entry, package_dir=self.package)
        self.assertEqual((findings[-1]['code'], findings[-1]['severity'], findings[-1]['detail']),
                         ('package_file_digest_mismatch', 'refuse', DIGEST % self.helper_file))
        self.assertFalse(self.marker.exists())

    def test_a_missing_entry_is_named_when_no_entry_bytes_are_given_though_its_file_is_there(self):
        with self.assertRaises(Refused) as ctx:
            self.loader().load(self.raw, self.signature, None, package_dir=self.package)
        self.assertEqual(str(ctx.exception), 'package_file_missing: ' + MISSING % 'plugin.py')
        self.assertFalse(self.marker.exists())

    def test_the_plugins_own_imports_leave_no_compiled_file_in_its_package(self):
        child = subprocess.run(
            [sys.executable, '-B', '-c', NO_CACHE_CHILD, str(self.package), self.signature, self.developer,
             '0' if self.key is not None else '1'],
            env=dict(os.environ, PYTHONPATH=CORE), capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 0, child.stderr[-2000:])
        trust, writes, names = child.stdout.split()
        self.assertEqual((trust, writes), ('signed', 'True'))
        self.assertNotIn('__pycache__', names.split(','))
        self.assertEqual(self.marker.read_text(), 'helper\n')

    # -- what is not held to the listing ------------------------------------------------------------------------
    def test_an_unsigned_package_keeps_the_rule_it_had(self):
        # As before: an unsigned load compares plugin.py with the manifest's digest and nothing else.
        self.write(self.helper_file, self.helper.replace(b'"signed"', b'"changed"'))
        self.write('extra.py', b'VALUE = 1\n')
        os.chmod(self.package / 'lib' / 'data.txt', 0o666)
        loaded = self.load(trusted=False)
        self.assertEqual((loaded.trust, loaded.package_verified, loaded.module.VALUE), ('unsigned', False, 'changed'))
        self.forget()
        fx.write_install(self.config, trusted={})
        record, = self.host().records
        self.assertEqual((record['state'], record['trust'], record['verified']),
                         ('loaded', 'unsigned', plugins.VERIFIED_ENTRY_UNSIGNED))

    def test_a_signed_manifest_that_lists_no_file_says_only_plugin_py_was_checked(self):
        shutil.rmtree(self.package)
        source = b'VALUE = 1\n'
        raw = fx.manifest_for(source, self.developer, plugin_id='fixture.vnext', permissions=())
        fx.write_plugin(self.plugins / 'vnext', source, manifest=raw, signature=self.sign(raw))
        (self.plugins / 'vnext' / 'extra.py').write_bytes(b'VALUE = 2\n')
        record, = self.host().records
        self.assertEqual((record['state'], record['trust'], record['verified']),
                         ('loaded', 'signed', plugins.VERIFIED_ENTRY_SIGNED))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class SignedPackageContentByPathTests(SignedPackageContentTests):
    """The same cases where a folder cannot be held by its descriptor: every step is an lstat."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(loader_module, '_WALK_BY_FD', False))
        self.enterContext(mock.patch.object(loader_module, '_LIST_BY_FD', False))


if __name__ == '__main__':
    unittest.main()
