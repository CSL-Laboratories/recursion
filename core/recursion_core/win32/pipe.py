"""In-memory named-pipe backend for the Windows control door.

The real Win32 pipe calls are not on ``win32._api.Api`` or ``win32.fake.RecordingFake``
(those cover the state directory and the daemon lock). This module is the pipe
seam the door talks to. ``RecordingPipe`` records every call and moves bytes
between a server end and a client end. Nothing here loads a Windows DLL.

On a real Windows host the same method names are what a ctypes backend would
implement (``CreateNamedPipeW``, ``ConnectNamedPipe``, client and server
process ids, ``ImpersonateNamedPipeClient``, the thread token, ``RevertToSelf``,
``OpenProcess`` plus ``OpenProcessToken``). That backend is not in this file:
the door stays on the labelled stub until a caller passes a backend.
"""

from __future__ import annotations

import threading
import time

# Win32 constants the door asks for. Values are the SDK's, so a recording and
# a later ctypes backend compare the same bits.
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
ERROR_ACCESS_DENIED = 5

# One id for every pipe this fake creates. Tests override the token, not the id.
SERVER_PID = 4000


class PipeNameTaken(OSError):
    """``CreateNamedPipeW`` failed because the name is already held.

    Windows reports that collision as ``ERROR_ACCESS_DENIED`` when the first
    instance was created with ``FILE_FLAG_FIRST_PIPE_INSTANCE``.
    """

    def __init__(self, name: str) -> None:
        super().__init__(ERROR_ACCESS_DENIED, "CreateNamedPipeW", name)


class PipeClosed(Exception):
    """The listening instance was shut. ``wait_for_client`` uses this to return."""


def pipe_name(sid: str) -> str:
    """``\\\\.\\pipe\\recursion-<sid>-control``. The SID text is the only varying part."""

    return "\\\\.\\pipe\\recursion-" + sid + "-control"


class _Lane:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.cv = threading.Condition()
        self.closed = False


class _Link:
    def __init__(
        self,
        client_pid: int,
        client_sid: str,
        server_pid: int,
        fail_impersonate: bool,
        fail_token: bool,
    ) -> None:
        self.client_pid = client_pid
        self.client_sid = client_sid
        self.server_pid = server_pid
        self.fail_impersonate = fail_impersonate
        self.fail_token = fail_token
        self.to_server = _Lane()
        self.to_client = _Lane()


class _Listener:
    def __init__(self, name: str, server_pid: int) -> None:
        self.name = name
        self.server_pid = server_pid
        self.queue: list[int] = []
        self.cv = threading.Condition()
        self.closed = False


