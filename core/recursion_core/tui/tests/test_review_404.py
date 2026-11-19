"""Review 1 of #404 (the keymap at 33458e28): both reads' findings, each as a test that fails on that head
(the guard coverage of R9 passes there by design: it holds rules that had no test).

DeepSeek V4.1 Flash's break-it read: M1 per-client matching state in a shared Keymap, M2 the pager offset
after an attach, M3 a leader that default bindings use, M4 Esc with a sequence pending on the line, in the
help or in the pager, and L1 to L6. GPT-6 Astra's read: R1 Esc only cancels, R2 wrapped rows reachable,
R3 an anchored view across partial lines, R4 the status command's run change, R5 every help row readable,
R6 and R7 (the same as M3 and M1), R8 SPINE's plugin names, R9 seven guards no test held.
"""

from __future__ import annotations

import io
import unittest

from recursion_core.tui import term
from recursion_core.tui.keymap import Keymap, load
from recursion_core.tui.keys import Paste
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.test_keymap import new_app


class SharedKeymapTest(unittest.TestCase):  # M1
    def test_two_clients_given_one_keymap_do_not_share_a_pending_sequence(self) -> None:
        km = Keymap.default()
        first, _ = new_app(km)
        second, second_api = new_app(km)
        first.press("ctrl+x")
        self.assertEqual(first.keymap.pending, ("ctrl+x",))
        second.press("k")
        self.assertEqual((second.mode, second.selected), ("keys", "run-2"), "k is the second client's own key")
        self.assertEqual(second_api.calls, [])
        self.assertEqual(km.pending, (), "the shared table holds no client's keys")
        first.press("k")
        self.assertEqual(first.mode, "confirm_stop", "the first client's sequence is still its own")
        self.assertIs(first.keymap.bindings, km.bindings, "the bindings are shared, not copied")


class AttachTest(unittest.TestCase):  # M2
    def test_attaching_another_run_shows_its_newest_lines(self) -> None:
        app, _ = new_app()
        app.resize(100, 20)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "".join(f"one {i}\n" for i in range(60))})
        app.on_event({"run_id": "run-2", "text": "two 0\ntwo 1\ntwo 2\n"})
        app.press("tab")
        for _ in range(25):
            app.press("k")
        app.press("/")
        for ch in "one":
            app.press(ch)
        app.press("enter")
        self.assertGreater(app.scroll, 0)
        app.attach("run-2")
        self.assertEqual((app.scroll, app.search), (0, ""))
        frame = "\n".join(t for t, _ in app.frame())
        self.assertIn("two 2", frame)
        self.assertNotIn("rows up", frame)


class LeaderTest(unittest.TestCase):  # M3
    def test_a_leader_the_default_bindings_use_is_refused(self) -> None:
        for leader in ("tab", "up", "down", "home", "end", "pageup", "pagedown", "ctrl+d", "ctrl+u"):
            with self.subTest(leader=leader):
                km = load(f'version = 1\nleader = "{leader}"\n')
                self.assertTrue(any("binding file not used" in n and "a key the default bindings use" in n
                                    for n in km.notes), km.notes)
                self.assertEqual(km.bindings, Keymap.default().bindings, "the defaults stand")

    def test_a_leader_no_default_uses_where_leaders_apply_is_taken(self) -> None:
        for leader in ("f1", "ctrl+a", "ctrl+g"):  # Ctrl+A is the line's own key; the line sees no leader
            with self.subTest(leader=leader):
                app, api = new_app(load(f'version = 1\nleader = "{leader}"\n'))
                self.assertEqual(app.keymap.notes, [])
                app.press(leader)
                app.press("q")
                self.assertTrue(app.closed)
                self.assertEqual(api.calls, [])

    def test_the_leader_is_not_bound_alone_where_leaders_apply(self) -> None:
        for context, action in (("runs", "runs.next"), ("global", "help.show"), ("output", "pager.top")):
            with self.subTest(context=context):
                km = load(f'version = 1\n[bindings.{context}]\n"ctrl+x" = "{action}"\n')
                self.assertTrue(any("the leader is not bound alone" in n for n in km.notes), km.notes)
                app, _ = new_app(km)
                if context == "output":
                    app.press("tab")
                app.press("ctrl+x")
                app.press("k")
                self.assertEqual(app.mode, "confirm_stop", "the leader still starts its sequences")
        km = load('version = 1\n[bindings.command]\n"ctrl+x" = "line.home"\n')
        self.assertEqual(km.notes, [], "the line sees no leader, so the key is free there")


