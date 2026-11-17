"""Review 5 of #372 (Sol, later): one bracketed paste split inside a UTF-8 character is one event, not a
burst. The bytes of a character inside a paste are the paste's, so they are not "part of a key held from
an earlier read". A typed character split the same way stays marked, as round 2 made it (Sol's round-2
probe, a single UTF-8 completion remembers the held bytes)."""

from __future__ import annotations

import itertools
import unittest
from unittest import mock

from recursion_core.tui.keys import KeyDecoder, Paste
from recursion_core.tui.tests.test_review_372 import fresh, tty_for

OPEN, CLOSE = b"\x1b[200~", b"\x1b[201~"
TEXT = "st" + chr(0xE9) + "p " + chr(0x20AC) + " run-1 " + chr(0x1F600)  # 2-, 3- and 4-byte characters
BODY = TEXT.encode()
WHOLE = OPEN + BODY + CLOSE


def read_all(chunks: list[bytes]) -> list[tuple[object, bool]]:
    tty = tty_for(-1)
    out = []
    with mock.patch.object(tty, "_read", side_effect=itertools.chain(chunks, itertools.repeat(None))):
        for _ in range(len(chunks) + 2):
            key = tty.read_key(0)
            if key is not None:
                out.append((key, tty.burst))
    return out


def inside_characters(data: bytes) -> list[int]:
    """Every offset that falls inside a multi-byte UTF-8 character of ``data``."""

    return [i for i in range(1, len(data)) if 0x80 <= data[i] < 0xC0]


class PasteSplitInsideACharacterTest(unittest.TestCase):
    def test_a_paste_split_in_two_anywhere_is_one_event_and_not_a_burst(self) -> None:
        cuts = range(1, len(WHOLE))
        self.assertTrue(set(inside_characters(WHOLE)) <= set(cuts))
        for cut in cuts:
            with self.subTest(cut=cut):
                self.assertEqual(read_all([WHOLE[:cut], WHOLE[cut:]]), [(Paste(TEXT), False)])

    def test_a_paste_split_in_three_inside_its_characters_is_one_event_and_not_a_burst(self) -> None:
        inside = inside_characters(WHOLE)
        for a, b in itertools.combinations(sorted(set(inside) | {len(OPEN), len(OPEN) + 1}), 2):
            with self.subTest(a=a, b=b):
                self.assertEqual(read_all([WHOLE[:a], WHOLE[a:b], WHOLE[b:]]), [(Paste(TEXT), False)])

    def test_a_split_inside_a_character_changes_nothing_beside_the_paste(self) -> None:
        # The key in the read that opens the paste, and the key in the read that ends it, are marked as
        # they are when the paste is split between two ASCII bytes.
        for before, after in ((b"x", b""), (b"", b"x"), (b"x", b"y")):
            data = before + WHOLE + after
            plain_cut = len(before) + len(OPEN) + 1  # between "s" and "t"
            for cut in inside_characters(data):
                with self.subTest(before=before, after=after, cut=cut):
                    self.assertEqual(read_all([data[:cut], data[cut:]]),
                                     read_all([data[:plain_cut], data[plain_cut:]]))
            got = read_all([data[:plain_cut], data[plain_cut:]])
            if before:
                self.assertEqual(got[0], ("x", True), "a key beside a paste begun is burst")
            if after:
                self.assertEqual(got[-1][1], True, "a key read with the paste's end is burst")
                self.assertEqual(got[-2], (Paste(TEXT), True))

    def test_a_typed_character_split_over_two_reads_stays_marked(self) -> None:
        for char in (chr(0xE9), chr(0x20AC), chr(0x1F600)):
            raw = char.encode()
            for cut in range(1, len(raw)):
                with self.subTest(char=hex(ord(char)), cut=cut):
                    self.assertEqual(read_all([raw[:cut], raw[cut:]]), [(char, True)])

    def test_the_line_takes_such_a_paste_and_one_enter_sends_it(self) -> None:
        run_id = "run-" + chr(0xE9)
        data = OPEN + f"status {run_id}".encode() + CLOSE
        cut = data.index(chr(0xE9).encode()) + 1
        for chunks in ([data[:cut], data[cut:]], [data]):
            with self.subTest(split=len(chunks)):
                app, api = fresh()
                app.press(":")
                for key, burst in read_all(chunks):
                    app.press(key, burst=burst)
                self.assertEqual(app.line, f"status {run_id}")
                app.press("enter")
                self.assertEqual(api.calls, [("run.status", {"run_id": run_id})])


class HoldingTest(unittest.TestCase):
    def test_holding_counts_a_character_held_outside_a_paste_only(self) -> None:
        decoder = KeyDecoder()
        self.assertEqual(decoder.feed(OPEN + b"st\xc3"), [])
        self.assertTrue(decoder.in_paste)
        self.assertFalse(decoder.holding, "the held bytes are the paste's")
        self.assertEqual(decoder.feed(b"\xa9" + CLOSE), [Paste("st" + chr(0xE9))])
        self.assertEqual(decoder.feed(b"x\xc3"), ["x"])
        self.assertTrue(decoder.holding, "outside a paste they are part of a key")
        self.assertEqual(decoder.feed(b"\xa9"), [chr(0xE9)])
        self.assertFalse(decoder.holding)


if __name__ == "__main__":
    unittest.main()
