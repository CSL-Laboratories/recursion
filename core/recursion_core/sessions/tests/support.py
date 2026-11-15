# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""What the lane's tests share: the fakes of its ports, ids and a clock that are the same on every run, and the
way a test composes the lane on SPINE's own ``Hooks()`` with the one shared fake of the event store
(``FakeStore``, ``control/tests/test_seams.py``).

A RESTART is a new ``Sessions`` over the same ``FakeStore`` (whose file outlives the old object) followed by
``recover()``. A CRASH is ``Cut``, a ``BaseException`` that nothing in the lane catches: raised inside a
transaction it rolls that transaction back, as the process ending there would leave it uncommitted; raised
between two transactions it leaves what was committed.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import threading

from recursion_core.control import hooks as H
from recursion_core.control.tests.test_seams import Cut, FakeStore
from recursion_core.sessions import hooks as session_hooks
from recursion_core.sessions import tables
from recursion_core.sessions.ports import Ports, UnitRefused
from recursion_core.sessions.service import Sessions

REPO = pathlib.Path(__file__).resolve().parents[4]
SCHEMAS = REPO / 'schemas' / 'control' / 'v1'
FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'
BOUND_S = 30.0     # only ends a wait that would otherwise never end; it never decides a pass

__all__ = ('Cut', 'FakeStore', 'UnitRefused')


def flags(**on) -> dict:
    names = ('multi_turn', 'mid_turn_input', 'interrupt', 'resume', 'fork', 'permission_bridge', 'typed_limits',
             'usage')
    return {name: bool(on.get(name, False)) for name in names}


def declaration(engine: str, *, input_ack: bool = False, **on) -> dict:
    """A driver.v2 declaration (#368)."""
    return {'engine': engine, 'driver': 2, 'flags': flags(**on), 'input_ack': input_ack}


# The drivers the tests use. 'one_turn' is today's path (every flag false); 'worker' keeps a worker that takes
# another message and input mid-turn; 'resumer' runs one turn per run and resumes the engine's session.
DECLARATIONS = {
    'one_turn': declaration('claude_code'),
    'worker': declaration('codex', input_ack=True, multi_turn=True, mid_turn_input=True, resume=True),
    'resumer': declaration('opencode', resume=True),
}
ENGINE_OF = {'one_turn': 'claude_code', 'worker': 'codex', 'resumer': 'opencode'}


class FakeDrivers:
    def __init__(self, by_engine: dict | None = None) -> None:
        self.by_engine = {ENGINE_OF[name]: found for name, found in DECLARATIONS.items()} if by_engine is None \
            else dict(by_engine)

    def declaration(self, engine):
        return self.by_engine.get(engine)


class FakeRoutes:
    """Routing is lane ROUTING's; this one takes the hand pick or the engine as given, on one made-up login."""

    def __init__(self) -> None:
        self.refusal = None

    def resolve(self, principal, params):
        if self.refusal is not None:
            return self.refusal
        pick = params.get('hand_pick')
        engine = pick['engine'] if isinstance(pick, dict) else params.get('engine')
        if not isinstance(engine, str):
            return ('request_invalid', 'engine')
        model = pick.get('model') if isinstance(pick, dict) else None
        return {'engine': engine, 'model': model, 'connection': {'kind': 'subscription', 'id': 'made-up-login'}}


class FakeUnits:
    """Records every call. ``then`` is a list of what the next calls do, in order: 'ok', a ``UnitRefused``, an
    ``Exception`` (an outcome nobody can vouch for), ``'cut-before'`` (the process ends before the write) or
    ``'cut-after'`` (the write happened and the process ends before anything records it). Stops answer from
    ``stop_answers`` in order; with none left a Stop answers 'stopped'."""

    def __init__(self) -> None:
        self.launched: list = []
        self.sent: list = []
        self.stops: list = []
        self.then: list = []
        self.stop_answers: list = []
        self.registered: set = set()     # the run ids a launch actually started
        self.lock = threading.Lock()

    def _next(self):
        with self.lock:
            return self.then.pop(0) if self.then else 'ok'

    def _do(self, record: list, order) -> None:
        what = self._next()
        if what == 'cut-before':
            raise Cut()
        if isinstance(what, (UnitRefused,)):
            raise what
        if isinstance(what, Exception):
            raise what
        record.append(order)
        if what == 'cut-after':
            raise Cut()

    def launch(self, order) -> None:
        self._do(self.launched, order)
        self.registered.add(order.run_id)

    def send(self, order) -> None:
        self._do(self.sent, order)

    def stop(self, run_id):
        self.stops.append(run_id)
        if self.stop_answers:
            answer = self.stop_answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if run_id not in self.registered:
            return None
        return stop_answer(run_id, 'stopped')


