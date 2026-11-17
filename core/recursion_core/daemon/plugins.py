"""Operator plugins for the running daemon, loaded only through ``plugin_security`` (CSL-720, CSL-738).

Spec: PRODUCT.md d84cdd26 (CSLR-D-012), section 4.1 (plugins run in the daemon's process, with
checks the developer sees), 4.2 (approval gate and breaker), 4.3 (signed means signed by a key on
this install's trusted list; a plugin that fails the signature check loads as unsigned; grants are
bound to the artifact digest and the signing key) and 3.5 (plugins contribute panels).

The daemon never imports a plugin module by name. ``PluginHost`` reads each installed plugin as
bytes and hands them to ``plugin_security.PluginLoader``, which checks the manifest, the artifact
digest, the signature against this install's trusted keys and the installer's grant before any
plugin code runs. A plugin left with no effective permission is refused ``plugin_not_granted``
and none of its code is compiled or run. A plugin the loader refuses stays out, its typed refusal
and findings are kept, and the daemon runs without it. Nothing here is a containment boundary: a
loaded plugin is ordinary Python in the daemon's process and uid (``plugin_security/README.md``).

What runs before a grant is checked: only Recursion's own code (the file reads below, the
manifest parse, the digest, the signature check and reading ``install.json``). What runs after:
the plugin's whole module body (its top level and everything it imports), then ``registry()``
and ``bind_core()`` if it has the permission for them, all in the daemon's process.

An install directory that exists and is not an operator-owned directory nobody else can write
(a link, another owner, group- or other-writable) refuses every plugin, the ``--plugin``
directories included, and its ``install.json`` is not read (decision D4, Lucas, Nov 12 2026:
"An untrusted plugin config folder refuses every plugin").

Loading can wait. ``PluginHost(..., load=False)`` is a host with nothing loaded yet
(``state == 'not_started'``); ``load_installed()`` loads every plugin once, on the calling thread,
and ``start_loading()`` does it on a thread of its own (``state == 'loading'`` until ``'ready'``).
Every read (the inventory, the GUI rows, an O7 read) answers while a plugin is still importing,
from what has been published so far, and a plugin is published only whole: its record, its panels
and its reader together. This is what will let the control socket and Stop come up before any
plugin code runs (decision D1) once ``compose_control`` builds the host with ``load=False`` (lane
STOP's reorder; today it still loads in the constructor). A plugin that hangs while loading cannot
be stopped in-process; it holds back only the plugins after it. After the host's shutdown an O7 read
answers 503 and no plugin code is called; the inventory still shows the last state.

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
  and out) for the approval gate, the authority profile and the breaker, only with a permission
  that client serves (``order`` today: ``CORE_CLIENT_PERMISSIONS``). Without one it is not called.

A plugin call that turns the GIL on (decision D2) poisons the core: what that plugin offered is
not used (no panels, no reader, and ``bind_core`` is not called after a ``registry()`` that did
it), the O7 read that did it answers 503 like every later one, and every later plugin is refused.

No silent verification skip: when ``cryptography`` cannot be imported, no signature can be checked,
so every installed plugin is refused ``plugin_verification_unavailable`` and none is loaded, signed
or not.

Not here: any surface that can approve or veto (so no effect is ever authorized), a plugin-owned
run (every run is bound to ``host``), the O7 interventions, and an installer (the authority ledger
is never created by the daemon; without it an order effect refuses ``authority_unconfigured``).
"""
from __future__ import annotations

import collections
import json
import logging
import os
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
from recursion_core.plugin_security.loader import module_entry, plain_modules, type_name
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
MAX_MANIFEST_BYTES, MAX_ARTIFACT_BYTES, MAX_INSTALL_BYTES = 64 * 1024, 8 * 1024 * 1024, 1024 * 1024
VERIFICATION_UNAVAILABLE = 'plugin_verification_unavailable'
CRYPTOGRAPHY_MISSING = 'cryptography missing; install recipe in plugin_security/README.md'
EFFECT_PERMISSIONS = {'order': 'order'}
AUTHORITY_REQUIRED = frozenset({'order'})
# R4 finding 7: bind_core() is called only for a plugin holding a permission its core client serves.
CORE_CLIENT_PERMISSIONS = frozenset(EFFECT_PERMISSIONS.values())
POISONED = 'unavailable: core_poisoned_restart_required'
SHUT_DOWN = 'unavailable: the plugin host has shut down'
LOADING = 'plugins are still loading'
NOT_STARTED = 'plugin loading has not been started'
CONFIG_UNTRUSTED = 'the install directory cannot be trusted, so its grants are not read'
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


