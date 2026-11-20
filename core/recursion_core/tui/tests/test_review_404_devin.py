"""The five Devin threads on #404 at 256599ad, answered at the Liaison's ruling on Q22 (Nov 15, 11:14 PM). Each test
fails on 256599ad.
T3 (keymap.py:309-317): a plugin action bound in the command line or the keys view was taken and did nothing; it
is refused there. T4 (app.py:644): a search match on a line taller than the pane stayed off screen. T5
(app.py:546-552): a binding beginning with a key that types text lost that key from the command line; such a
binding is refused there. T6 (keymap.py:173): a printable but invisible character was a key name the decoder never
gives. T7 (keymap.py:289-293): what "none" does where the global bindings also apply is said in the README.
"""

from __future__ import annotations

import pathlib
import unittest

from recursion_core.tui import keymap as keymap_module
from recursion_core.tui.keymap import load
from recursion_core.tui.tests.test_keymap import new_app


class PluginContextTest(unittest.TestCase):  # T3
    def test_a_plugin_action_is_refused_where_no_leader_sequence_is_typed(self) -> None:
        for context in ("command", "help"):
            with self.subTest(context=context):
                km = load(f'version=1\n[bindings.{context}]\n"<leader> p x" = "plugin.foo.bar"')
                self.assertNotIn((km.leader, "p", "x"), km.bindings.get(context, {}))
                self.assertTrue(any("plugin actions are bound only where a leader sequence is typed" in n
                                    for n in km.notes), km.notes)
        km = load('version=1\n[bindings.runs]\n"<leader> p x" = "plugin.foo.bar"')
        self.assertEqual(km.bindings["runs"][(km.leader, "p", "x")], "plugin.foo.bar", "where it is typed, it is")


class SearchTallLineTest(unittest.TestCase):  # T4
    def test_a_match_on_a_line_taller_than_the_pane_is_in_the_pane(self) -> None:
        app, _ = new_app()
        app.resize(30, 30)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "NEEDLE " + "x" * 450 + "\n"})
        app.frame()
        app._search("NEEDLE", 0)
        pane = [text[2:] for text, _ in app.frame() if text.startswith("│ ")]
        self.assertTrue(any("NEEDLE" in row for row in pane), pane)


class CommandPrefixTest(unittest.TestCase):  # T5
    def test_a_binding_that_begins_with_a_typed_key_is_refused_on_the_command_line(self) -> None:
        km = load('version=1\n[bindings.command]\n"g g" = "line.home"')
        self.assertNotIn(("g", "g"), km.bindings.get("command", {}))
        self.assertTrue(any("types text on the command line" in n for n in km.notes), km.notes)
        app, _ = new_app(km)
        app.press(":")
        for ch in "status run-go":
            app.press(ch)
        self.assertEqual(app.line, "status run-go")


class InvisibleKeyTest(unittest.TestCase):  # T6
    def test_a_character_the_decoder_never_gives_is_no_key(self) -> None:
        self.assertIsNone(keymap_module.key_name("͏"))
        km = load('version=1\n[bindings.runs]\n"͏" = "runs.refresh"')
        self.assertNotIn(("͏",), km.bindings.get("runs", {}))
        self.assertTrue(km.notes)
        self.assertEqual(keymap_module.key_name(" "), " ", "a space is still a key")


class NoneSentenceTest(unittest.TestCase):  # T7
    def test_the_readme_says_what_none_does_where_global_bindings_apply(self) -> None:
        readme = " ".join((pathlib.Path(keymap_module.__file__).parent / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("in `runs` and `output` a global binding of the same keys then applies", readme)
        self.assertIn("`[safety] never_bind` takes a key away everywhere", readme)


if __name__ == "__main__":
    unittest.main()
