"""The key store: user-entered model-provider API keys, and the credential source for API-key routes.

What the contract asks (CONTRACT 256-267, 275, 1048-1054): Recursion keeps user-entered API keys in local
secure storage, in the OS keychain where there is one and in an owner-only file on a host with none; they
are never logged, never put in a prompt and never sent to CSL; engine sign-ins stay in each engine's own
store; any vendor's key can be kept.

TWO PLACES. The INDEX (``index.json``) holds what is safe to show: per key id, the provider, the variable name
the engine reads it from, when it was saved, which backend holds it, and its spend opt-in and cap. It never
holds a value. The VALUE is in the BACKEND: the Secret Service, or the owner-only file (``backends.py``). Both
files live in one owner-only folder, ``$XDG_CONFIG_HOME/recursion/keys`` (``~/.config/recursion/keys``), with
the checks of ``files.py``. Nothing here is in the event database: no key state changes with an event, and no
secret sits beside the event log.

PINNING. The first save records the backend the host offered then (``system.detect``) in the index. Every
later operation that touches a value asks again and refuses when the answer differs: ``keychain_unavailable``
when a keychain was pinned and the host now offers only the file, ``backend_changed`` otherwise. A locked or
failing keychain is refused with its own code and is never answered from the file. The pin is released only
when no key is left, so the next save is a first save again.

SPEND. Every key has ``metered_spend_allowed`` and ``cap_usd``, typed when it is added; with no opt-in the cap
is 0. ``credentials()`` hands a key out only when both say spend is allowed (opt-in and a cap above 0):
"Zero-dollar reservation" (grill Q44), and the brief's default (a) for spend before lane QUOTA lands, which
is Lucas's to confirm. QUOTA enforces the cap; until it does, the console's own spend limit is the stop.

THE CREDENTIAL SOURCE. ``credentials(connection_id, env_name)`` is ``routes.base.Credentials``: the value
only when the id is known AND its saved variable name is ``env_name``. An unknown id, or a known one asked for
under another name, raises ``KeyError(connection_id)``, exactly as seam S7's default does; a known id the store
refuses raises ``KeyRefused`` (a ``KeyError`` with a code). It never returns None or an empty string. Lane
KEYS' ``hooks.py`` registers it into S7 when SPINE's seams land; until then a test passes it where the daemon
passes ``no_custody``.

Order of writes. Add: the value goes into the backend, then the index; if the index cannot be written the
value is taken out again. Remove: the value comes out of the backend, then the index. So a reader that holds
no lock can see an index entry whose value is missing (refused ``secret_missing``), never a value the index
does not name for longer than one operation.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from recursion_core.keys import validate as v
from recursion_core.keys.backends import BACKENDS
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
FIELDS = ('provider', 'env_name', 'created_at', 'store', 'metered_spend_allowed', 'cap_usd')


@dataclass(frozen=True)
class KeyRecord:
    """What the index keeps for one key. No value."""

    key_id: str
    provider: str
    env_name: str
    created_at: str
    store: str
    metered_spend_allowed: bool
    cap_usd: int | float

    def spend_allowed(self) -> bool:
        return self.metered_spend_allowed is True and self.cap_usd > 0

    def as_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Index:
    backend: str | None          # the pinned backend; None before the first save, or once no key is left
    keys: dict                   # key id -> KeyRecord


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


class KeyStore:
    """One user's key store. Every call opens and checks the folder again; nothing is cached between calls."""

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
    def _write_index(folder: Folder, index: Index) -> None:
        keys = {kid: {field: getattr(record, field) for field in FIELDS} for kid, record in sorted(index.keys.items())}
        body = json.dumps({'version': INDEX_VERSION, 'backend': index.backend if keys else None, 'keys': keys},
                          indent=1, ensure_ascii=True)
        folder.write(INDEX_NAME, (body + '\n').encode('ascii'))

    def _backend(self, pinned: str | None):
        """The backend object to use now. With a pin, the host must still offer the pinned backend."""
        offered = detect(self.system)
        if pinned is not None and offered != pinned:
            if pinned != 'owner_file' and offered == 'owner_file':
                raise KeyStoreError('keychain_unavailable',
                                    f'keys were saved in {STORE_TEXT[pinned]}, which does not answer now; '
                                    'they are not looked for anywhere else')
            raise KeyStoreError('backend_changed',
                                f'keys were saved in {STORE_TEXT[pinned]}, but this host now offers {STORE_TEXT[offered]}; '
                                'remove and add them again to move them')
        name = pinned or offered
        if name not in BACKENDS:
            raise KeyStoreError('backend_not_built',
                                f'the {STORE_TEXT[name]} backend is not built yet; no key is written to a file on a host '
                                'that has a keychain')
        backend = BACKENDS[name]
        return backend() if name == 'owner_file' else backend(self.system)

    # -- commands ----------------------------------------------------------------------------------
    def add(self, key_id: str, provider: str, env_name: str, value: str, *, metered_spend_allowed: bool = False,
            cap_usd: float = 0) -> KeyRecord:
        """Save a new key. An id that is already saved is refused (``key_exists``); remove it first."""
        if not v.valid_key_id(key_id):
            raise KeyStoreError('id_invalid', 'a key id is 1 to 128 of A-Z a-z 0-9 . _ : -, starting with a letter or digit')
        if not v.valid_provider(provider):
            raise KeyStoreError('provider_invalid', f'a provider is one of: {", ".join(v.PROVIDER_IDS)}')
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
            self._backend(None)     # a host this store cannot save on is refused before any folder is made
        else:
            existing.close()
        with self._folder(create=True) as folder, folder.locked():
            index = self._read_index(folder)
            if key_id in index.keys:
                raise KeyStoreError('key_exists', f'{key_id} is already saved; remove it first')
            backend = self._backend(index.backend)       # None once no key is left: the pin is released
            record = KeyRecord(key_id, provider, env_name, _now(self._clock), backend.name,
                               metered_spend_allowed, cap_usd)
            keys = dict(index.keys)
            keys[key_id] = record
            keep = frozenset(keys)
            backend.put(key_id, value, folder, keep)
            try:
                self._write_index(folder, Index(backend.name, keys))
            except BaseException:
                with contextlib.suppress(Exception):
                    backend.delete(key_id, folder, frozenset(index.keys))
                raise
            return record

    def remove(self, key_id: str) -> KeyRecord:
        """Delete a key's value from its backend, then its entry from the index."""
        if not v.valid_key_id(key_id):
            raise KeyStoreError('id_invalid', 'not a key id')
        folder = self._folder(create=False)
        if folder is None:
            raise KeyStoreError('key_unknown', f'{key_id} is not saved')
        with folder, folder.locked():
            index = self._read_index(folder)
            record = index.keys.get(key_id)
            if record is None:
                raise KeyStoreError('key_unknown', f'{key_id} is not saved')
            backend = self._backend(index.backend)
            keys = {kid: rec for kid, rec in index.keys.items() if kid != key_id}
            backend.delete(key_id, folder, frozenset(keys))
            self._write_index(folder, Index(index.backend, keys))
            return record

    def list(self) -> Index:
        """The index: the pinned backend and every key's record. Touches no backend and no keychain."""
        folder = self._folder(create=False)
        if folder is None:
            return Index(None, {})
        with folder:
            return self._read_index(folder)

    def values(self) -> tuple:
        """For ``audit`` only: ``({key id: value}, {key id: refusal code})``, every value from the pinned backend."""
        folder = self._folder(create=False)
        if folder is None:
            return {}, {}
        with folder:
            index = self._read_index(folder)
            if not index.keys:
                return {}, {}
            backend = self._backend(index.backend)
            found, missing = {}, {}
            for kid in sorted(index.keys):
                try:
                    found[kid] = backend.get(kid, folder)
                except KeyStoreError as exc:
                    if exc.code not in ('secret_missing', 'value_invalid'):
                        raise           # the backend itself cannot be read: no audit can be complete
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
        try:
            folder = self._folder(create=False)
            if folder is None:
                raise KeyError(connection_id)
            with folder:
                index = self._read_index(folder)
                record = index.keys.get(connection_id)
                if record is None or record.env_name != env_name:
                    raise KeyError(connection_id)
                if not record.spend_allowed():
                    raise KeyStoreError('spend_not_allowed',
                                        'this key has no metered-spend opt-in and cap; add it again with one')
                return self._backend(index.backend).get(connection_id, folder)
        except KeyRefused:
            raise
        except KeyError:
            raise KeyError(connection_id) from None
        except KeyStoreError as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001 - a store that fails has no credential to give
            log.warning('the key store failed for %s: %s', connection_id, type(exc).__name__)
            code = 'store_failed'
        # Raised here, outside every except block, so the refusal carries no __context__.
        log.warning('the key store refused %s: %s', connection_id, code)
        raise KeyRefused(connection_id, code)

    # -- for other lanes: QUOTA's spend policy, INSTALL's doctor -----------------------------------
    def spend_policy(self, connection_id: str) -> dict:
        """``{"metered_spend_allowed": bool, "cap_usd": number}`` as typed at ``add``; with no opt-in, the cap is 0.

        Reads the index only (no backend, no keychain prompt). An unknown id raises ``KeyError(connection_id)``;
        an index the store cannot read raises ``KeyRefused`` with its code, as ``credential`` does.
        """
        if not v.valid_key_id(connection_id):
            raise KeyError(connection_id)
        try:
            index = self.list()
        except KeyStoreError as exc:
            code = exc.code
        else:
            record = index.keys.get(connection_id)
            if record is None:
                raise KeyError(connection_id)
            return {'metered_spend_allowed': record.metered_spend_allowed, 'cap_usd': record.cap_usd}
        raise KeyRefused(connection_id, code)

    def describe(self) -> dict:
        """Which store holds the keys and which backend this host offers now, for ``doctor``. Never a value.

        ``store``: the pinned backend, or None when no key is saved. ``offered``: what the host offers now, or None
        with ``offered_refusal`` saying why it cannot say. ``refusal``: why the index itself cannot be read, or None.
        It never raises for a store it cannot use: a doctor reports that, it does not fail on it.
        """
        out = {'folder': str(self.root), 'store': None, 'keys': 0, 'owner_file': None, 'refusal': None,
               'offered': None, 'offered_refusal': None}
        try:
            index = self.list()
        except KeyStoreError as exc:
            out['refusal'] = {'code': exc.code, 'detail': exc.detail}
        else:
            out['store'], out['keys'] = index.backend, len(index.keys)
        try:
            out['offered'] = detect(self.system)
        except KeyStoreError as exc:
            out['offered_refusal'] = {'code': exc.code, 'detail': exc.detail}
        if 'owner_file' in (out['store'], out['offered']):
            out['owner_file'] = str(self.owner_file())
        return out


