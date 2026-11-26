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
     "authority_anchor": "<64 hex installer public key>" or null,
     "registry_labels": {"<sha256 of manifest.json bytes>": "verified" or "non_verified"}}

``registry_labels`` is the label the installer saw for that manifest. The key is the SHA-256 of
the manifest file's bytes. A missing key, or a key that is not the manifest now installed, is
unlisted. The label is not a permission and not a grant: it never changes trust, a permission,
or whether the plugin loads.

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

Not here: any surface that can approve or veto (so no effect is ever authorized), a plugin-owned
run (every run is bound to ``host``), the O7 interventions, and an installer (the authority ledger
is never created by the daemon; without it an order effect refuses ``authority_unconfigured``).
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import sys
import threading
import time

from recursion_core.gui.client import ACTIONS, READS
from recursion_core.gui.panels import panel_registry
from recursion_core.plugin_security import (ApprovalGate, Authority, CoreHealth, CoreService, GilProbe, Grant,
                                            PluginLoader, Refused, ReservedEffects, TrustStore,
                                            create_revocation_store)
from recursion_core.plugin_security.limits import ARTIFACT, MAX_ARTIFACT_BYTES, MAX_MANIFEST_BYTES
from recursion_core.plugin_security.manifest import PERMISSIONS_V1, developer_key_id_ok
from recursion_core.plugin_security.signing import backend_available

from .breaker import STORE_DIR
from .gui import _snapshot
from .launcher import private_dir, require_launcher_platform

log = logging.getLogger(__name__)

PLUGINS_DIR = 'plugins'                 # $XDG_CONFIG_HOME/recursion/plugins
INSTALL_FILE = 'install.json'
MANIFEST, SIGNATURE = 'manifest.json', 'manifest.sig'   # ARTIFACT: plugin_security/limits.py
REVOCATION_FILE = 'revoked.sqlite3'     # <state dir>/plugin-security/
LEDGER_FILE = 'ledger.sqlite3'          # installer-created; never created here
MAX_INSTALL_BYTES = 1024 * 1024        # MAX_MANIFEST_BYTES, MAX_ARTIFACT_BYTES: plugin_security/limits.py
VERIFICATION_UNAVAILABLE = 'plugin_verification_unavailable'
CRYPTOGRAPHY_MISSING = 'cryptography missing; install recipe in plugin_security/README.md'
EFFECT_PERMISSIONS = {'order': 'order'}
AUTHORITY_REQUIRED = frozenset({'order'})
_INSTALL_KEYS = frozenset({'trusted_keys', 'grants', 'reserved', 'authority_anchor', 'registry_labels'})
_LABELS = frozenset({'verified', 'non_verified'})
_SHA256 = re.compile(r'[0-9a-f]{64}')
_GRANT_KEYS = frozenset({'plugin_id', 'artifact_sha256', 'signer_key_id', 'permissions', 'unsigned_order_acknowledged',
                         'order_after_revoked_key', 'order_after_revocation_generation', 'developer_key_id'})
_SAFE_ID = re.compile(r'[^A-Za-z0-9_.-]')
# The registry label a plugin row shows. A refused row whose label could not be read says so and why,
# never "unlisted", which would claim the installer's map was looked up for these bytes.
_SHOWN = {'verified': 'verified', 'non_verified': 'non-verified', 'unlisted': 'unlisted'}
NOT_READ_DIRECTORY = 'not read (the plugin directory was refused, so nothing in it was opened)'
NOT_READ_MANIFEST = 'not read (manifest.json was refused)'
NOT_READ_INSTALL = 'not read (install.json or its directory was refused)'
_NOT_READ = frozenset({NOT_READ_DIRECTORY, NOT_READ_MANIFEST, NOT_READ_INSTALL})
# A refused directory is remembered for the run, so what a row says it found is what the first look found.
DIRECTORY_REREAD = ('what was found is what the first look found; a directory repaired after the daemon started '
                    'is read again only after a restart of the daemon')
