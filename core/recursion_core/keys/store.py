"""The key store: user-entered model-provider API keys, and the credential source for API-key routes.

What the contract asks (CONTRACT 256-267, 275, 1048-1054): Recursion keeps user-entered API keys in local
secure storage, in the OS keychain where there is one and in an owner-only file on a host with none; they
are never logged, never put in a prompt and never sent to CSL; engine sign-ins stay in each engine's own
store; any vendor's key can be kept.

TWO PLACES. The INDEX (``index.json``) holds what is safe to show: per key id, the provider, the variable name
the engine reads it from, when it was saved, which backend holds it, and its spend opt-in and cap; and the
operations that were started on a backend and not finished. It never holds a value. The VALUE is in the
BACKEND: the Secret Service, or the owner-only file (``backends.py``). Both files live in one owner-only folder,
``$XDG_CONFIG_HOME/recursion/keys`` (``~/.config/recursion/keys``), with the checks of ``files.py``. Nothing here
is in the event database: no key state changes with an event, and no secret sits beside the event log.

IDENTITY. Nothing about a store depends on how its folder is spelled. The store's id, which names its items in a
keychain all of the user's stores share, is random and is recorded in the index at the first save; every
spelling that reaches the folder (``..``, a symlinked parent, a trailing or doubled separator, a bind mount,
another letter case on a file system that ignores case) opens the same index, so it reads the same id, takes the
same lock and reaches the same values. A relative folder is refused, so the working directory never matters.
THE BINDING. Each save also gets a random ``save_id``, recorded with its key. The backend stores the value WITH
the store's id, that save id and the record's provider (a Secret Service item's attributes; the owner-only file's
entry and its header), and ``credential`` asks for the value of exactly that record: a value is released only
when all three match the record being used and the store. So an older index put back, or an edited record, gets
``secret_missing``, never another save's value. An index copied in from another store's folder carries that
store's id: on a keychain it reaches that store's own values through that store's own records, and on the
owner-only file it reaches nothing (the file names another store). A copy of the whole folder carries the store's
id too: on a keychain both indexes name one set of items, so a removal through either deletes the value the other's
record names (questions.md K18); on the owner-only file the copy has a file of its own, and the two part at their
first write. A removal deletes every value saved for the id in the store, whichever save stored it; a recovery or a
rollback deletes only the value of the save its intent was written for (ORDER OF WRITES).

THE INDEX FORMAT, version 1: ``{"version": 1, "store_id": <32 hex>, "backend": <store or null>, "keys": {<id>:
{"provider", "env_name", "created_at", "store", "metered_spend_allowed", "cap_usd", "save_id"[, "keychain"]}},
"pending": {<id>: {"op": "add" | "remove", "started_at", "save_id"}}[, "forgotten": [{"key_id", "at", "store"[,
"keychain"]}, ...]]}``, at most 1 MiB. ``keychain`` and ``forgotten`` are written only when there is one (KEYCHAIN). Anything else is refused
``index_invalid``. An intent's ``save_id`` names the save it was written for: the new record's for an add, the
record's for a remove. An index written before intents carried it (an intent with only ``op`` and ``started_at``) is
read as it is; see ORDER OF WRITES for what a recovery does with such an intent. A listing says what the index holds;
it does not say that a keychain is reachable or unlocked now.

PINNING. The first save records the backend the host offered then (``system.detect``) in the index. Every later
operation that touches a value asks again and refuses when the answer differs: ``keychain_unavailable`` when a keychain
was pinned and the host now offers only the file, ``backend_changed`` otherwise. A locked or failing keychain is refused
with its own code and is never answered from the file. A key pinned to a keychain is removed only where that keychain
answers, since its removal must reach the value. One exception: a key pinned to the owner-only file is REMOVED whatever
the host offers now, the file being in the store's own folder, so that once a keychain appears the keys can be removed
and added again into it; a file in its place that the store did not write (a folder, a symlink, a FIFO, another shape)
is refused, as every command that reads the file refuses it (``list`` reads only the index). The pin is released when
nothing is left in the index.

SPEND. Every key has ``metered_spend_allowed`` and ``cap_usd``, typed when it is added; with no opt-in the cap
is 0. ``credentials()`` hands a key out only when both say spend is allowed (opt-in and a cap above 0):
"Zero-dollar reservation" (grill Q44), and the brief's default (a) for spend before lane QUOTA lands, which
is Lucas's to confirm. QUOTA enforces the cap; until it does, the console's own spend limit is the stop.

THE CREDENTIAL SOURCE. ``credentials(connection_id, env_name)`` is ``routes.base.Credentials``: the value
only when the id is known AND its saved variable name is ``env_name`` AND its provider is one of the published
vendors (``validate.PROVIDERS``): a key saved for any other vendor is kept and listed and handed to no engine
(``provider_unrouted``), because no route or grant names a provider yet. An unknown id, or a known one asked for
under another name, raises ``KeyError(connection_id)``, exactly as seam S7's default does; a known id the store
refuses, or an id whose add or removal is unfinished, raises ``KeyRefused`` (a ``KeyError`` with a code). It
never returns None or an empty string. It is the ONE place a value leaves the store for a route: ``values()``
and a backend's ``get`` apply none of these checks and must never feed an environment. The
index is read and the value fetched under the folder's SHARED lock, which every writer takes exclusively, so
the authorization and the value come from one state of the store: a key removed and added again meanwhile is
never handed out under the old one's variable name or opt-in. Lane KEYS' ``hooks.py`` registers it into S7 at
composition (SPINE's seams); until the daemon calls the composed hooks, which master's does not yet, a test
passes it where the daemon passes ``no_custody``.

ORDER OF WRITES, and what a failure leaves. Every write holds the folder's exclusive lock. An add first writes
the index with a PENDING entry for the id (the intent, before anything outside the folder happens), then saves
the value in the backend, then writes the index with the key and without the intent. A remove does the same
with a pending ``remove``. When saving the value fails, the backend is asked whether it holds a value for the id
(``holds``): only a verified "no" drops the intent again. A helper that was killed after it saved, one that ran
out of time, or a keychain that cannot say, leaves the intent in the index, as does a failure after the value
was saved: ``list`` shows it, ``credential`` refuses that id (``operation_pending``), the next add or remove
recovers it (deletes the orphaned value, or finishes the removal), and ``remove <id>`` clears it on request. Both
index states are checked against the size limit before anything is changed (``store_full``). Every write first
deletes the temp files an interrupted write left in the folder (``Folder.sweep``).
A recovery, and the rollback of an add whose last index write failed, delete only the value of the save the intent
was written for (the intent's ``save_id``), never a newer or an older save of the same id: an older index put back
can carry an intent whose id has been saved again since. An intent written before intents carried a save id takes
its record's save id when a record names the id; with no record it is finished only when the backend verifiably
holds no value of the id, and otherwise stays (the id is refused ``operation_pending``) until ``remove <id>``, which
deletes every value of the id on the person's request, clears it: which save it was for cannot be told, and a value
the person did not ask to delete is never guessed away.

VALUES NO RECORD NAMES. An index lost, emptied or put back no longer names every value the store holds. A save, a
removal and a recovery change the values of the id they name and of no other, so such a value is never deleted by an
unrelated write. The store's OWN owner-only file (``_own_file``) is the one in its folder, except while its index pins
the store to a keychain (then it has none) or has records on the file (then only a file that names the index's store
id). With no record, the file in the folder is the store's own whichever store it names, whatever the host offers, even
beside an index that kept its id: the same folder is the same store (a save into the file takes its id, below). A value
in its own file that no record reaches stays there: the audit searches for it (``UNBOUND``) and names its id; ``remove
<id>``, for an id no record or intent names, deletes it whatever the host offers now, in one write of the file and with
no intent (nothing outside the folder changes), and changes nothing else, the index's store id included; and ``add
<id>`` on the file is refused (``key_exists``) until it is removed, so no save replaces a value it was not given. A file
that is not the store's own (one naming another store while the index has records on the file, or any while the index
pins the store to a keychain) is never changed by it: the audit searches for the values of one it can read (``FOREIGN``)
and says the file is not the store's, and names one it cannot read as unreadable (the audit is then incomplete); its
values go only through their own store or with the file. A file of the store's own that it cannot read refuses the
audit, as every command that reads the file refuses it (``list`` reads only the index, and a save into a keychain never
reads the file): a value with no binding beside it is refused, never guessed at. With no record that is the folder's
file on any host; a save into the keychain (the file is then one the store does not use) or moving the file away ends
it. A store whose backend is the owner-only file and that has no record takes the file's store id at its next save (the
same folder is the same store). A store on a keychain never takes it, and never reads the owner-only file to save or
release a key, nor to remove one while its index pins it there (only the audit reads it, for values to look for). On a
keychain an item is named by the store's id, which only the index records: an index LOST leaves the store's items with
no name this store knows, so it can neither release nor remove them (the keychain's own tool can: they carry
``application recursion``). ``remove <id>`` of an id a record names deletes every item of the id in the store, whichever
save stored it. The audit counts every Recursion item in the keychain that neither a record names by its save nor an
unfinished operation by its id (``unnamed_items``), and is incomplete while there is one.

KEYCHAIN. One config folder can meet more than one keychain (another session bus, a container or an ssh session with
a keyring of its own, a keyring wiped and made again), and the pin names a backend, not a keychain. So each record of
a Secret Service save names the keychain its value was saved into: ``keychain``, ``{"collection": <the default
collection's object path>, "created": <its Created time, seconds>}``, asked of the bus at the save
(``system.keychain_identity``); a record has no ``keychain`` when the service did not say (a ``Created`` of 0 says
nothing, so two such keychains are never taken for one), and the owner-only file needs none: it is the one in the
store's folder. ``remove`` of a record first asks where its value is (``_place``), before anything is changed or
recovered. The identifier is asked first: the keychain that answers is the SAME one when its path and ``Created``
are the record's. When it is not (or the record or the keychain names none), the record's OWN save is looked for in
the keychain that answers (``presence`` with the record's save id: a lookup and a listing, nothing changed): a save
id is random per save, so only the keychain that holds that save can show it, and that keychain is taken for the
one it was saved into whatever its identifier says. No other item of the id counts. Then:
- the SAME keychain, or one holding the record's own save: the items of the id are deleted; when none was left the
  record is removed all the same, and the removal says so (``report`` gets ``already_gone``);
- ANOTHER keychain (another collection, or one made before the save) without the save: refused ``keychain_other``,
  nothing changed, and the refusal names the keychain the key was saved into (the last part of its path and the
  day it was made: neither is secret) and that ``remove`` deletes it where that keychain answers;
- one with the record's collection path made AFTER the save (wiped and made again, or another one made since)
  without the save: refused ``keychain_remade``, nothing changed, and the refusal names ``forget``;
- a record that names no keychain (saved before records named one), or a keychain that does not say which it is,
  without the save: refused ``keychain_unrecorded``, nothing changed, naming ``forget``.
Two keychains at one path that say the same ``Created`` (made in the same second) are still taken for one: with
the item gone from the one that answers, the record is removed and the removal says the item was already gone.
``forget <id>`` is the person's own step for ONE named key whose value no command can reach from here: it removes
that key's record (and any unfinished operation on it) from the index, deletes nothing, and records its use in the
index (``forgotten``: the id, when, the store and the keychain the record named). It refuses ``value_reachable``,
changing nothing, while ``remove`` can delete the key where it is: a record of the owner-only file, or one whose
keychain answers or holds its own save (asked as ``remove`` asks); with no keychain of its kind answering here it
asks nothing. So once it has run, the value may still sit only in another keychain. No hook or seam reaches it
(``hooks.py`` registers the credential source only), and it reads no setting: the command asks for the id on the
terminal. ``status`` and the audit show each record whose value is not where ``remove`` can reach it (asked the
same way), and every use of ``forget``.
"""
from __future__ import annotations

