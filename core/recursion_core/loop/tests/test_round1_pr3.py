"""#423's first round (astra R1-R12, sol R1-R10, DeepSeek F1-F10), kept as tests: a journal cut off by a crash is
read, appended to and read again; the results the reader put back together are journaled; the user's text and
its ledger entry are one record; the journal file is a private plain file of this user, opened without
following a link; a damaged complete line or a part of the wrong shape refuses the resume, and the writers
refuse what the reader would; compaction keeps the roles alternating, fits the window or refuses, and keeps
the user's words once; the summary call has no tool, a request under the ceiling, and a typed limit ends the
turn; a resumed conversation passes the current secret policy; a published id can be resumed at once."""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import unittest
from unittest import mock

from recursion_core.loop import compaction as c
from recursion_core.loop import store as st
from recursion_core.loop.engine import Config, Session, UsageTotal
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import POSIX, Lines, private_dir, registry, workspace
from recursion_core.loop.tests.test_process import CORE
from recursion_core.loop.tools.workspace import ToolError

U = lambda text: b.Message("user", (b.TextPart(text),))  # noqa: E731
A = lambda text: b.Message("assistant", (b.TextPart(text),))  # noqa: E731
CALL = b.ToolCall("call1", "read", {"path": "a.txt"}, '{"path":"a.txt"}')
LINUX = sys.platform.startswith("linux")


def seeded(case, messages=(U("go"), b.Message("assistant", (CALL,))), result=None) -> st.ConversationStore:
    s = st.ConversationStore(private_dir(case), "s")
    for m in messages:
        if m.role == "user" and len(m.parts) == 1 and isinstance(m.parts[0], b.TextPart):
            s.user(f"turn-{len(m.parts[0].text)}", m.parts[0].text)
        else:
            s.message(m)
    if result is not None:
        s.result(result)
    s.close()
    return s


def session(case, script, store=None, loaded=None, secrets=(), **config):
    lines = Lines(list(secrets))
    provider = FakeProvider(script)
    s = Session(provider, registry(), workspace(case, {"a.txt": "x\n"}), lines.writer, lines.redactor,
                Config(model="m", retry_delays=(), **config), store=store, loaded=loaded)
    return s, provider, lines


def size(s: Session, request: b.TurnRequest) -> int:
    return c.estimate(request.messages) + len(request.system) // c.CHARS_PER_TOKEN + c.estimate_tools(request.tools)


def alternates(messages) -> bool:
    roles = [m.role for m in messages]
    return all(x != y for x, y in zip(roles, roles[1:]))


class TornTail(unittest.TestCase):  # astra R1, sol R5
    def test_a_journal_cut_off_by_a_crash_is_appended_to_and_read_again(self) -> None:
        for torn in (b'{"k":"result","par', b'{"k":"message","role":"assis', b'{"k":"user","turn":"x","text":"\xe2\x82'):
            with self.subTest(torn=torn):
                s = seeded(self)
                with open(s.path, "ab") as fh:
                    fh.write(torn)
                loaded = s.load()
                self.assertEqual(loaded.repaired, ["call1"])
                sess, _, _ = session(self, [{"text": "checked"}], store=s, loaded=loaded)
                sess.run_turn("turn-2", "what happened?", b.CancelToken())
                s.close()
                again = s.load()
                self.assertEqual(again.messages[-1], A("checked"))
                data = s.path.read_bytes()
                self.assertTrue(data.endswith(b"\n"))
                for line in data.splitlines():
                    json.loads(line)  # the cut-off record is gone: every line is a whole record

    def test_a_complete_line_that_is_damaged_refuses_even_at_the_end(self) -> None:
        for bad in (b"{bad json}\n", b"BROKEN\n", b"\xff\n", b'\xff\n{"k":"user","turn":"t","text":"after"}\n',
                    b'{"k":"user","turn":"t","text":"\xe2\x82"}\n'):
            with self.subTest(bad=bad):
                s = seeded(self)
                with open(s.path, "ab") as fh:
                    fh.write(bad)
                with self.assertRaises(st.StoreError):
                    s.load()


