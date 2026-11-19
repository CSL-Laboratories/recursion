"""Dispatcher regressions for an interrupted command slot, failure logs and a typed refusal.

Run from the repository root with PYTHONPATH=core. Markers here are synthetic.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import logging
import sys
import threading
import time
import unittest

from recursion_core.control import commands, methods


class MemoryStore:
    """Only the dispatcher's two receipt operations and transactional exit."""

    def __init__(self):
        self.receipts = {}
        self.commits = self.rollbacks = 0
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            pending = copy.deepcopy(self.receipts)

            class Tx:
                def receipt_get(self, tenant, method, command_id):
                    return copy.deepcopy(pending.get((tenant, method, command_id)))

                def receipt_put(self, tenant, method, command_id, digest, answer):
                    pending[(tenant, method, command_id)] = (digest, copy.deepcopy(answer))

            try:
                yield Tx()
            except BaseException:
                self.rollbacks += 1
                raise
            else:
                self.receipts = pending
                self.commits += 1


def make_table(method, handler, *, tenant=methods.INSTALL_TENANT):
    table = methods.MethodTable()
    table.store = MemoryStore()
    options = {"tenant": tenant} if methods.METHODS[method].idempotency == "command_id" else {}
    table.register(method, handler, **options)
    table.freeze()
    return table


def _entry_return_code():
    """The return the lone-caller test interrupts.

    The pre-change entry interrupts the return of ``_Slot.__enter__``. The answered entry has no
    ``_Slot``; it interrupts the return of ``_enter_count``, which is the last event before the
    per-key ``with``. The outcome assertions are the same either way.
    """
    if hasattr(methods, "_Slot"):
        return methods._Slot.__enter__.__code__
    return methods.MethodTable._enter_count.__code__


def slot_observation():
    """One interruption at slot entry; one bounded same-key retry.

    The fixture releases an orphaned lock only after recording the defect, so
    the helper thread always has a chance to finish; it is never left behind.
    """
    table = make_table("session.turn", lambda ctx, params: {"code": "ok"})
    params = {"command_id": "command-one", "text": "hello"}
    injected = []
    entry_return = _entry_return_code()

    def trace(frame, event, arg):
        if not injected and frame.f_code is entry_return and event == "return":
            injected.append(True)
            raise KeyboardInterrupt("synthetic interruption at slot entry")
        return trace

    before = sys.gettrace()
    try:
        sys.settrace(trace)
        try:
            table.dispatch("session.turn", params, None, None)
        except KeyboardInterrupt:
            pass
    finally:
        sys.settrace(before)
    key = (methods.INSTALL_TENANT, "session.turn", "command-one")
    orphan = table._slots.get(key)
    stranded = orphan is not None and orphan[0].locked()
    commits_before_retry = table.store.commits
    attempted, finished = threading.Event(), threading.Event()
    answers, errors = [], []

    def retry():
        attempted.set()
        try:
            answers.append(table.dispatch("session.turn", params, None, None))
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            finished.set()

    worker = threading.Thread(target=retry, daemon=True)
    worker.start()
    assert attempted.wait(2.0)
    returned_before_fixture_cleanup = finished.wait(0.2)
    if stranded:
        orphan[0].release()  # fixture recovery, not product recovery
    worker.join(2.0)
    assert not worker.is_alive(), "bounded fixture cleanup failed"
    return {"injected": bool(injected), "orphan_lock_held": stranded,
            "commits_before_retry": commits_before_retry,
            "retry_returned_before_cleanup": returned_before_fixture_cleanup,
            "retry_answers": answers, "retry_errors": errors,
            "slots_remaining": len(table._slots)}


@contextlib.contextmanager
def captured_method_logs():
    stream = io.StringIO()
    sink = logging.StreamHandler(stream)
    logger = methods.log
    old_handlers, old_level, old_propagate = logger.handlers, logger.level, logger.propagate
    logger.handlers, logger.propagate = [sink], False
    logger.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        logger.setLevel(old_level)