import calendar
import contextlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from recursion_core.keys import validate as v
from recursion_core.keys.backends import OwnerFile, SecretService
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.files import Folder, open_folder
from recursion_core.keys.system import System, _object_path, detect

log = logging.getLogger(__name__)

STORES = ('secret_service', 'macos_keychain', 'windows_credential', 'owner_file')   # key.list's enum
STORE_TEXT = {
    'secret_service': 'the Secret Service (the desktop keychain)',
    'macos_keychain': 'the macOS Keychain',
    'windows_credential': 'the Windows Credential Manager',
    'owner_file': 'an owner-only file',
}
INDEX_NAME = 'index.json'
INDEX_MAX = 1 << 20
INDEX_VERSION = 1
FIELDS = ('provider', 'env_name', 'created_at', 'store', 'metered_spend_allowed', 'cap_usd', 'save_id')
KEYCHAIN_FIELD = 'keychain'        # a record's keychain, written only when there is one (KEYCHAIN)
FORGOTTEN_FIELDS = ('key_id', 'at', 'store')
# Where a record's keychain is, against the one that answers now (KEYCHAIN):
WHERE = ('same', 'other', 'remade', 'unrecorded', 'unknown')
SHOWN = ('key_id', 'provider', 'env_name', 'created_at', 'store', 'metered_spend_allowed', 'cap_usd')
PENDING_FIELDS = ('op', 'started_at')
PENDING_FIELDS_SAVED = ('op', 'started_at', 'save_id')     # an intent that names its save (the older shape is read too)
OPS = ('add', 'remove')
RANDOM_ID = re.compile(r'[0-9a-f]{32}')
UNBOUND = '(a value in the owner-only file that no record of this index reaches)'
FOREIGN = '(a value in an owner-only file this store does not use)'


