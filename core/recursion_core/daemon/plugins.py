"""Operator plugins for the running daemon, loaded only through ``plugin_security`` (CSL-720, CSL-738).

Spec: PRODUCT.md d84cdd26 (CSLR-D-012), section 4.1 (plugins run in the daemon's process, with
checks the developer sees), 4.2 (approval gate and breaker), 4.3 (signed means signed by a key on
this install's trusted list; a plugin that fails the signature check loads as unsigned; grants are
bound to the artifact digest and the signing key) and 3.5 (plugins contribute panels).

The daemon never imports a plugin module by name. ``PluginHost`` reads each installed plugin as
bytes and hands them to ``plugin_security.PluginLoader``, which checks the manifest, the artifact
digest, the signature against this install's trusted keys and the installer's grant before any
plugin code runs. A plugin the loader refuses stays out, its typed refusal and findings are kept,
and the daemon runs without it. Nothing here is a containment boundary: a loaded plugin is
ordinary Python in the daemon's process and uid (``plugin_security/README.md``).

Install layout, all operator-owned and not group- or other-writable::

    $XDG_CONFIG_HOME/recursion/plugins/install.json      trusted keys, grants, reserved effects
    $XDG_CONFIG_HOME/recursion/plugins/<name>/manifest.json   canonical csl-plugin-manifest/vNext bytes
    $XDG_CONFIG_HOME/recursion/plugins/<name>/manifest.sig    hex Ed25519 signature (absent: unsigned)
    $XDG_CONFIG_HOME/recursion/plugins/<name>/plugin.py       the artifact the manifest's digest names

``install.json``::

    {"trusted_keys": {"<label>": "<64 hex public key>"},
     "grants": [{"plugin_id": "...", "artifact_sha256": "...", "signer_key_id": "ed25519:..." or null,
                 "permissions": ["read", "panel"]}],
     "reserved": {"hosts": [], "credential_prefixes": []},
     "authority_anchor": "<64 hex installer public key>" or null}

What a loaded plugin may give the daemon (plain values; a plugin imports nothing from Recursion):

* ``registry()`` returning ``{"panels": [...], "required": {...}}`` for the GUI, with the ``panel``
  permission. The panels go through the GUI's own validation and its one venue-data filter.
* ``operator(command)`` answering an ``o7.operator.v0`` read command, with the ``read`` permission.
  Each call runs on its own thread with a deadline; past it the read answers 504, the plugin is
  marked degraded, and no new call goes to it until the stuck one returns.
* ``bind_core(call)``, which receives this plugin's ``csl-core/v0`` client (canonical JSON bytes in
  and out) for the approval gate, the authority profile and the breaker.

No silent verification skip: when ``cryptography`` cannot be imported, no signature can be checked,
so every installed plugin is refused ``plugin_verification_unavailable`` and none is loaded, signed
or not.

A plugin effect is approved by the OS user who owns the daemon, over the owner-only socket, or by
the device token, and only through ``approval.answer`` and then ``PluginEffectSink.carry`` (the
effect is charged, then the approval is consumed, once). The charge goes through the core's one
charge path, ``CoreService.authorize_effect``, with every check a plugin's own charge passes: core
health, the permission a key revocation removes, the run and its connection, the commit fence a
halt drains, and the queue record still approved, read again inside that fence. The in-memory gate still names no
principal, so a decision there authorizes nothing. Not here: a plugin principal and a plugin-owned
run (every run is bound to ``host``), the O7 interventions, and an installer (the authority ledger
is never created by the daemon; without it an order effect refuses ``authority_unconfigured``).
"""
from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import time

from recursion_core.gui.client import ACTIONS, READS
from recursion_core.gui.panels import panel_registry
from recursion_core.plugin_security import (ApprovalGate, Authority, CoreHealth, CoreService, GilProbe, Grant,
                                            PERMISSIONS, PluginLoader, Refused, ReservedEffects, TrustStore,
                                            create_revocation_store)
from recursion_core.plugin_security.signing import backend_available

from .breaker import STORE_DIR
from .gui import _snapshot
from .launcher import private_dir, require_launcher_platform

log = logging.getLogger(__name__)

PLUGINS_DIR = 'plugins'                 # $XDG_CONFIG_HOME/recursion/plugins
INSTALL_FILE = 'install.json'
MANIFEST, SIGNATURE, ARTIFACT = 'manifest.json', 'manifest.sig', 'plugin.py'
REVOCATION_FILE = 'revoked.sqlite3'     # <state dir>/plugin-security/
LEDGER_FILE = 'ledger.sqlite3'          # installer-created; never created here
APPROVAL_EVENTS = 'approval-events.sqlite3'
EFFECT_NOTES = 'effect-receipts.json'   # envelope and receipt, so a later process can return effect_replayed
EFFECT_NOTES_NEEDED = 'effect-receipts-needed.json'  # the notes of approvals whose charge has not finished
_TOKEN_CAP = 256
# The approval notes files' figures. Each becomes a row of the person's one settings file, by the
# name in NOTES_ROWS, with this value as its shipped default, once that file exists; None is
# 'no limit'. They are read at each use, so this is the one place they come from. The first three
# govern only finished notes (an approval consumed, or one whose charge was refused: given and not
# carried out), and the size figure prunes the oldest of those first, so the finished file never
# refuses an approval. Notes still needed for a carry are kept in the needed file, whose size row
# ships as 'no limit'.
NOTES_SIZE_LIMIT_BYTES = 1024 * 1024   # the most bytes the finished notes file holds after pruning
NOTES_KEEP_FOR_S = 30 * 24 * 3600      # a finished approval's notes are kept this many seconds
NOTES_KEEP_AT_MOST = 1000              # the notes of at most this many finished approvals are kept, the newest
NOTES_NEEDED_SIZE_LIMIT_BYTES = None   # the most bytes the needed notes file holds; None: no limit of ours
NOTES_ROWS = {
    'NOTES_SIZE_LIMIT_BYTES': 'plugin_effect_notes.size_limit_bytes',
    'NOTES_KEEP_FOR_S': 'plugin_effect_notes.keep_for_s',
    'NOTES_KEEP_AT_MOST': 'plugin_effect_notes.keep_at_most',
    'NOTES_NEEDED_SIZE_LIMIT_BYTES': 'plugin_effect_notes.needed_size_limit_bytes',
}
_CGROUP_CAP = 64 * 1024
# Not an approver. approval.answer refuses it scope_refused before it reads the record.
_STRANGER = {'kind': 'plugin', 'id': 'plugin'}
MAX_MANIFEST_BYTES, MAX_ARTIFACT_BYTES, MAX_INSTALL_BYTES = 64 * 1024, 8 * 1024 * 1024, 1024 * 1024
VERIFICATION_UNAVAILABLE = 'plugin_verification_unavailable'
CRYPTOGRAPHY_MISSING = 'cryptography missing; install recipe in plugin_security/README.md'
EFFECT_PERMISSIONS = {'order': 'order'}
AUTHORITY_REQUIRED = frozenset({'order'})
_INSTALL_KEYS = frozenset({'trusted_keys', 'grants', 'reserved', 'authority_anchor'})
_GRANT_KEYS = frozenset({'plugin_id', 'artifact_sha256', 'signer_key_id', 'permissions', 'unsigned_order_acknowledged',
                         'order_after_revoked_key', 'order_after_revocation_generation'})
