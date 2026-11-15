"""Fake-binary hash and version fixtures (lane LAUNCH-CHECK, first slice). No real engine is here.

Two tables, both plain data:

* ``HASHES``: byte strings shaped like the files the check meets (a native executable by its magic, a
  same-size replacement, a truncated copy, a script, an empty file, files at the hasher's 1 MiB chunk
  edge), each with the SHA-256, size and ``file_kind`` recorded when the fixture was written. The bytes
  are built from constants, so a run anywhere rebuilds the same files. ``check_hash_fixture`` rebuilds
  a fixture and refuses it if a recorded value does not match: a fixture that lies is caught, not used.
* ``VERSIONS``: version lines per engine, real shapes and broken ones (truncated, oversized, a byte
  that is not UTF-8 as the probe decodes it, control characters, a prerelease, another engine's line),
  each with the parse it must give and the outcome ``grant()`` must reach when a probe returns it.

None of these files can run: the native ones carry only a magic number and filler, so a test that
needs a process uses ``fixtures.Factory`` and these are for hashing, kind and parsing only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 9  # 64-bit, little-endian, version 1: what file_kind reads as native
PE = b"MZ" + b"\x90\x00" * 7
MACHO = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" * 3
CHUNK = 1 << 20  # binary.sha256_file reads in chunks of this size


def _fill(seed: bytes, size: int) -> bytes:
    """Deterministic filler: SHA-256 counter blocks of ``seed``, cut to ``size`` bytes."""
    out = bytearray()
    n = 0
    while len(out) < size:
        out += hashlib.sha256(seed + n.to_bytes(8, "big")).digest()
        n += 1
    return bytes(out[:size])


def _native(head: bytes, seed: bytes, size: int) -> bytes:
    return head + _fill(seed, size - len(head))


def _flip_last(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 1])


_SMALL = _native(ELF, b"csl-lc-small", 4096)


@dataclass(frozen=True)
class HashFixture:
    name: str
    data: bytes
    sha256: str  # recorded when the fixture was written; never computed here
    size: int
    kind: str  # binary.file_kind's answer: native, script or other


HASHES: tuple[HashFixture, ...] = (
    HashFixture("elf-small", _SMALL,
                "7af2580ccdf4bbf2be606a85429194885f5338fdad9fa62bfab2a3fd965421e8", 4096, "native"),
    HashFixture("elf-small-same-size-replacement", _flip_last(_SMALL),
                "223f51ca5de74a19501fa9004bd5b091c219bd5f4319a802be034818fd1e1d36", 4096, "native"),
    HashFixture("elf-small-truncated", _SMALL[:2048],
                "8fb2fae4c52aa53b6e4754c4aaa1940919140a30424fb5aeadb2836467e715f4", 2048, "native"),
    HashFixture("elf-chunk-edge", _native(ELF, b"csl-lc-edge", CHUNK),
                "6053fb503f9e615a82f819d9d9042356213f067c3e14c0c7ff83fec24ab26f65", CHUNK, "native"),
    HashFixture("elf-chunk-edge-plus-one", _native(ELF, b"csl-lc-edge", CHUNK + 1),
                "4275b7fbf28505f83183fd785ceb4f370b08587eed65b5096a4d52d795f7c153", CHUNK + 1, "native"),
    HashFixture("pe-small", _native(PE, b"csl-lc-pe", 1024),
                "797b5222ab81f3a9c4fa8bb2a02f68030def4e1c71e991102066ab21bb1001a6", 1024, "native"),
    HashFixture("macho-small", _native(MACHO, b"csl-lc-macho", 1024),
                "21d2c34c5e7273715ddb8039f8e96f508b1aec28595d28a82e38143cad1f7c38", 1024, "native"),
    HashFixture("script", b"#!/bin/sh\necho '2.1.318 (Claude Code)'\n",
                "f0a06ba10c709fff787ce66cd85aa3e247babdb9aa18f1668fbdebc95455eec2", 39, "script"),
    HashFixture("empty", b"",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", 0, "other"),
    HashFixture("other", _fill(b"csl-lc-other", 512),
                "78b24f463fb3e5f114abc35a8b66bb6b80e88330547a538eb7d2e49f692d0ac0", 512, "other"),
)


def hash_fixture(name: str) -> HashFixture:
    for fx in HASHES:
        if fx.name == name:
            return fx
    raise KeyError(name)


def check_hash_fixture(fx: HashFixture) -> None:
    """Refuse a fixture whose recorded digest, size or kind does not match its bytes."""
    digest = hashlib.sha256(fx.data).hexdigest()
    if digest != fx.sha256 or len(fx.data) != fx.size:
        raise AssertionError(f"hash fixture {fx.name} lies: recorded {fx.sha256}/{fx.size}, bytes give {digest}/{len(fx.data)}")
    head = fx.data[:4]
    kind = "script" if head.startswith(b"#!") else "native" if head.startswith((ELF[:4], PE[:2], MACHO[:4])) else "other"
    if kind != fx.kind:
        raise AssertionError(f"hash fixture {fx.name} lies: recorded kind {fx.kind}, bytes give {kind}")


@dataclass(frozen=True)
class VersionFixture:
    engine: str
    raw: str  # the first line a probe returns
    key: tuple[int, ...] | None  # parse_version's key, or None for unparsed
    prerelease: str | None
    outcome: str  # "granted" or the refusal code grant() reaches with this line


VERSIONS: tuple[VersionFixture, ...] = (
    # Real shapes (README "Identity"): each engine's line as its binary prints it.
    VersionFixture("claude_code", "2.1.318 (Claude Code)", (2, 1, 318), None, "granted"),
    VersionFixture("codex", "codex-cli 0.160.0", (0, 160, 0), None, "granted"),
    VersionFixture("opencode", "opencode v2.2.5", (2, 2, 5), None, "granted"),
    VersionFixture("grok", "grok 1.0.0", (1, 0, 0), None, "granted"),
    # Floors.
    VersionFixture("claude_code", "2.1.311 (Claude Code)", (2, 1, 311), None, "version_refused"),
    VersionFixture("claude_code", "2.1.314 (Claude Code)", (2, 1, 314), None, "granted"),
    VersionFixture("codex", "codex-cli 0.130.9", (0, 130, 9), None, "version_refused"),
    VersionFixture("opencode", "opencode v1.18.32", (1, 18, 32), None, "version_refused"),
    VersionFixture("codex", "codex-cli 0.160.0-alpha.2", (0, 160, 0), "alpha.2", "version_refused"),
    # Identity.
    VersionFixture("claude_code", "2.1.318", (2, 1, 318), None, "identity_mismatch"),
    VersionFixture("claude_code", "codex-cli 2.1.318", (2, 1, 318), None, "engine_mismatch"),
    VersionFixture("grok", "2.1.318 (Claude Code)", (2, 1, 318), None, "engine_mismatch"),
    # Broken lines.
    VersionFixture("claude_code", "", None, None, "identity_mismatch"),
    VersionFixture("claude_code", "2.1. (Claude Code)", None, None, "version_unparsed"),
    VersionFixture("claude_code", "2.1.318 (Claude", (2, 1, 318), None, "granted"),  # truncated after the token
    VersionFixture("claude_code", "2.1.318abc (Claude Code)", None, None, "version_unparsed"),
    VersionFixture("claude_code", "2.1.318 (Claude Code) " + "x" * 300, None, None, "version_unparsed"),
    VersionFixture("claude_code", "2.1.318 (Claude\ufffd Code)", None, None, "version_unparsed"),
    VersionFixture("claude_code", "2.1.318 (Claude Code)\x1b[0m", None, None, "version_unparsed"),
    VersionFixture("claude_code", "2.1.318 (Claude Code)\x00", None, None, "version_unparsed"),
    VersionFixture("grok", "grok\t1.0.0", None, None, "version_unparsed"),
)
