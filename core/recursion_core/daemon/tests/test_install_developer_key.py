"""read_install takes developer_key_id as null or a key id, and refuses any other shape.

The member is a grant field the schema publishes. A file that names it in that grammar is read.
A trailing newline, a boolean, a list, an empty string, or any other text is a grant value the
reader refuses. The id is checked here and is not a second binding rule.
"""
import os
import shutil
import tempfile
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests.plugin_fixture import FAKE_DEVELOPER_KEY, write_install
from recursion_core.plugin_security.signing import key_id

KEY_ID = key_id(FAKE_DEVELOPER_KEY)


class ReadInstallDeveloperKeyTest(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.mkdtemp(prefix='rc-install-key-')
        self.addCleanup(shutil.rmtree, self.config, True)

    def write(self, developer_key_id='omit'):
        grant = {
            'plugin_id': 'paper-desk',
            'artifact_sha256': 'ab' * 32,
            'permissions': ['read'],
            'signer_key_id': None,
        }
        if developer_key_id != 'omit':
            grant['developer_key_id'] = developer_key_id
        return write_install(self.config, grants=[grant])

    def _refuses_when_nofollow_is_missing(self, folder):
        """Where ``os.O_NOFOLLOW`` is missing, ``read_install`` refuses before any plugin file is opened.

        Returns True when that refusal was asserted. A host that has the flag returns False, and the
        caller keeps its POSIX assertions. The return is an assertion, not a skip.
        """
        if os.name != 'nt' and hasattr(os, 'O_NOFOLLOW'):
            return False
        opened = []
        real_open = os.open

        def trap(path, flags, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, flags, *args, **kwargs)

        os.open = trap
        try:
            with self.assertRaises(plugins.PluginRefused) as caught:
                plugins.read_install(folder)
        finally:
            os.open = real_open
        self.assertEqual(caught.exception.code, 'platform_unsupported')
        self.assertEqual(opened, [])
        return True

    def test_a_missing_nofollow_refuses_before_any_plugin_file_is_read(self):
        folder = self.write(None)
        if self._refuses_when_nofollow_is_missing(folder):
            return
        # POSIX: the no-follow guard stays. A symlink is not the install that is read.
        target_dir = folder.parent / 'elsewhere'
        target_dir.mkdir(mode=0o700)
        os.chmod(target_dir, 0o700)
        target = target_dir / 'install.json'
        target.write_text(
            '{"trusted_keys": {}, "grants": [{"plugin_id": "followed", "artifact_sha256": "'
            + ('cd' * 32) + '", "permissions": ["read"], "signer_key_id": null}]}')
        os.chmod(target, 0o600)
        link = folder / 'install.json'
        link.unlink()
        os.symlink(target, link)
        with self.assertRaises(plugins.PluginRefused) as caught:
            plugins.read_install(folder)
        self.assertEqual(caught.exception.code, 'plugin_file_unreadable')

    def test_null_and_a_key_id_are_read_and_a_second_read_agrees(self):
        for value in (None, KEY_ID):
            with self.subTest(value=value):
                folder = self.write(value)
                if self._refuses_when_nofollow_is_missing(folder):
                    continue
                first = plugins.read_install(folder)
                second = plugins.read_install(folder)
                self.assertEqual(set(first['grants']), {'paper-desk'})
                self.assertEqual(set(second['grants']), {'paper-desk'})
                self.assertEqual(first['grants']['paper-desk'].plugin_id, 'paper-desk')

    def test_a_grant_that_leaves_the_member_out_is_read(self):
        folder = self.write('omit')
        if self._refuses_when_nofollow_is_missing(folder):
            return
        got = plugins.read_install(folder)
        self.assertIn('paper-desk', got['grants'])

    def test_a_hostile_developer_key_id_is_refused_as_a_grant_value(self):
        """The key is known. The shape is not a key id, so the refusal is the value, not the field list."""
        hostile = (KEY_ID + '\n', KEY_ID + '\r', True, [], '', 'not-a-key', 'ab' * 32)
        for value in hostile:
            with self.subTest(value=value):
                folder = self.write(value)
                if self._refuses_when_nofollow_is_missing(folder):
                    continue
                with self.assertRaises(plugins.PluginRefused) as caught:
                    plugins.read_install(folder)
                self.assertEqual((caught.exception.code, caught.exception.detail),
                                 ('plugin_install_config_invalid', 'grant value'))

    def test_an_unknown_grant_field_is_still_refused_as_grant_fields(self):
        folder = write_install(self.config, grants=[{
            'plugin_id': 'paper-desk', 'artifact_sha256': 'ab' * 32, 'permissions': ['read'], 'other': 1,
        }])
        if self._refuses_when_nofollow_is_missing(folder):
            return
        with self.assertRaises(plugins.PluginRefused) as caught:
            plugins.read_install(folder)
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ('plugin_install_config_invalid', 'grant fields'))

    def test_a_file_that_is_not_json_is_still_refused(self):
        folder = self.write(None)
        path = folder / 'install.json'
        path.write_text('{')
        os.chmod(path, 0o600)
        if self._refuses_when_nofollow_is_missing(folder):
            return
        with self.assertRaises(plugins.PluginRefused) as caught:
            plugins.read_install(folder)
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ('plugin_install_config_invalid', 'not JSON'))