_SAFE_ID = re.compile(r'[^A-Za-z0-9_.-]')
# O7 reads (ADV2-3). Uncalibrated defaults: a plugin call that has not answered by its deadline is
# answered 504 and the plugin is marked degraded; a read that finds another one running waits this
# long for its turn, then answers busy. A timed-out call cannot be stopped in-process, so while it
# has not returned no new call goes into that plugin.
OPERATOR_DEADLINE_S = 10.0
OPERATOR_TURN_WAIT_S = 1.5
OPERATOR_TIMED_OUT = 'unavailable: operator plugin timed out'
# ADV2-7 (owner call Nov 11 2:20am PST): a load-time finding, not a refusal. Only plugin.py's
# bytes are digest- and signature-checked; whatever else it imports runs under the same grant.
UNVERIFIED_IMPORTS = 'plugin_imported_unverified_modules'
# Not reported: the daemon's own package and its signature backend, whose submodules the host's
# own signature check can import for the first time while a plugin loads.
_HOST_PACKAGES = frozenset({'recursion_core', 'cryptography'})


class PluginRefused(Exception):
    """A typed refusal from the daemon's side of plugin loading (files, configuration)."""

    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


def _finding(code, severity, detail=''):
    return {'stage': 'daemon', 'code': code, 'severity': severity, 'detail': detail}


def _first_imports(before, own=None):
    """Non-stdlib modules in ``sys.modules`` now that were not in ``before``, sorted.

    Left out: the standard library (top-level name in ``sys.stdlib_module_names``), the host's
    packages (``_HOST_PACKAGES``), the plugin's own module object ``own`` under any name, and a
    submodule whose parent package is itself listed (the package's entry covers it). A module that
    was already imported before the load is not listed: its code did not first run for this plugin.

    A finding, not a control: it reads names. A module named like a standard-library one, an import
    the plugin removes from ``sys.modules`` again, and an import made later inside a call are not
    seen, and an import made by another daemon thread during the load is attributed to this plugin.
    """
    fresh = {name for name, module in list(sys.modules.items())
             if name not in before and module is not None and module is not own
             and name.partition('.')[0] not in sys.stdlib_module_names
             and name.partition('.')[0] not in _HOST_PACKAGES}
    return sorted(name for name in fresh
                  if not any(name[:i] in fresh for i, char in enumerate(name) if char == '.'))


def _read_owned(path, limit, *, missing_ok=False):
    """Bytes of an operator-owned regular file nobody else can write, or a typed refusal."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PluginRefused('plugin_file_missing', Path(path).name) from None
    except OSError as exc:
        raise PluginRefused('plugin_file_unreadable', f'{Path(path).name}: {type(exc).__name__}') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PluginRefused('plugin_file_untrusted',
                                f'{Path(path).name}: must be an operator-owned regular file, not group/other writable')
        if info.st_size > limit:
            raise PluginRefused('plugin_file_too_large', Path(path).name)
        with os.fdopen(os.dup(fd), 'rb') as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def _owned_directory(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


def read_install(directory):
    """Parse ``install.json``. With no file: no trusted key and no grant, so nothing gets a permission."""
    raw = _read_owned(Path(directory) / INSTALL_FILE, MAX_INSTALL_BYTES, missing_ok=True)
    if raw is None:
        return {'trusted_keys': {}, 'grants': {}, 'reserved': ReservedEffects(), 'authority_anchor': None}
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, RecursionError):
        raise PluginRefused('plugin_install_config_invalid', 'not JSON') from None
    if not isinstance(data, dict) or set(data) - _INSTALL_KEYS:
        raise PluginRefused('plugin_install_config_invalid', 'unknown field')
    keys = data.get('trusted_keys', {})
    if not isinstance(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
        raise PluginRefused('plugin_install_config_invalid', 'trusted_keys')
    grants = {}
    listed = data.get('grants', [])
    if not isinstance(listed, list):
        raise PluginRefused('plugin_install_config_invalid', 'grants')
    for item in listed:
        if not isinstance(item, dict) or set(item) - _GRANT_KEYS or not {'plugin_id', 'artifact_sha256',
                                                                           'permissions'} <= set(item):
            raise PluginRefused('plugin_install_config_invalid', 'grant fields')
        perms, signer = item['permissions'], item.get('signer_key_id')
        generation = item.get('order_after_revocation_generation')
        if (not isinstance(item['plugin_id'], str) or not isinstance(item['artifact_sha256'], str)
                or not isinstance(perms, list) or any(p not in PERMISSIONS for p in perms)
                or not (signer is None or isinstance(signer, str))
                or type(item.get('unsigned_order_acknowledged', False)) is not bool
                or not (item.get('order_after_revoked_key') is None or isinstance(item['order_after_revoked_key'], str))
                or not (generation is None or type(generation) is int)
                or item['plugin_id'] in grants):
            raise PluginRefused('plugin_install_config_invalid', 'grant value')
        grants[item['plugin_id']] = Grant(item['plugin_id'], item['artifact_sha256'], signer, tuple(perms),
                                          item.get('unsigned_order_acknowledged', False),
                                          item.get('order_after_revoked_key'), generation)
    reserved = data.get('reserved', {})
    hosts = reserved.get('hosts', []) if isinstance(reserved, dict) else None
    prefixes = reserved.get('credential_prefixes', []) if isinstance(reserved, dict) else None
    if (not isinstance(reserved, dict) or set(reserved) - {'hosts', 'credential_prefixes'}
            or not isinstance(hosts, list) or not isinstance(prefixes, list)
            or not all(isinstance(v, str) and v for v in [*hosts, *prefixes])):
        raise PluginRefused('plugin_install_config_invalid', 'reserved')
    anchor = data.get('authority_anchor')
    if not (anchor is None or isinstance(anchor, str)):
        raise PluginRefused('plugin_install_config_invalid', 'authority_anchor')
    return {'trusted_keys': keys, 'grants': grants,
            'reserved': ReservedEffects(frozenset(hosts), tuple(prefixes)), 'authority_anchor': anchor}


class OwnerSocketPeer:
    """A peer the daemon admitted on its owner-only socket.

    A plugin's canonical JSON cannot be this object, so a plugin cannot present itself as the owner.
    """

    __slots__ = ('uid', 'pid')

    def __init__(self, uid, pid):
        self.uid, self.pid = uid, pid


class DevicePresentation:
    """A device token presented to the daemon. A plugin's canonical JSON cannot be this object."""

    __slots__ = ('token',)

    def __init__(self, token):
        self.token = token


def _command_id():
    return 'cmd' + secrets.token_hex(8)


