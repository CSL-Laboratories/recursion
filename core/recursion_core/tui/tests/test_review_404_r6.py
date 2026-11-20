"""Round 6 of #404: DeepSeek V4.1 Flash's read of b52b8f13 (F1 to F5; F6 is round 5's F9, declared), and the keys
view's hints from the Sol sweep that composed #404 with master ff3f8071 (its F2; its F1 is round 5's landing,
held here). F1: three rules of round 5 that no test held: a search made after retention dropped lines, a match
whose unfinished line was cut before the frame, a search that finds nothing after one that did. F2: attaching
another run kept the search's landing. F3: the frame's landing and its pager limit read the width again, so a
resize on another thread during the frame landed the match at a width the pane is not drawn at. F4: a lone
surrogate was a key name, alone or after ``alt+``. F5: the sentence on ``for_client``. The sweep's F2: the keys
view's hints named the default keys after a binding file changed them, and its list left out its own keys. The
tests marked "holds" pass on b52b8f13 too: they keep what it already did, so a mutant is caught; every other
test fails on it.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import keymap as keymap_module
from recursion_core.tui.app import _Output
from recursion_core.tui.tests.test_keymap import new_app

W = 28  # the pane's width at a 30-column terminal
TALL = "x" * (W * 20) + "NEEDLE" + "z" * (W * 25)  # NEEDLE begins row 20 of 46
EARLY = "NEEDLE" + "z" * (W * 45)  # NEEDLE on row 0 of 46
HELP_FILE = 'version = 1\n[bindings.help]\n"q" = "help.down"\n"f2" = "help.close"\n'


def shown(app) -> str:
    return "".join(text[2:] for text, _ in app.frame() if text.startswith("│ "))


def with_line(line: str, *, width: int = 30, height: int = 30):
    app, api = new_app()
    app.resize(width, height)
    app.attach("run-1")
    app.on_event({"run_id": "run-1", "text": line + "\n"})
    app.frame()
    return app, api


def with_output(out: _Output):
    app, _ = new_app()
    app.resize(30, 30)
    app.attach("run-1")
    app.frame()
    app.outputs["run-1"] = out
    app.scroll = 0
    app.frame()
    return app


class RulesNoTestHeldTest(unittest.TestCase):  # F1
    def test_a_search_after_retention_dropped_lines_lands_on_the_match(self) -> None:  # holds
        out = _Output(3, "run-1")  # three lines kept: "a" is dropped before the search
        out.feed("a\nb\n" + TALL + "\nc\n")
        app = with_output(out)
        app._search("NEEDLE", 0)
        self.assertIn("NEEDLE", shown(app))

    def test_a_match_whose_unfinished_line_was_cut_before_the_frame_is_not_landed(self) -> None:  # holds
        app, _ = new_app()
        app.resize(30, 30)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "a" * 3000 + "NEEDLE"})  # unfinished: no newline
        app.frame()
        app._search("NEEDLE", 0)
        self.assertEqual(app.scroll, 0)  # the match is in the pane's newest rows: the pager follows
        app.on_event({"run_id": "run-1", "text": "b" * 6000})  # the unfinished line is cut at its start
        app.frame()
        self.assertEqual(app.scroll, 0)  # its text at the match's offset is another now: nothing is landed

    def test_a_search_that_finds_nothing_leaves_the_pane_where_it_was(self) -> None:  # holds
        app, _ = with_line(TALL)
        app._search("NEEDLE", 0)
        landed = app.scroll
        app._search("absent", 0)
        app.resize(30, 18)
        shown(app)
        self.assertEqual(app.scroll, landed)
        self.assertIn("not found", app.notice)

    def test_a_match_at_a_long_line_s_start_before_a_short_line_is_in_the_pane(self) -> None:  # holds; sweep F1
        app, _ = new_app()
        app.resize(20, 20)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "needle" + "x" * 90 + "\nlast\n"})
        app.frame()
        for key in ["tab", "/", *"needle", "enter"]:
            app.press(key)
        self.assertIn("search needle: line 1 of 2", app.notice)
        self.assertIn("needle", shown(app))


class LandingTest(unittest.TestCase):  # F2, F3
    def test_attaching_another_run_and_back_forgets_the_search_s_landing(self) -> None:
        app, _ = with_line(TALL)
        app._search("NEEDLE", 0)
        app.attach("run-2")
        app.attach("run-1")
        shown(app)
        self.assertEqual((app.scroll, app.search), (0, ""))

    def test_a_resize_during_the_frame_lands_at_the_width_the_pane_is_drawn_at(self) -> None:
        for line in (TALL, EARLY):
            with self.subTest(line=line[:8]):
                app, _ = with_line(line)
                app._search("NEEDLE", 0)
                status_row = app.status_row

                def resized_meanwhile(app=app, status_row=status_row) -> str:  # after the frame read the size
                    app.resize(60, 30)  # as another thread would: resize takes no lock
                    return status_row()

                app.status_row = resized_meanwhile
                self.assertIn("NEEDLE", shown(app))  # drawn at 30 columns


class SurrogateTest(unittest.TestCase):  # F4
    def test_a_lone_surrogate_is_no_key_alone_or_after_alt(self) -> None:
        for ch in ("\ud800", "\udfff"):
            for token in (ch, "alt+" + ch):
                with self.subTest(token=ascii(token)):
                    self.assertIsNone(keymap_module.key_name(token))
                    km = keymap_module.load(f'version=1\n[bindings.runs]\n"{token}" = "runs.refresh"')
                    self.assertTrue(km.notes)
                    self.assertNotIn((token,), km.bindings.get("runs", {}))
        self.assertEqual(keymap_module.key_name("alt+é"), "alt+é")


class SentenceTest(unittest.TestCase):  # F5
    def test_for_client_shares_the_tables(self) -> None:
        doc = " ".join(keymap_module.__doc__.split())
        self.assertIn("each client keeps its own pending sequence, ``for_client``; the binding tables are shared", doc)
        self.assertNotIn("its own copy", doc)
        base = keymap_module.Keymap.default()
        self.assertIs(base.for_client().bindings, base.bindings)


class KeysViewFollowsTheKeymapTest(unittest.TestCase):  # the sweep's F2
    def test_the_hints_name_the_keys_that_leave_it(self) -> None:
        km = keymap_module.load(HELP_FILE)
        self.assertEqual(km.notes, [])
        app, _ = new_app(km)
        app.press("?")
        app.frame()
        app.press("q")
        self.assertEqual(app.mode, "help")  # q scrolls now
        self.assertIn("Esc, ? or F2 closes", app.prompt())
        header = next(text for text, _ in app.frame() if text.startswith("-- keys"))
        self.assertIn("Esc, ? or F2 closes", header)

    def test_its_list_has_its_own_keys(self) -> None:
        app, _ = new_app(keymap_module.load(HELP_FILE))
        app.press("?")
        text = app.log.latest("help").text
        self.assertIn("keys, this keys view:", text)
        self.assertIn("F2", text)

    def test_the_default_hints_are_as_before(self) -> None:  # holds
        app, _ = new_app()
        app.press("?")
        rows = app.frame()
        self.assertEqual(app.prompt(), "recursion: keys shown; j/k and Page Up/Down scroll; Esc, q or ? closes")
        self.assertTrue(any(text.startswith("-- keys (Esc, q or ? closes") for text, _ in rows))


if __name__ == "__main__":
    unittest.main()
