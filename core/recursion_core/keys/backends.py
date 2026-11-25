"""Where a key's VALUE is kept: the Secret Service (through ``secret-tool``) or the owner-only file.

Each backend is made for one store (its random id, from the index) and has ``get(key_id, folder, record)``,
``put(key_id, value, folder, record=...)``, ``delete(key_id, folder, save_id=None)`` and ``holds(key_id, folder)``.
``folder`` is the store's open, checked folder (only the owner-only file uses it). A ``put`` or a ``delete`` changes
the values of the id it names and of no other id: a value no record of the index names any more (the index lost,
emptied or put back) stays where it is until a removal names its id. ``delete`` given a ``save_id`` (a recovery, or
the rollback of an add) deletes only the value that save stored, and leaves a newer or older save of the id; it
returns True when it found a value to delete, False when there verifiably was none. ``holds`` says whether a value
for the id is
there: True, False only when that is verified, None when it cannot be told. A missing value is
``secret_missing``; any other failure is a typed ``KeyStoreError``. No error, repr or log line of a backend
carries a value.

THE BINDING. A value is stored WITH what authorizes it: the store's id, the save id of the record that saved it
and that record's provider. ``get`` given a record returns the value only when all three match the record and
the store; anything else is ``secret_missing``, on both backends. So no index (an older one put back, one copied
in, one edited) can pair one save's provider and opt-in with another save's value. ``get`` with no record (the
audit's read of an interrupted add, never a release) returns any value of the id; ``delete`` and ``holds`` act on
every value of the id. A stored value with no binding beside it is refused, never guessed at.

SECRET SERVICE. ``secret-tool`` is given the value on its standard input (``store``) and gives it back on its
standard output (``lookup``). An item carries five attributes: ``application recursion``, ``recursion_store
<the store's id>`` (a random id the store's index records at its first save, so every spelling of the store's
folder names one store, and two stores of one user never share an item), ``recursion_key_id <id>``,
``recursion_save <the save's id>`` and ``recursion_provider <the record's provider>``. A value is looked up
with all five, so it is returned only to the record of the save that stored it; ``clear`` and the presence
check use the first three, so a removal reaches every value ever saved for the id in this store. ``count`` asks
``search --all`` how many items carry ``application recursion`` (any store's), this store's items of one id, or
the item of one record's save, for the audit. Neither the value nor anything derived from it is ever in the
helper's arguments or its environment.
WHICH KEYCHAIN. ``identity`` names the keychain that answers now, by the default collection's path and its
``Created`` time, asked of the bus (``system.keychain_identity``); the store records it with each save (see
``store.py``, KEYCHAIN). ``has_item`` says whether an item of an id (of one save) is there, locked or not, by asking
the service over the bus for the items that carry its attributes (``system.keychain_items``): the answer is the
items' paths, never a secret, and no unlock prompt is raised, so ``status``, ``forget`` and the refusals of
``remove`` learn where a key is without reading its value.
``lookup`` exits 1 with nothing on its standard error when it finds no unlocked item; anything else that is not
success is a failure, ``keychain_locked`` when its message says the collection is locked or the unlock prompt
was dismissed, else ``keychain_failed``. A failure's detail is fixed text: no line a helper printed is ever
repeated. A failure is never answered by trying the file.

GONE IS VERIFIED. ``lookup`` and ``clear`` also exit 1 silently for a locked item whose unlock was cancelled
(libsecret's unlock yields no path and no error), so their silence never means "no item". An item is gone only
when ``lookup`` (which unlocks, prompting if it must) finds nothing AND ``search --all``, which lists locked
items too, exits 0 and lists none (``presence``). A removal succeeds only then; an add whose helper failed drops
its intent only then.

OWNER-ONLY FILE. ``secrets.json`` in the store's folder: ``{"version": 1, "store_id": <the store's id>,
"secrets": {id: {"save_id", "provider", "value"}}}``, read and written through ``files.Folder`` (0600, owner and
mode checked on every read, temp file, fsync, rename). Any other shape is refused (``owner_file_invalid``). A
file whose store id is not the index's belongs to another store: it releases nothing to this store's records
(``secret_missing``), holds nothing this store can remove (a removal or recovery succeeds without touching it),
and is not written over while this store has records (``owner_file_invalid``); the audit searches for its values
as not the store's own. A store whose backend is this file and that has no record takes the file's id as its own
at its next save, and with it every value the file holds (``KeyStore.add``); a removal takes no id, and a store on
a keychain never takes it.
"""
from __future__ import annotations

