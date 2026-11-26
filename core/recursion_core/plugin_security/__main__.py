"""``python -m recursion_core.plugin_security`` runs the local plugin commands."""

from __future__ import annotations

import sys

from ._entrypoints import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
