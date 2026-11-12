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
    """
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
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


def _foreign_store_problem(vault: Path) -> str | None:
    """Reason the vault's objects live outside that directory, if they do.

    A missing .git is not this case: git show reports that as a failed read.
    A gitdir file, a symlink, commondir, or an alternates file is.
    """
    git_dir = vault / ".git"
    if git_dir.is_symlink() or (_points_at_a_path(git_dir) and not git_dir.is_dir()):
        return f"vault object store is not self-contained: {git_dir} is not a directory"
    if not git_dir.is_dir():
        return None
    commondir = git_dir / "commondir"
    if _points_at_a_path(commondir):
        return f"vault object store is not self-contained: {commondir} exists"
    objects = git_dir / "objects"
    for name in ("alternates", "http-alternates"):
        alternate = objects / "info" / name
        if _points_at_a_path(alternate):
            return f"vault object store is not self-contained: {alternate} exists"
    if not _points_at_a_path(objects):
        return None
    try:
        resolved = objects.resolve()
        root = vault.resolve()
    except (OSError, ValueError) as exc:
        return f"vault object store is not self-contained: {objects} ({exc})"
    if not resolved.is_relative_to(root):
        return f"vault object store is not self-contained: {resolved} is outside {vault}"
    return None


def _rev_parse_store(vault: Path) -> tuple[str, str]:
    """Return absolute common-dir and objects paths from the vault itself."""
    done = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(vault),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "--git-path",
            "objects",
        ],
        check=True,
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env=_git_env(),
    )
    lines = done.stdout.decode("utf-8", "replace").splitlines()
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
    """Ask git where the common dir and objects are, and refuse anything outside vault."""
    if not (vault / ".git").is_dir() or (vault / ".git").is_symlink():
        return None
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
    """Compare the pinned blob in a local csl-research clone with the pinned SHA-256."""
    if pin.get("status") != "pinned":
        return []
    spec = f"{pin['commit']}:{pin['path']}"
    try:
        foreign = _foreign_store_problem(vault)
        if foreign:
            return [_printable(foreign)]
        redirected = _store_redirect_problem(vault)
        if redirected:
            return [_printable(redirected)]
        blob = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(vault), "show", spec],
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        ).stdout
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