import json
import re

from recursion_core.keys import system as sysmod
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.files import Folder
from recursion_core.keys.validate import decode_value, valid_key_id, valid_provider, valid_value

OWNER_FILE_NAME = 'secrets.json'
OWNER_FILE_MAX = 4 << 20


class SecretService:
    name = 'secret_service'

    def __init__(self, system: sysmod.System, store_id: str) -> None:
        self._system = system
        self._store_id = store_id

    def __repr__(self) -> str:
        return 'SecretService()'

    def _tool(self) -> str:
        tool = self._system.which('secret-tool')
        if tool is None:
            raise KeyStoreError('keychain_tool_missing', 'secret-tool is not installed (libsecret-tools)')
        return tool

    def _attributes(self, key_id: str) -> list:
        """Every item saved for ``key_id`` in this store, whichever save stored it."""
        return ['application', 'recursion', 'recursion_store', self._store_id, 'recursion_key_id', key_id]

    def _saved(self, key_id: str, save_id: str | None) -> list:
        """The item one save stored, whatever its provider; with no save id, every item of the id."""
        return self._attributes(key_id) + (['recursion_save', save_id] if save_id else [])

    def _item(self, key_id: str, record) -> list:
        """The item a record saved: its store, id, save and provider. With no record, any item of the id."""
        if record is None:
            return self._attributes(key_id)
        return self._attributes(key_id) + ['recursion_save', record.save_id, 'recursion_provider', record.provider]

    def identity(self) -> tuple | None:
        """Which keychain answers now (``system.keychain_identity``): asks the bus, never ``secret-tool``."""
        return sysmod.keychain_identity(self._system)

    def _run(self, argv: list, *, input: bytes | None = None) -> sysmod.Completed:
        code = None
        try:
            return self._system.run(argv, input=input, timeout=sysmod.KEYCHAIN_TIMEOUT_S)
        except sysmod.ToolTimeout:
            code, detail = 'keychain_timeout', 'secret-tool did not answer in time'
        except sysmod.ToolMissing:
            code, detail = 'keychain_tool_missing', 'secret-tool could not be started'
        raise KeyStoreError(code, detail)       # outside the handlers: no __context__

    @staticmethod
    def _absent(done: sysmod.Completed) -> bool:
        """``lookup`` and ``clear`` exit 1 with nothing on standard error when they found no unlocked item."""
        return done.returncode == 1 and not done.stderr.strip()

    def get(self, key_id: str, folder: Folder, record=None) -> str:
        done = self._run([self._tool(), 'lookup', *self._item(key_id, record)])
        if done.returncode == 0:
            value = decode_value(done.stdout)
            if value is None:
                raise KeyStoreError('value_invalid', f'the Secret Service item for {key_id} is not a key this store saved')
            return value
        if self._absent(done):
            raise KeyStoreError('secret_missing', f'the Secret Service has no item for {key_id}')
        raise _failure('lookup', done)

    def put(self, key_id: str, value: str, folder: Folder, *, record) -> None:
        argv = [self._tool(), 'store', f'--label=Recursion API key {key_id}', *self._item(key_id, record)]
        done = self._run(argv, input=value.encode('ascii'))
        if done.returncode != 0:
            raise _failure('store', done)

    def presence(self, key_id: str, save_id: str | None = None) -> bool:
        """True when an item of ``key_id`` (of that save, when ``save_id`` is given) is there, False when it verifiably
        is not (``lookup`` finds none and ``search --all`` lists none); a helper that fails, and so cannot tell, raises
        its typed refusal."""
        words = self._saved(key_id, save_id)
        look = self._run([self._tool(), 'lookup', *words])
        if look.returncode == 0:
            return True
        if not self._absent(look):
            raise _failure('lookup', look)
        return self._listed(words) > 0

    def has_item(self, key_id: str, save_id: str | None = None) -> bool:
        """True when an item of ``key_id`` (of that save, when ``save_id`` is given) is in the Secret Service, in any
        collection, locked or not; False when there is none. Asked over the session bus by the item's attributes
        (``system.keychain_items``): only item paths come back, never a secret, and no unlock prompt is raised. A
        service that cannot be asked so raises ``keychain_failed``: nothing is read in place of it."""
        found = sysmod.keychain_items(self._system, self._saved(key_id, save_id))
        if found is None:
            raise KeyStoreError('keychain_failed', 'the Secret Service could not be asked over the session bus which '
                                'items it holds (busctl or dbus-send did not answer)')
        return found > 0

    def count(self, key_id: str | None = None, record=None) -> int:
        """How many items ``search --all`` lists, locked ones too: with ``key_id`` and a record, the item that record's
        save stored; with ``key_id`` alone, this store's items of that id, whichever save stored them; without, every
        item that carries ``application recursion``, whichever store saved it (for the audit's count of the items no
        record names). A helper that fails raises its typed refusal.

        ``search`` prints the secrets of the unlocked items it lists: its output is read for item lines only
        (``[<id>]``, one per item) and dropped.
        """
        return self._listed(self._item(key_id, record) if key_id is not None else ['application', 'recursion'])

    def _listed(self, words: list) -> int:
        found = self._run([self._tool(), 'search', '--all', *words])
        if found.returncode != 0:
            raise _failure('search', found)
        items = sum(1 for line in found.stdout.split(b'\n') if line.startswith(b'[') and line.endswith(b']'))
        found = None
        return items

    def holds(self, key_id: str, folder: Folder) -> bool | None:
        try:
            return self.presence(key_id)
        except Exception:  # noqa: BLE001 - a probe that fails cannot tell
            return None

    def delete(self, key_id: str, folder: Folder, save_id: str | None = None) -> bool:
        """Remove every item of ``key_id`` in this store (only the one ``save_id`` stored, when it is given); succeed
        only when none is verifiably left. True when an item was there to remove, False when none ever was.

        ``clear`` removes the unlocked ones. Then ``presence`` decides: gone is done; still there (a locked item,
        which presence's ``lookup`` may just have unlocked) is cleared once more and asked again; a helper that
        cannot tell, or an item still there after the second try, is refused, and the caller keeps its record.
        """
        words = self._saved(key_id, save_id)
        for attempt in range(2):
            done = self._run([self._tool(), 'clear', *words])
            if done.returncode != 0 and not self._absent(done):
                raise _failure('clear', done)
            if self.presence(key_id, save_id) is False:
                return attempt > 0 or done.returncode == 0
        raise KeyStoreError('keychain_locked', f'the Secret Service still holds {key_id} (in a locked collection?); it '
                            'stays on record')


