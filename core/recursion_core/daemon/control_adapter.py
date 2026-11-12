"""The daemon's control-API adapter: one composition of registry, routes, Stop and surfaces (CSL-738).

``compose_control`` builds, for the running daemon:

* ``DurableRunRegistry``: the control API's run records (CSL-798 shape, compare-and-swap on
  the generation, attested Stop) stored in the daemon's own durable registry file;
* ``RouteStarter``: ``run.start`` goes through ``routes.RouteService`` and the Claude Code and
  Codex adapters, launched by ``launcher.ProductionLauncher``;
* ``UnitKillTree``: ``run.stop`` goes through ``Daemon.stop`` (the #916 procedure) and answers
  ``stopped`` only on the sweep's attestation;
* ``DaemonControl``: the one object every surface calls. The owner-only Unix socket
  (``control_transport.UnixControlServer``), the CLI behind it and the loopback GUI all end
  in ``DaemonControl.call``, so there is one Stop path (PRODUCT.md d84cdd26, CSLR-D-012,
  section 3.4 S6 line 888: Stop is the same on every surface).

What a surface reads is the stored record plus what the daemon last observed of the unit
(``observed``); ``run.status`` also carries the tail of the engine's stdout file (``output``) and
the route result once there is one (``result``). The control record's vocabulary has no word for a run that ended on its own
or was lost at a reboot, so an answer shows ``state: "ended"`` or ``"lost_reboot"`` for such a
run (``"unknown"`` when the unit could not be observed) and keeps the stored word in
``recorded_state``. Reads never write. ``run.list`` is cut to what one socket frame carries, with
``next_after`` naming the last run sent (ADV1-5; PRODUCT.md d84cdd26, CSLR-D-012, section 3.6: every
surface is a client of the same control API); a list that fits is answered as it always was.

Reads beyond the run records: ``route.list`` is the route inventory (the engines this daemon
has adapters for, with what ``routes.json`` configured, the breaker's view of each connection,
the surfaces it serves and the plugin rows), and ``plugin.list`` is the plugin registry as
``plugins.PluginHost`` loaded it. The GUI's ``GET /v1/routes`` and ``POST /v1/operator`` are
those reads and the loaded operator plugin's O7 reads. A read with no source answers
``unavailable: <reason>``, never an empty or invented answer.

The route breaker is ``breaker.DurableRouteBreaker``: trips, run bindings and faults are kept in
the plugin-security breaker store under the daemon's state directory and survive a restart.

Not here: custody (an API-key route has no credential source and refuses; a route that brings
its own credential, ``key_source: api_key_helper`` or ``passthrough``, is refused before launch,
see ``route_unsupported``), the O7 interventions, bar evidence for any route (every route reads
BETA), scopes on the socket beyond the uid check, and the Control MCP transport.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from recursion_core.control.api import ControlAPI
from recursion_core.control.client import socket_directory
from recursion_core.control.registry import InMemoryRunRegistry, StaleGeneration
from recursion_core.control_transport import UnixControlServer, with_ping_health
from recursion_core.control_transport.framing import MAX_FRAME_BYTES
from recursion_core.gui.client import ControlClient
from recursion_core.plugin_security import Refused
from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as route_contract

from . import RUN_ID, TERMINAL
from .breaker import DurableRouteBreaker, open_breaker_store, store_probe_lease_s
from .launcher import (EngineUnitManager, ProductionLauncher, RunContext, UnitKillTree, bound_run,
                       private_dir, require_launcher_platform)
from .plugins import PluginHost

log = logging.getLogger(__name__)

DEVICE_TOKEN_FILE = 'gui-device-token'
DEVICE_COOKIE = 'recursion_device'
ROUTES_FILE = 'routes.json'         # $XDG_CONFIG_HOME/recursion/routes.json
OUTPUT_TAIL_BYTES = 64 * 1024
OUTPUT_TAIL_LINES = 50
ENDED, LOST_REBOOT = 'ended', 'lost_reboot'
WIRED_ENGINES = ('claude_code', 'codex')  # the engines compose_control builds an adapter and a launcher for
NO_BAR_EVIDENCE = 'no bar evidence is recorded by the daemon'
# reconcile() asks again for a Stop whose request never got through only after a wait that doubles
# from the base to the cap (ADV1-6).
STOP_RETRY_BASE_S, STOP_RETRY_MAX_S = 30.0, 600.0
# Socket connections kept for run.stop when every ordinary one is taken (ADV1-4).
STOP_RESERVED_CONNECTIONS = 4
# The most bytes a run.list answer may take: the socket's frame, less room for the reply envelope
# ({"ok":true,"result":...} and next_after). The request's own "id" is counted per call (ADV1-5).
LIST_ENVELOPE_BYTES = 4096
LIST_REPLY_BYTES = MAX_FRAME_BYTES - LIST_ENVELOPE_BYTES


def _wire_size(value):
    """Bytes ``value`` takes in a control frame (``framing.encode_frame``'s JSON settings)."""
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str).encode('utf-8'))


