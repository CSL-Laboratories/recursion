#!/usr/bin/env python3
"""Product-contract pin check: contract/PRODUCT.pin.json names exact csl-research bytes.

The product contract lives in csl-research; this repository only links to it by
repository, full commit, path and SHA-256 (D-044, CSL-585). Deterministic and
provider-free. Run from the repository root:

    python -X utf8 -B tools/check_product_pin.py
    python -X utf8 -B tools/check_product_pin.py --vault ../csl-research

The default run checks only the pin's shape. --vault reads the pinned blob from a
local csl-research clone and compares its SHA-256; it never touches the network.
A pass does not accept, admit or bind the contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import unicodedata
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = ROOT / "contract" / "PRODUCT.pin.json"

FORMAT = "csl-product-pin/v1"
REPOSITORY = "CSL-Laboratories/csl-research"
KEYS = {"format", "status", "repository", "path", "commit", "sha256", "claim", "limitations"}
PINNED_FIELDS = ("path", "commit", "sha256", "claim")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_TIMEOUT_SECONDS = 60
# Cc is control characters (NUL, CR, LF). Zl/Zp are Unicode line breaks. Cs is a surrogate.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})


def _git_env() -> dict[str, str]:
    """Environment for the vault read.

    Allow only GIT_NO_LAZY_FETCH. Every other GIT_* variable is dropped so the
    child cannot be redirected (GIT_DIR, GIT_COMMON_DIR, GIT_OBJECT_DIRECTORY,
    GIT_ALTERNATE_OBJECT_DIRECTORIES, and the rest).

    GIT_NO_LAZY_FETCH is unenforced on upstream git 2.43.0-2.43.3 without the
    CVE-2024-32465 backport. The --no-lazy-fetch flag exits 129 on git 2.43, so
    only this variable is set.

    HOME and XDG_* stay. Vault commands then set GIT_CEILING_DIRECTORIES to the
    vault's parent; an operator ceiling is not kept.
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


class _StoreRefusal(Exception):
    """The vault's git dir or object store is not the directory we were given."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _ObjectMissing(Exception):
    """The pinned object is not a regular object in the vault's own store."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _DeltaCycle(_ObjectMissing):
    """This path refers to an object already being resolved.

    That is a property of the path, not a reason to remember the oid as
    missing. The message matches the depth cap so callers see one refusal.
    """

    def __init__(self) -> None:
        super().__init__("delta chain is too deep")


class _DeltaTooDeep(_ObjectMissing):
    """The chain, counting this base, is deeper than git's pack.depth maximum."""

    def __init__(self) -> None:
        super().__init__("delta chain is too deep")


class _HashMismatch(_ObjectMissing):
    """These bytes inflate, but they are not the object named by this oid."""


class _DeltaMemo:
    """Results remembered for the duration of one ``get`` call.

    A base oid is stored only when the outcome does not depend on an object
    still on the current path. A cycle is not a stored miss. Each pack
    offset is inflated at most once, and an offset that failed for a reason
    other than a cycle or the depth cap is not expanded again.
    """

    def __init__(self) -> None:
        self.oid_ok: dict[bytes, tuple[bytes, bytes]] = {}
        self.oid_fail: dict[bytes, _ObjectMissing] = {}
        self.cycle_block: set[bytes] = set()
        self.inflight: set[bytes] = set()
        self.off_ok: dict[tuple[int, int], tuple[bytes, bytes]] = {}
        self.off_fail: dict[tuple[int, int], _ObjectMissing] = {}
        self.off_raw: dict[tuple[int, int], tuple[int, bytes, int | bytes]] = {}
        self.rejected: dict[tuple[bytes, int, int], _HashMismatch] = {}


class _LooseUnusable(_ObjectMissing):
    """A loose file is present, but it is not a usable copy of its object name."""


class _NotPresent(Exception):
    """A lookup path is absent. Distinct from a symlink or an unreadable file."""


def _ceiling_directories(vault: Path) -> str:
    """Absolute parents git must not search. Joined with os.pathsep."""
    texts: list[str] = []
    parents = [os.path.dirname(os.path.abspath(vault))]
    try:
        parents.append(os.path.dirname(os.path.realpath(vault)))
    except (OSError, ValueError):
        pass
    for text in parents:
        if not text or text in texts:
            continue
        if os.pathsep in text or any(char in text for char in "\r\n\x00"):
            raise OSError("cannot bound git discovery for this vault path")
        texts.append(text)
    if not texts:
        raise OSError("cannot bound git discovery for this vault path")
    return os.pathsep.join(texts)


def _git_env_for_vault(vault: Path) -> dict[str, str]:
    """Stripped environment plus a ceiling at the vault's parent."""
    env = _git_env()
    env["GIT_CEILING_DIRECTORIES"] = _ceiling_directories(vault)
    return env


def _points_at_a_path(path: Path) -> bool:
    """True when path exists, including a dangling symlink."""
    return path.is_symlink() or path.exists()


def _inside_vault(vault: Path, raw: str) -> bool:
    """True when raw resolves to vault or a path inside it."""
    text = raw.strip()
    if not text or any(char in text for char in "\n\r\x00"):
        return False
    path = Path(text)
    if not path.is_absolute():
        path = vault / path
    try:
        return path.resolve().is_relative_to(vault.resolve())
    except (OSError, ValueError):
        return False


def _layout_problem(vault: Path, git_dir: Path) -> str | None:
    """Refuse commondir, alternates, or an object store that leaves git_dir.

    git_dir is vault/.git for a non-bare repository and the vault itself when bare.
    """
    commondir = git_dir / "commondir"
    if _points_at_a_path(commondir):
        return f"vault object store is not self-contained: {commondir} exists"
    objects = git_dir / "objects"
    for name in ("alternates", "http-alternates"):
        alternate = objects / "info" / name
        if _points_at_a_path(alternate):
            return f"vault object store is not self-contained: {alternate} exists"
    return _objects_problem(vault, objects)


def _objects_problem(vault: Path, objects: Path) -> str | None:
    """Refuse an objects directory that is linked or resolves outside the vault."""
    if not _points_at_a_path(objects):
        return None
    try:
        resolved = objects.resolve()
        root = vault.resolve()
    except (OSError, ValueError) as exc:
        return f"vault object store is not self-contained: {objects} ({exc})"
    if not resolved.is_relative_to(root):
        return f"vault object store is not self-contained: {resolved} is outside {vault}"
    if objects.is_symlink():
        return f"vault object store is not self-contained: {objects} is a symlink"
    walked = _walk_objects(objects)
    if walked:
        return walked
    return _outside_reference_problem(objects)


