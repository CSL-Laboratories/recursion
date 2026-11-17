"""The approvals port's withdraw (routes/driver.py, PORT WITHDRAW): the Liaison's ruling of Nov 15, 7:07 PM, for
#402's round 5 (P2), as corrected after #432's first reads (Codex GPT-6 Astra and GPT-6.1 Sol R1 to R3, DeepSeek
F1 to F7, and the review thread on driver.py's guarantee).

A request admitted to the port is made visible only through ``ticket.publish(reveal)``; ``withdraw_port`` (and the
close input) seal the worker's port first and then end every ticket, waiting only for a reveal already running. When
``withdraw_port`` returns, no approval of the worker becomes visible afterwards. The stand-in port below keeps its
approvals in a SQLite file, and a second connection is the surface that reads them."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from recursion_core.routes import driver as d

# The race below forces the port into its reveal in half of the rounds; 200 rounds are enough to show the old
# protocol's window on either interpreter (the first reads measured it in 200 of 200 held requests).
RACE_ROUNDS = int(os.environ.get("PORT_WITHDRAW_ROUNDS", "200"))
GUARD_S = 30.0  # bound only against a hang; no assertion depends on it


class _Driver(d.Driver):
    declaration = d.Declaration("claude_code", d.Flags(multi_turn=True, permission_bridge=True), input_ack=False)

    def __init__(self, send_raises: BaseException | None = None) -> None:
        self.sent: list[dict] = []
        self._raises = send_raises

    def _send(self, handle, item):
        if self._raises is not None:
            raise self._raises
        self.sent.append(item)


class Store:
    """APPROVALS' side, as the corrected contract asks: the reveal writes the approval's row and commits it, which is
    what makes it visible; nothing is written unless the ticket runs the reveal. ``visible`` reads as a surface does."""

    def __init__(self, test: unittest.TestCase) -> None:
        folder = tempfile.mkdtemp(prefix="port-withdraw-")
        self.path = os.path.join(folder, "approvals.db")
        test.addCleanup(shutil.rmtree, folder, True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY)")
        db.close()
        self.reader = sqlite3.connect(self.path, check_same_thread=False)
        test.addCleanup(self.reader.close)

    def visible(self) -> list[str]:
        return [row[0] for row in self.reader.execute("SELECT id FROM approvals ORDER BY id")]

    def request(self, ticket: d.PortTicket, approval_id: str, before_commit=lambda: None) -> bool:
        db = sqlite3.connect(self.path, timeout=GUARD_S)
        try:
            def reveal():
                before_commit()
                try:
                    db.execute("INSERT INTO approvals (id) VALUES (?)", (approval_id,))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

            # False: refused, the request was withdrawn (APPROVALS answers a typed refusal here); nothing was written.
            return ticket.publish(reveal)
        finally:
            db.close()


class _OneShotRaisingLock:
    """A ticket's lock whose next acquisition raises ``exc`` once (an interruption at the lock), then behaves."""

    def __init__(self, exc: BaseException) -> None:
        self._lock, self._exc = threading.Lock(), exc

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        exc, self._exc = self._exc, None
        if exc is not None:
            raise exc
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _handle() -> d.Handle:
    return d.Handle("run-1", None)


def _thread(test: unittest.TestCase, target, *release: threading.Event) -> threading.Thread:
    """Start ``target``. Cleanup releases every event FIRST, then joins with a bound: a regression fails, not hangs."""
    t = threading.Thread(target=target, daemon=True)
    t.start()

    def stop():
        for event in release:
            event.set()
        t.join(GUARD_S)

    test.addCleanup(stop)
    return t


