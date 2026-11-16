"""Where a key's VALUE is kept: the Secret Service (through ``secret-tool``), the macOS Keychain (through
``security``) or the owner-only file.

Each backend is made for one store (its random id, from the index) and has ``get(key_id, folder, record)``,
``put(key_id, value, folder, record=...)``, ``delete(key_id, folder)`` and ``holds(key_id, folder)``. ``folder``
is the store's open, checked folder (only the owner-only file uses it). A ``put`` or a ``delete`` changes the
values of the id it names and of no other id: a value no record of the index names any more (the index lost,
emptied or put back) stays where it is until a removal names its id. ``holds`` says whether a value for the id is
there: True, False only when that is verified, None when it cannot be told. A missing value is
``secret_missing``; any other failure is a typed ``KeyStoreError``. No error, repr or log line of a backend
carries a value.

THE BINDING. A value is stored WITH what authorizes it: the store's id, the save id of the record that saved it
and that record's provider. ``get`` given a record returns the value only when all three match the record and
the store; anything else is ``secret_missing``, on every backend. So no index (an older one put back, one copied
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
``lookup`` exits 1 with nothing on its standard error when it finds no unlocked item; anything else that is not
success is a failure, ``keychain_locked`` when its message says the collection is locked or the unlock prompt
was dismissed, else ``keychain_failed``. A failure's detail is fixed text: no line a helper printed is ever
repeated. A failure is never answered by trying the file.

GONE IS VERIFIED. ``lookup`` and ``clear`` also exit 1 silently for a locked item whose unlock was cancelled
(libsecret's unlock yields no path and no error), so their silence never means "no item". An item is gone only
when ``lookup`` (which unlocks, prompting if it must) finds nothing AND ``search --all``, which lists locked
items too, exits 0 and lists none (``presence``). A removal succeeds only then; an add whose helper failed drops
its intent only then.

MACOS KEYCHAIN. Apple's ``/usr/bin/security``, at that fixed path and never one found on PATH, and the login keychain,
which ``security login-keychain`` names once per operation and which every command then names: a save, a lookup, a
removal and a count all go to it, never to the default keychain or the search list (lane KEYS' questions.md K21, default
(d)). Each save is its own generic password: account ``<key id>``, service ``recursion.<store id>.<save
id>.<provider>``, generic attribute (``-G``) ``<store id>``, comment (``-j``) ``<provider>``, label ``Recursion API key
<id>``. Account and service are the item's primary key, and given both, ``security`` searches by those two alone (it
then ignores ``-G`` and ``-j``; Apple's keychain_find.c), so the service carries the whole binding: a value is returned
only to the record of the save that stored it, in this store, for that provider. ``delete`` and ``holds`` search by
account and generic attribute, with no service, which ``security`` does match, so a removal reaches every save of the id
in this store, one item per call. No save meets another's item, so none replaces a value it was not given. The value is
written with ``security -i``, which reads its command from standard input: one ``add-generic-password`` line, the value
as hexadecimal (``-X``), nothing of it in the arguments or the environment. ``security -i`` keeps at most 4095 bytes of
a line and reads the rest as the next command (security.c, readline.c), so a key whose line would be longer is refused
(``value_invalid``, with the limit) before the store writes anything; with the longest id, provider and login-keychain
path that is about 1,800 characters. The exit status of ``-i`` is not documented to report one command's failure, so
every save is read back by its record and compared; one not kept exactly is taken out again (its own item, no other) and
refused ``keychain_failed``; a read-back the keychain does not answer keeps its own refusal (a locked keychain stays
``keychain_locked``) and the save's intent stays. Exit 44 ("item not found") is a hint and never proof: ``security``
also answers it when its search failed, so absence is established only by a complete listing of the login keychain
(``count``) that shows no item of the id, and a removal, or an add that failed, keeps its record or intent until then.
``count`` reads ``dump-keychain``, which without ``-d`` prints attributes only (no password) and asks for access to
none. The audit's count of the items no record names comes from ONE listing (``unnamed``), so a change between two
listings cannot hide an item. A listing that exits with a failure, writes anything on standard error, prints nothing it
can read as items, or lists a generic password without its account and service (or one of Recursion's without its
generic attribute and comment) is refused: Apple's dump loop ignores an item it could not print whole, so such a listing
may be incomplete. An empty listing is an empty keychain. "User interaction is not allowed" or a locked keychain is
``keychain_locked``; a timeout is ``keychain_timeout``; a tool that vanished is ``keychain_tool_missing``; a failure's
detail is fixed text. Written against Apple's documented interface and its published source and tested against a
stand-in program that follows them: it has not run on a Mac.

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

import contextlib
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

    def _item(self, key_id: str, record) -> list:
        """The item a record saved: its store, id, save and provider. With no record, any item of the id."""
        if record is None:
            return self._attributes(key_id)
        return self._attributes(key_id) + ['recursion_save', record.save_id, 'recursion_provider', record.provider]

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

    def presence(self, key_id: str) -> bool:
        """True when an item of ``key_id`` is there, False when it verifiably is not (``lookup`` finds none and
        ``count`` lists none); a helper that fails, and so cannot tell, raises its typed refusal."""
        look = self._run([self._tool(), 'lookup', *self._attributes(key_id)])
        if look.returncode == 0:
            return True
        if not self._absent(look):
            raise _failure('lookup', look)
        return self.count(key_id) > 0

    def count(self, key_id: str | None = None, record=None) -> int:
        """How many items ``search --all`` lists, locked ones too: with ``key_id`` and a record, the item that record's
        save stored; with ``key_id`` alone, this store's items of that id, whichever save stored them; without, every
        item that carries ``application recursion``, whichever store saved it (for the audit's count of the items no
        record names). A helper that fails raises its typed refusal.

        ``search`` prints the secrets of the unlocked items it lists: its output is read for item lines only
        (``[<id>]``, one per item) and dropped.
        """
        words = self._item(key_id, record) if key_id is not None else ['application', 'recursion']
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

    def delete(self, key_id: str, folder: Folder) -> None:
        """Remove every item of ``key_id`` in this store; succeed only when none is verifiably left.

        ``clear`` removes the unlocked ones. Then ``presence`` decides: gone is done; still there (a locked item,
        which presence's ``lookup`` may just have unlocked) is cleared once more and asked again; a helper that
        cannot tell, or an item still there after the second try, is refused, and the caller keeps its record.
        """
        for _ in range(2):
            done = self._run([self._tool(), 'clear', *self._attributes(key_id)])
            if done.returncode != 0 and not self._absent(done):
                raise _failure('clear', done)
            if self.presence(key_id) is False:
                return
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


class MacKeychain:
    """The login keychain through Apple's ``/usr/bin/security`` (see MACOS KEYCHAIN above)."""

    name = 'macos_keychain'
    TOOL = sysmod.MAC_SECURITY      # Apple's tool at its fixed path; never one found on PATH
    NOT_FOUND = 44                  # errSecItemNotFound as an exit status: a hint, never proof that an item is gone
    DELETE_LIMIT = 64               # items of one id a removal deletes before it gives up and refuses
    LINE_MAX = 4094                 # security -i keeps 4095 bytes of a line (a 4096-byte buffer); the rest runs next

    def __init__(self, system: sysmod.System, store_id: str) -> None:
        self._system = system
        self._store_id = store_id
        self._keychain = None       # the login keychain's path: resolved once, then named by every command

    def __repr__(self) -> str:
        return 'MacKeychain()'

    def _tool(self) -> str:
        if not self._system.is_executable(self.TOOL):
            raise KeyStoreError('keychain_tool_missing', f'the macOS security tool was not found at {self.TOOL}')
        return self.TOOL

    def _run(self, argv: list, *, input: bytes | None = None) -> sysmod.Completed:
        code = None
        try:
            return self._system.run(argv, input=input, timeout=sysmod.KEYCHAIN_TIMEOUT_S)
        except sysmod.ToolTimeout:
            code, detail = 'keychain_timeout', 'security did not answer in time'
        except sysmod.ToolMissing:
            code, detail = 'keychain_tool_missing', 'security could not be started'
        raise KeyStoreError(code, detail)       # outside the handlers: no __context__

    def _login(self) -> str:
        """The login keychain's path, from ``security login-keychain``, resolved once for this backend's life (one
        store operation), so every command of it names the same keychain. Anything but one absolute, quoted path is
        refused, never guessed at."""
        if self._keychain is None:
            done = self._run([self._tool(), 'login-keychain'])
            if done.returncode != 0:
                raise _mac_failure('login-keychain', done)
            path = _login_path(done.stdout)
            if path is None:
                raise KeyStoreError('keychain_failed', 'security login-keychain named no login keychain this store '
                                    'can use')
            self._keychain = path
        return self._keychain

    def _service(self, save_id: str, provider: str) -> str:
        """The service of the item a save stored: it carries the store, the save and the provider."""
        return f'recursion.{self._store_id}.{save_id}.{provider}'

    def _of_id(self, key_id: str) -> list:
        """Every item of ``key_id`` in this store, whichever save stored it: account and generic attribute (no
        service, so ``security`` matches both)."""
        return ['-a', key_id, '-G', self._store_id]

    def _of_record(self, key_id: str, record) -> list:
        """The item a record saved: account and service, its primary key. With no record, any item of the id."""
        if record is None:
            return self._of_id(key_id)
        return ['-a', key_id, '-s', self._service(record.save_id, record.provider)]

    def _line(self, key_id: str, provider: str, save_id: str, hexvalue: str) -> bytes:
        """The ``add-generic-password`` line, without its newline. The id, the ids and the provider are tokens and
        the value is hexadecimal, so only the label and the keychain's path are quoted (that path holds no quote,
        backslash or control character, ``_login_path``)."""
        return (f'add-generic-password -a {key_id} -s {self._service(save_id, provider)} -G {self._store_id} '
                f'-j {provider} -l "Recursion API key {key_id}" -X {hexvalue} "{self._login()}"').encode()

    def longest(self, key_id: str, provider: str) -> int:
        """The longest key, in characters, whose line ``security -i`` reads whole for this id and provider here."""
        fixed = len(self._line(key_id, provider, '0' * 32, ''))
        return max((self.LINE_MAX - fixed) // 2, 0)

    def admit(self, key_id: str, value: str, record) -> None:
        """Refuse, before the store writes anything, a key that line could not carry whole."""
        longest = self.longest(key_id, record.provider)
        if len(value) > longest:
            raise KeyStoreError('value_invalid', f'a key kept in the macOS Keychain is at most {longest} characters '
                                'for this id and keychain (security -i reads one line of at most 4095 bytes); nothing '
                                'was saved')

    def get(self, key_id: str, folder: Folder, record=None) -> str:
        done = self._run([self._tool(), 'find-generic-password', *self._of_record(key_id, record), '-w',
                          self._login()])
        if done.returncode == 0:
            raw = done.stdout[:-1] if done.stdout.endswith(b'\n') else done.stdout
            done = None
            value = decode_value(raw)
            raw = None
            if value is None:
                raise KeyStoreError('value_invalid',
                                    f'the macOS Keychain item for {key_id} is not a key this store saved')
            return value
        if done.returncode == self.NOT_FOUND:
            raise KeyStoreError('secret_missing', f'the macOS Keychain has no item for {key_id}')
        raise _mac_failure('find-generic-password', done)

    def put(self, key_id: str, value: str, folder: Folder, *, record) -> None:
        self.admit(key_id, value, record)
        line = self._line(key_id, record.provider, record.save_id, value.encode('ascii').hex()) + b'\n'
        done = self._run([self._tool(), '-i'], input=line)
        line = None
        if done.returncode != 0:
            raise _mac_failure('add-generic-password', done)
        try:
            kept = self.get(key_id, folder, record) == value
        except KeyStoreError as exc:
            if exc.code not in ('secret_missing', 'value_invalid'):
                raise                   # the keychain did not answer: its own refusal, and the save's intent stays
            kept = False
        if not kept:
            with contextlib.suppress(KeyStoreError):     # this save's own item, and no other save of the id
                self._run([self._tool(), 'delete-generic-password', *self._of_record(key_id, record), self._login()])
            raise KeyStoreError('keychain_failed', f'the macOS Keychain did not keep the key for {key_id} as given')

    def presence(self, key_id: str) -> bool:
        """True when an item of ``key_id`` is in this store; False only when a complete listing of the login
        keychain shows none (exit 44 alone is not proof). A helper that fails, and so cannot tell, raises its typed
        refusal. Reads no password."""
        done = self._run([self._tool(), 'find-generic-password', *self._of_id(key_id), self._login()])
        if done.returncode == 0:
            return True
        if done.returncode != self.NOT_FOUND:
            raise _mac_failure('find-generic-password', done)
        return self.count(key_id) > 0

    def holds(self, key_id: str, folder: Folder) -> bool | None:
        try:
            return self.presence(key_id)
        except Exception:  # noqa: BLE001 - a probe that fails cannot tell
            return None

    def count(self, key_id: str | None = None, record=None) -> int:
        """How many of Recursion's generic passwords a complete listing of the login keychain shows: with ``key_id``
        and a record, the item that record's save stored; with ``key_id`` alone, this store's items of that id,
        whichever save stored them; without, every item whose service is a Recursion save's, whichever store saved
        it (the audit's count of the items no record names). ``dump-keychain`` without ``-d`` prints attributes
        only: no password, and it asks for access to none. A listing that is not complete raises its typed
        refusal (see MACOS KEYCHAIN)."""
        if key_id is None:
            wanted = {}
        elif record is None:
            wanted = {'acct': key_id, 'gena': self._store_id}
        else:
            wanted = {'acct': key_id, 'svce': self._service(record.save_id, record.provider)}
        return sum(1 for item in self._inventory() if all(item.get(name) == value for name, value in wanted.items()))

    def unnamed(self, index) -> int:
        """The audit's count of the items no record names, from ONE listing of the login keychain: an item is named
        by a record of ``index`` when it is that record's save, and by an unfinished operation when it is an item of
        that id in this store. One listing, not one per id, so a change between two listings cannot hide an item."""
        def named(item) -> bool:
            key_id = item.get('acct')
            if key_id in index.pending:
                return item.get('gena') == self._store_id
            record = index.keys.get(key_id)
            return record is not None and item.get('svce') == self._service(record.save_id, record.provider)
        return sum(1 for item in self._inventory() if not named(item))

    def _inventory(self) -> list:
        """Recursion's generic passwords in one complete listing of the login keychain, or a typed refusal."""
        done = self._run([self._tool(), 'dump-keychain', self._login()])
        if done.returncode != 0:
            raise _mac_failure('dump-keychain', done)
        if done.stderr.strip():
            raise KeyStoreError('keychain_failed', 'security dump-keychain reported an error beside its listing, which '
                                'may therefore be incomplete')
        return [item for item in _dumped(done.stdout) if MAC_SERVICE.fullmatch(item.get('svce') or '')]

    def delete(self, key_id: str, folder: Folder) -> None:
        """Remove every item of ``key_id`` in this store, one per call; succeed only when a complete listing shows
        none left."""
        for _ in range(self.DELETE_LIMIT):
            done = self._run([self._tool(), 'delete-generic-password', *self._of_id(key_id), self._login()])
            if done.returncode == self.NOT_FOUND:
                break
            if done.returncode != 0:
                raise _mac_failure('delete-generic-password', done)
        if self.presence(key_id):
            raise KeyStoreError('keychain_failed', f'the macOS Keychain still holds {key_id}; it stays on record')


def _mac_failure(action: str, done: sysmod.Completed) -> KeyStoreError:
    """A ``security`` failure as a typed refusal. The detail is fixed text: nothing the tool printed is repeated."""
    lowered = done.stderr.decode('ascii', 'replace').lower()
    if 'interaction is not allowed' in lowered or 'locked' in lowered:
        code, why = 'keychain_locked', ': the keychain is locked, or would have to ask'
    else:
        code, why = 'keychain_failed', ''
    return KeyStoreError(code, f'security {action} exited {done.returncode}{why}')


def _login_path(out: bytes) -> str | None:
    """The one path ``security login-keychain`` prints, ``"<absolute path>"`` (indented); None for anything else,
    and for a path holding a quote, a backslash or a control character (it is quoted on the ``-i`` line)."""
    text = out.decode('utf-8', 'replace').strip()
    if len(text) < 3 or not (text.startswith('"') and text.endswith('"')):
        return None
    path = text[1:-1]
    if not path.startswith('/') or any(c in '"\\�' or ord(c) < 32 or ord(c) == 127 for c in path):
        return None
    return path


# The service of an item a save wrote: recursion.<store id>.<save id>.<provider>.
MAC_SERVICE = re.compile(r'recursion\.[0-9a-f]{32}\.[0-9a-f]{32}\.[a-z0-9][a-z0-9._-]{0,63}')
_DUMP_ATTRIBUTE = re.compile(rb'\s+"([A-Za-z0-9]{4})"<[A-Za-z0-9]+>=(.*)')
_DUMP_HEX = re.compile(rb'0x((?:[0-9A-Fa-f]{2})+)(?:\s+".*")?')


def _dumped(out: bytes) -> list:
    """The generic passwords in ``security dump-keychain``'s output, each as ``{attribute: text or None}``.

    An item starts at a ``keychain:`` line and its class is on a ``class:`` line (``"genp"`` for a generic
    password); an attribute line is ``"name"<type>=value``, the value ``"text"``, ``0x<hexadecimal>`` (the text after
    it) or ``<NULL>``. Refused (``keychain_failed``), never read as fewer items: output that is not empty and holds no
    ``keychain:`` line; an item with no ``class:`` line; a generic password without its account and service; one of
    Recursion's without its generic attribute and comment. Nothing of it is repeated in an error."""
    items, item = [], None
    for line in out.split(b'\n'):
        if line.startswith(b'keychain: '):
            item = {'class': None}
            items.append(item)
        elif item is not None and line.startswith(b'class: '):
            item['class'] = line[len(b'class: '):].strip()
        elif item is not None:
            match = _DUMP_ATTRIBUTE.fullmatch(line.rstrip(b'\r'))
            if match is not None:
                item[match.group(1).decode('ascii')] = _dumped_value(match.group(2).strip())
    if out.strip() and not items:
        raise KeyStoreError('keychain_failed', 'security dump-keychain printed nothing that reads as keychain items')
    generic = []
    for item in items:
        if item['class'] is None:
            raise KeyStoreError('keychain_failed', 'security dump-keychain listed an item without its class')
        if item['class'] != b'"genp"':
            continue
        if 'acct' not in item or 'svce' not in item or (
                MAC_SERVICE.fullmatch(item['svce'] or '') and ('gena' not in item or 'icmt' not in item)):
            raise KeyStoreError('keychain_failed', 'security dump-keychain listed a generic password without all of '
                                'its attributes; the listing may be incomplete')
        generic.append(item)
    return generic


def _dumped_value(raw: bytes) -> str | None:
    if len(raw) >= 2 and raw.startswith(b'"') and raw.endswith(b'"'):
        return raw[1:-1].decode('utf-8', 'replace')
    match = _DUMP_HEX.fullmatch(raw)
    return bytes.fromhex(match.group(1).decode('ascii')).decode('utf-8', 'replace') if match is not None else None


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

    def delete(self, key_id: str, folder: Folder) -> None:
        entries = self._load(folder)     # none when the file is another store's: nothing of this store to remove
        if key_id in entries:
            del entries[key_id]          # this id's entry only: every other value stays
            self._save(folder, entries)


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


BACKENDS = {'secret_service': SecretService, 'macos_keychain': MacKeychain, 'owner_file': OwnerFile}