def stop_answer(run_id: str, code: str) -> dict:
    """A stop.result.v1 answer as today's daemon gives it (#368)."""
    if code == 'stopped':
        return {'code': 'stopped', 'run_id': run_id, 'state': 'stopped', 'repeat': False}
    if code == 'pending':
        return {'code': 'pending', 'run_id': run_id, 'state': 'pending', 'reason': 'cgroup_populated'}
    return {'code': 'ended', 'run_id': run_id, 'state': 'ended', 'claim': 'ended_before_stop_removal_unverified'}


class Minter:
    """Ids that are the same on every run: the prefix and a counter as 32 hex digits."""

    def __init__(self, start: int = 0) -> None:
        self.count = start
        self.lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self.lock:
            self.count += 1
            return f'{prefix}{self.count:032x}'


class Clock:
    """A clock that moves one second per reading, from a fixed moment."""

    def __init__(self) -> None:
        self.second = 0
        self.lock = threading.Lock()

    def __call__(self) -> str:
        with self.lock:
            self.second += 1
            minute, second = divmod(self.second, 60)
            return f'2026-11-14T09:{minute % 60:02d}:{second:02d}.000Z'


class Lane:
    """The lane composed for one test: a FakeStore with the lane's tables, the fakes, a ``Sessions``, and a
    ``Hooks()`` with the lane installed on it (``hooks.install``), frozen, with the store opened through the
    ``event_store`` hook as composition opens it."""

    def __init__(self, store: FakeStore | None = None, *, units: FakeUnits | None = None, minter=None,
                 clock=None, declare: bool = True) -> None:
        self.store = store if store is not None else FakeStore()
        if declare:
            tables.declare(self.store)
        self.units = units if units is not None else FakeUnits()
        self.drivers, self.routes = FakeDrivers(), FakeRoutes()
        self.minter, self.clock = minter or Minter(), clock or Clock()
        self.sessions = Sessions(self.store, Ports(self.drivers, self.routes, self.units),
                                 mint=self.minter, now=self.clock)
        self.hooks = H.Hooks()
        self.service = session_hooks.install(self.hooks, self.sessions)
        self.hooks.set('event_store', lambda state_dir: self.store)
        self.hooks.freeze()
        self.hooks.open_store(None)

    def restarted(self) -> 'Lane':
        """The same store after the process ended: a new lane over it (tables are kept), then recovery."""
        lane = Lane(self.store, units=self.units, minter=self.minter, clock=self.clock, declare=False)
        lane.sessions.recover()
        return lane

    def call(self, method: str, params: dict, principal=H.OWNER) -> dict:
        def nested(inner, inner_params=None):
            raise AssertionError('no nested call is expected')
        return self.hooks.methods.dispatch(method, params, principal, nested)

    def settle(self) -> None:
        """One pass of the service: the queue, then the work, until nothing more can be started."""
        for _ in range(20):
            if not (self.sessions.dispatch_queued() + self.sessions.run_pending()):
                return
        raise AssertionError('the service did not settle in 20 passes')

    def start(self, engine='claude_code', prompt=None, command_id='cmd-start-0001', tenant='t1') -> dict:
        params = {'command_id': command_id, 'tenant': tenant, 'engine': engine}
        if prompt is not None:
            params['prompt'] = prompt
        return self.call('session.start', params)

    def status(self, session_id: str) -> dict:
        return self.call('session.status', {'session_id': session_id})

    def accept_input(self, session_id: str) -> str:
        """The engine takes the input of the session's turn in progress: its acceptance, as the driver would draft
        it from the engine's typed signal, through the service."""
        with self.store.transaction() as tx:
            from recursion_core.sessions import store as S
            open_rows = S.deliveries(tx, state='delivering', session_id=session_id)
        assert len(open_rows) == 1, open_rows
        row = open_rows[0]
        return self.sessions.accepted({'delivery_id': row['delivery_id'], 'session_id': session_id,
                                       'run_id': row['run_id'], 'turn_id': row['target_turn_id'], 'how': row['how']})


def load_json(path) -> object:
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def wire_client():
    """SPINE's conformance client (#368, merged into this branch), or None on an interpreter without jsonschema."""
    if importlib.util.find_spec('jsonschema') is None:
        return None
    path = REPO / 'conformance' / 'wire_client.py'
    spec = importlib.util.spec_from_file_location('sessions_wire_client', path)
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    return client
