"""Round 8 of #404: the Grok read of 6d60493f (ruling 10). R1: the draft no longer merged with master 8c45b168
(#405); the plain merge c0bf0524 joins the two sides, and these tests hold the join: a search in a transcript or in
the session list lands in the pane, also when a page arrives before the frame (``_land`` takes the pane's own
output; ``extend`` advances ``finished``), and attaching the run from a transcript forgets the landing. R2: the
keys view names no scrolling when its keymap binds no scroll key, a rule no test held. Grok's advisable point: with
one of ``help.down`` and ``help.up`` bound, the scroll hint names that key. The tests marked "holds" pass on
c0bf0524 too (the merge carries R1's code; R2's rule was there): a mutant of each rule is caught; the other test
fails on it.
"""

from __future__ import annotations

import unittest

from recursion_core.tui import keymap as keymap_module
from recursion_core.tui.app import SESSION_LIST, _Output
from recursion_core.tui.tests.test_keymap import new_app

W = 28  # the pane's width at a 30-column terminal
TALL = "x" * (W * 20) + "NEEDLE" + "z" * (W * 25)  # NEEDLE begins row 20 of 46
NO_SCROLL = ('version=1\n[bindings.help]\n"j"="none"\n"down"="none"\n"k"="none"\n"up"="none"\n'
             '"pagedown"="none"\n"pageup"="none"\n')


def shown(app) -> str:
    return "".join(row[2:] for row, _ in app.frame() if row.startswith("│ "))


def opened(text: str):
    app, _ = new_app(keymap_module.load(text))
    app.resize(100, 30)
    app.press("?")
    header = next(row for row, _ in app.frame() if row.startswith("-- keys"))
    return header, app.prompt()


def with_pane(kind: str):
    app, _ = new_app()
    app.resize(30, 30)
    app.attach("run-1")
    app.frame()
    out = _Output(2000, "s-1")
    out.extend(["older", TALL])
    if kind == "transcript":
        app.transcript_rows["s-1"] = out
        app.pane = "s-1"
    else:
        app.session_view = out
        app.pane = SESSION_LIST
    app.scroll = 0
    app.frame()
    return app, out


class PaneLandingTest(unittest.TestCase):  # R1, the join
    def test_a_match_in_a_transcript_or_the_session_list_lands_in_the_pane(self) -> None:  # holds
        for kind in ("transcript", "sessions"):
            for later in (0, 1):
                with self.subTest(kind=kind, rows_before_the_frame=later):
                    app, out = with_pane(kind)
                    app._search("NEEDLE", 0)
                    out.extend(["newer"] * later)  # a page that arrives before the frame
                    self.assertIn("NEEDLE", shown(app))
                    self.assertIn("line 2 of 2", app.notice)

    def test_attaching_the_run_from_a_transcript_forgets_the_landing(self) -> None:  # holds
        app, _ = with_pane("transcript")
        app._search("NEEDLE", 0)
        app.attach("run-1")  # the same run: back to its output
        self.assertEqual((app.scroll, app.search, app._landing, app.pane), (0, "", None, None))


class ScrollHintTest(unittest.TestCase):  # R2, and Grok's advisable point
    def test_with_no_scroll_key_neither_hint_names_scrolling(self) -> None:  # holds
        header, prompt = opened(NO_SCROLL)
        self.assertIn("; rows 1-", header)
        self.assertNotIn("scroll", header)
        self.assertEqual(prompt, "recursion: keys shown; Esc, q or ? closes")

    def test_one_scroll_key_bound_is_named_alone(self) -> None:
        header, prompt = opened('version=1\n[bindings.help]\n"k"="none"\n"up"="none"\n')
        self.assertIn("j and Page Up/Down scroll", header)
        self.assertIn("j and Page Up/Down scroll", prompt)


if __name__ == "__main__":
    unittest.main()
