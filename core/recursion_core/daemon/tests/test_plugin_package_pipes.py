"""A pipe at a package's own files is refused at once on every read of the install path.

``verify_package`` opens each file without blocking and refuses a pipe. The host then reads
``manifest.json``, ``manifest.sig`` and ``plugin.py`` again for the loader's ``doctor``, and reads
``plugin.py`` again when the verified bytes do not hold it. Each of those reads must refuse a pipe
too, and must not wait for a writer that never comes.

Each start of the host runs in a thread with the test's own bound. A start still running after it
is taken as blocked; the test then opens the pipe for writing without blocking (that open fails at
once when nothing waits on the far end) and closes it, so the blocked read ends and the run goes on.

Also pinned: the record of a ``package_file_missing`` refusal found through ``doctor`` carries the
findings ``doctor`` gave for that package, so it names its own plugin.

Pipes are a POSIX thing. On Windows (and in the simulated-Windows step, which deletes
``os.mkfifo``) every test here is skipped, as the plugin host itself refuses that platform.
"""

import contextlib
import os
import threading
import unittest
from unittest import mock

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.daemon.tests.test_plugins import LINUX, LINUX_ONLY, MISSING, HostCase, codes
from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import Key
from recursion_core.plugin_security.tests.test_manifest_v1 import NOTES_SOURCE, v1

# The test's own bound on one start of the host, in seconds. A start holds a few small files and
# ends in well under a second; one still running after this long is waiting on the pipe.
START_BOUND_SECONDS = 5
# After that bound, how often the far end is opened and closed to end a blocked read, and how long
# the test waits for the start to end after each try.
RELEASE_TRIES = 25
RELEASE_WAIT_SECONDS = 0.2


@unittest.skipUnless(LINUX and hasattr(os, 'mkfifo'), LINUX_ONLY)
@unittest.skipUnless(backend_available(), 'needs cryptography to sign the package')
class PipeAtPackageFileTests(HostCase):
    def setUp(self):
        super().setUp()
        self.key = Key()
        fx.write_install(self.config, trusted={'dev': self.key.public})
        self.marker = self.tmp / 'plugin-code-ran'
        self.source = ('open(%r, "w").close()\n' % str(self.marker)).encode() + fx.OPERATOR_SOURCE
        self.package = self.signed('fixture', 'fixture.operator', self.source)

    def signed(self, name, plugin_id, source, *, whole=True):
        """A signed version-1 package listing plugin.py, notes/__init__.py and notes/index.py."""
        raw = v1(self.key.public, source=source, plugin_id=plugin_id, permissions=())
        package = self.plugins / name
        fx.write_plugin(package, source, manifest=raw, signature=self.key.sign(raw))
        if whole:
            notes = package / 'notes'
            notes.mkdir(mode=0o700)
            for file_name, data in (('__init__.py', b''), ('index.py', NOTES_SOURCE)):
                (notes / file_name).write_bytes(data)
                os.chmod(notes / file_name, 0o600)
        return package

    def pipe_at(self, name):
        path = self.package / name
        path.unlink()
        os.mkfifo(path, 0o600)
        return path

    def start_bounded(self, pipe, *, verify=None):
        """Start the host in a thread; return (blocked, record).

        ``blocked`` is True when the start was still running after ``START_BOUND_SECONDS``. The
        pipe's far end is only ever opened without blocking, so the test cannot wait on it.
        """
        out = {}

        def start():
            try:
                out['host'] = self.host()
            except BaseException as exc:  # reported below, in the test's own thread
                out['error'] = exc

        thread = threading.Thread(target=start, name='pipe-test-host-start', daemon=True)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(plugins.log, 'error'))
            if verify is not None:
                stack.enter_context(mock.patch.object(plugins, 'verify_package', side_effect=verify))
            thread.start()
            thread.join(START_BOUND_SECONDS)
            blocked = thread.is_alive()
            for _ in range(RELEASE_TRIES):
                if not thread.is_alive():
                    break
                try:
                    os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:
                    pass  # nothing waits on the far end at this moment
                thread.join(RELEASE_WAIT_SECONDS)
        self.assertFalse(thread.is_alive(), 'the start of the host still runs after the pipe was released')
        self.assertNotIn('error', out)
        record, = out['host'].records
        return blocked, record

    def assert_refused_at_once(self, pipe, code, detail, **options):
        blocked, record = self.start_bounded(pipe, **options)
        self.assertFalse(blocked, 'the start of the host blocked on a pipe at %s' % pipe.name)
        self.assertEqual((record['state'], record['code']), ('refused', code))
        self.assertTrue(record['detail'].startswith(detail), record['detail'])
        self.assertNotIn('active', codes(record))
        self.assertFalse(self.marker.exists(), 'plugin code ran')

    def test_a_pipe_at_plugin_py_is_refused_without_blocking(self):
        pipe = self.pipe_at('plugin.py')
        self.assert_refused_at_once(pipe, 'plugin_file_unreadable', 'plugin.py')

    def test_a_pipe_at_manifest_sig_is_refused_without_blocking(self):
        pipe = self.pipe_at('manifest.sig')
        self.assert_refused_at_once(pipe, 'plugin_file_untrusted', 'manifest.sig')

    def test_a_pipe_at_manifest_json_is_refused_without_blocking(self):
        pipe = self.pipe_at('manifest.json')
        self.assert_refused_at_once(pipe, 'plugin_file_untrusted', 'manifest.json')

    def test_a_pipe_swapped_in_at_plugin_py_after_verification_is_refused_without_blocking(self):
        # plugin.py differs from its digest, so the verified bytes do not hold it and the host reads
        # it again; a pipe put in its place between the two reads must not block that read.
        (self.package / 'plugin.py').write_bytes(self.source + b'\n# changed\n')
        real = plugins.verify_package
        pipe = self.package / 'plugin.py'

        def then_swap(source, trust):
            result = real(source, trust)
            pipe.unlink()
            os.mkfifo(pipe, 0o600)
            return result

        self.assert_refused_at_once(pipe, 'plugin_file_untrusted', 'plugin.py', verify=then_swap)

    def test_a_pipe_at_a_listed_file_other_than_the_entry_is_package_file_missing(self):
        (self.package / 'notes' / 'index.py').unlink()
        pipe = self.package / 'notes' / 'index.py'
        os.mkfifo(pipe, 0o600)
        blocked, record = self.start_bounded(pipe)
        self.assertFalse(blocked, 'the start of the host blocked on a pipe at notes/index.py')
        self.assertEqual((record['state'], record['code'], record['detail']),
                         ('refused', 'package_file_missing', MISSING % 'notes/index.py'))
        self.assertFalse(self.marker.exists(), 'plugin code ran')


