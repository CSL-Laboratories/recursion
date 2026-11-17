"""One daemon per user: an exclusive, non-shared handle on a lock file.

The file stays after the handle is released. The hold is the open handle with
share mode 0, not the presence of the file, so a second open fails
``already_running`` and a released handle can be taken again. A reparse point
is refused before the open. A file owned by another SID is released and refused
``owner_mismatch`` without a security write. An exception or a cancellation
after the open releases the handle.
"""
from __future__ import annotations

from recursion_core.win32.paths import (
    SharingViolation,
    Win32Refused,
    _join,
    _sid_key,
    ensure_state_dir,
    protected_sddl,
    user_only_dacl,
)

LOCK_NAME = "recursion-daemon.lock"


class UserLock:
    def __init__(self, api: object) -> None:
        self._api = api
        self._handle: object | None = None
        self.path: str | None = None

    def acquire(self) -> UserLock:
        if self._handle is not None:
            raise Win32Refused("already_running")
        state = ensure_state_dir(self._api)
        sid = self._api.current_user_sid()
        path = _join(state, LOCK_NAME)
        sddl = protected_sddl(sid)
        handle: object | None = None
        try:
            info = self._api.stat(path)
            if info is not None and info.is_reparse:
                raise Win32Refused("reparse_point")
            handle = self._api.open_exclusive(path, sddl)
            opened = self._api.describe(handle)
            if opened.is_reparse:
                raise Win32Refused("reparse_point")
            if _sid_key(opened.owner) != _sid_key(sid):
                raise Win32Refused("owner_mismatch")
            if not user_only_dacl(opened.dacl, sid):
                self._api.apply_handle(handle, sddl)
                opened = self._api.describe(handle)
                if opened.is_reparse:
                    raise Win32Refused("reparse_point")
                if _sid_key(opened.owner) != _sid_key(sid):
                    raise Win32Refused("owner_mismatch")
                if not user_only_dacl(opened.dacl, sid):
                    raise Win32Refused("dacl_refused")
            self._handle = handle
            self.path = path
            handle = None
            return self
        except SharingViolation:
            if handle is not None:
                self._api.release_handle(handle)
            raise Win32Refused("already_running")
        except BaseException:
            if handle is not None:
                self._api.release_handle(handle)
            raise

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._api.release_handle(handle)

    def __enter__(self) -> UserLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.release()
        return False