class _CorePoisoned(Exception):
    """Raised by ``PluginHost._guarded`` instead of calling plugin code once the core is poisoned."""


class _HostShutDown(Exception):
    """Raised by ``PluginHost._guarded`` instead of calling plugin code once the host has shut down."""


_PLAIN_SCALARS = (str, int, float, bool, type(None))


def _plain_registry(value):
    """A ``registry()`` answer as plain data, built-in types exactly, or ``ValueError``.

    Runs inside the guard. ``_snapshot`` first reads one-shot iterables (plugin code); then every
    nested value must be a ``str``, ``int``, ``float``, ``bool``, ``None``, ``dict`` with ``str`` keys,
    ``list`` or ``tuple`` of exactly those types. A subclass is refused before it is iterated or
    compared, so no plugin code runs after the guard's GIL read (review 2 of #380, both reads' C1;
    PAGE's ``gui.panels/1`` takes plain data the same way). Types are compared by identity, never by
    equality: ``kind in (list, tuple)`` asks the metaclass of ``kind``, plugin code that can say yes
    for a subclass (review 3 of #380, GPT-6 Astra 1 and GPT-6.1 Sol's required 1).
    """
    def plain(item):
        kind = type(item)
        if any(kind is scalar for scalar in _PLAIN_SCALARS):
            return item
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise ValueError('registry keys must be plain strings')
            return {key: plain(entry) for key, entry in item.items()}
        if kind is list or kind is tuple:
            return kind(plain(entry) for entry in item)
        raise ValueError(f'registry value is not plain data: {type_name(item)}')
    return plain(_snapshot(value))


class _View(collections.namedtuple('_View', 'records panels operators')):
    """The host's published state: records (tuple), panels ((plugin id, registry) tuple), operators (dict).

    Never mutated: ``_publish`` builds the next one, so a reader that takes ``self._view`` once sees
    one consistent moment."""


def _finding(code, severity, detail=''):
    return {'stage': 'daemon', 'code': code, 'severity': severity, 'detail': detail}


def _one_line(value):
    """Log text with CR and LF escaped, so a plugin path cannot forge a second log record."""
    return str(value).replace('\r', '\\r').replace('\n', '\\n')


def _first_imports(before, own=None):
    """Non-stdlib modules in ``sys.modules`` now that were not in ``before``, sorted.

    Left out: the standard library (top-level name in ``sys.stdlib_module_names``), the host's
    packages (``_HOST_PACKAGES``), the plugin's own module object ``own`` under any name, and a
    submodule whose parent package is itself listed (the package's entry covers it). A module that
    was already imported before the load is not listed: its code did not first run for this plugin.

    A finding, not a control: it reads names. A module named like a standard-library one, an import
    the plugin removes from ``sys.modules`` again, and an import made later inside a call are not
    seen, and an import made by another daemon thread during the load is attributed to this plugin.
    It runs after the plugin's last guard, so it reads ``sys.modules`` as plain data
    (``plain_modules``): a name that is not a plain ``str`` is not listed, and nothing a plugin put
    there is hashed, compared or asked for an attribute (review 3 of #380).
    """
    fresh = {name for name, module in plain_modules().items()
             if name not in before and module is not None and module is not own
             and name.partition('.')[0] not in sys.stdlib_module_names
             and name.partition('.')[0] not in _HOST_PACKAGES}
    return sorted(name for name in fresh
                  if not any(name[:i] in fresh for i, char in enumerate(name) if char == '.'))


def _read_owned(path, limit, *, missing_ok=False):
    """Bytes of an operator-owned regular file nobody else can write, or a typed refusal."""
    try:
        # O_NONBLOCK: opening a FIFO for reading would wait for a writer for ever; with it the open returns
        # and the regular-file check below refuses it (review 1 of #380, DeepSeek F2). No effect on a file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
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
            data = handle.read(limit + 1)
        if len(data) > limit:  # grew after the fstat above (R4 finding 7)
            raise PluginRefused('plugin_file_too_large', Path(path).name)
        return data
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