class WithdrawTest(unittest.TestCase):
    def test_a_request_held_between_admission_and_publication_is_withdrawn_by_close_and_opens_nothing(self) -> None:
        for close in ("withdraw_port", "close input"):
            with self.subTest(close=close):
                drv, h, store = _Driver(), _handle(), Store(self)
                ticket = drv.admit_port(h)
                if close == "withdraw_port":
                    drv.withdraw_port(h)
                else:
                    drv.send(h, {"kind": "close"})
                self.assertEqual(ticket.state, d.WITHDRAWN)
                # The caller still holds the ticket and calls the port: the call is refused there and opens nothing.
                self.assertFalse(store.request(ticket, "apr-1"))
                self.assertEqual(store.visible(), [])

    def test_no_request_is_admitted_after_close_or_on_a_closed_input(self) -> None:
        drv, h = _Driver(), _handle()
        drv.withdraw_port(h)
        with self.assertRaises(d.DriverStateError):
            drv.admit_port(h)
        h2 = _handle()
        h2._closed = True  # an input closed without the seal (the state an interruption could once leave)
        with self.assertRaises(d.DriverStateError):
            drv.admit_port(h2)

    def test_a_published_request_stays_published_and_close_withdraws_only_the_rest(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        first, second = drv.admit_port(h), drv.admit_port(h)
        self.assertTrue(store.request(first, "apr-1"))
        drv.withdraw_port(h)
        self.assertFalse(store.request(second, "apr-2"))
        self.assertEqual((first.state, second.state, store.visible()), (d.PUBLISHED, d.WITHDRAWN, ["apr-1"]))
        drv.withdraw_port(h)  # harmless again
        self.assertFalse(first.withdraw())

    def test_withdraw_reaches_every_unpublished_ticket(self) -> None:
        drv, h = _Driver(), _handle()
        tickets = [drv.admit_port(h) for _ in range(5)]
        drv.withdraw_port(h)
        self.assertEqual([t._state for t in tickets], [d.WITHDRAWN] * 5)  # each ended, not only read as sealed
        self.assertEqual(h._tickets, [])

    def test_close_is_bounded_while_the_port_holds_a_request(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        ticket = drv.admit_port(h)
        inside, release, opened = threading.Event(), threading.Event(), []

        def port_call():  # the port has the request and is slow to reach its commit
            inside.set()
            release.wait(GUARD_S)
            opened.append(store.request(ticket, "apr-1"))

        t = _thread(self, port_call, release)
        self.assertTrue(inside.wait(GUARD_S))
        returned = threading.Event()
        _thread(self, lambda: (drv.withdraw_port(h), returned.set()))
        self.assertTrue(returned.wait(GUARD_S), "close waited for the port")
        self.assertEqual(ticket.state, d.WITHDRAWN)
        release.set()
        t.join(GUARD_S)
        self.assertEqual((opened, store.visible()), ([False], []))  # the late call was refused

    def test_no_approval_becomes_visible_after_withdraw_returned(self) -> None:
        """THE RACE (both first reads' R1, the review thread at driver.py 384, and APPROVALS' #436 reads): in odd
        rounds the port is inside its reveal, about to commit, when the withdraw is called; in even rounds the
        withdraw is called first. Whatever is visible after the port finished must have been visible when
        withdraw_port returned."""
        outcomes = {"opened before the withdraw returned": 0, "refused": 0}
        late = []
        for n in range(RACE_ROUNDS):
            drv, h, store = _Driver(), _handle(), Store(self)
            ticket = drv.admit_port(h)
            in_reveal, withdraw_called = threading.Event(), threading.Event()
            got: dict[str, object] = {}

            def committing(in_reveal=in_reveal, withdraw_called=withdraw_called):
                in_reveal.set()
                withdraw_called.wait(GUARD_S)
                time.sleep(0.002)  # let the withdraw run first if nothing holds it back

            def port(store=store, ticket=ticket, committing=committing, got=got, n=n):
                got["opened"] = store.request(ticket, f"apr-{n}", committing)

            t = _thread(self, port, withdraw_called)
            if n % 2:
                self.assertTrue(in_reveal.wait(GUARD_S))
            withdraw_called.set()
            drv.withdraw_port(h)
            at_return = store.visible()
            t.join(GUARD_S)
            self.assertFalse(t.is_alive())
            after = store.visible()
            if after != at_return:
                late.append(n)
            self.assertEqual(bool(got["opened"]), ticket.state == d.PUBLISHED)  # never both, never neither
            self.assertEqual(bool(after), got["opened"])
            outcomes["opened before the withdraw returned" if got["opened"] else "refused"] += 1
        print(f"\nport withdraw race: {RACE_ROUNDS} rounds, {outcomes}, opened after the withdraw returned: "
              f"{len(late)}", flush=True)
        self.assertEqual(late, [], "an approval became visible after withdraw_port returned")

    def test_withdraw_waits_for_every_running_reveal_and_only_for_it(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        tickets = [drv.admit_port(h) for _ in range(3)]  # the last never reaches the port
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        for i in range(2):
            def hold(i=i):
                entered[i].set()
                release[i].wait(GUARD_S)

            _thread(self, lambda i=i, hold=hold: store.request(tickets[i], f"apr-{i}", hold), release[i])
            self.assertTrue(entered[i].wait(GUARD_S))
        returned = threading.Event()
        _thread(self, lambda: (drv.withdraw_port(h, wait=GUARD_S), returned.set()))
        self.assertFalse(returned.wait(0.2))   # two reveals are running: it waits for them
        release[1].set()
        self.assertFalse(returned.wait(0.2))   # one is still running
        release[0].set()
        self.assertTrue(returned.wait(GUARD_S))
        self.assertEqual([t.state for t in tickets], [d.PUBLISHED, d.PUBLISHED, d.WITHDRAWN])
        self.assertEqual(store.visible(), ["apr-0", "apr-1"])

    def test_a_reveal_that_raises_ends_the_ticket_withdrawn_and_nothing_is_visible(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        ticket = drv.admit_port(h)

        def failing_commit():
            raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaises(sqlite3.OperationalError):
            store.request(ticket, "apr-1", failing_commit)
        self.assertEqual((ticket.state, store.visible()), (d.WITHDRAWN, []))
        self.assertFalse(store.request(ticket, "apr-1"))  # no second try on the same ticket
        drv.withdraw_port(h)
        self.assertEqual(store.visible(), [])

    def test_a_reveal_past_the_wait_makes_the_withdraw_raise_and_the_rest_stay_sealed(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        slow, other = drv.admit_port(h), drv.admit_port(h)
        entered, release = threading.Event(), threading.Event()

        def stall():
            entered.set()
            release.wait(GUARD_S)

        t = _thread(self, lambda: store.request(slow, "apr-slow", stall), release)
        self.assertTrue(entered.wait(GUARD_S))
        began = time.monotonic()
        with self.assertRaises(d.PortWithdrawIncomplete) as cm:
            drv.withdraw_port(h, wait=0.2)
        self.assertLess(time.monotonic() - began, GUARD_S)
        self.assertEqual(cm.exception.tickets, [slow])
        self.assertTrue(h.port_sealed)
        self.assertFalse(store.request(other, "apr-other"))
        release.set()
        t.join(GUARD_S)
        drv.withdraw_port(h)  # now it returns: the reveal began before the seal and finished
        self.assertEqual((slow.state, other.state, store.visible()), (d.PUBLISHED, d.WITHDRAWN, ["apr-slow"]))

    def test_a_withdraw_from_inside_a_reveal_refuses_instead_of_waiting_for_itself(self) -> None:
        drv, h, store = _Driver(), _handle(), Store(self)
        ticket = drv.admit_port(h)
        seen = []

        def reenter():
            try:
                drv.withdraw_port(h)
            except d.DriverStateError as exc:
                seen.append(exc)

        began = time.monotonic()
        self.assertTrue(store.request(ticket, "apr-1", reenter))
        self.assertEqual(len(seen), 1)
        self.assertNotIsInstance(seen[0], d.PortWithdrawIncomplete)  # refused at once, not after waiting for itself
        self.assertLess(time.monotonic() - began, d.REVEAL_WAIT_S)
        drv.withdraw_port(h)
        self.assertEqual((ticket.state, store.visible()), (d.PUBLISHED, ["apr-1"]))

    def test_concurrent_admissions_are_all_registered(self) -> None:
        # Every admitted ticket must be on the handle's list, or a withdraw would not wait for its reveal.
        drv, h = _Driver(), _handle()
        start, got = threading.Barrier(4), [[] for _ in range(4)]

        def admit(mine):
            start.wait(GUARD_S)
            for _ in range(250):
                mine.append(drv.admit_port(h))

        threads = [_thread(self, lambda mine=mine: admit(mine)) for mine in got]
        for t in threads:
            t.join(GUARD_S)
        admitted = [t for mine in got for t in mine]
        self.assertEqual(len(admitted), 1000)
        self.assertEqual({id(t) for t in h._tickets}, {id(t) for t in admitted})

    def test_a_ticket_ends_once(self) -> None:
        ran = []
        t = d.PortTicket()
        self.assertEqual((t.withdraw(), t.publish(lambda: ran.append(1)), t.withdraw(), t.state, ran),
                         (True, False, False, d.WITHDRAWN, []))
        t = d.PortTicket()
        self.assertEqual((t.publish(lambda: ran.append(1)), t.withdraw(), t.publish(lambda: ran.append(2)), t.state,
                          ran), (True, False, False, d.PUBLISHED, [1]))

    def test_malformed_arguments_are_refused(self) -> None:
        drv = _Driver()
        for call in (lambda: drv.admit_port(None), lambda: drv.withdraw_port("run-1"),
                     lambda: drv.withdraw_port(_handle(), wait=-1), lambda: drv.withdraw_port(_handle(), wait=True),
                     lambda: d.PortTicket().publish(None)):
            with self.assertRaises(d.DriverInputError):
                call()


class InterruptionTest(unittest.TestCase):
    """Both first reads' R2 and DeepSeek F1: an interruption inside the withdraw leaves no ticket able to open, the
    interruption itself leaves, and a repeated withdraw or close finishes the job."""

    def _three(self, drv, h, interrupted: int):
        tickets = [drv.admit_port(h) for _ in range(3)]
        tickets[interrupted]._lock = _OneShotRaisingLock(KeyboardInterrupt())
        return tickets

    def test_an_interruption_at_any_tickets_lock_leaves_no_ticket_publishable(self) -> None:
        for interrupted in range(3):
            for how in ("withdraw_port", "close input", "close input, write interrupted too"):
                with self.subTest(ticket=interrupted, how=how):
                    drv = _Driver(KeyboardInterrupt() if how.endswith("too") else None)
                    h, store = _handle(), Store(self)
                    tickets = self._three(drv, h, interrupted)
                    with self.assertRaises(KeyboardInterrupt):
                        if how == "withdraw_port":
                            drv.withdraw_port(h)
                        else:
                            drv.send(h, {"kind": "close"})
                    self.assertTrue(h.port_sealed)
                    try:
                        opened = [store.request(t, f"apr-{i}") for i, t in enumerate(tickets)]
                    except BaseException as exc:  # noqa: B036 -- a lock the withdraw never took: a failure here
                        self.fail(f"the withdraw never reached the interrupted ticket's lock: {exc!r}")
                    self.assertEqual((opened, store.visible()), ([False] * 3, []))
                    self.assertEqual([t.state for t in tickets], [d.WITHDRAWN] * 3)

    def test_a_repeated_close_after_an_interrupted_one_finishes_the_withdraw(self) -> None:
        drv, h = _Driver(), _handle()
        tickets = self._three(drv, h, 1)
        with self.assertRaises(KeyboardInterrupt):
            drv.send(h, {"kind": "close"})
        self.assertEqual(drv.sent, [{"kind": "close"}])  # the close was written once
        self.assertEqual(len(h._tickets), 1)              # the interrupted ticket is still to be seen ended
        with self.assertRaises(d.DriverStateError):        # refused as before ...
            drv.send(h, {"kind": "close"})
        self.assertEqual(h._tickets, [])                   # ... after finishing the withdraw
        self.assertEqual([t._state for t in tickets], [d.WITHDRAWN] * 3)
        self.assertEqual(drv.sent, [{"kind": "close"}])   # and nothing was written again
        with self.assertRaises(d.DriverStateError):
            drv.send(h, {"kind": "user_message", "turn_id": "turn-1", "text": "x", "delivery_id": "dlv-1"})

    def test_a_close_whose_write_raises_still_withdraws_and_the_write_error_leaves(self) -> None:
        drv, h, store = _Driver(OSError("broken pipe")), _handle(), Store(self)
        ticket = drv.admit_port(h)
        with self.assertRaises(OSError):
            drv.send(h, {"kind": "close"})
        self.assertEqual((ticket._state, store.request(ticket, "apr-1"), store.visible()), (d.WITHDRAWN, False, []))


if __name__ == "__main__":
    unittest.main()
