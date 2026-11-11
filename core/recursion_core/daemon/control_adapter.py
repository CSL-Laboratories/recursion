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
``recorded_state``. Reads never write.

Not here: custody (an API-key route has no credential source and refuses), the O7 operator
reads and the route inventory (the GUI is told they are not implemented), scopes on the
socket beyond the uid check, and the Control MCP transport.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
import time
import uuid

from recursion_core.control.api import ControlAPI
from recursion_core.control.client import socket_directory
from recursion_core.control.registry import InMemoryRunRegistry, StaleGeneration
from recursion_core.control_transport import UnixControlServer, with_ping_health
from recursion_core.gui.client import ControlClient
from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as route_contract

from . import RUN_ID, TERMINAL
from .launcher import (EngineUnitManager, ProductionLauncher, RunContext, UnitKillTree, bound_run,
                       private_dir, require_launcher_platform)

log = logging.getLogger(__name__)

DEVICE_TOKEN_FILE = 'gui-device-token'
DEVICE_COOKIE = 'recursion_device'
ROUTES_FILE = 'routes.json'         # $XDG_CONFIG_HOME/recursion/routes.json
OUTPUT_TAIL_BYTES = 64 * 1024
OUTPUT_TAIL_LINES = 50
ENDED, LOST_REBOOT = 'ended', 'lost_reboot'
# reconcile() asks again for a Stop whose request never got through only after a wait that doubles
# from the base to the cap (ADV1-6).
STOP_RETRY_BASE_S, STOP_RETRY_MAX_S = 30.0, 600.0
# Socket connections kept for run.stop when every ordinary one is taken (ADV1-4).
STOP_RESERVED_CONNECTIONS = 4


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
        if engine not in ('claude_code', 'codex') or not isinstance(entry, dict):
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


def no_custody(connection_id, env_name):
    """No credential custody layer exists yet (A3). An API-key route therefore refuses to launch."""
    raise KeyError(connection_id)


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

    def __init__(self, service, routes, daemon, state_dir, *, start_timeout_s=20.0, exit_grace_s=5.0):
        self._service, self._routes, self._daemon = service, dict(routes), daemon
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

    def __init__(self, api, registry, daemon, starter, state_dir):
        self.api, self.registry, self.daemon, self.starter = api, registry, daemon, starter
        self._state = Path(state_dir)
        self._stop_retry = {}  # run_id -> (not before, wait) for a Stop reconcile() could not get sent
        self._starts_lock = threading.Lock()
        self._starts_in_flight = 0  # run.start calls of this process that have not answered yet

    # -- calls ---------------------------------------------------------------------------------
    def call(self, method, params=None):
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
        return {'ok': True, 'result': self.call(method, params)}

    # -- GUI -----------------------------------------------------------------------------------
    def gui_transport(self, method, path, body):
        """The GUI client's transport, in process. Only Stop exists; the rest says so."""
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
            return 501, b'{"error": "route_inventory_not_implemented"}'
        if (method, path) == ('POST', '/v1/operator'):
            return 501, b'{"error": "operator_contract_not_implemented"}'
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
        private_dir(directory)
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

    def __init__(self, control, server, door):
        self.control, self.server, self.door = control, server, door
        self.client = ControlClient(control.gui_transport)

    def close(self):
        try:
            self.server.close()
        finally:
            self.door.close()


def compose_control(daemon, state_dir, *, environ=None, routes=None, clock=time.monotonic,
                    confirm_timeout_s=5.0):
    """Build the control API over ``daemon`` and serve it on the owner-only socket.

    ``daemon.manager`` must be an ``EngineUnitManager``. ``routes`` overrides the operator's
    route file (tests). A route file that cannot be trusted leaves every start refused
    (``route_not_configured``) and is logged; reads and Stop still work.
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
    registry = DurableRunRegistry(daemon.registry, processes=pid_alive(manager))
    adapters = {}
    for engine, cls in (('claude_code', ClaudeCodeAdapter), ('codex', CodexAdapter)):
        entry = routes.get(engine)
        launcher = ProductionLauncher(engine, daemon, manager, state_dir)
        options = {'binary': entry['binary']} if entry and entry.get('binary') else {}
        adapters[engine] = cls(launcher, clock, no_custody, environ, **options)
    service = RouteService(adapters, ConnectionBreaker(clock), clock)
    starter = RouteStarter(service, routes, daemon, state_dir)
    api = ControlAPI(registry, UnitKillTree(daemon), starter, time.time, confirm_timeout_s=confirm_timeout_s)
    control = DaemonControl(api, registry, daemon, starter, state_dir)
    directory = socket_directory(environ)
    door = DeviceDoor(directory, publish=False)
    server = UnixControlServer(directory, with_ping_health(control.handle), admit=admit_peer,
                               priority=is_stop_request, reserved_connections=STOP_RESERVED_CONNECTIONS)
    server.start()
    try:
        door.publish()  # only now: the socket lock is ours, so no live daemon's token file is replaced
    except BaseException:
        server.close()
        raise
    return Composition(control, server, door)
