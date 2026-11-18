"""Lane TUI slice 2: slice 1's input rules carried through the keymap.

Slice 1 (#372) marks a key "burst" when it came in one read with other input; a burst key cannot confirm a
Stop or send the command line, nor can the next key when the Stop was armed or the line typed with burst
input. With the keymap, a Stop or a line can be opened by a sequence (Ctrl+X k, Ctrl+X n), so the mark of a
sequence is that of all its keys. Ctrl+C ends the TUI from any mode before the keymap sees it, so no
binding and no leader may take it. Every key, whatever action it reaches, changes state under the app's
lock.
"""

from __future__ import annotations

import threading
import unittest

from recursion_core.tui.keymap import Keymap, load
from recursion_core.tui.keys import Paste
from recursion_core.tui.tests.test_keymap import new_app

STOP = ("run.stop", {"run_id": "run-1"})
START = "--tenant t1 --engine codex --prompt hi"


def type_text(app, text: str) -> None:
    for ch in text:
        app.press(ch)


class SequenceBurstTest(unittest.TestCase):
    def arm(self, keys, marks):
        app, api = new_app()
        for key, burst in zip(keys, marks, strict=True):
            app.press(key, burst=burst)
        self.assertEqual((app.mode, app.stop_target), ("confirm_stop", "run-1"))
        return app, api

    def test_a_stop_armed_by_a_sequence_with_any_burst_key_is_not_confirmed_by_the_next_y(self) -> None:
        for marks in ((True, False), (False, True), (True, True)):
            with self.subTest(marks=marks):
                app, api = self.arm(("ctrl+x", "k"), marks)
                app.press("y")
                self.assertEqual(api.calls, [])
                self.assertEqual(app.mode, "keys")
                self.assertIn("the Ctrl+X k or the y came in one read with other input", app.notice)
                self.assertIn("press Ctrl+X k and then y again", app.notice)
                app.press("ctrl+x")
                app.press("k")
                app.press("y")
                self.assertEqual(api.calls, [STOP], "the mark ended with its sequence and its Stop")
        app, api = self.arm(("ctrl+x", "k"), (False, False))
        app.press("y")
        self.assertEqual(api.calls, [STOP])

    def test_a_burst_y_after_a_clean_sequence_still_cancels(self) -> None:
        app, api = self.arm(("ctrl+x", "k"), (False, False))
        app.press("y", burst=True)
        self.assertEqual(api.calls, [])
        self.assertIn("the Ctrl+X k or the y came in one read", app.notice)

    def test_the_notice_names_the_sequence_in_force(self) -> None:
        km = load('version = 1\nleader = "ctrl+a"\n[bindings.runs]\n"s" = "none"\n"x x" = "run.stop"\n')
        for keys, said in ((("x", "x"), "x x"), (("ctrl+a", "k"), "Ctrl+A k")):
            with self.subTest(keys=keys):
                app, api = new_app(km)
                app.press(keys[0], burst=True)
                app.press(keys[1])
                self.assertEqual(app.mode, "confirm_stop")
                app.press("y")
                self.assertEqual(api.calls, [])
                self.assertIn(f"the {said} or the y came in one read", app.notice)
                self.assertIn(f"press {said} and then y again", app.notice)

    def test_a_key_read_again_after_a_broken_prefix_keeps_only_its_own_mark(self) -> None:
        # Ctrl+D waits for a second Ctrl+D; s breaks it and is read again on its own, as s.
        app, api = self.arm(("ctrl+d", "s"), (True, False))
        self.assertEqual(app.keymap.last, ("s",))
        app.press("y")
        self.assertEqual(api.calls, [STOP], "the burst Ctrl+D named nothing; the s alone armed the Stop")
        app, api = self.arm(("ctrl+d", "s"), (False, True))
        app.press("y")
        self.assertEqual(api.calls, [])
        self.assertIn("the s or the y came in one read", app.notice)

    def test_a_cancelled_or_broken_sequence_leaves_no_mark_behind(self) -> None:
        for ending in (["esc"], ["z"], [Paste("text")]):
            with self.subTest(ending=ending):
                app, api = new_app()
                app.press("ctrl+x", burst=True)
                for key in ending:
                    app.press(key)
                self.assertEqual(app.keymap.pending, ())
                app.press("ctrl+x")
                app.press("k")
                app.press("y")
                self.assertEqual(api.calls, [STOP])

    def test_a_long_sequence_keeps_the_mark_of_its_first_key(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"s" = "none"\n"x y z" = "run.stop"\n')
        app, api = new_app(km)
        for key, burst in (("x", True), ("y", False), ("z", False)):
            app.press(key, burst=burst)
        self.assertEqual(app.mode, "confirm_stop")
        app.press("y")
        self.assertEqual(api.calls, [])

    def test_reads_reached_by_burst_keys_still_read(self) -> None:
        # Slice 1 holds back only what a paste could send by itself: a Stop and the command line.
        app, api = new_app()
        app.press("ctrl+x", burst=True)
        app.press("r", burst=True)
        app.press("a", burst=True)
        self.assertEqual([m for m, _ in api.calls], ["run.list", "run.status"])


class LineBurstTest(unittest.TestCase):
    def test_a_line_opened_by_a_burst_key_or_sequence_waits_for_a_second_enter(self) -> None:
        openers = {":": ([":"], "list", "run.list"), "/": (["/"], "list", "run.list"),
                   "Ctrl+X n": (["ctrl+x", "n"], START, "run.start")}
        for name, (keys, text, method) in openers.items():
            for burst_at in range(len(keys)):
                with self.subTest(opener=name, burst_at=burst_at):
                    app, api = new_app()
                    for i, key in enumerate(keys):
                        app.press(key, burst=i == burst_at)
                    self.assertEqual(app.mode, "command")
                    type_text(app, text)
                    app.press("enter")
                    self.assertEqual(api.calls, [])
                    self.assertEqual(app.mode, "command", "the line stays, to be sent on purpose")
                    self.assertIn("press Enter to send it", app.notice)
                    app.press("enter")
                    self.assertEqual([m for m, _ in api.calls], [method])

    def test_a_line_opened_cleanly_is_sent_by_one_enter(self) -> None:
        for keys, text, method in (([":"], "list", "run.list"), (["ctrl+x", "n"], START, "run.start")):
            with self.subTest(keys=keys):
                app, api = new_app()
                for key in keys:
                    app.press(key)
                type_text(app, text)
                app.press("enter")
                self.assertEqual([m for m, _ in api.calls], [method])

    def test_the_pager_search_line_follows_the_same_rule(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "alpha\nbeta\n"})
        app.press("tab")
        app.press("/", burst=True)
        self.assertEqual((app.mode, app.line_kind), ("command", "search"))
        type_text(app, "alpha")
        app.press("enter")
        self.assertEqual(app.mode, "command")
        self.assertIn("press Enter to send it", app.notice)
        app.press("enter")
        self.assertEqual(app.mode, "keys")
        self.assertIn("search alpha: line 1 of 2", app.notice)

    def test_the_refusal_names_the_send_key_in_force(self) -> None:
        km = load('version = 1\n[bindings.command]\n"enter" = "none"\n"ctrl+s" = "line.submit"\n')
        app, api = new_app(km)
        app.press(":")
        type_text(app, "list")
        app.press("ctrl+s", burst=True)
        self.assertEqual(api.calls, [])
        self.assertIn("it or its Ctrl+S came in one read with other input", app.notice)
        self.assertIn("press Ctrl+S to send it", app.notice)
        app.press("ctrl+s")
        self.assertEqual([m for m, _ in api.calls], ["run.list"])

    def test_a_burst_key_of_a_pending_sequence_on_the_line_marks_it(self) -> None:
        km = load('version = 1\n[bindings.command]\n"ctrl+k ctrl+k" = "line.cancel"\n')
        app, api = new_app(km)
        app.press(":")
        type_text(app, "list")
        app.press("ctrl+k", burst=True)
        app.press("x")  # breaks the prefix; read again, typed
        self.assertEqual(app.line, "listx")
        app.press("backspace")
        app.press("enter")
        self.assertEqual(api.calls, [], "the burst Ctrl+K was input on this line")
        app.press("enter")
        self.assertEqual([m for m, _ in api.calls], ["run.list"])

    def test_a_burst_paste_marks_the_line_and_a_paste_breaks_a_pending_sequence(self) -> None:
        app, api = new_app()
        app.press(":")
        app.press(Paste("list"), burst=True)
        app.press("enter")
        self.assertEqual(api.calls, [])
        app.press("enter")
        self.assertEqual([m for m, _ in api.calls], ["run.list"])
        km = load('version = 1\n[bindings.command]\n"ctrl+k ctrl+k" = "line.cancel"\n')
        app, api = new_app(km)
        app.press(":")
        app.press("ctrl+k")
        app.press(Paste("x"))
        self.assertEqual(app.keymap.pending, (), "a paste breaks a pending sequence on the line too")
        app.press("ctrl+k")
        self.assertEqual((app.mode, app.line, app.keymap.pending), ("command", "x", ("ctrl+k",)))

    def test_esc_gives_the_next_line_a_fresh_mark(self) -> None:
        app, api = new_app()
        app.press(":", burst=True)
        app.press("esc")
        app.press("/")
        type_text(app, "list")
        app.press("enter")
        self.assertEqual([m for m, _ in api.calls], ["run.list"])


