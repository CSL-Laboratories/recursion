"""Windows state directory and the one-daemon-per-user lock.

``_api`` holds the ctypes declarations. It is imported only when ``os.name == 'nt'``
and a caller asks for the real backend. Importing this package does not load a
Windows DLL.
"""
from __future__ import annotations

from .lock import UserLock
from .paths import Win32Refused, ensure_state_dir

__all__ = ["UserLock", "Win32Refused", "ensure_state_dir"]
