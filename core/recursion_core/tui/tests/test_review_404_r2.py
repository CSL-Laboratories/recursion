"""Review 2 of #404 (the keymap at ca43dbae): both reads' findings, each as a test that fails on that head
(the coverage that Astra's required change 5 and DeepSeek's L2 ask for passes there by design: it holds rules
that no test held).

GPT-6 Astra's read: 1 a binding file's ``[safety] never_bind`` cannot take Esc's cancel away (DeepSeek's M1
too), 2 the open keys show entries on a 13-row terminal and never name an empty range, 3 a frame wraps only
the rows it shows, not the whole history, 4 a key name is judged as the key a terminal sends (``ctrl+i`` is
Tab), 5 six rules that no test held. DeepSeek V4.1 Flash's read: M1 (as Astra 1), L1 a new leader keeps the
bindings of ``base``, L2 the keys' clamp after a resize, L3 a wrong type given to ``bind`` or ``press`` is
said, not raised, L4 the pager's sentences in rows, L5 a binding added or output handed in while another
client reads, L6 an output event whose text is not text.
"""

from __future__ import annotations

import re
import threading
import unittest
from unittest import mock

from recursion_core.tui import app as app_module
from recursion_core.tui import keymap as km_mod
from recursion_core.tui.app import TuiApp, _Output
from recursion_core.tui.keymap import Keymap, load
from recursion_core.tui.keys import KeyDecoder
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.test_keymap import new_app, press_all

BAR = chr(0x2502)  # the output pane's left edge
WIDE, COMBINING = chr(0x754C), "e" + chr(0x301)


def pane(app: TuiApp) -> list[str]:
    return [text[2:] for text, _ in app.frame() if text.startswith(BAR + " ")]


def blank_pane_rows(app: TuiApp) -> int:
    return sum(1 for text, _ in app.frame() if text == BAR)


def keys_header(app: TuiApp) -> str | None:
    return next((text for text, _ in app.frame() if text.startswith("-- keys")), None)


def decoder_names() -> set[str]:
    """Every key name the decoder makes of what a terminal sends: each ASCII byte alone and after ESC (Alt),
    and every CSI, SS3 and tilde sequence it knows, with each xterm modifier."""

    names: set[str] = set()

    def take(data: bytes) -> None:
        decoder = KeyDecoder()
        names.update(k for k in decoder.feed(data) + decoder.timeout() if isinstance(k, str))

    for b in range(0x80):
        take(bytes([b]))
        take(b"\x1b" + bytes([b]))
    for final in b"ABCDHFPQRS":
        take(b"\x1b[" + bytes([final]))
        take(b"\x1bO" + bytes([final]))
        for mod in range(2, 9):
            take(b"\x1b[1;%d" % mod + bytes([final]))
    take(b"\x1bOM")
    take(b"\x1b[Z")
    for code in (1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24):
        take(b"\x1b[%d~" % code)
        for mod in range(2, 9):
            take(b"\x1b[%d;%d~" % (code, mod))
    return names


class EscStaysTheCancelTest(unittest.TestCase):  # Astra 1, DeepSeek M1
    FILE = 'version = 1\n[safety]\nnever_bind = ["esc"]\n'

    def test_a_file_whose_never_bind_holds_esc_is_not_used(self) -> None:
        km = load(self.FILE)
        self.assertTrue(any("binding file not used" in n and "never_bind cannot hold esc" in n for n in km.notes),
                        km.notes)
        self.assertEqual(km.bindings, Keymap.default().bindings, "the defaults stand")

    def test_esc_still_leaves_the_line_the_keys_and_the_pager(self) -> None:
        app, api = new_app(load(self.FILE))
        press_all(app, [":", *"stop run-1", "esc"])
        self.assertEqual((app.mode, app.line), ("keys", ""))
        app.press("enter")  # on the run list Enter attaches (reads run.status); the line is gone
        press_all(app, ["?", "esc"])
        self.assertEqual(app.mode, "keys")
        press_all(app, ["tab", "esc"])
        self.assertEqual(app.focus, "runs")
        self.assertEqual([m for m, _ in api.calls], ["run.status"], "no run.stop: the line never sent")

    def test_esc_with_a_sequence_pending_still_leaves_the_line(self) -> None:
        app, api = new_app(load('version = 1\n[safety]\nnever_bind = ["esc"]\n'
                                '[bindings.command]\n"f2 f3" = "line.home"\n'))
        press_all(app, [":", "x", "f2", "esc"])
        self.assertEqual((app.mode, app.line, app.keymap.pending), ("keys", "", ()))
        self.assertEqual(api.calls, [])


