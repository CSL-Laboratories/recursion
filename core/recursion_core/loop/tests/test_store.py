"""The conversation store: what a resumed engine reads back, and what it does with a call that never finished."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest

from recursion_core.loop import protocol as p
from recursion_core.loop import store as st
from recursion_core.loop.engine import Config, Session, UsageTotal
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import FAKE_KEY, POSIX, Lines, private_dir, registry, workspace
from recursion_core.loop.tests.test_process import CORE

CALL_A = b.ToolCall("toolu_a", "write", {"path": "a.txt", "content": "x"}, '{"path":"a.txt","content":"x"}')
CALL_B = b.ToolCall("toolu_b", "shell", {"command": "make"}, '{"command":"make"}')


class RoundTrip(unittest.TestCase):
    def test_every_part_comes_back(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "abc123")
        msgs = [b.Message("user", (b.TextPart("hi"),)),
                b.Message("assistant", (b.OpaquePart("anthropic", {"type": "thinking", "thinking": "",
                                                                   "signature": "sig"}),
                                        b.TextPart("on it"), CALL_A,
                                        b.ToolCall("toolu_x", "read", None, '{"pa', "input is not valid JSON"))),
                b.Message("user", (b.ToolResult("toolu_a", "created", False), b.ToolResult("toolu_x", "bad", True)))]
        s.user("turn-1", "hi")  # the user's text: its message and its ledger entry, one record (#423 round 1)
        for m in msgs[1:]:
            s.message(m)
        total = UsageTotal()
        total.add(b.Usage(input_tokens=b.observed(5)), "m1")
        s.totals(total.to_state())
        s.close()
        got = st.ConversationStore(home, "abc123").load()
        self.assertEqual(got.messages, msgs)
        self.assertEqual(got.user_messages, [("turn-1", "hi")])
        self.assertEqual(UsageTotal.from_state(got.totals).to_wire(), total.to_wire())
        self.assertEqual(got.repaired, [])

    def test_a_compaction_replaces_what_came_before(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "c1")
        s.message(b.Message("user", (b.TextPart("old"),)))
        s.replace([b.Message("user", (b.TextPart("summary"),))])
        s.message(b.Message("assistant", (b.TextPart("next"),)))
        s.close()
        self.assertEqual([m.parts[0].text for m in st.ConversationStore(home, "c1").load().messages],
                         ["summary", "next"])


class Crash(unittest.TestCase):
    def test_a_call_that_never_reported_has_an_unknown_outcome(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "crash1")
        s.message(b.Message("user", (b.TextPart("go"),)))
        s.message(b.Message("assistant", (CALL_A, CALL_B)))
        s.result(b.ToolResult("toolu_a", "created a.txt", False))  # then the engine stopped
        s.close()
        got = st.ConversationStore(home, "crash1").load()
        last = got.messages[-1]
        self.assertEqual(last.role, "user")
        self.assertEqual(last.parts, (b.ToolResult("toolu_a", "created a.txt", False),
                                      b.ToolResult("toolu_b", st.UNKNOWN_OUTCOME, True)))
        self.assertEqual(got.repaired, ["toolu_b"])

    def test_a_line_cut_off_at_the_end_is_ignored_and_damage_elsewhere_refuses(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "torn")
        s.message(b.Message("user", (b.TextPart("go"),)))
        s.close()
        with open(s.path, "a", encoding="utf-8") as fh:
            fh.write('{"k":"message","role":"assis')
        self.assertEqual(len(st.ConversationStore(home, "torn").load().messages), 1)
        with open(s.path, "a", encoding="utf-8") as fh:
            fh.write('\n{"k":"user","turn":"turn-2","text":"x"}\n')
        with self.assertRaises(st.StoreError):
            st.ConversationStore(home, "torn").load()

    def test_unknown_ids_and_bad_ids(self) -> None:
        home = private_dir(self)
        with self.assertRaises(st.StoreError):
            st.ConversationStore(home, "nothing-here").load()
        for bad in ("", "../x", "a/b", "x" * 129, "a\nb"):
            with self.subTest(bad=bad), self.assertRaises(st.StoreError):
                st.ConversationStore(home, bad)


class Private(unittest.TestCase):
    def test_modes_and_no_key_on_disk(self) -> None:
        home = private_dir(self)
        red = p.Redactor([FAKE_KEY])
        s = st.ConversationStore(home, "keys", red.scrub_obj)
        s.message(b.Message("user", (b.TextPart(f"my key is {FAKE_KEY}"),)))
        s.close()
        self.assertNotIn(FAKE_KEY, s.path.read_text(encoding="utf-8"))
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(s.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(s.folder).st_mode), 0o700)


class SessionResume(unittest.TestCase):
    def test_a_session_journals_and_a_resumed_one_continues(self) -> None:
        ws = workspace(self, {"a.py": "print(1)\n"})
        home = private_dir(self)
        lines = Lines()
        s1 = st.ConversationStore(home, "sess1")
        provider = FakeProvider([{"tool_calls": [{"id": "toolu_r", "name": "read", "input": {"path": "a.py"}}],
                                  "usage": {"input_tokens": 10}},
                                 {"text": "it prints 1", "usage": {"input_tokens": 12}}])
        Session(provider, registry(), ws, lines.writer, lines.redactor, Config(model="m"),
                store=s1).run_turn("turn-1", "what does a.py do?", b.CancelToken())
        s1.close()
        loaded = st.ConversationStore(home, "sess1").load()
        provider2 = FakeProvider([{"text": "yes", "usage": {"input_tokens": 30}}])
        lines2 = Lines()
        s2 = st.ConversationStore(home, "sess1")
        Session(provider2, registry(), ws, lines2.writer, lines2.redactor, Config(model="m"), store=s2,
                loaded=loaded).run_turn("turn-2", "and then?", b.CancelToken())
        s2.close()
        sent = provider2.requests[0].messages
        self.assertEqual([m.role for m in sent], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(sent[0].parts[0].text, "what does a.py do?")
        total = [x for x in lines2.all() if x["type"] == "usage" and x["span"] == "session_total"][0]
        self.assertEqual(total["input_tokens"]["value"], 52, "the session's running total carries over")
        self.assertEqual(st.ConversationStore(home, "sess1").load().user_messages,
                         [("turn-1", "what does a.py do?"), ("turn-2", "and then?")])


class Journal(unittest.TestCase):
    def test_each_result_is_journaled_before_the_results_message(self) -> None:
        ws = workspace(self, {"a.py": "1\n", "b.py": "2\n"})
        home = private_dir(self)
        lines = Lines()
        s = st.ConversationStore(home, "j1")
        provider = FakeProvider([{"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "a.py"}},
                                                 {"id": "toolu_2", "name": "read", "input": {"path": "b.py"}}]},
                                 {"text": "done"}])
        Session(provider, registry(), ws, lines.writer, lines.redactor, Config(model="m"), store=s).run_turn(
            "turn-1", "read both", b.CancelToken())
        s.close()
        kinds = [json.loads(x)["k"] for x in s.path.read_text().splitlines()]
        # The user's text is one record, its message and its ledger entry together (#423 round 1, astra R3).
        self.assertEqual(kinds, ["user", "message", "result", "result", "message", "message", "totals"])

    def test_a_repaired_conversation_stays_valid_after_the_next_turn(self) -> None:
        ws = workspace(self)
        home = private_dir(self)
        s = st.ConversationStore(home, "rep")
        s.message(b.Message("user", (b.TextPart("go"),)))
        s.message(b.Message("assistant", (CALL_B,)))  # the engine stopped while `make` ran
        s.close()
        lines = Lines()
        s2 = st.ConversationStore(home, "rep")
        Session(FakeProvider([{"text": "I will check what make did."}]), registry(), ws, lines.writer,
                lines.redactor, Config(model="m"), store=s2, loaded=s2.load()).run_turn(
            "turn-2", "what happened?", b.CancelToken())
        s2.close()
        msgs = st.ConversationStore(home, "rep").load().messages
        # The repaired results and the next user message are one user message: the roles alternate (#379).
        self.assertEqual([m.role for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual(msgs[2].parts, (b.ToolResult("toolu_b", st.UNKNOWN_OUTCOME, True),
                                         b.TextPart("what happened?")))

    def test_a_compacted_conversation_is_what_resumes(self) -> None:
        ws = workspace(self, {"a.py": "print(1)\n" * 50})
        home = private_dir(self)
        lines = Lines()
        s = st.ConversationStore(home, "comp")
        big = {"input_tokens": 16_900}
        provider = FakeProvider([{"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "a.py"}}],
                                  "usage": big}, {"text": "the summary"}, {"text": "done", "usage": big}])
        Session(provider, registry(), ws, lines.writer, lines.redactor,
                Config(model="m", context_window=20_000, keep_tail_tokens=200), store=s).run_turn(
            "turn-1", "keep it short", b.CancelToken())
        s.close()
        self.assertIn("compaction", [x["type"] for x in lines.all()])
        msgs = st.ConversationStore(home, "comp").load().messages
        self.assertTrue(msgs[0].parts[0].text.startswith("[harness] The earlier part of this conversation"))
        self.assertIn("keep it short", msgs[0].parts[0].text)

    @unittest.skipUnless(POSIX, "POSIX permission bits")
    def test_a_store_others_can_open_is_refused(self) -> None:
        home = private_dir(self)
        s = st.ConversationStore(home, "open")
        s.message(b.Message("user", (b.TextPart("go"),)))
        s.close()
        os.chmod(s.folder, 0o755)
        with self.assertRaises(Exception):
            st.ConversationStore(home, "open").load()


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class ProcessResume(unittest.TestCase):
    def run_engine(self, ws, home, script, extra, inputs):
        tag = os.urandom(4).hex()
        path = ws.parent / f"{ws.name}-script-{tag}.json"
        path.write_text(json.dumps(script), encoding="utf-8")
        self.addCleanup(path.unlink)
        record = ws.parent / f"{ws.name}-record-{tag}.jsonl"
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8"}
        proc = subprocess.run([sys.executable, "-B", "-m", "recursion_core.loop", "--provider", "fake",
                               "--fake-script", str(path), "--fake-record", str(record), "--home", str(home),
                               *extra], input="".join(json.dumps(i) + "\n" for i in inputs).encode(),
                              capture_output=True, env=env, cwd=ws, check=False)
        docs = [json.loads(x) for x in proc.stdout.decode().splitlines()]
        recorded = [json.loads(x) for x in record.read_text().splitlines()] if record.exists() else []
        return docs, proc.returncode, proc.stderr.decode(), recorded

    def test_resume_by_the_id_the_ready_line_named(self) -> None:
        ws = private_dir(self, "loop-ws-")
        home = private_dir(self, "loop-home-")
        msg = {"kind": "user_message", "turn_id": "turn-1", "text": "remember: blue", "delivery_id": "dlv-1"}
        docs, code, err, _ = self.run_engine(ws, home, [{"text": "noted"}], [], [msg, {"kind": "close"}])
        self.assertEqual(code, 0, err)
        sid = docs[0]["engine_session_id"]
        self.assertRegex(sid, r"^[0-9a-f]{32}$")
        self.assertIs(docs[0]["resumed"], False)
        msg2 = dict(msg, turn_id="turn-2", text="what colour?", delivery_id="dlv-2")
        docs, code, err, recorded = self.run_engine(ws, home, [{"text": "blue"}], ["--resume", sid],
                                                    [msg2, {"kind": "close"}])
        self.assertEqual(code, 0, err)
        self.assertEqual((docs[0]["engine_session_id"], docs[0]["resumed"]), (sid, True))
        texts = [part.get("text") for m in recorded[0]["messages"] for part in m["parts"]]
        self.assertEqual(texts, ["remember: blue", "noted", "what colour?"])
        docs, code, err, _ = self.run_engine(ws, home, [], ["--resume", "0" * 32], [])
        self.assertNotEqual(code, 0)
        self.assertIn("no conversation is stored", err)


if __name__ == "__main__":
    unittest.main()