class EscWithASequencePendingTest(unittest.TestCase):  # M4
    def test_esc_leaves_the_line_even_with_a_sequence_pending(self) -> None:
        app, api = new_app(load('version = 1\n[bindings.command]\n"g g" = "line.home"\n'))
        app.press(":")
        for ch in "ab":
            app.press(ch)
        app.press("g")
        self.assertEqual(app.keymap.pending, ("g",))
        app.press("esc")
        self.assertEqual((app.mode, app.line, app.keymap.pending), ("keys", "", ()))
        self.assertIn("g cancelled; nothing sent", app.notice)
        self.assertEqual(api.calls, [])

    def test_esc_leaves_the_help_even_with_a_sequence_pending(self) -> None:
        app, _ = new_app(load('version = 1\n[bindings.help]\n"g g" = "help.close"\n'))
        app.press("?")
        app.press("g")
        app.press("esc")
        self.assertEqual((app.mode, app.keymap.pending), ("keys", ()))

    def test_esc_leaves_the_pager_even_with_a_sequence_pending(self) -> None:
        app, api = new_app()
        app.press("tab")
        app.press("g")
        self.assertEqual(app.keymap.pending, ("g",))
        app.press("esc")
        self.assertEqual((app.focus, app.keymap.pending), ("runs", ()))
        self.assertIn("g cancelled; nothing sent", app.notice)
        self.assertEqual(api.calls, [])

    def test_on_the_run_list_esc_only_cancels_the_sequence(self) -> None:
        app, api = new_app()
        app.press("ctrl+x")
        app.press("esc")
        self.assertIn("Ctrl+X cancelled; nothing done", app.notice)
        self.assertEqual((app.mode, app.focus, api.calls), ("keys", "runs", []))


