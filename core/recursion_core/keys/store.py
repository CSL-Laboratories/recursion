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
owner-only file it reaches nothing (the file names another store). A copy of the whole folder is a second store
over the same values (questions.md K18). A removal deletes every value saved for the id in the store, whichever
save stored it.

THE INDEX FORMAT, version 1: ``{"version": 1, "store_id": <32 hex>, "backend": <store or null>, "keys": {<id>:
{"provider", "env_name", "created_at", "store", "metered_spend_allowed", "cap_usd", "save_id"}}, "pending":
{<id>: {"op": "add" | "remove", "started_at"}}}``, at most 1 MiB. Anything else is refused ``index_invalid``. A
listing says what the index holds; it does not say that a keychain is reachable or unlocked now.

PINNING. The first save records the backend the host offered then (``system.detect``) in the index. Every
later operation that touches a value asks again and refuses when the answer differs: ``keychain_unavailable``
when a keychain was pinned and the host now offers only the file, ``backend_changed`` otherwise. A locked or
failing keychain is refused with its own code and is never answered from the file. A key pinned to a keychain
is removed only where that keychain answers, since its removal must reach the value. One exception: a key pinned
to the owner-only file can always be REMOVED, the file being always reachable, so that once a keychain appears
the keys can be removed and added again into it. The pin is released when nothing is left in the index.

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
never handed out under the old one's variable name or opt-in. Lane KEYS' ``hooks.py`` registers it into S7 when
SPINE's seams land; until then a test passes it where the daemon passes ``no_custody``.

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
"""
from __future__ import annotations

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
from recursion_core.keys.system import System, detect

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
SHOWN = ('key_id', 'provider', 'env_name', 'created_at', 'store', 'metered_spend_allowed', 'cap_usd')
PENDING_FIELDS = ('op', 'started_at')
OPS = ('add', 'remove')
RANDOM_ID = re.compile(r'[0-9a-f]{32}')
UNBOUND = '(a value in the owner-only file that no record of this index reaches)'


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

    def spend_allowed(self) -> bool:
        return self.metered_spend_allowed is True and self.cap_usd > 0

    def as_json(self) -> dict:
        """What ``list --json`` shows: the record without its save id, which is the store's own business."""
        return {name: getattr(self, name) for name in SHOWN}


@dataclass(frozen=True)
class Pending:
    """An operation started on the backend and not finished: ``op`` is ``add`` or ``remove``."""

    key_id: str
    op: str
    started_at: str