# install.json is not read while its directory is refused, so nothing it may hold can be checked: no plugin loads.
INSTALL_NOT_READ = ('install.json was not read: the install directory %s %s; so the reserved effects, grants and '
                    'trusted keys written in it could not be checked, and no plugin is loaded while it is so; %s, then '
                    'restart the daemon')
# Said once by the run itself, at the daemon's start and in its status, while install.json is not read for its directory.
INSTALL_UNREAD_RUN = ('install.json was not read, so no plugin was loaded: the install directory %s %s; %s, then '
                      'restart the daemon')
# The modes a repair command can change. Any other finding (a link, another owner, not a directory) gets words only.
_MODE_ONLY = frozenset({'group-writable', 'other-writable'})
_UNREAD = object()
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


# What ``lstat`` found where a plugin file or directory was expected, in the words a refused row shows.
_KINDS = ((stat.S_ISLNK, 'a symbolic link (links are not followed)'), (stat.S_ISFIFO, 'a FIFO (named pipe)'),
          (stat.S_ISSOCK, 'a socket'), (stat.S_ISCHR, 'a character device'), (stat.S_ISBLK, 'a block device'),
          (stat.S_ISDIR, 'a directory'), (stat.S_ISREG, 'a regular file'))
DIRECTORY_REFUSED = 'must be an operator-owned directory, not group/other writable'


def _kind(mode):
    return next((words for test, words in _KINDS if test(mode)), 'not a regular file')


def plugin_directory_verdict(path):
    """Whether any file under the plugin directory ``path`` may be read at all.

    ``None`` when it may: a real directory (not a link), owned by this user, that neither group nor
    other can write. Otherwise the ``PluginRefused`` (``plugin_directory_untrusted``) whose words say
    what was found there. It looks with ``lstat`` only and opens nothing. Every reader of a plugin file
    goes through it: ``_read_owned`` asks it about the file's own directory before it looks at the
    file, and ``PluginHost`` asks it (through ``_directory_refusal``, which remembers a refusal for the
    rest of the run) before the loader, a plugin row or the label lookup reads anything there.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return PluginRefused('plugin_directory_untrusted', DIRECTORY_REFUSED + '; found: nothing at that path')
    except OSError as exc:
        return PluginRefused('plugin_directory_untrusted', f'{DIRECTORY_REFUSED}; found: {type(exc).__name__}')
    if not stat.S_ISDIR(info.st_mode):
        found = [_kind(info.st_mode)]
    else:
        found = [words for wrong, words in ((info.st_uid != os.geteuid(), 'owned by another user'),
                                            (info.st_mode & 0o020, 'group-writable'),
                                            (info.st_mode & 0o002, 'other-writable')) if wrong]
    if not found:
        return None
    return PluginRefused('plugin_directory_untrusted', '%s; found: %s' % (DIRECTORY_REFUSED, ', '.join(found)))


def _check_owned(name, info, limit):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PluginRefused('plugin_file_untrusted',
                            f'{name}: must be an operator-owned regular file, not group/other writable')
    if info.st_size > limit:
        raise PluginRefused('plugin_file_too_large', f'{name}: {info.st_size} bytes, over the {limit}-byte limit; '
                                                     'nothing was read')


def _read_owned(path, limit, *, missing_ok=False):
    """Bytes of an operator-owned regular file nobody else can write, or a typed refusal. It never waits.

    The one function that opens a plugin file. The file's directory must pass
    ``plugin_directory_verdict`` first. Then the entry is looked at without following a link
    (``lstat``): a FIFO, a socket, a device, a directory or a link is refused and never opened, and so
    is a file another user owns or can write, or one larger than ``limit``. The open adds
    ``O_NONBLOCK``, so not even an entry swapped in after the look can make it wait, and the file that
    was opened must be the entry that was looked at (``st_dev`` and ``st_ino``) before a byte is read.
    """
    # Same shape as subscription_routes.policy._check_home: a typed refusal before any
    # file is opened. Windows has no O_NOFOLLOW; a no-follow open for it is not this round,
    # so install refuses instead of dropping the guard (no default of 0, no open without it).
    if os.name == 'nt' or not hasattr(os, 'O_NOFOLLOW'):
        raise PluginRefused('platform_unsupported', 'O_NOFOLLOW is missing; no plugin file is read')
    if not hasattr(os, 'O_NONBLOCK'):
        raise PluginRefused('platform_unsupported', 'O_NONBLOCK is missing; no plugin file is read')
    path = Path(path)
    refusal = plugin_directory_verdict(path.parent)
    if refusal is not None:
        if missing_ok and not os.path.lexists(path.parent):
            return None
        raise refusal
    try:
        seen = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PluginRefused('plugin_file_missing', path.name) from None
    except OSError as exc:
        raise PluginRefused('plugin_file_unreadable', f'{path.name}: {type(exc).__name__}') from None
    if stat.S_ISLNK(seen.st_mode):
        # The code a no-follow open has always given a link; the words now say what was found.
        raise PluginRefused('plugin_file_unreadable', f'{path.name}: {_kind(seen.st_mode)}; it was not opened')
    if not stat.S_ISREG(seen.st_mode):
        raise PluginRefused('plugin_file_untrusted',
                            f'{path.name}: {_kind(seen.st_mode)}, not a regular file; it was not opened')
    _check_owned(path.name, seen, limit)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise PluginRefused('plugin_file_missing', path.name) from None
    except OSError as exc:
        raise PluginRefused('plugin_file_unreadable', f'{path.name}: {type(exc).__name__}') from None
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
            raise PluginRefused('plugin_file_untrusted',
                                f'{path.name}: changed between the look and the open; nothing was read')
        _check_owned(path.name, info, limit)
        with os.fdopen(os.dup(fd), 'rb') as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def _registry_labels(value):
    """The installer's labels, or a typed refusal. Absent and ``{}`` are no labels."""
    if not isinstance(value, dict):
        raise PluginRefused('plugin_install_config_invalid', 'registry_label')
    labels = {}
    for digest, label in value.items():
        if (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                or not isinstance(label, str) or label not in _LABELS):
            raise PluginRefused('plugin_install_config_invalid', 'registry_label')
        labels[digest] = label
    return labels


