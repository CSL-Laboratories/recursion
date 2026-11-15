"""Compaction: the ceiling, the cut, the verbatim user messages, the summary's rejection, and the loop using it;
plus the bounded retry of an overloaded or failed provider call."""

from __future__ import annotations

import json
import threading
import unittest

from recursion_core.loop import compaction as c
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, registry, workspace

U = lambda text: b.Message("user", (b.TextPart(text),))  # noqa: E731
A = lambda text: b.Message("assistant", (b.TextPart(text),))  # noqa: E731


def call(i: int, path: str = "a.py") -> b.Message:
    return b.Message("assistant", (b.OpaquePart("anthropic", {"type": "thinking", "thinking": "", "signature": "s"}),
                                   b.ToolCall(f"toolu_{i}", "read", {"path": path}, "")))


def result(i: int, size: int = 4000) -> b.Message:
    return b.Message("user", (b.ToolResult(f"toolu_{i}", "x" * size),))


def reply(text: str, stop: str = b.END_TURN, tools: bool = False) -> b.TurnResponse:
    parts = (b.TextPart(text),) + ((b.ToolCall("toolu_s", "read", {"path": "a"}, ""),) if tools else ())
    return b.TurnResponse(parts, stop, b.Usage())


class Ceiling(unittest.TestCase):
    def test_the_reserve_and_the_small_window_rule(self) -> None:
        self.assertEqual(c.ceiling(200_000), 170_000)
        self.assertEqual(c.ceiling(1_000_000), 850_000)
        self.assertEqual(c.ceiling(32_768), 16_384, "16k reserve from 32k up")
        self.assertEqual(c.ceiling(20_000), 17_000, "below 32k only the 15% share: never negative")
        with self.assertRaises(c.WindowTooSmall):
            c.ceiling(4_096)


class Cut(unittest.TestCase):
    def convo(self) -> list[b.Message]:
        return [U("task one"), call(1), result(1), call(2), result(2), A("done one"),
                U("task two"), call(3), result(3), call(4), result(4), A("done two")]

    def test_the_tail_never_starts_at_a_tool_result(self) -> None:
        msgs = self.convo()
        starts = set()
        for keep in range(1, 5000, 3):  # every tail size, including those just between a call and its result
            i = c.split(msgs, keep)
            starts.add(i)
            self.assertFalse(any(isinstance(p, b.ToolResult) for p in msgs[i].parts), f"keep={keep}: cut at {i}")
        self.assertGreater(len(starts), 3, "the sweep reaches several cut points")

    def test_every_kept_result_keeps_its_call(self) -> None:
        msgs = self.convo()
        out = c.compact(msgs, [("turn-1", "task one"), ("turn-2", "task two")], lambda prefix: reply("summary"),
                        keep_tokens=2500)
        calls_seen: set[str] = set()
        for m in out.messages:
            for part in m.parts:
                if isinstance(part, b.ToolCall):
                    calls_seen.add(part.call_id)
                if isinstance(part, b.ToolResult):
                    self.assertIn(part.call_id, calls_seen, "a result whose call was cut away")
        self.assertEqual(out.messages[0].role, "user")
        self.assertFalse(any(isinstance(p, b.OpaquePart) for m in out.messages for p in m.parts),
                         "thinking blocks bound to the old conversation are dropped")
        self.assertTrue(any(isinstance(p, b.ToolCall) for m in out.messages for p in m.parts),
                        "tool calls in the tail stay")

    def test_nothing_to_replace(self) -> None:
        self.assertIsNone(c.compact([U("only")], [("turn-1", "only")], lambda p: reply("s")))


class Verbatim(unittest.TestCase):
    def test_a_late_constraint_survives_three_compactions_word_for_word(self) -> None:
        late = "From now on: never edit anything under tests/, and keep line length at 99."
        ledger: list[tuple[str, str]] = []
        msgs: list[b.Message] = []
        dropped = lambda prefix: reply("The work so far. (This summary leaves the constraint out.)")  # noqa: E731
        for n in range(1, 5):
            text = late if n == 2 else f"task {n}"
            ledger.append((f"turn-{n}", text))
            msgs += [U(text), call(n), result(n, 20_000), A(f"done {n}")]
            if n >= 2:
                out = c.compact(msgs, ledger, dropped, keep_tokens=3000)
                msgs = out.messages
        flat = "\n".join(p.text for m in msgs for p in m.parts if isinstance(p, b.TextPart))
        self.assertIn(late, flat)
        self.assertEqual(flat.count(late), 1, "re-injected once from the ledger, not piled up")
        for _, text in ledger:
            self.assertIn(text, flat)


class Summary(unittest.TestCase):
    def test_a_bad_summary_falls_back_to_the_record(self) -> None:
        msgs = [U("task"), call(1, "src/app.py"), result(1), A("I read the app."), U("next"), A("ok")]
        bad = {
            "failed": None, "refused": reply("no", b.REFUSAL), "cut": reply("half a", b.MAX_TOKENS),
            "tool": reply("let me look", b.TOOL_USE, tools=True), "empty": reply("   "),
            "long": reply("y" * (c.SUMMARY_MAX_CHARS + 1)),
        }
        for name, response in bad.items():
            with self.subTest(name=name):
                out = c.compact(msgs, [("turn-1", "task"), ("turn-2", "next")], lambda p, r=response: r,
                                keep_tokens=1)
                self.assertEqual(out.summary_source, "fallback")
                head = out.messages[0].parts[0].text
                self.assertIn("- read src/app.py", head)
                self.assertIn("I read the app.", head)
        good = c.compact(msgs, [("turn-1", "task"), ("turn-2", "next")], lambda p: reply("Read app.py."),
                         keep_tokens=1)
        self.assertEqual(good.summary_source, "model")
        self.assertIn("Read app.py.", good.messages[0].parts[0].text)