class PluginHost:
    """What the running daemon holds of its operator plugins: the inventory, the panels, the O7 reader."""

    def __init__(self, config_dir, state_dir, breaker=None, *, plugin_dirs=(), verification_available=None,
                 gil_probe=None, clock=time.time, operator_deadline_s=OPERATOR_DEADLINE_S,
                 operator_turn_wait_s=OPERATOR_TURN_WAIT_S, load=True):
        """``load=False``: nothing is read or run yet; call ``load_installed()`` or ``start_loading()`` later."""
        require_launcher_platform()  # Windows: typed PlatformUnsupported; no plugin file is opened
        self._config, self._state = Path(config_dir) / PLUGINS_DIR, Path(state_dir)
        self._breaker, self._plugin_dirs = breaker, tuple(plugin_dirs)
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
        # Published state: ONE immutable value, replaced whole by the loading thread (its only writer) and
        # read once per answer, so no reader can see a plugin's record without its panels or its reader.
        self._view = _View((), (), {})
        # Load admission and store ownership against close(): never held while plugin code runs.
        self._lifecycle = threading.Lock()
        self.state = 'not_started'       # 'loading' once a load is admitted, 'ready' once it has finished
        self.ready = threading.Event()   # set with state 'ready'
        self.loading_source = None       # the plugin directory being loaded now, if any
        self._load_started = self._closed = False
        self.pending_approvals = collections.deque(maxlen=64)  # (approval id, digest) drawn, newest last
        self.authority_state = 'not configured'
        self._available = backend_available() if verification_available is None else verification_available()
        self.verification = 'available' if self._available else f'unavailable: {CRYPTOGRAPHY_MISSING}'
        if load:
            self.load_installed()

    # -- published state ------------------------------------------------------------------------
    @property
    def records(self):
        """Every plugin's record so far, loaded or refused (a fresh list)."""
        return list(self._view.records)

    @records.setter
    def records(self, value):
        view = self._view
        self._view = _View(tuple(value), view.panels, view.operators)

    @property
    def registries(self):
        """The accepted plugins' panel registries, in load order (the GUI's input)."""
        return tuple(entry for _, entry in self._view.panels)

    @property
    def _operators(self):
        return dict(self._view.operators)

    @_operators.setter
    def _operators(self, value):
        view = self._view
        self._view = _View(view.records, view.panels, dict(value))

    # -- loading --------------------------------------------------------------------------------
    def load_installed(self):
        """Discover and load every installed plugin, on this thread.

        Once per host: a second call raises ``RuntimeError``. After ``close()`` it loads nothing and opens
        no store (close is terminal). Always ends with ``state == 'ready'`` and ``ready`` set, also when
        loading raised; a discovery error is a typed refusal row, not an exception.
        """
        admitted = self._admit_load()
        if admitted is None:
            raise RuntimeError('PluginHost.load_installed runs once')
        self._run_load(admitted)

    def start_loading(self):
        """``load_installed()`` on a daemon thread of its own; returns that thread. Reads answer meanwhile.

        Returns None, and starts nothing, when a load was already started (review 1 of #380, DeepSeek F5).
        The thread is not joined by ``close()``: a plugin hanging in its import cannot be stopped in-process.
        """
        admitted = self._admit_load()
        if admitted is None:
            return None
        thread = threading.Thread(target=self._run_load, args=(admitted,), name='recursion-plugin-load',
                                  daemon=True)
        thread.start()
        return thread

    def _admit_load(self):
        """None if a load was already started; else whether this host was already shut down (True/False)."""
        with self._lifecycle:
            if self._load_started:
                return None
            self._load_started = True
            self.state = 'loading'
            return self._closed

    def _run_load(self, closed):
        try:
            if not closed:
                self._load_all()
        finally:
            self.loading_source = None
            self.state = 'ready'
            self.ready.set()

    def _own(self, attribute, store):
        """Give ``store`` to this host, or close it at once if ``close()`` has already run. True if kept."""
        with self._lifecycle:
            if not self._closed:
                setattr(self, attribute, store)
                return True
        store.close()
        return False

    def _load_all(self):
        try:
            sources, trusted = self._sources(self._plugin_dirs)
        except OSError as exc:  # the install directory could not be listed: typed, never an escape
            log.error('plugin directory %s cannot be listed (%s): no plugin is loaded', _one_line(self._config),
                      type(exc).__name__)
            self._publish({'source': str(self._config), 'plugin_id': None, 'state': 'refused',
                           'code': 'plugin_directory_unreadable', 'detail': type(exc).__name__,
                           'findings': [_finding('plugin_directory_unreadable', 'refuse', type(exc).__name__)]})
            return
        if not sources:
            return
        if not self._available:
            # No signature can be checked. Loading anyway, even as "unsigned", would be an
            # unverified load: refuse every plugin and say why.
            log.error('%s: %s; %d plugin(s) refused, none loaded', VERIFICATION_UNAVAILABLE, CRYPTOGRAPHY_MISSING,
                      len(sources))
            self._refuse_all(sources, VERIFICATION_UNAVAILABLE, CRYPTOGRAPHY_MISSING)
            return
        if not trusted:
            self._refuse_all(sources, 'plugin_directory_untrusted', CONFIG_UNTRUSTED)
            return
        try:
            install = read_install(self._config)
            trust = TrustStore(install['trusted_keys'], revocation_store=self._revocation_store())
        except (PluginRefused, Refused) as exc:
            log.error('no plugin can load: %s', exc)
            self._refuse_all(sources, exc.code, exc.detail)
            return
        except (OSError, sqlite3.Error) as exc:
            log.error('no plugin can load: revocation_store_invalid: %s', type(exc).__name__)
            self._refuse_all(sources, 'revocation_store_invalid', type(exc).__name__)
            return
        if not self._own('_trust', trust):
            return  # close() ran while the store was opening: it is closed again and nothing loads
        self.loader = PluginLoader(trust, install['grants'], install['reserved'], self.health, self._gil)
        if not self._compose_core(install, self._breaker):
            return
        for source in sources:
            if self._closed:
                break
            self.loading_source = str(source)
            self._publish(*self._load(source))

    def _publish(self, record, offer=None):
        """Make one plugin visible to every read at once: its record, its panels and its reader.

        Builds the next view from the current one and replaces it in one assignment; only the loading
        thread publishes, so there is no second writer to exclude.
        """
        offer, view = offer or {}, self._view
        panels = view.panels + (((record['plugin_id'], offer['panels']),) if 'panels' in offer else ())
        operators = {**view.operators, record['plugin_id']: offer['operator']} if 'operator' in offer \
            else view.operators
        self._view = _View(view.records + (record,), panels, operators)

    # -- discovery ------------------------------------------------------------------------------
    def _sources(self, plugin_dirs):
        """``(plugin directories, install directory trusted)``. An untrusted one is recorded here (D4)."""
        explicit = [Path(p) for p in plugin_dirs]
        if not os.path.lexists(self._config):
            return explicit, True
        if not _owned_directory(self._config):
            log.error('plugin directory %s is not an operator-owned directory, or is group/other writable: '
                      'install.json is not read and every plugin is refused', _one_line(self._config))
            self._publish({'source': str(self._config), 'plugin_id': None, 'state': 'refused',
                           'code': 'plugin_directory_untrusted', 'detail': 'install directory', 'findings': []})
            return explicit, False
        return sorted(p for p in self._config.iterdir() if p.is_dir() or p.is_symlink()) + explicit, True

    def _refuse_all(self, sources, code, detail):
        for source in sources:
            self._publish({'source': str(source), 'plugin_id': None, 'state': 'refused', 'code': code,
                           'detail': detail, 'findings': [_finding(code, 'refuse', detail)]})

    def _revocation_store(self):
        path = private_dir(self._state / STORE_DIR) / REVOCATION_FILE
        if not os.path.lexists(path):
            create_revocation_store(str(path))
        # The directory check does not cover this file. A symlink would be followed by the
        # store open, including a link to a 0666 file in a shared directory (E21).
        info = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise PluginRefused('revocation_store_untrusted',
                                'must be an operator-owned regular file, not a symlink and not group/other writable')
        return str(path)

    def _compose_core(self, install, breaker):
        """The approval gate, the authority profile and the core service. False if close() ran meanwhile."""
        # The core draws every approval (4.2). No surface can show or decide one yet, so the
        # authenticator names no principal and nothing can be approved: effects stay refused.
        self.gate = ApprovalGate(lambda approval_id, digest, _raw: self.pending_approvals.append((approval_id, digest)),
                                 lambda _credential: None, lambda: int(self._clock()))
        anchor = install['authority_anchor']
        if anchor is not None:
            try:
                authority = Authority(anchor, str(self._state / STORE_DIR / LEDGER_FILE), lambda: int(self._clock()))
            except Refused as exc:
                self.authority_state = f'unavailable: {exc.code}'
                log.error('authority profile is unavailable (%s); order effects refuse authority_unconfigured', exc.code)
            else:
                if not self._own('authority', authority):
                    return False
                self.authority_state = 'configured'
        if breaker is not None:  # with no durable breaker store there is no core boundary to hand a plugin
            self.core = CoreService(loader=self.loader, gate=self.gate, breaker=breaker, health=self.health,
                                    authority=self.authority, effect_permissions=EFFECT_PERMISSIONS,
                                    authority_required=AUTHORITY_REQUIRED)
        return True

    # -- one plugin -----------------------------------------------------------------------------
    def _load(self, source):
        """``(record, offer)``: what to publish for this plugin; ``offer`` holds its panels and reader."""
        record = {'source': str(source), 'plugin_id': None, 'state': 'refused', 'code': None, 'detail': '',
                  'findings': []}
        # Loads are serial (load_installed's loop; the loader holds its own lock), so the modules
        # that appear between here and the end of this load were first imported for this plugin.
        before = set(plain_modules())
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
            log.error('plugin at %s refused: %s; the daemon runs without it', _one_line(source), _one_line(exc))
            return record, {}
        record.update(plugin_id=loaded.plugin_id, state='loaded', code=None, trust=loaded.trust,
                      signer_key_id=loaded.signer_key_id, registry_label=loaded.registry_label,
                      permissions=list(loaded.permissions), artifact_sha256=loaded.artifact_sha256,
                      findings=list(loaded.findings), provides=[], containment='in process, same uid: not contained')
        try:
            offer = self._attach(loaded, record)
        except BaseException as exc:  # noqa: BLE001 - nothing a plugin does escapes its own load (DeepSeek F1)
            if self._gil.gil_enabled():  # plugin code ran outside a guard: read the GIL as a guard would
                self.health.poison('gil_enabled_after_plugin_call')
            record['findings'].append(_finding('plugin_attach_failed', 'refuse',
                                               f'{type_name(exc)}: nothing this plugin offers is used'))
            record['provides'], offer = [], {}
        # After registry() and bind_core(), which are plugin code too. The plugin stays loaded.
        record['findings'] += self._import_findings(before, loaded.module)
        log.info('plugin %s loaded through the plugin loader: trust=%s permissions=%s provides=%s',
                 loaded.plugin_id, loaded.trust, ','.join(loaded.permissions) or '-', ','.join(record['provides']) or '-')
        return record, offer

    @staticmethod
    def _import_findings(before, own=None):
        names = _first_imports(before, own)
        return [_finding(UNVERIFIED_IMPORTS, 'warn', ','.join(names))] if names else []

    def _attach(self, loaded, record):
        """Take what the plugin offers, each part only with its permission. Plugin code runs here.

        Returns the offer to publish (``panels``, ``operator``) and fills ``record['provides']``.
        Nothing is published from a plugin whose own call turned the GIL on (D2, R4 finding 4), and
        ``bind_core`` is not called after a ``registry()`` that did. No hook is called once the core is
        poisoned (``_guarded``), and ``bind_core`` is decided on the plugin's live permissions.
        """
        module, perms = loaded.module, loaded.permissions
        offer, client = {}, False
        factory = self._hook(module, 'registry')
        if callable(factory):
            if 'panel' not in perms:
                record['findings'].append(_finding('panel_permission_required', 'warn', 'registry() not read'))
            else:
                try:
                    # Copied to plain values inside the guard: a generator or a dict subclass in the answer
                    # is plugin code too, and the GIL is read after it (review 1 of #380, Sol R2).
                    snapshot = self._guarded(lambda: _plain_registry(factory()))
                    required = snapshot.get('required', {})
                    if not all(isinstance(cls, str) and all(isinstance(s, str) for s in subjects)
                               for cls, subjects in required.items()):
                        raise ValueError('required must map a class to subject strings')
                    snapshot = {key: snapshot[key] for key in ('panels', 'required') if key in snapshot}
                    # The GUI's own validation, over every panel already accepted plus these.
                    accepted = [p for _, entry in self._view.panels for p in entry.get('panels', ())]
                    panel_registry(accepted + list(snapshot.get('panels', ())))
                except (_CorePoisoned, _HostShutDown):
                    pass  # the core was poisoned, or the host shut down, before the call: not called
                except BaseException as exc:  # noqa: BLE001 - a bad registry costs the plugin its panels, not the daemon
                    record['findings'].append(_finding('plugin_registry_invalid', 'refuse',
                                                       f'{type_name(exc)}: panels not registered'))
                else:
                    offer['panels'] = snapshot
        handler = self._hook(module, 'operator')
        if callable(handler):
            if 'read' not in perms:
                record['findings'].append(_finding('read_permission_required', 'warn', 'operator() not used'))
            else:
                offer['operator'] = handler
        bind = self._hook(module, 'bind_core')
        if callable(bind) and self.core is None:  # no breaker store, so no core client to give (DeepSeek, part 6)
            record['findings'].append(_finding('core_client_unavailable', 'warn',
                                               'bind_core() not called: no breaker store, no core client'))
        elif callable(bind):
            # The live permissions, not the ones read at load: a key revoked during registry() has taken
            # order away already (review 1 of #380, Astra R1).
            if not CORE_CLIENT_PERMISSIONS & set(self.loader.permissions(loaded.plugin_id)):
                record['findings'].append(_finding('core_client_permission_required', 'warn',
                                                   'bind_core() not called: needs ' + ' or '.join(
                                                       sorted(CORE_CLIENT_PERMISSIONS))))
            else:
                try:
                    self._guarded(bind, self.core.client_for(loaded.plugin_id))
                    client = True
                except (_CorePoisoned, _HostShutDown):
                    pass  # not called: the check below discards everything this plugin offered
                except BaseException as exc:  # noqa: BLE001 - SystemExit or KeyboardInterrupt included (_guarded)
                    record['findings'].append(_finding('plugin_bind_core_failed', 'warn', type_name(exc)))
        if self.health.poisoned:  # by one of this plugin's own calls, or by the sweep meanwhile
            return self._poisoned(record)
        record['provides'] += ([name for key, name in (('panels', 'panels'), ('operator', 'operator_reads'))
                                if key in offer] + (['core_client'] if client else []))
        return offer

    @staticmethod
    def _hook(module, name):
        """A hook from the module's own namespace (a plain dict), never through ``getattr``.

        ``getattr(module, name, None)`` would call a module-level ``__getattr__`` (PEP 562), plugin code
        whose raise escaped the host (review 1 of #380, DeepSeek F1). A hook is a name the plugin's
        module defines; reading it runs no plugin code. ``module_entry`` walks the namespace instead
        of looking the name up, since a lookup compares the name with any key of equal hash, and a
        key the plugin made can be a ``str`` subclass whose ``__eq__`` is plugin code, run here after
        the registry guard's GIL read (review 3 of #380).
        """
        return module_entry(module, name)

    def _poisoned(self, record):
        record['findings'].append(_finding('core_poisoned_restart_required', 'refuse',
                                           f'{self.health.poisoned}: nothing this plugin offers is used'))
        return {}

    def _guarded(self, call, *args):
        """Run plugin code, then read the GIL: a plugin that turned it on poisons the core (4.1).

        Every plugin callback after the import goes through here (``registry()``, ``bind_core()``, each
        ``operator()`` read), so this is where a poisoned core refuses plugin work: ``_CorePoisoned`` is
        raised before the call (D2, "refuses plugin work"; review 1 of #380, Sol R1 and Astra R2). A
        call already running when another thread poisons the core cannot be stopped in-process; its
        result is discarded by the caller's own poison check.

        Callers catch ``BaseException`` from plugin code, ``SystemExit`` and ``KeyboardInterrupt``
        included: they cost the plugin, not the daemon (ADV2-4). Nothing the daemon needs for its
        own shutdown is raised there: SIGTERM and SIGINT only set the main loop's stop event
        (``daemon/__main__.py``), as the loader's import step already assumes.

        What the host reads after the call runs no plugin code: an answer is plain data before the
        GIL is read, and an exception is named with ``type_name`` (review 3 of #380, Sol's
        required 2). Not ordered by any guard: a finalizer (``__del__``) or weakref callback of a
        plugin object, which runs whenever the garbage collector or a dropped reference runs it;
        the sweep's GIL read is what sees those (D2).
        """
        if self._closed:  # a read that waited for its turn across the shutdown (review 2 of #380, C2)
            raise _HostShutDown(SHUT_DOWN)
        if self.health.poisoned:
            raise _CorePoisoned(self.health.poisoned)
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
                'approval_surface': 'none: no surface can approve or veto yet, so no effect is authorized',
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
        if self._closed:  # no plugin call is served after shutdown (review 1 of #380, DeepSeek F3)
            return 503, {'error': SHUT_DOWN}
        if self.health.poisoned:
            return 503, {'error': POISONED}
        view = self._view  # one published state for this whole answer
        operators = view.operators
        providers = [pid for pid in operators if 'read' in self.loader.permissions(pid)]
        if not providers:
            if self.state == 'not_started':
                reason = NOT_STARTED
            elif self.state == 'loading':
                reason = LOADING
            elif not view.records:
                reason = 'no operator plugin is installed'
            elif self.verification != 'available':
                reason = f'{VERIFICATION_UNAVAILABLE}: {CRYPTOGRAPHY_MISSING}'
            else:
                reason = 'no loaded plugin serves the operator contract'
            return 503, {'error': f'unavailable: {reason}'}
        if len(providers) > 1:
            return 503, {'error': 'unavailable: more than one loaded plugin serves the operator contract'}
        plugin_id = providers[0]
        outcome = self._call_operator(plugin_id, operators[plugin_id], json.loads(json.dumps(command)))
        if self._closed:  # the host shut down while this read ran or waited: its answer is not used
            return 503, {'error': SHUT_DOWN}
        if self.health.poisoned:
            # D2: the call that turned the GIL on answers 503, as every later read does; its answer is not used.
            return 503, {'error': POISONED}
        if isinstance(outcome, tuple):
            return outcome
        if 'error' in outcome:
            return 502, {'error': f'unavailable: operator plugin failed: {type_name(outcome["error"])}'}
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
                # The JSON copy runs inside the guard: a dict subclass's items() is plugin code too.
                outcome['body'] = self._guarded(lambda: json.loads(json.dumps(handler(command))))
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
        # The wait already ended. A worker that finishes before this thread takes ``_turn`` again
        # does not turn that timeout into a timely answer (F07). It is not left marked stuck.
        with self._turn:
            finished = done.is_set()
            self.degraded[plugin_id] = self.degraded.get(plugin_id, 0) + 1
            if not finished:
                self._stuck = plugin_id
            self._turn.notify_all()
        if finished:
            log.error('operator plugin %s answered after its %.1f s deadline: answered %s; marked degraded',
                      _one_line(plugin_id), self._operator_deadline_s, OPERATOR_TIMED_OUT)
        else:
            log.error('operator plugin %s did not answer within %.1f s: answered %s; marked degraded, and no new '
                      'call goes to it until this one returns', _one_line(plugin_id), self._operator_deadline_s,
                      OPERATOR_TIMED_OUT)
        return 504, {'error': OPERATOR_TIMED_OUT}

    def close(self):
        """Terminal: no load is admitted after it, no plugin call is served, and every store is closed.

        A store a loading thread opens after this returns is closed by that thread (``_own``). A load
        still running publishes nothing more than the plugin it is in. A read answers 503 ``SHUT_DOWN``;
        the inventory still answers, as the last state. Idempotent. Never waits for plugin code.
        """
        with self._lifecycle:
            self._closed = True
            stores = (self.authority, self._trust)
            for item in stores:
                if item is not None:
                    item.close()