def _no_install():
    """What an install with no install.json gives: no trusted key, no grant, no reserved effect, no anchor, no label."""
    return {'trusted_keys': {}, 'grants': {}, 'reserved': ReservedEffects(), 'authority_anchor': None,
            'registry_labels': {}}


def _repair(path, detail):
    """What to do about the refused directory ``path`` whose refusal says ``detail``.

    A shell command is given only where it is exact: when the mode alone is wrong, as
    ``chmod go-w -- <path quoted for a POSIX shell>``, so a space stays inside the one argument and a
    leading dash is not read as an option. A path with a line break or another control character
    would not stay one line, so it gets plain words and no command.
    """
    words = 'make %s a directory of your own (not a link) that group and other cannot write' % path
    found = set(detail.rpartition('; found: ')[2].split(', '))
    if not found <= _MODE_ONLY:
        return words
    text = os.fsdecode(path)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return words + (' (its path holds a line break or another control character, so no command is printed: '
                        'take the write permission of group and other off that directory)')
    return words + ' (only its mode is wrong: chmod go-w -- %s)' % shlex.quote(text)


def read_install(directory):
    """Parse ``install.json``. With no file: no trusted key, no grant and no label.

    ``developer_key_id`` and ``order_after_revoked_key`` are each null or ``ed25519:`` plus 32 lower-case hex; a grant
    may name the five version-1 permissions.
    A manual grant is bound to that developer key, as ``plugin_security.manifest`` binds it: an unbound manual
    grant that asks for permissions is refused ``grant_unbound`` before plugin code runs.
    """
    raw = _read_owned(Path(directory) / INSTALL_FILE, MAX_INSTALL_BYTES, missing_ok=True)
    if raw is None:
        return _no_install()
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
                or not isinstance(perms, list) or any(p not in PERMISSIONS_V1 for p in perms)
                or not (signer is None or isinstance(signer, str))
                or type(item.get('unsigned_order_acknowledged', False)) is not bool
                or not developer_key_id_ok(item.get('order_after_revoked_key'))
                or not (generation is None or type(generation) is int)
                or not developer_key_id_ok(item.get('developer_key_id'))
                or item['plugin_id'] in grants):
            raise PluginRefused('plugin_install_config_invalid', 'grant value')
        grants[item['plugin_id']] = Grant(item['plugin_id'], item['artifact_sha256'], signer, tuple(perms),
                                          item.get('unsigned_order_acknowledged', False),
                                          item.get('order_after_revoked_key'), generation,
                                          developer_key_id=item.get('developer_key_id'))
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
            'reserved': ReservedEffects(frozenset(hosts), tuple(prefixes)), 'authority_anchor': anchor,
            'registry_labels': _registry_labels(data.get('registry_labels', {}))}