class CtrlCTest(unittest.TestCase):
    def test_ctrl_c_ends_the_tui_from_every_mode_and_pending_sequence(self) -> None:
        for before in ([], ["ctrl+x"], ["ctrl+d"], [":"], [":", "l"], ["?"], ["s"], ["tab"], ["tab", "g"],
                       ["tab", "/"]):
            with self.subTest(before=before):
                app, api = new_app()
                for key in before:
                    app.press(key)
                app.press("ctrl+c")
                self.assertTrue(app.closed)
                self.assertEqual(api.calls, [], "closing sends nothing, and an armed Stop is not sent")
                self.assertEqual(app.log.latest("close").event_type, "close")

    def test_no_binding_and_no_leader_can_take_ctrl_c(self) -> None:
        for context, seq in (("runs", "ctrl+c"), ("global", "<leader> ctrl+c"), ("output", "g ctrl+c"),
                             ("command", "ctrl+c")):
            with self.subTest(context=context, seq=seq):
                action = {"runs": "runs.refresh", "global": "runs.refresh", "output": "pager.top",
                          "command": "line.cancel"}[context]
                km = load(f'version = 1\n[bindings.{context}]\n"{seq}" = "{action}"\n')
                self.assertTrue(any("ctrl+c is kept: it ends the TUI from any mode" in n for n in km.notes), km.notes)
                self.assertFalse(any("ctrl+c" in s for table in km.bindings.values() for s in table))
        km = load('version = 1\nleader = "ctrl+c"\n')
        self.assertTrue(any("binding file not used" in n and "is kept" in n for n in km.notes), km.notes)
        self.assertEqual(km.bindings, Keymap.default().bindings)
        app, api = new_app(load('version = 1\n[bindings.runs]\n"ctrl+c" = "runs.refresh"\n'))
        app.press("ctrl+c")
        self.assertEqual((app.closed, api.calls), (True, []))

    def test_help_says_ctrl_c_closes(self) -> None:
        app, _ = new_app()
        app.press("?")
        self.assertIn("Ctrl+C closes the TUI from any mode", app.log.latest("help").text)