class Reconstructed(unittest.TestCase):  # astra R2; sol R9 (a result of an earlier call of the same id)
    def test_results_the_reader_put_together_survive_the_next_resume(self) -> None:
        recorded = b.ToolResult("call1", "recorded result", False)
        s = seeded(self, result=recorded)
        loaded = s.load()
        self.assertEqual((loaded.messages[-1].parts, loaded.repaired, loaded.reconstructed), ((recorded,), [], True))
        sess, _, _ = session(self, [{"text": "checked"}], store=s, loaded=loaded)
        sess.run_turn("turn-2", "continue", b.CancelToken())
        s.close()
        again = s.load()
        self.assertIn(recorded, [p for m in again.messages for p in m.parts])
        self.assertFalse(again.reconstructed)

    def test_an_earlier_result_never_settles_a_later_call_of_the_same_id(self) -> None:
        s = st.ConversationStore(private_dir(self), "j")
        call = b.ToolCall("same", "read", {"path": "a"}, "")
        old = b.ToolResult("same", "old result")
        s.user("t1", "first")
        s.message(b.Message("assistant", (call,)))
        s.result(old)
        s.message(b.Message("user", (old,)))
        s.message(A("finished"))
        s.user("t2", "second")
        s.message(b.Message("assistant", (call,)))
        s.close()
        got = s.load()
        self.assertEqual(got.repaired, ["same"])
        self.assertEqual(got.messages[-1].parts[0].text, st.UNKNOWN_OUTCOME)


WORDS = "Never change the acceptance tests."
WORKER = r'''
import sys, threading
from pathlib import Path
from recursion_core.loop import store as st
from recursion_core.loop.engine import Config, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, registry
from recursion_core.loop.tools.workspace import SpillStore, Workspace
home, ws = Path(sys.argv[1]), Path(sys.argv[2])
s = st.ConversationStore(home, "killed")
real, n = s._write, [0]
def write(record):
    n[0] += 1
    if n[0] == 2:
        print("AT_BOUNDARY", flush=True)
        threading.Event().wait(30)
        raise SystemExit(3)
    real(record)
s._write = write
lines = Lines()
Session(FakeProvider([{"text": "ok"}]), registry(), Workspace(ws, SpillStore(home)), lines.writer, lines.redactor,
        Config(model="m"), store=s).run_turn("turn-1", sys.argv[3], b.CancelToken())
'''


class UserRecord(unittest.TestCase):  # astra R3, sol R1
    def test_no_prefix_of_the_journal_holds_the_words_without_their_ledger_entry(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "full")
        sess, _, _ = session(self, [{"tool_calls": [{"id": "c1", "name": "read", "input": {"path": "a.txt"}}]},
                                    {"text": "done"}], store=s)
        sess.run_turn("turn-1", WORDS, b.CancelToken())
        s.close()
        records = s.path.read_bytes().splitlines(keepends=True)
        for k in range(len(records) + 1):
            with self.subTest(records=k):
                part = st.ConversationStore(home, f"p{k}")
                part.create()
                part.close()
                part.path.write_bytes(b"".join(records[:k]))
                loaded = part.load()
                in_messages = WORDS in [p.text for m in loaded.messages for p in m.parts if isinstance(p, b.TextPart)]
                self.assertEqual(in_messages, ("turn-1", WORDS) in loaded.user_messages)

    @unittest.skipUnless(LINUX, "a killed worker process (Linux)")
    def test_a_worker_killed_after_its_first_record_kept_the_words_and_their_entry(self) -> None:
        home, ws = private_dir(self), private_dir(self)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8",
               "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        proc = subprocess.Popen([sys.executable, "-B", "-c", WORKER, str(home), str(ws), WORDS],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 60)
            self.assertTrue(ready, "the worker did not reach its second record")
            self.assertEqual(proc.stdout.readline().strip(), b"AT_BOUNDARY")
            proc.kill()
            proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=30)
        loaded = st.ConversationStore(home, "killed").load()
        self.assertEqual(loaded.messages, [U(WORDS)])
        self.assertEqual(loaded.user_messages, [("turn-1", WORDS)])