# -- registry ------------------------------------------------------------------------------------
class ControlRecordInvalid(ValueError):
    """A stored control record is not a JSON object for the run it is filed under."""


class DurableRunRegistry(InMemoryRunRegistry):
    """The control API's registry, with every stored record written to the daemon registry first.

    The compare-and-swap, the attested-Stop rule and the start-key index are the reviewed
    ``InMemoryRunRegistry`` code; this class only makes ``_store`` durable and loads what an
    earlier daemon stored. A write that cannot be made durable raises, the in-memory copy is
    left as it was, and the control API answers ``registry_unavailable`` or ``recorded: false``.
    A stored ``stopped`` record that fails the attestation check at load is refused with
    ``StopUnattested``, and a row that is not a JSON object carrying the run id it is filed under
    with ``ControlRecordInvalid`` (ADV1-7: a non-string ``run_id`` used to load and then break every
    list and reconcile). The daemon does not start on a registry it cannot trust.
    """

    def __init__(self, store, *, processes=None):
        self._durable = None
        records = []
        for run_id, text in store.control_all():
            rec = json.loads(text)
            if not isinstance(rec, dict) or rec.get('run_id') != run_id:
                raise ControlRecordInvalid(f'control record filed under {run_id!r} is not a record of that run')
            records.append(rec)
        super().__init__(records, processes=processes)
        self._durable = store

    def _new_marker(self):
        return f'cg-{uuid.uuid4().hex}'  # unique across restarts; a counter would repeat

    def _store(self, rec):
        if self._durable is not None:
            self._durable.control_put(rec['run_id'], json.dumps(rec, sort_keys=True))
        super()._store(rec)


def pid_alive(manager):
    """``pid -> bool`` for the registry: only a positive "gone" is not alive."""
    return lambda pid: manager.pid_alive(pid) is not False


# -- routes --------------------------------------------------------------------------------------
class RoutesUnreadable(Exception):
    pass


def load_routes(path):
    """Read the operator's route file: ``{"routes": {"<engine>": {...}}}``, or {} when there is none.

    Each entry is the route part of a ``recursion.route/v0`` request (model, connection, and the
    engine's own fields) plus ``deadline_s`` and an optional absolute ``binary``. Nothing is
    defaulted: an engine with no entry cannot be started. The file must be a regular file of
    ours that no one else can write.
    """
    require_launcher_platform()  # the owner check below needs a uid
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RoutesUnreadable('routes file must be an operator-owned regular file, not group/other writable')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RoutesUnreadable(f'routes file unreadable: {type(exc).__name__}') from None
    routes = data.get('routes') if isinstance(data, dict) else None
    if not isinstance(routes, dict):
        raise RoutesUnreadable('routes file has no "routes" object')
    out = {}
    for engine, entry in routes.items():
        if engine not in WIRED_ENGINES or not isinstance(entry, dict):
            raise RoutesUnreadable(f'route {engine!r}: only claude_code and codex are wired')
        deadline = entry.get('deadline_s')
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline <= 0:
            raise RoutesUnreadable(f'route {engine}: deadline_s')
        binary = entry.get('binary')
        if binary is not None and not (isinstance(binary, str) and os.path.isabs(binary)):
            raise RoutesUnreadable(f'route {engine}: binary must be an absolute path')
        route = {k: v for k, v in entry.items() if k not in ('deadline_s', 'binary')}
        route['engine'] = engine
        out[engine] = {'route': route, 'deadline_s': float(deadline), 'binary': binary}
    return out


def route_connection(entry):
    """The connection key of a loaded route entry, or None when the entry names none."""
    try:
        return route_contract.connection_key(entry['route'])
    except (KeyError, TypeError):
        return None


def no_custody(connection_id, env_name):
    """No credential custody layer exists yet (A3). An API-key route therefore refuses to launch."""
    raise KeyError(connection_id)


def route_unsupported(entry):
    """Why the daemon will not launch a loaded route entry, or None when it may.

    Two kinds of route bring a credential the daemon has no custody layer to hand to the engine: an
    API-key route whose key comes from the user's own ``apiKeyHelper`` (``key_source: api_key_helper``),
    and a ``passthrough`` route (Bedrock, Vertex, Foundry, a gateway). The launcher's child environment
    gives the engine a home of its own and only the policy's variables, so the helper's settings and the
    cloud variables never reach it, while the user's sign-in does: the run would be carried by the
    subscription and recorded and metered as the other connection. Until custody exists both are
    refused ``launch_refused`` before anything is bound or launched. The subscription route is not
    touched, and an API-key route with ``key_source: custody`` keeps its own refusal
    (``no credential``).
    """
    route = entry.get('route') if isinstance(entry, dict) else None
    connection = route.get('connection') if isinstance(route, dict) else None
    if not isinstance(connection, dict):
        return None
    if connection.get('kind') == 'passthrough':
        return 'no credential custody: a passthrough route is not launched'
    if connection.get('key_source') == 'api_key_helper':
        return 'no credential custody: a key_source api_key_helper route is not launched'
    return None