def _random_id() -> str:
    return secrets.token_hex(16)


def _valid_random_id(text: object) -> bool:
    return isinstance(text, str) and text.isascii() and RANDOM_ID.fullmatch(text) is not None


@dataclass(frozen=True)
class KeyRecord:
    """What the index keeps for one key. No value. ``save_id`` names the one save whose value it may be given."""

    key_id: str
    provider: str
    env_name: str
    created_at: str
    store: str
    metered_spend_allowed: bool
    cap_usd: int | float
    save_id: str
    keychain: tuple | None = None      # (collection path, created) of the keychain it was saved into; see KEYCHAIN

    def spend_allowed(self) -> bool:
        return self.metered_spend_allowed is True and self.cap_usd > 0

    def as_json(self) -> dict:
        """What ``list --json`` shows: the record without its save id, which is the store's own business."""
        return {name: getattr(self, name) for name in SHOWN}


@dataclass(frozen=True)
class Pending:
    """An operation started on the backend and not finished: ``op`` is ``add`` or ``remove``. ``save_id`` names the
    save it was written for; None in an intent written before intents carried it."""

    key_id: str
    op: str
    started_at: str
    save_id: str | None = None


@dataclass(frozen=True)
class Forgotten:
    """One use of ``forget``: which key's record was removed, when, from which store, and the keychain it named."""

    key_id: str
    at: str
    store: str | None
    keychain: tuple | None = None


@dataclass(frozen=True)
class Index:
    backend: str | None                            # the pinned backend; None while nothing is in the index
    keys: dict                                     # key id -> KeyRecord
    pending: dict = field(default_factory=dict)    # key id -> Pending
    store_id: str | None = None                    # the store's random id; None only before the first save
    forgotten: tuple = ()                          # every use of ``forget`` (Forgotten), oldest first

    def live(self) -> bool:
        return bool(self.keys or self.pending)


def default_root(system: System | None = None) -> Path:
    """``$XDG_CONFIG_HOME/recursion/keys``, or ``~/.config/recursion/keys`` (a relative value is ignored)."""
    system = system or System()
    base = system.environ.get('XDG_CONFIG_HOME')
    if not base or not os.path.isabs(base):
        base = str(system.home() / '.config')
    return Path(base) / 'recursion' / 'keys'


def _now(clock) -> str:
    moment = clock()
    whole = int(moment)
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(whole)) + f'.{int((moment - whole) * 1000):03d}Z'


def index_bytes(index: Index) -> bytes:
    """The index file exactly as it is written. Every write carries the store's id (``add`` makes it first)."""
    keys = {kid: {**{name: getattr(record, name) for name in FIELDS}, **_keychain_json(record.keychain)}
            for kid, record in sorted(index.keys.items())}
    pending = {kid: {'op': p.op, 'started_at': p.started_at, **({'save_id': p.save_id} if p.save_id else {})}
               for kid, p in sorted(index.pending.items())}
    doc = {'version': INDEX_VERSION, 'store_id': index.store_id,
           'backend': index.backend if index.live() else None, 'keys': keys, 'pending': pending}
    if index.forgotten:
        doc['forgotten'] = [{'key_id': f.key_id, 'at': f.at, 'store': f.store, **_keychain_json(f.keychain)}
                            for f in index.forgotten]
    body = json.dumps(doc, indent=1, ensure_ascii=True)
    return (body + '\n').encode('ascii')


def _keychain_json(keychain: tuple | None) -> dict:
    return {} if keychain is None else {KEYCHAIN_FIELD: {'collection': keychain[0], 'created': keychain[1]}}