def _parse_index(raw: bytes) -> Index | None:
    """The index exactly as ``_write_index`` writes it, or None. Nothing raises with the text attached."""
    try:
        doc = json.loads(raw.decode('ascii'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, _Duplicate):
        doc = None
    if not isinstance(doc, dict) or set(doc) != {'version', 'backend', 'keys'}:
        return None
    if type(doc['version']) is not int or doc['version'] != INDEX_VERSION or not isinstance(doc['keys'], dict):
        return None
    backend = doc['backend']
    if not (backend is None and not doc['keys']) and backend not in STORES:
        return None
    keys = {}
    for key_id, entry in doc['keys'].items():
        if not v.valid_key_id(key_id) or not isinstance(entry, dict) or set(entry) != set(FIELDS):
            return None
        allowed, cap = entry['metered_spend_allowed'], entry['cap_usd']
        if not (v.valid_provider(entry['provider']) and v.valid_env_name(entry['env_name'])
                and v.valid_timestamp(entry['created_at']) and entry['store'] == backend
                and type(allowed) is bool and v.valid_cap(cap) and (cap > 0) == allowed):
            return None
        keys[key_id] = KeyRecord(key_id, entry['provider'], entry['env_name'], entry['created_at'], entry['store'],
                                 allowed, cap)
    return Index(backend if keys else None, keys)


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


def spend_policy(connection_id: str) -> dict:
    """The default store's spend policy for one key: what lane QUOTA reserves against at admission."""
    return default_store().spend_policy(connection_id)


def describe() -> dict:
    """The default store's description: what lane INSTALL's ``doctor`` reports."""
    return default_store().describe()
