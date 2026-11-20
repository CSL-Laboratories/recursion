"""Round 5 of #404: DeepSeek V4.1 Flash's read of 5f69dc0f (F1 to F8) and GPT-6.1 Sol's (required 1 to 3; Sol stood
in for Astra). F1 and required 1: a match spanning three or more display rows, or one over a row's end in a line
whose rows lower otherwise than the whole line (a final sigma), landed on a row that does not show it. F3 and
required 2: the landing used the last frame's rows, not those of the frame that draws it (a resize, in Loop.tick
after the keys too). F4 and required 3: an ``alt+`` name of a character the decoder never gives was a key. F5 to
F8: the sentences. The tests marked "holds" pass on 5f69dc0f too: they keep what it already did, so a mutant of
the row finding is caught (F2); every other test fails on it.
"""

from __future__ import annotations

import pathlib
import unittest

from recursion_core.tui import app as app_module
from recursion_core.tui import keymap as keymap_module
from recursion_core.tui.app import Loop, _Output
from recursion_core.tui.keys import KeyDecoder
from recursion_core.tui.tests.test_keymap import new_app

W = 28  # the pane's width at a 30-column terminal
TALL = "x" * (W * 20) + "NEEDLE" + "z" * (W * 25)  # NEEDLE begins row 20 of 46


def pane_rows(app) -> list[str]:
    return [text[2:] for text, _ in app.frame() if text.startswith("│ ")]


def with_line(line: str, *, width: int = 30, height: int = 30):
    app, api = new_app()
    app.resize(width, height)
    app.attach("run-1")
    app.on_event({"run_id": "run-1", "text": line + "\n"})
    app.frame()
    return app, api


def searched(line: str, needle: str, *, shrink_to: int | None = None) -> str:
    app, _ = with_line(line)
    if shrink_to is not None:
        app.resize(30, shrink_to)  # no frame between the resize and the search
    app._search(needle, 0)
    return "".join(pane_rows(app))


class RowOfTheMatchTest(unittest.TestCase):  # F1, F2; Sol's required 1
    def test_a_match_spanning_three_rows_past_the_pane_is_in_the_pane(self) -> None:
        self.assertIn("N" * 60, searched("a" * (W * 17) + "N" * 60 + "b" * (W * 5), "N" * 60))

    def test_a_phrase_over_three_rows_is_in_the_pane(self) -> None:  # Sol's T4 three rows
        phrase = "0123456789" * 7
        self.assertIn(phrase, searched("x" * (W * 20) + phrase + "z" * (W * 20), phrase))

    def test_a_match_over_a_final_sigma_row_end_is_in_the_pane(self) -> None:  # Sol's T4 Greek sigma
        # rows lowered one by one end "ος" (a final sigma); the line lowered whole has "οσα"
        self.assertIn("ΟΣΑ", searched("x" * (W * 20 - 2) + "ΟΣΑ" + "z" * (W * 25), "οσα"))

    def test_a_match_after_characters_that_lower_to_two_is_in_the_pane(self) -> None:  # holds
        self.assertIn("needle", searched("İ" * (W * 20) + "needle" + "z" * (W * 25), "needle"))

    def test_a_match_in_the_middle_of_a_row_past_the_pane_is_in_the_pane(self) -> None:  # holds
        self.assertIn("NEEDLE", searched("a" * (W * 17 + 5) + "NEEDLE" + "b" * (W * 10), "NEEDLE"))

    def test_short_matches_at_every_wrap_position_are_in_the_pane(self) -> None:  # holds; Sol's T4 cases
        for width, height in ((18, 24), (30, 30), (47, 23)):
            app, _ = new_app()
            app.resize(width, height)
            app.attach("run-1")
            app.frame()
            w = app._pane_width()
            for offset in (0, w - 3, w * 15 + 3, w * 20 - 2):
                with self.subTest(width=width, height=height, offset=offset):
                    out = _Output(2000, "run-1")
                    out.feed("x" * offset + "NEEDLE" + "z" * (w * 25) + "\n")
                    app.outputs["run-1"] = out
                    app.scroll = 0
                    app.frame()
                    app._search("NEEDLE", 0)
                    self.assertIn("NEEDLE", "".join(pane_rows(app)))
                    self.assertIn("line 1 of 1", app.notice)

    def test_a_case_folded_wide_match_after_other_lines_is_in_the_pane(self) -> None:  # holds; Sol's T4
        app, _ = new_app()
        app.resize(33, 30)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "older\n" + "界" * 350 + "MiXeD" + "尾" * 350 + "\nnewer\n"})
        app.frame()
        app._search("mixed", 0)
        self.assertIn("MiXeD", "".join(pane_rows(app)))


