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

The socket binds the caller's principal from the peer credentials and asks the authorizer
before the method runs. The peer whose uid is the daemon's is the owner and keeps every
scope. A named peer with any other uid holds nothing (no device credential). Credentials
that cannot be read, and a uid that names no process, are refused and are not the owner.

Not here: custody (an API-key route has no credential source and refuses; a route that brings
its own credential, ``key_source: api_key_helper`` or ``passthrough``, is refused before launch,
see ``route_unsupported``), the O7 interventions, bar evidence for any route (every route reads
BETA), a device credential, and the Control MCP transport.
"""
from __future__ import annotations

import errno
import hmac
import json
import logging
import math
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
from recursion_core.control.hooks import Hooks, Principal
from recursion_core.control.registry import InMemoryRunRegistry, StaleGeneration
from recursion_core.control.scopes import register as register_scopes
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


def _scope_hooks():
    """The authorizer in seam S5, and nothing else. The credential service stays unregistered."""
    hooks = Hooks()
    register_scopes(hooks)
    hooks.freeze()
    return hooks


def _scope_refused(detail):
    return {'ok': False, 'error': {'code': 'scope_refused', 'detail': detail}}
# reconcile() asks again for a Stop whose request never got through only after a wait that doubles
# from the base to the cap (ADV1-6).
STOP_RETRY_BASE_S, STOP_RETRY_MAX_S = 30.0, 600.0
# Socket connections kept for run.stop when every ordinary one is taken (ADV1-4).
STOP_RESERVED_CONNECTIONS = 4
# The most bytes a run.list answer may take: the socket's frame, less room for the reply envelope
# ({"ok":true,"result":...} and next_after). The request's own "id" is counted per call (ADV1-5).
LIST_ENVELOPE_BYTES = 4096
LIST_REPLY_BYTES = MAX_FRAME_BYTES - LIST_ENVELOPE_BYTES
# The most bytes the route result of a run.status answer may take. The output tail and the run record share
# the frame with it, so a result past this is shortened until the answer fits (E18). Every member that can
# be large is shortened, not only text: a large detail alone used to fail the whole answer.
RESULT_STATUS_BYTES = MAX_FRAME_BYTES // 4
# Members of the status branch of run.view.v1 ``result``. A cut keeps these and adds none: the record
# has no mark for a shortened result, and an added member matches no branch of that schema.
_STATUS_RESULT_KEYS = ('contract', 'request_id', 'status', 'code', 'text', 'limit', 'events', 'detail',
                       'retry_at', 'breaker_effect', 'limit_notice')
_LIST_RESULT_KEYS = ('status', 'code', 'detail')


def _wire_size(value):
    """Bytes ``value`` takes with the control frame's JSON separators.

    ``surrogatepass`` counts a lone surrogate instead of raising, and a non-finite float is
    counted as its dump text. ``framing.encode_frame`` refuses both, so a route result is
    rewritten with ``_frame_scalar`` before an answer carries it. Other text keeps the same
    length as a strict UTF-8 encode.
    """
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str).encode(
        'utf-8', 'surrogatepass'))


def _frame_scalar(value):
    """A value ``framing.encode_frame`` can send. ``value`` itself is not changed.

    A lone surrogate code point becomes one U+FFFD. A non-finite float becomes None.
    A list, a tuple or an object is walked. When nothing needs that, ``value`` is returned
    as it stands, so a clean result stays the same object.

    Two object keys can frame to one text (two keys that differ only in lone surrogates, or
    a lone surrogate and a key that already holds U+FFFD). Every member stays on the answer: a key
    that was not rewritten keeps its name, and a rewritten key that meets a name already
    taken gets a counted mark, ``~2``, then ``~3``, until the name is free.

    The walk is an explicit stack. A call per nesting level raised ``RecursionError``
    out of ``run.list`` on a result the frame encoder can send.
    """
    def frame_text(text):
        if any('\ud800' <= char <= '\udfff' for char in text):
            return ''.join('\ufffd' if '\ud800' <= char <= '\udfff' else char for char in text)
        return text

    if isinstance(value, str):
        return frame_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, (dict, list, tuple)):
        return value

    # ``pending`` stops a cyclic object from walking forever. A repeated object is left as it is.
    framed = {}
    pending = set()
    stack = [('visit', value)]
    while stack:
        kind, node = stack.pop()
        if kind == 'visit':
            if id(node) in framed:
                continue
            if isinstance(node, str):
                framed[id(node)] = frame_text(node)
            elif isinstance(node, float):
                framed[id(node)] = node if math.isfinite(node) else None
            elif isinstance(node, dict):
                if id(node) in pending:
                    framed[id(node)] = node
                    continue
                pending.add(id(node))
                stack.append(('dict', node))
                for key, item in node.items():
                    stack.append(('visit', item))
                    if isinstance(key, str):
                        stack.append(('visit', key))
            elif isinstance(node, (list, tuple)):
                if id(node) in pending:
                    framed[id(node)] = node
                    continue
                pending.add(id(node))
                stack.append(('seq', node))
                for item in node:
                    stack.append(('visit', item))
            else:
                framed[id(node)] = node
        elif kind == 'dict':
            pending.discard(id(node))
            changed = False
            for key, item in node.items():
                new_key = framed[id(key)] if isinstance(key, str) else key
                new_item = framed[id(item)]
                if new_key is not key or new_item is not item:
                    changed = True
                    break
            if not changed:
                framed[id(node)] = node
                continue
            out = {}
            kept = {key for key in node if isinstance(key, str) and framed[id(key)] is key}
            for key, item in node.items():
                new_key = framed[id(key)] if isinstance(key, str) else key
                if new_key is not key:
                    base, count = new_key, 1
                    while new_key in out or new_key in kept:
                        count += 1
                        new_key = f'{base}~{count}'
                out[new_key] = framed[id(item)]
            framed[id(node)] = out
        else:
            pending.discard(id(node))
            changed = False
            built = []
            for item in node:
                new_item = framed[id(item)]
                if new_item is not item:
                    changed = True
                built.append(new_item)
            framed[id(node)] = node if not changed else type(node)(built)
    return framed[id(value)]


def _fit_chars(short, key, *, force=False):
    """Shorten ``short[key]`` to the longest head that lets ``short`` fit the status budget.

    Without ``force``, a member that is still over budget even when empty is left unchanged: some
    other member is what does not fit, and emptying this one would only throw away a short value.
    """
    text = short.get(key)
    if not isinstance(text, str) or _wire_size(short) <= RESULT_STATUS_BYTES:
        return
    if not force:
        short[key] = ''
        if _wire_size(short) > RESULT_STATUS_BYTES:
            short[key] = text
            return
    lo, hi, best = 0, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        short[key] = text[:mid]
        if _wire_size(short) <= RESULT_STATUS_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    short[key] = text[:best]


def _fit_items(short, key, *, force=False):
    """Keep the longest prefix of ``short[key]`` that lets ``short`` fit the status budget."""
    items = short.get(key)
    if not isinstance(items, list) or _wire_size(short) <= RESULT_STATUS_BYTES:
        return
    if not force:
        short[key] = []
        if _wire_size(short) > RESULT_STATUS_BYTES:
            short[key] = items
            return
    lo, hi, best = 0, len(items), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        short[key] = items[:mid]
        if _wire_size(short) <= RESULT_STATUS_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    short[key] = items[:best]


def _drop_if_that_fits(short, key):
    """Replace a bulky object with ``None`` when that is what brings ``short`` under the budget."""
    if key not in short or short[key] is None or _wire_size(short) <= RESULT_STATUS_BYTES:
        return
    saved = short[key]
    short[key] = None
    if _wire_size(short) > RESULT_STATUS_BYTES:
        short[key] = saved


def _status_result(result):
    """The route result as ``run.status`` carries it: whole while it fits ``RESULT_STATUS_BYTES``.

    A larger one keeps the members the public record already names. When the result has the status
    branch (``run.view.v1``), those members stay and nothing is added; otherwise only status, code
    and detail stay. Text is shortened first, then the events list, then a limit object is dropped,
    and detail is shortened only once the rest cannot make room. The whole result stays in the run's
    ``result.json`` when that write succeeded. A result kept only in memory is one whose write failed.
    """
    if not isinstance(result, dict) or _wire_size(result) <= RESULT_STATUS_BYTES:
        return result
    keys = _STATUS_RESULT_KEYS if all(key in result for key in _STATUS_RESULT_KEYS) else _LIST_RESULT_KEYS
    short = {key: result[key] for key in keys if key in result}
    # Shorten one member at a time, and only when that member is what does not fit, so a short text
    # survives a large detail and a short detail survives a large text.
    _fit_chars(short, 'text')
    _fit_items(short, 'events')
    _drop_if_that_fits(short, 'limit_notice')
    _drop_if_that_fits(short, 'limit')
    _fit_chars(short, 'detail')
    if _wire_size(short) > RESULT_STATUS_BYTES:
        # More than one member is large, so each non-force cut restored the member
        # it tried. Take the detail from the original first, then the limit when
        # that still fits, then a text head. The event list is shortened last and
        # keeps whatever budget remains. The head is at most an eighth of the
        # status budget: the rest stays an event prefix, not more text.
        saved_detail = result.get('detail') if isinstance(result.get('detail'), str) else ''
        saved_text = result.get('text') if isinstance(result.get('text'), str) else ''
        saved_limit = result.get('limit')
        saved_events = result.get('events') if isinstance(result.get('events'), list) else []
        if 'limit_notice' in short:
            short['limit_notice'] = None
        short['detail'] = ''
        short['text'] = ''
        short['events'] = []
        if 'limit' in short:
            short['limit'] = None
        if saved_detail:
            short['detail'] = saved_detail
            if _wire_size(short) > RESULT_STATUS_BYTES:
                _fit_chars(short, 'detail', force=True)
        if 'limit' in short:
            short['limit'] = saved_limit
            if _wire_size(short) > RESULT_STATUS_BYTES:
                short['limit'] = None
        if saved_text:
            head = RESULT_STATUS_BYTES // 8
            short['text'] = saved_text if len(saved_text) <= head else saved_text[:head]
            if _wire_size(short) > RESULT_STATUS_BYTES:
                _fit_chars(short, 'text', force=True)
        if saved_events:
            short['events'] = saved_events
            if _wire_size(short) > RESULT_STATUS_BYTES:
                _fit_items(short, 'events', force=True)
    return short


def _open_regular(path):
    """``path`` open for reading, or ``OSError`` when it is not a regular file.

    Opened ``O_NONBLOCK``: the open of a FIFO that has no writer waits for one, and a status or list
    call waited with it (E19). ``O_NOFOLLOW`` refuses a symlink, including one whose target is a
    regular file.
    """
    # A host that lacks a flag (the simulated Windows run of the suites) opens without it.
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def _clear_result_temp(tmp):
    """Remove a stale or planted ``result.json.tmp`` so the result can be written beside it.

    A file, a FIFO or a symlink is unlinked. An empty directory is removed too: this name is never
    a directory the daemon created, and ``unlink`` refuses one. A directory that still has entries
    is left in place, and the caller keeps the result in memory. The result is durable only when
    the write that follows this succeeds.
    """
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        return
    except OSError:
        info = os.lstat(tmp)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise
        os.rmdir(tmp)


def _discard_opened_result_temp(tmp):
    """Remove a result temp this write opened, after that write failed.

    A regular file or a symlink is unlinked. A directory is left in place, so a
    planted directory and its entries stay. The result stays in memory either way.
    A failure to remove the temp does not replace that in-memory result.
    """
    try:
        info = os.lstat(tmp)
    except OSError:
        return
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return
    try:
        os.unlink(tmp)
    except OSError:
        return


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


def _json_object(pairs):
    """One JSON object. A key that appears twice is refused, at whatever depth it sits."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise RoutesUnreadable('routes file has a repeated member')
        obj[key] = value
    return obj