@unittest.skipUnless(POSIX, "POSIX owners, modes and links")
class FilePrivacy(unittest.TestCase):  # astra R4, sol R4, DeepSeek F3; astra R12, sol R9 (the write side)
    def test_a_file_others_can_open_is_refused_for_reading_and_for_writing(self) -> None:
        s = seeded(self)
        os.chmod(s.path, 0o644)
        before = s.path.read_bytes()
        with self.assertRaises(st.StoreError):
            s.load()
        with self.assertRaises(st.StoreError):
            s.user("t", "private words")
        s.close()
        self.assertEqual(s.path.read_bytes(), before)

    def test_a_link_is_refused_for_reading_and_for_writing(self) -> None:
        s = seeded(self)
        target = s.folder.parent / "target.jsonl"
        s.path.rename(target)
        s.path.symlink_to(target)
        before = target.read_bytes()
        with self.assertRaises(st.StoreError):
            s.load()
        with self.assertRaises(st.StoreError):
            s.message(U("secret words"))
        s.close()
        self.assertEqual(target.read_bytes(), before)

    def test_something_other_than_a_plain_file_is_refused(self) -> None:
        s = st.ConversationStore(private_dir(self), "odd")
        s.folder.mkdir(mode=0o700)
        os.mkfifo(s.path, 0o600)
        with self.assertRaises(st.StoreError):
            s.load()
        with self.assertRaises(st.StoreError):
            s.user("t", "words")
        s.close()
        os.unlink(s.path)
        os.mkdir(s.path, 0o700)  # a folder in the journal's place (DeepSeek's probe p5, case 5)
        with self.assertRaises(st.StoreError):
            s.load()
        with self.assertRaises(st.StoreError):
            s.user("t", "words")
        s.close()

    def test_a_public_session_folder_is_refused_before_anything_is_written(self) -> None:
        home = private_dir(self)
        (home / "sessions").mkdir(mode=0o755)
        os.chmod(home / "sessions", 0o755)
        s = st.ConversationStore(home, "private")
        with self.assertRaises(ToolError):
            s.user("t", "private text")
        s.close()
        self.assertFalse(s.path.exists())

    def test_a_new_journal_is_private(self) -> None:
        s = st.ConversationStore(private_dir(self), "new")
        s.create()
        s.close()
        self.assertEqual(stat.S_IMODE(os.lstat(s.path).st_mode), 0o600)
        self.assertEqual(s.load().messages, [])


