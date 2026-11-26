"""Nothing is opened in a plugin directory the loader refused, and no plugin file read can wait.

A directory that group or other can write is refused by the loader before it opens anything in it.
The plugin rows, status and inventory must not open a file there either: another user may have
planted a FIFO named manifest.json, whose open with no writer waits for ever. In a directory the
operator owns, the one opener looks at each entry without following a link first and opens only a
regular file, with O_NONBLOCK, and only the entry it looked at.

Planted here: a FIFO, a socket file (it stands in for a device and needs no root; a character
device only when the tests run as root), a link out of the directory and a sparse file larger than
the manifest limit. Each call is bounded by PLUGIN_READ_GUARD_SECONDS.

While install.json is not read because its directory is refused (group- or other-writable, a link,
a link to a link, not a directory), no plugin is loaded and none of its code runs: the reserved
effects that file may hold could not be checked. Each plugin.py of those cases writes a marker
when it runs, and the marker must stay absent.
"""
import ast
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import canonical
from recursion_core.plugin_security.tests.support import FakeGilProbe

# The bound of every call below; a call that has not answered by then waited on a file. It becomes the
# settings row PLUGIN_READ_GUARD_SECONDS (shipped default 5) once the person's settings file is at this
# branch's base. The product carries no wait of its own: it never blocks on a plugin file.
PLUGIN_READ_GUARD_SECONDS = 5.0

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: FIFOs, socket files and uid ownership as the plugin host checks them'
SOURCE = b'def registry():\n    return {"panels": []}\n'
PLANTS = ('fifo', 'socket', 'link_out', 'sparse_large', 'char_device')
KIND_WORDS = {'fifo': 'a FIFO (named pipe)', 'socket': 'a socket', 'link_out': 'a symbolic link',
              'char_device': 'a character device', 'sparse_large': 'over the'}
FILE_CODES = {'fifo': 'plugin_file_untrusted', 'socket': 'plugin_file_untrusted', 'char_device': 'plugin_file_untrusted',
              'link_out': 'plugin_file_unreadable', 'sparse_large': 'plugin_file_too_large'}
TAIL = '; a label, not a gate'
NOT_READ_DIRECTORY = 'not read (the plugin directory was refused, so nothing in it was opened)'
NOT_READ_MANIFEST = 'not read (manifest.json was refused)'
NOT_READ_INSTALL = 'not read (install.json or its directory was refused)'
REREAD = 'a directory repaired after the daemon started is read again only after a restart of the daemon'
GROUP_WRITABLE = 'must be an operator-owned directory, not group/other writable; found: group-writable'
NOT_CHECKED = ('so the reserved effects, grants and trusted keys written in it could not be checked, and no plugin '
               'is loaded while it is so; ')
NOTHING_IN_IT = 'neither its install.json nor any plugin in it was read, and no plugin is loaded while it is so; '
# The four plugins of the install directory cases: one names a reserved host, one a reserved credential prefix,
# one has a grant, one names neither. (name, permissions, network destinations, credential refs, granted)
FOUR = (('res', ('network',), ('api.reserved.example:443',), (), True),
        ('cred', ('credential',), (), ('vault/token',), True),
        ('grant', ('panel',), (), (), True),
        ('plain', ('panel',), (), (), False))
LOOSENED = {'gw': 'group-writable', 'ow': 'other-writable', 'link': 'a symbolic link', 'link2': 'a symbolic link',
            'file': 'a regular file'}
PLUGINS_PY = Path(plugins.__file__).resolve()
PACKAGE = PLUGINS_PY.parent.parent