class ShortTerminalKeysTest(unittest.TestCase):  # Astra 2
    def test_the_keys_show_entries_on_a_thirteen_row_terminal(self) -> None:
        app, _ = new_app()
        app.resize(100, 13)
        app.press("?")
        every = app._help_rows()
        self.assertEqual(len(pane(app)), 3, "the run list gave rows to the keys until three show")
        self.assertIn(f"rows 1-3 of {len(every)}", keys_header(app))
        self.assertTrue(any(text == "runs:" for text, _ in app.frame()), "the run list keeps the rows left")
        seen: list[str] = []
        for _ in range(len(every)):
            seen += pane(app)
            app.press("pagedown")
        self.assertEqual(set(seen), set(every), "every entry was shown")
        self.assertEqual(pane(app)[-1], every[-1])

    def test_no_empty_range_is_ever_named(self) -> None:
        for height in range(6, 32):
            app, _ = new_app()
            app.resize(100, height)
            app.press("?")
            for key in ["pagedown"] * 25 + ["end", "home", "G"]:
                head, shown = keys_header(app), pane(app)
                with self.subTest(height=height, key=key):
                    found = re.search(r"rows (\d+)-(\d+) of (\d+)", head or "")
                    if found:
                        first, last, total = map(int, found.groups())
                        self.assertTrue(1 <= first <= last <= total, head)
                        self.assertEqual(last - first + 1, len(shown), head)
                    elif head is not None and not shown:
                        self.assertIn("no room to show them here", head)
                app.press(key)

    def test_the_run_list_keeps_its_rows_when_the_keys_are_not_open(self) -> None:
        app, _ = new_app()
        app.resize(100, 13)
        texts = [text for text, _ in app.frame()]
        first = texts.index("runs:")
        pane_head = next(i for i, text in enumerate(texts) if text.startswith("-- output"))
        self.assertEqual(pane_head - first - 1, app_module.RUN_ROWS, "only the open keys take the run list's rows")

    def test_a_terminal_with_no_row_for_an_entry_says_so(self) -> None:
        app, _ = new_app()
        app.resize(100, 7)
        app.press("?")
        self.assertEqual(pane(app), [])
        self.assertIn("no room to show them here", keys_header(app))
        self.assertNotIn("rows ", keys_header(app))


