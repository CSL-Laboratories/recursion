"""Read one plugin file on Windows through checked handles: every fact from the handle that is then read.

Nothing in the product calls this yet: Windows still refuses every plugin read at the shared adapter
boundary (PRODUCT 616), and ``require_posix`` stays where it is. This is the reader a later slice will wire.

``read_plugin_file(root, name, limit)`` mirrors the daemon's ``_read_owned`` (missing, unreadable, untrusted,
too large, a bounded read) and ``_owned_directory`` for each folder on the way:

1. The name is judged first (``windows_read_checks``' name rule: no ``:``, ``/``, ``.``/``..``, trailing dot
   or space, device name, reserved or control character); a name that fails opens nothing.
2. The root is opened as the operator gave it (followed once, so a root reached through a junction is pinned
   by its target) and its own final path, in volume GUID form, becomes the pinned root.
3. Each folder step and then the file are opened as ``\\\\?\\`` + that pinned root + the code's own names
   (no Win32 name rewriting, and no drive letter that SUBST or a remap could move), with
   FILE_FLAG_OPEN_REPARSE_POINT (the last name is opened as itself, never followed), FILE_SHARE_READ only
   (while held, nobody writes, renames or deletes it; measured on this host for a folder, its parent and
   the file), READ_CONTROL, and SECURITY_SQOS_PRESENT | SECURITY_ANONYMOUS. Every step stays open until
   the file is read.
4. From each handle: FileAttributeTagInfo (attributes, reparse tag), FileStandardInfo (link count, size,
   delete pending: passed as link count 0), FileIdInfo (the volume, compared with the root's),
   FileRemoteProtocolInfo (a success means a remote file), GetFileType, the normalized GUID final path,
   and the owner and access list (``windows_owner_check``, default A3). A volume change, a remote file or a
   failed volume or remote query is passed as a failed final path. Every failed query is passed as ``None``.
5. ``windows_read_checks.judge_handle`` decides each handle (kind directory for the root and each step, kind
   file for the leaf). On a refusal every handle is closed unread and ``Refused(cause, name)`` is raised.
6. The file's size is checked before the read (end of file) and after it (bytes read); at most limit + 1
   bytes are read, by ReadFile on the same handle. Every handle is closed in ``finally``.

Not protected here: the open still walks through an ancestor junction before its final path shows it (the
compare then refuses, nothing is read); the per-name walk with the parent handle is the next slice.
Answers are existing causes only: plugin_file_missing, plugin_file_unreadable, plugin_file_untrusted,
plugin_file_too_large, plugin_directory_untrusted and platform_unsupported (no Windows host or no ctypes).
"""

from __future__ import annotations

import sys

from . import windows_read_checks as checks
from .errors import Refused
from .windows_owner_check import AccessEntry, owner_ok

__all__ = ["ERROR_FILE_NOT_FOUND", "ERROR_PATH_NOT_FOUND", "read_plugin_file"]

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
_MISSING = (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND)
_ERROR_NAMES = {5: "ERROR_ACCESS_DENIED", 32: "ERROR_SHARING_VIOLATION", 87: "ERROR_INVALID_PARAMETER",
                123: "ERROR_INVALID_NAME", 1920: "ERROR_CANT_ACCESS_FILE"}
_SEPARATOR = "\\"

PLUGIN_FILE_MISSING = "plugin_file_missing"
PLUGIN_FILE_TOO_LARGE = "plugin_file_too_large"