def _failure(action: str, done: sysmod.Completed) -> KeyStoreError:
    """A helper's failure as a typed refusal. The detail is fixed text: nothing a helper printed is repeated."""
    lowered = done.stderr.decode('ascii', 'replace').lower()
    if 'locked' in lowered:
        code, why = 'keychain_locked', ': the collection is locked'
    elif 'dismissed' in lowered:
        code, why = 'keychain_locked', ': the unlock prompt was dismissed'
    else:
        code, why = 'keychain_failed', ''
    return KeyStoreError(code, f'secret-tool {action} exited {done.returncode}{why}')


class OwnerFile:
    name = 'owner_file'

    def __init__(self, store_id: str | None) -> None:
        self._store_id = store_id

    def __repr__(self) -> str:
        return 'OwnerFile()'

    def _read(self, folder: Folder) -> tuple:
        """``(store id or None when there is no file, {id: (save_id, provider, value)})``; a malformed file is
        refused."""
        raw = folder.read(OWNER_FILE_NAME, OWNER_FILE_MAX)
        if raw is None:
            return None, {}
        parsed = _parse(raw)
        if parsed is None:
            raise KeyStoreError('owner_file_invalid', f'{folder.path / OWNER_FILE_NAME} is not a file this store wrote')
        return parsed

    def _load(self, folder: Folder) -> dict:
        """This store's entries: none when the file belongs to another store."""
        store_id, entries = self._read(folder)
        return entries if store_id in (None, self._store_id) else {}

    def store_of(self, folder: Folder) -> str | None:
        """The store id the file names, or None when there is no file."""
        return self._read(folder)[0]

    def ids(self, folder: Folder) -> frozenset:
        """The ids of this store's values in the file, whether or not a record names them (none when the file is
        another store's); a malformed file is refused."""
        return frozenset(self._load(folder))

    def _save(self, folder: Folder, entries: dict) -> None:
        """Write the file, refusing ``store_full`` BEFORE writing when it would be larger than a read accepts."""
        secrets = {kid: {'save_id': save_id, 'provider': provider, 'value': value}
                   for kid, (save_id, provider, value) in sorted(entries.items())}
        body = json.dumps({'version': 1, 'store_id': self._store_id, 'secrets': secrets}, indent=1, ensure_ascii=True)
        data = (body + '\n').encode('ascii')
        body = secrets = None
        if len(data) > OWNER_FILE_MAX:
            raise KeyStoreError('store_full', f'the owner-only file would be larger than {OWNER_FILE_MAX} bytes; '
                                'remove a key first')
        folder.write(OWNER_FILE_NAME, data)

    def get(self, key_id: str, folder: Folder, record=None) -> str:
        entry = (self._load(folder) if record is not None else self._read(folder)[1]).get(key_id)
        if entry is None:
            raise KeyStoreError('secret_missing', f'the owner-only file has no value for {key_id}')
        save_id, provider, value = entry
        if record is not None and (save_id != record.save_id or provider != record.provider):
            raise KeyStoreError('secret_missing', f'the owner-only file holds no value saved by this record of {key_id}')
        return value

    def values(self, folder: Folder) -> dict:
        """For the audit only: every value the file holds, by id, whatever store or record it names."""
        return {kid: value for kid, (_, _, value) in self._read(folder)[1].items()}

    def holds(self, key_id: str, folder: Folder) -> bool | None:
        try:
            return key_id in self._load(folder)
        except Exception:  # noqa: BLE001 - a file that cannot be read cannot tell
            return None

    def put(self, key_id: str, value: str, folder: Folder, *, record) -> None:
        store_id, entries = self._read(folder)
        if store_id not in (None, self._store_id):
            raise KeyStoreError('owner_file_invalid', f'{folder.path / OWNER_FILE_NAME} belongs to another store; '
                                'remove this index\'s keys, or put its own index back, before saving here')
        entries[key_id] = (record.save_id, record.provider, value)     # this id's entry only: every other value stays
        self._save(folder, entries)

    def delete(self, key_id: str, folder: Folder, save_id: str | None = None) -> bool:
        entries = self._load(folder)     # none when the file is another store's: nothing of this store to remove
        if key_id not in entries or (save_id is not None and entries[key_id][0] != save_id):
            return False                 # nothing of the id (or of that save) is here: a newer or older save stays
        del entries[key_id]              # this id's entry only: every other value stays
        self._save(folder, entries)
        return True