class StartRefused(Exception):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code, self.detail = code, detail


class RouteStarter:
    """``starter(params) -> {"cgroup"}`` for the control API.

    The route request runs on its own thread, because the adapter drives the engine until its
    result. The starter returns once the launcher recorded the unit's cgroup, or raises when the
    request ended first (a refusal, a launch failure). A start that outlives ``start_timeout_s``
    is abandoned: the launcher refuses or ends the unit if it gets there later.
    """

    def __init__(self, service, routes, daemon, state_dir, *, start_timeout_s=20.0, exit_grace_s=5.0,
                 breaker=None, breaker_unavailable=None):
        self._service, self._routes, self._daemon = service, dict(routes), daemon
        # ``breaker`` is the DurableRouteBreaker (None in a test that composes without one);
        # ``breaker_unavailable`` is why its store could not be opened, and refuses every start.
        self._breaker, self._breaker_unavailable = breaker, breaker_unavailable
        self._state = Path(state_dir)
        self._start_timeout_s, self._exit_grace_s = start_timeout_s, exit_grace_s
        self._lock = threading.Lock()
        self._active = {}
        self.failures = {}   # run_id -> {"code", "detail"} of a start that did not launch
        self.results = {}    # run_id -> route result, when it could not be written to the run directory

    def active(self, run_id):
        with self._lock:
            return run_id in self._active

    def __call__(self, params):
        run_id, engine = params['run_id'], params['engine']
        if not RUN_ID.fullmatch(run_id):
            raise self._refuse(run_id, 'request_invalid', 'run_id')
        entry = self._routes.get(engine)
        if entry is None:
            raise self._refuse(run_id, 'route_not_configured', engine)
        unsupported = route_unsupported(entry)
        if unsupported is not None:
            raise self._refuse(run_id, route_contract.LAUNCH_REFUSED, unsupported)
        if self._breaker_unavailable is not None:
            raise self._refuse(run_id, 'breaker_store_unavailable', self._breaker_unavailable)
        connection = route_connection(entry)
        if self._breaker is not None and connection is not None:
            if not self._breaker.representable(connection):
                # A limit on this connection could never be kept across a restart: refuse, do not run unfenced.
                raise self._refuse(run_id, 'breaker_connection_unrepresentable', 'connection id')
            self._breaker.bind_run(run_id, connection, engine, str(entry['route'].get('model')))
        request = {'contract': route_contract.CONTRACT_VERSION, 'request_id': run_id,
                   'tenant': params['tenant'], 'prompt': params['prompt'],
                   'deadline_s': entry['deadline_s'], 'route': json.loads(json.dumps(entry['route']))}
        ctx = RunContext(run_id)
        with self._lock:
            if run_id in self._active:
                raise self._refuse(run_id, 'start_in_flight', '')
            self._active[run_id] = ctx
            self.failures.pop(run_id, None)
        threading.Thread(target=self._drive, args=(ctx, request), name=f'recursion-run-{run_id}',
                         daemon=True).start()
        if ctx.wait_started(self._start_timeout_s):
            return {'cgroup': ctx.cgroup}
        result = ctx.result
        if result is None:
            ctx.cancelled = True
            raise self._refuse(run_id, 'launch_timeout', '')
        raise self._refuse(run_id, str(result.get('code') or 'engine_exited'), str(result.get('detail') or ''))

    def _refuse(self, run_id, code, detail):
        with self._lock:
            self.failures[run_id] = {'code': code, 'detail': detail}
        return StartRefused(code, detail)

    def _drive(self, ctx, request):
        try:
            with bound_run(ctx):
                try:
                    result = self._service.handle(request)
                except Exception as exc:  # noqa: BLE001 - a route bug must not leave the start waiting
                    log.exception('route request for %s failed', ctx.run_id)
                    result = route_contract.result(ctx.run_id, route_contract.ENGINE_ERROR,
                                                   detail=f'route failure: {type(exc).__name__}')
            closing = getattr(self._daemon, 'closed', False)  # shutdown: the run is left to the next daemon
            if ctx.process is not None and not closing:
                self._record_result(ctx.run_id, result)
                if self._breaker is not None and result.get('code') is not None:
                    self._breaker.fault_run(ctx.run_id)  # an effect citing this run is refused (4.2)
            ctx.finished(result)
            if ctx.process is not None:
                if closing:
                    ctx.process.close()
                else:
                    self._settle(ctx)
        finally:
            with self._lock:
                self._active.pop(ctx.run_id, None)

    def _record_result(self, run_id, result):
        try:
            path = self._state / 'runs' / run_id / 'result.json'
            tmp = path.with_name('result.json.tmp')
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            log.exception('route result for %s was not written', run_id)
            with self._lock:
                self.results[run_id] = result

    def _settle(self, ctx):
        """After the route result: give the engine a moment to exit, then end what is left of its tree."""
        process = ctx.process
        try:
            deadline = time.monotonic() + self._exit_grace_s
            while time.monotonic() < deadline:
                if getattr(self._daemon, 'closed', False):
                    return  # the daemon is shutting down; it never stops a run on its way out
                try:
                    if self._daemon.reconcile(ctx.run_id).state in TERMINAL:
                        return
                except Exception:  # noqa: BLE001
                    log.exception('observing %s after its result failed', ctx.run_id)
                    break
                time.sleep(0.2)
            process.fence('result_recorded')
        finally:
            process.close()