def _keychain_parsed(entry: dict) -> tuple | None | bool:
    """A record's or a use's ``keychain`` as (path, created), None when absent, False when malformed."""
    if KEYCHAIN_FIELD not in entry:
        return None
    found = entry[KEYCHAIN_FIELD]
    if not isinstance(found, dict) or set(found) != {'collection', 'created'}:
        return False
    path, created = found['collection'], found['created']
    if not isinstance(path, str) or len(path) > 4096 or _object_path(path) != path:
        return False
    if type(created) is not int or created < 1:      # 0 is never written: a service that says 0 says nothing
        return False
    return path, created


def _epoch(stamp: str) -> int:
    """Seconds of a record's ``created_at`` (``_now``'s form)."""
    return calendar.timegm(time.strptime(stamp[:19], '%Y-%m-%dT%H:%M:%S'))


def where(record: KeyRecord, now: tuple | None) -> str:
    """Where ``record``'s keychain is against ``now``, the one that answers (KEYCHAIN): a name from ``WHERE``."""
    if record.store == 'owner_file':
        return 'same'                    # the store's own file, in its folder: there is no other
    if record.keychain is None:
        return 'unrecorded'
    if now is None:
        return 'unknown'
    if now == record.keychain:
        return 'same'
    if now[0] == record.keychain[0] and now[1] > _epoch(record.created_at):
        return 'remade'
    return 'other'


