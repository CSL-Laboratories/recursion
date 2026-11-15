"""Rules the merge of PR 1's reviewed head into PR 2 adds where the two meet: an interrupt that comes as a turn's
end is written does not replace ``done`` (the acceptance check had passed); the turn's and the session's usage
totals are written inside that end, before ``turn_ended``, and count a failed call too."""

from __future__ import annotations

import unittest

from recursion_core.loop import done as d
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import POSIX, Lines, registry, workspace
from recursion_core.loop.tests.test_edges import _interrupt, _message


def _session(case, steps, acceptance=None):
    lines = Lines()
    session = Session(FakeProvider(steps), registry(), workspace(case, {"a.txt": "x\n"}), lines.writer,
                      lines.redactor, Config(model="fake-model", acceptance=acceptance,
                                                             retry_delays=()))
    return session, lines


@unittest.skipUnless(POSIX, "the acceptance check runs a shell command")
class DoneIsKept(unittest.TestCase):
    def test_an_interrupt_read_as_the_end_of_an_accepted_turn_is_written_leaves_it_done(self) -> None:
        session, lines = _session(self, [{"text": "finished"}], d.Acceptance("true"))
        engine = Engine(session, lines.writer, log=lambda m: None)
        actual = engine._ending

        def end(write):
            if not lines.of("turn_ended"):
                engine.feed_line(_interrupt(1))
            actual(write)

        session.ending = end
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d_["outcome"] for d_ in lines.of("turn_ended")], ["done"])


class Totals(unittest.TestCase):
    def test_a_failed_call_counts_in_the_totals_written_before_the_end(self) -> None:
        failure = b.ServerError("synthetic failure", usage=b.Usage(input_tokens=b.observed(11)))
        session, lines = _session(self, [failure])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
        kinds = [(x["type"], x.get("span")) for x in lines.all()]
        self.assertEqual(kinds, [("usage", "call"), ("usage", "turn"), ("usage", "session_total"),
                                 ("turn_ended", None)])
        turn = lines.of("usage")[1]
        self.assertEqual(turn["input_tokens"], b.observed(11).to_wire())
        self.assertEqual(turn["output_tokens"], b.unknown().to_wire())


if __name__ == "__main__":
    unittest.main()
