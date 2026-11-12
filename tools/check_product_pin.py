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
import subprocess
import sys
import unicodedata
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
    return _walk_objects(objects)


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
        blob = _run_git(vault, ["--git-dir", str(git_dir), "show", spec]).stdout
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