class BindingFileTest(unittest.TestCase):  # L1, L2, L4, L6
    def test_load_leaves_its_base_unchanged(self) -> None:
        base = Keymap.default()
        first = load('version = 1\n[bindings.runs]\n"x" = "runs.refresh"\n', base)
        self.assertIsNot(first, base)
        self.assertNotIn(("x",), base.view("runs"))
        second = load('version = 1\n[bindings.runs]\n"z" = "no.such"\n', base)
        self.assertNotIn(("x",), second.view("runs"), "a second file does not keep the first file's bindings")
        self.assertEqual(base.notes, [])
        self.assertEqual(len(second.notes), 1)

    def test_the_version_is_the_integer_one(self) -> None:
        for text in ("version = true\n", "version = 1.0\n", 'version = "1"\n'):
            with self.subTest(text=text):
                km = load(text)
                self.assertTrue(any("version must be 1" in n for n in km.notes), km.notes)
        self.assertEqual(load("version = 1\n").notes, [])

    def test_an_empty_keys_path_is_a_usage_error(self) -> None:
        for argv in (["--keys="], ["--keys", ""]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                code = term.main(argv, None, stdin=io.StringIO(), stdout=io.StringIO(), stderr=err, environ={})
                self.assertEqual(code, 2)
                self.assertIn("usage error", err.getvalue())

    def test_a_path_with_a_nul_is_a_note_not_a_crash(self) -> None:
        km = term.load_keymap("keys\x00.toml", explicit=True)
        self.assertTrue(any("could not be read (ValueError)" in n for n in km.notes), km.notes)
        self.assertEqual(km.bindings, Keymap.default().bindings)


class PasteNoticeTest(unittest.TestCase):  # L3
    def test_the_paste_notice_names_where_the_paste_went(self) -> None:
        app, _ = new_app()
        app.press(Paste("x"))
        self.assertIn("the run list takes keys", app.notice)
        app.press("tab")
        app.press(Paste("x"))
        self.assertIn("the output pager takes keys", app.notice)
        app.press("tab")
        app.press("?")
        app.press(Paste("x"))
        self.assertIn("the keys view takes keys", app.notice)
        self.assertNotIn("press :", app.notice, "the keys view has no command line")
        self.assertEqual(app.mode, "help")


class ContextsTest(unittest.TestCase):  # L5: the sentence now says what the code does
    def test_the_line_and_the_help_use_their_own_bindings_only(self) -> None:
        km = Keymap.default()
        for focus in ("command", "help"):
            with self.subTest(focus=focus):
                self.assertEqual(set(km.view(focus)), set(km.bindings[focus]))


BAR = chr(0x2502) + " "  # the output pane's row prefix


def pane_rows(app) -> list[str]:
    return [t[len(BAR):] for t, _ in app.frame() if t.startswith(BAR) and t[len(BAR):].strip()]


# GPT-6 Astra's read (R1 to R9); the cases follow its evidence, test_review404_requirements.py,
# test_plugin_contract.py and test_guard_coverage.py, with credit.
class EscOnlyCancelsTest(unittest.TestCase):  # R1
    def test_esc_cannot_submit_a_line_through_a_binding_file(self) -> None:
        km = load('version = 1\n[bindings.command]\n"esc" = "line.submit"\n')
        self.assertTrue(any("Esc never sends a request or arms anything" in n for n in km.notes), km.notes)
        app, api = new_app(km)
        for key in [":", *"stop run-1", "esc"]:
            app.press(key)
        self.assertEqual((api.calls, app.mode), ([], "keys"), "Esc left the line and sent nothing")

    def test_esc_cannot_arm_a_stop_through_a_binding_file(self) -> None:
        for context in ("runs", "global"):
            with self.subTest(context=context):
                app, api = new_app(load(f'version = 1\n[bindings.{context}]\n"esc" = "run.stop"\n'))
                app.press("esc")
                self.assertEqual(app.mode, "keys")
                app.press("y")
                self.assertEqual(api.calls, [])

    def test_esc_is_bound_only_alone_to_the_cancel_of_its_context(self) -> None:
        refused = ('[bindings.output]\n"esc" = "pager.top"', '[bindings.help]\n"esc" = "help.down"',
                   '[bindings.runs]\n"g esc" = "runs.next"', '[bindings.global]\n"esc x" = "help.show"',
                   '[bindings.global]\n"<leader> p esc" = "plugin.example.run"', '[bindings.command]\n"esc" = "none"')
        for body in refused:
            with self.subTest(body=body):
                km = load(f"version = 1\n{body}\n")
                self.assertEqual(len(km.notes), 1, km.notes)
                self.assertTrue("Esc" in km.notes[0], km.notes)
                self.assertEqual(km.bindings, Keymap.default().bindings, "Esc still cancels")
        km = load('version = 1\n[bindings.command]\n"esc" = "line.cancel"\n[bindings.output]\n"esc" = "focus.runs"\n')
        self.assertEqual(km.notes, [], "restating Esc's own cancel is fine")


class WrappedRowsTest(unittest.TestCase):  # R2
    def test_the_first_row_of_one_long_line_is_reachable(self) -> None:
        for keys in (["g", "g"], ["home"], ["pageup"] * 40, ["k"] * 40):
            with self.subTest(keys=keys[:2]):
                app, _ = new_app()
                app.resize(45, 18)
                app.attach("run-1")
                app.on_event({"run_id": "run-1", "text": "UNIQUE_HEAD " + "x" * 1000 + " UNIQUE_TAIL\n"})
                app.frame()
                app.press("tab")
                for key in keys:
                    app.press(key)
                self.assertIn("UNIQUE_HEAD", "\n".join(pane_rows(app)))
                app.press("G")
                self.assertIn("UNIQUE_TAIL", "\n".join(pane_rows(app)))

    def test_every_row_is_shown_once_stepping_from_the_top(self) -> None:
        app, _ = new_app()
        app.resize(30, 14)
        app.attach("run-1")
        text = "".join(f"L{i}:" + "y" * (i * 7) + "\n" for i in range(12))
        app.on_event({"run_id": "run-1", "text": text})
        app.frame()
        app.press("tab")
        app.press("home")
        seen = [pane_rows(app)[-1]]
        while app.scroll:
            app.press("j")
            seen.append(pane_rows(app)[-1])
        expected = [row for line in text.splitlines() for row in wrap(line, 28)]
        self.assertEqual(seen, expected, "each display row was the pane's newest once, in order")

    def test_a_search_lands_on_the_matching_line_past_wrapped_ones(self) -> None:
        app, _ = new_app()
        app.resize(30, 20)
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "the target\n" + "w" * 120 + "\nlast\n"})
        app.frame()
        for key in ("tab", "/", *"target", "enter"):
            app.press(key)
        self.assertEqual(pane_rows(app)[-1], "the target", "the match is the pane's newest row")
        app.press("n")  # the only match: n wraps around to it again
        self.assertEqual(pane_rows(app)[-1], "the target")