def load_routes(path):
    """Read the operator's route file: ``{"routes": {"<engine>": {...}}}``, or {} when there is none.

    Each entry is the route part of a ``recursion.route/v0`` request (model, connection, and the
    engine's own fields) plus ``deadline_s`` and an optional absolute ``binary``. Nothing is
    defaulted: an engine with no entry cannot be started. A repeated key in any object is refused.
    The file must be a regular file of ours that no one else can write.
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
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_json_object)
    except (OSError, ValueError, RecursionError) as exc:
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
        try:
            seconds = float(deadline)
        except OverflowError:
            raise RoutesUnreadable(f'route {engine}: deadline_s') from None
        if not math.isfinite(seconds):  # json.loads reads NaN and Infinity; neither is a deadline
            raise RoutesUnreadable(f'route {engine}: deadline_s')
        binary = entry.get('binary')
        if binary is not None and not (isinstance(binary, str) and os.path.isabs(binary)):
            raise RoutesUnreadable(f'route {engine}: binary must be an absolute path')
        route = {k: v for k, v in entry.items() if k not in ('deadline_s', 'binary')}
        route['engine'] = engine
        out[engine] = {'route': route, 'deadline_s': seconds, 'binary': binary}
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

    The same holds for the routes the entry would switch to. With ``limits.on_limit: fallback`` the
    route service launches the next ``limits.fallback`` route once the primary connection is at a
    limit, so an entry whose fallback list names such a route is refused whole, naming the fallback
    by its place in the list. A fallback list that is only shown to the user (``ask``, ``wait``) is
    never launched from and does not refuse the entry.
    """
    route = entry.get('route') if isinstance(entry, dict) else None
    if not isinstance(route, dict):
        return None
    reason = _no_custody(route)
    if reason is not None:
        return f'no credential custody: {reason}'
    limits = route.get('limits')
    if isinstance(limits, dict) and limits.get('on_limit') == 'fallback' and isinstance(limits.get('fallback'), list):
        for place, other in enumerate(limits['fallback'], 1):
            reason = _no_custody(other)
            if reason is not None:
                return f'no credential custody: fallback {place}: {reason}'
    return None


