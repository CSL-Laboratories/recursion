"""Review 4 of #404 (the keymap at f6a747ce): DeepSeek V4.1 Flash's confirming read, LOW only. Each point that
changes behaviour is a test that fails on that head: N1 a base whose leader is another spelling of a key it
binds made load() raise, N2 a consumer's leader in another spelling could never be typed, N3 the note for
never_bind of Alt+Space named the key with a bare space.
"""

from __future__ import annotations

import unittest

from recursion_core.tui.keymap import Keymap, Pending, load
from recursion_core.tui.keys import KeyDecoder


class LeaderSpellingTest(unittest.TestCase):  # N1, N2
    def test_a_consumers_leader_is_the_key_a_terminal_sends(self) -> None:
        km = Keymap(leader="ctrl+i")
        self.assertEqual(km.leader, "tab")
        self.assertIsNone(km.bind("global", "<leader> k", "run.stop"))
        self.assertEqual(km.feed(KeyDecoder().feed(b"\t")[0], "runs"), Pending(("tab",)))
        self.assertEqual(km.feed("k", "runs"), "run.stop")

    def test_a_base_with_its_leader_in_another_spelling_loads(self) -> None:
        base = Keymap(leader="ctrl+h")
        base.bind("runs", "backspace", "runs.next")  # the key the leader names: refused alone once N2 holds
        for text in ("version = 1\n", 'version = 1\n[bindings.runs]\n"k" = "runs.prev"\n'):
            with self.subTest(text=text):
                km = load(text, base)  # once raised KeyError('leader')
                self.assertEqual((km.leader, km.notes), ("backspace", []))


class AltSpaceNoteTest(unittest.TestCase):  # N3
    def test_never_bind_of_alt_space_names_alt_space(self) -> None:
        km = load('version = 1\n[safety]\nnever_bind = ["alt+space"]\n[bindings.runs]\n"alt+space" = "runs.next"\n')
        self.assertTrue(any("Alt+Space is bound nowhere" in n for n in km.notes), km.notes)
        self.assertFalse(any("alt+  is" in n for n in km.notes), km.notes)


if __name__ == "__main__":
    unittest.main()
