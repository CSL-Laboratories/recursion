"""Whether this host is one a route adapter may launch on (R3-7, decision sheet D9).

The adapter modules never import ``os`` (``test_launch_guards`` holds them to that, so that no adapter
has a spawn path of its own). This module is the one place in ``routes`` that reads ``os``, and it reads
two things only: ``os.name`` and whether ``os.geteuid`` exists. It starts nothing.
"""

from __future__ import annotations

import os

# The refusal's detail word: the one child_env and subscription_routes give on such a host.
PLATFORM_UNSUPPORTED = "platform_unsupported"


def platform_supported() -> bool:
    """Not Windows, and a build with ``os.geteuid``: the same test as child_env and subscription_routes.

    Read at each call, never at import, so a test or a simulation that changes ``os.name`` is seen.
    """

    return os.name != "nt" and hasattr(os, "geteuid")