def _no_custody(route):
    """What kind of custody-less route ``route`` is, or None."""
    connection = route.get('connection') if isinstance(route, dict) else None
    if not isinstance(connection, dict):
        return None
    if connection.get('kind') == 'passthrough':
        return 'a passthrough route is not launched'
    if connection.get('key_source') == 'api_key_helper':
        return 'a key_source api_key_helper route is not launched'
    return None


def route_unbindable(entry, breaker):
    """Why no run on a loaded route entry could be bound in the breaker store: ``(code, detail)``, or None.

    A run is bound in the breaker store to its connection and its model before its engine starts,
    and a binding the store refuses stops the launch. Two things about a route make every binding
    fail. One is a connection id the store cannot key (``breaker_connection_unrepresentable``); a
    limit on such a connection could not be kept across a restart either. The other is a model the
    store cannot record (``breaker_model_unrepresentable``): one longer than 128 characters, or one
    with a space, a control character or a non-ASCII character in it. The start is refused for either
    before the route service is asked, and ``route.list`` names the same refusal as the route's
    blocker. Both read this one function, so they cannot differ. A model that is missing, empty or
    not text is not refused here: the route validator answers ``request_invalid`` for it, as before.

    As in ``route_unsupported``, the routes of a ``limits.fallback`` list count only when the daemon
    would launch from the list (``on_limit: fallback``). A list that is only shown to the user
    (``ask``, ``wait``) never refuses the entry. Every connection id that counts is looked at before
    any model, and a model in the list is named by its place in it.
    """
    route = entry.get('route') if isinstance(entry, dict) else None
    if breaker is None or not isinstance(route, dict):
        return None
    limits = route.get('limits')
    fallbacks = limits.get('fallback', ()) if isinstance(limits, dict) and limits.get('on_limit') == 'fallback' and isinstance(limits.get('fallback'), list) else ()
    for candidate in (route, *fallbacks):
        if isinstance(candidate, dict):
            conn = route_connection({'route': candidate})
            if conn is not None and not breaker.representable(conn):
                # A limit on this connection could never be kept across a restart: refuse, do not run unfenced.
                return 'breaker_connection_unrepresentable', 'connection id'
    model = route.get('model')
    if isinstance(model, str) and model and not breaker.model_representable(model):
        return 'breaker_model_unrepresentable', 'model'
    for place, other in enumerate(fallbacks, 1):
        model = other.get('model') if isinstance(other, dict) else None
        if isinstance(model, str) and model and not breaker.model_representable(model):
            return 'breaker_model_unrepresentable', f'fallback {place}: model'
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
        unbindable = route_unbindable(entry, self._breaker)
        if unbindable is not None:
            # No run on this route could be bound in the breaker store: refuse before the route service is asked.
            raise self._refuse(run_id, *unbindable)
        request = {'contract': route_contract.CONTRACT_VERSION, 'request_id': run_id,
                   'tenant': params['tenant'], 'prompt': params['prompt'],
                   'deadline_s': entry['deadline_s'], 'route': json.loads(json.dumps(entry['route']))}
        ctx = RunContext(run_id, breaker=self._breaker)
        with self._lock:
            in_flight = run_id in self._active
            if not in_flight:
                self._active[run_id] = ctx
                self.failures.pop(run_id, None)
        if in_flight:
            # Refused once the lock is released: ``_refuse`` takes it to record the refusal, and it is
            # not reentrant. Refusing inside the block left this thread waiting on itself, and every
            # later start, every worker's cleanup and ``active()`` waiting behind it.
            raise self._refuse(run_id, 'start_in_flight', '')
        try:
            threading.Thread(target=self._drive, args=(ctx, request), name=f'recursion-run-{run_id}',
                             daemon=True).start()
        except BaseException as exc:
            # No worker will ever run: nothing else would take the run out of ``_active``, and every retry
            # would answer ``start_in_flight`` until the daemon restarted (E04). The refusal is recorded
            # in the same lock that gives the run up. Recording it afterwards let a second start take the
            # id and then wear this attempt's error.
            with self._lock:
                self._active.pop(run_id, None)
                if isinstance(exc, Exception):
                    self.failures[run_id] = {'code': 'worker_start_failed', 'detail': type(exc).__name__}
            if isinstance(exc, Exception):
                raise StartRefused('worker_start_failed', type(exc).__name__) from None
            raise
        if ctx.wait_started(self._start_timeout_s):
            return {'cgroup': ctx.cgroup}
        result = ctx.result
        if result is None:
            if not ctx.abandon():
                return {'cgroup': ctx.cgroup}  # published as the wait ran out: the run started
            raise self._refuse(run_id, 'launch_timeout', '')
        raise self._refuse(run_id, str(result.get('code') or 'engine_exited'), str(result.get('detail') or ''))

    def _refuse(self, run_id, code, detail):
        with self._lock:
            self.failures[run_id] = {'code': code, 'detail': detail}
        return StartRefused(code, detail)

    def _drive(self, ctx, request):
        interrupt = None
        result = None
        try:
            try:
                with bound_run(ctx):
                    try:
                        result = self._service.handle(request)
                    except Exception as exc:  # noqa: BLE001 - a route bug must not leave the start waiting
                        log.error('route request for %s failed (%s)', ctx.run_id, type(exc).__name__)
                        result = route_contract.result(ctx.run_id, route_contract.ENGINE_ERROR,
                                                       detail=f'route failure: {type(exc).__name__}')
                    except BaseException as exc:  # an interrupt or SystemExit from the callback (E34)
                        # The published child must not outlive the worker that drove it: end it and release the
                        # start below as for any route failure, then let the interrupt go on.
                        log.error('route request for %s was ended by %s', ctx.run_id, type(exc).__name__)
                        interrupt = exc
                        result = route_contract.result(ctx.run_id, route_contract.ENGINE_ERROR,
                                                       detail=f'route failure: {type(exc).__name__}')
                self._after_route(ctx, result, interrupt)
            except BaseException as exc:
                # An interrupt in the result write, in finished, or in the settle is outside the callback
                # handler. The published child was left running, with both descriptors open. Do the same
                # release one step further out, on a best-effort basis, then let the interrupt go on.
                if interrupt is None:
                    log.error('route request for %s was ended by %s', ctx.run_id, type(exc).__name__)
                    interrupt = exc
                if result is None:
                    result = route_contract.result(ctx.run_id, route_contract.ENGINE_ERROR,
                                                   detail=f'route failure: {type(exc).__name__}')
                self._after_interrupt(ctx, result)
        finally:
            with self._lock:
                self._active.pop(ctx.run_id, None)
        if interrupt is not None:
            raise interrupt

    def _after_route(self, ctx, result, interrupt):
        closing = getattr(self._daemon, 'closed', False)  # shutdown: the run is left to the next daemon
        if ctx.process is not None and not closing:
            self._record_result(ctx.run_id, result)
            if self._breaker is not None and result.get('code') is not None:
                self._breaker.fault_run(ctx.run_id)  # an effect citing this run is refused (4.2)
        ctx.finished(result)
        if ctx.process is None:
            return
        if closing:
            ctx.process.close()
        elif interrupt is not None:
            self._settle(ctx, grace_s=0.0, reason='route_interrupted')
        else:
            self._settle(ctx)

    def _after_interrupt(self, ctx, result):
        """Record, release and fence after an interrupt that landed past the route callback."""
        closing = getattr(self._daemon, 'closed', False)
        if ctx.process is not None and not closing:
            try:
                self._record_result(ctx.run_id, result)
            except BaseException:
                log.error('route result for %s was not written after an interrupt', ctx.run_id)
                with self._lock:
                    self.results.setdefault(ctx.run_id, result)
        if ctx.result is None:
            try:
                ctx.finished(result)
            except BaseException:
                log.error('route result for %s was not published after an interrupt', ctx.run_id)
        process = ctx.process
        if process is None:
            return
        try:
            # `fence` records its reason before `stop` returns, so a non-empty `fenced` is not
            # proof the child was stopped. An interrupt inside `stop` leaves that list set and
            # the child running; only a fence call that returned is done.
            if closing or getattr(process, '_stop_returned', False):
                process.close()
            else:
                self._settle(ctx, grace_s=0.0, reason='route_interrupted')
        except BaseException:
            try:
                process.close()
            except BaseException:
                log.error('handles for %s stayed open after an interrupt', ctx.run_id)

    def _record_result(self, run_id, result):
        tmp = None
        opened = False
        try:
            path = self._state / 'runs' / run_id / 'result.json'
            tmp = path.with_name('result.json.tmp')
            # A stale or planted file is replaced: opening a FIFO here would wait for a reader.
            # An empty planted directory is removed too. The result is durable only when this write
            # succeeds; otherwise it is kept in memory. A temp this write opened is removed on that
            # failure when it is a regular file or a symlink. A directory at that name is left.
            _clear_result_temp(tmp)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            opened = True
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as exc:
            log.error('route result for %s was not written (%s)', run_id, type(exc).__name__)
            if opened:
                _discard_opened_result_temp(tmp)
            with self._lock:
                self.results[run_id] = result

    def _settle(self, ctx, *, grace_s=None, reason='result_recorded'):
        """After the route result: give the engine a moment to exit, then end what is left of its tree."""
        process = ctx.process
        try:
            deadline = time.monotonic() + (self._exit_grace_s if grace_s is None else grace_s)
            while time.monotonic() < deadline:
                if getattr(self._daemon, 'closed', False):
                    return  # the daemon is shutting down; it never stops a run on its way out
                try:
                    if self._daemon.reconcile(ctx.run_id).state in TERMINAL:
                        return
                except Exception as exc:  # noqa: BLE001
                    log.error('observing %s after its result failed (%s)', ctx.run_id, type(exc).__name__)
                    break
                time.sleep(0.2)
            process.fence(reason)
            # Set only after `fence` returns. `stop` raising BaseException skips this, and the
            # recovery above asks for the stop again.
            process._stop_returned = True
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
        # The uid of the process that built this door. A peer with that uid is the owner.
        self._owner_uid = os.geteuid() if hasattr(os, 'geteuid') else None
        self._hooks = _scope_hooks()

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
                unbindable = route_unbindable(entry, self._breaker)
                if unbindable is not None:  # every start on this route is refused before anything is launched
                    record['blocker'] = f'{unbindable[0]}: {unbindable[1]}'
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
        except Exception as exc:  # noqa: BLE001 - an unreadable registry is reported, never guessed around
            log.error('daemon registry read for %s failed (%s)', run_id, type(exc).__name__)
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
            # Counted surrogates still sit in the answer. The frame encode refuses them, and a
            # non-finite float, and the socket worker does not turn that into an error reply.
            result = _frame_scalar(result)
            run['result'] = _status_result(result) if full else {k: result.get(k) for k in ('status', 'code', 'detail')}
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
            with _open_regular(self._state / 'runs' / run_id / 'result.json') as handle:
                data = json.loads(handle.read().decode('utf-8'))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _output(self, run_id, live):
        """The tail of the engine's stdout file: whole lines only, newest last. A read of a file."""
        if not RUN_ID.fullmatch(run_id):
            return None
        try:
            with _open_regular(self._state / 'runs' / run_id / 'stdout.jsonl') as handle:
                size = os.fstat(handle.fileno()).st_size
                start = max(0, size - OUTPUT_TAIL_BYTES)
                # A nonzero start is mid-line unless the byte just before the window is a newline.
                aligned = not start
                if start:
                    handle.seek(start - 1)
                    aligned = handle.read(1) == b'\n'
                handle.seek(start)
                data = handle.read(OUTPUT_TAIL_BYTES)
        except FileNotFoundError:
            return None  # the run never reached a launch
        except OSError as exc:
            return {'unavailable': type(exc).__name__}
        if start and not aligned:  # mid-line: drop the partial first line; a line start is kept
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
        """``{"op": "control", "method", "params"}`` -> ``{"ok": true, "result": <control answer>}``.

        The principal is taken from the peer credentials, never from the request. The authorizer
        is asked for every method, and a refusal is returned before the method runs.
        """
        if message.get('op') != 'control':
            return {'ok': False, 'error': {'code': 'unknown_op', 'op': message.get('op')}}
        method, params = message.get('method'), message.get('params')
        if not isinstance(method, str) or not isinstance(params, (dict, type(None))):
            return {'ok': False, 'error': {'code': 'request_invalid'}}
        principal, refused = self._principal_from_peer(conn)
        if refused is not None:
            return refused
        verdict = self._hooks.authorize(principal, method, {} if params is None else params)
        if verdict is not None:
            return {'ok': False, 'error': {'code': verdict['code'], 'detail': verdict.get('detail', '')}}
        # The transport echoes the request's "id" in the reply frame; a run.list page leaves room for it.
        reserved = _wire_size(message['id']) + len(',"id":') if 'id' in message else 0
        return {'ok': True, 'result': self._answer(method, params, reserved)}

    def _principal_from_peer(self, conn):
        """``(principal, None)`` or ``(None, refusal)``. A bad read is never the owner."""
        try:
            peer = conn.peer
            uid, pid = peer.uid, peer.pid
        except Exception:  # noqa: BLE001 - unreadable credentials refuse the call
            return None, _scope_refused('peer_credentials_unreadable')
        if type(uid) is not int:
            return None, _scope_refused('peer_credentials_unreadable')
        if uid != self._owner_uid:
            # Not the owner. A positive pid names the process; with no device credential that
            # principal holds nothing. No pid means there is no principal to authorize.
            if type(pid) is int and pid > 0:
                return Principal('device', None, in_run_unit=peer_is_engine(pid)), None
            return None, _scope_refused('unknown_principal')
        return Principal('owner', 'local', in_run_unit=peer_is_engine(pid)), None

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