class KeyStore:
    """One user's key store. Every call opens and checks the folder again; nothing is cached between calls.

    The store's identity is read from its index on every call (see IDENTITY above), never from ``root``.
    """

    def __init__(self, root: Path | str | None = None, *, system: System | None = None, clock=time.time) -> None:
        self.system = system or System()
        self.root = Path(root) if root is not None else default_root(self.system)
        self._clock = clock

    def __repr__(self) -> str:
        return f'KeyStore({str(self.root)!r})'

    # -- the folder and the index ------------------------------------------------------------------
    def _supported(self) -> None:
        if self.system.platform == 'win32' or os.name == 'nt':
            raise KeyStoreError('backend_not_built',
                                'the Windows Credential Manager backend is not built yet; no key is written to a file '
                                'on a host that has a keychain')

    def _folder(self, *, create: bool) -> Folder | None:
        self._supported()
        return open_folder(self.root, create=create)

    def _read_index(self, folder: Folder) -> Index:
        raw = folder.read(INDEX_NAME, INDEX_MAX)
        if raw is None:
            return Index(None, {})
        index = _parse_index(raw)
        if index is None:
            raise KeyStoreError('index_invalid', f'{folder.path / INDEX_NAME} is not an index this store wrote')
        return index

    @staticmethod
    def _sized(index: Index) -> bytes:
        data = index_bytes(index)
        if len(data) > INDEX_MAX:
            raise KeyStoreError('store_full', f'the index would be larger than {INDEX_MAX} bytes; remove a key first')
        return data

    @classmethod
    def _write_index(cls, folder: Folder, index: Index) -> None:
        folder.write(INDEX_NAME, cls._sized(index))

    def _backend(self, pinned: str | None, store_id: str | None):
        """The backend object to use now, for the store named ``store_id``. With a pin, the host must still offer
        the pinned backend."""
        return self._made(self._backend_name(pinned), store_id)

    def _backend_name(self, pinned: str | None) -> str:
        """The backend to use now: the pinned one, which the host must still offer, or the one the host offers."""
        offered = detect(self.system)
        if pinned is not None and offered != pinned:
            if pinned != 'owner_file' and offered == 'owner_file':
                raise KeyStoreError('keychain_unavailable',
                                    f'keys were saved in {STORE_TEXT[pinned]}, which does not answer now; '
                                    'they are not looked for anywhere else, and are removed only where it answers')
            raise KeyStoreError('backend_changed',
                                f'keys were saved in {STORE_TEXT[pinned]}, but this host now offers {STORE_TEXT[offered]}; '
                                'they are removed only where that keychain answers, then added again here')
        return pinned or offered

    def _made(self, name: str, store_id: str | None):
        if name == 'owner_file':
            return OwnerFile(store_id)
        if name == 'secret_service':
            return SecretService(self.system, store_id)
        raise KeyStoreError('backend_not_built',
                            f'the {STORE_TEXT[name]} backend is not built yet; no key is written to a file on a host '
                            'that has a keychain')

    def _removal_backend(self, pinned: str | None, store_id: str | None):
        """The backend a removal uses: the owner-only file is in the store's folder, whatever the host offers now."""
        return OwnerFile(store_id) if pinned == 'owner_file' else self._backend(pinned, store_id)

    @staticmethod
    def _identity(folder: Folder, index: Index, name: str) -> Index:
        """The store's id when its index has no record (lost, emptied or replaced). A store whose backend is the
        owner-only file takes the id that file names: the same folder is the same store. A store on a keychain never
        takes it, so no file can merge two keychain namespaces. Only a folder with neither mints an id."""
        if index.live():
            return index
        found = OwnerFile(None).store_of(folder) if name == 'owner_file' else None
        return replace(index, store_id=found or index.store_id or _random_id())

    @staticmethod
    def _own_file(folder: Folder, index: Index) -> OwnerFile | None:
        """The store's own owner-only file (see VALUES NO RECORD NAMES): None while the index pins the store to a
        keychain; while it has records on the file, the file only when it names the index's store id; with no
        record, the file in the folder, whichever store id it names (the same folder is the same store). Asks no
        host. A file in its place that the store did not write is refused, as every command that reads the file
        refuses it."""
        if index.live():
            return OwnerFile(index.store_id) if index.backend == 'owner_file' else None
        return OwnerFile(OwnerFile(None).store_of(folder))

    def _unnamed(self, folder: Folder, index: Index, key_id: str) -> None:
        """For ``remove`` of an id no record or intent names: the value of it that the store's own owner-only file
        holds (an index lost, emptied or put back no longer names it) is deleted, whatever the host offers now, in
        one write of the file; no intent is recorded (nothing outside the folder changes, and the file is replaced
        whole) and the index is not changed, so a removal takes no store id. Anything else is ``key_unknown``: a
        store pinned to a keychain reads no file here and asks its keychain nothing."""
        own = self._own_file(folder, index)
        if own is None or key_id not in own.ids(folder):
            raise KeyStoreError('key_unknown', f'{key_id} is not saved')
        own.delete(key_id, folder)

    def _recover(self, folder: Folder, index: Index, backend, *, but: str | None = None) -> Index:
        """Finish or undo what earlier writes left pending (not ``but``). An entry that still cannot be reached stays."""
        if not index.pending:
            return index
        keys, pending = dict(index.keys), dict(index.pending)
        for kid, entry in sorted(index.pending.items()):
            if kid == but:
                continue
            if entry.op == 'add' and kid in keys:
                del pending[kid]                 # the add finished; only its intent was left behind
                continue
            save_id = entry.save_id
            if save_id is None and kid in keys:
                save_id = keys[kid].save_id      # an intent from before save ids: its record names the save
            if save_id is None:
                if backend.holds(kid, folder) is not False:
                    continue                     # which save it was for cannot be told: it stays, refused as unfinished
            else:
                try:
                    backend.delete(kid, folder, save_id=save_id)    # the value of that save only
                except KeyStoreError:
                    continue                     # still unreachable: the intent stays, and list shows it
            keys.pop(kid, None)                  # an unfinished remove is finished; an orphaned add is undone
            del pending[kid]
        if pending == index.pending:
            return index
        recovered = replace(index, keys=keys, pending=pending)
        self._write_index(folder, recovered)
        return recovered

    @staticmethod
    def _place(backend, record: KeyRecord) -> str:
        """Where ``record``'s value is against the keychain that answers (KEYCHAIN): ``where`` by the identifier,
        then, when that is not 'same', 'held' when the record's own save is in the keychain that answers. A keychain
        that cannot be asked raises its typed refusal, and nothing has been changed."""
        named = record.store == 'secret_service' and record.keychain is not None
        place = where(record, backend.identity() if named else None)
        if place != 'same' and backend.name == 'secret_service' and backend.presence(record.key_id, record.save_id):
            return 'held'
        return place

    def _out_of_reach(self, index: Index, record: KeyRecord) -> None:
        """For ``forget``: refuse ``value_reachable`` while ``remove`` can delete the key where it is (KEYCHAIN)."""
        if record.store == 'owner_file':
            raise _reachable(record.key_id, 'is in the owner-only file in its folder')
        try:
            backend = self._removal_backend(index.backend, index.store_id)
        except KeyStoreError:
            return                               # no keychain of its kind answers here: nothing here can reach it
        place = self._place(backend, record)
        if place == 'same':
            raise _reachable(record.key_id, 'was saved into the keychain that answers now')
        if place == 'held':
            raise _reachable(record.key_id, 'is in the keychain that answers now (its own save is there)')

    # -- commands ----------------------------------------------------------------------------------
    def add(self, key_id: str, provider: str, env_name: str, value: str, *, metered_spend_allowed: bool = False,
            cap_usd: float = 0) -> KeyRecord:
        """Save a new key. An id that is already saved is refused (``key_exists``); remove it first."""
        problem = v.id_problem(key_id)
        if problem is not None:
            raise KeyStoreError('id_invalid', problem)
        if not v.valid_provider(provider):
            raise KeyStoreError('provider_invalid', 'a provider is a lower-case vendor id: a-z 0-9 . _ -, starting with '
                                f'a letter or digit (published ids: {", ".join(v.PROVIDER_IDS)})')
        if not v.valid_env_name(env_name):
            raise KeyStoreError('env_name_invalid', 'a variable name is A-Z 0-9 _, not starting with a digit')
        if v.looks_like_a_key(provider):
            raise KeyStoreError('provider_invalid', 'this provider looks like a key, not a vendor id, so it is refused and '
                                'not repeated here; a key is never taken from the command line')
        if v.looks_like_a_key(env_name):
            raise KeyStoreError('env_name_invalid', 'this variable name looks like a key, not a name, so it is refused '
                                'and not repeated here; a key is never taken from the command line')
        if not v.valid_value(value):
            raise KeyStoreError('value_invalid', f'a key is {v.VALUE_MIN} to {v.VALUE_MAX} visible ASCII characters, '
                                'with no space')
        if type(metered_spend_allowed) is not bool or not v.valid_cap(cap_usd) \
                or (cap_usd > 0) != metered_spend_allowed:
            raise KeyStoreError('cap_invalid', 'spend is either allowed with a cap above 0, or not allowed with a cap of 0')
        existing = self._folder(create=False)
        if existing is None:
            self._backend(None, None)   # a host this store cannot save on is refused before any folder is made
        else:
            existing.close()
        with self._folder(create=True) as folder, folder.locked():
            folder.sweep()
            index = self._read_index(folder)
            name = self._backend_name(index.backend)     # no pin while the index is empty: a first save pins
            index = self._identity(folder, index, name)
            backend = self._made(name, index.store_id)
            index = self._recover(folder, index, backend)
            if key_id in index.keys:
                raise KeyStoreError('key_exists', f'{key_id} is already saved; remove it first')
            if key_id in index.pending:
                raise KeyStoreError('operation_pending', f'an earlier operation on {key_id} is not finished and its '
                                    'backend could not be reached; remove the key to clear it')
            if name == 'owner_file' and key_id in backend.ids(folder):
                raise KeyStoreError('key_exists', f'the owner-only file still holds a value of {key_id} that no saved '
                                    f'key names (its index was lost, emptied or put back); "remove {key_id}" deletes '
                                    'it, then add the key again')
            keychain = backend.identity() if name == 'secret_service' else None    # which keychain: KEYCHAIN
            now = _now(self._clock)
            record = KeyRecord(key_id, provider, env_name, now, backend.name, metered_spend_allowed, cap_usd,
                               _random_id(), keychain)
            keys = {**index.keys, key_id: record}
            intent = replace(index, backend=backend.name,
                             pending={**index.pending, key_id: Pending(key_id, 'add', now, record.save_id)})
            done = replace(index, backend=backend.name, keys=keys)
            self._sized(replace(intent, keys=keys))                     # both states fit, before anything changes
            self._write_index(folder, intent)                           # 1. the intent, before the outside effect
            try:
                backend.put(key_id, value, folder, record=record)     # 2. the value, bound to its record, and no other
            except KeyStoreError:
                if backend.holds(key_id, folder) is False:              # verified: nothing of the id was saved
                    with contextlib.suppress(Exception):
                        self._write_index(folder, index)
                raise
            try:
                self._write_index(folder, done)                         # 3. the key, and no intent
            except BaseException:
                try:
                    backend.delete(key_id, folder, save_id=record.save_id)    # this save's value, and no other's
                    undone = True
                except Exception:  # noqa: BLE001 - the intent stays in the index: the next write recovers it
                    undone = False
                if undone:
                    with contextlib.suppress(Exception):
                        self._write_index(folder, index)
                raise
            return record

    def remove(self, key_id: str, *, report: list | None = None) -> KeyRecord | None:
        """Delete a key's value from its backend, then its entry from the index.

        Returns the key's record, or None when no record named the id: an unfinished operation (an interrupted add)
        whose orphaned value this removal cleared, or a value of the id that the owner-only file held and the index no
        longer named (see VALUES NO RECORD NAMES). A record's keychain is asked first (KEYCHAIN); ``report``, when
        given, gets ``already_gone`` when the record's keychain held no item of it any more.
        """
        problem = v.id_problem(key_id)
        if problem is not None:
            raise KeyStoreError('id_invalid', problem)
        folder = self._folder(create=False)
        if folder is None:
            raise KeyStoreError('key_unknown', f'{key_id} is not saved')
        with folder, folder.locked():
            folder.sweep()
            index = self._read_index(folder)
            record = index.keys.get(key_id)
            if record is None and key_id not in index.pending:
                self._unnamed(folder, index, key_id)     # a value only the store's own file holds, or key_unknown
                return None
            backend = self._removal_backend(index.backend, index.store_id)
            place = 'same' if record is None else self._place(backend, record)
            if place not in ('same', 'held'):
                raise _elsewhere(record, place)        # before anything is changed or recovered
            index = self._recover(folder, index, backend, but=key_id)
            keys = {kid: rec for kid, rec in index.keys.items() if kid != key_id}
            others = {kid: p for kid, p in index.pending.items() if kid != key_id}
            earlier = index.pending.get(key_id)
            save_id = record.save_id if record is not None else (earlier.save_id if earlier is not None else None)
            now = _now(self._clock)
            self._write_index(folder, replace(index, pending={**others, key_id: Pending(key_id, 'remove', now, save_id)}))
            found = backend.delete(key_id, folder)   # every value of the id in this store, and no other id's, or it refuses
            self._write_index(folder, replace(index, keys=keys, pending=others))
            if not found and record is not None and report is not None:
                report.append('already_gone')
            return record

    def forget(self, key_id: str) -> KeyRecord | None:
        """The person's own step for ONE named key (KEYCHAIN): remove its record and any unfinished operation on it
        from the index, delete nothing, and record the use. Refused ``value_reachable``, nothing changed, while
        ``remove`` can delete the key where it is. Returns the record, or None when only an unfinished operation
        named the id."""
        problem = v.id_problem(key_id)
        if problem is not None:
            raise KeyStoreError('id_invalid', problem)
        folder = self._folder(create=False)
        if folder is None:
            raise KeyStoreError('key_unknown', f'{key_id} is not saved')
        with folder, folder.locked():
            folder.sweep()
            index = self._read_index(folder)
            record = index.keys.get(key_id)
            if record is None and key_id not in index.pending:
                raise KeyStoreError('key_unknown', f'{key_id} is not saved')
            if record is not None:
                self._out_of_reach(index, record)
            used = Forgotten(key_id, _now(self._clock), record.store if record is not None else index.backend,
                             record.keychain if record is not None else None)
            self._write_index(folder, replace(
                index, keys={kid: rec for kid, rec in index.keys.items() if kid != key_id},
                pending={kid: p for kid, p in index.pending.items() if kid != key_id},
                forgotten=index.forgotten + (used,)))
            return record

    def whereabouts(self) -> tuple:
        """For ``status`` and the audit: ``(index, {key id: a name from WHERE}, refusal code or None)``. Asks the bus
        which keychain answers (no item is read); a host that no longer offers the pinned backend gives its code."""
        index = self.list()
        if not index.keys:
            return index, {}, None
        try:
            backend = self._removal_backend(index.backend, index.store_id)
        except KeyStoreError as exc:
            return index, {}, exc.code
        now = backend.identity() if backend.name == 'secret_service' else None
        places = {}
        for kid, record in sorted(index.keys.items()):
            places[kid] = where(record, now)
            if places[kid] != 'same' and backend.name == 'secret_service':
                with contextlib.suppress(KeyStoreError):         # a keychain that cannot tell: the identifier's word
                    if backend.presence(kid, record.save_id):
                        places[kid] = 'same'                     # its own save is there: see KEYCHAIN
        return index, places, None

    def list(self) -> Index:
        """The index: the pinned backend, every key's record and every unfinished operation. Touches no backend."""
        folder = self._folder(create=False)
        if folder is None:
            return Index(None, {})
        with folder, folder.locked(shared=True) as held:
            index = self._read_index(folder)
            if index.live() and not held:
                raise KeyStoreError('file_invalid', f'{folder.path} has an index and no lock file beside it')
            return index

    def values(self, *, unreadable: list | None = None) -> tuple:
        """For ``audit`` only, never for an environment: ``({key id: value}, {key id: refusal code})``.

        Includes the value of an interrupted add the backend still holds, so that the audit looks for it too. A
        saved key whose value cannot be read (gone, or a keychain that does not answer now) is in the second
        mapping, with the code, and makes the audit incomplete. Every value the owner-only file holds that no record
        of the index reaches (an index lost or replaced, or a record naming another save) is searched for too, under
        its own id and a label: ``UNBOUND`` in the store's own file, ``FOREIGN`` in one it does not use (see VALUES
        NO RECORD NAMES). A file of the store's own that is not what it wrote is refused, as every command that
        reads the file refuses it; one it does not use and cannot read is named in ``unreadable`` when a list is
        given (the audit's), and refused when none is.
        """
        folder = self._folder(create=False)
        if folder is None:
            return {}, {}
        with folder, folder.locked(shared=True) as held:
            index = self._read_index(folder)
            if index.live() and not held:
                raise KeyStoreError('file_invalid', f'{folder.path} has an index and no lock file beside it')
            found, missing = self._bound_values(folder, index)
            own = self._own_file(folder, index)
            try:
                held = OwnerFile(None).values(folder)
                own_ids = own.ids(folder) if own is not None else frozenset()
            except KeyStoreError:
                if own is not None or unreadable is None:
                    raise
                unreadable.append(str(self.owner_file()))      # a file the store does not use: named, not refused
                return found, missing
            for kid, value in held.items():
                if found.get(kid) != value:
                    found[f'{kid} {UNBOUND if kid in own_ids else FOREIGN}'] = value
            return found, missing

    def _bound_values(self, folder: Folder, index: Index) -> tuple:
        """The values the index's records (and its unfinished adds) reach on the pinned backend."""
        if not index.live():
            return {}, {}
        try:
            backend = self._backend(index.backend, index.store_id)
        except KeyStoreError as exc:
            return {}, dict.fromkeys(index.keys, exc.code)
        found, missing = {}, {}
        for kid in sorted(set(index.keys) | set(index.pending)):
            record = index.keys.get(kid)
            try:
                found[kid] = backend.get(kid, folder, record)
            except KeyStoreError as exc:     # an unfinished add's value: no saved key's, so nothing to refuse
                if record is not None:
                    missing[kid] = exc.code
        return found, missing

    def unnamed_items(self) -> int | None:
        """For ``audit`` only: how many items in the Secret Service carry ``application recursion`` and are named
        neither by a record of this index (by its save) nor by an unfinished operation (by its id): another store's,
        or this store's from before its index was lost, emptied or put back (see VALUES NO RECORD NAMES). None when
        the store does not use the Secret Service. A pin the host no longer offers, or a keychain that cannot be
        asked, is refused with its code."""
        folder = self._folder(create=False)
        if folder is None:
            return self._unnamed_items(Index(None, {}))
        with folder, folder.locked(shared=True) as held:
            index = self._read_index(folder)
            if index.live() and not held:
                raise KeyStoreError('file_invalid', f'{folder.path} has an index and no lock file beside it')
            return self._unnamed_items(index)

    def _unnamed_items(self, index: Index) -> int | None:
        if index.backend == 'owner_file' or self._backend_name(index.backend) != 'secret_service':
            return None
        backend = SecretService(self.system, index.store_id)
        named = sum(backend.count(kid, None if kid in index.pending else index.keys[kid])   # a record names its save,
                    for kid in sorted(set(index.keys) | set(index.pending)))                  # an intent every save
        return max(backend.count() - named, 0)

    def owner_file(self) -> Path:
        """Where the owner-only file is, or would be."""
        from recursion_core.keys.backends import OWNER_FILE_NAME
        return self.root / OWNER_FILE_NAME

    # -- the credential source ---------------------------------------------------------------------
    def credential(self, connection_id: str, env_name: str) -> str:
        """The key for ``connection_id`` when it is saved under ``env_name``; see the module's text."""
        if not v.valid_key_id(connection_id) or not isinstance(env_name, str):
            raise KeyError(connection_id)
        unknown, code = False, None
        try:
            folder = self._folder(create=False)
            if folder is None:
                raise KeyError(connection_id)
            with folder, folder.locked(shared=True) as held:
                index = self._read_index(folder)
                record = index.keys.get(connection_id)
                unfinished = connection_id in index.pending
                if not unfinished and (record is None or record.env_name != env_name):
                    raise KeyError(connection_id)
                if not held:
                    raise KeyStoreError('file_invalid', f'{folder.path} has an index and no lock file beside it')
                if unfinished:       # an add or removal that did not finish, with or without a record yet
                    raise KeyStoreError('operation_pending', f'an operation on {connection_id} is not finished')
                if not v.known_provider(record.provider):
                    # A vendor outside the published list is kept and listed, never handed to an engine: no route
                    # or grant can name a provider yet, so nothing could tell that this route is meant for it.
                    raise KeyStoreError('provider_unrouted', f'{record.provider} is not a published vendor; the key is '
                                        'kept and listed, and handed to no engine until the vendor is published')
                if not record.spend_allowed():
                    raise KeyStoreError('spend_not_allowed',
                                        'this key has no metered-spend opt-in and cap; remove it and add it again '
                                        'with one')
                # The value of this record's own save, and no other: see IDENTITY.
                return self._backend(index.backend, index.store_id).get(connection_id, folder, record)
        except KeyRefused:
            raise
        except KeyError:
            unknown = True
        except KeyStoreError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001 - a store that fails has no credential to give
            log.warning('the key store failed for %s: %s', connection_id, type(exc).__name__)
            code = 'store_failed'
        # Raised here, outside every except block, so neither carries a __context__.
        if unknown:
            raise KeyError(connection_id)
        log.warning('the key store refused %s: %s', connection_id, code)
        raise KeyRefused(connection_id, code)


