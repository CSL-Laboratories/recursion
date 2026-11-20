"""``python -P -m recursion_core.remote relay [--socket PATH] [--key-label NAME]``: the relay (``relay.main``).

This is how the owner's authorized_keys forced command starts the relay (README.md). It registers
nothing with the daemon; the ``recursiond relay`` spelling the client sends is what sshd hands over in
``SSH_ORIGINAL_COMMAND``.
"""

from __future__ import annotations

import sys

from .relay import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