def _walk_objects(objects: Path) -> str | None:
    """Refuse any symlink under objects, or any entry whose realpath leaves it.

    lstat only: symlinks are not followed.
    """
    root = Path(os.path.realpath(objects))

    def fail_closed(err: OSError) -> None:
        raise err

    for dirpath, dirnames, filenames in os.walk(objects, followlinks=False, onerror=fail_closed):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath) / name
            try:
                mode = os.lstat(entry).st_mode
            except OSError as exc:
                return f"vault object store is not self-contained: {entry} ({exc})"
            if stat.S_ISLNK(mode):
                return f"vault object store is not self-contained: {entry} is a symlink"
            try:
                resolved = Path(os.path.realpath(entry))
            except (OSError, ValueError) as exc:
                return f"vault object store is not self-contained: {entry} ({exc})"
            if not resolved.is_relative_to(root):
                return f"vault object store is not self-contained: {resolved} is outside {objects}"
    return None


def _objects_snapshot(objects: Path) -> tuple[tuple, ...]:
    """Identity of every entry under objects. Taken before and after the read."""
    if not _points_at_a_path(objects):
        return ()
    rows = []

    def fail_closed(err: OSError) -> None:
        raise err

    for dirpath, dirnames, filenames in os.walk(objects, followlinks=False, onerror=fail_closed):
        dirnames.sort()
        for name in (*dirnames, *sorted(filenames)):
            entry = Path(dirpath) / name
            st = os.lstat(entry)
            rows.append(
                (
                    os.path.relpath(entry, objects),
                    st.st_mode,
                    st.st_ino,
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                )
            )
    return tuple(rows)


_HASH_BYTES = {1: 20, 2: 32}
_OBJ_TYPE = {1: b"commit", 2: b"tree", 3: b"blob", 4: b"tag"}
# git pack.depth cannot exceed 4095. Chains deeper than that are refused.
_MAX_DELTA_DEPTH = 4095
_LOOSE_TYPES = {b"commit", b"tree", b"blob", b"tag"}
_TREE_MODES = {b"40000", b"040000"}
_BLOB_MODES = {b"100644", b"100755", b"120000", b"100664"}
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_HEX_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _plain_basename(name: str, suffix: str) -> bool:
    """True for one path component, with no '..' and the given suffix."""
    if not name.endswith(suffix) or "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith(".") or not _PLAIN_NAME.fullmatch(name):
        return False
    return len(name) > len(suffix)


def _read_nofollow(root: Path, relative: str) -> bytes:
    """Read a regular file under root. Symlinks and '..' components are refused."""
    parts = [part for part in relative.split("/") if part != ""]
    if not parts or any(part in {".", ".."} or "\x00" in part for part in parts):
        raise _StoreRefusal(f"vault object store is not self-contained: {root / relative}")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index != len(parts) - 1:
                flags |= os.O_DIRECTORY
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError as exc:
                raise _NotPresent(str(root / relative)) from exc
            except OSError as exc:
                raise _StoreRefusal(
                    f"vault object store is not self-contained: {root / relative} ({exc})"
                ) from exc
            os.close(fd)
            fd = nxt
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _StoreRefusal(
                f"vault object store is not self-contained: {root / relative} is not a regular file"
            )
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _regular_basename(directory: Path, name: str) -> bool:
    """True when name is a regular, non-symlink file whose realpath stays in directory."""
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return False
    path = directory / name
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        resolved = Path(os.path.realpath(path))
        root = Path(os.path.realpath(directory))
    except (OSError, ValueError):
        return False
    return resolved.is_relative_to(root)