class Shapes(unittest.TestCase):  # astra R7, sol R6, DeepSeek F4, F7
    def test_a_part_or_record_of_the_wrong_shape_refuses(self) -> None:
        cases = [{"k": "message", "role": "assistant", "parts": [{"t": "opaque", "vendor": "anthropic", "data": []}]},
                 {"k": "message", "role": "assistant", "parts": [{"t": "opaque", "vendor": None, "data": 42}]},
                 {"k": "message", "role": "user", "parts": [{"t": "result", "id": "c", "text": "x", "error": "false"}]},
                 {"k": "message", "role": "user", "parts": [{"t": "result", "id": 42, "text": "x", "error": False}]},
                 {"k": "message", "role": "assistant",
                  "parts": [{"t": "call", "id": "c", "name": "read", "input": {}, "raw": False, "error": None}]},
                 {"k": "message", "role": "user",
                  "parts": [{"t": "call", "id": "c", "name": "read", "input": {}, "raw": "", "error": None}]},
                 {"k": "message", "role": "assistant", "parts": []},
                 {"k": "message", "role": "assistant", "parts": [{"t": "text", "text": "x", "extra": 1}]},
                 {"k": "user", "turn": "t", "text": "x", "extra": 1}]
        for rec in cases:
            with self.subTest(record=rec):
                s = seeded(self)
                with open(s.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
                with self.assertRaises(st.StoreError):
                    s.load()

    def test_token_totals_are_whole_numbers_and_a_cost_may_be_a_fraction(self) -> None:
        total = UsageTotal()
        total.add(b.Usage(input_tokens=b.observed(5), cost_usd=b.observed(0.25)), "m")
        s = seeded(self)
        s.totals(total.to_state())
        s.close()
        self.assertEqual(s.load().totals["sum"]["cost_usd"], 0.25)
        bad = total.to_state()
        bad["sum"]["input_tokens"] = 1.5
        with open(s.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"k": "totals", "state": bad}) + "\n")
        with self.assertRaises(st.StoreError):
            s.load()
        with self.assertRaises(ValueError):
            UsageTotal.from_state({"calls": 1})

    def test_the_writers_refuse_what_the_reader_would_refuse_and_write_nothing(self) -> None:
        s = seeded(self)
        before = s.path.read_bytes()
        for write in (lambda: s.totals({"calls": "many"}), lambda: s.message(U("x" + chr(0xD800))),
                      lambda: s.message(b.Message("assistant", (b.ToolCall("c", "read", {"n": float("nan")}, ""),))),
                      lambda: s.user("t", 5)):
            with self.subTest(write=write), self.assertRaises(st.StoreError):
                write()
        s.close()
        self.assertEqual(s.path.read_bytes(), before)


class Alternation(unittest.TestCase):  # astra R5, sol R2, DeepSeek F1, F8
    def test_compaction_alternates_whichever_role_the_tail_starts_with(self) -> None:
        summary = lambda prefix: b.TurnResponse((b.TextPart("summary"),), b.END_TURN, b.Usage())  # noqa: E731
        for msgs, ledger in (([U("first"), A("x" * 4000), U("second")], [("1", "first"), ("2", "second")]),
                             ([U("old"), A("x" * 80000), U("late"), A("tail" * 2000)], [("1", "old"), ("2", "late")]),
                             # a message of thinking blocks alone between two of the user's: dropped, they join
                             ([U("task"), A("x" * 4000), U("a"), b.Message("assistant", (b.OpaquePart(
                                 "anthropic", {"type": "thinking", "thinking": "", "signature": "s"}),)), U("b"),
                               A("ok")], [("1", "task"), ("2", "a"), ("3", "b")])):
            for keep in (1, 200, 3000):
                with self.subTest(n=len(msgs), keep=keep):
                    out = c.compact(msgs, ledger, summary, keep)
                    self.assertTrue(alternates(out.messages), [m.role for m in out.messages])
                    self.assertTrue(out.messages[0].parts[0].text.startswith(c.HEAD))

    def test_the_summary_request_alternates_and_carries_no_tool(self) -> None:
        sess, provider, _ = session(self, [{"tool_calls": [{"id": "c", "name": "read", "input": {"path": "a.txt"}}],
                                            "usage": {"input_tokens": 19000}}, {"text": "summary"}, {"text": "done"}],
                                    context_window=20000)
        sess.run_turn("1", "first", b.CancelToken())
        sess2, provider2, _ = session(self, [{"text": "a", "usage": {"input_tokens": 19000}}, {"text": "summary"},
                                             {"text": "done"}], context_window=20000, keep_tail_tokens=1)
        sess2.run_turn("1", "first", b.CancelToken())
        sess2.run_turn("2", "second", b.CancelToken())
        for req in (provider.requests[1], provider2.requests[1]):
            self.assertTrue(alternates(req.messages), [m.role for m in req.messages])
            self.assertEqual(req.tools, ())
            self.assertEqual(req.messages[-1].parts[-1].text, c.SUMMARY_PROMPT)
        for req in provider.requests + provider2.requests:
            self.assertTrue(alternates(req.messages))

    def test_the_tail_never_starts_at_a_result_once_thinking_is_dropped(self) -> None:
        thinking = b.OpaquePart("anthropic", {"type": "thinking", "thinking": "", "signature": "s"})
        msgs = [U("task"), b.Message("assistant", (b.ToolCall("c1", "read", {"path": "a"}, ""),)),
                b.Message("assistant", (thinking,)), b.Message("user", (b.ToolResult("c1", "x" * 4000),)), A("ok"),
                U("next"), A("y" * 4000)]
        for keep in range(1, 3000, 7):
            out = c.compact(msgs, [("1", "task"), ("2", "next")], lambda p: None, keep)
            seen = set()
            for m in out.messages:
                for part in m.parts:
                    if isinstance(part, b.ToolCall):
                        seen.add(part.call_id)
                    if isinstance(part, b.ToolResult):
                        self.assertIn(part.call_id, seen, f"keep={keep}")
            self.assertTrue(alternates(out.messages), f"keep={keep}")


