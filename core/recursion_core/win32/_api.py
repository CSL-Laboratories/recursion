"""ctypes declarations for the Windows state directory and the daemon lock.

Imported only when ``os.name == 'nt'`` and a caller asks for the real backend.
Every open of an existing object uses ``FILE_FLAG_OPEN_REPARSE_POINT``, and a
reparse point is returned as such without a security write. The state
directory's DACL is the protected SDDL from ``paths.protected_sddl``. A lock
file is opened with share mode 0; ``ERROR_SHARING_VIOLATION`` is
``SharingViolation``.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

from recursion_core.win32.paths import Probe, SharingViolation, Win32Refused

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_INVALID = ctypes.c_void_p(-1).value

TOKEN_QUERY = 0x0008
TokenUser = 1
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_SHARING_VIOLATION = 32
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183

FILE_READ_ATTRIBUTES = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_TAG_INFO = 9

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1
_ATTR_SHARE = FILE_SHARE_ALL
_NOFOLLOW = FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


def _prototypes() -> None:
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.SetSecurityInfo.restype = wintypes.DWORD
    _advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]


_prototypes()


def _last_error(action: str, path: str | None = None) -> OSError:
    err = ctypes.get_last_error()
    if err == ERROR_SHARING_VIOLATION:
        return SharingViolation(err, action, path)
    return OSError(err, action, path)


def _sid_string(psid: object) -> str:
    raw = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(raw)):
        raise _last_error("ConvertSidToStringSidW")
    try:
        return ctypes.wstring_at(raw.value)
    finally:
        _kernel32.LocalFree(raw)


def _sddl_to_sd(sddl: str) -> ctypes.c_void_p:
    sd = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(sd), None,
    ):
        raise _last_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    return sd


def _sd_to_sddl(sd: object) -> str:
    raw = ctypes.c_void_p()
    if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        sd, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, ctypes.byref(raw), None,
    ):
        raise _last_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
    try:
        return ctypes.wstring_at(raw.value)
    finally:
        _kernel32.LocalFree(raw)


def _release(handle: object) -> None:
    if handle and handle != _INVALID:
        _kernel32.CloseHandle(handle)


class Api:
    """Real Windows backend. One instance talks to kernel32 and advapi32."""

    def current_user_sid(self) -> str:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            raise _last_error("OpenProcessToken")
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
                raise _last_error("GetTokenInformation")
            buf = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(token, TokenUser, buf, needed.value, ctypes.byref(needed)):
                raise _last_error("GetTokenInformation")
            user = _TOKEN_USER.from_buffer(buf)
            return _sid_string(user.User.Sid)
        finally:
            _release(token)

    def local_appdata(self) -> str:
        value = os.environ.get("LOCALAPPDATA", "")
        if not value:
            raise Win32Refused("localappdata_missing")
        return value

    def stat(self, path: str) -> Probe | None:
        try:
            handle = self._open_existing(path, FILE_READ_ATTRIBUTES, _ATTR_SHARE)
        except FileNotFoundError:
            return None
        try:
            return self._attributes(handle)
        finally:
            _release(handle)

    def security(self, path: str) -> Probe:
        handle = self._open_existing(path, FILE_READ_ATTRIBUTES | READ_CONTROL, _ATTR_SHARE)
        try:
            attrs = self._attributes(handle)
            if attrs.is_reparse:
                return attrs
            owner, dacl = self._read_security(handle)
            return Probe(attrs.is_dir, False, owner, dacl)
        finally:
            _release(handle)

    def create_directory(self, path: str, sddl: str) -> None:
        sd = _sddl_to_sd(sddl)
        try:
            sa = _SECURITY_ATTRIBUTES()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = sd
            sa.bInheritHandle = False
            if _kernel32.CreateDirectoryW(path, ctypes.byref(sa)):
                return
            err = ctypes.get_last_error()
            if err == ERROR_ALREADY_EXISTS:
                raise FileExistsError(path)
            raise OSError(err, "CreateDirectoryW failed", path)
        finally:
            _kernel32.LocalFree(sd)

    def apply_security(self, path: str, sddl: str) -> None:
        handle = self._open_existing(
            path, FILE_READ_ATTRIBUTES | READ_CONTROL | WRITE_DAC | WRITE_OWNER, _ATTR_SHARE,
        )
        try:
            attrs = self._attributes(handle)
            if attrs.is_reparse:
                raise Win32Refused("reparse_point")
            self._write_security(handle, sddl)
        finally:
            _release(handle)

    def open_exclusive(self, path: str, sddl: str) -> object:
        sd = _sddl_to_sd(sddl)
        try:
            sa = _SECURITY_ATTRIBUTES()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = sd
            sa.bInheritHandle = False
            access = GENERIC_READ | GENERIC_WRITE | READ_CONTROL | WRITE_DAC | WRITE_OWNER
            handle = _kernel32.CreateFileW(
                path, access, 0, ctypes.byref(sa), OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, None,
            )
        finally:
            _kernel32.LocalFree(sd)
        if handle in (None, 0, _INVALID):
            err = ctypes.get_last_error()
            if err == ERROR_SHARING_VIOLATION:
                raise SharingViolation(err, "lock file is already open", path)
            raise OSError(err, "CreateFileW failed", path)
        return handle

    def describe(self, handle: object) -> Probe:
        attrs = self._attributes(handle)
        if attrs.is_reparse:
            return attrs
        owner, dacl = self._read_security(handle)
        return Probe(attrs.is_dir, False, owner, dacl)

    def apply_handle(self, handle: object, sddl: str) -> None:
        attrs = self._attributes(handle)
        if attrs.is_reparse:
            raise Win32Refused("reparse_point")
        self._write_security(handle, sddl)

    def release_handle(self, handle: object) -> None:
        _release(handle)

    def _open_existing(self, path: str, access: int, share: int) -> object:
        handle = _kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING, _NOFOLLOW, None)
        if handle in (None, 0, _INVALID):
            err = ctypes.get_last_error()
            if err in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                raise FileNotFoundError(err, path)
            if err == ERROR_SHARING_VIOLATION:
                raise SharingViolation(err, "open failed", path)
            raise OSError(err, "CreateFileW failed", path)
        return handle

    def _attributes(self, handle: object) -> Probe:
        info = _FILE_ATTRIBUTE_TAG_INFO()
        ok = _kernel32.GetFileInformationByHandleEx(
            handle, FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            raise _last_error("GetFileInformationByHandleEx")
        return Probe(
            bool(info.FileAttributes & FILE_ATTRIBUTE_DIRECTORY),
            bool(info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT),
            None,
            None,
        )

    def _read_security(self, handle: object) -> tuple[str, str]:
        owner = ctypes.c_void_p()
        sd = ctypes.c_void_p()
        err = _advapi32.GetSecurityInfo(
            handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, None, None, ctypes.byref(sd),
        )
        if err:
            raise OSError(err, "GetSecurityInfo failed")
        try:
            return _sid_string(owner), _sd_to_sddl(sd)
        finally:
            _kernel32.LocalFree(sd)

    def _write_security(self, handle: object, sddl: str) -> None:
        sd = _sddl_to_sd(sddl)
        try:
            owner = ctypes.c_void_p()
            defaulted = wintypes.BOOL()
            if not _advapi32.GetSecurityDescriptorOwner(sd, ctypes.byref(owner), ctypes.byref(defaulted)):
                raise _last_error("GetSecurityDescriptorOwner")
            present = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            dacl_defaulted = wintypes.BOOL()
            if not _advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(dacl_defaulted),
            ):
                raise _last_error("GetSecurityDescriptorDacl")
            if not present:
                raise Win32Refused("dacl_refused")
            err = _advapi32.SetSecurityInfo(
                handle, SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                owner, None, dacl, None,
            )
            if err:
                raise OSError(err, "SetSecurityInfo failed")
        finally:
            _kernel32.LocalFree(sd)