class AnchoredViewTest(unittest.TestCase):  # R3
    def scrolled(self, text: str):
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": text})
        app.press("tab")
        app.press("k")
        self.assertEqual(pane_rows(app)[-1], "two")
        return app

    def test_finishing_a_partial_line_keeps_the_view(self) -> None:
        app = self.scrolled("one\ntwo\npartial")
        app.on_event({"run_id": "run-1", "text": "\n"})
        self.assertEqual(pane_rows(app)[-1], "two")

    def test_beginning_and_growing_a_partial_line_keeps_the_view(self) -> None:
        app = self.scrolled("one\ntwo\nthree\n")
        app.on_event({"run_id": "run-1", "text": "new unfinished"})
        self.assertEqual(pane_rows(app)[-1], "two")
        app.on_event({"run_id": "run-1", "text": " " + "z" * 300})  # the partial wraps onto more rows
        self.assertEqual(pane_rows(app)[-1], "two")
        app.on_event({"run_id": "run-1", "text": "\nfour\n"})
        self.assertEqual(pane_rows(app)[-1], "two")

    def test_a_view_on_rows_that_retention_dropped_shows_the_oldest_kept(self) -> None:
        app, _ = new_app()
        app._output_lines = 5  # the run's output keeps five lines
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "".join(f"old {i}\n" for i in range(5))})
        for key in ("tab", "g", "g"):
            app.press(key)
        self.assertEqual(pane_rows(app)[-1], "old 0")
        app.on_event({"run_id": "run-1", "text": "".join(f"new {i}\n" for i in range(5))})
        self.assertEqual(pane_rows(app)[-1], "new 0", "the oldest row kept, not an empty pane")


class StatusCommandTest(unittest.TestCase):  # R4
    def test_status_of_another_run_starts_at_its_newest_output(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "".join(f"first {i}\n" for i in range(30))})
        for key in ("tab", "g", "g", "esc"):
            app.press(key)
        app.on_event({"run_id": "run-2", "text": "second current\n"})
        app.command("status run-2")
        self.assertEqual(app.scroll, 0)
        self.assertIn("second current", "\n".join(pane_rows(app)))

    def test_status_of_the_same_run_keeps_the_view(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "".join(f"first {i}\n" for i in range(30))})
        app.press("tab")
        app.press("k")
        app.command("status run-1")
        self.assertEqual(app.scroll, 1)


class HelpReachableTest(unittest.TestCase):  # R5
    def test_every_help_row_can_be_read_in_full_screen(self) -> None:
        for keys in (["pagedown"] * 20, ["j"] * 200, ["end"], ["G"], ["ctrl+d"] * 20):
            with self.subTest(keys=keys[0]):
                app, _ = new_app()
                app.resize(100, 30)
                app.press("?")
                seen = "\n".join(t for t, _ in app.frame())
                for key in keys:
                    app.press(key)
                    seen += "\n" + "\n".join(t for t, _ in app.frame())
                self.assertIn("Esc never sends anything", seen, "the last help row")
                self.assertEqual(app.mode, "help")
        app.press("home")
        self.assertIn("keys, run list:", "\n".join(t for t, _ in app.frame()))
        self.assertIn("j/k and Page Up/Down scroll", "\n".join(t for t, _ in app.frame()))


