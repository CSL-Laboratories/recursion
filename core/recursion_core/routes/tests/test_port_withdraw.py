"""The approvals port's withdraw (routes/driver.py, PORT WITHDRAW): the Liaison's ruling of Nov 15, 7:07 PM, for
#402's round 5 (P2, both Codex reads). A request admitted to the port and not yet published is withdrawn by close,
close never waits for the port, and a withdraw racing the publication ends in exactly one of the two."""

from __future__ import annotations

import os
import threading
import unittest

from recursion_core.routes import driver as d

RACE_ROUNDS = int(os.environ.get("PORT_WITHDRAW_ROUNDS", "2000"))


class _Driver(d.Driver):
    declaration = d.Declaration("claude_code", d.Flags(multi_turn=True, permission_bridge=True), input_ack=False)

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def _send(self, handle, item):
        self.sent.append(item)


class Port:
    """A stand-in for APPROVALS' side: it opens an approval only if the ticket publishes."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.refused: list[str] = []

    def request(self, ticket: d.PortTicket, name: str) -> None:
        if ticket.publish():
            self.opened.append(name)
        else:
            self.refused.append(name)  # APPROVALS answers a typed refusal here (its side)


def _handle() -> d.Handle:
    return d.Handle("run-1", None)


class WithdrawTest(unittest.TestCase):
    def test_a_request_held_between_admission_and_publication_is_withdrawn_by_close_and_opens_nothing(self) -> None:
        for close in ("withdraw_port", "close input"):
            with self.subTest(close=close):
                drv, h, port = _Driver(), _handle(), Port()
                ticket = drv.admit_port(h)
                if close == "withdraw_port":
                    drv.withdraw_port(h)
                else:
                    drv.send(h, {"kind": "close"})
                self.assertEqual(ticket.state, d.WITHDRAWN)
                port.request(ticket, "apr-1")
                self.assertEqual((port.opened, port.refused), ([], ["apr-1"]))

    def test_no_request_is_admitted_after_close(self) -> None:
        drv, h = _Driver(), _handle()
        drv.withdraw_port(h)
        with self.assertRaises(d.DriverStateError):
            drv.admit_port(h)

    def test_a_published_request_stays_published_and_close_withdraws_only_the_rest(self) -> None:
        drv, h, port = _Driver(), _handle(), Port()
        first, second = drv.admit_port(h), drv.admit_port(h)
        port.request(first, "apr-1")
        drv.withdraw_port(h)
        port.request(second, "apr-2")
        self.assertEqual((first.state, second.state, port.opened, port.refused),
                         (d.PUBLISHED, d.WITHDRAWN, ["apr-1"], ["apr-2"]))
        drv.withdraw_port(h)  # idempotent
        self.assertFalse(first.withdraw())

    def test_close_is_bounded_while_the_port_holds_a_request(self) -> None:
        drv, h = _Driver(), _handle()
        ticket = drv.admit_port(h)
        inside, release = threading.Event(), threading.Event()

        def port_call():  # the port has the request and is slow to publish
            inside.set()
            release.wait()
            ticket.publish()

        t = threading.Thread(target=port_call)
        t.start()
        self.addCleanup(lambda: (release.set(), t.join()))
        self.assertTrue(inside.wait(30))
        returned = threading.Event()
        closer = threading.Thread(target=lambda: (drv.withdraw_port(h), returned.set()))
        closer.start()
        self.addCleanup(closer.join)
        self.assertTrue(returned.wait(30), "close waited for the port")  # bound only against a hang
        self.assertEqual(ticket.state, d.WITHDRAWN)
        release.set()
        t.join()
        self.assertEqual(ticket.state, d.WITHDRAWN)  # the late publication was refused

    def test_a_withdraw_racing_the_publication_ends_in_exactly_one(self) -> None:
        outcomes = {"published": 0, "withdrawn": 0}
        for n in range(RACE_ROUNDS):
            drv, h = _Driver(), _handle()
            ticket = drv.admit_port(h)
            start = threading.Barrier(2)
            got: dict[str, object] = {}

            def publish(start=start, got=got, ticket=ticket):
                start.wait()
                got["publish"] = ticket.publish()

            def close(start=start, got=got, drv=drv, h=h, ticket=ticket):
                start.wait()
                drv.withdraw_port(h)
                got["after_close"] = ticket.state  # what is true when close has returned

            # Alternate which side runs on the started thread, so both outcomes occur on either interpreter.
            other, mine = (publish, close) if n % 2 else (close, publish)
            t = threading.Thread(target=other)
            t.start()
            mine()
            t.join()
            after_close = got["after_close"]
            self.assertIn(after_close, (d.PUBLISHED, d.WITHDRAWN))
            self.assertEqual(got["publish"], ticket.state == d.PUBLISHED)  # never both, never neither
            self.assertEqual(after_close, ticket.state)  # nothing changes after close returned
            outcomes[ticket.state] += 1
        self.assertEqual(sum(outcomes.values()), RACE_ROUNDS)
        print(f"\nport withdraw race: {RACE_ROUNDS} rounds, {outcomes}", flush=True)

    def test_a_ticket_ends_once(self) -> None:
        t = d.PortTicket()
        self.assertEqual((t.withdraw(), t.publish(), t.withdraw(), t.state), (True, False, False, d.WITHDRAWN))
        t = d.PortTicket()
        self.assertEqual((t.publish(), t.withdraw(), t.publish(), t.state), (True, False, False, d.PUBLISHED))


if __name__ == "__main__":
    unittest.main()
