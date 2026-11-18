"""The operator page draws each plugin row's registry label as text.

PRODUCT.md G7 (line 1900): an install shows a plugin verified or non-verified.
PRODUCT.md 4.1 (line 1237): verified is a label, not a gate.
PRODUCT.md 3.5 (lines 1005-1007): Recursion draws that text itself, outside a plugin's panel.

Three hand-installed plugins, one label each. The page's plugin rows come from
``PluginHost.route_records``. The core writes ``verified``, ``non-verified`` or
``unlisted``, and the words "a label, not a gate". A plugin's own panel cannot
change those cells. The label is display only: it does not decide whether the
plugin loads.
"""
from http.client import HTTPConnection
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import plugins
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import ControlClient
from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the plugin host checks file ownership by uid and keeps its stores under the daemon '
              'state directory')
READ = 'operator.positions.inspect'
MARK = '; registry label: '
PHRASE = {
    'verified': 'verified; a label, not a gate',
    'non_verified': 'non-verified; a label, not a gate',
    'unlisted': 'unlisted; a label, not a gate',
}
_SPECS = (
    # Directory name matches plugin_id, so a row refused before the manifest is read
    # keeps the same id the page shows once the plugin loads.
    ('fixture.verified', 'fixture.verified', 'panel.verified', 'Fixture positions', 'verified'),
    # This plugin's panel title is the word "verified". Its install label is non_verified.
    ('fixture.nonverified', 'fixture.nonverified', 'panel.nonverified', 'verified', 'non_verified'),
    # This title repeats a verified phrase. Its install recorded no label for these bytes.
    ('fixture.unlisted', 'fixture.unlisted', 'panel.unlisted',
     'registry label: verified; a label, not a gate', None),
)


def _source(panel_id, title):
    """A plugin that tries to name its own label. The page must not use that name."""
    return (
        'def registry():\n'
        '    return {"registry_label": "verified", "label": "verified",\n'
        '            "panels": [{"id": "%s", "title": "%s", "reads": ["%s"]}]}\n'
        % (panel_id, title, READ)
    ).encode()


def _row(page, plugin_id):
    found = re.search(r'<tr><th scope="row">plugin:%s</th>.*?</tr>' % re.escape(plugin_id), page)
    if found is None:
        raise AssertionError('no plugin row for %s' % plugin_id)
    return found.group(0)


def _label_cell(row):
    cells = re.findall(r'<td class="registry-label">(.*?)</td>', row)
    if len(cells) != 1:
        raise AssertionError('plugin row has %d registry-label cells: %s' % (len(cells), row))
    return cells[0]


@unittest.skipUnless(LINUX, LINUX_ONLY)
class LabelOnThePageTests(unittest.TestCase):
    """One page, three hand-installed plugins, one registry label each."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-label-page-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config, self.state = self.tmp / 'config' / 'recursion', self.tmp / 'state'
        plugins_dir = self.config / 'plugins'
        # A digest that is not any installed manifest must not paint a row verified.
        labels = {'0' * 64: 'verified'}
        grants = []
        for directory, plugin_id, panel_id, title, label in _SPECS:
            source = _source(panel_id, title)
            manifest = fx.write_plugin(plugins_dir / directory, source, plugin_id=plugin_id,
                                       permissions=('panel',))
            grants.append(fx.grant_for(source, plugin_id=plugin_id, permissions=('panel',)))
            if label is not None:
                labels[hashlib.sha256(manifest).hexdigest()] = label
        fx.write_install(self.config, grants=grants, registry_labels=labels)
        if backend_available():
            self.host = plugins.PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        else:
            with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                self.host = plugins.PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        self.addCleanup(self.host.close)
        control = adapter.DaemonControl(None, None, None, None, self.state, routes={}, plugins=self.host)
        self.transport = lambda method, path, body: control.gui_transport(method, path, body)

    def page(self, gui, path):
        conn = HTTPConnection(*gui.address, timeout=5)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_three_hand_installed_plugins_show_their_labels_as_text(self):
        def shown_id(record):
            return record['plugin_id'] or Path(record['source']).name

        by_id = {shown_id(record): record for record in self.host.records}
        self.assertEqual(sorted(by_id), ['fixture.nonverified', 'fixture.unlisted', 'fixture.verified'])
        if self.host.verification == 'available':
            for record in by_id.values():
                # The label decides nothing: each plugin loaded, with the same permission.
                self.assertEqual((record['state'], record['permissions'], record['provides']),
                                 ('loaded', ['panel'], ['panels']))
            self.assertEqual(by_id['fixture.verified']['registry_label'], 'verified')
            self.assertEqual(by_id['fixture.nonverified']['registry_label'], 'non_verified')
            self.assertEqual(by_id['fixture.unlisted']['registry_label'], 'unlisted')
        else:
            # No signature backend: every plugin is refused for that, and the label is still shown.
            for record in by_id.values():
                self.assertEqual((record['state'], record['plugin_id'], record['code']),
                                 ('refused', None, 'plugin_verification_unavailable'))
            self.assertEqual(self.host.registries, ())
        rows = {row['id']: row for row in self.host.route_records()}
        for plugin_id, stored in (('fixture.verified', 'verified'), ('fixture.nonverified', 'non_verified'),
                                  ('fixture.unlisted', 'unlisted')):
            blocker = rows['plugin:' + plugin_id]['blocker']
            self.assertTrue(blocker.endswith(MARK + PHRASE[stored]), blocker)
            self.assertEqual(rows['plugin:' + plugin_id]['configured'], self.host.verification == 'available')
        with LoopbackGUI(ControlClient(self.transport), lambda env: True,
                         plugins=self.host.registries, port=0) as gui:
            status, home = self.page(gui, '/')
            self.assertEqual(status, 200)
            style = re.search(r'<style>(.*?)</style>', home)
            self.assertIsNotNone(style)
            self.assertNotIn('registry-label', style.group(1))  # the label is text, not a colour rule
            seen = {}
            for plugin_id, stored in (('fixture.verified', 'verified'), ('fixture.nonverified', 'non_verified'),
                                      ('fixture.unlisted', 'unlisted')):
                row = _row(home, plugin_id)
                self.assertEqual(_label_cell(row), PHRASE[stored])
                self.assertIn(MARK + PHRASE[stored], row)
                seen[plugin_id] = row
            if self.host.registries:
                # The non-verified plugin's own panel says "verified". Opening it does not rewrite the row.
                status, panel = self.page(gui, '/panel?id=panel.nonverified')
                self.assertEqual(status, 200)
                self.assertIn('>verified<', panel)
                self.assertNotIn('class="registry-label"', panel)
                status, again = self.page(gui, '/')
                self.assertEqual(status, 200)
                for plugin_id, stored in (('fixture.verified', 'verified'),
                                          ('fixture.nonverified', 'non_verified'),
                                          ('fixture.unlisted', 'unlisted')):
                    self.assertEqual(_label_cell(_row(again, plugin_id)), PHRASE[stored])
                    self.assertEqual(_row(again, plugin_id), seen[plugin_id])
                # The unlisted plugin's title repeats a verified phrase. Its cell stays unlisted.
                self.assertIn(PHRASE['verified'], again)
                self.assertEqual(_label_cell(_row(again, 'fixture.unlisted')), PHRASE['unlisted'])
            else:
                status, missing = self.page(gui, '/panel?id=panel.nonverified')
                self.assertEqual(status, 400)
                self.assertNotIn('class="registry-label"', missing)


if __name__ == '__main__':
    unittest.main()