class Ceiling(unittest.TestCase):  # astra R6, sol R3, DeepSeek F2
    def test_a_message_too_large_for_the_window_is_never_sent(self) -> None:
        sess, provider, lines = session(self, [{"text": "accepted"}], context_window=20000)
        self.assertEqual(sess.run_turn("1", "x" * 80000, b.CancelToken()), "failed")
        self.assertEqual(provider.requests, [])
        end = lines.of("turn_ended")[-1]
        self.assertEqual((end["reason"], end["result"]["code"]), ("context_full", "context_full"))

    def test_words_that_cannot_fit_are_refused_never_dropped(self) -> None:
        for steps, messages, ledger in (([{"text": "s"}], [U("x" * 80000), A("y" * 80000)], [("t", "x" * 80000)]),
                                        ([{"text": "summary"}, {"text": "done"}], [U("x" * 80000), A("past")],
                                         [("old", "x" * 80000)])):
            with self.subTest(n=len(steps)):
                sess, provider, lines = session(self, steps, context_window=20000, keep_tail_tokens=1)
                sess.messages, sess.user_messages = list(messages), list(ledger)
                with self.assertRaises(c.ContextFull):
                    sess._maybe_compact("t", b.CancelToken(), UsageTotal())
                self.assertEqual(sess.run_turn("new", "next", b.CancelToken()), "failed")
                self.assertEqual(provider.requests, [])
                self.assertIn("x" * 80000, [x for _, x in sess.user_messages])

    def test_every_request_fits_the_ceiling_the_summary_call_included(self) -> None:
        sess, provider, lines = session(self, [{"text": "summary"}, {"text": "done"}], context_window=20000)
        sess.messages, sess.user_messages = [U("first"), A("x" * 68000)], [("old", "first")]
        self.assertEqual(sess.run_turn("new", "next", b.CancelToken()), "candidate_done")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([lines.of("compaction")[0]["summary"]], ["model"])
        for req in provider.requests:
            self.assertLessEqual(size(sess, req), sess._ceiling)

    def test_a_long_tool_session_is_brought_under_the_ceiling(self) -> None:
        sess, provider, lines = session(self, [{"text": "short summary"}], context_window=20000)
        sess.messages, sess.user_messages = [U("task")], [("turn-1", "task")]
        for n in range(8):
            sess.messages += [b.Message("assistant", (b.ToolCall(str(n), "read", {"path": "a"}, ""),)),
                              b.Message("user", (b.ToolResult(str(n), "z" * 18000),))]
        sess._maybe_compact("turn-1", b.CancelToken(), UsageTotal())
        self.assertLessEqual(c.estimate(sess.messages) + sess._overhead(), sess._ceiling)
        self.assertLessEqual(size(sess, provider.requests[0]), sess._ceiling)

    def test_a_reply_that_joins_the_last_message_counts_toward_the_ceiling(self) -> None:
        sess, provider, lines = session(self, [
            {"text": "first", "stop_reason": b.PAUSE, "usage": {"input_tokens": 16900}},
            {"text": "x" * 8000, "stop_reason": b.PAUSE, "usage": {"input_tokens": 16900}},
            {"text": "summary"}, {"text": "done"}], context_window=20000, keep_tail_tokens=100)
        sess.run_turn("turn-1", "task", b.CancelToken())
        self.assertTrue(lines.of("compaction"))

    def test_the_loops_own_estimate_counts_when_the_providers_count_is_lower(self) -> None:
        sess, provider, lines = session(self, [{"text": "summary"}, {"text": "done"}], context_window=20000)
        sess.messages, sess.user_messages = [U("first"), A("x" * 68000)], [("old", "first")]
        sess._prompt_tokens, sess._counted = 100, c.estimate(sess.messages)
        sess.run_turn("new", "next", b.CancelToken())
        self.assertTrue(lines.of("compaction"))
        for req in provider.requests:
            self.assertLessEqual(size(sess, req), sess._ceiling)

    def test_too_large_with_nothing_to_compact_is_refused(self) -> None:
        sess, provider, lines = session(self, [{"text": "never"}], context_window=20000)
        sess.messages = [b.Message("user", (b.TextPart("go"), b.TextPart("[harness] " + "f" * 80000)))]
        sess.user_messages = [("t", "go")]
        with self.assertRaises(c.ContextFull):
            sess._maybe_compact("t", b.CancelToken(), UsageTotal())

    def test_a_summary_is_cut_to_the_room_left(self) -> None:
        msgs = [U("task"), A("w" * 1990), U("next")]
        long = lambda prefix: b.TurnResponse((b.TextPart("y" * 20000),), b.END_TURN, b.Usage())  # noqa: E731
        out = c.compact(msgs, [("1", "task"), ("2", "next")], long, keep_tokens=1, room=300)
        self.assertLessEqual(c.estimate(out.messages), 300)
        self.assertEqual((out.summary_source, out.reason), ("fallback", "the summary did not fit the window"))
        self.assertIn("[the rest of the summary is cut]", out.messages[0].parts[0].text)
        self.assertIn("next", [p.text for p in out.messages[0].parts])

    def test_a_parsed_tool_input_counts_in_the_estimate(self) -> None:
        msg = b.Message("assistant", (b.ToolCall("c", "read", {"path": "a", "payload": "x" * 80000}),))
        self.assertGreaterEqual(c.estimate([msg]), 20000)

    def test_repeated_long_messages_never_send_a_request_over_the_ceiling(self) -> None:
        script = []
        for n in range(5):
            script += [{"text": f"reply {n}", "usage": {"input_tokens": 19_000}}, {"text": f"summary {n}"}]
        sess, provider, lines = session(self, script, context_window=20000, keep_tail_tokens=2000)
        outcomes = [sess.run_turn(f"turn-{n}", f"rule {n}: " + "w" * 12000, b.CancelToken()) for n in range(5)]
        for req in provider.requests:
            self.assertLessEqual(size(sess, req), sess._ceiling)
        self.assertTrue(set(outcomes) <= {"candidate_done", "failed"}, outcomes)  # failed: context_full
        flat = "\n".join(p.text for m in sess.messages for p in m.parts if isinstance(p, b.TextPart))
        for _, text in sess.user_messages[:2]:
            self.assertEqual(flat.count(text), 1)

    def test_a_message_still_in_the_tail_is_not_repeated_in_the_head(self) -> None:
        msgs = [U("alpha rule"), A("x" * 4000), U("gamma rule")]
        out = c.compact(msgs, [("1", "alpha rule"), ("2", "gamma rule")], lambda p: None, keep_tokens=10)
        flat = "\n".join(p.text for m in out.messages for p in m.parts if isinstance(p, b.TextPart))
        self.assertEqual((flat.count("alpha rule"), flat.count("gamma rule")), (1, 1))


