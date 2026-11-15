"""Lane TUI slice 1: terminal input decoding (frontier digest brief P5-1; TUI brief v3.3, build step 1).

The decoder is driven with bytes directly and, through ``term.Tty``, through an OS pipe: right arrow,
Home, Shift+Tab, Alt+x, a lone Esc and a bracketed paste of "stop run-1" decode as one key each; every
character of a multi-byte read comes through in order; an unknown sequence is dropped, never read as
Esc. In the ":" command line the cursor keys move a cursor and no longer clear the line. A key that came
in one read with other input cannot confirm a Stop or send the command line. Nothing here opens a real
terminal: the pipe stands in for it.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from recursion_core.tui import keys, term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.keys import KeyDecoder, Paste
from recursion_core.tui.tests.fakes import Clock, FakeDriver, RecordingAPI, ScriptedAPI
from recursion_core.tui.tests.test_inline import ALT_SCREEN, FakeTerminal, TtyOut

# select() on a pipe is POSIX only (Windows refuses it with WinError 10038): the tests that read a real
# pipe through the terminal driver skip there. The mocked reads beside them run everywhere.
posix_select = unittest.skipIf(os.name == "nt", "select() on a pipe is POSIX only")

# (bytes a terminal sends, the keys they decode to)
TABLE = [
    (b"\x1b[C", ["right"]), (b"\x1b[D", ["left"]), (b"\x1b[A", ["up"]), (b"\x1b[B", ["down"]),
    (b"\x1bOC", ["right"]), (b"\x1bOA", ["up"]),  # application cursor mode
    (b"\x1b[H", ["home"]), (b"\x1b[F", ["end"]), (b"\x1bOH", ["home"]), (b"\x1bOF", ["end"]),
    (b"\x1b[1~", ["home"]), (b"\x1b[7~", ["home"]), (b"\x1b[4~", ["end"]), (b"\x1b[8~", ["end"]),
    (b"\x1b[5~", ["pageup"]), (b"\x1b[6~", ["pagedown"]), (b"\x1b[2~", ["insert"]), (b"\x1b[3~", ["delete"]),
    (b"\x1b[Z", ["shift+tab"]), (b"\t", ["tab"]),
    (b"\x1bOP", ["f1"]), (b"\x1bOS", ["f4"]), (b"\x1b[15~", ["f5"]), (b"\x1b[24~", ["f12"]),
    (b"\x1b[1;5C", ["ctrl+right"]), (b"\x1b[1;2A", ["shift+up"]), (b"\x1b[1;3D", ["alt+left"]),
    (b"\x1b[1;6B", ["ctrl+shift+down"]), (b"\x1b[3;5~", ["ctrl+delete"]), (b"\x1b[1;2P", ["shift+f1"]),
    (b"\x1bx", ["alt+x"]), (b"\x1bX", ["alt+X"]), (b"\x1b\r", ["alt+enter"]), (b"\x1b\x7f", ["alt+backspace"]),
    (b"\x1b\x04", ["ctrl+alt+d"]),
    (b"\r", ["enter"]), (b"\n", ["enter"]), (b"\x7f", ["backspace"]), (b"\x08", ["backspace"]),
    (b"\x04", ["ctrl+d"]), (b"\x18", ["ctrl+x"]), (b"\x10", ["ctrl+p"]), (b"\x00", ["ctrl+space"]),
    (b"\x1c", ["ctrl+\\"]), (b"\x1f", ["ctrl+_"]),
    (b"stop run-1", list("stop run-1")), ((" " + chr(0xE9) + chr(0x4E2D)).encode(), [" ", chr(0xE9), chr(0x4E2D)]),
    (b"\x1b\x1b[A", ["esc", "up"]),
    (b"\x1b[200~stop run-1\x1b[201~", [Paste("stop run-1")]),
    (b"a\x1b[Cb", ["a", "right", "b"]),
]

# Sequences that name no key here: each is dropped and the "j" after it still decodes.
UNKNOWN = [b"\x1b[I", b"\x1b[O", b"\x1b[?1;2c", b"\x1b[999~", b"\x1b[1;99A", b"\x1b[2;5A", b"\x1bOz",
           b"\x1b[97;5u", b"\x1b[>0;95;0c", b"\x1b[201~", b"\x1b[1;2Z", b"\x1bO2P", b"\x1b[" + b"1;" * 40 + b"A"]


def decode(*chunks: bytes | str) -> list:
    d = KeyDecoder()
    out: list = []
    for chunk in chunks:
        out += d.feed(chunk)
    return out


class DecoderTest(unittest.TestCase):
    def test_each_sequence_decodes_to_its_key(self) -> None:
        for data, want in TABLE:
            with self.subTest(data=data):
                self.assertEqual(decode(data), want)

    def test_a_split_anywhere_decodes_the_same(self) -> None:
        # A sequence that arrives over two reads decodes as if it had come in one, at every byte.
        for data, want in TABLE:
            for cut in range(1, len(data)):
                with self.subTest(data=data, cut=cut):
                    self.assertEqual(decode(data[:cut], data[cut:]), want)

    def test_unknown_sequences_are_dropped_never_read_as_esc(self) -> None:
        for data in UNKNOWN:
            with self.subTest(data=data):
                self.assertEqual(decode(data + b"j"), ["j"])

    def test_c1_bidi_and_invisible_characters_are_not_keys(self) -> None:
        for ch in (chr(0x9B), chr(0x85), chr(0x202E), chr(0x200B), chr(0x2028), chr(0xFE0F), chr(0x3164)):
            with self.subTest(ch=hex(ord(ch))):
                self.assertEqual(decode(ch + "j"), ["j"])

    def test_a_control_character_ends_an_unfinished_sequence_and_still_counts(self) -> None:
        self.assertEqual(decode(b"\x1b[1\r"), ["enter"])
        self.assertEqual(decode(b"\x1b[1\x1b[C"), ["right"])

    def test_a_lone_esc_waits_for_the_timeout(self) -> None:
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertTrue(d.waiting)
        self.assertEqual(d.timeout(), ["esc"])
        self.assertFalse(d.waiting)
        self.assertEqual(d.feed(b"j"), ["j"])
        for data, want in ((b"\x1b[", ["alt+["]), (b"\x1bO", ["alt+O"]), (b"\x1b[1;", [])):
            with self.subTest(data=data):
                d = KeyDecoder()
                self.assertEqual(d.feed(data), [])
                self.assertEqual(d.timeout(), want)
                self.assertEqual(d.feed(b"k"), ["k"])

    def test_a_bracketed_paste_is_one_event_whatever_it_holds(self) -> None:
        data = b"\x1b[200~a\x1b[Ab\r\nc\x03q\x1b[201~j"
        self.assertEqual(decode(data), [Paste("a\x1b[Ab\r\nc\x03q"), "j"])
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.assertEqual(decode(data[:cut], data[cut:]), [Paste("a\x1b[Ab\r\nc\x03q"), "j"])
        self.assertEqual(decode(b"\x1b[200~x\x1b[201y\x1b[201~"), [Paste("x\x1b[201y")])

    def test_a_paste_is_not_timed_out_and_end_delivers_what_arrived(self) -> None:
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[200~abc\x1b[20"), [])
        self.assertTrue(d.in_paste)
        self.assertFalse(d.waiting)
        self.assertEqual(d.timeout(), [])
        self.assertEqual(d.end(), [Paste("abc\x1b[20")])

    def test_a_paste_past_the_cap_keeps_the_start_and_counts_the_rest(self) -> None:
        with mock.patch.object(keys, "PASTE_CAP", 4):
            self.assertEqual(decode(b"\x1b[200~abcdefg\x1b[201~"), [Paste("abcd", 3)])

    def test_bytes_that_end_inside_a_utf8_character_become_one_replacement(self) -> None:
        d = KeyDecoder()
        self.assertEqual(d.feed(b"ok\xe2\x82"), ["o", "k"])
        self.assertEqual(d.end(), [chr(0xFFFD)])


def pipe_keys(data: bytes, *, keep_open: bool = False) -> tuple[list, list[bool], bool]:
    """Write ``data`` into a pipe, read keys through a real ``term.Tty`` until it has none."""

    r_fd, w_fd = os.pipe()
    try:
        tty = object.__new__(term.Tty)
        tty.fd = r_fd
        tty.esc_wait_s = 0  # the pipe already holds everything that will come
        os.write(w_fd, data)
        if not keep_open:
            os.close(w_fd)
            w_fd = -1
        got, bursts = [], []
        for _ in range(64):
            key = tty.read_key(0)
            if key is None:
                break
            got.append(key)
            bursts.append(tty.burst)
        return got, bursts, tty.eof
    finally:
        if w_fd >= 0:
            os.close(w_fd)
        os.close(r_fd)


class TtyPipeTest(unittest.TestCase):
    @posix_select
    def test_piped_keys_decode(self) -> None:
        # The brief's "done when": a piped right arrow, Home, Shift+Tab and a bracketed paste of
        # "stop run-1" decode correctly. Before this lane each of them read as "esc".
        for data, want in ((b"\x1b[C", ["right"]), (b"\x1b[H", ["home"]), (b"\x1b[Z", ["shift+tab"]),
                           (b"\x1b[6~", ["pagedown"]), (b"\x1bx", ["alt+x"]),
                           (b"\x1b[200~stop run-1\x1b[201~", [Paste("stop run-1")])):
            with self.subTest(data=data):
                got, _, eof = pipe_keys(data)
                self.assertEqual(got, want)
                self.assertTrue(eof)

    @posix_select
    def test_a_lone_esc_reads_as_esc_after_the_wait(self) -> None:
        got, _, eof = pipe_keys(b"\x1b", keep_open=True)
        self.assertEqual((got, eof), (["esc"], False))

    @posix_select
    def test_keys_from_one_read_are_marked_burst(self) -> None:
        got, bursts, _ = pipe_keys(b"sy")
        self.assertEqual((got, bursts), (["s", "y"], [True, True]))
        got, bursts, _ = pipe_keys(b"\x1b[C")
        self.assertEqual((got, bursts), (["right"], [False]))

    def test_a_hung_up_terminal_is_the_end_of_input(self) -> None:
        tty = object.__new__(term.Tty)
        tty.fd = -1
        with mock.patch.object(term.select, "select", return_value=([-1], [], [])), \
                mock.patch.object(term.os, "read", side_effect=OSError(5, "Input/output error")):
            self.assertIsNone(tty.read_key(0))
        self.assertTrue(tty.eof)


def typed(app: TuiApp, text: str) -> None:
    for ch in text:
        app.press(ch)


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.run_id = self.driver.start_run()
        self.api = RecordingAPI(self.driver.api)
        self.app = TuiApp(self.api, clock=Clock(), environ={"NO_COLOR": "1"})
        self.app.open()
        self.api.calls.clear()

    def test_a_left_arrow_moves_the_cursor_and_keeps_the_line(self) -> None:
        app = self.app
        app.press(":")
        typed(app, "stop rn-1")
        for _ in range(3):
            app.press("left")
        self.assertEqual((app.mode, app.line, app.cursor), ("command", "stop rn-1", 6))
        app.press("u")
        self.assertEqual(app.line, "stop run-1")
        self.assertEqual(app.prompt(), "recursion: stop ru" + chr(0x258F) + "n-1")
        app.press("enter")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_home_end_right_backspace_and_delete(self) -> None:
        app = self.app
        app.press(":")
        typed(app, "xlist")
        app.press("home")
        app.press("delete")
        self.assertEqual((app.line, app.cursor), ("list", 0))
        app.press("backspace")  # nothing before the cursor
        app.press("right")
        app.press("right")
        app.press("backspace")
        self.assertEqual((app.line, app.cursor), ("lst", 1))
        app.press("end")
        app.press("right")  # already at the end
        self.assertEqual(app.cursor, 3)
        self.assertEqual(app.prompt(), "recursion: lst", "no cursor mark at the end of the line")
        for key in ("up", "down", "pageup", "f1", "alt+x", "ctrl+x", "tab", "shift+tab"):
            app.press(key)  # decoded, bound to nothing here: the line is untouched
        self.assertEqual((app.mode, app.line), ("command", "lst"))
        self.assertEqual(self.api.calls, [])

    def test_esc_still_leaves_the_line_and_sends_nothing(self) -> None:
        app = self.app
        app.press(":")
        typed(app, "stop run-1")
        app.press("esc")
        self.assertEqual((app.mode, app.line, app.cursor), ("keys", "", 0))
        self.assertEqual(self.api.calls, [])

    def test_a_paste_goes_into_the_line_whole_and_never_sends_it(self) -> None:
        app = self.app
        app.press(":")
        app.press(Paste("stop run-1\n"))
        self.assertEqual((app.mode, app.line), ("command", "stop run-1 "))
        self.assertEqual(self.api.calls, [], "a pasted line break is not Enter")
        app.press("enter")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_a_paste_goes_in_at_the_cursor_with_controls_dropped(self) -> None:
        app = self.app
        app.press(":")
        typed(app, "stop ")
        app.press("left")
        app.press(Paste("\x1b[31mru" + chr(0x202E) + "n-1\tX"))
        self.assertEqual(app.line, "stop[31mrun-1 X ")
        self.assertEqual(app.cursor, len("stop[31mrun-1 X"))

    def test_a_paste_past_the_cap_says_so(self) -> None:
        app = self.app
        app.press(":")
        app.press(Paste("list", dropped=9))
        self.assertEqual(app.line, "list")
        self.assertIn("paste cut: 9 characters", app.notice)

    def test_a_paste_in_the_run_list_is_ignored(self) -> None:
        app = self.app
        app.press(Paste("sy"))
        app.press(Paste(":stop run-1\n"))
        self.assertEqual(app.mode, "keys")
        self.assertIn("paste ignored", app.notice)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.driver.registry.get(self.run_id)["state"], "running")

    def test_a_paste_cancels_a_stop_confirmation(self) -> None:
        app = self.app
        app.press("s")
        app.press(Paste("y"))
        self.assertEqual(app.mode, "keys")
        self.assertEqual(self.api.calls, [])

    def test_a_burst_y_does_not_confirm_a_stop(self) -> None:
        app = self.app
        app.press("s", burst=True)
        app.press("y", burst=True)
        self.assertEqual(app.mode, "keys")
        self.assertIn("the y came in one read with other input", app.notice)
        self.assertEqual(self.api.calls, [])
        app.press("s")
        app.press("y")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_a_burst_enter_does_not_send_the_command_line(self) -> None:
        app = self.app
        app.press(":", burst=True)
        for ch in "stop run-1":
            app.press(ch, burst=True)
        app.press("enter", burst=True)
        self.assertEqual((app.mode, app.line), ("command", "stop run-1"))
        self.assertIn("was not sent", app.notice)
        self.assertEqual(self.api.calls, [])
        app.press("enter")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})])

    def test_q_still_closes_and_sends_nothing(self) -> None:
        self.app.press("q")
        self.assertTrue(self.app.closed)
        self.assertEqual(self.api.calls, [])


class BurstTerminal(FakeTerminal):
    """``FakeTerminal`` whose keys all report that they came in one read with other input."""

    burst = True


class InteractiveInputTest(unittest.TestCase):
    def run_tui(self, argv: list[str], terminal) -> tuple[str, list[TuiApp], RecordingAPI, int]:
        driver = FakeDriver()
        driver.start_run()
        api, out, apps = RecordingAPI(driver.api), TtyOut(), []
        original = term.interactive
        term.interactive = lambda *a, **k: original(*a, **k, on_app=apps.append)
        try:
            code = term.main(argv, lambda: api, stdin=TtyOut(), stdout=out, environ={"NO_COLOR": "1"},
                             terminal=terminal)
        finally:
            term.interactive = original
        return out.getvalue(), apps, api, code

    def test_bracketed_paste_is_on_while_the_tui_runs_and_off_after(self) -> None:
        out, _, _, code = self.run_tui([], FakeTerminal(["q"]))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(ALT_SCREEN + "\x1b[?25l" + term.PASTE_ON))
        self.assertTrue(out.endswith(term.PASTE_OFF + "\x1b[?25h\x1b[?1049l"))
        out, _, _, _ = self.run_tui(["--no-alt-screen"], FakeTerminal(["q"]))
        self.assertTrue(out.startswith(term.PASTE_ON))
        self.assertNotIn(ALT_SCREEN, out)
        self.assertEqual(out.count(term.PASTE_OFF), 1)
        self.assertLess(out.index(term.PASTE_ON), out.index(term.PASTE_OFF))

    def test_the_terminal_burst_mark_reaches_the_app(self) -> None:
        keys_in = [":", *"list", "enter", "esc", "q"]  # Esc leaves a line that a burst Enter did not send
        _, _, api, _ = self.run_tui([], FakeTerminal(list(keys_in)))
        self.assertEqual([m for m, _ in api.calls], ["run.list", "run.list"])
        _, apps, api, _ = self.run_tui([], BurstTerminal(list(keys_in)))
        self.assertEqual([m for m, _ in api.calls], ["run.list"], "a burst Enter sent nothing")
        self.assertTrue(apps[0].closed)

    def test_ctrl_c_closes_after_the_loop_stops_and_turns_paste_mode_off(self) -> None:
        class Interrupted(FakeTerminal):
            def read_key(self, timeout):
                raise KeyboardInterrupt

        out, apps, api, code = self.run_tui([], Interrupted([]))
        self.assertEqual(code, 0)
        self.assertTrue(apps[0].closed)
        self.assertEqual(list(apps[0].log)[-1].event_type, "close", "nothing is logged after the close")
        self.assertTrue(out.endswith(term.PASTE_OFF + "\x1b[?25h\x1b[?1049l"))
        self.assertNotIn("run.stop", [m for m, _ in api.calls])


class BurstProvenanceTest(unittest.TestCase):
    """The reviews of #372: a burst is not forgotten at a read boundary (Sol R1), and unfinished input
    in the same read counts as other input (Astra R3). The rule is a heuristic: input that arrives one
    key per read cannot be told from typing, and the last test says so."""

    def setUp(self) -> None:
        self.driver = FakeDriver()
        self.driver.start_run()
        self.api = RecordingAPI(self.driver.api)
        self.app = TuiApp(self.api, clock=Clock(), environ={"NO_COLOR": "1"})
        self.app.open()
        self.api.calls.clear()

    def drive(self, chunks: list[bytes], read_bytes: int | None = None) -> None:
        """Each chunk is one OS write; every key read so far reaches the app before the next write."""

        if os.name == "nt":
            self.skipTest("select() on a pipe is POSIX only")
        r_fd, w_fd = os.pipe()
        tty = object.__new__(term.Tty)
        tty.fd = r_fd
        tty.esc_wait_s = 0
        try:
            with mock.patch.object(term, "READ_BYTES", read_bytes or term.READ_BYTES):
                for chunk in chunks:
                    os.write(w_fd, chunk)
                    for _ in range(len(chunk) + 4):
                        key = tty.read_key(0)
                        if key is None:
                            break
                        self.app.press(key, burst=tty.burst)
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_a_line_padded_past_one_read_is_not_sent_by_its_lone_newline(self) -> None:
        # Sol's decisive probe: one write, the newline at byte READ_BYTES + 1.
        data = b":stop run-1" + b" " * (term.READ_BYTES - len(b":stop run-1")) + b"\n"
        self.drive([data])
        self.assertEqual(self.api.calls, [])
        self.assertIn("press Enter to send it", self.app.notice)

    def test_a_split_unbracketed_paste_does_not_submit(self) -> None:
        self.drive([b":stop run-1", b"\n"])
        self.assertEqual((self.api.calls, self.app.mode), ([], "command"))
        self.app.press("enter")
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})], "the next Enter on its own sends")

    def test_a_burst_s_cannot_be_confirmed_by_a_lone_y(self) -> None:
        self.drive([b" sy"], read_bytes=2)
        self.assertEqual(self.api.calls, [])
        self.assertIn("the s or the y came in one read", self.app.notice)
        self.drive([b"s", b"y"])
        self.assertEqual(self.api.calls, [("run.stop", {"run_id": "run-1"})], "separately typed s and y still stop")

    def test_enter_with_part_of_another_character_in_its_read_is_burst(self) -> None:
        # Astra R3: Enter and the first byte of a UTF-8 character in one read.
        for ch in ":list":
            self.app.press(ch)
        tty = object.__new__(term.Tty)
        tty.fd = -1
        with mock.patch.object(tty, "_read", return_value=b"\n\xc3"):
            key = tty.read_key(0)
        self.app.press(key, burst=tty.burst)
        self.assertEqual((key, tty.burst, self.api.calls), ("enter", True, []))
        with mock.patch.object(tty, "_read", return_value=b"\xa9"):
            self.assertEqual((tty.read_key(0), tty.burst), (chr(0xE9), True), "the key that completes it too")

    def test_esc_lookahead_is_marked_conservatively(self) -> None:
        tty = object.__new__(term.Tty)
        tty.fd = -1
        with mock.patch.object(tty, "_read", side_effect=[b"s\x1b", b"[999~\x1b[A"]):
            self.assertEqual((tty.read_key(0), tty.burst), ("s", True))
            self.assertEqual((tty.read_key(0), tty.burst), ("up", True))

    def test_a_bracketed_paste_split_anywhere_stays_inert(self) -> None:
        data = b"\x1b[200~:stop run-1\nsy\x1b[201~"
        for cut in range(len(data) + 1):
            with self.subTest(cut=cut):
                driver = FakeDriver()
                driver.start_run()
                api = RecordingAPI(driver.api)
                app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
                app.open()
                api.calls.clear()
                tty = object.__new__(term.Tty)
                tty.fd = -1
                chunks = [c for c in (data[:cut], data[cut:]) if c] + [None] * 4
                with mock.patch.object(tty, "_read", side_effect=chunks):
                    for _ in range(4):
                        key = tty.read_key(0)
                        if key is not None:
                            app.press(key, burst=tty.burst)
                self.assertEqual((api.calls, app.mode, app.line), ([], "keys", ""))

    def test_a_burst_typed_into_a_line_opened_on_its_own_marks_it(self) -> None:
        self.drive([b":", b"stop run-1", b"\n"])
        self.assertEqual((self.api.calls, self.app.mode), ([], "command"))

    def test_esc_clears_the_mark_of_a_line(self) -> None:
        self.drive([b":lis"])
        self.app.press("esc")
        for key in (":", *"list", "enter"):
            self.app.press(key)
        self.assertEqual(self.api.calls, [("run.list", {})])

    def test_input_that_arrives_one_key_per_read_is_typing(self) -> None:
        # The limit both reviews name: nothing at this interface tells it from a person typing.
        self.drive([b":", b"l", b"i", b"s", b"t", b"\n"])
        self.assertEqual(self.api.calls, [("run.list", {})])


class ReviewCoverageTest(unittest.TestCase):
    """Rules that every lane test survived without (the reviews' mutation passes): each fails without it."""

    def test_the_cursor_never_goes_left_of_the_start(self) -> None:
        app = TuiApp(ScriptedAPI({"run.list": {"code": "ok", "runs": []}}), clock=Clock(), environ={})
        app.open()
        app.press(":")
        app.press("left")
        self.assertEqual(app.cursor, 0)
        for key in ("a", "home", "left", "b"):
            app.press(key)
        self.assertEqual((app.line, app.cursor), ("ba", 1))

    def test_an_overlong_sequence_is_dropped_even_when_it_would_name_a_key(self) -> None:
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[" + b"1" * (keys.MAX_SEQ + 1)), [])
        self.assertLessEqual(len(d._seq), keys.MAX_SEQ, "the parameters kept stay bounded")
        self.assertEqual(d.feed(b"Ax"), ["x"])
        self.assertEqual(decode(b"\x1b[1;" + b"0" * keys.MAX_SEQ + b"5C" + b"j"), ["j"], "leading zeros past the cap")

    def test_every_xterm_modifier(self) -> None:
        for number, prefix in ((1, ""), (2, "shift+"), (3, "alt+"), (4, "alt+shift+"), (5, "ctrl+"),
                               (6, "ctrl+shift+"), (7, "ctrl+alt+"), (8, "ctrl+alt+shift+")):
            with self.subTest(modifier=number):
                self.assertEqual(decode(f"\x1b[1;{number}C".encode()), [prefix + "right"])
                self.assertEqual(decode(f"\x1b[3;{number}~".encode()), [prefix + "delete"])

    def test_f6_keypad_enter_and_a_triple_modifier(self) -> None:
        self.assertEqual(decode(b"\x1b[17~\x1bOM\x1b[1;8A"), ["f6", "enter", "ctrl+alt+shift+up"])

    def test_a_crlf_in_a_paste_is_one_space(self) -> None:
        app = TuiApp(ScriptedAPI({"run.list": {"code": "ok", "runs": []}}), clock=Clock(), environ={})
        app.open()
        app.press(":")
        app.press(Paste("a\r\nb\tc"))
        self.assertEqual(app.line, "a b c")


if __name__ == "__main__":
    unittest.main()
