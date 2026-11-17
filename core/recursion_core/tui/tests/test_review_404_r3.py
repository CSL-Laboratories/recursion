"""Review 3 of #404 (the keymap at 1cb91492): DeepSeek V4.1 Flash's break-it read, each finding as a test that
fails on that head (F8 is coverage of a rule no test held, and passes there by design).

F1 the row counts left out of step with the lines by one failure, F2 a first count at width 0, F3 a key bound
twice (a leader that base already uses, two spellings in one table) lost with no note, F4 never_bind given by
a consumer in another spelling, F5 Alt+Space, F6 the open keys with no row for an entry, F7 the counts counted
again when they are not as many as the lines, F8 base's leader sequences of three keys.
"""

from __future__ import annotations

import collections
import unittest
from unittest import mock

from recursion_core.tui import app as app_module
from recursion_core.tui.app import _Output
from recursion_core.tui.keymap import Keymap, load, show_sequence
from recursion_core.tui.keys import KeyDecoder
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.test_keymap import new_app

BAR = chr(0x2502)  # the output pane's left edge


def every_row(out: _Output, width: int) -> list[str]:
    return [row for line in out.source() for row in wrap(line, width)]


def failing_wrap(after: int = 0, on: str | None = None):
    """``wrap`` that raises on its ``after``-th call, or on the text ``on``, as a MemoryError over a long history
    would."""

    calls = {"n": 0}
    real = app_module.wrap

    def wrapped(text, width):
        calls["n"] += 1
        if calls["n"] == after or text == on:
            raise RuntimeError("a wrap failed part way")
        return real(text, width)

    return wrapped


class CountsStayWithTheLinesTest(unittest.TestCase):  # F1, F7
    def test_a_failure_while_counting_leaves_nothing_wrong(self) -> None:
        out = _Output(50, "run-1")
        out.feed("one\n" + "x" * 30 + "\nthree\n")
        out.counts(20)  # kept at another width, as many as the lines
        with mock.patch.object(app_module, "wrap", failing_wrap(after=2)), self.assertRaises(RuntimeError):
            out.counts(12)
        for _ in range(2):
            self.assertEqual(out.tail(4, 12), every_row(out, 12)[-4:])
        out.feed("four\n")
        self.assertEqual(out.tail(3, 12), ["xxxxxx", "three", "four"])

    def test_a_failure_while_feeding_leaves_nothing_wrong_even_when_full(self) -> None:
        for cap in (3, 50):  # at 3 the lines are full, so both deques drop their oldest as they grow
            with self.subTest(cap=cap):
                out = _Output(cap, "run-1")
                out.feed("one\n" + "x" * 30 + "\nthree\n")
                out.counts(12)
                with mock.patch.object(app_module, "wrap", failing_wrap(after=2)), self.assertRaises(RuntimeError):
                    out.feed("four\n" + "y" * 40 + "\nsix\n")
                self.assertEqual(out.tail(5, 12), every_row(out, 12)[-5:])
                self.assertEqual(out.total_rows(12), len(every_row(out, 12)))

    def test_the_frame_after_a_failed_frame_shows_the_output(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "alpha\nbeta\n"})
        with mock.patch.object(app_module, "wrap", failing_wrap(on="beta")), self.assertRaises(RuntimeError):
            app.frame()  # the frame counts the history first, and the count fails at "beta"
        self.assertIn(BAR + " beta", [t for t, _ in app.frame()])

    def test_counts_not_as_many_as_the_lines_are_counted_again(self) -> None:  # F7
        out = _Output(50, "run-1")
        out.feed("one\n" + "x" * 30 + "\nthree\n")
        out.counts(12)
        out._rows = collections.deque(maxlen=out.lines.maxlen)  # out of step, at the same width
        self.assertEqual(out.tail(5, 12), every_row(out, 12)[-5:])


