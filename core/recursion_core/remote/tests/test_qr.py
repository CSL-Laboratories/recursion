"""The QR encoder (lane REMOTE): published reference values, and a reader of its own that takes codes back.

No QR library is installed here to check against, and none may be added. So the tests check the encoder
three independent ways: against values published with the standard and its well-known worked example
(the Reed-Solomon codewords of "HELLO WORLD" at 1-M, the format information table, the version
information table); against the module count of each version, computed from a formula and not from the
encoder's tables; and by reading every code back with the reader below, written apart from the encoder
from the standard's description: it finds the function modules by region, reads both copies of the
format information, unmasks, reads the zigzag, checks that every block's Reed-Solomon syndromes are zero,
and parses the byte-mode segment.
"""

from __future__ import annotations

import re
import unittest

from recursion_core.remote import qr
from recursion_core.remote.pairing import pairing_link

# -- reference values --------------------------------------------------------------------------------
# ISO/IEC 18004 worked example, as published: "HELLO WORLD" at 1-M, its 16 data codewords and 10 EC codewords.
HELLO_DATA = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
HELLO_EC = [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
# The format information table: level, then the 15 bits for masks 0 to 7.
FORMAT_TABLE = {
    "L": ["111011111000100", "111001011110011", "111110110101010", "111100010011101",
          "110011000101111", "110001100011000", "110110001000001", "110100101110110"],
    "M": ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
          "100010111111001", "100000011001110", "100111110010111", "100101010100000"],
}
VERSION_TABLE = {7: "000111110010010100", 8: "001000010110111100", 9: "001001101010011001",
                 10: "001010010011010011"}

# -- an independent reader ---------------------------------------------------------------------------
_EXP, _LOG = [0] * 510, [0] * 256
_v = 1
for _k in range(255):
    _EXP[_k] = _EXP[_k + 255] = _v
    _LOG[_v] = _k
    _v = (_v << 1) ^ (0x11D if _v & 0x80 else 0)

BLOCKS_M = {1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32] * 2), 5: (24, [43] * 2), 6: (16, [27] * 4),
            7: (18, [31] * 4), 8: (22, [38] * 2 + [39] * 2), 9: (22, [36] * 3 + [37] * 2),
            10: (26, [43] * 4 + [44])}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}