def exception_log_observation():
    # An inert canary, never a credential and never read from an environment or a file.
    marker = "SYNTHETIC_PRIVATE_VALUE_42"
    observations = {}
    for failure_site in ("read_handler", "command_handler", "tenant", "callback"):
        def broken(*args):
            raise ValueError(marker)

        def with_callback(ctx, params):
            ctx.after_commit(broken)
            return {"code": "ok"}

        method = "session.status" if failure_site == "read_handler" else "session.turn"
        handler = with_callback if failure_site == "callback" else broken
        tenant = broken if failure_site == "tenant" else methods.INSTALL_TENANT
        table = make_table(method, handler, tenant=tenant)
        with captured_method_logs() as logs:
            answer = table.dispatch(method, {"command_id": "command-one"}, None, None)
        observations[failure_site] = {"answer": answer, "canary_in_log": marker in logs.getvalue(),
                                      "canary_in_answer": marker in json.dumps(answer)}
    return observations


def parser_diagnostic_observation():
    marker = "SYNTHETIC_PRIVATE_VALUE_42"

    def broken(family):
        raise ValueError(marker)

    table = commands.CommandTable()
    table.register(("session",), broken, lambda args, client: 0)
    table.freeze()
    parser = argparse.ArgumentParser()
    try:
        table.add_parsers(parser.add_subparsers(dest="command"))
    except commands.CommandTableError as exc:
        return {"exception": type(exc).__name__, "canary_in_refusal": marker in str(exc)}
    raise AssertionError("broken parser was not refused")


def refusal_observation():
    observations = []
    for method in ("session.status", "session.turn"):
        for invalid in ({}, {"code": 7}):
            callbacks = []

            def handler(ctx, params):
                ctx.after_commit(lambda: callbacks.append("ran"))
                raise methods.NotAccepted(invalid)

            table = make_table(method, handler)
            answer = table.dispatch(method, {"command_id": "command-one"}, None, None)
            observations.append({"method": method, "refusal": invalid, "answer": answer,
                                 "callbacks": callbacks, "receipts": len(table.store.receipts)})
    return observations


# Frames of the command-slot entry. Exit frames (after the handler has started) are not in this set:
# the per-key ``with`` still has the ordinary exit window of any ``with``, and the entry path is what
# the reviews hold.
_SLOT_ENTRY = {
    "_Slot.__enter__",
    "_SlotEntry.__enter__",
    "MethodTable._slot",
    "MethodTable._with_slot",
    "MethodTable._enter_count",
}
_BOUND_S = 2.0


def _qual(frame):
    return getattr(frame.f_code, "co_qualname", frame.f_code.co_name)


def _is_slot_entry(frame):
    return _qual(frame) in _SLOT_ENTRY


def make_commands(turn_handler, queue_handler=None):
    table = methods.MethodTable()
    table.store = MemoryStore()
    table.register("session.turn", turn_handler, tenant=methods.INSTALL_TENANT)
    table.register(
        "session.queue",
        queue_handler or (lambda ctx, params: {"code": "ok"}),
        tenant=methods.INSTALL_TENANT,
    )
    table.freeze()
    return table


def _lone_entry_events():
    """Every slot-entry trace event of one cold dispatch, in order, before the handler runs."""
    seen, done = [], []

    def handler(ctx, params):
        return {"code": "ok"}

    table = make_commands(handler)

    def trace(frame, event, arg):
        if done:
            return trace
        if frame.f_code.co_name == "handler":
            done.append(True)
            return trace
        if _is_slot_entry(frame):
            seen.append((_qual(frame), event, frame.f_lineno, frame.f_lasti))
        return trace

    before = sys.gettrace()
    sys.settrace(trace)
    try:
        table.dispatch("session.turn", {"command_id": "command-one", "text": "hello"}, None, None)
    finally:
        sys.settrace(before)
    return seen