def _peer_in_run_unit(pid):
    """True when ``pid`` is inside a ``recursion-run-*`` cgroup, or that cgroup cannot be read.

    The same fail-closed rule as the control socket: an unreadable cgroup is not the owner. Kept
    here so this module does not import the control adapter (that adapter imports ``PluginHost``).
    """
    if type(pid) is not int or pid <= 0:
        return True
    try:
        fd = os.open(f'/proc/{pid}/cgroup', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return True
    try:
        info = os.fstat(fd)
        # procfs reports size 0 for a file that still has bytes. Read a capped amount.
        if not stat.S_ISREG(info.st_mode):
            return True
        raw = os.read(fd, _CGROUP_CAP + 1)
    except OSError:
        return True
    finally:
        os.close(fd)
    if len(raw) > _CGROUP_CAP:
        return True
    text = raw.decode('utf-8', 'replace')
    return any('recursion-run-' in part for line in text.splitlines() for part in line.split('/'))


def _device_token_bytes(path):
    """The owner-only device token, without its one trailing newline, or None when it cannot be used."""
    if path is None:
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        if not isinstance(runtime, str) or not runtime or '\x00' in runtime:
            return None
        path = Path(runtime) / 'recursion' / 'gui-device-token'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= _TOKEN_CAP):
            return None
        raw = os.read(fd, _TOKEN_CAP)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) != info.st_size:
        return None
    if raw.endswith(b'\n'):
        raw = raw[:-1]
    if not raw or b'\n' in raw:
        return None
    return raw


def _token_matches(presented, path):
    if type(presented) is not str or not presented:
        return False
    try:
        offered = presented.encode('utf-8')
    except UnicodeError:
        return False
    held = _device_token_bytes(path)
    if held is None or len(offered) != len(held):
        return False
    return hmac.compare_digest(offered, held)


class _ApprovalCall:
    """The slice of a method context ``approval.answer`` reads: principal, transaction, after commit."""

    __slots__ = ('principal', 'tx', 'after_commit')

    def __init__(self, principal, tx, after_commit):
        self.principal, self.tx, self.after_commit = principal, tx, after_commit


class _EffectReceipts:
    """Receipts this process has already been given. The notes file is what a later call reads."""

    def __init__(self):
        self._memory = {}


