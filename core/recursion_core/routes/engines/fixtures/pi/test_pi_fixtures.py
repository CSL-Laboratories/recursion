# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.
"""The synthetic fixtures, replayed through the parser, give the drafts written beside them; every draft
has driver.v2's ``event_draft`` shape as SPINE's own reader reads the published schemas; every tool
input's digest is the one SPINE's ``conformance/rules.py`` computes.

The fixtures are SYNTHETIC (README.md beside them). When Lucas's attended run is captured, its redacted
stdout goes beside them under a name without ``synthetic-`` and is replayed the same way.

Run by dotted name: PYTHONPATH=core <python> -m unittest recursion_core.routes.engines.fixtures.pi.test_pi_fixtures
The schema and digest checks read ``schemas/control/v1`` and ``conformance/`` (SPINE's, from #368, merged
into this branch); where those are absent they are skipped, and the skip names why.
"""

import importlib.util
import json
import unittest
from pathlib import Path

from recursion_core.routes.engines import pi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]

# name: (mode, connection id, the argv prompt of a one-shot run)
FIXTURES = {
    "synthetic-json-basic": ("json", "subscription:pi-chatgpt", "Say hello in one word."),
    "synthetic-json-tools": ("json", "subscription:pi-chatgpt", "List the files and read notes.txt."),
    "synthetic-json-error-quota": ("json", "api_key:openai-key-1", "Summarise the repository."),
    "synthetic-rpc-two-turns": ("rpc", "api_key:openai-key-1", None),
    "synthetic-rpc-steer-abort": ("rpc", "subscription:pi-chatgpt", None),
    "synthetic-rpc-abort-before-steer": ("rpc", "api_key:openai-key-1", None),
}

# event.v1's payload keys for each type a driver may draft (every key required, no other allowed).
DRAFT_KEYS = {
    "engine.output": {"stream", "text", "offset_start", "offset_end"},
    "tool.call": {"call_id", "name", "input_digest", "input"},
    "tool.result": {"call_id", "status", "text", "output_ref", "truncated"},
    "usage": {"connection_id", "model", "bucket", "span", "input_tokens", "output_tokens", "reasoning_tokens",
              "cache_read_tokens", "cache_write_tokens", "cost_usd"},
    "limit": {"connection_id", "scope", "kind", "model", "bucket", "reset_at"},
    "result": {"code", "detail"},
    "input.accepted": {"delivery_id", "turn_id", "how"},
}


def data_file(name: str) -> Path:
    mode = FIXTURES[name][0]
    return HERE / (f"{name}.stdout.jsonl" if mode == "json" else f"{name}.transcript.jsonl")


def replay(name: str):
    """``(stream, drafts)`` for one fixture. An RPC transcript's lines to Pi are made again with the
    stream's own writers and must be the lines recorded; its lines from Pi are fed in order."""
    mode, connection, prompt = FIXTURES[name]
    stream = pi.PiStream(mode, connection_id=connection)
    drafts = []
    records = pi.records_of(data_file(name).read_bytes())
    if mode == "json":
        stream.argv_prompt(prompt, delivery_id="dlv-1", turn_id="turn-1")
        for line in records:
            drafts += stream.feed(line)
        return stream, drafts
    for line in records:
        step = json.loads(line)
        if step.get("to") == "pi":
            record = step["record"]
            if record["type"] == "prompt":
                _token, written = stream.prompt(record["message"], **step["delivery"])
            else:
                _token, written = stream.command(record["type"])
            if json.loads(written) != record:
                raise AssertionError(f"{name}: the stream wrote {written!r}, the transcript has {record!r}")
        else:
            drafts += stream.feed(json.dumps(step["record"]))
    return stream, drafts


def expected(name: str) -> dict:
    return json.loads((HERE / f"{name}.expected.json").read_text(encoding="utf-8"))


