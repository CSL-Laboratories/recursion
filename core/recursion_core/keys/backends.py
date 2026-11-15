"""Where a key's VALUE is kept: the Secret Service (through ``secret-tool``) or the owner-only file.

Each backend has ``get(key_id, folder)``, ``put(key_id, value, folder, keep)`` and
``delete(key_id, folder, keep)``. ``folder`` is the store's open, checked folder (only the owner-only file
uses it); ``keep`` is the set of ids the store's index will hold once the call is done, so the owner-only
file also drops a value whose id the index no longer has. A missing value is ``secret_missing``; any other
failure is a typed ``KeyStoreError``. No error, repr or log line of a backend carries a value.

SECRET SERVICE. ``secret-tool`` is given the value on its standard input (``store``) and gives it back on its
standard output (``lookup``); the item is found by two attributes, ``application recursion`` and
``recursion_key_id <id>``. Neither the value nor anything derived from it is ever in its arguments or its
environment. ``lookup`` exits 1 with nothing on its standard error when there is no such item; anything else
that is not success is a failure, ``keychain_locked`` when its message says the collection is locked or the
unlock prompt was dismissed, else ``keychain_failed``. A failure is never answered by trying the file.

OWNER-ONLY FILE. ``secrets.json`` in the store's folder: ``{"version": 1, "secrets": {id: value}}``, read and
written through ``files.Folder`` (0600, owner and mode checked on every read, temp file, fsync, rename).
"""
from __future__ import annotations

import json

from recursion_core.keys import system as sysmod
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.files import Folder
from recursion_core.keys.validate import decode_value, valid_key_id, valid_value

OWNER_FILE_NAME = 'secrets.json'
OWNER_FILE_MAX = 4 << 20


class SecretService:
    name = 'secret_service'

    def __init__(self, system: sysmod.System) -> None:
        self._system = system

    def __repr__(self) -> str:
        return 'SecretService()'

    def _tool(self) -> str:
        tool = self._system.which('secret-tool')
        if tool is None:
            raise KeyStoreError('keychain_tool_missing', 'secret-tool is not installed (libsecret-tools)')
        return tool

    @staticmethod
    def _attributes(key_id: str) -> list:
        return ['application', 'recursion', 'recursion_key_id', key_id]

    def _run(self, argv: list, *, input: bytes | None = None) -> sysmod.Completed:
        try:
            return self._system.run(argv, input=input, timeout=sysmod.KEYCHAIN_TIMEOUT_S)
        except sysmod.ToolTimeout:
            raise KeyStoreError('keychain_timeout', 'secret-tool did not answer in time') from None
        except sysmod.ToolMissing:
            raise KeyStoreError('keychain_tool_missing', 'secret-tool could not be started') from None

    def get(self, key_id: str, folder: Folder) -> str:
        done = self._run([self._tool(), 'lookup', *self._attributes(key_id)])
        if done.returncode == 0:
            value = decode_value(done.stdout)
            if value is None:
                raise KeyStoreError('value_invalid', f'the Secret Service item for {key_id} is not a key this store saved')
            return value
        if done.returncode == 1 and not done.stderr.strip():
            raise KeyStoreError('secret_missing', f'the Secret Service has no item for {key_id}')
        raise _failure('lookup', done)

    def put(self, key_id: str, value: str, folder: Folder, keep: frozenset) -> None:
        argv = [self._tool(), 'store', f'--label=Recursion API key {key_id}', *self._attributes(key_id)]
        done = self._run(argv, input=value.encode('ascii'))
        if done.returncode != 0:
            raise _failure('store', done, value)

    def delete(self, key_id: str, folder: Folder, keep: frozenset) -> None:
        done = self._run([self._tool(), 'clear', *self._attributes(key_id)])
        if done.returncode != 0:
            raise _failure('clear', done)


def _failure(action: str, done: sysmod.Completed, value: str | None = None) -> KeyStoreError:
    text = done.stderr.decode('ascii', 'replace')
    if value and value in text:      # the tool never echoes its input; if one ever did, say nothing of it
        text = ''
    line = ''.join(ch if ' ' <= ch <= '~' else ' ' for ch in text.strip().splitlines()[0][:200]) if text.strip() else ''
    lowered = line.lower()
    code = 'keychain_locked' if ('locked' in lowered or 'dismissed' in lowered) else 'keychain_failed'
    return KeyStoreError(code, f'secret-tool {action} exited {done.returncode}' + (f': {line}' if line else ''))


class OwnerFile:
    name = 'owner_file'

    def __repr__(self) -> str:
        return 'OwnerFile()'

    def _load(self, folder: Folder) -> dict:
        raw = folder.read(OWNER_FILE_NAME, OWNER_FILE_MAX)
        if raw is None:
            return {}
        secrets = _parse(raw)
        if secrets is None:
            raise KeyStoreError('owner_file_invalid', f'{folder.path / OWNER_FILE_NAME} is not a file this store wrote')
        return secrets

    def _save(self, folder: Folder, secrets: dict) -> None:
        body = json.dumps({'version': 1, 'secrets': dict(sorted(secrets.items()))}, indent=1, ensure_ascii=True)
        folder.write(OWNER_FILE_NAME, (body + '\n').encode('ascii'))

    def get(self, key_id: str, folder: Folder) -> str:
        value = self._load(folder).get(key_id)
        if value is None:
            raise KeyStoreError('secret_missing', f'the owner-only file has no value for {key_id}')
        return value

    def put(self, key_id: str, value: str, folder: Folder, keep: frozenset) -> None:
        secrets = {kid: v for kid, v in self._load(folder).items() if kid in keep}
        secrets[key_id] = value
        self._save(folder, secrets)

    def delete(self, key_id: str, folder: Folder, keep: frozenset) -> None:
        secrets = self._load(folder)
        kept = {kid: v for kid, v in secrets.items() if kid in keep and kid != key_id}
        if kept != secrets:
            self._save(folder, kept)


def _parse(raw: bytes) -> dict | None:
    """The secrets of an owner-only file, or None when it is not exactly what ``_save`` writes.

    Nothing here raises with the file's text attached: a decoding or parsing error is caught and turned into
    None BEFORE any exception is raised, so no traceback, ``__context__`` or exception attribute holds a value.
    """
    try:
        doc = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, _Duplicate):
        doc = None
    if not isinstance(doc, dict) or set(doc) != {'version', 'secrets'} or type(doc['version']) is not int \
            or doc['version'] != 1 or not isinstance(doc['secrets'], dict):
        return None
    out = {}
    for key_id, value in doc['secrets'].items():
        if not valid_key_id(key_id) or not valid_value(value):
            return None
        out[key_id] = value
    return out


class _Duplicate(Exception):
    pass


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate()
        out[key] = value
    return out


def _constant(name):
    raise ValueError('not a JSON number')


BACKENDS = {'secret_service': SecretService, 'owner_file': OwnerFile}
