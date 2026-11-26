"""The sizes of a plugin package the daemon accepts.

They live in this package so that the plugin host channel (``host_channel.py``) can derive the size of
its largest ``load`` frame from them without importing the daemon: ``daemon/plugins.py`` imports these
names from here and keeps them reachable under its own names. The values are the daemon's, unchanged.
"""

from __future__ import annotations

ARTIFACT = "plugin.py"                  # the one package file the daemon reads beside the manifest
MAX_MANIFEST_BYTES, MAX_ARTIFACT_BYTES = 64 * 1024, 8 * 1024 * 1024