@dataclass(frozen=True)
class Index:
    backend: str | None                            # the pinned backend; None while nothing is in the index
    keys: dict                                     # key id -> KeyRecord
    pending: dict = field(default_factory=dict)    # key id -> Pending
    store_id: str | None = None                    # the store's random id; None only before the first save

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
    keys ={kid: {name: getattr(record, name) for name in FIELDS} for kid, record in sorted(index.keys.items())}
    pending = {kid: {'op': p.op, 'started_at': p.started_at} for kid, p in sorted(index.pending.items())}
    body = json.dumps({'version': INDEX_VERSION, 'store_id': index.store_id,
                       'backend': index.backend if index.live() else None, 'keys': keys, 'pending': pending},
                      indent=1, ensure_ascii=True)
    return (body + '\n').encode('ascii')


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
        offered = detect(self.system)
        if pinned is not None and offered != pinned:
            if pinned != 'owner_file' and offered == 'owner_file':
                raise KeyStoreError('keychain_unavailable',
                                    f'keys were saved in {STORE_TEXT[pinned]}, which does not answer now; '
                                    'they are not looked for anywhere else, and are removed only where it answers')
            raise KeyStoreError('backend_changed',
                                f'keys were saved in {STORE_TEXT[pinned]}, but this host now offers {STORE_TEXT[offered]}; '
                                'they are removed only where that keychain answers, then added again here')
        name = pinned or offered
        if name == 'owner_file':
            return OwnerFile(store_id)
        if name == 'secret_service':
            return SecretService(self.system, store_id)
        raise KeyStoreError('backend_not_built',
                            f'the {STORE_TEXT[name]} backend is not built yet; no key is written to a file on a host '
                            'that has a keychain')

    def _removal_backend(self, pinned: str | None, store_id: str | None):
        """The backend a removal uses: the owner-only file is always reachable, whatever the host offers now."""
        return OwnerFile(store_id) if pinned == 'owner_file' else self._backend(pinned, store_id)

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
            try:
                backend.delete(kid, folder, frozenset(k for k in keys if k != kid))
            except KeyStoreError:
                continue                         # still unreachable: the intent stays, and list shows it
            keys.pop(kid, None)                  # an unfinished remove is finished; an orphaned add is undone
            del pending[kid]
        if pending == index.pending:
            return index
        recovered = replace(index, keys=keys, pending=pending)
        self._write_index(folder, recovered)
        return recovered

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
            if not index.live():
                # A store with no record takes the id of the owner-only file already in its folder (an index lost,
                # replaced or emptied): the same folder is the same store. Only a folder with neither mints one.
                index = replace(index, store_id=OwnerFile(None).store_of(folder) or index.store_id or _random_id())
            backend = self._backend(index.backend, index.store_id)   # None while the index is empty: a first save pins
            index = self._recover(folder, index, backend)
            if key_id in index.keys:
                raise KeyStoreError('key_exists', f'{key_id} is already saved; remove it first')
            if key_id in index.pending:
                raise KeyStoreError('operation_pending', f'an earlier operation on {key_id} is not finished and its '
                                    'backend could not be reached; remove the key to clear it')
            now = _now(self._clock)
            record = KeyRecord(key_id, provider, env_name, now, backend.name, metered_spend_allowed, cap_usd,
                               _random_id())
            keys = {**index.keys, key_id: record}
            intent = replace(index, backend=backend.name, pending={**index.pending, key_id: Pending(key_id, 'add', now)})
            done = replace(index, backend=backend.name, keys=keys)
            self._sized(replace(intent, keys=keys))                     # both states fit, before anything changes
            self._write_index(folder, intent)                           # 1. the intent, before the outside effect
            try:
                backend.put(key_id, value, folder, frozenset(keys), record=record)     # 2. the value, bound to its record
            except KeyStoreError:
                if backend.holds(key_id, folder) is False:              # verified: nothing of the id was saved
                    with contextlib.suppress(Exception):
                        self._write_index(folder, index)
                raise
            try:
                self._write_index(folder, done)                         # 3. the key, and no intent
            except BaseException:
                try:
                    backend.delete(key_id, folder, frozenset(index.keys))
                    undone = True
                except Exception:  # noqa: BLE001 - the intent stays in the index: the next write recovers it
                    undone = False
                if undone:
                    with contextlib.suppress(Exception):
                        self._write_index(folder, index)
                raise
            return record

    def remove(self, key_id: str) -> KeyRecord | None:
        """Delete a key's value from its backend, then its entry from the index.

        Returns the key's record, or None when the id was only an unfinished operation (an interrupted add) whose
        orphaned value this removal cleared.
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
                raise KeyStoreError('key_unknown', f'{key_id} is not saved')
            backend = self._removal_backend(index.backend, index.store_id)
            index = self._recover(folder, index, backend, but=key_id)
            keys = {kid: rec for kid, rec in index.keys.items() if kid != key_id}
            others = {kid: p for kid, p in index.pending.items() if kid != key_id}
            now = _now(self._clock)
            self._write_index(folder, replace(index, pending={**others, key_id: Pending(key_id, 'remove', now)}))
            backend.delete(key_id, folder, frozenset(keys))     # every value of the id in this store, or it refuses
            self._write_index(folder, replace(index, keys=keys, pending=others))
            return record

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

    def values(self) -> tuple:
        """For ``audit`` only, never for an environment: ``({key id: value}, {key id: refusal code})``.

        Includes the value of an interrupted add the backend still holds, so that the audit looks for it too. A
        saved key whose value cannot be read (gone, or a keychain that does not answer now) is in the second
        mapping, with the code, and makes the audit incomplete. A file of the store that is not what it wrote is
        refused, as every command refuses it. Every value the owner-only file holds is searched for too, under its
        own id and the ``UNBOUND`` label, when no record of the index reaches it (an index lost or replaced, or a
        record naming another save).
        """
        folder = self._folder(create=False)
        if folder is None:
            return {}, {}
        with folder, folder.locked(shared=True) as held:
            index = self._read_index(folder)
            if index.live() and not held:
                raise KeyStoreError('file_invalid', f'{folder.path} has an index and no lock file beside it')
            found, missing = self._bound_values(folder, index)
            for kid, value in OwnerFile(index.store_id).values(folder).items():
                if found.get(kid) != value:
                    found[f'{kid} {UNBOUND}'] = value
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
            except KeyStoreError as exc:
                if exc.code in ('owner_file_invalid', 'file_invalid'):
                    raise
                if record is not None:
                    missing[kid] = exc.code
        return found, missing

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


def _parse_index(raw: bytes) -> Index | None:
    """The index exactly as ``index_bytes`` writes it, or None. Nothing raises with the text attached."""
    try:
        doc = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, _Duplicate):
        doc = None
    if not isinstance(doc, dict) or set(doc) != {'version', 'store_id', 'backend', 'keys', 'pending'}:
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
        if not v.valid_key_id(key_id) or not isinstance(entry, dict) or set(entry) != set(FIELDS):
            return None
        allowed, cap = entry['metered_spend_allowed'], entry['cap_usd']
        if not (v.valid_provider(entry['provider']) and v.valid_env_name(entry['env_name'])
                and v.valid_timestamp(entry['created_at']) and entry['store'] == backend
                and type(allowed) is bool and v.valid_cap(cap) and (cap > 0) == allowed
                and _valid_random_id(entry['save_id'])):
            return None
        keys[key_id] = KeyRecord(key_id, entry['provider'], entry['env_name'], entry['created_at'], entry['store'],
                                 allowed, cap, entry['save_id'])
    if len({record.save_id for record in keys.values()}) != len(keys):
        return None                      # two records naming one save: an index this store never writes
    pending = {}
    for key_id, entry in doc['pending'].items():
        if not v.valid_key_id(key_id) or not isinstance(entry, dict) or set(entry) != set(PENDING_FIELDS):
            return None
        if entry['op'] not in OPS or not v.valid_timestamp(entry['started_at']):
            return None
        pending[key_id] = Pending(key_id, entry['op'], entry['started_at'])
    return Index(backend if live else None, keys, pending, doc['store_id'])


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
