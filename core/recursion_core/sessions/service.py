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

THE RESTART (``recover``), in one transaction:
1. every delivery still ``delivering`` becomes ``outcome_unknown``, with its event (queue.item.v1);
2. every launch, send or interrupt not done becomes ``outcome_unknown``: a delivery was quarantined, so it is
   never written, even when the crash came before the write, and an interrupt is overtaken by step 3;
3. every session that has, or may have, a unit (a worker, live or waiting) is given a recovery Stop and goes
   ``stop_pending``: nothing here can reattach to a unit that outlived the daemon, so no new unit starts for
   that session until the old one is confirmed stopped. A session already ``stop_pending`` keeps waiting, and
   its Stop is asked again. Pending never returns a session to idle: only ``stopped`` or ``ended`` does.

QUEUED MESSAGES (``dispatch_queued``). A session's items go in their order, one delivery in flight at a time.
An idle session's first queued item opens the next turn (``new_turn``). On a live session the first item, if
it is a steer, goes into the turn in progress (``mid_turn``) when the driver declares ``mid_turn_input``, the
worker is alive and the turn is running; otherwise it waits for the next turn (the method table's fallback for
``session.steer``: ``next_turn``). Nothing here reads ``input_ack``.

AN INTERRUPT (``interrupt``) acts on a turn, not a run. Where the driver declares ``interrupt`` and the turn is
running in a live worker, the driver's own cancel is committed as work and the turn ends ``interrupted`` only when
the driver reports it (``turn_interrupted``); the worker lives on. Otherwise (no such flag, or the turn has not
started) a Stop of the unit is asked instead (the method table's fallback ``stop_then_resume``): the session is
``stop_pending`` and refuses new turns until the Stop is confirmed, and the next turn then runs in a new unit that
resumes the engine's session where the driver can. A driver that refuses the cancel, or whose cancel has an outcome
nobody can vouch for, while the turn is still in progress, gets the same Stop: an interrupt is never left hanging.
The ``interrupt`` event says ``delivered`` only once the driver reported the interruption.

``Sessions`` holds what the handlers and the service share; ``SessionsService`` is the S13 service object.
Nothing in this module is registered anywhere (``hooks.py``).
"""
from __future__ import annotations

import logging
import threading

from recursion_core.sessions import delivery, events, machine, records, store
from recursion_core.sessions.ports import InterruptOrder, LaunchOrder, Ports, SendOrder, UnitRefused

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
        flags, worker, index = found['flags'], session['worker'], len(session['turns'])
        resume = None
        if worker is not None and worker['alive']:
            if not flags.get('multi_turn'):
                raise machine.Refused('busy', 'worker_alive')     # never a second unit beside a live one
            kind, run_id, alive = 'send', worker['run_id'], True
        elif index == 0:
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
                if operation['kind'] != 'stop':
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
        """Start the delivery of each session's first queued message where it can go now. Returns how many."""
        with self.store.transaction() as tx:
            waiting = store.with_queued_items(tx)
        started = 0
        for session_id in waiting:
            with self.store.transaction() as tx:
                session = store.load(tx, session_id)
                if session is None or store.deliveries(tx, state='delivering', session_id=session_id):
                    continue
                queued = store.items(tx, session_id, ('queued',))
                if not queued:
                    continue
                first = queued[0]
                if session['state'] == 'idle':
                    try:
                        self.open_turn(tx, session, first['text'], item=first)
                    except machine.Refused as refusal:
                        log.info('session %s keeps its queue: %s', session_id, refusal)
                        continue
                    started += 1
                elif session['state'] == 'live' and first['delivery'] == 'steer':
                    started += self._steer_now(tx, session, first)
        return started

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
            if not store.move_work(tx, operation_id, 'pending', 'attempting'):
                return 0
        if operation['kind'] == 'stop':
            self._stop(operation)
        elif operation['kind'] == 'interrupt':
            self._interrupt(operation)
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
                    resume=payload['resume']))
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

    # ---------------------------------------------------------------------------------------- interrupt
    def interrupt(self, session_id: str, expected_turn_id: str, requested_by: dict) -> tuple:
        """Ask the interruption of the turn in progress, which must be ``expected_turn_id``. Commits what it asks and
        returns ``(outcome, session after)``: 'requested' (the driver's cancel is committed as work),
        'fell_back_to_stop' (a Stop of the unit is committed instead), 'stop_pending' (a Stop was already pending:
        nothing more is asked) or 'no_live_turn'. Raises ``machine.Refused``: ``session_unknown``, or
        ``conflict``/``stale_turn`` when another turn is in progress. Nothing outside the store happens here."""
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None:
                raise machine.Refused('session_unknown')
            active = session['active_turn_id']
            if active is None:
                tx.append(events.interrupt(session_id, None, None, requested_by, 'no_live_turn'))
                return 'no_live_turn', session
            if active != expected_turn_id:
                raise machine.Refused('conflict', 'stale_turn')
            if session['state'] == 'stop_pending':
                return 'stop_pending', session
            turn = next(turn for turn in session['turns'] if turn['turn_id'] == active)
            found = self.declaration(session['route']['engine'])
            worker = session['worker']
            if (found is not None and found['flags'].get('interrupt') and turn['state'] == 'running'
                    and worker is not None and worker['alive'] and worker['run_id'] == turn['run_id']):
                store.insert_work(tx, self.mint('op-'), session_id, 'interrupt',
                                  {'turn_id': active, 'run_id': worker['run_id'],
                                   'worker_incarnation': incarnation_of(worker['run_id']),
                                   'requested_by': dict(requested_by)})
                outcome, after = 'requested', session
            else:
                after = self._ask_stop(tx, session)
                tx.append(events.interrupt(session_id, worker['run_id'], active, requested_by, 'fell_back_to_stop'))
                outcome = 'fell_back_to_stop'
        self.wake.set()
        return outcome, after

    def _interrupt(self, operation: dict) -> None:
        payload = operation['payload']
        try:
            self.ports.units.interrupt(InterruptOrder(session_id=operation['session_id'], turn_id=payload['turn_id'],
                                                      run_id=payload['run_id'],
                                                      worker_incarnation=payload['worker_incarnation']))
        except UnitRefused as refusal:
            self._interrupt_fell_back(operation, 'failed', {'code': str(refusal.code), 'detail': str(refusal.detail)})
            return
        except Exception as exc:  # noqa: BLE001 - an outcome nobody can vouch for: never retried; a Stop instead
            log.exception('the interrupt of %s ended with an unknown outcome', payload['turn_id'])
            self._interrupt_fell_back(operation, 'outcome_unknown', {'error': type(exc).__name__})
            return
        with self.store.transaction() as tx:
            store.move_work(tx, operation['operation_id'], 'attempting', 'done', {'sent': True})

    def _interrupt_fell_back(self, operation: dict, state: str, outcome: dict) -> None:
        """The driver did not cancel the turn, or nobody can say whether it did: if that turn is still the one in
        progress, a Stop of its unit is asked instead, so an interrupt never leaves the turn running unseen."""
        payload = operation['payload']
        with self.store.transaction() as tx:
            store.move_work(tx, operation['operation_id'], 'attempting', state, outcome)
            session = store.load(tx, operation['session_id'])
            if session['state'] == 'live' and session['active_turn_id'] == payload['turn_id']:
                self._ask_stop(tx, session)
                tx.append(events.interrupt(session['session_id'], payload['run_id'], payload['turn_id'],
                                           payload['requested_by'], 'fell_back_to_stop'))
        self.wake.set()

    def turn_interrupted(self, session_id: str, turn_id: str, *, worker_alive: bool) -> dict:
        """The driver reports that its cancel interrupted the turn: the turn is ``interrupted``, every delivery aimed
        at it with no acceptance is unknown, and ``turn.ended`` and ``interrupt`` (``delivered``) are appended, in one
        commit. ``requested_by`` is the asker of the interrupt intent for that turn. Returns the session after."""
        with self.store.transaction() as tx:
            session = store.load(tx, session_id)
            if session is None:
                raise machine.Refused('session_unknown')
            asked = [operation for operation in store.work(tx, session_id=session_id)
                     if operation['kind'] == 'interrupt' and operation['payload']['turn_id'] == turn_id]
            if not asked:
                raise machine.Refused('no_interrupt_asked', turn_id)
            after = machine.turn_interrupted(session, turn_id, self.now(), worker_alive=worker_alive)
            store.save(tx, session, after)
            delivery.quarantine_turn(tx, session_id, turn_id)
            run_id = next(turn['run_id'] for turn in after['turns'] if turn['turn_id'] == turn_id)
            tx.append(events.turn_ended(session_id, run_id, turn_id, 'interrupted', None))
            tx.append(events.interrupt(session_id, run_id, turn_id, asked[-1]['payload']['requested_by'], 'delivered'))
        self.wake.set()
        return after

    def _stop(self, operation: dict) -> None:
        run_id = operation['payload']['run_id']
        try:
            answer = self.ports.units.stop(run_id)
        except Exception:  # noqa: BLE001 - a Stop is asked again; it is never taken as done
            log.exception('the Stop of %s failed; it will be asked again', run_id)
            answer = {'code': 'pending', 'run_id': run_id, 'state': 'pending', 'reason': 'confirm_unknown'}
        if answer is None:
            code = 'ended'            # no run of that id was ever registered: nothing of it runs
        elif isinstance(answer, dict) and answer.get('run_id') == run_id and answer.get('code') in ('stopped', 'ended'):
            code = answer['code']
        else:
            code = 'pending'          # a code or a shape this lane does not know is NOT stopped
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

    # --------------------------------------------------------------- what the engine side reports back
    def accepted(self, facts: dict) -> str:
        """The engine's typed acceptance of an input: ``{delivery_id, session_id, run_id, turn_id, how}``, read from
        an input.accepted draft of the driver. Returns 'delivered', or the refusal's code (``delivery_unknown``
        for a delivery id this lane never committed; the rule's own codes otherwise). A refusal changes nothing,
        except that an acceptance for an ``outcome_unknown`` delivery is kept as evidence."""
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
    starting the thread; ``ready`` is true once recovery committed; ``stop`` returns only when the thread has
    joined. Not registered anywhere in this slice."""

    name = 'sessions'

    def __init__(self, sessions: Sessions, *, interval_s: float = 1.0) -> None:
        self.sessions = sessions
        self.interval_s = interval_s
        self.ready = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.passes = 0
        self.failure: BaseException | None = None

    def start(self, context) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name='sessions-service', daemon=True)
        self._thread.start()

    def stop(self, context) -> None:
        self._stopping.set()
        self.sessions.wake.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        self._thread = None

    def _loop(self) -> None:
        try:
            self.sessions.recover()
        except BaseException as exc:  # noqa: BLE001 - not ready; the failure is kept for whoever asks
            log.exception('sessions recovery failed; the service is not ready')
            self.failure = exc
            return
        self.ready = True
        while not self._stopping.is_set():
            self.sessions.wake.clear()
            try:
                self.sessions.dispatch_queued()
                self.sessions.run_pending()
            except Exception:  # noqa: BLE001 - a failed pass is logged; the next pass tries again
                log.exception('a sessions pass failed')
            self.passes += 1
            self.sessions.wake.wait(self.interval_s)