@unittest.skipUnless(LINUX, LINUX_ONLY)
@unittest.skipUnless(backend_available(), 'needs cryptography to sign the package')
class MissingFileRecordTests(HostCase):
    """The record of a package_file_missing refusal found through doctor names its own plugin."""

    def setUp(self):
        super().setUp()
        self.key = Key()
        fx.write_install(self.config, trusted={'dev': self.key.public})

    signed = PipeAtPackageFileTests.signed

    @staticmethod
    def own_manifest(record):
        return [f['detail'] for f in record['findings'] if f['code'] == 'manifest_ok']

    def test_the_record_carries_the_findings_doctor_gave(self):
        self.signed('fixture', 'fixture.operator', fx.OPERATOR_SOURCE, whole=False)
        with mock.patch.object(plugins.log, 'error'):
            host = self.host()
        record, = host.records
        self.assertEqual((record['state'], record['code'], record['detail']),
                         ('refused', 'package_file_missing', MISSING % 'notes/__init__.py'))
        self.assertEqual(record['plugin_id'], 'fixture.operator')
        self.assertEqual(self.own_manifest(record), ['fixture.operator'])

    def test_after_an_earlier_load_the_record_names_the_refused_plugin(self):
        self.signed('fixture', 'fixture.operator', fx.OPERATOR_SOURCE)  # whole: loads first, signed
        self.signed('zz-second', 'fixture.second', fx.OPERATOR_SOURCE, whole=False)
        with mock.patch.object(plugins.log, 'error'):
            host = self.host()
        first, second = host.records
        self.assertEqual((first['state'], first['plugin_id'], first['trust']),
                         ('loaded', 'fixture.operator', 'signed'))
        self.assertEqual((second['state'], second['code']), ('refused', 'package_file_missing'))
        self.assertEqual(second['plugin_id'], 'fixture.second')
        self.assertEqual(self.own_manifest(second), ['fixture.second'])


if __name__ == '__main__':
    unittest.main()