class Ledger(unittest.TestCase):  # DeepSeek F5
    def test_an_empty_ledger_beside_the_users_text_is_refused(self) -> None:
        with self.assertRaises(c.LedgerMismatch):
            c.compact([U("never delete tests/"), A("x" * 4000), U("next")], [], lambda p: None, keep_tokens=1)

    def test_a_kernel_that_lost_a_message_is_caught(self) -> None:
        out = c.compact([U("one"), A("x" * 4000), U("two")], [("1", "one"), ("2", "two")], lambda p: None, 1)
        head = out.messages[0]
        broken = [b.Message("user", (b.TextPart(head.parts[0].text.replace("one", "on")),) + head.parts[1:])]
        with self.assertRaises(c.LedgerMismatch):
            c._check_verbatim(broken + out.messages[1:], [("1", "one"), ("2", "two")])


class Summary(unittest.TestCase):  # astra R9, sol R7
    def test_a_typed_limit_on_the_summary_call_pauses_the_turn_with_no_further_call(self) -> None:
        sess, provider, lines = session(self, [{"text": "x" * 4000, "usage": {"input_tokens": 19000}},
                                               b.LimitError("synthetic 429", retry_after_s=123),
                                               {"text": "never"}], context_window=20000, keep_tail_tokens=100)
        sess.run_turn("turn-1", "first", b.CancelToken())
        self.assertEqual(sess.run_turn("turn-2", "second", b.CancelToken()), "paused")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(lines.of("limit")[-1]["retry_after_s"], 123)
        self.assertEqual([u.get("purpose") for u in lines.of("usage") if u["span"] == "call"], [None, "compaction"])


