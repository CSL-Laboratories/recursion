"""A QR code encoder for the pairing link (lane REMOTE): byte mode, error correction level M, versions 1 to 10.

No dependency (the brief: "shown as a QR (remote/qr.py, no new dependency)"). Written from the QR code
standard (ISO/IEC 18004): Reed-Solomon over GF(256) with the polynomial x^8 + x^4 + x^3 + x^2 + 1, the
level-M block table for versions 1 to 10, the function patterns, the zigzag placement, the eight masks
with the four penalty rules, and the BCH-coded format and version information.

The range is chosen for what it carries: a pairing link (``pairing.pairing_link``) is about 100 bytes, and
version 10 at level M holds 213 bytes in byte mode. Longer input is refused (``QrTooLong``); nothing here
falls back to a weaker error correction level. Level M restores about 15 per cent of damaged codewords.

``encode(data)`` returns a ``QrCode``; ``render_text`` draws it with half-block characters for a terminal,
``render_svg`` as an SVG document for a page. Both are pure functions of the code.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_VERSION = 10
QUIET_ZONE = 4
# Error correction level bits of the format information (ISO/IEC 18004, table 12): L 01, M 00, Q 11, H 10.
_ECL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
_FORMAT_GENERATOR = 0b10100110111
_FORMAT_XOR = 0b101010000010010
_VERSION_GENERATOR = 0b1111100100101

# Level M, versions 1 to 10: (EC codewords per block, ((block count, data codewords per block), ...)).
_BLOCKS_M = {
    1: (10, ((1, 16),)),
    2: (16, ((1, 28),)),
    3: (26, ((1, 44),)),
    4: (18, ((2, 32),)),
    5: (24, ((2, 43),)),
    6: (16, ((4, 27),)),
    7: (18, ((4, 31),)),
    8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}
# Row and column of the alignment pattern centres, per version.
_ALIGNMENT = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
    7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}


class QrTooLong(ValueError):
    """The data does not fit version 10 at level M (213 bytes)."""


# -- GF(256) and Reed-Solomon ----------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i] = _value
    _LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs_generator(degree: int) -> list[int]:
    """The generator polynomial (x - a^0)(x - a^1)...(x - a^(degree-1)), highest power first."""
    poly = [1]
    for i in range(degree):
        product = [0] * (len(poly) + 1)
        for j, coefficient in enumerate(poly):
            product[j] ^= coefficient
            product[j + 1] ^= _gf_mul(coefficient, _EXP[i])
        poly = product
    return poly


def rs_remainder(data: list[int], degree: int) -> list[int]:
    """The ``degree`` error correction codewords for ``data`` (the remainder of data * x^degree / generator)."""
    generator = rs_generator(degree)
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for j in range(degree):
            remainder[j] ^= _gf_mul(generator[j + 1], factor)
    return remainder


# -- format and version information ----------------------------------------------------------------
def _bch(value: int, generator: int) -> int:
    """The remainder of ``value`` (already shifted) divided by ``generator`` over GF(2)."""
    degree = generator.bit_length() - 1
    while value.bit_length() > degree:
        value ^= generator << (value.bit_length() - generator.bit_length())
    return value


def format_bits(ecl: str, mask: int) -> int:
    """The 15 format bits for a level and a mask: 5 data bits, 10 BCH bits, masked with 101010000010010."""
    if ecl not in _ECL_BITS or mask not in range(8):
        raise ValueError("a level is L, M, Q or H and a mask is 0 to 7")
    data = _ECL_BITS[ecl] << 3 | mask
    return (data << 10 | _bch(data << 10, _FORMAT_GENERATOR)) ^ _FORMAT_XOR


def version_bits(version: int) -> int:
    """The 18 version bits (versions 7 and up): 6 data bits and 12 BCH bits."""
    if not 7 <= version <= 40:
        raise ValueError("version information exists for versions 7 to 40")
    return version << 12 | _bch(version << 12, _VERSION_GENERATOR)


# -- the code --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class QrCode:
    """A finished code: ``modules[row][column]`` is True for a dark module."""

    version: int
    mask: int
    modules: tuple[tuple[bool, ...], ...]

    @property
    def size(self) -> int:
        return len(self.modules)


def data_capacity(version: int) -> int:
    """Data codewords of a version at level M."""
    return sum(count * size for count, size in _BLOCKS_M[version][1])


def byte_capacity(version: int) -> int:
    """How many bytes a version holds at level M in byte mode."""
    count_bits = 8 if version <= 9 else 16
    return (data_capacity(version) * 8 - 4 - count_bits) // 8


def _choose_version(length: int) -> int:
    for version in range(1, MAX_VERSION + 1):
        if length <= byte_capacity(version):
            return version
    raise QrTooLong(f"{length} bytes do not fit version {MAX_VERSION} at level M ({byte_capacity(MAX_VERSION)} bytes)")


def _data_codewords(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> (length - 1 - i)) & 1 for i in range(length))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version <= 9 else 16)
    for byte in data:
        put(byte, 8)
    # The terminator. In byte mode the data ends 4 bits short of a byte boundary (4 + 8 or 16 + 8n bits), and
    # whole codewords leave at least those 4 bits free, so the 4-bit terminator always ends the last byte.
    put(0, 4)
    words = [int("".join(str(bit) for bit in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    used = len(words)
    while len(words) < data_capacity(version):
        words.append((0xEC, 0x11)[(len(words) - used) % 2])  # pad codewords, alternating
    return words


def _interleave(words: list[int], version: int) -> list[int]:
    ec_length, groups = _BLOCKS_M[version]
    blocks, start = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[start:start + size])
            start += size
    corrections = [rs_remainder(block, ec_length) for block in blocks]
    out = [block[i] for i in range(max(len(b) for b in blocks)) for block in blocks if i < len(block)]
    out += [correction[i] for i in range(ec_length) for correction in corrections]
    return out


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col] = dark
        self.function[row][col] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):  # timing patterns
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for row, col in ((3, 3), (3, size - 4), (size - 4, 3)):  # finders with their separators
            for dr in range(-4, 5):
                for dc in range(-4, 5):
                    r, c = row + dr, col + dc
                    if 0 <= r < size and 0 <= c < size:
                        self.set_function(r, c, max(abs(dr), abs(dc)) not in (2, 4))
        centres = _ALIGNMENT[self.version]
        for row in centres:
            for col in centres:
                if (row, col) in ((6, 6), (6, centres[-1]), (centres[-1], 6)):
                    continue  # these three overlap the finders
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set_function(row + dr, col + dc, max(abs(dr), abs(dc)) != 1)
        self.draw_format(0)  # reserves the format areas and the dark module; drawn again for the chosen mask
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set_function(b, a, dark)
                self.set_function(a, b, dark)

    def draw_format(self, mask: int) -> None:
        size, bits = self.size, format_bits("M", mask)

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set_function(i, 8, bit(i))
        self.set_function(7, 8, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(8, 7, bit(8))
        for i in range(9, 15):
            self.set_function(8, 14 - i, bit(i))
        for i in range(8):
            self.set_function(8, size - 1 - i, bit(i))
        for i in range(8, 15):
            self.set_function(size - 15 + i, 8, bit(i))
        self.set_function(size - 8, 8, True)  # the dark module

    def place(self, codewords: list[int]) -> None:
        size, index, total = self.size, 0, len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5  # the vertical timing pattern is skipped
            upward = (right + 1) & 2 == 0
            for vertical in range(size):
                row = size - 1 - vertical if upward else vertical
                for col in (right, right - 1):
                    if not self.function[row][col] and index < total:
                        self.dark[row][col] = (codewords[index >> 3] >> (7 - (index & 7))) & 1 == 1
                        index += 1
            right -= 2
        if index != total:
            raise AssertionError("the codewords did not fill the data area")

    def apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for row in range(self.size):
            for col in range(self.size):
                if not self.function[row][col] and test(row, col):
                    self.dark[row][col] = not self.dark[row][col]


# Mask conditions, with i the row and j the column (ISO/IEC 18004, table 10).
_MASKS = (
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
)
_FINDER_LIKE = ((True, False, True, True, True, False, True, False, False, False, False),
                (False, False, False, False, True, False, True, True, True, False, True))


def penalty(modules: list[list[bool]] | tuple[tuple[bool, ...], ...]) -> int:
    """The four penalty rules: runs of five or more, 2x2 blocks, finder-like patterns, dark balance."""
    size = len(modules)
    lines = [list(row) for row in modules] + [[modules[r][c] for r in range(size)] for c in range(size)]
    score = 0
    for line in lines:
        run = 1
        for k in range(1, size + 1):
            if k < size and line[k] == line[k - 1]:
                run += 1
                continue
            if run >= 5:
                score += 3 + (run - 5)
            run = 1
        for k in range(size - 10):
            if tuple(line[k:k + 11]) in _FINDER_LIKE:
                score += 40
    for r in range(size - 1):
        for c in range(size - 1):
            if modules[r][c] == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in modules)
    score += 10 * (abs(dark * 100 // (size * size) - 50) // 5)
    return score


def encode(data: bytes | str, *, mask: int | None = None) -> QrCode:
    """Encode ``data`` (UTF-8 for text) in the smallest version that holds it.

    The mask is the one of the eight with the lowest penalty (the first of equals), unless ``mask`` names one.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if mask is not None and mask not in range(8):
        raise ValueError("a mask is 0 to 7")
    version = _choose_version(len(raw))
    codewords = _interleave(_data_codewords(raw, version), version)
    best: tuple[int, int, list[list[bool]]] | None = None
    for mask in (range(8) if mask is None else (mask,)):
        matrix = _Matrix(version)
        matrix.draw_function_patterns()
        matrix.place(codewords)
        matrix.apply_mask(mask)
        matrix.draw_format(mask)
        score = penalty(matrix.dark)
        if best is None or score < best[0]:
            best = (score, mask, matrix.dark)
    assert best is not None
    _, mask, modules = best
    return QrCode(version, mask, tuple(tuple(row) for row in modules))