def _interrupt_lone(nth):
    """Interrupt the nth slot-entry event of a cold dispatch. Record locks before the test clears them."""
    injected, done, count = [], [], [0]
    params = {"command_id": "command-one", "text": "hello"}

    def handler(ctx, params):
        return {"code": "ok"}

    table = make_commands(handler)

    def trace(frame, event, arg):
        if threading.current_thread().name != "lone":
            return trace
        if done:
            return trace
        if frame.f_code.co_name == "handler":
            done.append(True)
            return trace
        if _is_slot_entry(frame):
            if count[0] == nth and not injected:
                injected.append((_qual(frame), event, frame.f_lineno))
                raise KeyboardInterrupt("synthetic interruption at slot entry")
            count[0] += 1
        return trace

    def run_caller():
        try:
            table.dispatch("session.turn", params, None, None)
        except KeyboardInterrupt:
            pass

    # The caller is a helper so a stranded table lock fails this bound instead of stalling the run.
    threading.settrace(trace)
    caller = threading.Thread(target=run_caller, name="lone", daemon=True)
    caller.start()
    caller.join(_BOUND_S)
    threading.settrace(None)
    if caller.is_alive() and table._slots_lock.locked():
        table._slots_lock.release()  # the test lets the helper go; the product did not
        caller.join(_BOUND_S)
    observed = {
        "injected": injected,
        "table_lock_held": table._slots_lock.locked(),
        "slots": {key: (slot[0].locked(), slot[1]) for key, slot in table._slots.items()},
    }
    other_done, other = threading.Event(), []

    def other_call():
        try:
            other.append(table.dispatch("session.queue", {"command_id": "command-other"}, None, None))
        except BaseException as exc:  # noqa: BLE001 - the observation is the failure mode
            other.append(type(exc).__name__)
        finally:
            other_done.set()

    other_thread = threading.Thread(target=other_call, daemon=True)
    other_thread.start()
    observed["other_finished"] = other_done.wait(_BOUND_S)
    observed["other"] = list(other)
    retry_done, retry = threading.Event(), []

    def retry_call():
        try:
            retry.append(table.dispatch("session.turn", params, None, None))
        except BaseException as exc:  # noqa: BLE001 - the observation is the failure mode
            retry.append(type(exc).__name__)
        finally:
            retry_done.set()

    retry_thread = threading.Thread(target=retry_call, daemon=True)
    retry_thread.start()
    observed["retry_finished"] = retry_done.wait(_BOUND_S)
    observed["retry"] = list(retry)
    if table._slots_lock.locked():
        table._slots_lock.release()  # the test lets the helper go; the product did not
    for slot in list(table._slots.values()):
        if slot[0].locked():
            slot[0].release()
    other_thread.join(_BOUND_S)
    retry_thread.join(_BOUND_S)
    observed["threads_left"] = other_thread.is_alive() or retry_thread.is_alive()
    return observed


def _post_acquire_events():
    """Slot-entry events after a queued caller actually takes the per-key lock, before its handler."""
    return _with_holder(record=True)


def _interrupt_after_acquire(nth):
    """Interrupt that nth post-acquire event while another caller is queued on the same lock."""
    return _with_holder(record=False, nth=nth)


def _with_holder(record, nth=None):
    release, holding = threading.Event(), threading.Event()
    params = {"command_id": "command-one", "text": "hello"}
    key = (methods.INSTALL_TENANT, "session.turn", "command-one")

    def handler(ctx, params):
        # The holder waits inside the handler, then leaves by interruption so it stores no receipt.
        # A stored receipt would make the next caller a replay, and the replay never reaches the handler.
        if not holding.is_set():
            holding.set()
            release.wait(_BOUND_S)
            raise KeyboardInterrupt("holder leaves without a receipt")
        return {"code": "ok"}

    def run_holder():
        try:
            table.dispatch("session.turn", params, None, None)
        except KeyboardInterrupt:
            pass

    table = make_commands(handler)
    holder = threading.Thread(target=run_holder, name="holder", daemon=True)
    holder.start()
    if not holding.wait(_BOUND_S):
        release.set()
        holder.join(_BOUND_S)
        return {"setup": "holder did not enter", "events": [], "injected": []}
    # ``capture`` turns on only once the victim has gone quiet inside the per-key acquire.
    # Events before that are the entry that happens while the holder still owns the lock.
    capture, recorded, injected, done = [], [], [], []
    last_event = [time.monotonic()]

    def trace(frame, event, arg):
        if threading.current_thread().name != "victim":
            return trace
        last_event[0] = time.monotonic()
        if not capture:
            return trace
        if done:
            return trace
        if frame.f_code.co_name == "handler":
            done.append(True)
            return trace
        if not _is_slot_entry(frame):
            return trace
        recorded.append((_qual(frame), event, frame.f_lineno, frame.f_lasti))
        if record or injected:
            return trace
        if len(recorded) == nth + 1:
            injected.append(recorded[-1])
            _queue_waiter(table, params, key, injected)
            raise KeyboardInterrupt("synthetic interruption after acquire")
        return trace

    def victim_is_blocked():
        slot = table._slots.get(key)
        quiet = time.monotonic() - last_event[0] > 0.05
        return slot is not None and slot[1] >= 2 and quiet

    def run_victim():
        try:
            table.dispatch("session.turn", params, None, None)
        except KeyboardInterrupt:
            pass

    threading.settrace(trace)
    victim = threading.Thread(target=run_victim, name="victim", daemon=True)
    victim_started = False
    try:
        victim.start()
        victim_started = True
        if not _wait_until(victim_is_blocked):
            return {"setup": "victim did not block in acquire", "events": list(recorded), "injected": injected}
        capture.append(True)
        release.set()
        victim.join(_BOUND_S)
        holder.join(_BOUND_S)
    finally:
        threading.settrace(None)
        release.set()
        holder.join(_BOUND_S)
        if victim_started:
            victim.join(_BOUND_S)
    if record:
        return recorded
    # The queued waiter was started from the trace, while the victim held the per-key lock.
    waiter = injected[1] if len(injected) > 1 else None
    finished = False
    answer = []
    if waiter is not None:
        finished = waiter["done"].wait(_BOUND_S)
        answer = list(waiter["answers"])
    table_held = table._slots_lock.locked()
    slots = {item: (slot[0].locked(), slot[1]) for item, slot in table._slots.items()}
    if table._slots_lock.locked():
        table._slots_lock.release()
    for slot in list(table._slots.values()):
        if slot[0].locked():
            slot[0].release()
    if waiter is not None:
        waiter["thread"].join(_BOUND_S)
    victim.join(_BOUND_S)
    return {
        "setup": "ok",
        "injected": injected[:1],
        "waiter_finished": finished,
        "waiter_answers": answer,
        "table_lock_held": table_held,
        "slots": slots,
        "threads_left": victim.is_alive() or (waiter is not None and waiter["thread"].is_alive()),
    }