def _digest(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PluginReadGuardTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-read-guard-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        # opened: every os.open; reads: every call of the host's file reader; count: one folder per install.
        self.fifos, self.opened, self.reads, self.swap, self.count = [], [], [], None, 0
        real_open, real_read = os.open, plugins._read_owned

        def recorded_open(path, *args, **kwargs):
            name = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else repr(path)
            self.opened.append(name)
            if self.swap is not None and name == self.swap[0]:
                target, action = self.swap
                self.swap = None
                action(target)  # the entry changes after the look and before the open
            return real_open(path, *args, **kwargs)

        def recorded_read(path, limit, **kwargs):
            self.reads.append(os.fsdecode(path))
            return real_read(path, limit, **kwargs)

        for patcher in (mock.patch.object(os, 'open', recorded_open),
                        mock.patch.object(plugins, '_read_owned', recorded_read)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._unblock)

    # -- helpers ----------------------------------------------------------------------------------
    def _unblock(self):
        """Give a FIFO reader that waits a writer and an end of file, so a failing case cannot hang the run."""
        for path in self.fifos:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                continue
            os.close(fd)

    def bounded(self, what, call):
        box = {}

        def run():
            try:
                box['value'] = call()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
                box['error'] = exc

        thread = threading.Thread(target=run, name='plugin-read-guard', daemon=True)
        thread.start()
        thread.join(PLUGIN_READ_GUARD_SECONDS)
        if thread.is_alive():
            deadline = time.monotonic() + PLUGIN_READ_GUARD_SECONDS
            while thread.is_alive() and time.monotonic() < deadline:
                self._unblock()
                thread.join(0.05)
            self.fail('%s did not answer within %g s: a plugin file read waited' % (what, PLUGIN_READ_GUARD_SECONDS))
        if 'error' in box:
            raise box['error']
        return box['value']

    def plant(self, kind, target, outside):
        target.unlink()
        if kind == 'fifo':
            os.mkfifo(target, 0o600)
            self.fifos.append(target)
        elif kind == 'socket':
            os.mknod(target, stat.S_IFSOCK | 0o600)
        elif kind == 'char_device':
            if os.geteuid() != 0:
                self.skipTest('a character device needs root to make; the socket file stands in for it')
            os.mknod(target, stat.S_IFCHR | 0o600, os.makedev(1, 3))
        elif kind == 'link_out':
            os.symlink(outside, target)  # a manifest whose label is recorded: following the link would find it
        else:
            with open(target, 'wb') as handle:
                handle.truncate(plugins.MAX_MANIFEST_BYTES * 1024 * 16)  # sparse: no data blocks are written
            os.chmod(target, 0o600)

    def install(self, kind=None, *, refuse_dir=False, labels=True):
        """config/recursion/plugins/planted with ``kind`` planted as manifest.json; the label is recorded."""
        self.count += 1
        config = self.tmp / ('install-%d' % self.count) / 'recursion'
        directory = config / 'plugins' / 'planted'
        manifest = fx.write_plugin(directory, SOURCE, plugin_id='fixture.planted', permissions=('panel',))
        fx.write_install(config, grants=[fx.grant_for(SOURCE, plugin_id='fixture.planted', permissions=('panel',))],
                         registry_labels={_digest(manifest): 'verified'} if labels else {})
        outside = config.parent / 'outside'
        outside.mkdir(mode=0o700)
        (outside / 'manifest.json').write_bytes(manifest)
        os.chmod(outside / 'manifest.json', 0o600)
        if kind is not None:
            self.plant(kind, directory / 'manifest.json', outside / 'manifest.json')
        if refuse_dir:
            os.chmod(directory, 0o770)  # group-writable: the loader refuses it
        return config, directory, manifest

    def host(self, config, available, state=None):
        def build():
            with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                return plugins.PluginHost(config, state or config.parent / 'state', gil_probe=FakeGilProbe(),
                                          verification_available=lambda: available)
        made = self.bounded('the daemon start (PluginHost)', build)
        self.addCleanup(made.close)
        return made

    def row(self, host):
        rows = self.bounded('the plugin rows (route_records)', host.route_records)
        return next(r for r in rows if r['id'] == 'plugin:planted')['blocker']

    def opened_under(self, directory):
        return [p for p in self.opened + self.reads if p == str(directory) or p.startswith(str(directory) + os.sep)]

    # -- a directory the loader refused ------------------------------------------------------------
    def test_nothing_is_opened_in_a_refused_directory_by_rows_status_or_inventory(self):
        for kind in PLANTS:
            for available in (True, False):
                with self.subTest(kind=kind, verification_available=available):
                    config, directory, _ = self.install(kind, refuse_dir=True)
                    host = self.host(config, available)
                    for _ in range(2):
                        blocker = self.row(host)
                    self.bounded('status', lambda: host.status('fixture.planted'))
                    listed = self.bounded('inventory', host.inventory)
                    self.assertEqual(len(listed['plugins']), 1)
                    self.assertEqual(self.opened_under(directory), [])
                    code = 'plugin_directory_untrusted' if available else 'plugin_verification_unavailable'
                    self.assertTrue(blocker.startswith('refused: ' + code), blocker)
                    if available:
                        self.assertIn('found: group-writable', blocker)
                    else:  # refused for another reason: the row still gives the directory's own
                        self.assertIn('; the plugin directory %s %s' % (directory, GROUP_WRITABLE), blocker)
                    self.assertIn(REREAD, blocker)
                    self.assertTrue(blocker.endswith('; registry label: ' + NOT_READ_DIRECTORY + TAIL), blocker)

    def test_a_refused_directory_stays_refused_for_the_run(self):
        config, directory, _ = self.install('fifo', refuse_dir=True)
        host = self.host(config, True)
        os.chmod(directory, 0o700)  # it looks owned now; this run read nothing there and reads nothing now
        self.assertTrue(self.row(host).endswith(NOT_READ_DIRECTORY + TAIL))
        self.assertIn(REREAD, self.row(host))  # the row says that a restart is what reads it again
        self.assertEqual(self.opened_under(directory), [])

    # -- an install directory others can write ---------------------------------------------------------
    def test_a_plugin_named_outside_an_install_directory_others_can_write_is_refused_and_says_why(self):
        for available in (True, False):
            with self.subTest(verification_available=available):
                self.count += 1
                root = self.tmp / ('outside-%d' % self.count)
                config, own = root / 'recursion', root / 'own' / 'mine'
                manifest = fx.write_plugin(own, SOURCE, plugin_id='fixture.mine', permissions=('panel',))
                # install.json grants the plugin and labels its bytes, but others can write its directory
                installed = fx.write_install(config, registry_labels={_digest(manifest): 'verified'},
                                             grants=[fx.grant_for(SOURCE, plugin_id='fixture.mine',
                                                                  permissions=('panel',))])
                os.chmod(installed, 0o770)

                def build():
                    with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                        return plugins.PluginHost(config, root / 'state', plugin_dirs=[own], gil_probe=FakeGilProbe(),
                                                  verification_available=lambda: available)
                host = self.bounded('the daemon start (PluginHost)', build)
                self.addCleanup(host.close)
                rows = {r['id']: r['blocker'] for r in self.bounded('the plugin rows', host.route_records)}
                self.assertNotIn(str(installed / 'install.json'), self.opened)
                self.assertEqual(self.opened_under(own), [])
                mine = rows['plugin:mine']
                repair = ('make %s a directory of your own (not a link) that group and other cannot write (only its '
                          'mode is wrong: chmod go-w -- %s), then restart the daemon' % (installed, shlex.quote(str(installed))))
                unread = 'install.json was not read: the install directory %s %s; ' % (installed, GROUP_WRITABLE)
                self.assertIn(unread + NOT_CHECKED + repair + '; registry label: ' + NOT_READ_INSTALL + TAIL, mine)
                self.assertTrue(mine.endswith('; registry label: ' + NOT_READ_INSTALL + TAIL), mine)
                # refused before its code runs, with no grant, wherever its own directory lies
                record = next(r for r in host.records if r['source'] == str(own))
                code = 'plugin_directory_untrusted' if available else 'plugin_verification_unavailable'
                self.assertEqual((record['state'], record['code'], record['plugin_id']), ('refused', code, None))
                self.assertTrue(mine.startswith('refused: %s: ' % code), mine)
                self.assertIsNone(host.loader)
                # its own directory is not blamed; the install directory's own row names it and what to do
                self.assertNotIn('the plugin directory %s' % own, mine)
                self.assertNotIn(str(own), mine)
                self.assertIn('refused: plugin_directory_untrusted: install directory %s %s; ' % (installed, GROUP_WRITABLE)
                              + NOTHING_IN_IT + repair, rows['plugin:plugins'])

    def four(self, form, *, inner=False, broken=False):
        """The four plugins named by --plugin, and an install.json that reserves the host and the credential
        prefix two of them name, grants three of them and labels one, with its directory in ``form``.

        Forms: sound; nofile (a sound directory, no install.json); absent (no install directory); gw and ow
        (group- or other-writable); link and link2 (a link to a sound directory, a link to a link); file (a
        regular file where the directory should be). ``inner``: the plugins lie inside the install directory.
        ``broken``: one more plugin whose manifest is not JSON, which the loader would refuse for its own reason.
        Returns (config, plugin directories, marker directory, the directory install.json is in or None)."""
        self.count += 1
        root = self.tmp / ('four-%d' % self.count)
        config, marks = root / 'recursion', root / 'marks'
        marks.mkdir(parents=True, mode=0o700)
        config.mkdir(parents=True, mode=0o700)
        home = config / 'plugins' if inner else root / 'own'
        dirs, grants, labels = [], [], {}
        for name, permissions, destinations, credentials, granted in FOUR:
            source = ('open(%r, "w").close()\n' % str(marks / name)).encode() + SOURCE
            manifest = canonical({'schema': 'csl-plugin-manifest/vNext', 'plugin_id': 'four.' + name,
                                  'version': '1.0.0', 'artifact_sha256': _digest(source),
                                  'dependency_lock_sha256': '0' * 64, 'install_scripts': [],
                                  'developer_key': fx.FAKE_DEVELOPER_KEY, 'permissions': list(permissions),
                                  'network_destinations': list(destinations), 'credential_refs': list(credentials)})
            fx.write_plugin(home / name, source, manifest=manifest)
            dirs.append(home / name)
            if granted:
                grants.append(fx.grant_for(source, plugin_id='four.' + name, permissions=permissions))
            if name == 'grant':
                labels[_digest(manifest)] = 'verified'
        if broken:
            fx.write_plugin(home / 'broken', SOURCE, manifest=b'not JSON')
            dirs.append(home / 'broken')
        content = dict(grants=grants, reserved={'hosts': ['reserved.example'], 'credential_prefixes': ['vault/']},
                       registry_labels=labels)
        installed = None
        if form in ('sound', 'gw', 'ow'):
            installed = fx.write_install(config, **content)
            if form != 'sound':
                os.chmod(installed, 0o770 if form == 'gw' else 0o702)
        elif form == 'nofile':
            (config / 'plugins').mkdir(mode=0o700, exist_ok=True)
            os.chmod(config / 'plugins', 0o700)
        elif form in ('link', 'link2'):
            installed = fx.write_install(root / 'elsewhere', **content)
            if form == 'link':
                os.symlink(installed, config / 'plugins')
            else:
                os.symlink(installed, root / 'middle')
                os.symlink(root / 'middle', config / 'plugins')
        elif form == 'file':
            (config / 'plugins').write_text('{}')
            os.chmod(config / 'plugins', 0o600)
        return config, dirs, marks, installed

    def build(self, config, dirs, available=True):
        def make():
            with self.assertLogs('recursion_core.daemon.plugins', 'INFO'):
                return plugins.PluginHost(config, config.parent / 'state', plugin_dirs=dirs, gil_probe=FakeGilProbe(),
                                          verification_available=lambda: available)
        host = self.bounded('the daemon start (PluginHost)', make)
        self.addCleanup(host.close)
        return host

    def rows_of(self, host):
        rows = {r['id']: r['blocker'] for r in self.bounded('the plugin rows', host.route_records)}
        self.bounded('status', lambda: host.status('four.res'))
        self.bounded('inventory', host.inventory)
        return rows

    def assert_refused_and_never_run(self, form, config, dirs, marks, host, rows, available):
        self.assertEqual(sorted(p.name for p in marks.iterdir()), [], 'plugin code ran')
        self.assertIsNone(host.loader)
        self.assertEqual([p for p in self.opened if p.endswith(os.sep + 'install.json')], [])  # never opened
        code = 'plugin_directory_untrusted' if available else 'plugin_verification_unavailable'
        unread = 'install.json was not read: the install directory %s %s; found: %s' % (
            config / 'plugins', plugins.DIRECTORY_REFUSED, LOOSENED[form])
        for own in dirs:
            with self.subTest(plugin=own.name):
                self.assertEqual(self.opened_under(own), [])
                record = next(r for r in host.records if r['source'] == str(own))
                self.assertEqual((record['state'], record['code'], record['plugin_id']), ('refused', code, None))
                row = rows['plugin:' + own.name]
                self.assertTrue(row.startswith('refused: %s: ' % code), row)
                self.assertIn(unread, row)
                self.assertIn(NOT_CHECKED, row)
                self.assertIn(', then restart the daemon; registry label: ', row)
                self.assertEqual(row.count('install.json was not read'), 1, row)
                self.assertNotIn('unlisted', row)
                self.assertTrue(row.endswith('; registry label: ' + NOT_READ_INSTALL + TAIL), row)
        if form != 'file':  # discovery finds the install directory itself and gives it its own row
            self.assertIn('refused: plugin_directory_untrusted: install directory %s ' % (config / 'plugins'),
                          rows['plugin:plugins'])
            self.assertIn(NOTHING_IN_IT, rows['plugin:plugins'])

    def test_an_install_directory_refused_for_its_mode_or_kind_loads_no_plugin_and_runs_none(self):
        for form in LOOSENED:
            for available in (True, False):
                with self.subTest(form=form, verification_available=available):
                    config, dirs, marks, _ = self.four(form)
                    host = self.build(config, dirs, available)
                    rows = self.rows_of(host)
                    self.assert_refused_and_never_run(form, config, dirs, marks, host, rows, available)

    def test_a_plugin_inside_the_refused_install_directory_is_refused_like_every_other(self):
        for form in ('gw', 'ow'):
            with self.subTest(form=form):
                config, dirs, marks, installed = self.four(form, inner=True)
                self.assertTrue(all(own.parent == installed for own in dirs))
                host = self.build(config, dirs)
                rows = self.rows_of(host)
                self.assert_refused_and_never_run(form, config, dirs, marks, host, rows, True)

    def test_a_plugin_refused_for_its_own_reason_under_an_unread_install_json_says_not_read(self):
        for form in ('gw', 'link'):
            for available in (True, False):
                with self.subTest(form=form, verification_available=available):
                    config, dirs, marks, _ = self.four(form, broken=True)
                    host = self.build(config, dirs, available)
                    rows = self.rows_of(host)
                    self.assert_refused_and_never_run(form, config, dirs, marks, host, rows, available)
                    self.assertTrue(rows['plugin:broken'].endswith('; registry label: ' + NOT_READ_INSTALL + TAIL))

    def test_with_a_sound_install_directory_or_none_the_four_plugins_load_as_before(self):
        reserved = {'res': ('refused', 'reserved_destination', None), 'cred': ('refused', 'reserved_credential', None),
                    'grant': ('loaded', None, ['panel']), 'plain': ('loaded', None, [])}
        unrestricted = {name: ('loaded', None, []) for name, *_ in FOUR}
        for form, expected in (('sound', reserved), ('nofile', unrestricted), ('absent', unrestricted)):
            with self.subTest(form=form):
                config, dirs, marks, _ = self.four(form)
                host = self.build(config, dirs)
                rows = self.rows_of(host)
                got = {Path(r['source']).name: (r['state'], r['code'], r.get('permissions')) for r in host.records}
                self.assertEqual(got, expected)
                ran = sorted(name for name, (state, _, _) in expected.items() if state == 'loaded')
                self.assertEqual(sorted(p.name for p in marks.iterdir()), ran)
                self.assertTrue(all('install.json was not read' not in row for row in rows.values()), rows)
                label = 'verified' if form == 'sound' else 'unlisted'
                self.assertTrue(rows['plugin:four.grant'].endswith('; registry label: %s%s' % (label, TAIL)))

    def test_the_repair_command_is_one_exact_command_or_none(self):
        for case in ('space', 'dash', 'newline'):
            with self.subTest(case=case):
                self.count += 1
                own = self.tmp / ('own-%d' % self.count) / 'mine'
                fx.write_plugin(own, SOURCE, plugin_id='fixture.mine', permissions=('panel',))
                if case == 'dash':  # a relative configuration directory that begins with a dash, as given
                    work = self.tmp / ('cwd-%d' % self.count)
                    work.mkdir(mode=0o700)
                    previous = os.getcwd()
                    os.chdir(work)
                    self.addCleanup(os.chdir, previous)
                    config = Path('-x') / 'recursion'
                else:
                    config = self.tmp / ('sp ace-%d' % self.count if case == 'space' else 'line\nbreak-%d' % self.count) / 'recursion'
                installed = fx.write_install(config, grants=[])
                os.chmod(installed, 0o770)
                host = self.build(config, [own])
                rows = self.rows_of(host)
                path = str(config / 'plugins')
                for row in (rows['plugin:plugins'], rows['plugin:mine']):
                    if case == 'newline':
                        self.assertNotIn('chmod', row)
                        self.assertIn('so no command is printed: take the write permission of group and other off '
                                      'that directory), then restart the daemon', row)
                        continue
                    command = 'chmod go-w -- ' + shlex.quote(path)
                    self.assertIn('(only its mode is wrong: %s), then restart the daemon' % command, row)
                    self.assertEqual(row.count('chmod'), 1, row)
                    # what a POSIX shell makes of it: one command, the options ended, the whole path one argument
                    self.assertEqual(shlex.split(command), ['chmod', 'go-w', '--', path])

    def test_a_repair_command_is_given_only_when_the_mode_alone_is_wrong(self):
        path = Path('/run/x')
        for found, has_command in (('group-writable', True), ('group-writable, other-writable', True),
                                   ('other-writable', True), ('owned by another user, group-writable', False),
                                   ('a symbolic link (links are not followed)', False), ('a regular file', False),
                                   ('nothing at that path', False)):
            with self.subTest(found=found):
                words = plugins._repair(path, plugins.DIRECTORY_REFUSED + '; found: ' + found)
                self.assertTrue(words.startswith('make /run/x a directory of your own (not a link) that group and '
                                                 'other cannot write'), words)
                self.assertEqual('chmod go-w -- /run/x' in words, has_command, words)

    def test_install_json_in_a_directory_others_can_write_is_refused_never_read_as_no_file(self):
        config = self.tmp / 'gw' / 'recursion'
        installed = fx.write_install(config, registry_labels={'0' * 64: 'verified'})
        os.chmod(installed, 0o770)
        with self.assertRaises(plugins.PluginRefused) as caught:
            plugins.read_install(installed)
        self.assertEqual((caught.exception.code, caught.exception.detail), ('plugin_directory_untrusted', GROUP_WRITABLE))
        self.assertNotIn(str(installed / 'install.json'), self.opened)

    # -- a directory the operator owns ---------------------------------------------------------------
    def test_each_planted_entry_is_refused_with_its_kind_and_never_opened(self):
        for kind in PLANTS:
            for available in (True, False):
                with self.subTest(kind=kind, verification_available=available):
                    config, directory, _ = self.install(kind)
                    host = self.host(config, available)
                    blocker = self.row(host)
                    self.assertNotIn(str(directory / 'manifest.json'), self.opened)  # looked at, never opened
                    self.assertTrue(blocker.endswith('; registry label: ' + NOT_READ_MANIFEST + TAIL), blocker)
                    if available:
                        record, = host.records
                        self.assertEqual(record['code'], FILE_CODES[kind])
                        self.assertIn(KIND_WORDS[kind], record['detail'])
                        self.assertIn(KIND_WORDS[kind], blocker)

    def test_an_entry_swapped_after_the_look_is_refused_before_a_byte_is_read(self):
        config, directory, manifest = self.install()
        target = directory / 'manifest.json'

        def replace(path):
            other = directory / 'other.json'
            other.write_bytes(manifest.replace(b'fixture.planted', b'fixture.swapped'))
            os.chmod(other, 0o600)
            os.replace(other, path)

        self.swap = (str(target), replace)
        host = self.host(config, True)
        record, = host.records
        self.assertEqual((record['code'], record['plugin_id']), ('plugin_file_untrusted', None))
        self.assertIn('changed between the look and the open', record['detail'])

    def test_a_fifo_swapped_in_after_the_look_cannot_make_the_open_wait(self):
        config, directory, _ = self.install()
        target = directory / 'manifest.json'

        def to_fifo(path):
            os.unlink(path)
            os.mkfifo(path, 0o600)
            self.fifos.append(path)

        self.swap = (str(target), to_fifo)
        host = self.host(config, True)
        record, = host.records
        self.assertEqual(record['code'], 'plugin_file_untrusted')

    # -- the two label threads -----------------------------------------------------------------------
    def test_a_refusing_trust_store_still_shows_the_recorded_label(self):
        config, _, _ = self.install()
        state = config.parent / 'state'
        store = state / 'plugin-security'
        store.mkdir(parents=True, mode=0o700)
        os.chmod(state, 0o700)
        (store / 'revoked.sqlite3').write_bytes(b'not a database')
        os.chmod(store / 'revoked.sqlite3', 0o600)
        host = self.host(config, True, state=state)
        self.assertEqual([r['code'] for r in host.records], ['revocation_store_invalid'])
        self.assertTrue(self.row(host).endswith('; registry label: verified' + TAIL))

    def test_the_install_file_is_read_for_labels_only_when_a_row_needs_one_and_once_per_change(self):
        empty = self.tmp / 'empty' / 'recursion'
        fx.write_install(empty, registry_labels={'0' * 64: 'verified'})
        bare = plugins.PluginHost(empty, empty.parent / 'state', gil_probe=FakeGilProbe(),
                                  verification_available=lambda: False)
        self.addCleanup(bare.close)
        for _ in range(3):
            self.assertEqual(self.bounded('the plugin rows', bare.route_records), [])
        install = empty / 'plugins' / 'install.json'
        self.assertEqual([p for p in self.opened if p == str(install)], [], 'no row, yet install.json was read')

        config, _, manifest = self.install()
        host = self.host(config, False)
        install = config / 'plugins' / 'install.json'
        for _ in range(3):
            self.assertTrue(self.row(host).endswith('; registry label: verified' + TAIL))
        self.assertEqual(len([p for p in self.opened if p == str(install)]), 1)
        fx.write_install(config, grants=[], registry_labels={_digest(manifest): 'non_verified', '1' * 64: 'verified'})
        for _ in range(2):
            self.assertTrue(self.row(host).endswith('; registry label: non-verified' + TAIL))
        self.assertEqual(len([p for p in self.opened if p == str(install)]), 2)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PluginReadStructureTests(unittest.TestCase):
    """ast over daemon/ and gui/: a plugin file is opened only by the one opener, after the one verdict."""

    OPENS = frozenset({'open', 'fdopen', 'read_bytes', 'read_text'})
    LISTS = frozenset({'iterdir', 'listdir', 'scandir'})
    PLUGIN_NAMES = frozenset({'MANIFEST', 'SIGNATURE', 'ARTIFACT', 'INSTALL_FILE', 'PLUGINS_DIR', 'manifest.json',
                              'manifest.sig', 'plugin.py', 'install.json', '_read_owned'})

    @staticmethod
    def _name(node):
        func = node.func
        return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None

    def _calls_by_function(self, tree):
        """{function name: [(line, called name, call node)]}, each call under its innermost function."""
        found = {}

        def walk(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name)
                    continue
                if isinstance(child, ast.Call) and owner is not None:
                    found.setdefault(owner, []).append((child.lineno, self._name(child), child))
                walk(child, owner)
        walk(tree, None)
        return found

    def _first(self, calls, name):
        lines = [line for line, called, _ in calls if called == name]
        return min(lines) if lines else None

    def test_the_plugin_host_opens_a_file_only_in_the_one_opener_and_only_after_the_verdict(self):
        calls = self._calls_by_function(ast.parse(PLUGINS_PY.read_text(encoding='utf-8')))
        self.assertIn('_read_owned', calls)
        openers = sorted(fn for fn, items in calls.items() if any(c in self.OPENS for _, c, _ in items))
        self.assertEqual(openers, ['_read_owned'])
        listers = sorted(fn for fn, items in calls.items() if any(c in self.LISTS for _, c, _ in items))
        self.assertEqual(listers, ['_sources'])
        own = calls['_read_owned']
        verdict, look, opened = (self._first(own, n) for n in ('plugin_directory_verdict', 'lstat', 'open'))
        self.assertTrue(None not in (verdict, look, opened) and verdict < look < opened,
                        'the opener must ask the directory verdict, then lstat, then open')
        flags = {n.attr for _, c, call in own if c == 'open' for n in ast.walk(call) if isinstance(n, ast.Attribute)}
        self.assertTrue({'O_NOFOLLOW', 'O_NONBLOCK'} <= flags, flags)
        text = PLUGINS_PY.read_text(encoding='utf-8')
        opener = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name == '_read_owned')
        compared = {n.attr for c in ast.walk(opener) if isinstance(c, ast.Compare) for n in ast.walk(c)
                    if isinstance(n, ast.Attribute)}
        self.assertTrue({'st_dev', 'st_ino'} <= compared, 'the opened file must be the entry that was looked at')
        self.assertIn('plugin_directory_verdict', {c for _, c, _ in calls['_directory_refusal']})
        for fn, items in calls.items():
            uses = [line for line, c, _ in items if c == '_read_owned' or c in self.LISTS]
            if not uses or fn in ('read_install', '_read_owned'):
                continue
            first_use = min(uses)
            guard = self._first(items, '_directory_refusal')
            self.assertTrue(guard is not None and guard < first_use,
                            '%s reads under a plugin directory without asking _directory_refusal first' % fn)

    def test_no_other_module_of_daemon_or_gui_opens_a_plugin_file(self):
        offenders = []
        for folder in ('daemon', 'gui'):
            for path in sorted((PACKAGE / folder).glob('*.py')):
                if path.resolve() == PLUGINS_PY:
                    continue
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Name, ast.Attribute)) and \
                            (node.id if isinstance(node, ast.Name) else node.attr) == '_read_owned':
                        offenders.append('%s:%d names _read_owned' % (path.name, node.lineno))
                    if not isinstance(node, ast.Call) or self._name(node) not in self.OPENS | self.LISTS:
                        continue
                    for part in ast.walk(node):
                        word = (part.id if isinstance(part, ast.Name) else part.attr if isinstance(part, ast.Attribute)
                                else part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                                else None)
                        if word in self.PLUGIN_NAMES:
                            offenders.append('%s:%d opens %s' % (path.name, node.lineno, word))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