def read_plugin_file(root: str, name: str, limit: int, *, missing_ok: bool = False, native=None) -> bytes | None:
    """The bytes of ``root``\\``name`` (at most ``limit``), or ``None`` for a missing file with ``missing_ok``.

    ``name`` is the code's own relative name below ``root``, parts joined by a backslash. ``native`` is the
    OS layer; tests pass a fake one. Raises ``Refused`` with an existing cause on every other outcome."""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative int")
    if native is None:
        native = _load_native()
    if type(root) is not str or not root:
        raise Refused(checks.PLUGIN_DIRECTORY_UNTRUSTED, "root")
    if type(name) is not str or not checks._plain_name(name, checks.KIND_FILE):
        raise Refused(checks.PLUGIN_FILE_UNREADABLE, "name")
    parts = name.split(_SEPARATOR)
    held = []
    try:
        handle, error = native.open(root, True, True)
        if handle is None:
            raise Refused(checks.PLUGIN_DIRECTORY_UNTRUSTED, f"root: {_error_name(error)}")
        held.append(handle)
        process_sid = native.process_sid()
        root_final = native.final_path(handle)
        root_volume = native.volume(handle)
        if type(root_final) is not str or not root_final or root_volume is None:
            raise Refused(checks.PLUGIN_DIRECTORY_UNTRUSTED, "root")
        facts, _ = _facts(native, handle, root_final, root_volume, "", process_sid, True)
        cause = checks.judge_handle(facts, checks.KIND_DIRECTORY)
        if cause is not None:
            raise Refused(cause, "root")
        pinned = root_final.rstrip(_SEPARATOR)
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            relative = _SEPARATOR.join(parts[:index + 1])
            handle, error = native.open(pinned + _SEPARATOR + relative, not leaf, False)
            if handle is None:
                if not leaf:
                    raise Refused(checks.PLUGIN_DIRECTORY_UNTRUSTED, f"{part}: {_error_name(error)}")
                if error in _MISSING:
                    if missing_ok:
                        return None
                    raise Refused(PLUGIN_FILE_MISSING, part)
                raise Refused(checks.PLUGIN_FILE_UNREADABLE, f"{part}: {_error_name(error)}")
            held.append(handle)
            facts, size = _facts(native, handle, root_final, root_volume, relative, process_sid, not leaf)
            cause = checks.judge_handle(facts, checks.KIND_FILE if leaf else checks.KIND_DIRECTORY)
            if cause is not None:
                raise Refused(cause, part)
        if size is None:
            raise Refused(checks.PLUGIN_FILE_UNREADABLE, parts[-1])
        if size > limit:
            raise Refused(PLUGIN_FILE_TOO_LARGE, parts[-1])
        data = _read(native, held[-1], limit + 1, parts[-1])
        if len(data) > limit:
            raise Refused(PLUGIN_FILE_TOO_LARGE, parts[-1])
        return data
    finally:
        for handle in reversed(held):
            native.close(handle)


def _error_name(error) -> str:
    return _ERROR_NAMES.get(error, f"WinError {error}")


def _read(native, handle, wanted: int, part: str) -> bytes:
    chunks, total = [], 0
    while total < wanted:
        chunk = native.read(handle, wanted - total)
        if chunk is None:
            raise Refused(checks.PLUGIN_FILE_UNREADABLE, f"{part}: read")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _facts(native, handle, root_final, root_volume, relative, process_sid, directory):
    """``WindowsHandleFacts`` for one open handle, and its end-of-file size (``None`` if unknown)."""
    attribute_tag = native.attribute_tag(handle)
    attributes, reparse_tag = attribute_tag if attribute_tag is not None else (None, None)
    standard = native.standard(handle)
    links, size = None, None
    if standard is not None:
        links, size, delete_pending = standard
        if delete_pending is None:
            links = None
        elif delete_pending:
            links = 0
    final_path = native.final_path(handle)
    volume = native.volume(handle)
    remote = native.remote(handle)
    if volume is None or volume != root_volume or remote is not False:
        final_path = None
    security = native.security(handle)
    if security is None:
        ok = None
    else:
        owner_sid, entries = security
        ok = owner_ok(owner_sid, process_sid, entries, directory=directory)
    facts = checks.WindowsHandleFacts(
        attributes=attributes, reparse_tag=reparse_tag, link_count=links, file_type=native.file_type(handle),
        final_path=final_path, root_final_path=root_final, expected_name=relative, owner_ok=ok)
    return facts, size


def _load_native():
    """The real OS layer, or ``platform_unsupported`` where there is none."""
    if not hasattr(sys, "getwindowsversion"):
        raise Refused(checks.PLATFORM_UNSUPPORTED, "not a Windows host")
    try:
        return _Native()
    except (ImportError, OSError, AttributeError) as exc:
        raise Refused(checks.PLATFORM_UNSUPPORTED, type(exc).__name__) from None