class RecordingPipe:
    """Recording stand-in for the pipe calls the substrate API does not have.

    ``calls`` grows by one tuple per Win32-shaped operation. ``client_sid``,
    ``client_pid``, ``fail_impersonate`` and ``fail_token`` describe the next
    ``open_client``. ``take_name`` makes the next create of that name raise
    ``PipeNameTaken``. ``set_process_sid`` / ``deny_process`` answer the
    client's check of the server process token.
    """

    def __init__(self, user_sid: str) -> None:
        self.user_sid = user_sid
        self.calls: list[tuple] = []
        self.client_sid: str | None = None
        self.client_pid: int | None = None
        self.fail_impersonate = False
        self.fail_token = False
        self._taken: set[str] = set()
        self._by_name: dict[str, _Listener] = {}
        self._process_sids: dict[int, str] = {}
        self._deny: set[int] = set()
        self._handles: dict[int, tuple] = {}
        self._next = 1
        self._next_client_pid = 5000
        self._lock = threading.Lock()
        self._local = threading.local()
        self.server_pid = SERVER_PID

    def snapshot(self) -> list[tuple]:
        with self._lock:
            return list(self.calls)

    def take_name(self, name: str) -> None:
        with self._lock:
            self._taken.add(name)

    def set_process_sid(self, pid: int, sid: str) -> None:
        with self._lock:
            self._process_sids[pid] = sid

    def deny_process(self, pid: int) -> None:
        with self._lock:
            self._deny.add(pid)

    def current_user_sid(self) -> str:
        self._record(("current_user_sid",))
        return self.user_sid

    def create_named_pipe(self, name: str, *, access: int, mode: int, flags: int, sddl: str) -> int:
        self._record(("CreateNamedPipeW", name, access, mode, flags, sddl))
        with self._lock:
            if name in self._taken or name in self._by_name:
                raise PipeNameTaken(name)
            listener = _Listener(name, self.server_pid)
            handle = self._alloc(("listener", listener))
            self._by_name[name] = listener
            return handle

    def wait_for_client(self, listener_handle: int, timeout: float) -> int | None:
        listener = self._listener(listener_handle)
        deadline = time.monotonic() + timeout
        with listener.cv:
            while not listener.queue and not listener.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                listener.cv.wait(remaining)
            if not listener.queue:
                raise PipeClosed()
            handle = listener.queue.pop(0)
        self._record(("ConnectNamedPipe", listener_handle, handle))
        return handle

    def open_client(self, name: str) -> int:
        with self._lock:
            self.calls.append(("CreateFileW", name))
            listener = self._by_name.get(name)
            if listener is None or listener.closed:
                raise FileNotFoundError(name)
            sid = self.user_sid if self.client_sid is None else self.client_sid
            if self.client_pid is None:
                pid = self._next_client_pid
                self._next_client_pid += 1
            else:
                pid = self.client_pid
            link = _Link(pid, sid, listener.server_pid, self.fail_impersonate, self.fail_token)
            client_handle = self._alloc(("conn", link, "client"))
            server_handle = self._alloc(("conn", link, "server"))
        with listener.cv:
            if listener.closed:
                raise FileNotFoundError(name)
            listener.queue.append(server_handle)
            listener.cv.notify()
        return client_handle

    def client_process_id(self, handle: int) -> int:
        self._record(("GetNamedPipeClientProcessId", handle))
        link, end = self._conn(handle)
        if end != "server":
            raise OSError("client process id is read on the server end")
        return link.client_pid

    def server_process_id(self, handle: int) -> int:
        self._record(("GetNamedPipeServerProcessId", handle))
        link, end = self._conn(handle)
        if end != "client":
            raise OSError("server process id is read on the client end")
        return link.server_pid

    def impersonate_named_pipe_client(self, handle: int) -> None:
        self._record(("ImpersonateNamedPipeClient", handle))
        link, end = self._conn(handle)
        if end != "server":
            raise OSError("impersonation is of a server end")
        if link.fail_impersonate:
            raise OSError(ERROR_ACCESS_DENIED, "ImpersonateNamedPipeClient")
        self._local.on = True
        self._local.handle = handle
        self._local.sid = link.client_sid

    def thread_user_sid(self) -> str:
        self._record(("OpenThreadToken",))
        if not getattr(self._local, "on", False):
            raise OSError(1008, "not impersonating")
        link, _end = self._conn(self._local.handle)
        if link.fail_token:
            raise OSError(ERROR_ACCESS_DENIED, "GetTokenInformation")
        return self._local.sid

    def revert_to_self(self) -> None:
        self._record(("RevertToSelf",))
        self._local.on = False
        self._local.handle = None
        self._local.sid = None

    def process_user_sid(self, pid: int) -> str:
        self._record(("OpenProcessToken", pid))
        with self._lock:
            if pid in self._deny:
                raise OSError(ERROR_ACCESS_DENIED, "OpenProcess")
            return self._process_sids.get(pid, self.user_sid)

    def read(self, handle: int, n: int, timeout: float | None) -> bytes:
        self._record(("ReadFile", handle, n))
        link, end = self._conn(handle)
        lane = link.to_server if end == "server" else link.to_client
        return _read_lane(lane, n, timeout)

    def write(self, handle: int, data: bytes, timeout: float | None) -> None:
        self._record(("WriteFile", handle, len(data)))
        link, end = self._conn(handle)
        lane = link.to_client if end == "server" else link.to_server
        _write_lane(lane, data)

    def close_handle(self, handle: int) -> None:
        with self._lock:
            self.calls.append(("CloseHandle", handle))
            info = self._handles.pop(handle, None)
        if info is None:
            return
        kind = info[0]
        if kind == "listener":
            listener = info[1]
            with listener.cv:
                listener.closed = True
                listener.cv.notify_all()
            with self._lock:
                self._by_name.pop(listener.name, None)
            return
        link = info[1]
        _close_lane(link.to_server)
        _close_lane(link.to_client)

    def _record(self, item: tuple) -> None:
        with self._lock:
            self.calls.append(item)

    def _alloc(self, info: tuple) -> int:
        handle = self._next
        self._next += 1
        self._handles[handle] = info
        return handle

    def _listener(self, handle: int) -> _Listener:
        with self._lock:
            info = self._handles.get(handle)
        if info is None or info[0] != "listener":
            raise OSError("not a pipe listener")
        return info[1]

    def _conn(self, handle: int) -> tuple[_Link, str]:
        with self._lock:
            info = self._handles.get(handle)
        if info is None or info[0] != "conn":
            raise OSError("not a connected pipe")
        return info[1], info[2]


def _read_lane(lane: _Lane, n: int, timeout: float | None) -> bytes:
    if n <= 0:
        return b""
    deadline = None if timeout is None else time.monotonic() + timeout
    with lane.cv:
        while not lane.buf and not lane.closed:
            if timeout == 0:
                raise TimeoutError("pipe read")
            if deadline is None:
                lane.cv.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pipe read")
            lane.cv.wait(remaining)
        if not lane.buf:
            return b""
        take = min(n, len(lane.buf))
        out = bytes(lane.buf[:take])
        del lane.buf[:take]
        return out


def _write_lane(lane: _Lane, data: bytes) -> None:
    with lane.cv:
        if lane.closed:
            raise BrokenPipeError("pipe closed")
        lane.buf.extend(data)
        lane.cv.notify_all()


def _close_lane(lane: _Lane) -> None:
    with lane.cv:
        lane.closed = True
        lane.cv.notify_all()


class PipeStream:
    """The ``recv`` / ``sendall`` / ``settimeout`` face ``framing`` already uses.

    A pipe handle is not a socket. This object is the socket-like stream the
    door passes to ``read_frame`` and ``write_frame``.
    """

    def __init__(self, backend: RecordingPipe, handle: int) -> None:
        self._backend = backend
        self.handle = handle
        self._timeout: float | None = None
        self._closed = False

    def recv(self, n: int) -> bytes:
        if self._closed:
            return b""
        return self._backend.read(self.handle, n, self._timeout)

    def send(self, data: bytes) -> int:
        self.sendall(data)
        return len(data)

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("pipe closed")
        self._backend.write(self.handle, data, self._timeout)

    def settimeout(self, seconds: float | None) -> None:
        self._timeout = seconds

    def setblocking(self, flag: bool) -> None:
        self._timeout = None if flag else 0.0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.close_handle(self.handle)

    def shutdown(self, _how: object = None) -> None:
        self.close()

    def detach(self) -> int:
        self._closed = True
        return -1