class FrameWorkTest(unittest.TestCase):  # Astra 3
    def test_a_following_frame_does_not_wrap_the_whole_history(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": ("x" * 1000 + "\n") * 2000})
        app.frame()  # the first frame at this width counts the history once
        wrapped: list[int] = []
        real = app_module.wrap

        def counted(text: str, width: int) -> list[str]:
            wrapped.append(len(text))
            return real(text, width)

        with mock.patch.object(app_module, "wrap", counted):
            for _ in range(3):
                app.frame()
            app.on_event({"run_id": "run-1", "text": "y" * 500 + "\nnew partial"})
            app.frame()
        self.assertLess(sum(wrapped), 100_000, f"{sum(wrapped)} characters wrapped for four frames of 2 MB kept")
        self.assertEqual(pane(app)[-1], "new partial")

    def test_a_scrolled_frame_wraps_only_the_lines_it_shows(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "".join(f"line {i:04} " + "x" * 990 + "\n" for i in range(2000))})
        press_all(app, ["tab", "home"])
        app.frame()
        wrapped: list[int] = []
        real = app_module.wrap
        with mock.patch.object(app_module, "wrap", lambda text, width: wrapped.append(len(text)) or real(text, width)):
            top = pane(app)
        self.assertTrue(top[-1].startswith("line 0000"), top[-1:])
        self.assertLess(sum(wrapped), 20_000, "the oldest rows are found by count, not by wrapping the rest")

    def test_the_rows_shown_are_the_rows_of_the_whole_history(self) -> None:
        out = _Output(7, "run-1")  # seven lines kept: older ones leave, and their counts with them
        text = "".join(f"{i} " + (WIDE + COMBINING + "x") * (i * 3 % 17) + "\n" for i in range(12))
        text += "\n\npartial " + WIDE * 30
        for chunk in (text[:40], text[40:41], text[41:200], text[200:], "tail\n", "", "more"):
            out.feed(chunk)
            for width in (27, 1, 3, 8, 27):  # a width change counts again; the same width keeps the counts
                every = [row for line in out.source() for row in wrap(line, width)]
                self.assertEqual(out.total_rows(width), len(every))
                for rows in (1, 4, 30):
                    for offset in sorted({*range(0, len(every) + 2, max(1, len(every) // 20)), len(every) - 1}):
                        end = max(0, len(every) - offset)
                        with self.subTest(chunk=chunk[:8], width=width, rows=rows, offset=offset):
                            self.assertEqual(out.tail(rows, width, offset), every[max(0, end - rows):end])


class TerminalKeyNamesTest(unittest.TestCase):  # Astra 4
    def test_a_leader_written_as_a_control_byte_is_judged_as_the_key_it_arrives_as(self) -> None:
        for leader, sent, arrives in (("ctrl+i", b"\t", "tab"), ("ctrl+j", b"\n", "enter"), ("ctrl+m", b"\r", "enter")):
            with self.subTest(leader=leader):
                self.assertEqual(KeyDecoder().feed(sent), [arrives])
                km = load(f'version = 1\nleader = "{leader}"\n')
                self.assertTrue(any("binding file not used" in n and f"a terminal sends it as '{arrives}'" in n
                                    for n in km.notes), km.notes)
                self.assertEqual(km.bindings, Keymap.default().bindings)

    def test_a_leader_written_as_a_control_byte_works_as_the_key_it_arrives_as(self) -> None:
        app, api = new_app(load('version = 1\nleader = "ctrl+h"\n'))
        self.assertEqual((app.keymap.notes, app.keymap.leader), ([], "backspace"))
        app.press(KeyDecoder().feed(b"\x7f")[0])
        app.press("q")
        self.assertTrue(app.closed, "Backspace then q closes: the leader arrived")
        self.assertEqual(api.calls, [])

    def test_every_name_the_decoder_makes_is_a_key_name_of_its_own(self) -> None:
        names = decoder_names()
        self.assertIn("ctrl+alt+right", names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertEqual(km_mod.key_name(name), name)

    def test_every_key_name_taken_is_one_the_decoder_makes(self) -> None:
        names = decoder_names()
        bases = ["enter", "tab", "backspace", "esc", "space", "up", "home", "pageup", "insert", "delete", "f1",
                 "f12", *(chr(c) for c in range(0x20, 0x7f)), *(f"ctrl+{chr(c)}" for c in range(0x60, 0x7f)),
                 "ctrl+space", "ctrl+\\", "ctrl+]", "ctrl+^", "ctrl+_", "ctrl+[", "shift+tab"]
        mods = ["", "ctrl+", "alt+", "shift+", "alt+shift+", "ctrl+shift+", "ctrl+alt+", "ctrl+alt+shift+"]
        candidates = {m + b for m in mods for b in bases} | {"ctrl+alt+" + b[5:] for b in bases if b.startswith("ctrl+")}
        taken = 0
        for name in sorted(candidates):
            canonical = km_mod.key_name(name)
            if canonical is not None:
                taken += 1
                with self.subTest(name=name):
                    self.assertIn(canonical, names, f"{name} is taken as {canonical}, which no terminal key is")
        self.assertGreater(taken, 250)
        for never in ("ctrl+enter", "shift+enter", "alt+esc", "ctrl+tab", "ctrl+backspace", "alt+shift+tab"):
            self.assertIsNone(km_mod.key_name(never), never)

    def test_a_binding_written_as_a_control_byte_acts_on_the_key_it_arrives_as(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"ctrl+i" = "runs.next"\n[bindings.global]\n"ctrl+h" = "help.show"\n')
        self.assertEqual(km.notes, [])
        app, api = new_app(km)
        app.press(KeyDecoder().feed(b"\t")[0])
        self.assertEqual((app.focus, app.selected), ("runs", "run-2"), "Tab selects: the file bound it")
        app.press(KeyDecoder().feed(b"\x08")[0])
        self.assertEqual(app.mode, "help")
        self.assertEqual(api.calls, [])

    def test_never_bind_names_the_key_as_it_arrives(self) -> None:
        km = load('version = 1\n[safety]\nnever_bind = ["ctrl+i"]\n')
        self.assertNotIn(("tab",), km.view("runs"))
        self.assertTrue(any("Tab removed: [safety] never_bind" in n for n in km.notes), km.notes)

    def test_never_bind_holding_the_leader_under_another_name_is_refused(self) -> None:
        km = load('version = 1\nleader = "backspace"\n[safety]\nnever_bind = ["ctrl+h"]\n')
        self.assertTrue(any("never_bind holds the leader" in n for n in km.notes), km.notes)
        self.assertEqual(km.leader, "ctrl+x", "the file is not used")

    def test_a_name_no_key_arrives_as_is_refused(self) -> None:
        km = load('version = 1\n[bindings.runs]\n"ctrl+enter" = "runs.next"\n"alt+esc" = "runs.prev"\n')
        self.assertEqual(len([n for n in km.notes if "unknown key" in n]), 2, km.notes)
        self.assertEqual(km.bindings, Keymap.default().bindings)


class UnheldRulesTest(unittest.TestCase):  # Astra 5 (passes on ca43dbae by design)
    def test_a_dirty_template_gives_three_clients_clean_matching_state(self) -> None:
        km = Keymap.default()
        km.feed("ctrl+x", "runs")
        km.last = ("old",)
        (a, a_api), (b, b_api), (c, c_api) = new_app(km), new_app(km), new_app(km)
        for app in (a, b, c):
            self.assertEqual((app.keymap.pending, app.keymap.last), ((), ()))
        a.press("ctrl+x", burst=True)
        b.press("ctrl+x")
        c.press("k")
        press_all(b, ["k", "y"])
        press_all(a, ["k", "y"])
        self.assertEqual(b_api.calls, [("run.stop", {"run_id": "run-1"})])
        self.assertEqual((a_api.calls, c_api.calls), ([], []))
        self.assertEqual(km.pending, ("ctrl+x",), "the template itself is left as it was")

    def help_open(self) -> TuiApp:
        app, _ = new_app()
        app.resize(100, 30)
        app.press("?")
        app.frame()
        return app

    def test_paging_past_the_last_keys_keeps_them_in_view(self) -> None:
        app = self.help_open()
        app.press("end")
        last = pane(app)
        self.assertIn("Esc never sends anything", "\n".join(last))
        for key in ["pagedown"] * 10 + ["j"] * 10:
            app.press(key)
            self.assertEqual(pane(app), last)

    def test_the_keys_cannot_scroll_before_the_first(self) -> None:
        app = self.help_open()
        first = pane(app)
        self.assertIn("keys, run list:", "\n".join(first))
        for key in ["k", "pageup", "ctrl+u"] * 3:
            app.press(key)
            self.assertEqual(pane(app), first)

    def test_reopening_the_keys_starts_at_the_first(self) -> None:
        app = self.help_open()
        app.press("end")
        app.frame()
        press_all(app, ["esc", "?"])
        self.assertEqual(app.help_scroll, 0)
        self.assertIn("keys, run list:", "\n".join(pane(app)))

    def test_up_returns_to_the_row_before(self) -> None:
        for up in ("k", "up"):
            app = self.help_open()
            first = pane(app)
            app.press("j")
            self.assertNotEqual(pane(app), first)
            app.press(up)
            self.assertEqual(pane(app), first)

    def test_page_up_returns_to_the_page_before(self) -> None:
        for down, up in (("pagedown", "pageup"), ("ctrl+d", "ctrl+u")):
            app = self.help_open()
            first = pane(app)
            app.press(down)
            self.assertNotEqual(pane(app), first)
            app.press(up)
            self.assertEqual(pane(app), first)


class LeaderMovesBaseTest(unittest.TestCase):  # DeepSeek L1
    def test_a_new_leader_keeps_what_base_carried(self) -> None:
        base = Keymap.default()
        self.assertIsNone(base.bind("runs", "x", "runs.refresh"))
        self.assertIsNone(base.bind("global", "<leader> z", "conditions.show"))
        km = load('version = 1\nleader = "f1"\n', base)
        self.assertEqual(km.notes, [])
        view = km.view("runs")
        self.assertEqual(view[("x",)], "runs.refresh", "base's own binding is kept")
        self.assertEqual(view[("f1", "z")], "conditions.show", "base's leader binding moves with the leader")
        self.assertEqual(view[("f1", "k")], "run.stop")
        self.assertNotIn(("ctrl+x", "z"), view)
        self.assertEqual((base.leader, base.view("runs")[("ctrl+x", "z")]), ("ctrl+x", "conditions.show"),
                         "base is left as it was")

    def test_only_a_leader_sequence_moves(self) -> None:
        base = load('version = 1\nleader = "ctrl+a"\n')  # the line keeps its own Ctrl+A
        km = load('version = 1\nleader = "f1"\n', base)
        self.assertEqual(km.notes, [])
        self.assertEqual(km.view("command")[("ctrl+a",)], "line.home", "the line's Ctrl+A is no leader sequence")
        self.assertNotIn(("f1",), km.view("command"))
        self.assertEqual(km.view("runs")[("f1", "k")], "run.stop")

    def test_never_bind_is_judged_against_the_leader_in_force(self) -> None:
        base = load('version = 1\nleader = "alt+x"\n')
        km = load('version = 1\n[safety]\nnever_bind = ["alt+x"]\n', base)
        self.assertTrue(any("never_bind holds the leader" in n for n in km.notes), km.notes)
        self.assertEqual(km.view("runs")[("alt+x", "k")], "run.stop", "the leader's sequences stand")


class KeysClampTest(unittest.TestCase):  # DeepSeek L2 (passes on ca43dbae by design)
    def test_a_taller_terminal_shows_the_last_keys_at_the_bottom_of_a_full_pane(self) -> None:
        app, _ = new_app()
        app.resize(100, 14)
        app.press("?")
        app.frame()
        app.press("G")
        app.frame()
        app.resize(100, 40)
        every, shown = app._help_rows(), pane(app)
        self.assertEqual(shown[-1], every[-1])
        self.assertEqual(blank_pane_rows(app), 0, "the pane is full")
        self.assertEqual(app.help_scroll, len(every) - len(shown))
        self.assertIn(f"-{len(every)} of {len(every)}", keys_header(app))


class WrongTypesTest(unittest.TestCase):  # DeepSeek L3
    def test_bind_says_what_is_wrong_with_a_wrong_type(self) -> None:
        km = Keymap.default()
        for seq, action in (("x", None), ("x", 5), (5, "runs.next"), (["x"], "runs.next")):
            with self.subTest(seq=seq, action=action):
                self.assertIn("are text", km.bind("runs", seq, action))
        self.assertEqual(km.bindings, Keymap.default().bindings)

    def test_a_press_of_something_that_is_no_key_is_an_error_not_a_crash(self) -> None:
        app, api = new_app()
        for key in (["j"], ("j",), 5, None):
            with self.subTest(key=key):
                errors = len(app.errors)
                app.press(key)
                self.assertEqual(len(app.errors), errors + 1)
                self.assertIn("is not a key name or a paste; nothing done", app.errors[-1])
        self.assertEqual((app.selected, app.mode, api.calls), ("run-1", "keys", []))
        app.press("j")
        self.assertEqual(app.selected, "run-2", "the app still takes keys")


class RowSentencesTest(unittest.TestCase):  # DeepSeek L4
    def test_the_search_says_it_moves_by_display_rows(self) -> None:
        self.assertIn("last display row becomes the pager's", TuiApp._search.__doc__)
        self.assertNotIn("newest line.", TuiApp._search.__doc__)


class ConcurrentReadersTest(unittest.TestCase):  # DeepSeek L5
    def test_a_binding_added_while_a_client_reads_the_table_does_not_break_its_key(self) -> None:
        km = Keymap.default()
        client = km.for_client()

        class Growing(dict):  # another client binds while this one walks the table
            def items(self):
                for n, item in enumerate(dict.items(self)):
                    if n == 0:
                        self[("f9",)] = "runs.refresh"
                    yield item

            def __iter__(self):
                for n, seq in enumerate(dict.__iter__(self)):
                    if n == 0:
                        self[("f7",)] = "runs.refresh"
                    yield seq

        km.bindings["runs"] = Growing(km.bindings["runs"])
        self.assertEqual(client.feed("j", "runs"), "runs.next")
        self.assertIsNone(km.bind("runs", "f8", "runs.refresh"))

    def test_output_handed_in_from_another_thread_waits_for_the_frame(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "one\ntwo\n"})
        out = app.outputs["run-1"]
        real_tail, seen = out.tail, {}

        def tail(*args, **kwargs):
            worker = threading.Thread(target=app.on_event, args=({"run_id": "run-1", "text": "three\n"},))
            worker.start()
            worker.join(0.2)  # time is used only to show that something has not happened yet
            seen.update(worker=worker, waited=worker.is_alive(), lines=list(out.lines))
            return real_tail(*args, **kwargs)

        out.tail = tail
        app.frame()
        seen["worker"].join()  # the frame is made, so the lock is free and the worker ends
        self.assertTrue(seen["waited"], "the other thread's output waited for the frame")
        self.assertEqual(seen["lines"], ["one", "two"])
        self.assertIn("three", list(out.lines))


class OutputTextTest(unittest.TestCase):  # DeepSeek L6
    def test_an_output_event_whose_text_is_not_text_is_an_error(self) -> None:
        app, _ = new_app()
        app.attach("run-1")
        for text in (None, 5, ["a"]):
            with self.subTest(text=text):
                errors = len(app.errors)
                app.on_event({"run_id": "run-1", "text": text})
                self.assertEqual(len(app.errors), errors + 1)
                self.assertIn("skipped: its text is a", app.errors[-1])
        self.assertNotIn("run-1", app.outputs, "nothing was counted as output")
        app.on_event({"run_id": "run-1", "text": "fine\n"})
        self.assertEqual(app.outputs["run-1"].events, 1)
        self.assertIn("fine", pane(app))


if __name__ == "__main__":
    unittest.main()