class _Native:
    """kernel32 and advapi32 through ctypes. Each query answers ``None`` when it fails, never 0."""

    _READ_DATA, _READ_ATTRIBUTES, _READ_CONTROL, _SYNCHRONIZE = 0x1, 0x80, 0x20000, 0x100000
    _SHARE_READ, _OPEN_EXISTING = 0x1, 3
    _BACKUP_SEMANTICS, _OPEN_REPARSE_POINT = 0x02000000, 0x00200000
    _SQOS_PRESENT, _SQOS_ANONYMOUS = 0x00100000, 0x0
    _ATTRIBUTE_TAG_INFO, _STANDARD_INFO, _REMOTE_PROTOCOL_INFO, _ID_INFO = 9, 1, 13, 18
    _NORMALIZED_GUID = 0x0 | 0x1  # FILE_NAME_NORMALIZED | VOLUME_NAME_GUID
    _OWNER_DACL = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    _SE_FILE_OBJECT, _TOKEN_QUERY, _TOKEN_USER = 1, 0x8, 1

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        handle, dword, pointer = wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, dword, dword, pointer, dword, dword, handle]
        k32.CreateFileW.restype = handle
        k32.CloseHandle.argtypes = [handle]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.GetFileInformationByHandleEx.argtypes = [handle, ctypes.c_int, pointer, dword]
        k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k32.GetFinalPathNameByHandleW.argtypes = [handle, ctypes.c_wchar_p, dword, dword]
        k32.GetFinalPathNameByHandleW.restype = dword
        k32.GetFileType.argtypes = [handle]
        k32.GetFileType.restype = dword
        k32.ReadFile.argtypes = [handle, pointer, dword, ctypes.POINTER(dword), pointer]
        k32.ReadFile.restype = wintypes.BOOL
        k32.LocalFree.argtypes = [pointer]
        k32.LocalFree.restype = pointer
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = handle
        adv.GetSecurityInfo.argtypes = [handle, ctypes.c_int, dword] + [ctypes.POINTER(pointer)] * 5
        adv.GetSecurityInfo.restype = dword
        adv.GetAce.argtypes = [pointer, dword, ctypes.POINTER(pointer)]
        adv.GetAce.restype = wintypes.BOOL
        adv.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(ctypes.c_wchar_p)]
        adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        adv.OpenProcessToken.argtypes = [handle, dword, ctypes.POINTER(handle)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [handle, ctypes.c_int, pointer, dword, ctypes.POINTER(dword)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        self.k32, self.adv = k32, adv
        self.invalid = ctypes.c_void_p(-1).value

        class AttributeTag(ctypes.Structure):
            _fields_ = [("attributes", ctypes.c_uint32), ("tag", ctypes.c_uint32)]

        class Standard(ctypes.Structure):
            _fields_ = [("allocation", ctypes.c_int64), ("end_of_file", ctypes.c_int64),
                        ("links", ctypes.c_uint32), ("delete_pending", ctypes.c_ubyte),
                        ("directory", ctypes.c_ubyte)]

        class FileId(ctypes.Structure):
            _fields_ = [("volume", ctypes.c_uint64), ("file_id", ctypes.c_ubyte * 16)]

        class AclHeader(ctypes.Structure):
            _fields_ = [("revision", ctypes.c_ubyte), ("sbz1", ctypes.c_ubyte), ("size", ctypes.c_uint16),
                        ("count", ctypes.c_uint16), ("sbz2", ctypes.c_uint16)]

        class AceHeader(ctypes.Structure):
            _fields_ = [("ace_type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", ctypes.c_uint16)]

        self.AttributeTag, self.Standard, self.FileId = AttributeTag, Standard, FileId
        self.AclHeader, self.AceHeader = AclHeader, AceHeader

    def open(self, name, directory, follow):
        flags = self._SQOS_PRESENT | self._SQOS_ANONYMOUS
        if directory:
            flags |= self._BACKUP_SEMANTICS
        if not follow:
            flags |= self._OPEN_REPARSE_POINT
        access = self._READ_DATA | self._READ_ATTRIBUTES | self._READ_CONTROL | self._SYNCHRONIZE
        handle = self.k32.CreateFileW(name, access, self._SHARE_READ, None, self._OPEN_EXISTING, flags, None)
        if handle is None or handle == self.invalid:
            return None, self.ctypes.get_last_error()
        return handle, 0

    def close(self, handle):
        self.k32.CloseHandle(handle)

    def _info(self, handle, kind, structure):
        value = structure()
        if not self.k32.GetFileInformationByHandleEx(handle, kind, self.ctypes.byref(value),
                                                     self.ctypes.sizeof(value)):
            return None
        return value

    def attribute_tag(self, handle):
        value = self._info(handle, self._ATTRIBUTE_TAG_INFO, self.AttributeTag)
        return None if value is None else (value.attributes, value.tag)

    def standard(self, handle):
        value = self._info(handle, self._STANDARD_INFO, self.Standard)
        return None if value is None else (value.links, value.end_of_file, bool(value.delete_pending))

    def volume(self, handle):
        value = self._info(handle, self._ID_INFO, self.FileId)
        return None if value is None else value.volume

    def remote(self, handle):
        """True when FileRemoteProtocolInfo answers (a remote file); a local NTFS file answers 87."""
        buffer = self.ctypes.create_string_buffer(116)
        if self.k32.GetFileInformationByHandleEx(handle, self._REMOTE_PROTOCOL_INFO, buffer, len(buffer)):
            return True
        return False if self.ctypes.get_last_error() == 87 else None

    def file_type(self, handle):
        self.ctypes.set_last_error(0)
        value = self.k32.GetFileType(handle)
        return None if value == 0 and self.ctypes.get_last_error() else value

    def final_path(self, handle):
        size = self.k32.GetFinalPathNameByHandleW(handle, None, 0, self._NORMALIZED_GUID)
        if not size:
            return None
        buffer = self.ctypes.create_unicode_buffer(size + 1)
        written = self.k32.GetFinalPathNameByHandleW(handle, buffer, size + 1, self._NORMALIZED_GUID)
        if not written or written > size:
            return None
        return buffer.value

    def _sid(self, sid):
        text = self.ctypes.c_wchar_p()
        if not self.adv.ConvertSidToStringSidW(sid, self.ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            self.k32.LocalFree(text)

    def security(self, handle):
        ctypes = self.ctypes
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if self.adv.GetSecurityInfo(handle, self._SE_FILE_OBJECT, self._OWNER_DACL, ctypes.byref(owner), None,
                                    ctypes.byref(dacl), None, ctypes.byref(descriptor)):
            return None
        try:
            owner_sid = self._sid(owner) if owner.value else None
            if not dacl.value:
                return owner_sid, None
            entries = []
            for index in range(self.AclHeader.from_address(dacl.value).count):
                ace = ctypes.c_void_p()
                if not self.adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    return None
                header = self.AceHeader.from_address(ace.value)
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                sid = self._sid(ace.value + 8) if header.ace_type in (0, 1) else None
                entries.append(AccessEntry(header.ace_type, header.flags, mask, sid))
            return owner_sid, tuple(entries)
        finally:
            self.k32.LocalFree(descriptor)

    def process_sid(self):
        ctypes = self.ctypes
        token = ctypes.c_void_p()
        if not self.adv.OpenProcessToken(self.k32.GetCurrentProcess(), self._TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = ctypes.c_uint32()
            self.adv.GetTokenInformation(token, self._TOKEN_USER, None, 0, ctypes.byref(needed))
            if not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.adv.GetTokenInformation(token, self._TOKEN_USER, buffer, needed, ctypes.byref(needed)):
                return None
            return self._sid(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.k32.CloseHandle(token)

    def read(self, handle, size):
        buffer = self.ctypes.create_string_buffer(size)
        got = self.ctypes.c_uint32()
        if not self.k32.ReadFile(handle, buffer, size, self.ctypes.byref(got), None):
            return None
        return buffer.raw[:got.value]