class WidthZeroTest(unittest.TestCase):  # F2
    def test_nothing_is_counted_before_a_width_is(self) -> None:
        out, wrapped = _Output(10, "run-1"), []
        real = app_module.wrap
        with mock.patch.object(app_module, "wrap", lambda text, width: wrapped.append(text) or real(text, width)):
            out.feed("abc\ndef\n")
        self.assertEqual(wrapped, [], "a feed counts only once a frame has counted at a width")

    def test_a_first_count_at_width_0_counts(self) -> None:
        out = _Output(10, "run-1")
        out.feed("abc\ndef\n")
        self.assertEqual(out.total_rows(0), len(every_row(out, 0)))
        self.assertEqual(out.tail(2, 0), every_row(out, 0)[-2:])


class KeyBoundTwiceTest(unittest.TestCase):  # F3
    def test_a_leader_a_binding_in_force_uses_is_refused(self) -> None:
        base = Keymap.default()
        self.assertIsNone(base.bind("runs", "f1 k", "run.attach"))
        km = load('version = 1\nleader = "f1"\n', base)
        self.assertTrue(any("binding file not used" in n and "bindings in force use" in n for n in km.notes), km.notes)
        self.assertEqual(km.leader, "ctrl+x")
        self.assertEqual(km.view("runs")[("ctrl+x", "k")], "run.stop", "the Stop keeps its leader binding")

    def test_two_spellings_of_one_key_in_one_table_are_said(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"ctrl+i" = "runs.next"\n"tab" = "runs.prev"\n')
        self.assertTrue(any("'tab' = 'runs.prev' skipped: the same keys as 'ctrl+i'" in n for n in km.notes), km.notes)
        self.assertEqual(km.view("runs")[("tab",)], "runs.next", "the first is kept")


class ConsumerNeverBindTest(unittest.TestCase):  # F4
    def test_never_bind_given_by_a_consumer_is_the_key_a_terminal_sends(self) -> None:
        km = Keymap(never_bind=frozenset({"ctrl+i"}))
        self.assertEqual(km.never_bind, {"tab"})
        self.assertIn("tab is bound nowhere", km.bind("runs", "tab", "runs.next") or "")
        self.assertIn("tab is bound nowhere", km.for_client().bind("runs", "ctrl+i", "runs.next") or "")


class AltSpaceTest(unittest.TestCase):  # F5
    def test_alt_space_is_written_alt_space(self) -> None:
        self.assertEqual(KeyDecoder().feed(b"\x1b "), ["alt+ "])
        km = load('version = 1\n[bindings.runs]\n"alt+space" = "runs.next"\n')
        self.assertEqual(km.notes, [])
        self.assertEqual(km.feed("alt+ ", "runs"), "runs.next")
        self.assertEqual(show_sequence(("alt+ ",)), "Alt+Space")


class NoRowForTheKeysTest(unittest.TestCase):  # F6
    def test_the_open_keys_say_so_on_every_terminal_of_two_rows_or_more(self) -> None:
        for height in range(2, 9):
            with self.subTest(height=height):
                app, _ = new_app()
                app.resize(100, height)
                app.press("?")
                rows = [t for t, _ in app.frame()]
                shown = any(t.startswith(BAR + " ") for t in rows)
                self.assertTrue(shown or any("no room to show them here" in t for t in rows), rows)
                self.assertFalse(any("keys shown" in t for t in rows) and not shown, rows)


class BaseLongSequenceTest(unittest.TestCase):  # F8 (passes on 1cb91492 by design)
    def test_a_three_key_leader_sequence_of_base_moves(self) -> None:
        base = Keymap.default()
        self.assertIsNone(base.bind("global", "<leader> z z", "conditions.show"))
        km = load('version = 1\nleader = "f1"\n', base)
        self.assertEqual(km.notes, [])
        self.assertEqual(km.view("runs")[("f1", "z", "z")], "conditions.show")
        self.assertNotIn(("ctrl+x", "z", "z"), km.view("runs"))


if __name__ == "__main__":
    unittest.main()
