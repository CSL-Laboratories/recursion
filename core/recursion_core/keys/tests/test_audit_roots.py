"""``key audit`` never says "nothing found" of a root it did not look in.

A root the audit searches (the state folder, Recursion's config folder, a ``--path``) that is there and is neither a
regular file nor a folder is named unreadable and not read: nothing follows a link, and the audit is incomplete (exit
4). A ``--path`` that is not there is named the same way. Every key here is made up; every path is in the test's own
temp folder.
"""
from __future__ import annotations

import io
import json
import os

from recursion_core.keys import __main__ as entry
from recursion_core.keys.tests.support import MARKER, FakeSystem, TempStoreCase, needs_posix


@needs_posix
class AuditRootTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.base / 'state'
        self.system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(self.state)})
        self.real = self.base / 'real'
        self.real.mkdir()

    def audit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(['audit', '--no-journal', *argv], out=out, err=err, store=self.store(self.system))
        return status, out.getvalue()

    def assert_named_not_read(self, status: int, out: str, place) -> None:
        self.assertEqual(status, 4, out)
        self.assertIn(f'unreadable: {place}\n', out)
        self.assertNotIn('FOUND', out)
        self.assertNotIn(MARKER, out)

    def test_a_state_folder_that_is_a_link_is_named_and_not_followed(self) -> None:
        self.add(self.store(self.system))
        (self.real / 'run.log').write_text('key ' + MARKER)
        self.state.mkdir()
        link = self.state / 'recursion'
        os.symlink(self.real, link)
        status, out = self.audit()
        self.assert_named_not_read(status, out, link)

    def test_a_config_folder_that_is_a_link_is_named_and_not_followed(self) -> None:
        link = self.root.parent                       # Recursion's config folder; the store's folder is inside it
        link.parent.mkdir(parents=True)
        os.symlink(self.real, link)
        self.add(self.store(self.system))
        (self.real / 'routes.json').write_text(json.dumps({'detail': MARKER}))
        status, out = self.audit()
        self.assert_named_not_read(status, out, link)

    def test_a_path_that_is_a_link_is_named_and_not_followed(self) -> None:
        self.add(self.store(self.system))
        (self.real / 'run.log').write_text('key ' + MARKER)
        link = self.base / 'logs'
        os.symlink(self.real / 'run.log', link)
        status, out = self.audit('--path', str(link))
        self.assert_named_not_read(status, out, link)

    def test_a_path_that_is_a_fifo_is_named_and_not_read(self) -> None:
        self.add(self.store(self.system))
        pipe = self.base / 'pipe'
        os.mkfifo(pipe)
        status, out = self.audit('--path', str(pipe))
        self.assert_named_not_read(status, out, pipe)

    def test_a_path_that_is_not_there_is_named(self) -> None:
        self.add(self.store(self.system))
        gone = self.base / 'no-such-folder'
        status, out = self.audit('--path', str(gone))
        self.assert_named_not_read(status, out, gone)