class ResumePolicy(unittest.TestCase):  # astra R8, DeepSeek F9
    SECRET = "SYNTHETIC-RESUME-SECRET-0000"

    def test_a_secret_of_this_run_in_the_loaded_history_is_not_sent(self) -> None:
        sec = self.SECRET
        call = b.ToolCall("call1", "read", {"path": sec}, json.dumps({"path": sec}))
        s = seeded(self, (U(f"remember {sec}"), b.Message("assistant", (b.TextPart(f"noted {sec}"), call))),
                   result=b.ToolResult("call1", f"read {sec}"))
        total = UsageTotal()
        total.add(b.Usage(), sec)
        with open(s.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"k": "totals", "state": total.to_state()}) + "\n")
        sess, provider, lines = session(self, [{"text": "done"}], store=s, loaded=s.load(), secrets=[sec])
        sess.run_turn("new", "continue", b.CancelToken())
        s.close()
        self.assertNotIn(sec, repr(provider.requests[0].messages))
        self.assertNotIn(sec, repr(sess.user_messages))
        self.assertNotIn(sec, lines.raw())
        self.assertNotIn(sec, json.dumps(sess.session_usage.to_state()))

    def test_a_vendor_block_holding_a_secret_of_this_run_refuses_the_resume(self) -> None:
        s = seeded(self, (U("go"), b.Message("assistant", (b.OpaquePart("anthropic", {"signature": self.SECRET}),
                                                           b.TextPart("ok")))))
        with self.assertRaises(st.StoreError):
            session(self, [], store=s, loaded=s.load(), secrets=[self.SECRET])

    def test_a_conversation_loaded_from_another_session_is_refused(self) -> None:
        s = seeded(self)
        other = st.ConversationStore(private_dir(self), "other")
        with self.assertRaises(st.StoreError):
            session(self, [], store=other, loaded=s.load())


