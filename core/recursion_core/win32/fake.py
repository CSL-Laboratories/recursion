"""Recording fake of the Windows backend ``_api.Api``.

The same methods, in memory: calls are appended to ``calls``, objects live in
``objects``, and an exclusive open is refused with ``SharingViolation`` while
another handle still holds that path. Nothing here loads a Windows DLL.
``hold_dacl`` records a security write and leaves the stored DACL as it was, so
a caller that must read the DACL back can be refused.
"""
from __future__ import annotations

from recursion_core.win32.paths import (
    Probe,
    SharingViolation,
    chain,
    normalize_windows_path,
    protected_sddl,
)


class _Object:
    def __init__(self, is_dir: bool, is_reparse: bool, owner: str | None, dacl: str | None) -> None:
        self.is_dir = is_dir
        self.is_reparse = is_reparse
        self.owner = owner
        self.dacl = dacl
        self.exclusive = False


class RecordingFake:
    def __init__(self, user_sid: str, local_appdata: str) -> None:
        self.user_sid = user_sid
        self._local_appdata = local_appdata
        self._root = normalize_windows_path(local_appdata)
        self._implicit = set(chain(self._root))
        self.calls: list[tuple] = []
        self.objects: dict[str, _Object] = {}
        self.hold_dacl = False
        self._handles: dict[int, str] = {}
        self._next = 1

    def plant(
        self,
        path: str,
        *,
        owner: str | None,
        dacl: str | None,
        is_dir: bool,
        is_reparse: bool = False,
    ) -> None:
        self.objects[path] = _Object(is_dir, is_reparse, owner, dacl)

    def current_user_sid(self) -> str:
        self.calls.append(("current_user_sid",))
        return self.user_sid

    def local_appdata(self) -> str:
        self.calls.append(("local_appdata",))
        return self._local_appdata

    def stat(self, path: str) -> Probe | None:
        self.calls.append(("stat", path))
        obj = self.objects.get(path)
        if obj is not None:
            return Probe(obj.is_dir, obj.is_reparse, obj.owner, obj.dacl)
        if path in self._implicit:
            return Probe(True, False, None, None)
        return None

    def security(self, path: str) -> Probe:
        self.calls.append(("security", path))
        obj = self.objects.get(path)
        if obj is None:
            if path in self._implicit:
                return Probe(True, False, self.user_sid, protected_sddl(self.user_sid))
            raise FileNotFoundError(path)
        if obj.is_reparse:
            return Probe(obj.is_dir, True, None, None)
        return Probe(obj.is_dir, False, obj.owner, obj.dacl)

    def create_directory(self, path: str, sddl: str) -> None:
        self.calls.append(("create_directory", path, sddl))
        if path in self.objects:
            raise FileExistsError(path)
        self.objects[path] = _Object(True, False, self.user_sid, sddl)

    def apply_security(self, path: str, sddl: str) -> None:
        self.calls.append(("apply_security", path, sddl))
        obj = self.objects[path]
        if obj.is_reparse or self.hold_dacl:
            return
        obj.owner = self.user_sid
        obj.dacl = sddl

    def open_exclusive(self, path: str, sddl: str) -> int:
        self.calls.append(("open_exclusive", path, sddl))
        obj = self.objects.get(path)
        if obj is not None and obj.exclusive:
            raise SharingViolation(path)
        if obj is not None and obj.is_reparse:
            return self._new_handle(path)
        if obj is None:
            obj = _Object(False, False, self.user_sid, sddl)
            obj.exclusive = True
            self.objects[path] = obj
        else:
            obj.exclusive = True
        return self._new_handle(path)

    def describe(self, handle: int) -> Probe:
        self.calls.append(("describe", handle))
        obj = self.objects[self._handles[handle]]
        return Probe(obj.is_dir, obj.is_reparse, obj.owner, obj.dacl)

    def apply_handle(self, handle: int, sddl: str) -> None:
        self.calls.append(("apply_handle", handle, sddl))
        obj = self.objects[self._handles[handle]]
        if obj.is_reparse or self.hold_dacl:
            return
        obj.owner = self.user_sid
        obj.dacl = sddl

    def release_handle(self, handle: int) -> None:
        self.calls.append(("release_handle", handle))
        path = self._handles.pop(handle)
        if not any(held == path for held in self._handles.values()):
            self.objects[path].exclusive = False

    def _new_handle(self, path: str) -> int:
        handle = self._next
        self._next += 1
        self._handles[handle] = path
        return handle
