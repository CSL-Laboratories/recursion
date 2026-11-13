"""File identity helpers for R2a: native-magic check, full SHA-256, wrapper shapes (CSL-732, candidate)."""

from __future__ import annotations

import hashlib
import os
import re

# Wrapper and script extensions refused for a grant path and for argv[0].
WRAPPER_EXT = frozenset(
    {".cmd", ".bat", ".ps1", ".psm1", ".js", ".mjs", ".cjs", ".sh", ".bash", ".zsh", ".py", ".pyw",
     ".rb", ".pl", ".vbs", ".wsf", ".lnk", ".ts"}
)
# Interpreters and launchers refused as argv[0]: the engine must be exec'd directly.
INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh", "node", "nodejs",
     "python", "python3", "py", "env", "npx", "npm", "pnpm", "yarn", "uv", "uvx", "bun", "bunx",
     "deno", "wsl", "busybox", "perl", "ruby", "wscript", "cscript", "conhost", "start", "runas",
     "rundll32", "mshta", "sudo", "doas", "su", "nohup", "setsid", "timeout", "xargs", "exec",
     "script", "stdbuf", "nice", "ionice", "taskset", "chrt", "unshare", "nsenter", "firejail",
     "bwrap", "docker", "podman"}
)
# ELF, PE (MZ), Mach-O 32/64 both endians, Mach-O universal.
NATIVE_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe")
_PYTHON_VERSIONED = re.compile(r"python\d+(\.\d+)*[a-z]?$")
HEX64 = re.compile(r"[0-9a-f]{64}")
# Windows launcher extensions a pinned route may be granted through, bound to a pin (grant()).
PINNED_LAUNCHER_EXT = (".cmd", ".ps1")


def is_windows() -> bool:
    """The one Windows switch for detect and grant; tests patch it, never ``os.name``."""
    return os.name == "nt"


def stem(path: str) -> str:
    """Basename, with a trailing ``.exe`` dropped (case-insensitive)."""
    base = os.path.basename(path)
    root, ext = os.path.splitext(base)
    return root if ext.lower() == ".exe" else base


def is_wrapper_name(name: str) -> bool:
    base = stem(name).lower()
    ext = os.path.splitext(os.path.basename(name))[1].lower()
    return base in INTERPRETERS or bool(_PYTHON_VERSIONED.fullmatch(base)) or ext in WRAPPER_EXT


def file_kind(path: str) -> str:
    """``native``, ``script`` (shebang text) or ``other``. Raises OSError if unreadable."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head.startswith(b"#!"):
        return "script"
    if any(head.startswith(m) for m in NATIVE_MAGIC):
        return "native"
    return "other"


def sha256_file(path: str) -> tuple[str, int]:
    """Full SHA-256 (64 hex) and byte count of the whole file; never a stat shortcut."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