def _named(keychain: tuple) -> str:
    """A keychain as a person can find it: the last part of its collection path and the day it was made (UTC)."""
    day = time.strftime('%Y-%m-%d', time.gmtime(keychain[1]))
    return f'the keychain "{keychain[0].rsplit("/", 1)[-1]}" made on {day} (UTC)'


def _elsewhere(record: KeyRecord, place: str) -> KeyStoreError:
    key_id = record.key_id
    step = (f'If its value is gone for good, "key forget {key_id}" removes the record of this one key (yours alone to '
            'take); the value may still sit in another keychain')
    if place == 'other':
        return KeyStoreError('keychain_other', f'{key_id} was saved into another keychain than the one that answers now: '
                             f'into {_named(record.keychain)}, and the keychain that answers holds no item of it. '
                             f'Nothing was changed. "key remove {key_id}" where that keychain answers (the session or '
                             'machine the key was saved on) deletes it')
    if place == 'remade':
        return KeyStoreError('keychain_remade', f'the keychain that answers now was made after {key_id} was saved (wiped '
                             f'and made again, or another one) and holds no item of {key_id}; it was saved into '
                             f'{_named(record.keychain)}. Nothing was changed. {step}')
    return KeyStoreError('keychain_unrecorded', f'the keychain that answers holds no item of {key_id}, and which keychain '
                         'it was saved into is not known (its record names none, or this keychain does not say which '
                         f'it is). Nothing was changed. {step}')