def spine_rules():
    """SPINE's conformance/rules.py, loaded by path (it is not a package of the core), or None."""
    path = ROOT / "conformance" / "rules.py"
    if not path.is_file() or not (ROOT / "schemas" / "control" / "v1" / "driver.v2.json").is_file():
        return None
    spec = importlib.util.spec_from_file_location("engine_e8_spine_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RULES = spine_rules()
NO_RULES = "conformance/rules.py and schemas/control/v1 (SPINE's #368) are not in this tree"


class ReplayTest(unittest.TestCase):
    def test_each_fixture_replays_to_its_expected_drafts_signals_and_session(self):
        for name in FIXTURES:
            with self.subTest(name=name):
                stream, drafts = replay(name)
                want = expected(name)
                self.assertEqual(drafts, want["drafts"])
                self.assertEqual(stream.signals, want["signals"])
                self.assertEqual(stream.engine_session_id, want["engine_session_id"])
                self.assertEqual(stream.pending_deliveries(), [])
                self.assertEqual(stream.ignored, {})

    def test_every_payload_has_exactly_event_v1s_keys(self):
        for name in FIXTURES:
            for draft in replay(name)[1]:
                with self.subTest(name=name, type=draft["type"]):
                    self.assertEqual(set(draft), {"type", "payload"})
                    self.assertEqual(set(draft["payload"]), DRAFT_KEYS[draft["type"]])

    def test_the_one_shot_fixtures_end_as_expected(self):
        results = {name: [d for d in replay(name)[1] if d["type"] == "result"] for name in FIXTURES}
        self.assertEqual(results["synthetic-json-basic"], [{"type": "result", "payload": {"code": None, "detail": ""}}])
        self.assertEqual(results["synthetic-json-tools"], [{"type": "result", "payload": {"code": None, "detail": ""}}])
        self.assertEqual(results["synthetic-json-error-quota"][0]["payload"]["code"], "short_fault")
        self.assertEqual(results["synthetic-rpc-two-turns"], [])
        self.assertEqual(results["synthetic-rpc-steer-abort"], [])
        self.assertEqual(results["synthetic-rpc-abort-before-steer"], [])

    def test_a_steer_queued_before_an_abort_is_delivered_by_the_next_run(self):
        # Pi's abort keeps its steering queue (agent.ts abort; agent-loop.ts returns on an aborted reply
        # before it polls); the next prompt's run polls it first. The fixture shows the steer accepted,
        # counted while it waits, kept through the abort, and replayed after the next prompt's message.
        name = "synthetic-rpc-abort-before-steer"
        steps = [json.loads(line) for line in pi.records_of(data_file(name).read_bytes())]
        counts = [s["record"]["data"]["pendingMessageCount"] for s in steps
                  if s.get("from") == "pi" and s["record"].get("command") == "get_state"]
        self.assertEqual(counts, [1, 1, 0])
        users = [s["record"]["message"]["content"] for s in steps if s.get("from") == "pi"
                 and s["record"]["type"] == "message_start" and s["record"]["message"].get("role") == "user"]
        self.assertEqual(users, ["Refactor the parser.", "Go on.", "Keep the old names."])
        _stream, drafts = replay(name)
        accepted = [(d["payload"]["delivery_id"], d["payload"]["how"]) for d in drafts if d["type"] == "input.accepted"]
        self.assertEqual(accepted, [("dlv-c1", "new_turn"), ("dlv-c2", "mid_turn"), ("dlv-c3", "new_turn")])


class SchemaTest(unittest.TestCase):
    @unittest.skipIf(RULES is None, NO_RULES)
    def test_every_draft_has_driver_v2s_event_draft_shape(self):
        for name in FIXTURES:
            for index, draft in enumerate(replay(name)[1]):
                with self.subTest(name=name, index=index, type=draft["type"]):
                    self.assertTrue(RULES.fits("driver.v2.json#/$defs/event_draft", draft))

    @unittest.skipIf(RULES is None, NO_RULES)
    def test_the_reader_refuses_a_draft_that_breaks_the_shape(self):
        # So that the check above can fail: a usage that says 0 for an unknown count, and a draft type a
        # driver may not draft, are both refused by the same reader.
        _stream, drafts = replay("synthetic-json-basic")
        usage = next(d for d in drafts if d["type"] == "usage")
        broken = json.loads(json.dumps(usage))
        broken["payload"]["reasoning_tokens"] = {"value": 0, "quality": "unknown", "provenance": "unavailable"}
        self.assertFalse(RULES.fits("driver.v2.json#/$defs/event_draft", broken))
        self.assertFalse(RULES.fits("driver.v2.json#/$defs/event_draft", {"type": "turn.ended", "payload": {}}))

    @unittest.skipIf(RULES is None, NO_RULES)
    def test_tool_input_digests_agree_with_spines_rules(self):
        inputs = [d["payload"]["input"] for name in FIXTURES for d in replay(name)[1] if d["type"] == "tool.call"]
        self.assertGreater(len(inputs), 0)
        inputs += [{"b": [1, 2.5, -0.0, 3.0, 1e-7, 1.5e-12, 123456789.125, 0.1], "a": None, chr(0xE000): True,
                    chr(0x1F600): "x"},
                   {"s": "tab\tquote\"back\\slash" + chr(0x1F) + chr(0x2028)}, {}, {"n": 2**53 - 1}]
        for value in inputs:
            with self.subTest(value=str(value)[:60]):
                self.assertEqual(pi.input_digest(value)[1], RULES.input_digest(RULES.normalised(value)))
                self.assertEqual(pi.canonical_text(pi.normalised(value)), RULES.text_of(RULES.normalised(value)))


class FixtureFormTest(unittest.TestCase):
    def test_every_fixture_is_labelled_synthetic_and_listed_in_the_readme(self):
        readme = (HERE / "README.md").read_text(encoding="utf-8")
        data = sorted(p.name for p in HERE.iterdir() if p.suffix in (".jsonl", ".json"))
        self.assertEqual(len(data), 2 * len(FIXTURES))
        for file in data:
            with self.subTest(file=file):
                self.assertTrue(file.startswith("synthetic-"))
                self.assertIn(file, readme)

    def test_one_shot_lines_are_written_as_json_stringify_writes_them(self):
        # No spaces, keys in the order given, whole numbers without a fraction: a captured stdout.jsonl can
        # replace a synthetic one line for line.
        for name, (mode, _conn, _prompt) in FIXTURES.items():
            if mode != "json":
                continue
            for line in pi.records_of(data_file(name).read_bytes()):
                with self.subTest(name=name, line=line[:50]):
                    record = pi.read_record(line)
                    self.assertEqual(json.dumps(record, separators=(",", ":"), ensure_ascii=False), line)
                    self.assertNotRegex(line, r"[0-9]\.0[,}\]]")


if __name__ == "__main__":
    unittest.main()
