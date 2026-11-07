"""Required test 6: engine output with ESC, OSC 52, DCS, title changes and look-alike prompt text
cannot send input, change the target or draw outside its own pane (fixtures UI-10, RJ-F21)."""

from __future__ import annotations

import re
import unittest

from recursion_core.tui import sanitize as s
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI, fixture
from recursion_core.tui.view import to_ansi, to_text

CORPUS = fixture("injection.json")["cases"]
FORBIDDEN = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f؜​-‏ -‮⁠-⁩﻿]")
OWN_SGR = re.compile(r"\x1b\[(?:H|J|K|\d+(?:;\d+)*m)")


def violations(render, raw: str) -> list[str]:
    """Checks one renderer on one raw input; also run against a mutant below so the detector is live."""

    found = []
    whole = render([raw])
    if FORBIDDEN.search(whole):
        found.append("control or bidi character reached the display")
    step = 1 if len(raw) < 400 else 97
    for cut in range(0, len(raw) + 1, step):
        if render([raw[:cut], raw[cut:]]) != whole:
            found.append(f"split at {cut} renders differently")
            break
    data = raw.encode("utf-8")
    for cut in range(0, len(data) + 1, 1 if len(data) < 400 else 97):
        if render([data[:cut], data[cut:]]) != whole:
            found.append(f"byte split at {cut} renders differently")
            break
    return found


def real(chunks) -> str:
    san = s.OutputSanitizer()
    return "".join(san.feed(c) for c in chunks) + san.flush()


def passthrough_mutant(chunks) -> str:
    return "".join(c.decode("utf-8", "replace") if isinstance(c, bytes) else c for c in chunks)


class SanitizerTest(unittest.TestCase):
    def test_corpus_renders_exactly_and_split_invariant(self) -> None:
        for case in CORPUS:
            with self.subTest(case["name"]):
                self.assertEqual(real([case["raw"]]), case["rendered"])
                self.assertEqual(violations(real, case["raw"]), [])

    def test_detector_mutant_is_caught(self) -> None:
        """A suite that passes a pass-through renderer proves nothing (UI-10 detector mutant)."""

        caught = {c["name"] for c in CORPUS if violations(passthrough_mutant, c["raw"])}
        self.assertEqual(caught, {c["name"] for c in CORPUS if c["raw"] != c["rendered"]})
        self.assertGreaterEqual(len(caught), 20)

    def test_overlong_string_cannot_swallow_later_output(self) -> None:
        raw = "\x1b]52;c;" + "A" * (s.MAX_SEQ + 10) + "\nvisible after\n"
        out = real([raw])
        self.assertIn("overlong", out)
        self.assertIn("visible after", out)

    def test_unterminated_sequence_at_end_is_shown(self) -> None:
        self.assertTrue(real(["text \x1b]0;never ends"]).endswith("⟦OSC 0 unterminated⟧"))

    def test_api_fields_are_sanitized_too(self) -> None:
        self.assertEqual(s.field("run-1\x1b]0;x\x07\nfake"), "run-1⟦OSC 0⟧⟦LF⟧fake")


class InjectionInAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.r1, self.r2 = self.driver.start_run(), self.driver.start_run()
        self.api = RecordingAPI(self.driver.api)
        self.app = TuiApp(self.api, clock=Clock(), environ={}, width=60, height=24)
        self.app.open()
        self.app.attach(self.r1)
        self.api.calls.clear()

    def test_engine_output_cannot_send_input_or_change_target(self) -> None:
        before = self.app.frame()
        for case in CORPUS:
            self.app.on_event({"run_id": self.r1, "text": case["raw"]})
            self.app.on_event({"run_id": self.r1, "text": case["raw"].encode("utf-8")})
        self.assertEqual(self.api.calls, [], "engine output never becomes a request")
        self.assertEqual((self.app.selected, self.app.attached, self.app.mode), (self.r1, self.r1, "keys"))
        self.assertEqual(self.app.errors, [])
        self.assertEqual(self.driver.kill.requests, [])
        self.assertEqual(self.driver.model_calls, 2)
        after = self.app.frame()
        out_rows = [i for i, (t, _) in enumerate(after) if t.startswith("│")]
        for i, (row, prev) in enumerate(zip(after, before)):
            if i not in out_rows:
                self.assertEqual(row, prev, f"row {i} outside the output pane changed")
        self.app.press("s")  # the stop target is the selected run, whatever the output said
        self.app.press("y")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": self.r1})])
        self.assertEqual(self.driver.registry.get(self.r2)["state"], "running")

    def test_output_stays_inside_its_pane(self) -> None:
        for case in CORPUS:
            self.app.on_event({"run_id": self.r1, "text": case["raw"]})
        self.app.on_event({"run_id": self.r1, "text": "宽" * 200 + "\n"})  # double-width overflow
        frame = self.app.frame()
        self.assertEqual(len(frame), self.app.height)
        for text, _ in frame:
            self.assertLessEqual(sum(s.cell_width(ch) for ch in text), self.app.width, text)
            self.assertIsNone(FORBIDDEN.search(text), repr(text))
        ansi = to_ansi(frame, color=True)
        self.assertNotIn("\x1b", OWN_SGR.sub("", ansi), "only the TUI's own repaint and SGR codes are emitted")
        self.assertNotIn("\x1b", to_text(frame))

    def test_lookalike_prompt_is_attributed_to_the_output_pane(self) -> None:
        fake = "recursion: stop run run-2? y sends run.stop"
        self.app.on_event({"run_id": self.r1, "text": fake + "\n"})
        frame = [t for t, _ in self.app.frame()]
        self.assertIn("│ " + fake, frame)
        self.assertNotEqual(frame[-1], fake)
        self.assertTrue(frame[-1].startswith("recursion: j/k select"))
        self.app.press("y")  # a "y" after look-alike text confirms nothing
        self.assertEqual(self.api.calls, [])

    def test_output_for_another_run_is_not_drawn_in_this_pane(self) -> None:
        self.app.on_event({"run_id": self.r2, "text": "other run says hello\n"})
        self.assertNotIn("other run says hello", to_text(self.app.frame()))


if __name__ == "__main__":
    unittest.main()