def _midx_pack_names(data: bytes) -> list[str]:
    """Pack names from a version 1 or 2 multi-pack-index. Unknown layouts are refused.

    Version 2 is the documented default (``midx.version``). Its header, chunk
    table, PNAM chunk and trailer match version 1; version 2 may list packs in
    any order, which this reader already allows. A non-zero base-midx count is
    still refused.
    """
    if len(data) < 12 or data[:4] != b"MIDX":
        raise _StoreRefusal("not a multi-pack-index")
    version = data[4]
    hash_version = data[5]
    num_chunks = data[6]
    num_base = data[7]
    if version not in (1, 2) or hash_version not in _HASH_BYTES or num_base != 0 or num_chunks < 1:
        raise _StoreRefusal(f"unsupported multi-pack-index version {version}")
    hash_len = _HASH_BYTES[hash_version]
    toc_bytes = (num_chunks + 1) * 12
    if len(data) < 12 + toc_bytes + hash_len:
        raise _StoreRefusal("truncated multi-pack-index")
    chunks: list[tuple[bytes, int]] = []
    for index in range(num_chunks + 1):
        off = 12 + index * 12
        chunks.append((data[off : off + 4], int.from_bytes(data[off + 4 : off + 12], "big")))
    if chunks[-1][0] != b"\x00\x00\x00\x00":
        raise _StoreRefusal("multi-pack-index chunk table is truncated")
    end = chunks[-1][1]
    if end < 12 + toc_bytes or end + hash_len != len(data):
        raise _StoreRefusal("multi-pack-index trailer does not match its chunks")
    digest = data[-hash_len:]
    body = data[:-hash_len]
    expect = hashlib.sha1(body).digest() if hash_version == 1 else hashlib.sha256(body).digest()
    if digest != expect:
        raise _StoreRefusal("multi-pack-index checksum does not match")
    ordered = sorted(chunks[:-1], key=lambda item: item[1])
    bounds = [start for _, start in ordered] + [end]
    pnam = None
    for index, (cid, start) in enumerate(ordered):
        stop = bounds[index + 1]
        if start < 12 + toc_bytes or stop < start or stop > end:
            raise _StoreRefusal("multi-pack-index chunk offset is out of range")
        if cid == b"PNAM":
            if pnam is not None:
                raise _StoreRefusal("multi-pack-index has more than one PNAM chunk")
            pnam = data[start:stop]
    if pnam is None:
        raise _StoreRefusal("multi-pack-index has no PNAM chunk")
    num_packs = int.from_bytes(data[8:12], "big")
    if num_packs > len(pnam):
        raise _StoreRefusal("multi-pack-index pack count does not fit the PNAM chunk")
    names: list[str] = []
    pos = 0
    for _ in range(num_packs):
        nul = pnam.find(b"\x00", pos)
        if nul < 0:
            raise _StoreRefusal("multi-pack-index PNAM chunk is truncated")
        try:
            names.append(pnam[pos:nul].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise _StoreRefusal("multi-pack-index PNAM chunk is not utf-8") from exc
        pos = nul + 1
    if any(byte != 0 for byte in pnam[pos:]):
        raise _StoreRefusal("multi-pack-index PNAM chunk has trailing data")
    return names


def _require_pack_pair(pack_dir: Path, idx_name: str) -> None:
    """idx_name must be a plain basename, and both the idx and pack must sit in pack_dir."""
    if not _plain_basename(idx_name, ".idx"):
        raise _StoreRefusal(f"names {idx_name!r}")
    pack_name = idx_name[: -len(".idx")] + ".pack"
    if not _plain_basename(pack_name, ".pack"):
        raise _StoreRefusal(f"names {idx_name!r}")
    for name in (idx_name, pack_name):
        if not _regular_basename(pack_dir, name):
            raise _StoreRefusal(f"names {name!r}")


def _check_midx_file(pack_dir: Path, relative: str, root: Path) -> None:
    try:
        data = _read_nofollow(root, relative)
    except _NotPresent as exc:
        raise _StoreRefusal(f"vault object store is not self-contained: {root / relative} is missing") from exc
    except _StoreRefusal as refusal:
        if refusal.message.startswith("vault object store is not self-contained:"):
            raise
        raise _StoreRefusal(f"vault object store is not self-contained: {root / relative} ({refusal.message})") from refusal
    try:
        names = _midx_pack_names(data)
    except _StoreRefusal as refusal:
        detail = refusal.message
        if detail.startswith("vault object store is not self-contained:"):
            raise
        raise _StoreRefusal(f"vault object store is not self-contained: {root / relative} ({detail})") from refusal
    for name in names:
        try:
            _require_pack_pair(pack_dir, name)
        except _StoreRefusal as refusal:
            detail = refusal.message
            if detail.startswith("vault object store is not self-contained:"):
                raise
            raise _StoreRefusal(f"vault object store is not self-contained: {root / relative} {detail}") from refusal


def _outside_reference_problem(objects: Path) -> str | None:
    """Refuse index files whose recorded names can point outside objects/pack."""
    try:
        return _outside_reference_problem_raised(objects)
    except _StoreRefusal as refusal:
        return refusal.message


def _outside_reference_problem_raised(objects: Path) -> None:
    pack_dir = objects / "pack"
    root = objects
    midx = pack_dir / "multi-pack-index"
    if _points_at_a_path(midx):
        _check_midx_file(pack_dir, "pack/multi-pack-index", root)
    midx_d = pack_dir / "multi-pack-index.d"
    if _points_at_a_path(midx_d):
        if midx_d.is_symlink() or not midx_d.is_dir():
            raise _StoreRefusal(f"vault object store is not self-contained: {midx_d} is not a directory")
        chain_rel = "pack/multi-pack-index.d/multi-pack-index-chain"
        if _points_at_a_path(midx_d / "multi-pack-index-chain"):
            try:
                text = _read_nofollow(root, chain_rel)
            except _NotPresent as exc:
                raise _StoreRefusal(f"vault object store is not self-contained: {midx_d} chain is missing") from exc
            try:
                lines = text.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise _StoreRefusal(f"vault object store is not self-contained: {midx_d} chain is not utf-8") from exc
            for line in lines:
                if not _HEX_NAME.fullmatch(line):
                    raise _StoreRefusal(f"vault object store is not self-contained: {midx_d} names {line!r}")
                layer = f"multi-pack-index-{line}.midx"
                if not _regular_basename(midx_d, layer):
                    raise _StoreRefusal(f"vault object store is not self-contained: {midx_d} names {layer!r}")
                _check_midx_file(pack_dir, f"pack/multi-pack-index.d/{layer}", root)
    graphs = objects / "info" / "commit-graphs"
    chain = graphs / "commit-graph-chain"
    if _points_at_a_path(chain):
        try:
            text = _read_nofollow(root, "info/commit-graphs/commit-graph-chain")
        except _NotPresent as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {chain} is missing") from exc
        try:
            lines = text.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {chain} is not utf-8") from exc
        for line in lines:
            if not _HEX_NAME.fullmatch(line):
                raise _StoreRefusal(f"vault object store is not self-contained: {chain} names {line!r}")
            graph = f"graph-{line}.graph"
            if not _regular_basename(graphs, graph):
                raise _StoreRefusal(f"vault object store is not self-contained: {chain} names {graph!r}")
    packs_file = objects / "info" / "packs"
    if _points_at_a_path(packs_file):
        try:
            text = _read_nofollow(root, "info/packs")
        except _NotPresent as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {packs_file} is missing") from exc
        try:
            lines = text.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {packs_file} is not utf-8") from exc
        for line in lines:
            if line == "":
                continue
            if not line.startswith("P "):
                raise _StoreRefusal(f"vault object store is not self-contained: {packs_file} names {line!r}")
            name = line[2:]
            if not _plain_basename(name, ".pack"):
                raise _StoreRefusal(f"vault object store is not self-contained: {packs_file} names {name!r}")
            idx_name = name[: -len(".pack")] + ".idx"
            if not _regular_basename(pack_dir, name) or not _regular_basename(pack_dir, idx_name):
                raise _StoreRefusal(f"vault object store is not self-contained: {packs_file} names {name!r}")
    return None


def _inflate(buf: bytes, offset: int, size: int) -> bytes:
    """Inflate one pack zlib stream, refusing output past the declared size.

    ``max_length`` is ``size + 1`` so a stream that expands further is detected
    from the extra byte or from ``unconsumed_tail`` before that output is kept.
    Bytes after a finished stream (the next pack object) stay in ``unused_data``.
    """
    if size < 0 or size >= sys.maxsize:
        raise _ObjectMissing("pack object size does not match its header")
    decompressor = zlib.decompressobj()
    try:
        data = decompressor.decompress(buf[offset:], max_length=size + 1)
    except (zlib.error, OverflowError) as exc:
        raise _ObjectMissing(f"pack zlib stream is unreadable ({exc})") from exc
    if len(data) > size or decompressor.unconsumed_tail:
        raise _ObjectMissing("pack object size does not match its header")
    if not decompressor.eof:
        try:
            rest = decompressor.flush()
        except zlib.error as exc:
            raise _ObjectMissing(f"pack zlib stream is unreadable ({exc})") from exc
        if len(data) + len(rest) > size:
            raise _ObjectMissing("pack object size does not match its header")
        data += rest
        if not decompressor.eof:
            raise _ObjectMissing("pack zlib stream is truncated")
    if len(data) != size:
        raise _ObjectMissing("pack object size does not match its header")
    return data


def _delta_size(buf: bytes, pos: int) -> tuple[int, int]:
    size = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise _ObjectMissing("delta header is truncated")
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return size, pos
        shift += 7


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    base_size, pos = _delta_size(delta, 0)
    result_size, pos = _delta_size(delta, pos)
    if base_size != len(base):
        raise _ObjectMissing("delta base size does not match")
    if result_size < 0 or result_size >= sys.maxsize:
        raise _ObjectMissing("delta result size does not match")
    out = bytearray()
    while pos < len(delta):
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if cmd & (1 << bit):
                    if pos >= len(delta):
                        raise _ObjectMissing("delta copy is truncated")
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if cmd & (1 << (4 + bit)):
                    if pos >= len(delta):
                        raise _ObjectMissing("delta copy is truncated")
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            if size > len(base) or offset > len(base) - size:
                raise _ObjectMissing("delta copy extends past the base")
            if size > result_size - len(out):
                raise _ObjectMissing("delta result size does not match")
            out += base[offset : offset + size]
        elif cmd:
            if cmd > result_size - len(out):
                raise _ObjectMissing("delta result size does not match")
            if pos + cmd > len(delta):
                raise _ObjectMissing("delta insert is truncated")
            out += delta[pos : pos + cmd]
            pos += cmd
        else:
            raise _ObjectMissing("delta opcode 0")
    if len(out) != result_size:
        raise _ObjectMissing("delta result size does not match")
    return bytes(out)


def _pack_object_at(pack: bytes, offset: int) -> tuple[int, bytes, int | bytes]:
    """Return type, inflated payload, and ofs-delta base offset or ref-delta oid."""
    if pack[:4] != b"PACK":
        raise _ObjectMissing("pack signature is not PACK")
    if offset < 12 or offset >= len(pack) - 20:
        raise _ObjectMissing("pack offset is outside the pack")
    pos = offset
    byte = pack[pos]
    pos += 1
    obj_type = (byte >> 4) & 7
    size = byte & 15
    shift = 4
    while byte & 0x80:
        if pos >= len(pack) or shift > 63:
            raise _ObjectMissing("pack object header is truncated")
        byte = pack[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    extra: int | bytes = 0
    if obj_type == 6:
        if pos >= len(pack):
            raise _ObjectMissing("ofs-delta header is truncated")
        byte = pack[pos]
        pos += 1
        relative = byte & 0x7F
        while byte & 0x80:
            if pos >= len(pack):
                raise _ObjectMissing("ofs-delta header is truncated")
            byte = pack[pos]
            pos += 1
            relative = ((relative + 1) << 7) | (byte & 0x7F)
        extra = offset - relative
    elif obj_type == 7:
        if pos + 20 > len(pack):
            raise _ObjectMissing("ref-delta oid is truncated")
        extra = pack[pos : pos + 20]
        pos += 20
    elif obj_type not in _OBJ_TYPE:
        raise _ObjectMissing(f"unsupported pack object type {obj_type}")
    payload = _inflate(pack, pos, size)
    if len(payload) != size:
        raise _ObjectMissing("pack object size does not match its header")
    return obj_type, payload, extra


class _PackIndex:
    def __init__(self, idx: bytes, pack: bytes) -> None:
        self.pack = pack
        self._names, self._offsets = _parse_idx(idx)
        if hashlib.sha1(pack[:-20]).digest() != pack[-20:]:
            raise _ObjectMissing("pack checksum does not match")

    def offset(self, oid: bytes) -> int | None:
        lo = 0
        hi = len(self._names)
        while lo < hi:
            mid = (lo + hi) // 2
            name = self._names[mid]
            if name < oid:
                lo = mid + 1
            elif name > oid:
                hi = mid
            else:
                return self._offsets[mid]
        return None


def _parse_idx(data: bytes) -> tuple[list[bytes], list[int]]:
    if data[:4] == b"\xfftOc":
        version = struct.unpack(">I", data[4:8])[0]
        if version != 2:
            raise _ObjectMissing(f"unsupported pack index version {version}")
        fanout_at = 8
        names, offsets, count = _idx_v2_tables(data, fanout_at)
        return names, offsets
    fanout_at = 0
    if len(data) < 256 * 4 + 40:
        raise _ObjectMissing("pack index is truncated")
    count = struct.unpack(">I", data[fanout_at + 255 * 4 : fanout_at + 256 * 4])[0]
    entries_at = fanout_at + 256 * 4
    need = entries_at + count * 24 + 40
    if len(data) != need:
        raise _ObjectMissing("pack index size does not match its fanout")
    names = []
    offsets = []
    for index in range(count):
        at = entries_at + index * 24
        offsets.append(struct.unpack(">I", data[at : at + 4])[0])
        names.append(data[at + 4 : at + 24])
    if names != sorted(names):
        raise _ObjectMissing("pack index is not sorted")
    return names, offsets


def _idx_v2_tables(data: bytes, fanout_at: int) -> tuple[list[bytes], list[int], int]:
    if len(data) < fanout_at + 256 * 4:
        raise _ObjectMissing("pack index is truncated")
    count = struct.unpack(">I", data[fanout_at + 255 * 4 : fanout_at + 256 * 4])[0]
    names_at = fanout_at + 256 * 4
    crc_at = names_at + count * 20
    off_at = crc_at + count * 4
    large_at = off_at + count * 4
    if len(data) < large_at + 40:
        raise _ObjectMissing("pack index is truncated")
    large_count = (len(data) - 40 - large_at) // 8
    if large_at + large_count * 8 + 40 != len(data):
        raise _ObjectMissing("pack index size does not match its tables")
    names = [data[names_at + index * 20 : names_at + (index + 1) * 20] for index in range(count)]
    if names != sorted(names):
        raise _ObjectMissing("pack index is not sorted")
    offsets = []
    for index in range(count):
        at = off_at + index * 4
        value = struct.unpack(">I", data[at : at + 4])[0]
        if value & 0x80000000:
            slot = value & 0x7FFFFFFF
            if slot >= large_count:
                raise _ObjectMissing("pack index large offset is out of range")
            at = large_at + slot * 8
            value = struct.unpack(">Q", data[at : at + 8])[0]
        offsets.append(value)
    return names, offsets, count


def _parse_loose(raw: bytes, hex_oid: str) -> tuple[bytes, bytes]:
    """Inflate a loose object, capping output at the size declared in its header.

    The header is read first (it is only a few dozen bytes). The remaining
    content is then limited to that size plus one byte, so a stream that
    expands further is refused before it is retained. Bytes after a finished
    zlib member are ignored, matching git. Output past the declared size, a
    stream that has not ended with more input still waiting, and a short read
    are refused. The caller still checks the object hash.
    """
    decompressor = zlib.decompressobj()
    try:
        window = decompressor.decompress(raw, max_length=64)
    except (zlib.error, OverflowError) as exc:
        raise _LooseUnusable(f"loose object {hex_oid} is unreadable ({exc})") from exc
    nul = window.find(b"\0")
    if nul < 0 or b" " not in window[:nul]:
        raise _LooseUnusable(f"loose object {hex_oid} has no header")
    header = window[:nul]
    content = window[nul + 1 :]
    typ, size_text = header.split(b" ", 1)
    try:
        size = int(size_text.decode("ascii"))
    except ValueError as exc:
        raise _LooseUnusable(f"loose object {hex_oid} has a bad size") from exc
    canonical = str(size).encode("ascii")
    if size < 0 or size >= sys.maxsize or size_text != canonical or typ not in _LOOSE_TYPES:
        raise _LooseUnusable(f"loose object {hex_oid} header does not match")
    if len(content) > size:
        raise _LooseUnusable(f"loose object {hex_oid} header does not match")
    if len(content) < size:
        if decompressor.eof or not decompressor.unconsumed_tail:
            raise _LooseUnusable(f"loose object {hex_oid} header does not match")
        try:
            more = decompressor.decompress(decompressor.unconsumed_tail, max_length=size - len(content) + 1)
        except (zlib.error, OverflowError) as exc:
            raise _LooseUnusable(f"loose object {hex_oid} is unreadable ({exc})") from exc
        # A finished member leaves the following bytes in unused_data. Python 3.12
        # also copies that tail into unconsumed_tail once eof is set. That is not
        # more object data. More input before eof, or output past the declared size, is.
        if len(more) > size - len(content) or (not decompressor.eof and decompressor.unconsumed_tail):
            raise _LooseUnusable(f"loose object {hex_oid} header does not match")
        content += more
    if not decompressor.eof:
        try:
            extra = decompressor.flush()
        except zlib.error as exc:
            raise _LooseUnusable(f"loose object {hex_oid} is unreadable ({exc})") from exc
        if len(content) + len(extra) > size:
            raise _LooseUnusable(f"loose object {hex_oid} header does not match")
        content += extra
        if not decompressor.eof:
            raise _LooseUnusable(f"loose object {hex_oid} is unreadable (truncated)")
    if len(content) != size:
        raise _LooseUnusable(f"loose object {hex_oid} header does not match")
    return typ, content


class _ObjectStore:
    """Objects read from loose files and pack files inside one objects directory."""

    def __init__(self, objects: Path) -> None:
        self.objects = objects
        self.packs = _load_packs(objects / "pack", objects)
        self.cache: dict[bytes, tuple[bytes, bytes]] = {}
        self._memo: _DeltaMemo | None = None

    def get(self, oid: bytes) -> tuple[bytes, bytes]:
        cached = self.cache.get(oid)
        if cached is not None:
            return cached
        loose_error: _LooseUnusable | None = None
        try:
            loose = self._loose(oid)
        except _LooseUnusable as exc:
            loose = None
            loose_error = exc
        if loose is not None:
            self._verify(oid, loose[0], loose[1])
            self.cache[oid] = loose
            return loose
        # The first pack that names this oid may not hash to it. Try each later
        # copy and keep only bytes that verify. Unreadable copies are skipped the
        # same way; nothing is cached until the hash matches. The memo is shared
        # by every copy tried for this call, so a failed base is not re-walked.
        # This oid stays in flight so a REF chain back to it is a cycle for this
        # branch, not a stored miss that would hide a later real copy.
        memo = _DeltaMemo()
        self._memo = memo
        memo.inflight.add(oid)
        try:
            pack_error: _ObjectMissing | None = None
            for pack in self.packs:
                offset = pack.offset(oid)
                if offset is None:
                    continue
                try:
                    typ, content = self._resolve(pack, offset)
                    self._verify(oid, typ, content)
                except _ObjectMissing as exc:
                    pack_error = exc
                    continue
                self.cache[oid] = (typ, content)
                return typ, content
            if pack_error is not None:
                raise pack_error
            if loose_error is not None:
                raise loose_error
            raise _ObjectMissing(oid.hex())
        finally:
            self._memo = None

    def _verify(self, oid: bytes, typ: bytes, content: bytes) -> None:
        header = typ + b" " + str(len(content)).encode("ascii") + b"\0"
        if hashlib.sha1(header + content).digest() != oid:
            raise _HashMismatch(f"object {oid.hex()} does not match its bytes")

    def _loose(self, oid: bytes) -> tuple[bytes, bytes] | None:
        """Return a valid loose object.

        A missing file is ``None``. A file that will not inflate, or whose
        bytes are not the object named by its path, raises ``_LooseUnusable``
        so the caller can use a packed copy. Symlinks still raise
        ``_StoreRefusal`` from ``_read_nofollow``.
        """
        hex_oid = oid.hex()
        try:
            raw = _read_nofollow(self.objects, f"{hex_oid[:2]}/{hex_oid[2:]}")
        except _NotPresent:
            return None
        typ, content = _parse_loose(raw, hex_oid)
        if hashlib.sha1(typ + b" " + str(len(content)).encode("ascii") + b"\0" + content).digest() != oid:
            raise _LooseUnusable(f"loose object {hex_oid} does not match its name")
        return typ, content

    def _resolve(self, pack: _PackIndex, offset: int) -> tuple[bytes, bytes]:
        """Resolve one pack object, following deltas iteratively.

        Visited pack offsets stop a self-delta or a loop without walking the
        depth cap. The cap is git's ``pack.depth`` maximum. It is checked
        again after each delta, so a loose or cached leaf counts the same as
        a packed one: 4095 deltas resolve and 4096 are refused. A REF base
        uses the same rule as ``get``: the loose object, then each pack copy,
        keeping the first copy whose bytes hash to that oid. A copy that does
        not hash is skipped and is not cached.

        For this ``get``, each base oid is resolved at most once and each pack
        offset is inflated at most once. A failure is remembered when it does
        not depend on an oid still on the current path. A cycle is only for
        this branch.
        """
        # Tests build a store with ``__new__`` and call ``_resolve`` directly.
        own_memo = getattr(self, "_memo", None) is None
        if own_memo:
            self._memo = _DeltaMemo()
        memo = self._memo
        assert memo is not None
        try:
            return self._resolve_memo(pack, offset, memo)
        finally:
            if own_memo:
                self._memo = None

    def _resolve_memo(self, pack: _PackIndex, offset: int, memo: _DeltaMemo) -> tuple[bytes, bytes]:
        seen: set[tuple[int, int]] = set()
        trail: list[tuple[int, int]] = []

        def undo(to: int) -> None:
            while len(trail) > to:
                seen.remove(trail.pop())

        def release(dying: dict) -> None:
            if dying.get("holds_inflight"):
                memo.inflight.discard(dying["base_oid"])
                dying["holds_inflight"] = False

        def note(target: dict, exc: _ObjectMissing) -> None:
            target["error"] = exc
            if isinstance(exc, _DeltaCycle):
                target["cycle_blocked"] = True
            elif isinstance(exc, _DeltaTooDeep):
                target["depth_blocked"] = True
            else:
                target["hard_error"] = exc

        def fail(exc: _ObjectMissing) -> None:
            dying = frames[-1]
            release(dying)
            root = dying.get("root_marker")
            if root is not None and not isinstance(exc, (_DeltaCycle, _DeltaTooDeep, _HashMismatch)):
                memo.off_fail.setdefault(root, exc)
            if isinstance(exc, _HashMismatch) and dying.get("expect") is not None and root is not None:
                memo.rejected[(dying["expect"], root[0], root[1])] = exc
            undo(dying["trail_at"])
            if len(frames) == 1:
                raise exc
            note(frames[-2], exc)
            frames.pop()

        def new_frame(obj_pack: _PackIndex, obj_offset: int, depth: int, trail_at: int, expect: bytes | None) -> dict:
            return {
                "pack": obj_pack,
                "offset": obj_offset,
                "depth": depth,
                "deltas": [],
                "typ": b"",
                "content": b"",
                "mode": "walk",
                "base_oid": b"",
                "cursor": 0,
                "error": None,
                "trail_at": trail_at,
                "expect": expect,
                "root_marker": (id(obj_pack), obj_offset),
                "hard_error": None,
                "cycle_blocked": False,
                "depth_blocked": False,
                "lookup_started": False,
                "holds_inflight": False,
            }

        def finish_base(target: dict, typ: bytes, content: bytes) -> None:
            oid = target["base_oid"]
            resolved = (typ, content)
            self.cache[oid] = resolved
            memo.oid_ok[oid] = resolved
            release(target)
            target["typ"] = typ
            target["content"] = content
            target["mode"] = "apply"

        frames: list[dict] = [new_frame(pack, offset, 0, 0, None)]
        while True:
            frame = frames[-1]
            if frame["mode"] == "walk":
                try:
                    if frame["depth"] > _MAX_DELTA_DEPTH:
                        raise _DeltaTooDeep()
                    marker = (id(frame["pack"]), frame["offset"])
                    failed = memo.off_fail.get(marker)
                    if failed is not None:
                        raise failed
                    resolved = memo.off_ok.get(marker)
                    if resolved is not None:
                        frame["typ"], frame["content"] = resolved
                        frame["mode"] = "apply"
                        continue
                    if marker in seen:
                        raise _DeltaCycle()
                    seen.add(marker)
                    trail.append(marker)
                    raw = memo.off_raw.get(marker)
                    if raw is None:
                        raw = _pack_object_at(frame["pack"].pack, frame["offset"])
                        memo.off_raw[marker] = raw
                    obj_type, payload, extra = raw
                    if obj_type in _OBJ_TYPE:
                        frame["typ"] = _OBJ_TYPE[obj_type]
                        frame["content"] = payload
                        frame["mode"] = "apply"
                        continue
                    frame["deltas"].append(payload)
                    frame["depth"] += 1
                    # The base is one more step. Check before a loose, cached,
                    # or packed leaf so 4096 is refused on every path.
                    if frame["depth"] > _MAX_DELTA_DEPTH:
                        raise _DeltaTooDeep()
                    if obj_type == 6:
                        if not isinstance(extra, int):
                            raise _ObjectMissing("ofs-delta base is invalid")
                        base_marker = (id(frame["pack"]), extra)
                        if base_marker in seen:
                            raise _DeltaCycle()
                        if extra < 12 or extra >= frame["offset"] or extra >= len(frame["pack"].pack) - 20:
                            raise _ObjectMissing("ofs-delta base is invalid")
                        frame["offset"] = extra
                        continue
                    if not isinstance(extra, bytes) or len(extra) != 20:
                        raise _ObjectMissing("ref-delta oid is truncated")
                    base = self._cached_or_loose(extra)
                    if base is not None:
                        frame["typ"], frame["content"] = base
                        frame["mode"] = "apply"
                        continue
                    frame["base_oid"] = extra
                    frame["cursor"] = 0
                    frame["error"] = None
                    frame["hard_error"] = None
                    frame["cycle_blocked"] = False
                    frame["depth_blocked"] = False
                    frame["lookup_started"] = False
                    frame["mode"] = "search"
                except _ObjectMissing as exc:
                    fail(exc)
                continue
            if frame["mode"] == "search":
                # The base object itself is one more step. Past the cap, every
                # copy is too deep, so do not scan the rest of the packs.
                if frame["depth"] > _MAX_DELTA_DEPTH:
                    fail(_DeltaTooDeep())
                    continue
                if not frame["lookup_started"]:
                    frame["lookup_started"] = True
                    oid = frame["base_oid"]
                    cached = self.cache.get(oid) or memo.oid_ok.get(oid)
                    if cached is not None:
                        finish_base(frame, cached[0], cached[1])
                        continue
                    missed = memo.oid_fail.get(oid)
                    if missed is not None:
                        fail(missed)
                        continue
                    if oid in memo.inflight or oid in memo.cycle_block:
                        fail(_DeltaCycle())
                        continue
                    memo.inflight.add(oid)
                    frame["holds_inflight"] = True
                pushed = False
                while frame["cursor"] < len(self.packs) and frame["mode"] == "search":
                    cand = self.packs[frame["cursor"]]
                    frame["cursor"] += 1
                    off = cand.offset(frame["base_oid"])
                    if off is None:
                        continue
                    marker = (id(cand), off)
                    failed = memo.off_fail.get(marker)
                    if failed is not None:
                        note(frame, failed)
                        continue
                    rejected = memo.rejected.get((frame["base_oid"], marker[0], marker[1]))
                    if rejected is not None:
                        note(frame, rejected)
                        continue
                    if marker in seen:
                        note(frame, _DeltaCycle())
                        continue
                    resolved = memo.off_ok.get(marker)
                    if resolved is not None:
                        try:
                            self._verify(frame["base_oid"], resolved[0], resolved[1])
                        except _HashMismatch as exc:
                            memo.rejected[(frame["base_oid"], marker[0], marker[1])] = exc
                            note(frame, exc)
                            continue
                        finish_base(frame, resolved[0], resolved[1])
                        break
                    frames.append(new_frame(cand, off, frame["depth"], len(trail), frame["base_oid"]))
                    pushed = True
                    break
                if frame["mode"] == "apply" or pushed:
                    continue
                oid = frame["base_oid"]
                if frame["cycle_blocked"]:
                    # Every usable copy ran into an oid still on this path.
                    # Remember that so the next parent does not walk them again,
                    # without recording the oid as missing.
                    memo.cycle_block.add(oid)
                    fail(_DeltaCycle())
                elif frame["depth_blocked"]:
                    # A copy was only too deep from here. A shallower chain may
                    # still resolve it, so this is not a stored miss.
                    fail(frame["hard_error"] or _DeltaTooDeep())
                else:
                    exc = frame["hard_error"] or frame["error"] or _ObjectMissing(oid.hex())
                    if not isinstance(exc, (_DeltaCycle, _DeltaTooDeep)):
                        memo.oid_fail[oid] = exc
                    fail(exc)
                continue
            content = frame["content"]
            try:
                for delta in reversed(frame["deltas"]):
                    content = _apply_delta(content, delta)
                root = frame.get("root_marker")
                if root is not None:
                    memo.off_ok[root] = (frame["typ"], content)
                expect = frame["expect"]
                if expect is not None:
                    self._verify(expect, frame["typ"], content)
                    resolved = (frame["typ"], content)
                    self.cache[expect] = resolved
                    memo.oid_ok[expect] = resolved
            except _ObjectMissing as exc:
                fail(exc)
                continue
            undo(frame["trail_at"])
            if len(frames) == 1:
                return frame["typ"], content
            parent = frames[-2]
            release(parent)
            parent["typ"] = frame["typ"]
            parent["content"] = content
            parent["mode"] = "apply"
            frames.pop()

    def _cached_or_loose(self, oid: bytes) -> tuple[bytes, bytes] | None:
        cached = self.cache.get(oid)
        if cached is not None:
            return cached
        memo = getattr(self, "_memo", None)
        if memo is not None:
            found = memo.oid_ok.get(oid)
            if found is not None:
                return found
        try:
            loose = self._loose(oid)
        except _LooseUnusable:
            return None
        if loose is None:
            return None
        self._verify(oid, loose[0], loose[1])
        self.cache[oid] = loose
        if memo is not None:
            memo.oid_ok[oid] = loose
        return loose


def _load_packs(pack_dir: Path, objects: Path) -> list[_PackIndex]:
    if not pack_dir.is_dir():
        return []
    packs = []
    try:
        entries = sorted(pack_dir.iterdir())
    except OSError as exc:
        raise _StoreRefusal(f"vault object store is not self-contained: {pack_dir} ({exc})") from exc
    for entry in entries:
        name = entry.name
        if not name.endswith(".idx"):
            continue
        try:
            mode = os.lstat(entry).st_mode
        except OSError as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {entry} ({exc})") from exc
        if not stat.S_ISREG(mode):
            raise _StoreRefusal(f"vault object store is not self-contained: {entry} is not a regular file")
        pack_name = name[: -len(".idx")] + ".pack"
        if not _regular_basename(pack_dir, pack_name):
            raise _StoreRefusal(f"vault object store is not self-contained: {pack_dir / pack_name} is not a regular file")
        try:
            idx = _read_nofollow(objects, f"pack/{name}")
            pack = _read_nofollow(objects, f"pack/{pack_name}")
        except _NotPresent as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {entry} disappeared") from exc
        try:
            packs.append(_PackIndex(idx, pack))
        except _ObjectMissing as exc:
            raise _StoreRefusal(f"vault object store is not self-contained: {entry} ({exc})") from exc
    return packs


def _tree_entries(data: bytes) -> dict[bytes, tuple[bytes, bytes]]:
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    pos = 0
    while pos < len(data):
        space = data.find(b" ", pos)
        nul = data.find(b"\0", space + 1 if space >= 0 else pos)
        if space < 0 or nul < 0 or nul + 20 > len(data):
            raise _ObjectMissing("tree object is truncated")
        mode = data[pos:space]
        name = data[space + 1 : nul]
        oid = data[nul + 1 : nul + 21]
        pos = nul + 21
        entries[name] = (mode, oid)
    return entries


def _blob_at(objects: Path, commit_hex: str, path: str) -> bytes:
    """Bytes of path in the commit, read only from objects. Git is not asked."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit_hex):
        raise _ObjectMissing("commit is not a 40-hex object name")
    parts = path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _ObjectMissing("path is not inside the commit")
    encoded = [part.encode("utf-8") for part in parts]
    store = _ObjectStore(objects)
    typ, content = store.get(bytes.fromhex(commit_hex))
    if typ != b"commit" or not content.startswith(b"tree ") or content[45:46] != b"\n":
        raise _ObjectMissing("commit object has no tree line")
    tree_hex = content[5:45]
    if not re.fullmatch(rb"[0-9a-f]{40}", tree_hex):
        raise _ObjectMissing("commit tree is not a 40-hex object name")
    oid = bytes.fromhex(tree_hex.decode("ascii"))
    for index, name in enumerate(encoded):
        typ, content = store.get(oid)
        if typ != b"tree":
            raise _ObjectMissing("path walks through a non-tree")
        mode, oid = _tree_entries(content).get(name, (b"", b""))
        if not oid:
            raise _ObjectMissing("path is not in the commit")
        last = index == len(encoded) - 1
        if last:
            if mode not in _BLOB_MODES:
                raise _ObjectMissing("path is not a blob")
            typ, content = store.get(oid)
            if typ != b"blob":
                raise _ObjectMissing("path is not a blob")
            return content
        if mode not in _TREE_MODES:
            raise _ObjectMissing("path walks through a non-tree")
    raise _ObjectMissing("path is not in the commit")


def _run_git(vault: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(vault), *args],
        check=True,
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=_git_env_for_vault(vault),
    )


def _rev_parse_lines(vault: Path, *args: str) -> list[str]:
    done = _run_git(vault, ["rev-parse", *args])
    return [line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines()]


def _require_git_identity(vault: Path) -> Path:
    """Return the git dir when it is vault/.git, or the vault itself when bare.

    Discovery uses GIT_CEILING_DIRECTORIES, so a child directory cannot see a
    repository above the vault.
    """
    lines = _rev_parse_lines(vault, "--is-bare-repository", "--absolute-git-dir")
    if len(lines) != 2 or lines[0] not in {"true", "false"} or not lines[1]:
        raise OSError(f"git rev-parse returned {lines!r}")
    bare = lines[0] == "true"
    absolute_git_dir = lines[1]
    try:
        git_real = Path(os.path.realpath(absolute_git_dir))
        vault_real = Path(os.path.realpath(vault))
    except (OSError, ValueError) as exc:
        raise OSError(f"cannot resolve git dir for {vault}: {exc}") from exc
    git_link = vault / ".git"
    if bare:
        if git_real != vault_real or _points_at_a_path(git_link):
            raise _StoreRefusal(
                "vault object store is not self-contained: "
                f"git dir {absolute_git_dir} is not {vault}"
            )
        return git_real
    link_is_dir = git_link.is_dir() and not git_link.is_symlink()
    try:
        expected = Path(os.path.realpath(git_link)) if link_is_dir else None
    except (OSError, ValueError) as exc:
        raise OSError(f"cannot resolve {git_link}: {exc}") from exc
    if expected is None or git_real != expected:
        raise _StoreRefusal(
            "vault object store is not self-contained: "
            f"git dir {absolute_git_dir} is not {git_link}"
        )
    top = _rev_parse_lines(vault, "--show-toplevel")
    if len(top) != 1 or not top[0]:
        raise OSError(f"git rev-parse --show-toplevel returned {top!r}")
    try:
        top_real = Path(os.path.realpath(top[0]))
    except (OSError, ValueError) as exc:
        raise OSError(f"cannot resolve git toplevel: {exc}") from exc
    if top_real != vault_real:
        raise _StoreRefusal(
            "vault object store is not self-contained: "
            f"git toplevel {top[0]} is not {vault}"
        )
    return git_real


def _contained_git_dir(vault: Path) -> Path:
    """Git dir of a self-contained vault, or a refusal if bytes could come from elsewhere."""
    git_link = vault / ".git"
    if git_link.is_symlink() or (_points_at_a_path(git_link) and not git_link.is_dir()):
        raise _StoreRefusal(f"vault object store is not self-contained: {git_link} is not a directory")
    if git_link.is_dir():
        layout = _layout_problem(vault, git_link)
        if layout:
            raise _StoreRefusal(layout)
    git_dir = _require_git_identity(vault)
    if not git_link.is_dir():
        layout = _layout_problem(vault, git_dir)
        if layout:
            raise _StoreRefusal(layout)
    redirected = _store_redirect_problem(vault)
    if redirected:
        raise _StoreRefusal(redirected)
    # Git commands above run after the first walk. An alternates file or a
    # rewritten multi-pack-index written in that window is refused here.
    layout = _layout_problem(vault, git_dir)
    if layout:
        raise _StoreRefusal(layout)
    return git_dir


def _rev_parse_store(vault: Path) -> tuple[str, str]:
    """Return absolute common-dir and objects paths from the vault itself."""
    lines = _rev_parse_lines(
        vault,
        "--path-format=absolute",
        "--git-common-dir",
        "--git-path",
        "objects",
    )
    if len(lines) != 2:
        raise OSError(f"git rev-parse returned {len(lines)} paths")
    return lines[0], lines[1]


def _unsafe_text(text: str) -> bool:
    """True when text holds a control character, a Unicode line break, or a lone surrogate.

    U+2028 and U+2029 are line breaks (Zl/Zp), not Cc, and still split a FAIL line.
    """
    return any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in text)


def _printable(text: object) -> str:
    """Escape lone surrogates, NULs, and line breaks so one FAIL line cannot look like PASS."""
    raw = str(text).encode("utf-8", "backslashreplace").decode("utf-8")
    parts: list[str] = []
    for char in raw:
        if unicodedata.category(char) in _UNSAFE_CATEGORIES:
            parts.append(char.encode("unicode_escape").decode("ascii"))
        else:
            parts.append(char)
    return "".join(parts)


def errors(pin: object) -> list[str]:
    """Return every reason the pin is malformed; empty means the shape is valid."""
    if not isinstance(pin, dict):
        return ["pin must be a JSON object"]
    out = []
    missing = sorted(KEYS - pin.keys())
    extra = sorted(pin.keys() - KEYS)
    if missing:
        out.append(f"missing keys: {', '.join(missing)}")
    if extra:
        out.append(f"unknown keys: {', '.join(extra)}")
    if pin.get("format") != FORMAT:
        out.append(f"format must be {FORMAT!r}")
    if pin.get("repository") != REPOSITORY:
        out.append(f"repository must be {REPOSITORY!r}")
    limitations = pin.get("limitations")
    if not isinstance(limitations, str) or not limitations.strip():
        out.append("limitations must be a non-empty string")

    status = pin.get("status")
    if status == "awaiting":
        for key in PINNED_FIELDS:
            if pin.get(key) is not None:
                out.append(f"{key} must be null while status is awaiting")
    elif status == "pinned":
        path = pin.get("path")
        if not isinstance(path, str) or not path:
            out.append("path must be a non-empty string")
        else:
            parts = path.split("/")
            if path.startswith("/") or "\\" in path or {"", ".", ".."} & set(parts):
                out.append("path must be a relative posix path inside the repository")
            if _unsafe_text(path):
                out.append("path must not contain control characters, line breaks or lone surrogates")
        if not isinstance(pin.get("commit"), str) or not COMMIT.fullmatch(pin["commit"]):
            out.append("commit must be a full 40-hex lowercase SHA")
        if not isinstance(pin.get("sha256"), str) or not SHA256.fullmatch(pin["sha256"]):
            out.append("sha256 must be 64 lowercase hex characters")
        claim = pin.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            out.append("claim must be a non-empty string")
    else:
        out.append("status must be 'awaiting' or 'pinned'")
    return out


def _store_redirect_problem(vault: Path) -> str | None:
    """Ask git where the common dir and objects are, and refuse anything outside vault.

    The vault's own ceiling is set, so this cannot discover a parent repository.
    Bare and non-bare layouts both answer.
    """
    common, objects = _rev_parse_store(vault)
    outside = []
    if not _inside_vault(vault, common):
        outside.append(f"git common dir {common}")
    if not _inside_vault(vault, objects):
        outside.append(f"objects dir {objects}")
    if not outside:
        return None
    return "vault object store is not self-contained: " + ", ".join(outside) + f" is outside {vault}"


def vault_errors(pin: dict, vault: Path) -> list[str]:
    """Compare the pinned blob in a local csl-research clone with the pinned SHA-256.

    The vault must be the repository git reads: non-bare with git dir vault/.git,
    or bare with git dir the vault itself. Discovery stops at the vault's parent,
    and the object store must not redirect outside that git dir.
    """
    if pin.get("status") != "pinned":
        return []
    spec = f"{pin['commit']}:{pin['path']}"
    try:
        git_dir = _contained_git_dir(vault)
        objects = git_dir / "objects"
        before = _objects_snapshot(objects)
        blob = _blob_at(objects, str(pin["commit"]), str(pin["path"]))
        changed = _layout_problem(vault, git_dir)
        if changed:
            raise _StoreRefusal(changed)
        if _objects_snapshot(objects) != before:
            raise _StoreRefusal(
                f"vault object store is not self-contained: {objects} changed during the read"
            )
    except _ObjectMissing as missing:
        return [_printable(f"cannot read {spec} from {vault}: {missing}")]
    except _StoreRefusal as refusal:
        return [_printable(refusal.message)]
    except subprocess.TimeoutExpired:
        return [_printable(
            f"cannot read {spec!a} from {str(vault)!a}: git timed out after {GIT_TIMEOUT_SECONDS} seconds"
        )]
    except ValueError as exc:
        # A NUL byte or a lone surrogate in the path or the vault cannot be passed to git.
        return [_printable(f"cannot read {spec!a} from {str(vault)!a}: {_printable(exc)}")]
    except (OSError, subprocess.CalledProcessError) as exc:
        # CalledProcessError always has stderr, and it is None unless output was captured.
        # Windows hands a lone surrogate to git instead of raising ValueError, so escape here too.
        stderr = getattr(exc, "stderr", None)
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", "replace").strip()
        elif isinstance(stderr, str):
            detail = stderr.strip()
        else:
            detail = exc
        return [_printable(f"cannot read {spec} from {vault}: {detail}")]
    digest = hashlib.sha256(blob).hexdigest()
    if digest != pin["sha256"]:
        return [_printable(f"sha256 mismatch for {spec}: pinned {pin['sha256']}, vault has {digest}")]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", type=Path, default=PIN)
    parser.add_argument("--vault", type=Path, help="local csl-research clone to verify bytes")
    args = parser.parse_args(argv)

    try:
        pin = json.loads(args.pin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(_printable(f"FAIL: cannot read {args.pin}: {exc}"))
        return 1
    problems = errors(pin)
    if not problems and args.vault is not None:
        problems = vault_errors(pin, args.vault)
    if problems:
        for problem in problems:
            print(_printable(f"FAIL: {problem}"))
        return 1
    print(f"PASS: product pin is {pin['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
