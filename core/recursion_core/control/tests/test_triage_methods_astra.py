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


def slot_observation():
    """One interruption after slot acquisition; one bounded same-key retry.

    Trace return is the last event in __enter__, before the with body is armed.
    The fixture releases an orphaned lock only after recording the defect, so
    the helper thread always has a chance to finish; it is never left behind.
    """
    table = make_table("session.turn", lambda ctx, params: {"code": "ok"})
    params = {"command_id": "one", "text": "hello"}
    injected = []

    def trace(frame, event, arg):
        if not injected and frame.f_code is methods._Slot.__enter__.__code__ and event == "return":
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
    key = (methods.INSTALL_TENANT, "session.turn", "one")
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
            answer = table.dispatch(method, {"command_id": "one"}, None, None)
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
            answer = table.dispatch(method, {"command_id": "one"}, None, None)
            observations.append({"method": method, "refusal": invalid, "answer": answer,
                                 "callbacks": callbacks, "receipts": len(table.store.receipts)})
    return observations


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
