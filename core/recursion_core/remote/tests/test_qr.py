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
import string
import unittest

from recursion_core.remote import pairing, qr
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
                 10: "001010010011010011", 11: "001011101111110110", 12: "001100011101100010",
                 13: "001101100001000111", 14: "001110011000001101", 15: "001111100100101000",
                 16: "010000101101111000"}

# -- an independent reader ---------------------------------------------------------------------------
_EXP, _LOG = [0] * 510, [0] * 256
_v = 1
for _k in range(255):
    _EXP[_k] = _EXP[_k + 255] = _v
    _LOG[_v] = _k
    _v = (_v << 1) ^ (0x11D if _v & 0x80 else 0)

BLOCKS_M = {1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32] * 2), 5: (24, [43] * 2), 6: (16, [27] * 4),
            7: (18, [31] * 4), 8: (22, [38] * 2 + [39] * 2), 9: (22, [36] * 3 + [37] * 2),
            10: (26, [43] * 4 + [44]), 11: (30, [50] + [51] * 4), 12: (22, [36] * 6 + [37] * 2),
            13: (22, [37] * 8 + [38]), 14: (24, [40] * 4 + [41] * 5), 15: (24, [41] * 5 + [42] * 5),
            16: (28, [45] * 7 + [46] * 3)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62],
         14: [6, 26, 46, 66], 15: [6, 26, 48, 70], 16: [6, 26, 50, 74]}
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
    assert size == 17 + 4 * version and 1 <= version <= 16, f"not a version 1 to 16 size: {size}"
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
    for data, ec in zip(data_blocks, ec_blocks, strict=True):
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
        for version in range(1, 17):
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
        capacities = {1: 14, 2: 26, 3: 42, 4: 62, 5: 84, 6: 106, 7: 122, 8: 152, 9: 180, 10: 213, 11: 251,
                      12: 287, 13: 331, 14: 362, 15: 412, 16: 450}
        for version, capacity in capacities.items():
            with self.subTest(version=version):
                self.assertEqual(qr.byte_capacity(version), capacity)
                data = bytes((n * 37 + version) % 256 for n in range(capacity))
                code = qr.encode(data)
                self.assertEqual((code.version, code.size), (version, 17 + 4 * version))
                self.assertEqual(read_qr(code.modules)[0], data)
                if version < 16:
                    self.assertEqual(qr.encode(data + b"x").version, version + 1)

    def test_every_mask_reads_back(self) -> None:
        for mask in range(8):
            for data in (b"", b"a", self.LINK.encode(), bytes(range(200))):
                with self.subTest(mask=mask, length=len(data)):
                    self.assertEqual(read_qr(qr.encode(data, mask=mask).modules), (data, mask))

    def test_more_than_version_16_holds_is_refused(self) -> None:
        self.assertEqual(qr.encode(b"x" * 450).version, 16)
        with self.assertRaises(qr.QrTooLong):
            qr.encode(b"x" * 451)

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


# 64 characters: every one a pairing token may hold.
TOKEN = string.ascii_letters + string.digits + "-_"


def longest_host() -> str:
    """Four DNS labels of 63 characters and three dots: 255 characters, the longest host pairing_link takes."""
    return ".".join(c + "x-0" * 20 + "y" + c for c in "aZ9q")


class PairingLinkContractTests(unittest.TestCase):
    """Every link pairing.pairing_link can return is encoded (DeepSeek r1 F2 on #413)."""

    def test_the_longest_link_pairing_makes_is_encoded_and_reads_back(self) -> None:
        link = pairing_link(f"https://{longest_host()}:65535", TOKEN, TOKEN[::-1])
        self.assertEqual(len(link.encode("ascii")), pairing.MAX_LINK_BYTES)
        code = qr.encode(link)
        self.assertEqual(read_qr(code.modules), (link.encode("ascii"), code.mask))

    def test_a_bracketed_address_link_is_encoded_and_reads_back(self) -> None:
        link = pairing_link("https://[2001:db8:ffff:ffff:ffff:ffff:255.255.255.255]:00001/", TOKEN, TOKEN)
        code = qr.encode(link)
        self.assertEqual(read_qr(code.modules), (link.encode("ascii"), code.mask))

    def test_the_bound_is_pairings_grammar_and_the_smallest_version_that_holds_it(self) -> None:
        self.assertEqual(pairing.MAX_LINK_BYTES, len("https://") + 255 + len(":65535") + len("/pair#offer=")
                         + 64 + len("&code=") + 64)
        self.assertGreaterEqual(qr.byte_capacity(qr.MAX_VERSION), pairing.MAX_LINK_BYTES)
        self.assertLess(qr.byte_capacity(qr.MAX_VERSION - 1), pairing.MAX_LINK_BYTES)
        for module in (qr, pairing):  # the one contract, stated with its number in both modules
            self.assertIn(f"{pairing.MAX_LINK_BYTES} bytes", module.__doc__ or "")
        # One step past each bound of the grammar is refused by pairing itself, so nothing longer reaches qr.
        for origin, offer, code, refusal in ((f"https://a{longest_host()}", TOKEN, TOKEN, "origin_invalid"),
                                             (f"https://{longest_host()}:123456", TOKEN, TOKEN, "origin_invalid"),
                                             ("https://h.example", TOKEN + "a", TOKEN, "offer_invalid"),
                                             ("https://h.example", TOKEN, TOKEN + "a", "offer_invalid")):
            with self.subTest(origin=origin[:20], refusal=refusal), self.assertRaises(pairing.PairingRefused) as caught:
                pairing_link(origin, offer, code)
            self.assertEqual(caught.exception.code, refusal)