class NotRetried(unittest.TestCase):  # DeepSeek F10
    def test_a_failure_of_a_kind_never_retried_is_not_retried_whatever_its_class_says(self) -> None:
        for base in (b.AuthError, b.BillingError, b.RequestError, b.ProtocolError, b.LimitError):
            kind = type("Retryable" + base.__name__, (base,), {"retryable": True})
            with self.subTest(kind=base.__name__):
                sess, provider, lines = session(self, [kind("synthetic"), {"text": "never"}])
                sess.config.retry_delays = (0, 0)
                sess.run_turn("t", "go", b.CancelToken())
                self.assertEqual((len(provider.requests), lines.of("retry")), (1, []))


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class Process(unittest.TestCase):  # astra R11, sol R10; astra R12 (the CLI's scrub of model metadata)
    def run_engine(self, home, ws, script, extra=(), inputs=({"kind": "close"},), env=None):
        path = ws.parent / f"{ws.name}-script-{os.urandom(4).hex()}.json"
        path.write_text(json.dumps(script), encoding="utf-8")
        self.addCleanup(path.unlink)
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8"}
        proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.loop", "--provider", "fake",
                               "--fake-script", str(path), "--home", str(home), "--no-project-instructions", *extra],
                              input="".join(json.dumps(i) + "\n" for i in inputs).encode(), capture_output=True,
                              env=dict(base, **(env or {})), cwd=ws, check=False)
        return [json.loads(x) for x in proc.stdout.decode().splitlines()], proc.returncode, proc.stderr.decode()

    def test_the_id_on_the_ready_line_can_be_resumed_before_any_turn(self) -> None:
        home, ws = private_dir(self), private_dir(self)
        docs, code, err = self.run_engine(home, ws, [])
        self.assertEqual(code, 0, err)
        sid = docs[0]["engine_session_id"]
        self.assertEqual(stat.S_IMODE(os.lstat(home / "sessions" / f"{sid}.jsonl").st_mode), 0o600)
        docs, code, err = self.run_engine(home, ws, [], ["--resume", sid])
        self.assertEqual(code, 0, err)
        self.assertEqual((docs[0]["engine_session_id"], docs[0]["resumed"]), (sid, True))

    def test_the_served_model_name_is_scrubbed_on_its_way_to_the_journal(self) -> None:
        home, ws = private_dir(self), private_dir(self)
        synthetic = "SYNTHETIC-MODEL-SECRET-0000"
        msg = {"kind": "user_message", "turn_id": "turn-1", "delivery_id": "dlv-1", "text": "go"}
        docs, code, err = self.run_engine(home, ws, [{"text": "done", "model": synthetic}],
                                          inputs=(msg, {"kind": "close"}), env={"ANTHROPIC_API_KEY": synthetic})
        self.assertEqual(code, 0, err)
        files = list((home / "sessions").glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(synthetic, files[0].read_text(encoding="utf-8"))


class Sync(unittest.TestCase):  # astra R12, sol R9
    def test_sync_reaches_the_disk(self) -> None:
        s = st.ConversationStore(private_dir(self), "j")
        s.user("t", "data")
        try:
            with mock.patch.object(st.os, "fsync") as fsync:
                s.sync()
                fsync.assert_called_once_with(s._fh.fileno())
        finally:
            s.close()

    @unittest.skipUnless(LINUX, "/proc/self/fd")
    def test_the_journal_is_synced_before_the_turn_ends(self) -> None:
        s = st.ConversationStore(private_dir(self), "sync")
        lines = Lines()
        synced: list[str] = []
        real = os.fsync

        def fsync(fd):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
            return real(fd)

        ended = []
        lines.buffer.on_line = lambda doc: ended.append(str(s.path) in synced) if doc["type"] == "turn_ended" else None
        sess = Session(FakeProvider([{"text": "done"}]), registry(), workspace(self), lines.writer, lines.redactor,
                       Config(model="m"), store=s)
        try:
            with mock.patch("os.fsync", side_effect=fsync):
                sess.run_turn("t", "go", b.CancelToken())
        finally:
            s.close()
        self.assertEqual(ended, [True])


if __name__ == "__main__":
    unittest.main()