def run_engine(case, script, **config):
    ws = workspace(case, {"a.py": "print(1)\n" * 50})
    provider = FakeProvider(script)
    lines = Lines()
    session = Session(provider, registry(), ws, lines.writer, lines.redactor, Config(model="fake-model", **config))
    engine = Engine(session, lines.writer, log=lambda m: None)
    t = threading.Thread(target=engine.serve)
    t.start()
    return engine, t, lines, provider, session


def feed(engine, turn: str, text: str) -> None:
    engine.feed_line(json.dumps({"kind": "user_message", "turn_id": turn, "text": text,
                                 "delivery_id": f"dlv-{turn[5:]}"}).encode())


class InTheLoop(unittest.TestCase):
    def test_the_loop_compacts_near_the_ceiling_and_keeps_the_user_word_for_word(self) -> None:
        big = {"input_tokens": 16_900, "output_tokens": 10}  # each call reports a prompt just under the ceiling
        script = []
        for n in range(3):
            script += [{"tool_calls": [{"id": f"toolu_{n}", "name": "read", "input": {"path": "a.py"}}],
                        "usage": big},
                       {"text": f"summary {n}"},  # the compaction call
                       {"text": f"done {n}", "usage": big}]
        engine, t, lines, provider, session = run_engine(self, script, context_window=20_000, keep_tail_tokens=200)
        feed(engine, "turn-1", "task one")
        feed(engine, "turn-2", "Late rule: never touch tests/.")
        feed(engine, "turn-3", "task three")
        engine.feed_line(b'{"kind":"close"}')
        t.join()
        docs = lines.all()
        comps = [d for d in docs if d["type"] == "compaction"]
        self.assertGreaterEqual(len(comps), 2, docs)
        self.assertTrue(all(d["summary"] == "model" for d in comps))
        last = provider.requests[-1]
        flat = "\n".join(p.text for m in last.messages for p in m.parts if isinstance(p, b.TextPart))
        self.assertIn("Late rule: never touch tests/.", flat)
        summary_calls = [r for r in provider.requests if r.messages[-1].parts
                         and isinstance(r.messages[-1].parts[0], b.TextPart)
                         and r.messages[-1].parts[0].text == c.SUMMARY_PROMPT]
        self.assertEqual(len(summary_calls), len(comps))
        self.assertEqual([d["outcome"] for d in docs if d["type"] == "turn_ended"], ["candidate_done"] * 3)

    def test_no_window_no_compaction(self) -> None:
        engine, t, lines, provider, _ = run_engine(self, [{"text": "ok", "usage": {"input_tokens": 999_999}}])
        feed(engine, "turn-1", "hi")
        engine.feed_line(b'{"kind":"close"}')
        t.join()
        self.assertNotIn("compaction", [d["type"] for d in lines.all()])


class Retry(unittest.TestCase):
    def test_an_overload_is_retried_and_then_given_up(self) -> None:
        engine, t, lines, provider, _ = run_engine(self, [
            {"error": {"kind": "overload", "detail": "HTTP 529"}}, {"text": "ok"},
            {"error": {"kind": "overload", "detail": "HTTP 529"}}, {"error": {"kind": "server", "detail": "HTTP 500"}},
            {"error": {"kind": "transport", "detail": "reset"}},
        ], retry_delays=(0.0, 0.0))
        feed(engine, "turn-1", "a")
        feed(engine, "turn-2", "b")
        engine.feed_line(b'{"kind":"close"}')
        t.join()
        docs = lines.all()
        retries = [(d["turn_id"], d["attempt"], d["code"]) for d in docs if d["type"] == "retry"]
        self.assertEqual(retries, [("turn-1", 1, "provider_overloaded"), ("turn-2", 1, "provider_overloaded"),
                                   ("turn-2", 2, "provider_server")])
        ends = [(d["turn_id"], d["outcome"], (d["result"] or {}).get("code")) for d in docs
                if d["type"] == "turn_ended"]
        self.assertEqual(ends, [("turn-1", "candidate_done", None), ("turn-2", "failed", "provider_transport")])

    def test_a_limit_and_an_auth_failure_are_not_retried(self) -> None:
        engine, t, lines, provider, _ = run_engine(self, [
            {"error": {"kind": "limit", "detail": "HTTP 429", "retry_after_s": 1}},
            {"error": {"kind": "auth", "detail": "HTTP 401"}}, {"text": "never"},
        ], retry_delays=(0.0, 0.0))
        feed(engine, "turn-1", "a")
        feed(engine, "turn-2", "b")
        engine.feed_line(b'{"kind":"close"}')
        t.join()
        docs = lines.all()
        self.assertEqual([d for d in docs if d["type"] == "retry"], [])
        self.assertEqual(len(provider.requests), 2)

    def test_an_interrupt_during_the_wait_ends_the_turn(self) -> None:
        engine, t, lines, provider, _ = run_engine(self, [{"error": {"kind": "overload", "detail": "HTTP 529"}},
                                                          {"text": "never"}], retry_delays=(3600.0,))
        feed(engine, "turn-1", "a")
        while not lines.of("retry"):
            threading.Event().wait(0.005)
        engine.feed_line(b'{"kind":"interrupt","turn_id":"turn-1"}')
        engine.feed_line(b'{"kind":"close"}')
        t.join()
        self.assertEqual([d["outcome"] for d in lines.all() if d["type"] == "turn_ended"], ["interrupted"])
        self.assertEqual(len(provider.requests), 1)


if __name__ == "__main__":
    unittest.main()
