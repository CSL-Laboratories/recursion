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


def _unsafe_text(text: str) -> bool:
    """True when text holds a control character (NUL included) or a lone surrogate."""
    return any(unicodedata.category(char) in ("Cc", "Cs") for char in text)


def _printable(text: object) -> str:
    """One diagnostic line: lone surrogates, NULs and other controls are escaped.

    A key or a path must not be able to forge a second line or a raw terminal control.
    """
    raw = str(text).encode("utf-8", "backslashreplace").decode("utf-8")
    parts: list[str] = []
    for char in raw:
        if char == "\0":
            parts.append("\\x00")
        elif char != " " and unicodedata.category(char)[:1] in {"C", "Z"}:
            parts.append(f"\\u{ord(char):04x}")
        else:
            parts.append(char)
    return "".join(parts)


def _git_env() -> dict[str, str]:
    """Child git must not lazy-fetch a missing blob from a promisor remote."""
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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
                out.append("path must not contain control characters or lone surrogates")
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


def _object_label(kind: str) -> str:
    """A short object type. A hostile or huge type must not fill the diagnostic."""
    text = kind.strip() or "unknown object"
    return text[:40]


def vault_errors(pin: dict, vault: Path) -> list[str]:
    """Compare the pinned blob in a local csl-research clone with the pinned SHA-256.

    Callers that already ran errors() pass a well-shaped pin. A value that is not
    an object, or a pinned pin missing commit, path, or sha256, is refused here
    instead of raising.
    """
    if not isinstance(pin, dict):
        return ["pin must be a JSON object"]
    if pin.get("status") != "pinned":
        return []
    missing = [key for key in ("commit", "path", "sha256") if not isinstance(pin.get(key), str)]
    if missing:
        return [f"missing keys: {', '.join(missing)}"]
    spec = f"{pin['commit']}:{pin['path']}"
    try:
        # The commit field must name a commit. cat-file peels a tag, so a tag
        # object's id would otherwise look like a blob at commit:path.
        # cat-file, not show: show renders a tree listing, and that listing is not the
        # pinned blob. GIT_NO_LAZY_FETCH keeps a partial clone from calling the promisor.
        commit_kind = subprocess.run(
            ["git", "-c", "credential.helper=", "-C", str(vault), "cat-file", "-t", pin["commit"]],
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        ).stdout.decode("ascii", "replace").strip()
        if commit_kind != "commit":
            return [_printable(f"{pin['commit']} is a {_object_label(commit_kind)}, not a commit")]
        kind = subprocess.run(
            ["git", "-c", "credential.helper=", "-C", str(vault), "cat-file", "-t", spec],
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_git_env(),
        ).stdout.decode("ascii", "replace").strip()
        if kind != "blob":
            return [_printable(f"{spec} is a {_object_label(kind)}, not a pinned blob")]
        blob = subprocess.run(
            ["git", "-c", "credential.helper=", "-C", str(vault), "cat-file", "blob", spec],
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
        return [_printable(f"cannot read {spec!a} from {str(vault)!a}: {exc}")]
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip() if hasattr(exc, "stderr") else exc
        # Windows hands a lone surrogate to git instead of raising ValueError, so escape here too.
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
