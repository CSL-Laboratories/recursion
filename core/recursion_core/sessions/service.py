# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The lane's work intents, the service that carries them out, and recovery after a restart.

WHY THERE ARE WORK INTENTS. A command's transaction may touch only the store: no registry, daemon or engine
call happens inside it (the lanes' preamble, "Shared state"). So a command that needs a unit launched or an
input written commits, in the same transaction as its receipt, the turn it opened, the BINDING of the
delivery (``delivering``: the commit is made before anything is written, queue.item.v1) and a typed WORK
INTENT with a stable operation id. Its answer describes what was committed (a turn queued), never an effect
that has not happened. This service then carries the intent out, outside every transaction.

ONE INTENT, ITS STATES (``sessions_work``):

    pending --claim--> attempting --the port returned--> done
                                  --UnitRefused--------> failed        (definitely nothing was written)
                                  --anything else------> outcome_unknown (never retried by itself)

The claim is committed BEFORE the outside call, so exactly one caller makes it, and a crash after the claim
is seen at the restart as an attempt nobody can vouch for. A Stop is the one intent that is safe to repeat (an
emergency Stop never needs fresh state): an attempted Stop is asked again, never marked unknown.

THE FENCE BETWEEN A LAUNCH AND A STOP. Two passes may run at once (nothing here relies on one runner), so:
- a launch or a send that is still ``pending`` when its session is ``stop_pending`` is never attempted: it is
  ``failed`` (``stop_requested``) at its claim, and the Stop settles the turn as one that never started;
- a Stop settles a session only on a well-formed answer that confirms it (``records.stop_confirmed``). An
  answer of None (no run of that id is registered) is taken as "nothing of that run runs" only when, at the
  moment the Stop was CLAIMED (before it looked), no launch of that run was pending or attempting, and none had
  returned (``done``). A launch still open may register its run after the Stop looked, even if it ends before
  the Stop's answer is read. A launch that returned handed its input over, so its run exists: the port may
  register it after the call returned (``ports.py``), and once registered it is answered (``stopped``,
  ``ended`` or ``pending``), never None again. So for such a run None means "not registered YET". Only a
  launch that definitely never started (``failed``) or whose outcome nobody can vouch for
  (``outcome_unknown``) leaves None meaning "nothing runs". Until then the Stop is asked again and the session
  stays ``stop_pending``, so no replacement turn can open beside a unit that may still appear.

THE RESTART (``recover``), in one transaction:
1. every delivery still ``delivering`` becomes ``outcome_unknown``, with its event (queue.item.v1);
2. every launch or send not done becomes ``outcome_unknown``: its delivery was quarantined, so it is never
   written, even when the crash came before the write;
3. every session that has, or may have, a unit (a worker, live or waiting) is given a recovery Stop and goes
   ``stop_pending``: nothing here can reattach to a unit that outlived the daemon, so no new unit starts for
   that session until the old one is confirmed stopped. A session already ``stop_pending`` keeps waiting, and
   its Stop is asked again. Pending never returns a session to idle: only ``stopped`` or ``ended`` does.

QUEUED MESSAGES (``dispatch_queued``). A session's items go in their order, one delivery in flight at a time.
An idle session's first queued item opens the next turn (``new_turn``). On a live session the first item, if
it is a steer, goes into the turn in progress (``mid_turn``) when the driver declares ``mid_turn_input``, the
worker is alive and the turn is running; otherwise it waits for the next turn (the method table's fallback for
``session.steer``: ``next_turn``). Steering is offered only where the driver declares ``input_ack``: whether it
is offered on an engine that gives no typed acceptance is Lucas's open question (questions.md Q7), so there a
steer is refused at admission and a steer already queued (or edited into one) is HELD, neither sent mid-turn
nor turned into a next turn, until he answers.

A NEW TURN AFTER A FIRST LAUNCH THAT NEVER STARTED. A turn continues the conversation: a live worker that takes
another message, or a launch that resumes the engine's session. A session whose every turn so far was a launch
that definitely never started (refused by the launcher, or never attempted) has no conversation to continue
or to lose, so its next turn launches afresh. One whose conversation may have begun and cannot be continued is
refused ``unsupported`` (questions.md Q6, Lucas's).

``Sessions`` holds what the handlers and the service share; ``SessionsService`` is the S13 service object.
Nothing in this module is registered anywhere (``hooks.py``).
"""
from __future__ import annotations

import logging
import threading

from recursion_core.sessions import delivery, events, machine, records, store
from recursion_core.sessions.ports import LaunchOrder, Ports, SendOrder, UnitRefused

log = logging.getLogger(__name__)

TURN_KINDS = ('launch', 'send')


def incarnation_of(run_id: str) -> str:
    """The worker incarnation this lane binds an input to: a run is launched once (a second launch of a run id is
    refused), so the worker this lane launched under ``run_id`` has one incarnation from its side."""
    return f'{run_id}.1'


class Sessions:
    """What the method handlers and the service share: the event store's helper, the ports, the id and time
    sources, and the event that wakes the service after a commit."""

    def __init__(self, store_helper, ports: Ports, *, mint=records.mint, now=records.timestamp) -> None:
        self.store = store_helper
        self.ports = ports
        self.mint = mint
        self.now = now
        self.wake = threading.Event()

    # ------------------------------------------------------------- planning, inside the caller's transaction
    def declaration(self, engine: str):
        found = self.ports.drivers.declaration(engine)
        if not isinstance(found, dict) or not isinstance(found.get('flags'), dict):
            return None
        return found

    def open_turn(self, tx, session: dict, text: str, *, item: dict | None = None) -> tuple:
        """Open the next turn of an idle session for ``text`` and commit its dispatch, all through ``tx``: the turn
        (queued, its run bound), the binding (``delivering``), and the work intent. ``item`` is the queued message
        the turn carries, or None for a turn's own input. Returns ``(session after, turn id)``.

        Raises ``machine.Refused``: ``busy`` or ``conflict`` (the session is not idle), ``unavailable`` (no driver
        serves the engine), ``unsupported`` (the turn could not continue the conversation: neither a live worker
        that takes another message nor a resumable engine session; checked before anything is written)."""
        refusal = machine.turn_refusal(session)
        if refusal is not None:
            raise machine.Refused(*refusal)
        found = self.declaration(session['route']['engine'])
        if found is None:
            raise machine.Refused('unavailable', 'driver_absent')
        flags, worker = found['flags'], session['worker']
        resume = None
        if worker is not None and worker['alive']:
            if not flags.get('multi_turn'):
                raise machine.Refused('busy', 'worker_alive')     # never a second unit beside a live one
            kind, run_id, alive = 'send', worker['run_id'], True
        elif self._never_started(tx, session):
            kind, run_id, alive = 'launch', self.mint('run-'), False
        elif flags.get('resume') and session['engine_session_id'] is not None:
            kind, run_id, alive, resume = 'launch', self.mint('run-'), False, session['engine_session_id']
        else:
            raise machine.Refused('unsupported', 'not_implemented')
        turn_id, delivery_id = self.mint('turn-'), self.mint('dlv-')
        after = machine.open_turn(session, turn_id=turn_id, run_id=run_id, worker_alive=alive)
        store.save(tx, session, after)
        binding = {'delivery_id': delivery_id, 'session_id': session['session_id'], 'run_id': run_id,
                   'worker_incarnation': incarnation_of(run_id), 'target_turn_id': turn_id, 'how': 'new_turn'}
        if item is None:
            delivery.begin_input(tx, binding)
        else:
            delivery.begin(tx, item['item_id'], {**binding, 'item_id': item['item_id']})
        payload = {**binding, 'item_id': None if item is None else item['item_id'], 'text': text}
        if kind == 'launch':
            payload['resume'] = resume
        store.insert_work(tx, self.mint('op-'), session['session_id'], kind, payload)
        return after, turn_id

    def _never_started(self, tx, session: dict) -> bool:
        """True when no turn of ``session`` can have reached an engine: no engine reported a conversation id, and
        every turn so far was a launch that is ``failed`` (refused by the launcher, so nothing started, or never
        attempted). A session with no turn is the first case of it."""
        if session['engine_session_id'] is not None:
            return False
        never = {operation['payload']['target_turn_id']
                 for operation in store.work(tx, session_id=session['session_id'])
                 if operation['kind'] == 'launch' and operation['state'] == 'failed'}
        return all(turn['turn_id'] in never for turn in session['turns'])

    def steering_refusal(self, session: dict):
        """Why a steer cannot be offered on this session's engine, as an answer's ``(code, detail)``, or None: no
        driver serves the engine (``unavailable``), or it declares no typed acceptance of an input (``input_ack``
        false: Lucas's open question Q7, so steering stays unopened there; the table has no detail word for it
        yet, so the answer is ``unsupported``/``not_implemented``)."""
        found = self.declaration(session['route']['engine'])
        if found is None:
            return 'unavailable', 'driver_absent'
        if found.get('input_ack') is not True:
            return 'unsupported', 'not_implemented'
        return None

    def _steer_now(self, tx, session: dict, item: dict) -> bool:
        """Put a queued steer into the turn in progress, if the driver can take it there now."""
        found = self.declaration(session['route']['engine'])
        worker, active = session['worker'], session['active_turn_id']
        if found is None or not found['flags'].get('mid_turn_input') or worker is None or not worker['alive']:
            return False
        turn = next((turn for turn in session['turns'] if turn['turn_id'] == active), None)
        if turn is None or turn['state'] != 'running' or turn['run_id'] != worker['run_id']:
            return False
        binding = {'delivery_id': self.mint('dlv-'), 'item_id': item['item_id'], 'session_id': session['session_id'],
                   'run_id': worker['run_id'], 'worker_incarnation': incarnation_of(worker['run_id']),
                   'target_turn_id': active, 'how': 'mid_turn'}
        delivery.begin(tx, item['item_id'], binding)
        store.insert_work(tx, self.mint('op-'), session['session_id'], 'send', {**binding, 'text': item['text']})
        return True

    def _ask_stop(self, tx, session: dict) -> dict:
        """The session goes ``stop_pending`` and a Stop of its unit is committed as work."""
        after = machine.request_stop(session)
        store.save(tx, session, after)
        store.insert_work(tx, self.mint('op-'), session['session_id'], 'stop', {'run_id': session['worker']['run_id']})
        return after

    # ------------------------------------------------------------------- the service's own transactions
    def request_stop(self, session_id: str) -> dict:
        """Ask a Stop of a session's unit (for a later verb; and for the tests). Returns the session after."""
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None:
                raise machine.Refused('session_unknown')
            after = self._ask_stop(tx, session)
        self.wake.set()
        return after

    def recover(self) -> None:
        """The restart (see the module's text), in one transaction."""
        with self.store.transaction() as tx:
            delivery.quarantine_all(tx)
            for operation in store.work(tx, state='pending') + store.work(tx, state='attempting'):
                if operation['kind'] in TURN_KINDS:
                    store.move_work(tx, operation['operation_id'], operation['state'], 'outcome_unknown',
                                    {'why': 'daemon_restart'})
                elif operation['state'] == 'attempting':
                    store.move_work(tx, operation['operation_id'], 'attempting', 'pending', {'why': 'daemon_restart'})
            stops = {(operation['session_id'], operation['payload']['run_id'])
                     for operation in store.work(tx, state='pending') if operation['kind'] == 'stop'}
            for session_id in store.with_unit(tx):
                session = store.load(tx, session_id)
                if session['state'] == 'stop_pending':
                    if (session_id, session['worker']['run_id']) not in stops:
                        store.insert_work(tx, self.mint('op-'), session_id, 'stop',
                                          {'run_id': session['worker']['run_id']})
                elif session['state'] != 'ended':
                    self._ask_stop(tx, session)

    def dispatch_queued(self) -> int:
        """Start the delivery of each session's first queued message where it can go now. Returns how many.

        Each session is looked at in its own transaction. One that raises (a port, such as a driver's declaration,
        failing) is logged and changes nothing: its transaction is rolled back, its item stays queued, and the
        pass goes on with the next session; a later pass tries it again."""
        with self.store.transaction() as tx:
            waiting = store.with_queued_items(tx)
        started = 0
        for session_id in waiting:
            try:
                started += self._dispatch_one(session_id)
            except Exception:  # noqa: BLE001 - this session keeps its queue; the others still go
                log.exception('the dispatch of session %s failed; its queue is kept for a later pass', session_id)
        return started

    def _dispatch_one(self, session_id: str) -> int:
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None or store.deliveries(tx, state='delivering', session_id=session_id):
                return 0
            queued = store.items(tx, session_id, ('queued',))
            if not queued:
                return 0
            first = queued[0]
            if first['delivery'] == 'steer' and self.steering_refusal(session) is not None:
                log.info('session %s holds its steer %s: steering is not offered on its engine', session_id,
                         first['item_id'])
                return 0
            if session['state'] == 'idle':
                try:
                    self.open_turn(tx, session, first['text'], item=first)
                except machine.Refused as refusal:
                    log.info('session %s keeps its queue: %s', session_id, refusal)
                    return 0
                return 1
            if session['state'] == 'live' and first['delivery'] == 'steer':
                return int(self._steer_now(tx, session, first))
            return 0

    def run_pending(self) -> int:
        """Carry out every work intent that was pending when the pass began, each once. Returns how many it tried."""
        with self.store.transaction() as tx:
            pending = [operation['operation_id'] for operation in store.work(tx, state='pending')]
        tried = 0
        for operation_id in pending:
            tried += self._carry_out(operation_id)
        return tried

    def _carry_out(self, operation_id: str) -> int:
        with self.store.transaction() as tx:
            operation = store.load_work(tx, operation_id)
            if operation is None or operation['state'] != 'pending':
                return 0
            if operation['kind'] in TURN_KINDS:
                row = store.load_delivery(tx, operation['payload']['delivery_id'])
                if row is None or row['state'] != 'delivering' or row['written']:
                    store.move_work(tx, operation_id, 'pending', 'failed', {'code': 'not_delivering'})
                    return 0
                if store.load(tx, operation['session_id'])['state'] == 'stop_pending':
                    store.move_work(tx, operation_id, 'pending', 'failed', {'code': 'stop_requested'})
                    return 0
            if not store.move_work(tx, operation_id, 'pending', 'attempting'):
                return 0
            if operation['kind'] == 'stop':
                # Read in the claim, before the Stop looks: a launch of the run still open now may register it
                # after the Stop looked, whenever that launch ends; one that returned may register it late.
                operation['may_register'] = self._launch_may_register(tx, operation['payload']['run_id'])
        if operation['kind'] == 'stop':
            self._stop(operation)
        else:
            self._write(operation)
        return 1

    def _write(self, operation: dict) -> None:
        payload, kind = operation['payload'], operation['kind']
        try:
            if kind == 'launch':
                with self.store.transaction() as tx:
                    session = store.load(tx, operation['session_id'])
                self.ports.units.launch(LaunchOrder(
                    session_id=operation['session_id'], turn_id=payload['target_turn_id'], run_id=payload['run_id'],
                    worker_incarnation=payload['worker_incarnation'], delivery_id=payload['delivery_id'],
                    text=payload['text'], route=session['route'], workspace=session['workspace'],
                    resume=payload['resume'], tenant=session['tenant'], principal=session['principal'],
                    budget=session['budget']))
            else:
                self.ports.units.send(SendOrder(
                    session_id=operation['session_id'], turn_id=payload['target_turn_id'], run_id=payload['run_id'],
                    worker_incarnation=payload['worker_incarnation'], delivery_id=payload['delivery_id'],
                    text=payload['text'], how=payload['how']))
        except UnitRefused as refusal:
            self._refused(operation, refusal)
            return
        except Exception as exc:  # noqa: BLE001 - an outcome nobody can vouch for: recorded, never retried
            log.exception('the %s of %s ended with an unknown outcome', kind, operation['operation_id'])
            self._unknown(operation, exc)
            return
        with self.store.transaction() as tx:
            row = store.load_delivery(tx, payload['delivery_id'])
            try:
                delivery.written(tx, row, {'delivery_id': payload['delivery_id']})
            except delivery.Refused as refusal:
                log.info('the write of %s is not recorded on its delivery: %s', payload['delivery_id'], refusal.code)
            store.move_work(tx, operation['operation_id'], 'attempting', 'done', {'written': True})
        self.wake.set()

    def _refused(self, operation: dict, refusal: UnitRefused) -> None:
        """Nothing was written. A new turn that could not begin fails, and its delivery is quarantined with it (the
        target turn ended with no acceptance). A worker that refused a new turn may still be alive: it is stopped
        before any other unit starts. A steer that was refused waits for its turn's end like any other."""
        payload = operation['payload']
        code = refusal.code if records.CODE_WORD.fullmatch(str(refusal.code)) else 'start_failed'
        with self.store.transaction() as tx:
            store.move_work(tx, operation['operation_id'], 'attempting', 'failed',
                            {'code': code, 'detail': str(refusal.detail)})
            if payload['how'] == 'new_turn':
                session = store.load(tx, operation['session_id'])
                self._fail_turn(tx, session, payload['target_turn_id'], code, str(refusal.detail),
                                stop_worker=operation['kind'] == 'send')
        self.wake.set()

    def _unknown(self, operation: dict, exc: Exception) -> None:
        """Nobody can say whether the input was written or a unit started. The intent is ``outcome_unknown`` and
        is never retried. For a new turn the session's unit may exist, so it is stopped before anything else runs;
        the delivery stays ``delivering`` until its turn ends or the daemon restarts, and then it is unknown."""
        payload = operation['payload']
        with self.store.transaction() as tx:
            store.move_work(tx, operation['operation_id'], 'attempting', 'outcome_unknown',
                            {'error': type(exc).__name__})
            if payload['how'] == 'new_turn':
                session = store.load(tx, operation['session_id'])
                if session['state'] == 'live' and session['active_turn_id'] == payload['target_turn_id']:
                    self._ask_stop(tx, session)
        self.wake.set()

    def _fail_turn(self, tx, session: dict, turn_id: str, code: str, detail: str, *, stop_worker: bool) -> None:
        turn = next((turn for turn in session['turns'] if turn['turn_id'] == turn_id), None)
        if turn is None or turn['state'] not in ('queued', 'running'):
            return
        result = {'code': code, 'detail': detail, 'result_ref': None}
        after = machine.turn_finished(session, turn_id, outcome='failed', result=result, at=self.now(),
                                      worker_alive=stop_worker)
        store.save(tx, session, after)
        delivery.quarantine_turn(tx, session['session_id'], turn_id)
        tx.append(events.turn_ended(session['session_id'], turn['run_id'], turn_id, 'failed', result))
        if stop_worker and after['state'] == 'idle' and after['worker'] is not None:
            self._ask_stop(tx, after)

    def _stop(self, operation: dict) -> None:
        run_id = operation['payload']['run_id']
        try:
            answer = self.ports.units.stop(run_id)
        except Exception:  # noqa: BLE001 - a Stop is asked again; it is never taken as done
            log.exception('the Stop of %s failed; it will be asked again', run_id)
            answer = {'code': 'pending', 'run_id': run_id, 'state': 'pending', 'reason': 'confirm_unknown'}
        # None: no run of that id is registered, so nothing of it runs, unless a launch of it was still open, or
        # had returned, when this Stop was claimed (read in the claim, before the Stop looked). Any answer but a
        # well-formed confirmation of this run's Stop is NOT stopped.
        if answer is None:
            code = 'pending' if operation['may_register'] else 'ended'
        else:
            code = records.stop_confirmed(answer, run_id) or 'pending'
        with self.store.transaction() as tx:
            if code == 'pending':
                store.move_work(tx, operation['operation_id'], 'attempting', 'pending', {'answer': 'pending'})
                return
            store.move_work(tx, operation['operation_id'], 'attempting', 'done', {'answer': code})
            session = store.load(tx, operation['session_id'])
            if session['state'] != 'stop_pending' or session['worker'] is None or session['worker']['run_id'] != run_id:
                return
            active = session['active_turn_id']
            after = machine.stop_settled(session, code, self.now())
            store.save(tx, session, after)
            if active is not None:
                delivery.quarantine_turn(tx, session['session_id'], active)
                turn = next(turn for turn in after['turns'] if turn['turn_id'] == active)
                tx.append(events.turn_ended(session['session_id'], turn['run_id'], active, turn['outcome'],
                                            turn['result']))
        self.wake.set()

    def _launch_may_register(self, tx, run_id: str) -> bool:
        """Whether a launch of ``run_id`` is pending or attempting (it may still register that run), or returned
        (``done``: its run exists, and the port may register it after the call returned). A launch of a run is
        committed with the turn that binds the run, before any Stop of it can be asked, so a launch that is
        ``failed`` or ``outcome_unknown`` when a Stop is claimed never opens again."""
        return any(operation['kind'] == 'launch' and operation['payload']['run_id'] == run_id
                   for state in ('pending', 'attempting', 'done') for operation in store.work(tx, state=state))

    # --------------------------------------------------------------- what the engine side reports back
    def accepted(self, facts: dict) -> str:
        """The engine's typed acceptance of an input: ``{delivery_id, session_id, run_id, turn_id, how}``, read from
        an input.accepted draft of the driver. Returns 'delivered', or the refusal's code (``delivery_unknown``
        for a delivery id this lane never committed, and for facts that are not an object at all; the rule's own
        codes otherwise). A refusal changes nothing, except that an acceptance for an ``outcome_unknown`` delivery
        is kept as evidence.

        In a store whose transactions can overlap, two calls that change the same rows at once are refused by the
        store (``FakeStore(serial=False)`` raises its own error): the loser raises that error and has changed
        nothing. An acceptance can be repeated with the same facts, so the caller repeats a call that raised; the
        second call answers by the rule (``not_delivering`` when the first one delivered it). The store's
        conflict has no published type the lane could name (questions.md Q12)."""
        if not isinstance(facts, dict):
            return 'delivery_unknown'
        with self.store.transaction() as tx:
            row = store.load_delivery(tx, facts.get('delivery_id'))
            if row is None:
                return 'delivery_unknown'
            try:
                delivery.accept(tx, row, facts)
            except delivery.Refused as refusal:
                return refusal.code
            if row['how'] == 'new_turn':
                session = store.load(tx, row['session_id'])
                after = machine.turn_running(session, row['target_turn_id'], self.now())
                store.save(tx, session, after)
                turn = next(turn for turn in after['turns'] if turn['turn_id'] == row['target_turn_id'])
                text = self._text_of(tx, row)
                tx.append(events.turn_started(row['session_id'], row['run_id'], row['target_turn_id'],
                                              row['delivery_id'], turn['index'], text))
        self.wake.set()
        return 'delivered'

    def _text_of(self, tx, row: dict) -> str:
        for operation in store.work(tx, session_id=row['session_id']):
            if operation['kind'] in TURN_KINDS and operation['payload'].get('delivery_id') == row['delivery_id']:
                return operation['payload']['text']
        return ''

    def turn_finished(self, session_id: str, turn_id: str, *, outcome: str, result, worker_alive: bool) -> dict:
        """The engine yielded, or failed: the turn ends, every delivery aimed at it that has no acceptance is
        unknown, and ``turn.ended`` is appended, in one commit. Returns the session after."""
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None:
                raise machine.Refused('session_unknown')
            turn = next((turn for turn in session['turns'] if turn['turn_id'] == turn_id), None)
            after = machine.turn_finished(session, turn_id, outcome=outcome, result=result, at=self.now(),
                                          worker_alive=worker_alive)
            store.save(tx, session, after)
            delivery.quarantine_turn(tx, session_id, turn_id)
            tx.append(events.turn_ended(session_id, turn['run_id'], turn_id, outcome, result))
        self.wake.set()
        return after

    def engine_session_reported(self, session_id: str, engine_session_id: str) -> None:
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None:
                raise machine.Refused('session_unknown')
            try:
                after = machine.engine_session(session, engine_session_id)
            except machine.Refused as refusal:
                if refusal.code == 'unchanged':
                    return
                raise
            store.save(tx, session, after)


class SessionsService:
    """The S13 service of lane SESSIONS: recovery once at start, then passes over the queue and the work intents
    whenever it is woken (after a commit) and at least every ``interval_s``. ``start`` returns at once, after
    starting the thread; it does nothing while a loop it started is still running, and after a loop that has
    left (a crash, kept in ``failure``) it is a fresh start: a new thread, recovery again, and ``failure`` cleared.
    ``ready`` is true once recovery committed; ``stop`` returns only when the thread has joined. Not registered
    anywhere in this slice."""

    name = 'sessions'

    def __init__(self, sessions: Sessions, *, interval_s: float = 1.0) -> None:
        self.sessions = sessions
        self.interval_s = interval_s
        self.ready = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()      # one start or stop at a time; stop holds it until its thread joined
        self.passes = 0
        self.failure: BaseException | None = None

    def start(self, context) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self.failure = None
            self._thread = threading.Thread(target=self._loop, name='sessions-service', daemon=True)
            self._thread.start()

    def stop(self, context) -> None:
        with self._lock:
            self._stopping.set()
            self.sessions.wake.set()
            thread, self._thread = self._thread, None
            if thread is not None:
                thread.join()

    def _loop(self) -> None:
        """Recovery, then the passes. ``ready`` is true from the end of recovery until the loop leaves, for
        whatever reason: a stop, or a crash of a pass (a ``BaseException`` out of a port, kept in ``failure``)."""
        try:
            self.sessions.recover()
        except BaseException as exc:  # noqa: BLE001 - not ready; the failure is kept for whoever asks
            log.exception('sessions recovery failed; the service is not ready')
            self.failure = exc
            return
        self.ready = True
        try:
            while not self._stopping.is_set():
                self.sessions.wake.clear()
                try:
                    self.sessions.dispatch_queued()
                    self.sessions.run_pending()
                except Exception:  # noqa: BLE001 - a failed pass is logged; the next pass tries again
                    log.exception('a sessions pass failed')
                self.passes += 1
                self.sessions.wake.wait(self.interval_s)
        except BaseException as exc:  # noqa: BLE001 - the loop is gone: not ready, and the failure is kept
            log.exception('the sessions service stopped on an error; it is not ready')
            self.failure = exc
        finally:
            self.ready = False
