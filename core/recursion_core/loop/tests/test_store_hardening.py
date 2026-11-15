"""Where PR 3 (compaction, retries, resume) meets #379's rules, kept as tests: the journal of a conversation the
engine joined reads back joined; a journal holding what no request could carry, or a record of the wrong shape,
refuses the resume (a line cut at the end is still ignored); a compaction summary passes the checks any reply
does; every failed attempt of a retried call has its usage line and a wait cut short has none; a retry wait or
context window the loop cannot use is refused."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from recursion_core.loop import store as st
from recursion_core.loop.engine import Config, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, registry, workspace

BS = chr(92)
SEED = ['{"k":"message","role":"user","parts":[{"t":"text","text":"go"}]}',
        '{"k":"message","role":"assistant","parts":[{"t":"call","id":"c1","name":"read","input":{"path":"a"}}]}']


def _home(case) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="loop-store-"))
    os.chmod(folder, 0o700)
    case.addCleanup(shutil.rmtree, folder, True)
    return folder


def _journal(case, lines: list[str]) -> st.ConversationStore:
    home = _home(case)
    s = st.ConversationStore(home, "j")
    s.message(b.Message("user", (b.TextPart("seed"),)))
    s.close()
    s.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return st.ConversationStore(home, "j")


def _session(case, steps, **config):
    lines = Lines()
    session = Session(FakeProvider(steps), registry(), workspace(case, {"a.txt": "x\n"}), lines.writer,
                      lines.redactor, Config(model="m", **config))
    return session, lines


class JournalReadsBackJoined(unittest.TestCase):
    def test_a_turn_that_ends_with_no_reply_resumes_alternating(self) -> None:
        for step in (b.Cancelled(), b.ServerError("synthetic"), b.LimitError("synthetic", retry_after_s=1)):
            with self.subTest(step=type(step).__name__):
                home = _home(self)
                s1 = st.ConversationStore(home, "a")
                lines = Lines()
                Session(FakeProvider([step]), registry(), workspace(self), lines.writer, lines.redactor,
                        Config(model="m", retry_delays=()), store=s1).run_turn("turn-1", "go", b.CancelToken())
                s1.close()
                loaded = st.ConversationStore(home, "a").load()
                self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
                s2 = st.ConversationStore(home, "a")
                Session(FakeProvider([{"text": "fine"}]), registry(), workspace(self), lines.writer, lines.redactor,
                        Config(model="m"), store=s2, loaded=loaded).run_turn("turn-2", "again", b.CancelToken())
                s2.close()
                again = st.ConversationStore(home, "a").load().messages
                self.assertEqual([m.role for m in again], ["user", "assistant", "user", "assistant"])

    def test_two_journaled_messages_of_one_role_are_one_message(self) -> None:
        s = _journal(self, SEED[:1] + ['{"k":"message","role":"user","parts":[{"t":"text","text":"more"}]}'])
        msgs = s.load().messages
        self.assertEqual([m.role for m in msgs], ["user"])
        self.assertEqual(msgs[0].parts, (b.TextPart("go"), b.TextPart("more")))


class DamagedJournalsRefuseTheResume(unittest.TestCase):
    def test_what_no_request_could_carry_or_a_wrong_shape_is_a_store_error(self) -> None:
        cases = {
            "lone surrogate": '{"k":"message","role":"assistant","parts":[{"t":"text","text":"' + BS + 'ud800"}]}',
            "NaN": '{"k":"user","turn":"t","text":"x","n":NaN}',
            "1e400": '{"k":"user","turn":"t","text":"x","n":1e400}',
            "not an object": "5",
            "user text a number": '{"k":"user","turn":"t","text":5}',
            "call input a list": ('{"k":"message","role":"assistant","parts":[{"t":"call","id":"c2","name":"read",'
                                  '"input":[1]}]}'),
            "role invalid": '{"k":"message","role":"system","parts":[]}',
            "totals of the wrong shape": '{"k":"totals","state":{"calls":"many"}}',
        }
        for name, bad in cases.items():
            with self.subTest(case=name), self.assertRaises(st.StoreError):
                _journal(self, SEED + [bad, '{"k":"user","turn":"t","text":"after"}']).load()

    def test_a_line_cut_at_the_end_is_ignored_and_the_call_gets_an_unknown_outcome(self) -> None:
        loaded = _journal(self, SEED + ['{"k":"result","part":{"t":"result","id":"c1","te']).load()
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant", "user"])
        self.assertEqual(loaded.repaired, ["c1"])

    def test_totals_the_engine_wrote_read_back(self) -> None:
        session, lines = _session(self, [{"text": "fine"}])
        session.run_turn("turn-1", "go", b.CancelToken())
        s = _journal(self, SEED[:1])
        s.totals(session.session_usage.to_state())
        s.close()
        self.assertEqual(s.load().totals["calls"], 1)


class SummaryIsAReply(unittest.TestCase):
    def test_a_summary_holding_a_lone_surrogate_is_not_kept(self) -> None:
        big = {"text": "x" * 4000, "usage": {"input_tokens": 19_000}}
        summary = b.TurnResponse((b.TextPart("summary " + chr(0xD800)),), b.END_TURN, b.Usage())
        session, lines = _session(self, [big, summary, {"text": "after"}], context_window=20_000,
                                  keep_tail_tokens=200)
        session.run_turn("turn-1", "first", b.CancelToken())
        session.run_turn("turn-2", "second", b.CancelToken())
        self.assertEqual([c["summary"] for c in lines.of("compaction")], ["fallback"])
        texts = [getattr(part, "text", "") for m in session.messages for part in m.parts]
        self.assertFalse(b.holds_unencodable(texts))
        lines.raw().encode("utf-8")


class LedgerIsScrubbed(unittest.TestCase):
    def test_the_users_words_compaction_puts_back_hold_no_secret(self) -> None:
        secret = "synthetic-ledger-secret-0000"
        lines = Lines([secret])
        big = {"text": "x" * 4000, "usage": {"input_tokens": 19_000}}
        provider = FakeProvider([big, {"text": "summary"}, {"text": "after"}])
        session = Session(provider, registry(), workspace(self, {"a.txt": "x\n"}), lines.writer, lines.redactor,
                          Config(model="m", context_window=20_000, keep_tail_tokens=200))
        session.run_turn("turn-1", "my key is " + secret, b.CancelToken())
        session.run_turn("turn-2", "second", b.CancelToken())
        self.assertEqual(len(lines.of("compaction")), 1)
        self.assertNotIn(secret, repr(session.user_messages))
        self.assertNotIn(secret, repr(provider.requests[-1].messages))


class RetriedCallUsage(unittest.TestCase):
    def test_every_failed_attempt_has_its_usage_line(self) -> None:
        failure = b.ServerError("synthetic", usage=b.Usage(input_tokens=b.observed(5)))
        session, lines = _session(self, [failure, failure, {"text": "fine"}], retry_delays=(0, 0))
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
        calls = [u for u in lines.of("usage") if u["span"] == "call"]
        self.assertEqual([u["input_tokens"]["value"] for u in calls[:2]], [5, 5])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(lines.of("retry")), 2)

    def test_a_wait_cut_short_by_an_interrupt_makes_no_call_and_no_usage_line(self) -> None:
        cancel = b.CancelToken()

        def fail(request):
            cancel.cancel()
            raise b.ServerError("synthetic")

        session, lines = _session(self, [fail], retry_delays=(60.0,))
        self.assertEqual(session.run_turn("turn-1", "go", cancel), "interrupted")
        self.assertEqual(len([u for u in lines.of("usage") if u["span"] == "call"]), 1)


class ConfigRefusals(unittest.TestCase):
    def test_a_retry_wait_or_window_the_loop_cannot_use_is_refused(self) -> None:
        for bad in ((float("nan"),), (float("inf"),), (-1.0,), (True,), ("1",)):
            with self.subTest(retry_delays=repr(bad)), self.assertRaises(ValueError):
                Config(model="m", retry_delays=bad)
        for bad in (0, -5, 1.5, True, "20000"):
            with self.subTest(context_window=repr(bad)), self.assertRaises(ValueError):
                Config(model="m", context_window=bad)
        Config(model="m", retry_delays=(0, 2.5), context_window=200_000)


if __name__ == "__main__":
    unittest.main()