class KeymapLastTest(unittest.TestCase):
    def test_last_names_the_sequence_that_chose_the_action(self) -> None:
        km = Keymap.default()
        self.assertEqual(km.last, ())
        km.feed("ctrl+x", "runs")
        self.assertEqual(km.last, (), "a pending sequence chose nothing yet")
        self.assertEqual(km.feed("k", "runs"), "run.stop")
        self.assertEqual(km.last, ("ctrl+x", "k"))
        km.feed("ctrl+d", "runs")
        self.assertEqual(km.feed("s", "runs"), "run.stop")
        self.assertEqual(km.last, ("s",), "the key read again chose it alone")


class KeyLockTest(unittest.TestCase):
    def test_a_key_that_reaches_an_action_through_the_keymap_waits_for_the_state_lock(self) -> None:
        for keys, mode in ((["s"], "confirm_stop"), (["ctrl+x", "k"], "confirm_stop"), ([":"], "command"),
                           (["tab", "/"], "command")):
            with self.subTest(keys=keys):
                app, _ = new_app()
                for key in keys[:-1]:
                    app.press(key)
                with app._lock:
                    presser = threading.Thread(target=app.press, args=(keys[-1],))
                    presser.start()
                    presser.join(0.2)
                    self.assertTrue(presser.is_alive(), "the key changed state without the lock")
                    self.assertNotEqual(app.mode, mode)
                presser.join(5)
                self.assertFalse(presser.is_alive())
                self.assertEqual(app.mode, mode)

    def test_no_key_acts_after_close(self) -> None:
        for keys in (["ctrl+x", "k"], ["ctrl+x"], [":"], ["ctrl+d"]):
            with self.subTest(keys=keys):
                app, api = new_app()
                app.close()
                events = len(app.log)
                for key in [*keys, "y", "enter"]:
                    app.press(key)
                self.assertEqual((len(app.log), api.calls, app.mode), (events, [], "keys"))


if __name__ == "__main__":
    unittest.main()
