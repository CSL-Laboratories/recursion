"""Keep fds 0, 1 and 2 from being handed to a later socket or file.

A process started with one of those descriptors not open gives that number to the next
``open`` or ``socket``. A line then written to fd 2 by number goes into whatever took the
hole (on the Control MCP server, the daemon's control connection). Every process entry
calls ``occupy_standard_fds`` before it opens anything else. One helper, so each entry
uses this module and does not keep a copy.
"""

from __future__ import annotations

import os


def occupy_standard_fds() -> None:
    """Put the null device on each of fds 0, 1 and 2 that is not open.

    ``open`` takes the lowest free number, so one open per hole fills 0, then 1, then 2.
    A descriptor that is already open is left alone. The open does not set ``O_CLOEXEC``, so a
    later exec keeps the hole filled.
    """

    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            os.open(os.devnull, os.O_RDWR)
