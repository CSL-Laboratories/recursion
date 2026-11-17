"""Round 3 of #404, GPT-6 Astra's confirming read of 3d20bd3c: one required change. DeepSeek's round-3 F1 was only
partly answered: the output buffer replaced its retained lines before it marked their row counts "not counted",
so a failure in between, with the buffer full, left counts as many as the lines but not theirs, and the frame
showed old rows. The cases are Astra's (its evidence, ``test_cache_failure.py`` and ``test_latest.py``), each red
on 3d20bd3c. Astra's five coverage cases (its mutants N20, N22, N30, N38 and D17, rules no test held) pass on that
head too and are kept here as well.
"""

from __future__ import annotations

import collections
import inspect
import sys
import unittest

from recursion_core.tui import app as app_module
from recursion_core.tui.app import _Output
from recursion_core.tui.keymap import Keymap, Pending, load
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.test_keymap import new_app


def every(out: _Output, width: int) -> list[str]:
    return [row for line in out.source() for row in wrap(line, width)]


class OnceFailingDeque(collections.deque):
    """Fails after two of the lines it is given have replaced retained ones."""

    def extend(self, values):
        for i, value in enumerate(values):
            self.append(value)
            if i == 1:
                raise MemoryError("simulated allocation failure after two retained lines replaced")


class CacheFailure(unittest.TestCase):  # required 1
    def test_F1_partial_deque_extend_failure_recovers_at_capacity(self) -> None:
        out = _Output(3, "fake")
        out.feed("a\nb\nc\n")
        out.counts(4)
        out.lines = OnceFailingDeque(out.lines, maxlen=3)
        with self.assertRaises(MemoryError):
            out.feed("abcdefghijk\nlmnopqrstuv\nwxyz1234567\n")
        expected = every(out, 4)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(out.tail(6, 4), expected[-6:])
                self.assertEqual(out.total_rows(4), len(expected))

    def test_F1_app_frame_recovers_after_partial_deque_extend_failure(self) -> None:
        app, _ = new_app()
        app._output_lines = 3
        app.resize(30, 20)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "a\nb\nc\n"})
        app.frame()
        out = app.outputs["run-1"]
        out.lines = OnceFailingDeque(out.lines, maxlen=3)
        with self.assertRaises(MemoryError):
            app.on_event({"run_id": "run-1", "text": "x" * 90 + "\n" + "y" * 90 + "\n" + "z" * 90 + "\n"})
        expected = every(out, 28)
        shown = [text[2:] for text, _ in app.frame() if text.startswith("│ ")]
        self.assertEqual(shown, expected[-app._out_rows:])

    def test_F1_default_retention_capacity_keeps_counts_after_extend_failure(self) -> None:
        class AllocationFailure(collections.deque):
            def extend(self, values):
                for i, value in enumerate(values):
                    if i == 48:
                        raise MemoryError("simulated block allocation failure after 48 replacements")
                    self.append(value)

        out = _Output(2000, "fake")
        out.feed("a\n" * 2000)
        out.counts(4)
        out.lines = AllocationFailure(out.lines, maxlen=2000)
        with self.assertRaises(MemoryError):
            out.feed("abcdefghijk\n" * 60)
        expected = every(out, 4)
        self.assertEqual(out.total_rows(4), len(expected))
        self.assertEqual(out.tail(6, 4), expected[-6:])


class InterruptionTest(unittest.TestCase):  # required 1: "interruption immediately after extend also fails"
    def test_F1_interruption_after_full_deque_replaces_lines(self) -> None:
        out = _Output(3, "fake")
        out.feed("a\nb\nc\n")
        out.counts(4)
        source, start = inspect.getsourcelines(app_module._Output.feed)
        target = start + next(i for i, text in enumerate(source) if "self.lines.extend(done)" in text) + 1

        def trace(frame, event, arg):
            if frame.f_code is app_module._Output.feed.__code__ and event == "line" and frame.f_lineno == target:
                raise KeyboardInterrupt("bounded injected interruption after the retained lines were replaced")
            return trace

        sys.settrace(trace)
        try:
            with self.assertRaises(KeyboardInterrupt):
                out.feed("abcdefghijk\nlmnopqrstuv\nwxyz1234567\n")
        finally:
            sys.settrace(None)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.assertEqual(out.total_rows(4), len(every(out, 4)))
                self.assertEqual(out.tail(6, 4), every(out, 4)[-6:])


class CoverageTest(unittest.TestCase):  # Astra's later cases: rules its mutants N20, N22, N30, N38, D17 found unheld
    def test_nontext_leader_file_refuses_without_raising(self) -> None:  # N20
        for value in ("3", "true", '["f1"]', '{key="f1"}', "1.5"):
            with self.subTest(value=value):
                km = load("version=1\nleader=" + value)
                self.assertEqual(km.bindings, Keymap.default().bindings)
                self.assertTrue(any("not a usable key" in note for note in km.notes))

    def test_migrated_alias_leader_really_starts_sequence(self) -> None:  # N22
        for alias, key in (("ctrl+h", "backspace"), ("ctrl+alt+i", "alt+tab"), ("alt+space", "alt+ ")):
            with self.subTest(alias=alias):
                km = load('version=1\nleader="' + alias + '"')
                self.assertEqual(km.feed(key, "runs"), Pending((key,)))
                self.assertEqual(km.feed("k", "runs"), "run.stop")

    def test_dropped_migration_is_diagnosed_for_inherited_never_bind(self) -> None:  # N30
        base = load('version=1\n[safety]\nnever_bind=["f1"]')
        km = load('version=1\nleader="f1"', base)
        self.assertTrue(any("dropped with leader F1" in note for note in km.notes), km.notes)

    def test_invalid_key_after_close_leaves_state_unchanged(self) -> None:  # N38
        app, api = new_app()
        app.close()
        state = (list(app.errors), app.last_error, list(app.log), app.mode)
        for key in (None, {}, [], b"enter"):
            app.press(key)
            self.assertEqual((list(app.errors), app.last_error, list(app.log), app.mode), state)
        self.assertEqual(api.calls, [])

    def test_base_leader_assigned_an_alias_after_construction(self) -> None:  # D17
        base = Keymap()
        base.bind("runs", "backspace", "runs.next")
        base.leader = "ctrl+h"
        km = load("version=1", base)
        self.assertTrue(any("binding file not used" in note for note in km.notes), km.notes)
        self.assertEqual(km.bindings, base.bindings)
        self.assertEqual(base.leader, "ctrl+h")


if __name__ == "__main__":
    unittest.main()