# -- the object every surface calls -----------------------------------------------------------------
class DaemonControl:
    """``call(method, params) -> dict`` for the socket, the CLI and the GUI."""

    def __init__(self, api, registry, daemon, starter, state_dir, *, routes=None, plugins=None, breaker=None,
                 breaker_store='not composed', list_reply_bytes=LIST_REPLY_BYTES):
        self.api, self.registry, self.daemon, self.starter = api, registry, daemon, starter
        self._list_reply_bytes = list_reply_bytes
        self._state = Path(state_dir)
        self._routes, self.plugins, self._breaker = dict(routes or {}), plugins, breaker
        self._breaker_store = breaker_store
        self._stop_retry = {}  # run_id -> (not before, wait) for a Stop reconcile() could not get sent
        self._starts_lock = threading.Lock()
        self._starts_in_flight = 0  # run.start calls of this process that have not answered yet

    # -- calls ---------------------------------------------------------------------------------
    def call(self, method, params=None):
        return self._answer(method, params, 0)

    def _answer(self, method, params, reserved_bytes):
        if method == 'run.list':
            return self._list(params, reserved_bytes)
        if method == 'route.list':
            return {'code': 'ok', 'routes': self.route_records(), 'breaker_store': self._breaker_store}
        if method == 'plugin.list':
            if self.plugins is None:
                return {'code': 'unavailable', 'detail': 'no plugin host is composed'}
            return {'code': 'ok', **self.plugins.inventory()}
        if method != 'run.start':
            answer = self.api.call(method, dict(params or {}))
        else:
            # The API writes the launch intent before the starter knows the run. reconcile() must not
            # read that gap as a start that never reached a unit (ADV1-8).
            with self._starts_lock:
                self._starts_in_flight += 1
            try:
                answer = self.api.call(method, dict(params or {}))
            finally:
                with self._starts_lock:
                    self._starts_in_flight -= 1
        if method == 'run.start' and answer.get('code') == 'start_failed':
            failure = self.starter.failures.get(answer.get('run_id'))
            if failure is not None:
                answer['start_error'] = dict(failure)
        if isinstance(answer.get('run'), dict):
            self._observe(answer['run'], full=True)
        if isinstance(answer.get('runs'), list):
            for run in answer['runs']:
                if isinstance(run, dict):
                    self._observe(run, full=False)
        return answer

    def _list(self, params, reserved_bytes):
        """``run.list``, cut to what one control frame can carry (ADV1-5).

        The API answers the page asked for (``limit`` / ``after``). Each run is observed as before and
        kept while the answer still fits ``list_reply_bytes`` less ``reserved_bytes``; the first run that
        would not fit ends the page, and ``next_after`` names the last run kept. An answer that fits is
        returned unchanged, so a no-parameter list below the frame is what it was. A single run that
        does not fit alone is still sent: the transport answers ``response_too_large``, never a page
        that leaves the run out.
        """
        answer = self.api.call('run.list', dict(params or {}))
        runs = answer.get('runs')
        if answer.get('code') != 'ok' or not isinstance(runs, list):
            return answer
        budget = self._list_reply_bytes - reserved_bytes
        used = _wire_size({k: v for k, v in answer.items() if k != 'next_after'} | {'runs': []})
        for i, run in enumerate(runs):
            if isinstance(run, dict):
                self._observe(run, full=False)
            used += _wire_size(run) + (1 if i else 0)
            if used > budget and i:
                kept = runs[:i]
                return {**answer, 'runs': kept, 'next_after': kept[-1]['run_id']}
        return answer

    def route_records(self):
        """The route inventory: what this daemon can launch, what it serves, and its plugins.

        Read from the routes the daemon loaded and the plugin host, at the time of the call.
        No bar is claimed: the daemon records no bar evidence, so every row is beta (O50).
        """
        records = []
        for engine in WIRED_ENGINES:
            entry = self._routes.get(engine)
            record = {'id': f'engine:{engine}', 'class': 'engine', 'engine': engine, 'sign_in': '',
                      'configured': False, 'maturity': 'beta', 'beta': True, 'bars': {}}
            if entry is None:
                record['blocker'] = 'route_not_configured: no entry for this engine in routes.json'
            else:
                route = entry['route']
                connection = route.get('connection')
                kind = connection.get('kind') if isinstance(connection, dict) else None
                record.update(configured=True, sign_in=kind if isinstance(kind, str) else '',
                              model=str(route.get('model')), blocker=NO_BAR_EVIDENCE)
                key = route_connection(entry)
                if self._breaker is not None and key is not None:
                    state, tripped = self._breaker.state(key), self._breaker.tripped(key)
                    if tripped is None:
                        # The store could not be read: whether it holds a trip is unknown, so is the
                        # connection; a start on it is refused until the store reads (ADV2-1, ADV2-6).
                        state = 'unknown'
                    elif tripped and state == 'closed':
                        state = 'open'  # tripped under an earlier daemon; this one fences it at the first request
                    record['breaker'] = {'connection': key, 'state': state, 'tripped_in_store': tripped}
                    if tripped is None:
                        record['blocker'] = ('breaker state unknown: the breaker store could not be read; '
                                             f'starts on this connection are refused; {NO_BAR_EVIDENCE}')
                    elif tripped or state != 'closed':
                        record['blocker'] = f'breaker {state} on this connection (typed limit); {NO_BAR_EVIDENCE}'
                unsupported = route_unsupported(entry)
                if unsupported is not None:  # every start on this route is refused, whatever the breaker says
                    record['blocker'] = f'{route_contract.LAUNCH_REFUSED}: {unsupported}'
            records.append(record)
        records.append({'id': 'surface:cli', 'class': 'surface', 'surface': 'cli', 'sign_in': '', 'configured': True,
                        'maturity': 'beta', 'beta': True, 'bars': {}, 'blocker': NO_BAR_EVIDENCE})
        if self.plugins is not None:
            panels = sum(len(entry.get('panels', ())) for entry in self.plugins.registries)
            records.append({'id': 'plugin_panels', 'class': 'plugin_panels', 'sign_in': '', 'configured': panels > 0,
                            'maturity': 'beta', 'beta': True, 'bars': {},
                            'blocker': f'{panels} panel(s) from loaded plugins; {NO_BAR_EVIDENCE}'})
            records.extend(self.plugins.route_records())
        return records

    def _row(self, run_id):
        try:
            return self.daemon.registry.get(run_id)
        except KeyError:
            return None
        except Exception:  # noqa: BLE001 - an unreadable registry is reported, never guessed around
            log.exception('daemon registry read for %s failed', run_id)
            return False

    def _observe(self, run, *, full):
        """Add what the daemon last observed of the run's unit. The stored record is not changed."""
        run_id = run.get('run_id')
        row = self._row(run_id) if isinstance(run_id, str) else None
        if row is False:
            run['observed'] = {'state': 'unknown', 'detail': 'daemon registry unreadable'}
        elif row is None:
            run['observed'] = None  # no unit was ever recorded for this run
        else:
            run['observed'] = {'state': row.state, 'detail': row.detail, 'unit': row.unit,
                               'stop_requested': row.stop_requested}
            if run.get('state') in ('running', 'starting') and row.state in (*TERMINAL, 'unknown'):
                # The record says the run is live and the last observation does not: say what was seen.
                run['recorded_state'] = run['state']
                run['state'] = {'terminated': ENDED, 'lost_reboot': LOST_REBOOT}.get(row.state, 'unknown')
        result = self._result(run_id) if isinstance(run_id, str) else None
        if result is not None:
            run['result'] = result if full else {k: result.get(k) for k in ('status', 'code', 'detail')}
        if full and isinstance(run_id, str):
            # run.status carries the tail of the engine's stdout, so every surface that can read a
            # status can watch output arrive without a second method (the CLI and TUI share one
            # command set). ``complete`` is true only once the unit is recorded ended.
            output = self._output(run_id, live=not row or row.state not in TERMINAL)
            if output is not None:
                run['output'] = output

    def _result(self, run_id):
        kept = self.starter.results.get(run_id)
        if kept is not None:
            return kept
        if not RUN_ID.fullmatch(run_id):
            return None
        try:
            data = json.loads((self._state / 'runs' / run_id / 'result.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _output(self, run_id, live):
        """The tail of the engine's stdout file: whole lines only, newest last. A read of a file."""
        if not RUN_ID.fullmatch(run_id):
            return None
        try:
            with open(self._state / 'runs' / run_id / 'stdout.jsonl', 'rb') as handle:
                size = os.fstat(handle.fileno()).st_size
                start = max(0, size - OUTPUT_TAIL_BYTES)
                handle.seek(start)
                data = handle.read(OUTPUT_TAIL_BYTES)
        except FileNotFoundError:
            return None  # the run never reached a launch
        except OSError as exc:
            return {'unavailable': type(exc).__name__}
        if start:  # the window opens mid-line: drop the partial first line
            data = data[data.find(b'\n') + 1:] if b'\n' in data else b''
        cut = data.rfind(b'\n')
        whole = data if not live else data[:cut + 1]  # a last line with no newline is complete once the engine is gone
        lines = [line.decode('utf-8', 'replace') for line in whole.splitlines()][-OUTPUT_TAIL_LINES:]
        return {'bytes': size, 'lines': lines, 'complete': not live}

    # -- recovery ------------------------------------------------------------------------------
    def reconcile(self):
        """Bring control records in line with what the sweep attested. Called after every sweep.

        * A Stop recorded by the daemon registry (a Stop cut short by a daemon death, or a fence)
          whose control record is not ``stopped`` is finished through ``run.stop``: the same
          request, confirm and attestation path as any Stop.
        * A ``starting`` record with no cgroup that this process is not launching is a start the
          previous daemon did not finish recording. With a unit row, the unit's cgroup is recorded
          so Stop can reach it; with none, no unit was ever started and it is ``start_failed``
          (decided only while no ``run.start`` of this process is in flight).
        """
        try:
            records = self.registry.list()
        except Exception:  # noqa: BLE001
            log.exception('control registry unreadable; nothing reconciled this pass')
            return
        for rec in records:
            run_id = rec['run_id']
            try:
                self._reconcile_one(rec, self._row(run_id))
            except StaleGeneration:
                pass  # the record moved under us; the next pass reads it again
            except Exception:  # noqa: BLE001
                log.exception('control reconcile of %s failed', run_id)

    def _reconcile_one(self, rec, row):
        run_id = rec['run_id']
        if row is False:
            return
        if rec.get('state') == 'starting' and not rec.get('cgroup'):
            if self.starter.active(run_id):
                return  # this process is still launching it; the start records its own outcome
            if row is None:
                with self._starts_lock:
                    if self._starts_in_flight:
                        return  # it may be a start in flight here; a start left by an earlier daemon waits a pass
                rec['state'] = 'start_failed'
                self.registry.put(rec)
                return
            if row.state not in TERMINAL:
                group, _ = self.daemon.manager.unit_identity(row.unit)
                if group:
                    rec.update(state='running', cgroup=group)
                    rec = self.registry.put(rec)
        if rec.get('state') == 'stopped' or not rec.get('cgroup'):
            return
        stop = rec.get('stop')
        if not ((row is not None and row.stop_requested) or rec.get('state') == 'stop_pending' or stop is not None):
            return
        if rec.get('state') == 'stop_pending' and isinstance(stop, dict) and stop.get('requested') \
                and (stop.get('cgroup'), stop.get('cgroup_marker')) == (rec.get('cgroup'), rec.get('cgroup_marker')):
            # The request was sent and "pending" is recorded. Nothing is written or waited for again
            # until one observation of the unit attests the end (ADV1-6: no rewrite every sweep).
            if row is None or self.daemon.reconcile(run_id).state != 'terminated':
                return
        else:
            held = self._stop_retry.get(run_id)
            if held is not None and time.monotonic() < held[0]:
                return
        if self.api.call('run.stop', {'run_id': run_id}).get('code') == 'pending':
            wait = min(self._stop_retry[run_id][1] * 2, STOP_RETRY_MAX_S) if run_id in self._stop_retry \
                else STOP_RETRY_BASE_S
            self._stop_retry[run_id] = (time.monotonic() + wait, wait)
        else:
            self._stop_retry.pop(run_id, None)

    # -- socket --------------------------------------------------------------------------------
    def handle(self, message, conn):
        """``{"op": "control", "method", "params"}`` -> ``{"ok": true, "result": <control answer>}``."""
        if message.get('op') != 'control':
            return {'ok': False, 'error': {'code': 'unknown_op', 'op': message.get('op')}}
        method, params = message.get('method'), message.get('params')
        if not isinstance(method, str) or not isinstance(params, (dict, type(None))):
            return {'ok': False, 'error': {'code': 'request_invalid'}}
        if peer_is_engine(conn.peer.pid):
            # The uid check passes for every engine child too (control_transport.handlers). Until
            # scopes exist (D3), a peer inside a run unit, or one whose cgroup cannot be read, gets
            # no control at all.
            return {'ok': False, 'error': {'code': 'scope_refused', 'detail': 'peer is inside a run unit'}}
        # The transport echoes the request's "id" in the reply frame; a run.list page leaves room for it.
        reserved = _wire_size(message['id']) + len(',"id":') if 'id' in message else 0
        return {'ok': True, 'result': self._answer(method, params, reserved)}

    # -- GUI -----------------------------------------------------------------------------------
    def gui_transport(self, method, path, body):
        """The GUI client's transport, in process: Stop, the route inventory and the O7 reads."""
        if (method, path) == ('POST', '/v1/runs/stop'):
            try:
                payload = json.loads(body or b'')
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return 400, b'{"error": "request_invalid"}'
            answer = self.call('run.stop', {'run_id': payload.get('run_id')})
            return 200, json.dumps(answer, sort_keys=True).encode()
        if (method, path) == ('GET', '/v1/routes'):
            return 200, json.dumps({'routes': self.route_records()}, sort_keys=True).encode()
        if (method, path) == ('POST', '/v1/operator'):
            try:
                payload = json.loads(body or b'')
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return 400, b'{"error": "request_invalid"}'
            if self.plugins is None:
                return 503, b'{"error": "unavailable: no plugin host is composed"}'
            status, answer = self.plugins.operator(payload)
            return status, json.dumps(answer, sort_keys=True).encode()
        return 404, b'{"error": "not_found"}'


def admit_peer(cred):
    """Asked by the socket at accept, before a slot: a peer inside a run unit is closed at once (ADV1-4)."""
    return not peer_is_engine(cred.pid)


def is_stop_request(message):
    return message.get('op') == 'control' and message.get('method') == 'run.stop'


def peer_is_engine(pid):
    """True when the peer is inside a ``recursion-run-*`` unit, or its cgroup cannot be read."""
    if not isinstance(pid, int) or pid <= 0:
        return True
    try:
        text = Path(f'/proc/{pid}/cgroup').read_text()
    except OSError:
        return True
    return any('recursion-run-' in part for line in text.splitlines() for part in line.split('/'))


def door_directory(directory):
    """Create the door's directory at 0700, or check the one that is there.

    Like the transport (``ensure_private_dir``), an existing directory that is a symlink, is not
    ours, or grants any group or other bit is refused, not repaired: the door runs before the
    transport's check, and a chmod here would hide a wrong place from it (ADV1-13).
    """
    path = Path(directory)
    if not path.exists() and not path.is_symlink():
        return private_dir(path)
    require_launcher_platform()
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError(f'{path} must be an operator-owned directory')
    if info.st_mode & 0o077:
        raise PermissionError(f'{path} has mode {stat.S_IMODE(info.st_mode):04o}, want 0700')
    return path


class DeviceDoor:
    """The GUI's device door: a per-daemon-start secret in an owner-only runtime file.

    A request passes only with ``Authorization: Bearer <token>`` or the ``recursion_device`` cookie
    carrying the exact token (constant-time compare). The token is made new at every daemon start,
    lives only in ``$XDG_RUNTIME_DIR/recursion/gui-device-token`` (0600 in a 0700 directory) and is
    never logged. There is no pairing page and no remote path: holding the token means being able
    to read the operator's runtime directory on this machine.

    ``publish=False`` makes the door without writing the file; ``publish()`` writes it. The composition
    publishes only once the control socket is its own, and ``close`` removes only the file this door
    wrote, so a composition that loses the socket lock leaves the live daemon's token file alone (ADV1-12).
    """

    def __init__(self, directory, *, publish=True):
        door_directory(directory)
        self.path = Path(directory) / DEVICE_TOKEN_FILE
        self._token = secrets.token_urlsafe(32)
        self._published = None  # (device, inode) of the token file this door wrote
        if publish:
            self.publish()

    def publish(self):
        tmp = self.path.with_name(DEVICE_TOKEN_FILE + '.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, self._token.encode() + b'\n')
            info = os.fstat(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        self._published = (info.st_dev, info.st_ino)

    def __call__(self, env):
        offered = []
        header = env.get('HTTP_AUTHORIZATION')
        if isinstance(header, str) and header.startswith('Bearer '):
            offered.append(header[len('Bearer '):])
        cookie = env.get('HTTP_COOKIE')
        if isinstance(cookie, str):
            for part in cookie.split(';'):
                name, _, value = part.strip().partition('=')
                if name == DEVICE_COOKIE:
                    offered.append(value)
        return any(hmac.compare_digest(value.encode('utf-8', 'replace'), self._token.encode())
                   for value in offered)

    def close(self):
        published, self._published = self._published, None
        try:
            info = os.lstat(self.path)
            if published == (info.st_dev, info.st_ino):  # never a file another door wrote
                os.unlink(self.path)
        except OSError:
            pass


class Composition:
    """What ``compose_control`` built, and how to stop it. Closing never stops a run."""

    def __init__(self, control, server, door, plugins=None, breaker=None):
        self.control, self.server, self.door = control, server, door
        self.plugins, self.breaker = plugins, breaker
        self.client = ControlClient(control.gui_transport)

    def close(self):
        try:
            self.server.close()
        finally:
            try:
                self.door.close()
            finally:
                for item in (self.plugins, self.breaker):
                    if item is not None:
                        item.close()


def compose_control(daemon, state_dir, *, environ=None, routes=None, clock=time.monotonic,
                    confirm_timeout_s=5.0, plugin_dirs=(), plugin_options=None):
    """Build the control API over ``daemon`` and serve it on the owner-only socket.

    ``daemon.manager`` must be an ``EngineUnitManager``. ``routes`` overrides the operator's
    route file (tests). A route file that cannot be trusted leaves every start refused
    (``route_not_configured``) and is logged; reads and Stop still work. The same holds for a
    breaker store that exists and cannot be opened (``breaker_store_unavailable``). A loaded route
    the daemon has no credential custody for (``route_unsupported``) is logged here and refused
    ``launch_refused`` at each start; the other routes are not affected.

    Operator plugins under ``$XDG_CONFIG_HOME/recursion/plugins`` and in ``plugin_dirs`` are
    loaded through ``plugins.PluginHost``; one that is refused is kept in the inventory with its
    finding and the daemon runs without it. ``plugin_options`` are ``PluginHost`` keyword
    arguments for tests (a simulated GIL probe on a GIL build, a missing verification backend).
    """
    require_launcher_platform()  # Windows: typed PlatformUnsupported; no socket, token file or state is made
    environ = dict(os.environ if environ is None else environ)
    manager = daemon.manager
    if not isinstance(manager, EngineUnitManager):
        raise TypeError('compose_control needs an EngineUnitManager')
    state_dir = Path(state_dir)
    if routes is None:
        config = Path(environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')) / 'recursion' / ROUTES_FILE
        try:
            routes = load_routes(config)
        except RoutesUnreadable as exc:
            log.error('no route can start: %s', exc)
            routes = {}
    for engine, entry in sorted(routes.items()):
        unsupported = route_unsupported(entry)
        if unsupported is not None:
            log.error('route %s cannot start (%s): %s', engine, route_contract.LAUNCH_REFUSED, unsupported)
    config_dir = Path(environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')) / 'recursion'
    registry = DurableRunRegistry(daemon.registry, processes=pid_alive(manager))
    stored = breaker = breaker_unavailable = None
    route_breaker = ConnectionBreaker(clock)
    try:
        stored, created = open_breaker_store(state_dir, clock=clock,
                                             probe_lease_seconds=store_probe_lease_s(routes, route_breaker))
    except (Refused, OSError, sqlite3.Error) as exc:
        breaker_unavailable = exc.code if isinstance(exc, Refused) else type(exc).__name__
        log.error('breaker store cannot be opened (%s): every start is refused; reads and Stop still work',
                  breaker_unavailable)
    else:
        breaker = DurableRouteBreaker(route_breaker, stored)
        if created and registry.list():
            log.warning('the breaker store was missing and was created empty while run records exist: '
                        'earlier trips, run bindings and faults are not known to this daemon')
    plugins = PluginHost(config_dir, state_dir, stored, plugin_dirs=plugin_dirs, **(plugin_options or {}))
    adapters = {}
    for engine, cls in (('claude_code', ClaudeCodeAdapter), ('codex', CodexAdapter)):
        entry = routes.get(engine)
        launcher = ProductionLauncher(engine, daemon, manager, state_dir)
        options = {'binary': entry['binary']} if entry and entry.get('binary') else {}
        adapters[engine] = cls(launcher, clock, no_custody, environ, **options)
    service = RouteService(adapters, breaker if breaker is not None else route_breaker, clock)
    starter = RouteStarter(service, routes, daemon, state_dir, breaker=breaker,
                           breaker_unavailable=breaker_unavailable)
    api = ControlAPI(registry, UnitKillTree(daemon), starter, time.time, confirm_timeout_s=confirm_timeout_s)
    control = DaemonControl(api, registry, daemon, starter, state_dir, routes=routes, plugins=plugins,
                            breaker=breaker, breaker_store='durable' if breaker is not None
                            else f'unavailable: {breaker_unavailable}')
    directory = socket_directory(environ)
    door = DeviceDoor(directory, publish=False)
    server = UnixControlServer(directory, with_ping_health(control.handle), admit=admit_peer,
                               priority=is_stop_request, reserved_connections=STOP_RESERVED_CONNECTIONS)
    def release():
        door.close()  # removes only a token file this door wrote
        for item in (plugins, breaker):
            if item is not None:
                item.close()

    try:
        server.start()
    except BaseException:
        release()
        raise
    try:
        door.publish()  # only now: the socket lock is ours, so no live daemon's token file is replaced
    except BaseException:
        server.close()
        release()
        raise
    return Composition(control, server, door, plugins, breaker)