def _reachable(key_id: str, where_it_is: str) -> KeyStoreError:
    return KeyStoreError('value_reachable', f'{key_id} {where_it_is}, so "key remove {key_id}" deletes it there, with '
                         'its record (and removes the record alone if its item is already gone). "key forget" is only '
                         'for a key whose value no command can reach from here. Nothing was changed')


def _parse_index(raw: bytes) -> Index | None:
    """The index exactly as ``index_bytes`` writes it, or None. Nothing raises with the text attached."""
    try:
        doc = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, _Duplicate):
        doc = None
    base = {'version', 'store_id', 'backend', 'keys', 'pending'}
    if not isinstance(doc, dict) or set(doc) not in (base, base | {'forgotten'}):
        return None
    if type(doc['version']) is not int or doc['version'] != INDEX_VERSION or not _valid_random_id(doc['store_id']):
        return None
    if not isinstance(doc['keys'], dict) or not isinstance(doc['pending'], dict):
        return None
    live = bool(doc['keys'] or doc['pending'])
    backend = doc['backend']
    if (backend not in STORES) if live else (backend is not None):
        return None
    keys = {}
    for key_id, entry in doc['keys'].items():
        if not v.valid_key_id(key_id) or not isinstance(entry, dict) \
                or set(entry) not in (set(FIELDS), set(FIELDS) | {KEYCHAIN_FIELD}):
            return None
        keychain = _keychain_parsed(entry)
        if keychain is False or (keychain is not None and entry['store'] != 'secret_service'):
            return None
        allowed, cap = entry['metered_spend_allowed'], entry['cap_usd']
        if not (v.valid_provider(entry['provider']) and v.valid_env_name(entry['env_name'])
                and v.valid_timestamp(entry['created_at']) and entry['store'] == backend
                and type(allowed) is bool and v.valid_cap(cap) and (cap > 0) == allowed
                and _valid_random_id(entry['save_id'])):
            return None
        keys[key_id] = KeyRecord(key_id, entry['provider'], entry['env_name'], entry['created_at'], entry['store'],
                                 allowed, cap, entry['save_id'], keychain)
    if len({record.save_id for record in keys.values()}) != len(keys):
        return None                      # two records naming one save: an index this store never writes
    pending = {}
    for key_id, entry in doc['pending'].items():
        if not v.valid_key_id(key_id) or not isinstance(entry, dict) \
                or set(entry) not in (set(PENDING_FIELDS), set(PENDING_FIELDS_SAVED)):
            return None
        if entry['op'] not in OPS or not v.valid_timestamp(entry['started_at']):
            return None
        if 'save_id' in entry and not _valid_random_id(entry['save_id']):
            return None
        pending[key_id] = Pending(key_id, entry['op'], entry['started_at'], entry.get('save_id'))
    forgotten = []
    if 'forgotten' in doc:
        if not isinstance(doc['forgotten'], list) or not doc['forgotten']:
            return None
        for entry in doc['forgotten']:
            if not isinstance(entry, dict) or set(entry) not in (set(FORGOTTEN_FIELDS),
                                                                  set(FORGOTTEN_FIELDS) | {KEYCHAIN_FIELD}):
                return None
            keychain = _keychain_parsed(entry)
            if keychain is False or not v.valid_key_id(entry['key_id']) or not v.valid_timestamp(entry['at']) \
                    or entry['store'] not in (*STORES, None):
                return None
            forgotten.append(Forgotten(entry['key_id'], entry['at'], entry['store'], keychain))
    return Index(backend if live else None, keys, pending, doc['store_id'], tuple(forgotten))


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


# ------------------------------------------------------------------------------- the registered source
_default: KeyStore | None = None
_default_lock = threading.Lock()


def default_store() -> KeyStore:
    """The store of the user this process runs as, at its default place. Made on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = KeyStore()
        return _default


def credentials(connection_id: str, env_name: str) -> str:
    """``routes.base.Credentials`` over the default store: what lane KEYS registers into seam S7."""
    return default_store().credential(connection_id, env_name)