class PluginNamesTest(unittest.TestCase):  # R8
    def test_spines_plugin_ids_take_leader_bindings(self) -> None:
        for plugin_id in ("example..tools", "example.", "ex-ample_1.x"):
            with self.subTest(plugin_id=plugin_id):
                action = f"plugin.{plugin_id}.run"
                km = load(f'version = 1\n[bindings.global]\n"<leader> p r" = "{action}"\n')
                self.assertEqual(km.notes, [])
                app, api = new_app(km)
                for key in ("ctrl+x", "p", "r"):
                    app.press(key)
                self.assertIn(action, app.notice)
                self.assertEqual(api.calls, [])

    def test_names_outside_spines_rules_are_refused(self) -> None:
        for action in ("plugin.host.run", "plugin.Example.run", "plugin.x.run", "plugin.example.Run",
                       "plugin.example.run!", "plugin.example", "plugin..run", "plugin.example."):
            with self.subTest(action=action):
                km = load(f'version = 1\n[bindings.global]\n"<leader> p r" = "{action}"\n')
                self.assertTrue(any("in SPINE's names" in n for n in km.notes), km.notes)


class BindingFileShapeTest(unittest.TestCase):  # R9: the seven guards no test held
    def refused_whole_file(self, text: str) -> None:
        km = load(text)
        self.assertEqual(km.bindings, Keymap.default().bindings)
        self.assertTrue(any("binding file not used" in n for n in km.notes), km.notes)

    def test_bindings_and_safety_must_be_tables(self) -> None:
        self.refused_whole_file("version = 1\nbindings = []\n")
        self.refused_whole_file("version = 1\nsafety = []\n")

    def test_never_bind_is_a_list_of_key_names(self) -> None:
        for value in ('"ctrl+x"', "[1]", '["not-a-key"]'):
            with self.subTest(value=value):
                self.refused_whole_file(f"version = 1\n[safety]\nnever_bind = {value}\n")

    def test_an_unknown_context_is_refused_for_plugins_too(self) -> None:
        km = load('version = 1\n[bindings.imaginary]\n"<leader> p x" = "plugin.example.run"\n')
        self.assertTrue(any("unknown context" in n for n in km.notes), km.notes)
        self.assertNotIn("imaginary", km.bindings)

    def test_a_context_must_be_a_table_and_an_action_text(self) -> None:
        for text in ("version = 1\n[bindings]\nruns = []\n", 'version = 1\n[bindings.runs]\n"x" = 42\n'):
            with self.subTest(text=text):
                km = load(text)
                self.assertEqual(km.bindings, Keymap.default().bindings)
                self.assertTrue(km.notes)

    def test_a_file_that_is_not_utf8_is_a_note(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "keys.toml")
            with open(path, "wb") as f:
                f.write(b"\xff")
            km = term.load_keymap(path, explicit=True)
        self.assertEqual(km.bindings, Keymap.default().bindings)
        self.assertTrue(any("could not be read (UnicodeDecodeError)" in n for n in km.notes), km.notes)


class OptionValueTest(unittest.TestCase):  # Astra, "can follow" 4
    def test_a_keys_value_is_a_path_even_when_it_looks_like_a_flag(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        made = []

        def factory():
            made.append(True)
            return new_app()[1]

        code = term.main(["--plain", "--keys", "--print-keys"], factory, stdin=io.StringIO("q\n"), stdout=out,
                         stderr=err, environ={})
        self.assertEqual(code, 0)
        self.assertTrue(made, "the client ran; --print-keys was the file's name, not a mode")
        self.assertIn("binding file not used: --print-keys does not exist", out.getvalue())


if __name__ == "__main__":
    unittest.main()