class PluginHost:
    """What the running daemon holds of its operator plugins: the inventory, the panels, the O7 reader."""

    def __init__(self, config_dir, state_dir, breaker=None, *, plugin_dirs=(), verification_available=None,
                 gil_probe=None, clock=time.time, operator_deadline_s=OPERATOR_DEADLINE_S,
                 operator_turn_wait_s=OPERATOR_TURN_WAIT_S):
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
        self._labels = {}
        # A plugin directory refused once is never read again in this run: str(path) -> (code, detail).
        self._refused_dirs = {}
        self._dirs_lock = threading.Lock()
        # install.json as read for the rows when the constructor did not read it, keyed by its lstat marker.
        self._install_refusal = None
        # The refusal of the install directory when install.json was not read because of it, else None.
        self._install_unread = None
        self._install_view, self._install_marker = ({}, False), _UNREAD
        # The run's own sentence while install.json is not read because its directory is refused, else None.
        self.install_file = None
        self._install_lock = threading.Lock()
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
        except PluginRefused as exc:
            self._install_refusal = exc.code
            detail = exc.detail
            if exc.code == 'plugin_directory_untrusted':
                # The one opener refused install.json's own directory, so the file was not opened. The reserved
                # effects, grants and trusted keys it may hold cannot be checked, so no plugin is loaded, wherever
                # its own directory lies, and none of its code runs: each row says why and what to do.
                self._install_unread = exc
                detail = self._install_words(exc)
                self._say_install_unread(exc)
            else:
                log.error('no plugin can load: %s: %s', exc.code, detail)
            self._refuse_all(sources, exc.code, detail)
            return
        # The label is not a gate: it is on the host before the trust store can refuse below.
        self._labels = install['registry_labels']
        try:
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
        refusal = self._directory_refusal(self._config) if os.path.lexists(self._config) else None
        if refusal is not None:
            self._say_install_unread(refusal)
        if self._config.is_dir():
            if refusal is not None:
                self.records.append({'source': str(self._config), 'plugin_id': None, 'state': 'refused',
                                     'code': 'plugin_directory_untrusted',
                                     'detail': 'install directory %s %s; neither its install.json nor any plugin '
                                     'in it was read, and no plugin is loaded while it is so; %s, then restart the '
                                     'daemon' % (self._config, refusal.detail, _repair(self._config, refusal.detail)),
                                     'findings': []})
            else:
                found += sorted(p for p in self._config.iterdir() if p.is_dir() or p.is_symlink())
        return found + [Path(p) for p in plugin_dirs]

    def _say_install_unread(self, refusal):
        """The run says it once, at the start and in its status: install.json was not read, so no plugin was
        loaded, the install directory by its whole path, what was found and the repair."""
        if self.install_file is None:
            self.install_file = INSTALL_UNREAD_RUN % (self._config, refusal.detail, _repair(self._config, refusal.detail))
            log.error('%s', self.install_file)

    def _directory_refusal(self, path):
        """``plugin_directory_verdict`` for this run. A directory refused once stays refused: no row,
        status or label lookup of this run reads it again, whatever it looks like later."""
        key = str(path)
        with self._dirs_lock:
            known = self._refused_dirs.get(key)
        if known is None:
            refusal = plugin_directory_verdict(path)
            if refusal is None:
                return None
            with self._dirs_lock:
                known = self._refused_dirs.setdefault(key, (refusal.code, refusal.detail))
        return PluginRefused(*known)

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
        # The core draws every approval (4.2). No surface can show or decide one yet, so the
        # authenticator names no principal and nothing can be approved: effects stay refused.
        self.gate = ApprovalGate(lambda approval_id, digest, _raw: self.pending_approvals.append((approval_id, digest)),
                                 lambda _credential: None, lambda: int(self._clock()))
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

    # -- one plugin -----------------------------------------------------------------------------
    def _load(self, source):
        record = {'source': str(source), 'plugin_id': None, 'state': 'refused', 'code': None, 'detail': '',
                  'findings': []}
        # Loads are serial (the constructor's loop; the loader holds its own lock), so the modules
        # that appear between here and the end of this load were first imported for this plugin.
        before = set(sys.modules)
        try:
            refusal = self._directory_refusal(source)
            if refusal is not None:
                raise refusal
            manifest = _read_owned(source / MANIFEST, MAX_MANIFEST_BYTES)
            artifact = _read_owned(source / ARTIFACT, MAX_ARTIFACT_BYTES, missing_ok=True)
            signature = _read_owned(source / SIGNATURE, 4096, missing_ok=True)
            signature = None if signature is None else signature.decode('ascii', 'replace').strip()
            # The label is for these manifest bytes. Any other digest is not this manifest's label.
            label = self._labels.get(hashlib.sha256(manifest).hexdigest())
            loaded = self.loader.load(manifest, signature, artifact, registry_label=label, package_dir=source)
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
    def status(self, plugin_id):
        """One installed plugin, the same row ``inventory`` carries, or a typed refusal.

        For an id the loader holds active the answer is the loaded row, whatever the directory
        order; a refused row is answered only when no loaded row carries that id.
        The registry label on that row is what the installer recorded for this manifest.
        On a loaded row it is ``unlisted`` when the install recorded none, or one for other bytes.
        """
        if not isinstance(plugin_id, str) or not plugin_id:
            return {'code': 'request_invalid', 'detail': 'plugin_id'}
        rows = [item for item in self.inventory()['plugins'] if item.get('plugin_id') == plugin_id]
        if not rows:
            return {'code': 'plugin_unknown', 'detail': plugin_id}
        # A refused row carries the id its own, unverified manifest claims: it never hides a loaded one.
        row = next((item for item in rows if item['state'] == 'loaded'), rows[0])
        return {'code': 'ok', 'plugin': row}

    def inventory(self):
        """Every installed plugin, loaded or refused, with its findings. Plain JSON values."""
        plugins = []
        for record in self.records:
            item = dict(record)
            if item['state'] == 'loaded' and self.loader is not None:
                item['permissions'] = list(self.loader.permissions(item['plugin_id']))  # after a revoke or a poison
                item.update(self._degraded_fields(item['plugin_id']))
            plugins.append(item)
        answer = {'plugins': plugins, 'verification': self.verification, 'core_poisoned': self.health.poisoned,
                  'authority_profile': self.authority_state, 'approvals_drawn': len(self.pending_approvals),
                  'approval_surface': 'none: no surface can approve or veto yet, so no effect is authorized',
                  'containment': 'none: plugins run in the daemon process and uid'}
        if self.install_file is not None:
            answer['install_file'] = self.install_file  # present only while install.json is not read for its directory
        return answer

    def route_records(self):
        """The plugin rows of the GUI's inventory: what loaded, what was refused and why.

        Each row names the registry label as text: verified, non-verified or unlisted,
        and says it is a label, not a gate. The core writes that text. The label is
        not a permission and does not decide whether the plugin loaded. A refused row
        whose label could not be read says it was not read and why. Nothing here reads
        a file in a directory that was refused, and nothing here can wait on a file.
        """
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
            stored = record.get('registry_label')
            if stored not in _SHOWN and stored not in _NOT_READ:
                stored = self._refused_row_label(record)
            shown = stored if stored in _NOT_READ else _SHOWN.get(stored, 'unlisted')
            note += self._not_read_words(record, shown)
            note += '; registry label: %s; a label, not a gate' % shown
            rows.append({'id': 'plugin:' + _SAFE_ID.sub('_', name)[:80], 'class': 'plugin', 'sign_in': '',
                         'configured': record['state'] == 'loaded', 'maturity': 'beta', 'beta': True, 'bars': {},
                         'blocker': note})
        return rows

    def _not_read_words(self, record, shown):
        """Why a row's label was not read, where the row does not already say it, and what to do.
        Nothing is opened: the directory refusals are the remembered ones."""
        words, detail = '', record.get('detail', '')
        if shown == NOT_READ_DIRECTORY:
            refusal = self._directory_refusal(Path(record['source']))
            if (refusal is not None and (record.get('code'), detail) != (refusal.code, refusal.detail)
                    and '%s %s' % (record['source'], refusal.detail) not in detail):
                words = '; the plugin directory %s %s' % (record['source'], refusal.detail)
            words += '; ' + DIRECTORY_REREAD
        if record.get('source') == str(self._config):
            return words  # the install directory's own row already says what was not read and what to do
        refusal = self._install_unread
        if refusal is None and shown in (NOT_READ_DIRECTORY, NOT_READ_INSTALL) and os.path.lexists(self._config):
            refusal = self._directory_refusal(self._config)
        if refusal is not None and refusal.code == 'plugin_directory_untrusted':
            install = self._install_words(refusal)
            if install not in detail:
                words += '; ' + install
        return words

    def _install_words(self, refusal):
        """The words of every plugin row while install.json is not read because its directory is refused."""
        return INSTALL_NOT_READ % (self._config, refusal.detail, _repair(self._config, refusal.detail))

    def _refused_row_label(self, record):
        """The label of a row that does not carry the loader's: ``verified``, ``non_verified``, ``None``
        (unlisted) or a not-read phrase. The words come from the installer's map for these manifest
        bytes, never from the plugin, and only after the row's directory passed the one verdict."""
        source = record.get('source')
        if not isinstance(source, str):
            return None
        if self._directory_refusal(Path(source)) is not None:
            return NOT_READ_DIRECTORY
        labels, refused = self._row_labels()
        if refused:
            return NOT_READ_INSTALL
        if not labels:
            return None
        try:
            manifest = _read_owned(Path(source) / MANIFEST, MAX_MANIFEST_BYTES)
        except PluginRefused:
            return NOT_READ_MANIFEST
        return labels.get(hashlib.sha256(manifest).hexdigest())

    def _row_labels(self):
        """``(labels, refused)`` for the rows. Read only when a row needs a label, and at most once
        per change of install.json (its ``lstat`` marker), never once per request."""
        if self.verification == 'available' or self._install_refusal is not None:
            # The constructor read install.json, or tried and was refused.
            return self._labels, self._install_refusal is not None
        # Verification is unavailable, so the constructor did not read the file: read it for display only.
        if not os.path.lexists(self._config):
            return {}, False
        if self._directory_refusal(self._config) is not None:
            return {}, True
        try:
            seen = os.lstat(self._config / INSTALL_FILE)
            marker = (seen.st_dev, seen.st_ino, seen.st_size, seen.st_mtime_ns, seen.st_ctime_ns, seen.st_mode)
        except OSError:
            marker = None
        with self._install_lock:
            if marker != self._install_marker:
                try:
                    view = (read_install(self._config)['registry_labels'], False)
                except PluginRefused:
                    view = ({}, True)
                self._install_view, self._install_marker = view, marker
            return self._install_view

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
        for item in (self.authority, self._trust):
            if item is not None:
                item.close()
