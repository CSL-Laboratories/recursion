"""``python -m recursion_core.control.tui``: the TUI against this user's running daemon (CSL-799).

The TUI package owns no transport (its import boundary allows only the control API), so its
entry point lives here, beside the CLI's: both hand ``tui.term.main`` / ``cli.run`` the same API
factory, the daemon's owner-only Unix socket (``$XDG_RUNTIME_DIR/recursion/control.sock``). The
TUI's ``list``, ``status``, ``stop`` and ``start`` are therefore the daemon's own ``run.*`` calls,
byte-identical to the CLI's (PRODUCT.md d84cdd26, CSLR-D-012, section 3.6: a client of the same
control API; anything the TUI can do, the CLI can do).

With the daemon down, or on a platform without the Unix transport, every call answers
``control_api_unavailable`` and the TUI shows it as ``UNAVAILABLE``; nothing raises. Closing the TUI
sends nothing and stops no run.
"""

from __future__ import annotations

import sys

from recursion_core.control.client import SocketAPI
from recursion_core.std_fds import occupy_standard_fds
from recursion_core.tui import term


def _attach_missing_streams() -> None:
    """Give Python a text stream for a descriptor that was not open at startup.

    The interpreter leaves ``sys.stdin`` (or stdout, stderr) as None in that case, and filling
    the descriptor does not rebuild the stream. A later ``for line in sys.stdin`` would raise.
    The stream does not own the descriptor: the helper's null device stays open for the process.
    """

    if sys.stdin is None:
        sys.stdin = open(0, "r", encoding="utf-8", errors="surrogateescape", closefd=False)
    if sys.stdout is None:
        sys.stdout = open(1, "w", encoding="utf-8", errors="surrogateescape", closefd=False)
    if sys.stderr is None:
        sys.stderr = open(2, "w", encoding="utf-8", errors="surrogateescape", closefd=False)


def main(argv: list[str] | None = None, **streams) -> int:
    """``streams`` are ``tui.term.main``'s own keyword arguments (tests pass stdin and stdout)."""

    occupy_standard_fds()  # before any socket: a hole at fd 0, 1 or 2 would be taken by the next open
    _attach_missing_streams()
    return term.main(sys.argv[1:] if argv is None else argv, SocketAPI, **streams)


if __name__ == "__main__":
    sys.exit(main())