def _queue_waiter(table, params, key, injected):
    """Start a same-key caller and return once it is counted. The caller blocks on the lock we hold."""
    box = {"done": threading.Event(), "answers": [], "thread": None}

    def run():
        try:
            box["answers"].append(table.dispatch("session.turn", params, None, None))
        except BaseException as exc:  # noqa: BLE001 - the observation is the failure mode
            box["answers"].append(type(exc).__name__)
        finally:
            box["done"].set()

    before = (table._slots.get(key) or [None, 0])[1]
    thread = threading.Thread(target=run, name="queued", daemon=True)
    box["thread"] = thread
    thread.start()
    _wait_until(lambda: (table._slots.get(key) or [None, 0])[1] >= before + 1)
    injected.append(box)


def _wait_until(predicate):
    deadline = time.monotonic() + _BOUND_S
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _Uncopyable:
    """A refusal value whose copy raises. The dispatcher must still answer."""

    def __deepcopy__(self, memo):
        raise TypeError("cannot copy me")


class SweepS2bRegressionTest(unittest.TestCase):
    def test_f01_interrupted_slot_entry_releases_ownership(self):
        observed = slot_observation()
        self.assertTrue(observed["injected"])
        self.assertFalse(observed["orphan_lock_held"], observed)
        self.assertTrue(observed["retry_returned_before_cleanup"], observed)
        self.assertEqual(observed["retry_answers"], [{"code": "ok"}])
        self.assertEqual(observed["slots_remaining"], 0)

    def test_f03_failure_logging_omits_exception_values(self):
        for site, observed in exception_log_observation().items():
            with self.subTest(site=site):
                self.assertFalse(observed["canary_in_log"], observed)
        with self.subTest(site="command_parser"):
            observed = parser_diagnostic_observation()
            self.assertFalse(observed["canary_in_refusal"], observed)

    def test_f05_not_accepted_still_requires_a_typed_answer(self):
        for observed in refusal_observation():
            with self.subTest(method=observed["method"], refusal=observed["refusal"]):
                self.assertEqual(observed["answer"].get("code"), "unavailable", observed)
                self.assertEqual((observed["callbacks"], observed["receipts"]), ([], 0))

    def test_every_entry_frame_of_a_lone_caller_drops_the_slot(self):
        events = _lone_entry_events()
        self.assertGreater(len(events), 0, events)
        for nth, event in enumerate(events):
            with self.subTest(nth=nth, event=event):
                observed = _interrupt_lone(nth)
                self.assertEqual(observed["injected"], [event[:3]], observed)
                self.assertFalse(observed["table_lock_held"], observed)
                self.assertEqual(observed["slots"], {}, observed)
                self.assertTrue(observed["other_finished"], observed)
                self.assertEqual(observed["other"], [{"code": "ok"}], observed)
                self.assertTrue(observed["retry_finished"], observed)
                self.assertEqual(observed["retry"], [{"code": "ok"}], observed)
                self.assertFalse(observed["threads_left"], observed)

    def test_every_frame_after_acquire_with_a_waiter_queued_drops_the_lock(self):
        events = _post_acquire_events()
        self.assertIsInstance(events, list, events)
        self.assertGreater(len(events), 0, events)
        for nth, event in enumerate(events):
            with self.subTest(nth=nth, event=event):
                observed = _interrupt_after_acquire(nth)
                self.assertEqual(observed["setup"], "ok", observed)
                self.assertEqual(observed["injected"], [event], observed)
                self.assertFalse(observed["table_lock_held"], observed)
                self.assertEqual(observed["slots"], {}, observed)
                self.assertTrue(observed["waiter_finished"], observed)
                self.assertEqual(observed["waiter_answers"], [{"code": "ok"}], observed)
                self.assertFalse(observed["threads_left"], observed)

    def test_an_untyped_tenant_refusal_names_the_tenant(self):
        for invalid in ({}, {"code": 7}, {"code": ""}):
            with self.subTest(refusal=invalid):
                called = []

                def tenant_of(principal, params, invalid=invalid):
                    raise methods.NotAccepted(invalid)

                def handler(ctx, params):
                    called.append("ran")
                    return {"code": "ok"}

                table = methods.MethodTable()
                table.store = MemoryStore()
                table.register("session.turn", handler, tenant=tenant_of)
                table.freeze()
                answer = table.dispatch("session.turn", {"command_id": "command-one"}, None, None)
                self.assertEqual(answer, {"code": "unavailable", "detail": "tenant:NotAccepted"})
                self.assertEqual(called, [])
                self.assertEqual(table.store.receipts, {})
                self.assertEqual(table.store.commits, 0)

    def test_a_typed_tenant_refusal_is_returned_and_a_handler_refusal_names_the_handler(self):
        def tenant_of(principal, params):
            raise methods.NotAccepted({"code": "no", "detail": "because"})

        table = make_table("session.turn", lambda ctx, params: {"code": "ok"}, tenant=tenant_of)
        self.assertEqual(
            table.dispatch("session.turn", {"command_id": "command-one"}, None, None),
            {"code": "no", "detail": "because"},
        )
        for invalid in ({}, {"code": 7}, {"code": ""}):
            with self.subTest(refusal=invalid):
                callbacks = []

                def handler(ctx, params, invalid=invalid):
                    ctx.after_commit(lambda: callbacks.append("ran"))
                    raise methods.NotAccepted(invalid)

                table = make_table("session.turn", handler)
                answer = table.dispatch("session.turn", {"command_id": "command-one"}, None, None)
                self.assertEqual(answer, {"code": "unavailable", "detail": "handler:NotAccepted"})
                self.assertEqual(callbacks, [])
                self.assertEqual(table.store.receipts, {})

    def test_a_refusal_that_cannot_be_copied_is_still_an_answer(self):
        def handler(ctx, params):
            raise methods.NotAccepted({"code": "no", "value": _Uncopyable()})

        table = make_table("session.turn", handler)
        try:
            answer = table.dispatch("session.turn", {"command_id": "command-one"}, None, None)
        except BaseException as exc:  # noqa: BLE001 - the old dispatcher lets the copy error escape
            answer = exc
        self.assertEqual(answer, {"code": "unavailable", "detail": "handler:NotAccepted"})

    def test_a_command_whose_params_are_not_a_dict_is_refused(self):
        table = make_commands(lambda ctx, params: {"code": "ok"})
        for params in (None, [], "x"):
            with self.subTest(params=params):
                try:
                    answer = table.dispatch("session.turn", params, None, None)
                except BaseException as exc:  # noqa: BLE001 - the old dispatcher raises AttributeError
                    answer = exc
                self.assertEqual(answer, {"code": "request_invalid", "detail": "params"})
        # A read with no params is refused as well: master refuses params that are not a mapping on a read too.
        read = make_table("session.status", lambda ctx, params: {"code": "ok"})
        self.assertEqual(read.dispatch("session.status", None, None, None), {"code": "request_invalid", "detail": "params"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
