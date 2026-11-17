"""Round 8 of draft #379 (DeepSeek NOT YET, Grok's three P2).

A mapping that is not a dict is a tool input the registry accepts, and JSON can write it once it is a plain
dict. A finite integer retry time is kept, however large. A cyclic container is walked once: the call does
not run, and the walk ends.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from collections import UserDict
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider, step_from_json
from recursion_core.loop.tests.support import Lines, registry, workspace
from recursion_core.loop.tests.test_edges import SECRET, _message

HUGE = 10 ** 400
ALMOST = 10 ** 309


def _open(case, steps, files=None):
    provider = FakeProvider(steps)
    lines = Lines()
    session = Session(provider, registry(), workspace(case, files or {"a.txt": "x\n"}), lines.writer,
                      lines.redactor, Config("fake-model"))
    return session, provider, lines


def _serve(session, lines, turns: int = 1) -> int:
    engine = Engine(session, lines.writer, log=lambda message: None)
    for n in range(1, turns + 1):
        engine.feed_line(_message(n))
    engine.input_ended()
    return engine.serve()


class _RaisingMap(Mapping):
    """A mapping whose walk raises. The tool must not run."""

    def __getitem__(self, key):
        raise RuntimeError("synthetic")

    def __iter__(self):
        raise RuntimeError("synthetic")

    def __len__(self) -> int:
        raise RuntimeError("synthetic")


class _ReplyThenCancel(FakeProvider):
    """The reply is in hand and the turn is then cancelled, before any tool runs."""

    def stream_turn(self, request, *, on_event=None, cancel=None):
        self.requests.append(request)
        if cancel is not None:
            cancel.cancel()
        step = self._script.pop(0)
        return step if isinstance(step, b.TurnResponse) else step_from_json(step)


class _LimitThenCancel(FakeProvider):
    """A limit whose retry time is in hand, and the turn is already cancelled."""

    def __init__(self, retry) -> None:
        super().__init__([])
        self._retry = retry

    def stream_turn(self, request, *, on_event=None, cancel=None):
        self.requests.append(request)
        if cancel is not None:
            cancel.cancel()
        raise b.LimitError("ordinary limit", retry_after_s=self._retry)


class MappingInputs(unittest.TestCase):
    """Grok R1 and DeepSeek F-R8-3. A MappingProxyType or a UserDict is normalised to plain dicts, nested
    ones included, and the tool runs. NaN, an infinity and an integer JSON cannot write stay refused."""

    def test_a_mapping_proxy_and_a_user_dict_run_the_read(self) -> None:
        for kind, raw in (("proxy", MappingProxyType({"path": "a.txt"})),
                          ("user", UserDict({"path": "a.txt"}))):
            with self.subTest(kind=kind):
                reply = b.TurnResponse((b.ToolCall("c1", "read", raw),), b.TOOL_USE, b.Usage())
                session, provider, lines = _open(self, [reply, {"text": "done"}])
                kept = session._kept((b.ToolCall("c1", "read", raw),))
                self.assertEqual(kept[0].input, {"path": "a.txt"})
                self.assertIsNone(kept[0].input_error)
                self.assertIs(type(kept[0].input), dict)
                self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
                self.assertEqual(lines.of("tool_call")[0]["input"], {"path": "a.txt"})
                self.assertEqual(lines.of("tool_result")[0]["status"], "ok")
                self.assertIn("x", lines.of("tool_result")[0]["text"])

    def test_a_nested_mapping_becomes_plain_dicts(self) -> None:
        raw = {"metadata": [MappingProxyType({"label": "ordinary"}), UserDict({"label": "ordinary"})]}
        session, provider, lines = _open(self, [])
        (kept,) = session._kept((b.ToolCall("c1", "read", raw),))
        self.assertEqual(kept.input, {"metadata": [{"label": "ordinary"}, {"label": "ordinary"}]})
        self.assertIs(type(kept.input["metadata"][0]), dict)
        self.assertIs(type(kept.input["metadata"][1]), dict)
        self.assertIsNone(kept.input_error)

    def test_a_second_caller_keeps_its_own_copy(self) -> None:
        first_raw = UserDict({"path": "a.txt"})
        second_raw = UserDict({"path": "b.txt"})
        session, provider, lines = _open(self, [], {"a.txt": "x\n", "b.txt": "y\n"})
        (first,) = session._kept((b.ToolCall("c1", "read", first_raw),))
        first_raw["path"] = "changed"
        other, other_provider, other_lines = _open(self, [
            b.TurnResponse((b.ToolCall("c2", "read", second_raw),), b.TOOL_USE, b.Usage()),
            {"text": "done"},
        ], {"a.txt": "x\n", "b.txt": "y\n"})
        self.assertEqual(other.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
        self.assertEqual(first.input, {"path": "a.txt"})
        self.assertEqual(other_lines.of("tool_call")[0]["input"], {"path": "b.txt"})
        self.assertEqual(other_lines.of("tool_result")[0]["status"], "ok")
        self.assertIn("y", other_lines.of("tool_result")[0]["text"])

    def test_nan_infinity_and_a_huge_integer_in_a_mapping_stay_refused(self) -> None:
        samples = (
            MappingProxyType({"n": float("nan")}),
            UserDict({"n": float("inf")}),
            MappingProxyType({"n": 10 ** 5000}),
            {"metadata": [UserDict({"n": float("-inf")})]},
        )
        session, provider, lines = _open(self, [])
        for raw in samples:
            with self.subTest(raw=type(raw).__name__):
                (kept,) = session._kept((b.ToolCall("c1", "read", raw),))
                self.assertIsNone(kept.input)
                self.assertEqual(kept.input_error, b.INPUT_NOT_FINITE)

    def test_a_mapping_that_raises_does_not_run_the_tool(self) -> None:
        reply = b.TurnResponse((b.ToolCall("c1", "read", _RaisingMap()),), b.TOOL_USE, b.Usage())
        session, provider, lines = _open(self, [reply, {"text": "done"}])
        with self.assertRaises(RuntimeError):
            session.run_turn("turn-1", "go", b.CancelToken())
        self.assertEqual(lines.of("tool_result"), [])

    def test_a_cancelled_turn_does_not_run_a_mapping_input(self) -> None:
        raw = MappingProxyType({"path": "a.txt"})
        reply = b.TurnResponse((b.ToolCall("c1", "read", raw),), b.TOOL_USE, b.Usage())
        provider = _ReplyThenCancel([reply])
        lines = Lines()
        session = Session(provider, registry(), workspace(self, {"a.txt": "x\n"}), lines.writer,
                          lines.redactor, Config("fake-model"))
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "interrupted")
        self.assertEqual(lines.of("tool_call")[0]["input"], {"path": "a.txt"})
        result = lines.of("tool_result")[0]
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("\tx", result["text"])

    def test_an_opaque_mapping_stays_a_reply_and_is_sent_as_a_dict(self) -> None:
        data = MappingProxyType({"type": "redacted_thinking", "data": "ordinary"})
        reply = b.TurnResponse((b.OpaquePart("anthropic", data),), b.END_TURN, b.Usage())
        session, provider, lines = _open(self, [reply])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
        body = a.AnthropicProvider(SECRET).build_body(
            b.TurnRequest("fake-model", "", tuple(session.messages)))
        blocks = [block for message in body["messages"] for block in message["content"]]
        self.assertIn({"type": "redacted_thinking", "data": "ordinary"}, blocks)
        sent = next(block for block in blocks if block.get("type") == "redacted_thinking")
        self.assertIs(type(sent), dict)


class FiniteRetry(unittest.TestCase):
    """Grok R2 and DeepSeek F-R8-2. A finite integer past the float range pauses the turn and is kept on the
    limit line. NaN, the infinities, a bool and a non-number stay null. The turn does not fail inside the loop."""

    def _limit(self, retry, *, turns: int = 1, cancel: bool = False):
        step = b.LimitError("ordinary limit", retry_after_s=retry)
        script = [step, {"text": "next"}] if turns > 1 else [step]
        session, provider, lines = _open(self, script)
        if cancel:
            provider = _LimitThenCancel(retry)
            lines = Lines()
            session = Session(provider, registry(), workspace(self), lines.writer, lines.redactor,
                              Config("fake-model"))
        self.assertEqual(_serve(session, lines, turns), 0)
        return lines

    def test_a_huge_finite_integer_pauses_with_that_retry_time(self) -> None:
        for retry in (HUGE, ALMOST, -(10 ** 400), 2 ** 63):
            with self.subTest(retry=retry):
                lines = self._limit(retry)
                ended = lines.of("turn_ended")[0]
                self.assertEqual((ended["outcome"], ended["reason"]), ("paused", "limit"))
                self.assertEqual(len(lines.of("limit")), 1)
                self.assertEqual(lines.of("limit")[0]["retry_after_s"], retry)
                self.assertNotIn("loop_error", lines.raw())

    def test_a_cancelled_huge_retry_still_writes_the_limit_line(self) -> None:
        lines = self._limit(HUGE, cancel=True)
        self.assertEqual(len(lines.of("limit")), 1)
        self.assertEqual(lines.of("limit")[0]["retry_after_s"], HUGE)
        ended = lines.of("turn_ended")[0]
        self.assertEqual(ended["reason"], "limit")
        self.assertIn(ended["outcome"], ("paused", "interrupted"))
        self.assertNotEqual(ended["outcome"], "failed")

    def test_nan_infinity_bool_and_text_stay_null(self) -> None:
        for retry in (float("nan"), float("inf"), float("-inf"), True, False, "5", None):
            with self.subTest(retry=repr(retry)):
                lines = self._limit(retry)
                self.assertEqual(lines.of("turn_ended")[0]["reason"], "limit")
                self.assertIsNone(lines.of("limit")[0]["retry_after_s"])

    def test_a_second_turn_keeps_its_own_retry_time(self) -> None:
        session, provider, lines = _open(self, [
            b.LimitError("ordinary limit", retry_after_s=HUGE),
            b.LimitError("ordinary limit", retry_after_s=float("nan")),
        ])
        self.assertEqual(_serve(session, lines, 2), 0)
        limits = lines.of("limit")
        self.assertEqual([row["retry_after_s"] for row in limits], [HUGE, None])
        self.assertEqual([row["reason"] for row in lines.of("turn_ended")], ["limit", "limit"])


class CyclicInput(unittest.TestCase):
    """Grok R3 and DeepSeek F-R8-1. holds_unencodable and holds_nonfinite stop on a container they have
    already seen. A cyclic tool input does not run the tool, and nests_deeper still reports it."""

    def test_a_cycle_ends_and_the_tool_does_not_run(self) -> None:
        script = textwrap.dedent(
            """\
            import json, os, tempfile, threading, time
            from pathlib import Path

            def _stop():
                time.sleep(5)
                os._exit(3)

            from recursion_core.loop.engine import Config, Session
            from recursion_core.loop.providers import base as b
            from recursion_core.loop.providers.fake import FakeProvider
            from recursion_core.loop.tests.support import Lines, registry
            from recursion_core.loop.tools.workspace import SpillStore, Workspace

            threading.Thread(target=_stop, daemon=True).start()
            started = time.monotonic()
            cycle = {}
            cycle["k"] = cycle
            listed = []
            listed.append(listed)
            deep = b.nests_deeper(cycle, b.MAX_INPUT_DEPTH)
            held = b.holds_unencodable(cycle)
            held_list = b.holds_unencodable(listed)
            nonfinite = b.holds_nonfinite(cycle)
            marked = {}
            marked["k"] = marked
            marked["s"] = "\\ud800"
            held_marked = b.holds_unencodable(marked)
            root = Path(tempfile.mkdtemp())
            (root / "a.txt").write_bytes(b"x\\n")
            home = Path(tempfile.mkdtemp())
            lines = Lines()
            call = b.ToolCall("c1", "read", cycle)
            reply = b.TurnResponse((call,), b.TOOL_USE, b.Usage())
            done = {"text": "done"}
            session = Session(FakeProvider([reply, done]), registry(), Workspace(root, SpillStore(home)),
                              lines.writer, lines.redactor, Config("fake-model"))
            outcome = session.run_turn("turn-1", "go", b.CancelToken())
            tool = lines.of("tool_result")
            print(json.dumps({
                "deep": deep,
                "held": held,
                "held_list": held_list,
                "nonfinite": nonfinite,
                "held_marked": held_marked,
                "outcome": outcome,
                "status": tool[0]["status"] if tool else None,
                "text": tool[0]["text"] if tool else "",
                "dt": time.monotonic() - started,
            }))
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = "core" + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[4],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:] + proc.stdout[-500:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(got["deep"])
        self.assertFalse(got["held"])
        self.assertFalse(got["held_list"])
        self.assertFalse(got["nonfinite"])
        self.assertTrue(got["held_marked"])
        self.assertEqual(got["outcome"], "candidate_done")
        self.assertEqual(got["status"], "error")
        self.assertIn(b.INPUT_TOO_DEEP, got["text"])
        self.assertLess(got["dt"], 4.0)
        self.assertNotIn("x", got["text"])


class RunTurnDoc(unittest.TestCase):
    """DeepSeek F-R8-5. run_turn's docstring names the ValueError it raises for text UTF-8 cannot encode."""

    def test_the_docstring_names_the_value_error(self) -> None:
        text = Session.run_turn.__doc__ or ""
        self.assertIn("ValueError", text)
        self.assertIn("UTF-8", text)


if __name__ == "__main__":
    unittest.main()
