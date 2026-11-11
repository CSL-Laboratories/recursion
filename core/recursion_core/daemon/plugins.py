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
import stat
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


class PluginRefused(Exception):
    """A typed refusal from the daemon's side of plugin loading (files, configuration)."""

    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


def _finding(code, severity, detail=''):
    return {'stage': 'daemon', 'code': code, 'severity': severity, 'detail': detail}


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
    except ValueError:
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
                          findings=findings + [_finding(exc.code, 'refuse', exc.detail)])
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
        log.info('plugin %s loaded through the plugin loader: trust=%s permissions=%s provides=%s',
                 loaded.plugin_id, loaded.trust, ','.join(loaded.permissions) or '-', ','.join(record['provides']) or '-')
        return record

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
                                  'nothing was sent (run Stop is available)'}
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

        threading.Thread(target=run, name='recursion-operator-read', daemon=True).start()
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
