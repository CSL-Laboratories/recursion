"""The marker test: a made-up key goes through every operation and every refusal, and is then looked for in the
store's folder, the captured log, every error and every repr. It must be found only in the owner-only file
(where it is kept by design) and in what ``credentials()`` returns."""
from __future__ import annotations

import io
import logging
import os

from recursion_core.keys import __main__ as entry
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.tests.support import (
    MARKER,
    TempStoreCase,
    file_system,
    keychain_system,
    needs_posix,
)


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record) -> None:
        self.lines.append(self.format(record))
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


@needs_posix
class MarkerTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.capture = Capture()
        root = logging.getLogger()
        self.old_level = root.level
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self.old_level)
        self.seen = []        # every error, record and object the operations produced

    def attempt(self, call):
        try:
            result = call()
        except BaseException as exc:  # noqa: BLE001 - collected and searched below
            self.seen.extend([exc, exc.__context__, exc.__cause__, getattr(exc, 'args', ())])
            return None
        self.seen.append(result)
        return result

    def exercise(self, system) -> None:
        store = self.store(system)
        self.seen.append(store)
        self.attempt(lambda: self.add(store))
        self.attempt(lambda: self.add(store))                                     # key_exists
        self.attempt(lambda: store.list())
        self.attempt(lambda: store.values()[1])
        self.attempt(lambda: store.credential('codex-key', 'OPENAI_API_KEY'))     # another variable
        self.attempt(lambda: store.credential('nobody', 'CODEX_API_KEY'))
        value = store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(value, MARKER)
        index = self.root / 'index.json'
        index.chmod(0o644)
        self.attempt(lambda: store.credential('codex-key', 'CODEX_API_KEY'))
        index.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        self.attempt(lambda: entry.main(['list'], out=out, err=err, store=store))
        self.attempt(lambda: entry.main(['list', '--json'], out=out, err=err, store=store))
        self.attempt(lambda: entry.main(['audit', '--no-journal'], out=out, err=err, store=store))
        self.attempt(lambda: entry.main(['add', 'codex-key', MARKER, '--provider', 'x', '--env', 'X'], out=out, err=err,
                                        store=store))
        self.seen.extend([out.getvalue(), err.getvalue()])
        self.attempt(lambda: store.remove('codex-key'))
        self.attempt(lambda: store.remove('codex-key'))                          # key_unknown

    def assert_marker_nowhere(self) -> None:
        texts = [repr(item) for item in self.seen] + [str(item) for item in self.seen] + self.capture.lines
        for text in texts:
            self.assertNotIn(MARKER, text)
        for folder, _, files in os.walk(self.root):
            for name in files:
                if name == 'secrets.json':
                    continue
                with open(os.path.join(folder, name), 'rb') as fh:
                    self.assertNotIn(MARKER.encode(), fh.read(), name)

    def test_owner_file_host(self) -> None:
        self.exercise(file_system())
        self.assert_marker_nowhere()
        self.assertTrue(any('the key store refused codex-key: file_invalid' in line for line in self.capture.lines))

    def test_keychain_host_including_a_locked_keychain(self) -> None:
        system, chain = keychain_system()
        self.exercise(system)
        self.add(self.store(system))
        chain.mode = 'locked'
        self.attempt(lambda: self.store(system).credential('codex-key', 'CODEX_API_KEY'))
        self.attempt(lambda: self.store(system).values())
        self.assertFalse((self.root / 'secrets.json').exists())
        self.assert_marker_nowhere()

    def test_an_error_carries_no_value_in_its_text_or_its_chain(self) -> None:
        store = self.store()
        self.add(store)
        (self.root / 'secrets.json').chmod(0o600)
        os.unlink(self.root / 'secrets.json')
        fd = os.open(self.root / 'secrets.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write('{"version": 1, "secrets": {"codex-key": "' + MARKER + '\u00e9"}}')
        for call in (lambda: store.credential('codex-key', 'CODEX_API_KEY'), lambda: store.values(),
                     lambda: store.remove('codex-key')):
            with self.assertRaises((KeyError, KeyStoreError)) as caught:
                call()
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(MARKER, repr(caught.exception) + str(caught.exception))
        self.assertFalse(any(MARKER in line for line in self.capture.lines))