_RANDOM_ID = re.compile(r'[0-9a-f]{32}')
_ENTRY_FIELDS = {'save_id', 'provider', 'value'}


def _random_id(text: object) -> bool:
    return isinstance(text, str) and text.isascii() and _RANDOM_ID.fullmatch(text) is not None


def _parse(raw: bytes) -> tuple | None:
    """``(store_id, {id: (save_id, provider, value)})`` of an owner-only file, or None when it is not exactly what
    ``_save`` writes: a value with no save id, provider and store id beside it is refused, never guessed at.

    Nothing here raises with the file's text attached: a decoding or parsing error is caught and turned into
    None BEFORE any exception is raised, so no traceback, ``__context__`` or exception attribute holds a value.
    """
    try:
        doc = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, _Duplicate):
        doc = None
    if not isinstance(doc, dict) or set(doc) != {'version', 'store_id', 'secrets'} or type(doc['version']) is not int \
            or doc['version'] != 1 or not _random_id(doc['store_id']) or not isinstance(doc['secrets'], dict):
        return None
    out = {}
    for key_id, entry in doc['secrets'].items():
        if not valid_key_id(key_id) or not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            return None
        if not (_random_id(entry['save_id']) and valid_provider(entry['provider']) and valid_value(entry['value'])):
            return None
        out[key_id] = (entry['save_id'], entry['provider'], entry['value'])
    return doc['store_id'], out


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