class LandingInTheFrameDrawnTest(unittest.TestCase):  # F3; Sol's required 2
    def test_a_search_after_the_terminal_shrank_lands_in_the_pane_it_has_now(self) -> None:
        # 14 pane rows become 2: the row the match begins on is the pane's oldest
        self.assertIn("NEEDLE", searched(TALL, "NEEDLE", shrink_to=18))
        shown = searched("a" * (W * 17) + "N" * 60 + "b" * (W * 5), "N" * 60, shrink_to=18)
        self.assertTrue(shown.startswith("N" * W), shown)  # three rows of match, two of pane: its first two

    def test_a_resize_between_the_search_and_the_frame_lands_in_the_frame_drawn(self) -> None:
        for size in ((30, 18), (24, 30), (40, 20)):
            with self.subTest(size=size):
                app, _ = with_line(TALL)
                app._search("NEEDLE", 0)
                app.resize(*size)
                self.assertIn("NEEDLE", "".join(pane_rows(app)))

    def test_a_resize_in_the_loop_tick_after_the_search_lands_in_the_frame_it_draws(self) -> None:  # Sol's T4
        app, api = with_line(TALL)
        app.press("tab")
        app.press("/")
        for ch in "NEEDLE":
            app.press(ch)
        loop = Loop(app, api, size=lambda: (30, 18))
        try:
            loop.press("enter")
            loop.tick()  # keys first, the terminal's size after them, then the frame
            self.assertIn("NEEDLE", "".join(row[2:] for row, _ in loop.frame if row.startswith("│ ")))
        finally:
            loop.shutdown()

    def test_output_that_retention_trims_before_the_frame_keeps_the_match_s_line(self) -> None:
        app, _ = new_app()
        app.resize(30, 30)
        app.attach("run-1")
        app.frame()
        out = _Output(3, "run-1")  # three lines kept
        out.feed("a\n" + TALL + "\nb\n")
        app.outputs["run-1"] = out
        app.scroll = 0
        app.frame()
        app._search("NEEDLE", 0)
        app.on_event({"run_id": "run-1", "text": "c\n"})  # "a" is dropped: the match's line is first now
        app.resize(30, 18)
        self.assertIn("NEEDLE", "".join(pane_rows(app)))

    def test_a_pager_key_after_the_search_keeps_its_move(self) -> None:  # holds
        app, _ = with_line(TALL)
        app._search("NEEDLE", 0)
        landed = app.scroll
        app._page("pager.up")
        app.frame()
        self.assertEqual(app.scroll, landed + 1)


class AltInvisibleTest(unittest.TestCase):  # F4; Sol's required 3
    def test_an_alt_name_of_a_character_the_decoder_never_gives_is_no_key(self) -> None:
        for ch in ("͏", "ᅟ", "ᅠ", "ㅤ", "ﾠ", "️", "\U000e0100"):
            with self.subTest(codepoint=f"U+{ord(ch):04X}"):
                self.assertEqual(KeyDecoder().feed(b"\x1b" + ch.encode("utf-8")), [])
                self.assertIsNone(keymap_module.key_name("alt+" + ch))
                km = keymap_module.load(f'version=1\n[bindings.runs]\n"alt+{ch}" = "runs.refresh"')
                self.assertTrue(km.notes)
                self.assertNotIn(("alt+" + ch,), km.bindings.get("runs", {}))
        self.assertEqual(keymap_module.key_name("alt+a"), "alt+a")


class SentencesTest(unittest.TestCase):  # F5, F6, F7, F8
    def test_the_sentences_say_what_the_code_does(self) -> None:
        search = " ".join(app_module.TuiApp._search.__doc__.split())
        self.assertIn("the row the match begins on becomes the pane's oldest", search)
        self.assertIn("The frame that next draws the pane lands it again at its own width and height", search)
        doc = " ".join(keymap_module.__doc__.split())
        self.assertIn('the value ``"none"`` removes that context\'s binding, a default or a file\'s own', doc)
        self.assertIn("only where a leader sequence is typed", doc)
        self.assertIn("Binding is not serialised", doc)
        readme = " ".join((pathlib.Path(keymap_module.__file__).parent / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("under `<leader> p`, and only where a leader sequence is typed (global, runs, output)", readme)


if __name__ == "__main__":
    unittest.main()