class _EffectNotes:
    """Owner-only JSON of the envelopes and receipts for plugin-effect charges, in two files.

    Same-uid limit, as on the ledger: a plugin in this process can rewrite these files. They are not a
    boundary against that. They are what a later call in this uid reads so ``effect_replayed`` can
    return the receipt the first charge produced, without a second spend.

    The needed file (``needed_path``) holds the notes still needed for a carry: an approval approved
    whose charge has not finished (not charged yet, or charged and not yet consumed). Nothing prunes
    them, and the file has no size figure of ours: ``NOTES_NEEDED_SIZE_LIMIT_BYTES`` ships as None
    ('no limit'); only when the person sets that row is a write refused, with words that name the
    file, how many approvals hold it, why they stay and what the person can do.

    The finished file (``path``) holds the rest. ``consumed`` maps an approval id to
    ``[time, sequence]``: when its approval was seen consumed (or no longer approved). ``refused`` maps
    one to what happened to it: ``answered`` 'approve', ``carried`` False, ``code`` (the refusing
    check's own code), ``at`` and ``sequence``. Every write prunes those first, in the order seen:
    past ``NOTES_KEEP_FOR_S``, then past ``NOTES_KEEP_AT_MOST``, then, while the finished file would
    pass ``NOTES_SIZE_LIMIT_BYTES``, the oldest; so the finished file never refuses an approval.
    """

    _REFUSED_KEYS = frozenset({'answered', 'carried', 'code', 'at', 'sequence'})

    def __init__(self, path, *, needed_path=None, clock=time.time, states=None):
        self.path = Path(path)
        self.needed_path = (Path(needed_path) if needed_path is not None
                            else self.path.with_name(self.path.stem + '-needed' + self.path.suffix))
        self._lock = threading.Lock()
        self._clock = clock
        self._states = states  # approval ids -> their queue state; None: nothing is taken as consumed

    def read(self):
        with self._lock:
            return self._read()

    def update(self, mutator, *, charged=False):
        """Change the notes, prune, write. Returns the approval ids whose notes were pruned."""
        with self._lock:
            data = self._read()
            mutator(data)
            dropped = self._prune(data)
            self._write(data, charged=charged)
            return dropped

    def _read(self):
        """Both files as one: the finished file, with the needed file's notes added."""
        data = self._read_one(self.path, finished=True)
        needed = self._read_one(self.needed_path, finished=False)
        for key in ('envelopes', 'receipts'):
            for approval_id, value in needed[key].items():
                data[key].setdefault(approval_id, value)
        return data

    def _read_one(self, path, *, finished):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {'envelopes': {}, 'receipts': {}, 'consumed': {}, 'refused': {}}
        except OSError as exc:
            raise Refused('approval_note_unreadable', type(exc).__name__) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise Refused('approval_note_untrusted')
            # No size refusal here: a file past the size figure (one written before the figure was
            # lowered) is pruned down at the next write instead of refusing every approval.
            chunks, left = [], info.st_size + 1
            while left > 0:
                chunk = os.read(fd, left)
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            raw = b''.join(chunks)
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise Refused('approval_note_unreadable', type(exc).__name__) from None
        keys = set(data) if type(data) is dict else None
        shapes = (({'envelopes', 'receipts'}, {'envelopes', 'receipts', 'consumed'},
                   {'envelopes', 'receipts', 'consumed', 'refused'}) if finished else ({'envelopes', 'receipts'},))
        if keys not in shapes or any(type(data[key]) is not dict for key in keys):
            raise Refused('approval_note_unreadable', 'shape')
        data.setdefault('consumed', {})
        data.setdefault('refused', {})
        if not all(type(seen) is list and len(seen) == 2 and all(type(v) in (int, float) for v in seen)
                   for seen in data['consumed'].values()):
            raise Refused('approval_note_unreadable', 'shape')
        if not all(type(entry) is dict and set(entry) == self._REFUSED_KEYS and entry['answered'] == 'approve'
                   and entry['carried'] is False and type(entry['code']) is str
                   and type(entry['at']) in (int, float) and type(entry['sequence']) is int
                   for entry in data['refused'].values()):
            raise Refused('approval_note_unreadable', 'shape')
        return data

    @staticmethod
    def _payload(data):
        return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def _split(data):
        """The finished file's content and the needed file's content."""
        finished = set(data['consumed']) | set(data['refused'])
        done = {key: {k: v for k, v in data[key].items() if k in finished} for key in ('envelopes', 'receipts')}
        done.update(consumed=data['consumed'], refused=data['refused'])
        needed = {key: {k: v for k, v in data[key].items() if k not in finished} for key in ('envelopes', 'receipts')}
        return done, needed

    @staticmethod
    def _entry_bytes(data, approval_id):
        return sum(len(json.dumps({approval_id: data[key][approval_id]}, separators=(',', ':'))) - 1
                   for key in ('envelopes', 'receipts', 'consumed', 'refused') if approval_id in data[key])

    @staticmethod
    def _forget(data, approval_id):
        for key in ('envelopes', 'receipts', 'consumed', 'refused'):
            data[key].pop(approval_id, None)

    @staticmethod
    def _seen(data, approval_id):
        """``[time, sequence]`` of a finished approval."""
        if approval_id in data['consumed']:
            return data['consumed'][approval_id]
        entry = data['refused'][approval_id]
        return [entry['at'], entry['sequence']]

    @staticmethod
    def _next_sequence(data):
        return max([seen[1] for seen in data['consumed'].values()]
                   + [entry['sequence'] for entry in data['refused'].values()], default=0) + 1

    def mark_consumed(self, data, approval_id):
        """Record that ``approval_id`` was seen consumed now, after every earlier finished one."""
        if approval_id in data['consumed']:
            return
        sequence = self._next_sequence(data)
        data['refused'].pop(approval_id, None)
        data['consumed'][approval_id] = [self._clock(), sequence]

    def mark_refused(self, data, approval_id, code):
        """Record that ``approval_id`` was approved and not carried out: its charge was refused ``code``."""
        if approval_id in data['consumed']:
            return
        data['refused'][approval_id] = {'answered': 'approve', 'carried': False, 'code': code,
                                        'at': self._clock(), 'sequence': self._next_sequence(data)}

    @staticmethod
    def clear_refused(data, approval_id):
        data['refused'].pop(approval_id, None)

    def _prune(self, data):
        envelopes, receipts, consumed, refused = data['envelopes'], data['receipts'], data['consumed'], data['refused']
        held = set(envelopes) | set(receipts)
        for gone in [approval_id for approval_id in consumed if approval_id not in held]:
            del consumed[gone]
        unseen = sorted(held - set(consumed) - set(refused))
        if unseen and self._states is not None:
            states = self._states(unseen)
            for approval_id in unseen:
                if approval_id in states and states[approval_id] != 'approved':
                    self.mark_consumed(data, approval_id)  # consumed, or gone: no carry reads it again
        now = self._clock()
        finished = sorted(set(consumed) | set(refused),
                          key=lambda approval_id: (self._seen(data, approval_id)[1], approval_id))
        keep_for, keep_at_most, limit = NOTES_KEEP_FOR_S, NOTES_KEEP_AT_MOST, NOTES_SIZE_LIMIT_BYTES
        dropped = []
        if keep_for is not None:
            dropped += [approval_id for approval_id in finished if now - self._seen(data, approval_id)[0] >= keep_for]
            finished = [approval_id for approval_id in finished if approval_id not in set(dropped)]
        if keep_at_most is not None and len(finished) > keep_at_most:
            cut = len(finished) - keep_at_most
            dropped += finished[:cut]
            finished = finished[cut:]
        for approval_id in dropped:
            self._forget(data, approval_id)
        if limit is not None:
            size = len(self._payload(self._split(data)[0]))
            while finished and size > limit:
                approval_id = finished.pop(0)
                size -= self._entry_bytes(data, approval_id)
                self._forget(data, approval_id)
                dropped.append(approval_id)
                if size <= limit:
                    size = len(self._payload(self._split(data)[0]))  # exact again before it stops
        return dropped

    def _write(self, data, *, charged=False):
        directory = private_dir(self.path.parent)
        done, needed = self._split(data)
        needed_payload = self._payload(needed)
        limit = NOTES_NEEDED_SIZE_LIMIT_BYTES
        if limit is not None and len(needed_payload) > limit:
            row = NOTES_ROWS['NOTES_NEEDED_SIZE_LIMIT_BYTES']
            awaiting = len(set(needed['envelopes']) | set(needed['receipts']))
            if charged:
                tail = ('the charge was made and stands in the authority ledger, and its receipt is held in this '
                        f"process only, so do not request the effect again; set the row {row} to 'no limit' (its "
                        'shipped default) or to a larger figure so its receipt is kept')
            else:
                tail = (f"nothing was charged; set the row {row} to 'no limit' (its shipped default) or to a "
                        'larger figure, then request the effect again')
            raise Refused('approval_notes_full',
                          f'the file of approval notes still needed, {self.needed_path}, is at its size row {row} of '
                          f'{limit} bytes: {awaiting} approval(s) were approved and their charge has not finished (not '
                          'charged yet, or charged and not yet consumed); their notes cannot be dropped, because a '
                          "later carry of the same approval reads them to return the first charge's receipt instead "
                          f'of charging again; {tail}')
        self._replace(directory, self.path, self._payload(done))
        self._replace(directory, self.needed_path, needed_payload)

    @staticmethod
    def _replace(directory, path, payload):
        tmp = directory / (path.name + '.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if not written:
                    raise Refused('approval_note_unreadable', 'short write')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# What a person reads when an approved charge is stopped: what stopped it, that nothing was charged,
# and what to do next. The code stays the refusing check's own; ``{detail}`` is that check's detail.
_RUN_NOT_BOUND = ('the run named by the effect is not bound to this plugin on this connection; nothing was '
                  'charged; bind the run, then request the effect again')
_CHARGE_STOPPED = {
    'core_poisoned_restart_required': 'the plugin core is poisoned ({detail}), so no plugin effect can be charged; '
                                      'nothing was charged; restart the daemon, then request the effect again',
    'permission_required': "the plugin does not hold the '{detail}' permission now (its signing key was revoked, or "
                           'its grant does not give it); nothing was charged; install a grant that gives it again, '
                           'then request the effect again',
    'run_faulted': 'the run was halted by a run fault; nothing was charged; start a new run, then request the '
                   'effect again',
    'connection_tripped': "the connection's breaker tripped before or during this charge; nothing was charged; "
                          'reset the connection with a leased probe, then request the effect again',
    'run_unknown': _RUN_NOT_BOUND, 'run_not_owned': _RUN_NOT_BOUND, 'run_connection_mismatch': _RUN_NOT_BOUND,
    'approval_not_approved': 'the approval is no longer approved (it is {detail} now); nothing was charged; '
                             'request the effect again if it is still wanted',
    'core_unavailable': 'no plugin core is composed in this daemon (it has no breaker store), so no plugin effect '
                        'can be charged; nothing was charged; restart the daemon once its state directory opens',
    'authority_unconfigured': 'no authority ledger is installed, so no order can be charged; nothing was charged; '
                              'create the ledger with the installer, then request the effect again',
    'effect_replayed': 'this approval was charged earlier and its receipt is not held here; nothing was charged '
                       'again; read the charge in the authority ledger',
}
_CHARGE_STOPPED_OTHER = ('the charge was refused ({code}{detail}); nothing was charged; correct what the code names, '
                         'then request the effect again')


def _charge_stopped(refused):
    """The same refusal, its code unchanged, with the words a person reads."""
    detail = refused.detail or ''
    words = _CHARGE_STOPPED.get(refused.code)
    if words is None:
        words = _CHARGE_STOPPED_OTHER.format(code=refused.code, detail=': ' + detail if detail else '')
    else:
        words = words.format(detail=detail or 'unknown')
    return Refused(refused.code, words)


class _QueuedApproval:
    """The approval queue's record of one approved plugin effect, as the core's one charge path carries it.

    ``take`` reads the record after the core's checks; ``recheck`` reads it again inside the commit
    fence, so a record that stopped being approved in between is not charged. ``consumed`` passes
    as well: the sink consumes only after a charge, so the ledger then refuses ``effect_replayed``
    and the stored receipt is returned (a later process carrying it again).
    """

    __slots__ = ('_queue', '_approval_id', '_seen')

    def __init__(self, queue):
        self._queue = queue
        self._approval_id = self._seen = None

    def _read(self):
        from recursion_core.approvals import store as approval_rows
        from recursion_core.approvals.model import ApprovalRefused
        store = self._queue.store
        if store is None:
            raise Refused('approval_not_approved', 'unreadable: no approval store')
        try:
            with store.transaction() as tx:
                record = approval_rows.load(tx, self._approval_id)
        except ApprovalRefused as refused:  # a record that does not read whole is not an approved one
            raise Refused('approval_not_approved', f'unreadable: {refused.code}') from None
        state = record.get('state') if type(record) is dict else None
        if state not in ('approved', 'consumed'):
            raise Refused('approval_not_approved', state if type(state) is str else 'unknown')
        return record['revision'], record['digest']

    def take(self, approval_id, _effect):
        self._approval_id = approval_id
        self._seen = self._read()
        return self._seen[1]

    def recheck(self):
        if self._read() != self._seen:
            raise Refused('approval_not_approved', 'changed')


class PluginEffectApprover:
    """The daemon's owner, or the device token, answering a plugin effect through ``approval.answer``.

    ``authenticate`` is the in-memory gate's authenticator. It returns None for every credential,
    proof objects included, so that gate still names no principal and cannot mark an effect
    approved with nothing charged. ``answer`` is the path that charges.
    """

    def __init__(self, host, queue, notes_path):
        from recursion_core.approvals.sink import PluginEffectSink
        self._host, self._queue = host, queue
        self._notes = _EffectNotes(notes_path, needed_path=Path(notes_path).with_name(EFFECT_NOTES_NEEDED),
                                   clock=lambda: host._clock(), states=self._record_states)
        self._envelopes = {}
        self._receipts = _EffectReceipts()
        self.sink = PluginEffectSink(queue, self.charge)

    def authenticate(self, _credential):
        return None

    def request(self, effect):
        return self.sink.request(effect)

    def answer(self, peer, approval_id, effect, *, authority, answer='approve'):
        admitted = self._admit(peer)
        if admitted is None:
            return self._method('approval.answer', _STRANGER, {
                'command_id': _command_id(), 'approval_id': approval_id, 'surface': 'cli',
                'expected_revision': 1, 'digest': '0' * 64, 'answer': 'deny'})
        principal, surface = admitted
        opened = self._method('approval.open', principal, {
            'command_id': _command_id(), 'approval_id': approval_id, 'surface': surface})
        if opened.get('code') != 'ok':
            return opened
        shown = opened['approval']
        answered = self._method('approval.answer', principal, {
            'command_id': _command_id(), 'approval_id': approval_id, 'surface': surface,
            'expected_revision': shown['revision'], 'digest': shown['digest'], 'answer': answer})
        if answered.get('code') == 'conflict' and answered.get('detail') in ('already_resolved', 'already_consumed'):
            from recursion_core.approvals.model import ApprovalRefused
            raise ApprovalRefused(answered['detail'])
        if answer != 'approve' or answered.get('code') != 'ok':
            return answered
        try:
            self._note_envelope(approval_id, authority)
        except Refused as refused:
            self._mark_refused(approval_id, refused.code)  # approved, and not carried out: the notes say so
            raise
        carried = self.sink.carry(approval_id, effect)
        try:
            self._update(lambda data: self._notes.mark_consumed(data, approval_id))
        except (Refused, OSError) as refused:  # charged and consumed already; a later write marks it from the queue
            log.warning('approval %s was carried; its notes were not marked consumed: %s', approval_id,
                        getattr(refused, 'code', type(refused).__name__))
        return carried

    def _record_states(self, approval_ids):
        """The queue state of each approval id that reads whole; a missing record reads 'missing'."""
        from recursion_core.approvals import store as approval_rows
        from recursion_core.approvals.model import ApprovalRefused
        store = self._queue.store
        if store is None:
            return {}
        states = {}
        try:
            with store.transaction() as tx:
                for approval_id in approval_ids:
                    try:
                        record = approval_rows.load(tx, approval_id)
                    except ApprovalRefused:
                        continue  # unreadable: not taken as consumed, so it is kept
                    states[approval_id] = record.get('state') if type(record) is dict else 'missing'
        except Exception as exc:  # noqa: BLE001 - an unreadable store prunes nothing it cannot see
            log.warning('approval states unreadable for the notes file: %s', type(exc).__name__)
            return {}
        return states

    def _mark_refused(self, approval_id, code):
        """Note that this approval was given and not carried out, refused with ``code``.

        Its notes are then finished ones, pruned by the person's rows. The queue record is not changed:
        it stays approved, so a carry of the same approval can still be tried.
        """
        try:
            self._update(lambda data: self._notes.mark_refused(data, approval_id, code))
        except (Refused, OSError) as failed:  # the refusal itself still reaches the person
            log.warning('approval %s was refused %s; the notes could not record it: %s', approval_id, code,
                        getattr(failed, 'code', type(failed).__name__))

    def _update(self, mutator, *, charged=False):
        for approval_id in self._notes.update(mutator, charged=charged):
            self._envelopes.pop(approval_id, None)
            self._receipts._memory.pop(approval_id, None)

    def notes_status(self):
        """The notes file's figures ('no limit' for None) and how many approvals' notes it holds."""
        shown = {'path': str(self._notes.path), 'needed_path': str(self._notes.needed_path),
                 'rows': dict(NOTES_ROWS)}
        for name, value in (('size_limit_bytes', NOTES_SIZE_LIMIT_BYTES), ('keep_for_s', NOTES_KEEP_FOR_S),
                            ('keep_at_most', NOTES_KEEP_AT_MOST),
                            ('needed_size_limit_bytes', NOTES_NEEDED_SIZE_LIMIT_BYTES)):
            shown[name] = 'no limit' if value is None else value
        try:
            data = self._notes.read()
        except Refused as refused:
            shown.update(held=None, awaiting_carry=None, refused=None, unreadable=refused.code)
            return shown
        held = set(data['envelopes']) | set(data['receipts'])
        shown.update(held=len(held), awaiting_carry=len(held - set(data['consumed']) - set(data['refused'])),
                     refused=len(data['refused']))
        return shown

    def _admit(self, peer):
        # Exact type, not a subclass: a plugin cannot widen admission by inheriting the proof.
        if type(peer) is OwnerSocketPeer:
            if type(peer.uid) is not int or peer.uid != self._host._owner_uid or _peer_in_run_unit(peer.pid):
                return None
            return {'kind': 'owner', 'id': 'local'}, 'cli'
        if type(peer) is DevicePresentation and _token_matches(peer.token, self._host._device_token_path):
            return {'kind': 'device', 'id': 'page'}, 'page'
        return None

    def _method(self, name, principal, params):
        from recursion_core.control.methods import NotAccepted
        from recursion_core.control.methods.approval import handlers
        handler = handlers(self._queue)[name]
        callbacks = []

        def after(fn):
            callbacks.append(fn)

        try:
            with self._queue.store.transaction() as tx:
                result = handler(_ApprovalCall(principal, tx, after), params)
        except NotAccepted as refused:
            return refused.answer
        for fn in callbacks:
            fn()
        return result

    def _note_envelope(self, approval_id, authority):
        # A copy the file can store. The charge reads it back after this process forgets the dict.
        held = json.loads(json.dumps(authority)) if type(authority) is dict else None
        self._envelopes[approval_id] = held

        def put(data):
            data['envelopes'][approval_id] = held
            self._notes.clear_refused(data, approval_id)  # a new carry of it is about to be tried

        self._update(put)

    def _envelope_for(self, approval_id):
        if approval_id in self._envelopes:
            return self._envelopes[approval_id]
        data = self._notes.read()
        if approval_id not in data['envelopes']:
            return None
        self._envelopes[approval_id] = data['envelopes'][approval_id]
        return self._envelopes[approval_id]

    def _receipt_for(self, approval_id):
        if approval_id in self._receipts._memory:
            return self._receipts._memory[approval_id]
        data = self._notes.read()
        if approval_id not in data['receipts']:
            return None
        self._receipts._memory[approval_id] = data['receipts'][approval_id]
        return self._receipts._memory[approval_id]

    def _remember_receipt(self, approval_id, receipt):
        # Memory first: a write that fails still answers a replay in this process.
        self._receipts._memory[approval_id] = receipt

        def put(data):
            data['receipts'][approval_id] = receipt
            self._notes.clear_refused(data, approval_id)  # charged: no longer a refused one

        self._update(put, charged=True)

    def charge(self, effect, approval_id):
        """Charge this approval through the core's one charge path (``CoreService.authorize_effect``).

        Every refusal of a check is raised as itself (its code), with words that say what stopped the
        charge and that nothing was charged. ``effect_replayed`` returns the receipt already stored.
        """
        core = self._host.core
        if core is None:
            self._mark_refused(approval_id, 'core_unavailable')
            raise _charge_stopped(Refused('core_unavailable'))
        envelope = self._envelope_for(approval_id)
        try:
            outcome = core.authorize_effect(effect, approval_id, envelope, approval=_QueuedApproval(self._queue))
        except Refused as refused:
            if refused.code == 'effect_replayed':
                prior = self._receipt_for(approval_id)
                if prior is not None:
                    return prior
            else:  # the charge was not made: approved, and not carried out
                self._mark_refused(approval_id, refused.code)
            raise _charge_stopped(refused) from None
        receipt = outcome['charge']
        self._remember_receipt(approval_id, receipt)
        return receipt


class PluginHost:
    """What the running daemon holds of its operator plugins: the inventory, the panels, the O7 reader."""

    def __init__(self, config_dir, state_dir, breaker=None, *, plugin_dirs=(), verification_available=None,
                 gil_probe=None, clock=time.time, operator_deadline_s=OPERATOR_DEADLINE_S,
                 operator_turn_wait_s=OPERATOR_TURN_WAIT_S, device_token_path=None, owner_uid=None):
        require_launcher_platform()  # Windows: typed PlatformUnsupported; no plugin file is opened
        self._config, self._state = Path(config_dir) / PLUGINS_DIR, Path(state_dir)
        self._gil = gil_probe if gil_probe is not None else GilProbe()
        self._clock = clock
        # One operator() call at a time: the plugin is in-process code of unknown thread safety.
        self._turn = threading.Condition()
        self._busy = False
        self._stuck = None  # plugin id whose call passed its deadline and has not returned
        self._operator_deadline_s, self._operator_turn_wait_s = operator_deadline_s, operator_turn_wait_s
        self.degraded = {}  # plugin id -> operator() calls that passed their deadline
        self.health = CoreHealth()
        self.loader = self.core = self.gate = self.authority = self._trust = None
        self._approver = self.effect_sink = self._approval_queue = self._approval_store = None
        self._breaker = None
        self._device_token_path = device_token_path
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.records, self.registries = [], ()
        self.pending_approvals = collections.deque(maxlen=64)  # (approval id, digest) drawn, newest last
        self._operators = {}
        self._panels = []
        self.authority_state = 'not configured'
        available = backend_available() if verification_available is None else verification_available()
        self.verification = 'available' if available else f'unavailable: {CRYPTOGRAPHY_MISSING}'
        sources = self._sources(plugin_dirs)
        if not sources:
            return
        if not available:
            # No signature can be checked. Loading anyway, even as "unsigned", would be an
            # unverified load: refuse every plugin and say why.
            log.error('%s: %s; %d plugin(s) refused, none loaded', VERIFICATION_UNAVAILABLE, CRYPTOGRAPHY_MISSING,
                      len(sources))
            self._refuse_all(sources, VERIFICATION_UNAVAILABLE, CRYPTOGRAPHY_MISSING)
            return
        try:
            install = read_install(self._config)
            self._trust = TrustStore(install['trusted_keys'], revocation_store=self._revocation_store())
        except (PluginRefused, Refused) as exc:
            log.error('no plugin can load: %s', exc)
            self._refuse_all(sources, exc.code, exc.detail)
            return
        except (OSError, sqlite3.Error) as exc:
            log.error('no plugin can load: revocation_store_invalid: %s', type(exc).__name__)
            self._refuse_all(sources, 'revocation_store_invalid', type(exc).__name__)
            return
        self.loader = PluginLoader(self._trust, install['grants'], install['reserved'], self.health, self._gil)
        self._compose_core(install, breaker)
        for source in sources:
            self.records.append(self._load(source))
        self.registries = tuple(self._registries)

    # -- discovery ------------------------------------------------------------------------------
    def _sources(self, plugin_dirs):
        found = []
        if self._config.is_dir():
            if not _owned_directory(self._config):
                log.error('plugin directory %s is not operator-owned or is group/other writable; ignored', self._config)
                self.records.append({'source': str(self._config), 'plugin_id': None, 'state': 'refused',
                                     'code': 'plugin_directory_untrusted', 'detail': 'install directory', 'findings': []})
            else:
                found += sorted(p for p in self._config.iterdir() if p.is_dir() or p.is_symlink())
        return found + [Path(p) for p in plugin_dirs]

    def _refuse_all(self, sources, code, detail):
        for source in sources:
            self.records.append({'source': str(source), 'plugin_id': None, 'state': 'refused', 'code': code,
                                 'detail': detail, 'findings': [_finding(code, 'refuse', detail)]})

    def _revocation_store(self):
        path = private_dir(self._state / STORE_DIR) / REVOCATION_FILE
        if not os.path.lexists(path):
            create_revocation_store(str(path))
        return str(path)

    def _compose_core(self, install, breaker):
        # The in-memory gate still names no principal. PluginEffectApprover.authenticate returns
        # None for every credential, including an owner-socket peer and a device token, so a
        # plugin's JSON cannot become operator_ui and nothing is marked approved here before a
        # charge. The owner and the device answer through approval.answer and PluginEffectSink.carry.
        self._breaker = breaker
        self._approver = self._open_effect_approver() if breaker is not None else None
        authenticator = self._approver.authenticate if self._approver is not None else (lambda _credential: None)
        self.gate = ApprovalGate(lambda approval_id, digest, _raw: self.pending_approvals.append((approval_id, digest)),
                                 authenticator, lambda: int(self._clock()))
        anchor = install['authority_anchor']
        if anchor is not None:
            try:
                self.authority = Authority(anchor, str(self._state / STORE_DIR / LEDGER_FILE), lambda: int(self._clock()))
                self.authority_state = 'configured'
            except Refused as exc:
                self.authority_state = f'unavailable: {exc.code}'
                log.error('authority profile is unavailable (%s); order effects refuse authority_unconfigured', exc.code)
        if breaker is None:
            return  # no durable breaker store: there is no core boundary to hand a plugin
        self.core = CoreService(loader=self.loader, gate=self.gate, breaker=breaker, health=self.health,
                                authority=self.authority, effect_permissions=EFFECT_PERMISSIONS,
                                authority_required=AUTHORITY_REQUIRED)

    def _open_effect_approver(self):
        """The approval queue and the plugin-effect sink, or None when they cannot be opened.

        The deadline worker is not started: a pending request is not expired on a timer by this
        host. Plugins still load when the store cannot be opened; nothing can then be approved.
        """
        store = None
        try:
            from recursion_core.approvals.queue import ApprovalQueue
            from recursion_core.events.db import EventStore
            directory = private_dir(self._state / STORE_DIR)
            store = EventStore(directory / APPROVAL_EVENTS, clock=self._clock)
            queue = ApprovalQueue(clock=self._clock)
            queue.attach(store)
            approver = PluginEffectApprover(self, queue, directory / EFFECT_NOTES)
        except Exception as exc:  # noqa: BLE001 - a store that will not open leaves the plugins loaded
            log.error('plugin effect approver is not composed: %s', type(exc).__name__)
            if store is not None:
                try:
                    store.close()
                except Exception as close_exc:  # noqa: BLE001 - the host still has its plugins
                    log.error('plugin effect approval store would not release: %s', type(close_exc).__name__)
            self._approval_queue = self._approval_store = self.effect_sink = None
            return None
        self._approval_queue, self._approval_store, self.effect_sink = queue, store, approver.sink
        return approver

    def request_plugin_effect(self, effect):
        """Record one plugin effect on the approval queue, bound to the effect's own run."""
        if self._approver is None:
            raise PluginRefused('approval_surface_unavailable', 'no approver is composed')
        return self._approver.request(effect)

    def answer_plugin_effect(self, peer, approval_id, effect, *, authority, answer='approve'):
        """Open and answer through ``approval.answer``. An approval is carried only after it is approved."""
        if self._approver is None:
            raise PluginRefused('approval_surface_unavailable', 'no approver is composed')
        return self._approver.answer(peer, approval_id, effect, authority=authority, answer=answer)

    # -- one plugin -----------------------------------------------------------------------------
    def _load(self, source):
        record = {'source': str(source), 'plugin_id': None, 'state': 'refused', 'code': None, 'detail': '',
                  'findings': []}
        # Loads are serial (the constructor's loop; the loader holds its own lock), so the modules
        # that appear between here and the end of this load were first imported for this plugin.
        before = set(sys.modules)
        try:
            if not _owned_directory(source):
                raise PluginRefused('plugin_directory_untrusted',
                                    'must be an operator-owned directory, not group/other writable')
            manifest = _read_owned(source / MANIFEST, MAX_MANIFEST_BYTES)
            artifact = _read_owned(source / ARTIFACT, MAX_ARTIFACT_BYTES)
            signature = _read_owned(source / SIGNATURE, 4096, missing_ok=True)
            signature = None if signature is None else signature.decode('ascii', 'replace').strip()
            loaded = self.loader.load(manifest, signature, artifact)
        except (PluginRefused, Refused) as exc:
            findings = list(self.loader.last_findings) if isinstance(exc, Refused) else []
            record.update(code=exc.code, detail=exc.detail,
                          findings=findings + self._import_findings(before)
                          + [_finding(exc.code, 'refuse', exc.detail)])
            manifest_ok = next((f for f in findings if f.get('code') == 'manifest_ok'), None)
            if manifest_ok is not None:
                record['plugin_id'] = manifest_ok.get('detail')
            log.error('plugin at %s refused: %s; the daemon runs without it', source, exc)
            return record
        record.update(plugin_id=loaded.plugin_id, state='loaded', code=None, trust=loaded.trust,
                      signer_key_id=loaded.signer_key_id, registry_label=loaded.registry_label,
                      permissions=list(loaded.permissions), artifact_sha256=loaded.artifact_sha256,
                      findings=list(loaded.findings), provides=[], containment='in process, same uid: not contained')
        self._attach(loaded, record)
        # After registry() and bind_core(), which are plugin code too. The plugin stays loaded.
        record['findings'] += self._import_findings(before, loaded.module)
        log.info('plugin %s loaded through the plugin loader: trust=%s permissions=%s provides=%s',
                 loaded.plugin_id, loaded.trust, ','.join(loaded.permissions) or '-', ','.join(record['provides']) or '-')
        return record

    @staticmethod
    def _import_findings(before, own=None):
        names = _first_imports(before, own)
        return [_finding(UNVERIFIED_IMPORTS, 'warn', ','.join(names))] if names else []

    @property
    def _registries(self):
        return [entry for _, entry in self._panels]

    def _attach(self, loaded, record):
        """Take what the plugin offers, each part only with its permission. Plugin code runs here."""
        module, perms = loaded.module, loaded.permissions
        factory = getattr(module, 'registry', None)
        if callable(factory):
            if 'panel' not in perms:
                record['findings'].append(_finding('panel_permission_required', 'warn', 'registry() not read'))
            else:
                try:
                    snapshot = _snapshot(self._guarded(factory))
                    required = snapshot.get('required', {})
                    if not all(isinstance(cls, str) and all(isinstance(s, str) for s in subjects)
                               for cls, subjects in required.items()):
                        raise ValueError('required must map a class to subject strings')
                    snapshot = {key: snapshot[key] for key in ('panels', 'required') if key in snapshot}
                    # The GUI's own validation, over every panel already accepted plus these.
                    panel_registry([p for _, entry in self._panels for p in entry.get('panels', ())]
                                   + list(snapshot.get('panels', ())))
                except BaseException as exc:  # noqa: BLE001 - a bad registry costs the plugin its panels, not the daemon
                    record['findings'].append(_finding('plugin_registry_invalid', 'refuse',
                                                       f'{type(exc).__name__}: panels not registered'))
                else:
                    self._panels.append((loaded.plugin_id, snapshot))
                    record['provides'].append('panels')
        handler = getattr(module, 'operator', None)
        if callable(handler):
            if 'read' not in perms:
                record['findings'].append(_finding('read_permission_required', 'warn', 'operator() not used'))
            else:
                self._operators[loaded.plugin_id] = handler
                record['provides'].append('operator_reads')
        bind = getattr(module, 'bind_core', None)
        if callable(bind) and self.core is not None:
            try:
                self._guarded(bind, self.core.client_for(loaded.plugin_id))
                record['provides'].append('core_client')
            except BaseException as exc:  # noqa: BLE001 - SystemExit or KeyboardInterrupt included (_guarded)
                record['findings'].append(_finding('plugin_bind_core_failed', 'warn', type(exc).__name__))

    def _guarded(self, call, *args):
        """Run plugin code, then read the GIL: a plugin that turned it on poisons the core (4.1).

        Callers catch ``BaseException`` from plugin code, ``SystemExit`` and ``KeyboardInterrupt``
        included: they cost the plugin, not the daemon (ADV2-4). Nothing the daemon needs for its
        own shutdown is raised there: SIGTERM and SIGINT only set the main loop's stop event
        (``daemon/__main__.py``), as the loader's import step already assumes.
        """
        try:
            return call(*args)
        finally:
            if self._gil.gil_enabled():
                self.health.poison('gil_enabled_after_plugin_call')

    # -- reads ----------------------------------------------------------------------------------
    def inventory(self):
        """Every installed plugin, loaded or refused, with its findings. Plain JSON values."""
        plugins = []
        for record in self.records:
            item = dict(record)
            if item['state'] == 'loaded' and self.loader is not None:
                item['permissions'] = list(self.loader.permissions(item['plugin_id']))  # after a revoke or a poison
                item.update(self._degraded_fields(item['plugin_id']))
            plugins.append(item)
        return {'plugins': plugins, 'verification': self.verification, 'core_poisoned': self.health.poisoned,
                'authority_profile': self.authority_state, 'approvals_drawn': len(self.pending_approvals),
                'effect_notes': self._approver.notes_status() if self._approver is not None else None,
                'approval_surface': ('owner socket or device token, through approval.answer'
                                     if self._approver is not None else
                                     'none: no surface can approve or veto yet, so no effect is authorized'),
                'containment': 'none: plugins run in the daemon process and uid'}

    def route_records(self):
        """The plugin rows of the GUI's inventory: what loaded, what was refused and why."""
        rows = []
        for record in self.records:
            name = record['plugin_id'] or Path(record['source']).name
            if record['state'] == 'loaded':
                note = 'loaded %s; permissions: %s; provides: %s; in process, not contained' % (
                    record['trust'], ', '.join(self.loader.permissions(record['plugin_id'])) or 'none',
                    ', '.join(record['provides']) or 'nothing')
                degraded = self._degraded_fields(record['plugin_id'])
                if degraded:
                    note += '; degraded: ' + degraded['degraded']
            else:
                note = 'refused: %s%s' % (record['code'], ': ' + record['detail'] if record['detail'] else '')
            rows.append({'id': 'plugin:' + _SAFE_ID.sub('_', name)[:80], 'class': 'plugin', 'sign_in': '',
                         'configured': record['state'] == 'loaded', 'maturity': 'beta', 'beta': True, 'bars': {},
                         'blocker': note})
        return rows

    def _degraded_fields(self, plugin_id):
        with self._turn:
            count, stuck = self.degraded.get(plugin_id, 0), self._stuck == plugin_id
        if not count:
            return {}
        text = f'operator() passed its {self._operator_deadline_s:g} s deadline {count} time(s)'
        return {'degraded': text + ('; a call has still not returned, reads answer 504' if stuck else '')}

    def operator(self, command):
        """One ``o7.operator.v0`` command -> ``(http status, body)``. Reads go to the loaded plugin."""
        name = command.get('command') if isinstance(command, dict) else None
        if name in ACTIONS:
            return 503, {'error': 'unavailable: operator interventions are not wired to the daemon; '
                                  'nothing was sent'}
        if name not in READS:
            return 400, {'error': 'command_not_in_operator_contract'}
        if self.health.poisoned:
            return 503, {'error': 'unavailable: core_poisoned_restart_required'}
        providers = [pid for pid in self._operators if 'read' in self.loader.permissions(pid)]
        if not providers:
            if not self.records:
                reason = 'no operator plugin is installed'
            elif self.verification != 'available':
                reason = f'{VERIFICATION_UNAVAILABLE}: {CRYPTOGRAPHY_MISSING}'
            else:
                reason = 'no loaded plugin serves the operator contract'
            return 503, {'error': f'unavailable: {reason}'}
        if len(providers) > 1:
            return 503, {'error': 'unavailable: more than one loaded plugin serves the operator contract'}
        plugin_id = providers[0]
        outcome = self._call_operator(plugin_id, self._operators[plugin_id], json.loads(json.dumps(command)))
        if isinstance(outcome, tuple):
            return outcome
        if 'error' in outcome:
            return 502, {'error': f'unavailable: operator plugin failed: {type(outcome["error"]).__name__}'}
        if not isinstance(outcome['body'], dict):
            return 502, {'error': 'unavailable: operator plugin answered a non-object'}
        return 200, outcome['body']

    def _call_operator(self, plugin_id, handler, command):
        """Run one ``operator()`` call on its own thread, bounded by the read deadline (ADV2-3).

        Returns ``{"body": ...}`` or ``{"error": exc}``, or an ``(http status, body)`` refusal. The
        caller never waits more than the turn wait plus the deadline, so an O7 read (and the GUI
        thread serving it) is never held by a plugin that does not answer.
        """
        with self._turn:
            if self._stuck is None and self._busy:
                self._turn.wait_for(lambda: not self._busy or self._stuck is not None, self._operator_turn_wait_s)
            if self._stuck is not None:
                return 504, {'error': f'{OPERATOR_TIMED_OUT}; an earlier call has not returned, so no new call '
                                      'was made (plugin degraded)'}
            if self._busy:
                return 503, {'error': 'unavailable: operator plugin busy with an earlier read; nothing was read'}
            self._busy = True
        outcome, done = {}, threading.Event()

        def run():
            try:
                outcome['body'] = json.loads(json.dumps(self._guarded(handler, command)))
            except BaseException as exc:  # noqa: BLE001 - the plugin's failure is reported, never guessed around
                outcome['error'] = exc
            finally:
                with self._turn:
                    done.set()
                    self._busy = False
                    if self._stuck == plugin_id:
                        self._stuck = None
                        log.warning('operator plugin %s returned after its deadline; reads go to it again '
                                    '(it stays marked degraded)', plugin_id)
                    self._turn.notify_all()

        try:
            threading.Thread(target=run, name='recursion-operator-read', daemon=True).start()
        except BaseException:
            with self._turn:
                self._busy = False
                self._turn.notify_all()
            raise
        if done.wait(self._operator_deadline_s):
            return outcome
        with self._turn:
            if done.is_set():
                return outcome
            self._stuck = plugin_id
            self.degraded[plugin_id] = self.degraded.get(plugin_id, 0) + 1
            self._turn.notify_all()
        log.error('operator plugin %s did not answer within %.1f s: answered %s; marked degraded, and no new '
                  'call goes to it until this one returns', plugin_id, self._operator_deadline_s, OPERATOR_TIMED_OUT)
        return 504, {'error': OPERATOR_TIMED_OUT}

    def close(self):
        for item in (self.authority, self._trust, self._approval_store):
            if item is not None:
                item.close()
