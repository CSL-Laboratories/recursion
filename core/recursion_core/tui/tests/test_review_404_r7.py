"""Round 7 of #404: DeepSeek V4.1 Flash's read of 90435952. F1: the keys view's header named the default scroll
keys ("j/k and Page Up/Down") after a binding file moved them, while the prompt under it named the keymap's. F2:
three rules of round 6 no test held: the scroll hint from the keymap's down and up keys, Page Up/Down only while
they page, and a landing into a pane of no rows that leaves the match in it when the terminal grows. F3: a keymap
with no close key said " closes". F4: ``_top_scroll(0)`` read the pane's width. The tests marked "holds" pass on
90435952 too: they keep what it already did, so a mutant is caught; every other test fails on it.
"""

from __future__ import annotations

import copy
import unittest

from recursion_core.tui import keymap as keymap_module
from recursion_core.tui.app import _Output
from recursion_core.tui.tests.test_keymap import new_app

W = 28  # the pane's width at a 30-column terminal
TALL = "x" * (W * 20) + "NEEDLE" + "z" * (W * 25)  # NEEDLE begins row 20 of 46
MOVED = 'version=1\n[bindings.help]\n"j"="help.close"\n"k"="help.close"\n"u"="help.down"\n"d"="help.up"\n'


def opened(text: str | None = None, keymap=None):
    """The keys view open on a 100x30 terminal: its header row, and the prompt."""
    app, _ = new_app(keymap if keymap is not None else keymap_module.load(text) if text else None)
    app.resize(100, 30)
    app.press("?")
    header = next(row for row, _ in app.frame() if row.startswith("-- keys"))
    return header, app.prompt()


class HeaderFollowsTheKeymapTest(unittest.TestCase):  # F1, F2 (the scroll hint)
    def test_moved_scroll_keys_are_named_in_the_header_and_the_prompt(self) -> None:
        header, prompt = opened(MOVED)
        self.assertIn("u/d and Page Up/Down scroll", header)
        self.assertNotIn("j/k", header)
        self.assertIn("u/d and Page Up/Down scroll", prompt)
        self.assertIn("Esc, q, ?, j or k closes", header)

    def test_page_up_and_down_are_named_only_while_they_page(self) -> None:
        for text in ('version=1\n[bindings.help]\n"pagedown"="help.down"\n',
                     'version=1\n[bindings.help]\n"pagedown"="none"\n'):
            with self.subTest(text=text.splitlines()[-1]):
                header, prompt = opened(text)
                self.assertNotIn("Page Up/Down", header)
                self.assertNotIn("Page Up/Down", prompt)
                self.assertIn("j/k scroll", header)
                self.assertIn("j/k scroll", prompt)

    def test_the_default_header_reads_as_before(self) -> None:  # holds
        header, prompt = opened()
        self.assertIn("j/k and Page Up/Down scroll", header)
        self.assertTrue(header.startswith("-- keys (Esc, q or ? closes; rows 1-"))
        self.assertEqual(prompt, "recursion: keys shown; j/k and Page Up/Down scroll; Esc, q or ? closes")


class NoCloseKeyTest(unittest.TestCase):  # F3
    def test_a_keymap_with_no_close_key_says_so(self) -> None:
        km = copy.deepcopy(keymap_module.Keymap.default())
        km.bindings["help"] = {seq: a for seq, a in km.bindings["help"].items() if a != "help.close"}
        header, prompt = opened(keymap=km)
        self.assertIn("(no key closes it;", header)
        self.assertTrue(prompt.endswith("; no key closes it"), prompt)


class ZeroRowLandingTest(unittest.TestCase):  # F2 (the floor of one row)
    def test_a_landing_into_a_pane_of_no_rows_leaves_the_match_in_it_when_the_terminal_grows(self) -> None:  # holds
        app, _ = new_app()
        app.resize(30, 30)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": TALL + "\n"})
        app.frame()
        app._search("NEEDLE", 0)
        for height in range(3, 30):  # the first height whose output pane has its header row and no row of text
            app.resize(30, height)
            frame = [row for row, _ in app.frame()]
            if any(row.startswith("-- output") for row in frame) and not any(row.startswith("│") for row in frame):
                break
        else:
            self.fail("no height gives an output pane of no rows")
        app.resize(30, 30)
        self.assertIn("NEEDLE", "".join(row[2:] for row, _ in app.frame() if row.startswith("│ ")))


class TopScrollWidthTest(unittest.TestCase):  # F4
    def test_a_width_of_zero_is_that_width(self) -> None:
        app, _ = new_app()
        app.resize(30, 30)
        app.attach("run-1")
        out = _Output(10, "run-1")
        out.feed("abcdef\n")
        app.outputs["run-1"] = out
        self.assertEqual(app._top_scroll(0), out.total_rows(0) - 1)
        self.assertEqual(app._top_scroll(), out.total_rows(app._pane_width()) - 1)


if __name__ == "__main__":
    unittest.main()