# -- drawing ---------------------------------------------------------------------------------------
_UPPER, _LOWER, _FULL = "\u2580", "\u2584", "\u2588"


def render_text(code: QrCode, *, border: int = QUIET_ZONE, terminal_background: str = "dark") -> str:
    """The code in half-block characters, two module rows per line, with a quiet zone of ``border`` modules.

    A glyph is drawn in the terminal's text colour. On a dark background (the default) the light modules
    are drawn and the dark ones left as background, so that the code reads dark on light, as scanners expect.
    """
    if terminal_background not in ("dark", "light"):
        raise ValueError("terminal_background is 'dark' or 'light'")
    size = code.size + 2 * border

    def drawn(row: int, col: int) -> bool:
        inside = border <= row < border + code.size and border <= col < border + code.size
        dark = inside and code.modules[row - border][col - border]
        return (not dark) if terminal_background == "dark" else dark

    lines = []
    for row in range(0, size, 2):
        line = []
        for col in range(size):
            top, bottom = drawn(row, col), row + 1 < size and drawn(row + 1, col)
            line.append(_FULL if top and bottom else _UPPER if top else _LOWER if bottom else " ")
        lines.append("".join(line))
    return "\n".join(lines) + "\n"


def render_svg(code: QrCode, *, border: int = QUIET_ZONE, module_px: int = 8) -> str:
    """The code as a small SVG document: a white square and one black square per dark module."""
    if border < 0 or module_px < 1:
        raise ValueError("border is 0 or more and module_px 1 or more")
    side = (code.size + 2 * border) * module_px
    squares = "".join(
        f'<rect x="{(col + border) * module_px}" y="{(row + border) * module_px}" '
        f'width="{module_px}" height="{module_px}"/>'
        for row in range(code.size) for col in range(code.size) if code.modules[row][col]
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" viewBox="0 0 {side} {side}" '
            f'shape-rendering="crispEdges"><rect width="{side}" height="{side}" fill="#fff"/>'
            f'<g fill="#000">{squares}</g></svg>')