MASKS = {
    0: lambda i, j: (i + j) % 2 == 0, 1: lambda i, j: i % 2 == 0, 2: lambda i, j: j % 3 == 0,
    3: lambda i, j: (i + j) % 3 == 0, 4: lambda i, j: (i // 2 + j // 3) % 2 == 0,
    5: lambda i, j: (i * j) % 2 + (i * j) % 3 == 0, 6: lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    7: lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
}


def raw_data_modules(version: int) -> int:
    """Modules left for data and EC codewords, from the standard's layout (not from the encoder's tables)."""
    size = 17 + 4 * version
    total = size * size - 3 * 64 - 2 * (size - 16) - 31  # finders with separators, timing, format and dark module
    count = len(ALIGN[version])
    if count:
        total -= 25 * (count * count - 3) - 10 * (count - 2)  # alignment patterns, less their overlap with timing
    if version >= 7:
        total -= 36
    return total


def function_map(version: int) -> list[list[bool]]:
    size = 17 + 4 * version
    f = [[False] * size for _ in range(size)]

    def mark(rows, cols):
        for r in rows:
            for c in cols:
                f[r][c] = True

    mark(range(9), range(9))
    mark(range(9), range(size - 8, size))
    mark(range(size - 8, size), range(9))
    mark([6], range(size))
    mark(range(size), [6])
    centres = ALIGN[version]
    for r in centres:
        for c in centres:
            if (r, c) not in ((6, 6), (6, centres[-1]), (centres[-1], 6)):
                mark(range(r - 2, r + 3), range(c - 2, c + 3))
    if version >= 7:
        mark(range(6), range(size - 11, size - 8))
        mark(range(size - 11, size - 8), range(6))
    return f


def syndromes_are_zero(codeword: list[int], ec_length: int) -> bool:
    for i in range(ec_length):
        s = 0
        for c in codeword:
            s = (_EXP[_LOG[s] + i] if s else 0) ^ c  # Horner's rule at alpha^i
        if s:
            return False
    return True


def read_qr(modules) -> tuple[bytes, int]:
    """Bytes and mask of a level-M, byte-mode code; raises AssertionError where the code is not well formed."""
    size = len(modules)
    version = (size - 17) // 4
    assert size == 17 + 4 * version and 1 <= version <= 10
    first = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8),
             (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    second = [(size - k, 8) for k in range(1, 8)] + [(8, size - k) for k in range(8, 0, -1)]
    bits_of = lambda cells: "".join("1" if modules[r][c] else "0" for r, c in cells)  # noqa: E731
    assert bits_of(first) == bits_of(second), "the two copies of the format information differ"
    assert modules[size - 8][8], "the dark module is light"
    mask = FORMAT_TABLE["M"].index(bits_of(first))
    if version >= 7:
        top_right = "".join("1" if modules[i // 3][size - 11 + i % 3] else "0" for i in range(17, -1, -1))
        bottom_left = "".join("1" if modules[size - 11 + i % 3][i // 3] else "0" for i in range(17, -1, -1))
        assert top_right == bottom_left == VERSION_TABLE[version], "the version information is wrong"
    f = function_map(version)
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not f[r][c]:
                    bits.append(int(modules[r][c]) ^ int(MASKS[mask](r, c)))
        upward = not upward
        col -= 2
    assert len(bits) == raw_data_modules(version)
    codewords = [int("".join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits) - 7, 8)]
    ec_length, sizes = BLOCKS_M[version]
    total = sum(sizes) + ec_length * len(sizes)
    data_blocks = [[] for _ in sizes]
    k = 0
    for i in range(max(sizes)):
        for b, n in enumerate(sizes):
            if i < n:
                data_blocks[b].append(codewords[k])
                k += 1
    ec_blocks = [[] for _ in sizes]
    for _ in range(ec_length):
        for b in range(len(sizes)):
            ec_blocks[b].append(codewords[k])
            k += 1
    assert k == total
    for data, ec in zip(data_blocks, ec_blocks):
        assert syndromes_are_zero(data + ec, ec_length), "a block's Reed-Solomon syndromes are not zero"
    stream = "".join(f"{w:08b}" for block in data_blocks for w in block)
    assert stream[:4] == "0100", "not byte mode"
    count_bits = 8 if version <= 9 else 16
    length = int(stream[4:4 + count_bits], 2)
    start = 4 + count_bits
    end = start + 8 * length
    # After the data: the 4-bit terminator (it ends the byte here), then pad codewords 11101100 and 00010001
    # alternating to the end of the data codewords.
    tail = stream[end:]
    assert tail[:4] == "0000" and (end + 4) % 8 == 0, "no terminator ending the last byte"
    pads = tail[4:]
    assert pads == ("1110110000010001" * len(pads))[:len(pads)], "the pad codewords are not EC 11 alternating"
    return bytes(int(stream[start + 8 * n:start + 8 * n + 8], 2) for n in range(length)), mask


def reference_penalty(modules) -> int:
    """The four penalty rules, written apart from the encoder: runs of five or more score the run length
    less 2; each 2x2 block of one colour scores 3; each 1011101 pattern with four light modules on one side
    scores 40 (counted wherever it starts, overlaps too); and each full 5 per cent the dark share is away
    from 50 per cent scores 10."""
    size = len(modules)
    rows = ["".join("1" if v else "0" for v in row) for row in modules]
    columns = ["".join(row[c] for row in rows) for c in range(size)]
    score = 0
    for line in rows + columns:
        score += sum(len(run) - 2 for run in re.findall(r"0{5,}|1{5,}", line))
        score += 40 * (len(re.findall(r"(?=10111010000)", line)) + len(re.findall(r"(?=00001011101)", line)))
    score += 3 * sum(1 for r in range(size - 1) for c in range(size - 1)
                     if rows[r][c] == rows[r][c + 1] == rows[r + 1][c] == rows[r + 1][c + 1])
    dark = sum(row.count("1") for row in rows)
    return score + 10 * (abs(100 * dark // (size * size) - 50) // 5)


class ReferenceValueTests(unittest.TestCase):
    def test_reed_solomon_gives_the_published_codewords_for_hello_world(self) -> None:
        self.assertEqual(qr.rs_remainder(HELLO_DATA, 10), HELLO_EC)

    def test_format_bits_are_the_published_table(self) -> None:
        for ecl, row in FORMAT_TABLE.items():
            for mask, bits in enumerate(row):
                with self.subTest(ecl=ecl, mask=mask):
                    self.assertEqual(f"{qr.format_bits(ecl, mask):015b}", bits)

    def test_version_bits_are_the_published_table(self) -> None:
        for version, bits in VERSION_TABLE.items():
            with self.subTest(version=version):
                self.assertEqual(f"{qr.version_bits(version):018b}", bits)

    def test_the_block_table_fills_each_version_exactly(self) -> None:
        for version in range(1, 11):
            with self.subTest(version=version):
                ec_length, sizes = BLOCKS_M[version]
                self.assertEqual((sum(sizes) + ec_length * len(sizes)) * 8, raw_data_modules(version) // 8 * 8)
                self.assertEqual(qr.data_capacity(version), sum(sizes))


class RoundTripTests(unittest.TestCase):
    LINK = pairing_link("https://host-name.tailnet-example.ts.net", "Abc_DEF-123x", "Q" * 22)

    def test_a_pairing_link_reads_back_exactly(self) -> None:
        code = qr.encode(self.LINK)
        self.assertEqual(read_qr(code.modules), (self.LINK.encode(), code.mask))

    def test_every_version_reads_back_at_its_capacity_and_one_byte_more_needs_the_next(self) -> None:
        capacities = {1: 14, 2: 26, 3: 42, 4: 62, 5: 84, 6: 106, 7: 122, 8: 152, 9: 180, 10: 213}
        for version, capacity in capacities.items():
            with self.subTest(version=version):
                self.assertEqual(qr.byte_capacity(version), capacity)
                data = bytes((n * 37 + version) % 256 for n in range(capacity))
                code = qr.encode(data)
                self.assertEqual((code.version, code.size), (version, 17 + 4 * version))
                self.assertEqual(read_qr(code.modules)[0], data)
                if version < 10:
                    self.assertEqual(qr.encode(data + b"x").version, version + 1)

    def test_every_mask_reads_back(self) -> None:
        for mask in range(8):
            for data in (b"", b"a", self.LINK.encode(), bytes(range(200))):
                with self.subTest(mask=mask, length=len(data)):
                    self.assertEqual(read_qr(qr.encode(data, mask=mask).modules), (data, mask))

    def test_more_than_version_10_holds_is_refused(self) -> None:
        with self.assertRaises(qr.QrTooLong):
            qr.encode(b"x" * 214)

    def test_the_chosen_mask_has_the_lowest_penalty(self) -> None:
        for data in (self.LINK, "short", "x" * 150):
            with self.subTest(length=len(data)):
                scores = [reference_penalty(qr.encode(data, mask=m).modules) for m in range(8)]
                self.assertEqual(qr.encode(data).mask, scores.index(min(scores)))

    def test_the_penalty_is_the_four_rules_as_written_here(self) -> None:
        for data in (self.LINK, "short", "x" * 150, bytes(range(200))):
            for mask in range(8):
                with self.subTest(length=len(data), mask=mask):
                    modules = qr.encode(data, mask=mask).modules
                    self.assertEqual(qr.penalty(modules), reference_penalty(modules))


class PatternTests(unittest.TestCase):
    def test_finders_timing_and_alignment_are_where_the_standard_puts_them(self) -> None:
        code = qr.encode(PatternData.DATA)  # version 7: alignment and version information both present
        m, size = code.modules, code.size
        self.assertEqual(code.version, 7)
        ring = [[max(abs(r - 3), abs(c - 3)) for c in range(7)] for r in range(7)]
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            for r in range(7):
                for c in range(7):
                    self.assertEqual(m[top + r][left + c], ring[r][c] != 2, (top, left, r, c))
        separators = ([(7, c) for c in range(8)] + [(r, 7) for r in range(8)]
                      + [(7, size - 1 - c) for c in range(8)] + [(r, size - 8) for r in range(8)]
                      + [(size - 8, c) for c in range(8)] + [(size - 1 - r, 7) for r in range(8)])
        self.assertFalse(any(m[r][c] for r, c in separators), "a separator module is dark")
        for i in range(8, size - 8):
            self.assertEqual(m[6][i], i % 2 == 0)
            self.assertEqual(m[i][6], i % 2 == 0)
        for r in (6, 22, 38):
            for c in (6, 22, 38):
                if (r, c) in ((6, 6), (6, 38), (38, 6)):
                    continue
                self.assertTrue(m[r][c])
                self.assertFalse(any(m[r + dr][c + dc] for dr, dc in ((-1, -1), (-1, 0), (0, 1), (1, 1))))
                self.assertTrue(all(m[r + dr][c + dc] for dr, dc in ((-2, -2), (-2, 2), (2, -2), (2, 2))))


class PatternData:
    DATA = "v" * 110  # 107 to 122 bytes: version 7


class DrawingTests(unittest.TestCase):
    def test_the_text_drawing_holds_every_module_and_the_quiet_zone(self) -> None:
        code = qr.encode("pair me")
        for background in ("dark", "light"):
            with self.subTest(background=background):
                text = qr.render_text(code, terminal_background=background)
                self.assertTrue(text.endswith("\n"))
                lines = text.splitlines()
                side = code.size + 8
                self.assertEqual(len(lines), (side + 1) // 2)
                self.assertTrue(all(len(line) == side for line in lines))
                upper, lower, full = "\u2580", "\u2584", "\u2588"
                drawn = [[False] * side for _ in range(len(lines) * 2)]
                for n, line in enumerate(lines):
                    for c, glyph in enumerate(line):
                        drawn[2 * n][c] = glyph in (upper, full)
                        drawn[2 * n + 1][c] = glyph in (lower, full)
                for r in range(side):
                    for c in range(side):
                        inside = 4 <= r < 4 + code.size and 4 <= c < 4 + code.size
                        dark = inside and code.modules[r - 4][c - 4]
                        self.assertEqual(drawn[r][c], (not dark) if background == "dark" else dark, (r, c))

    def test_the_svg_drawing_has_one_black_square_per_dark_module(self) -> None:
        code = qr.encode("pair me")
        svg = qr.render_svg(code, module_px=3)
        self.assertTrue(svg.isascii())
        side = (code.size + 8) * 3
        self.assertIn(f'viewBox="0 0 {side} {side}"', svg)
        squares = {(int(y) // 3 - 4, int(x) // 3 - 4) for x, y in re.findall(r'<rect x="(\d+)" y="(\d+)"', svg)}
        self.assertEqual(squares, {(r, c) for r in range(code.size) for c in range(code.size) if code.modules[r][c]})

    def test_bad_drawing_arguments_are_refused(self) -> None:
        code = qr.encode("x")
        with self.assertRaises(ValueError):
            qr.render_text(code, terminal_background="blue")
        with self.assertRaises(ValueError):
            qr.render_svg(code, module_px=0)
        with self.assertRaises(ValueError):
            qr.encode("x", mask=8)


if __name__ == "__main__":
    unittest.main()