class ArgumentTests(unittest.TestCase):
    """Wrong arguments are refused with a type, never a wrong symbol or an IndexError (DeepSeek r1 F3 to F8)."""

    def test_encode_takes_bytes_or_text_only(self) -> None:
        for bad in (5, 0, [72, 73], None, 1.5):
            with self.subTest(data=bad), self.assertRaises(TypeError):
                qr.encode(bad)  # type: ignore[arg-type]
        self.assertEqual(qr.encode(bytearray(b"hi")), qr.encode(b"hi"))
        self.assertEqual(qr.encode(memoryview(b"hi")), qr.encode(b"hi"))
        for mask in (True, 0.0, -1, 8, "1"):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                qr.encode(b"x", mask=mask)  # type: ignore[arg-type]

    def test_capacities_outside_the_versions_are_refused(self) -> None:
        for version in (0, 17, -1, True, 1.0, "1"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    qr.byte_capacity(version)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    qr.data_capacity(version)  # type: ignore[arg-type]

    def test_penalty_takes_only_a_square_matrix(self) -> None:
        for bad in ([], [[]], [[True]], [[True, False], [True]], "abcd", [[True, False], [False, 2]]):
            with self.subTest(modules=bad), self.assertRaises(ValueError):
                qr.penalty(bad)  # type: ignore[arg-type]

    def test_reed_solomon_refuses_what_is_not_a_byte_or_a_degree(self) -> None:
        for data, degree in (([1], 0), ([256], 10), ([-1], 10), ([1.0], 10), ([True], 10), ([1], True)):
            with self.subTest(data=data, degree=degree), self.assertRaises(ValueError):
                qr.rs_remainder(data, degree)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            qr.rs_generator(0)

    def test_format_and_version_bits_refuse_what_the_standard_does_not_define(self) -> None:
        for ecl, mask in (("X", 0), ("M", 8), ("M", -1), ("M", True)):
            with self.subTest(ecl=ecl, mask=mask), self.assertRaises(ValueError):
                qr.format_bits(ecl, mask)
        for version in (6, 41, True):
            with self.subTest(version=version), self.assertRaises(ValueError):
                qr.version_bits(version)


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


def as_a_camera_sees(text: str, background: str) -> list[list[bool]]:
    """The dark cells of a terminal drawing as a camera sees them: a glyph's half shows the text colour (light
    on a dark terminal, dark on a light one), a blank half the background. Rows past the last are dropped."""
    text_is_dark = background == "light"
    lines = text.splitlines()
    side = len(lines[0])
    seen = []
    for line in lines:
        assert len(line) == side
        top = [text_is_dark if g in ("\u2580", "\u2588") else not text_is_dark for g in line]
        bottom = [text_is_dark if g in ("\u2584", "\u2588") else not text_is_dark for g in line]
        seen += [top, bottom]
    return seen[:side]


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

    def test_the_drawn_text_reads_back_as_a_camera_sees_it_on_its_background(self) -> None:
        # A glyph shows the terminal's text colour, a blank its background: on a dark terminal the text is
        # light, on a light one dark. What a camera sees is read with the reader above (DeepSeek r1 F1 on #413).
        for data in ("pair me", pairing_link(f"https://{longest_host()}:65535", TOKEN, TOKEN)):
            code = qr.encode(data)
            for background in ("dark", "light"):
                with self.subTest(length=len(data), background=background):
                    seen = as_a_camera_sees(qr.render_text(code, terminal_background=background), background)
                    self.assertEqual(len(seen), code.size + 8)
                    border = [seen[r][c] for r in range(len(seen)) for c in range(len(seen))
                              if min(r, c) < 4 or max(r, c) >= code.size + 4]
                    self.assertFalse(any(border), "the quiet zone is not light")
                    inner = [row[4:4 + code.size] for row in seen[4:4 + code.size]]
                    self.assertEqual(read_qr(inner), (data.encode("ascii"), code.mask))
                    # The drawing made for the other background is an inverted symbol on this one.
                    other = "light" if background == "dark" else "dark"
                    wrong = as_a_camera_sees(qr.render_text(code, terminal_background=other), background)
                    self.assertTrue(all(wrong[0]), "the wrong drawing's quiet zone is not dark")

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
        for border in (-1, -4, 1.5, True):
            with self.subTest(border=border):
                with self.assertRaises(ValueError):
                    qr.render_text(code, border=border)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    qr.render_svg(code, border=border)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            qr.render_svg(code, module_px=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            qr.encode("x", mask=8)


if __name__ == "__main__":
    unittest.main()
