"""``python -m recursion_core.control``: the control CLI against this user's running daemon.

The API factory is the daemon's owner-only Unix socket (``$XDG_RUNTIME_DIR/recursion/control.sock``).
With the daemon down, every command answers ``control_api_unavailable`` and exits 1.
"""

import sys

from recursion_core.control import cli
from recursion_core.control.client import SocketAPI


def main(argv: list[str] | None = None) -> int:
    return cli.run(sys.argv[1:] if argv is None else argv, SocketAPI)


if __name__ == "__main__":
    sys.exit(main())
