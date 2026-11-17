"""Windows control transport (F40 follow-on).

The Linux door is an ``AF_UNIX`` socket with a uid check. Windows has no such
credential, so this door is a named pipe: ``\\\\.\\pipe\\recursion-<user SID>-control``,
created with ``FILE_FLAG_FIRST_PIPE_INSTANCE`` and ``PIPE_REJECT_REMOTE_CLIENTS``,
and a protected DACL whose only ACE is that user SID. Each accept reads the
client pid, impersonates, compares the token SID, and reverts on every path.
A different SID is shut unread. The client checks the server process token the
same way.

Without a pipe backend both classes stay the labelled stub and raise
``PlatformNotSupported``, so a factory call on win32 still refuses until the
turn-on passes a backend. Nothing here falls back to TCP. Linux is the tested
path: the decisions run against ``win32.pipe.RecordingPipe``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

from ..win32.paths import Win32Refused, protected_sddl, user_only_dacl
from ..win32.pipe import (
    FILE_FLAG_FIRST_PIPE_INSTANCE,
    PIPE_ACCESS_DUPLEX,
    PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_TYPE_BYTE,
    PIPE_WAIT,
    PipeClosed,
    PipeNameTaken,
    PipeStream,
    pipe_name,
)
from .errors import (
    FrameError,
    InsecureSocketPath,
    PeerRefused,
    PlatformNotSupported,
    RemoteBindRefused,
)
from .framing import MAX_FRAME_BYTES, read_frame, write_frame
from .handlers import Connection, Handler, Message
from .peercred import PeerCred, get_peer_cred
from .unix import (
    BUSY_FRAME,
    DEFAULT_SOCKET_NAME,
    IDLE_TIMEOUT_S,
    MAX_CONNECTIONS,
    NOT_ADMITTED_FRAME,
    RESERVED_TIMEOUT_S,
    _handler_reply,
    _pending_refusal,
    _seconds,
    _transport_refusal,
)

WINDOWS_STUB_MESSAGE = (
    "control transport on Windows is not implemented: stub pending the F40 follow-on "
    "(owner-only named pipe with client SID check); Linux is the tested path"
)

_ACCEPT_POLL_S = 0.1
_PIPE_MODE = PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT
_PIPE_FLAGS = FILE_FLAG_FIRST_PIPE_INSTANCE | PIPE_REJECT_REMOTE_CLIENTS


def _sid_key(value: str) -> str:
    return value.upper()


def _is_remote_pipe(address: object) -> bool:
    """True for ``\\\\host\\pipe\\...`` (not the local ``\\\\.\\pipe\\`` form)."""

    if isinstance(address, os.PathLike):
        address = os.fspath(address)
    if isinstance(address, (bytes, bytearray)):
        try:
            address = address.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(address, str):
        return False
    text = address.replace("/", "\\")
    folded = text.upper()
    if folded.startswith("\\\\?\\UNC\\"):
        return True
    if text.startswith("\\\\.\\") or text.startswith("\\\\?\\"):
        return False
    return text.startswith("\\\\")


def _send_nowait(stream: PipeStream, frame: bytes) -> None:
    """Best effort: a transport frame is small and the pipe buffer holds it."""

    try:
        stream.setblocking(False)
        stream.send(frame)
    except OSError:
        pass


class WindowsControlServer:
    """Named-pipe server for the control door (F40 follow-on).

    Without a pipe backend this remains the labelled stub and raises
    ``PlatformNotSupported``. With a backend it serves one user-only pipe.
    The keyword set matches ``UnixControlServer`` so ``create_server`` can pass
    it through unchanged. ``directory`` is not a filesystem path here: a remote
    pipe name is refused, and anything else is ignored. The pipe name comes
    from the backend's user SID.
    """

    def __init__(
        self,
        directory: object,
        handler: Handler,
        *,
        name: str = DEFAULT_SOCKET_NAME,
        peer_cred: Callable[..., PeerCred] = get_peer_cred,
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        admit: Callable[[PeerCred], bool] | None = None,
        priority: Callable[[Message], bool] | None = None,
        reserved_connections: int = 0,
        reserved_timeout_s: float = RESERVED_TIMEOUT_S,
        pipe: Any = None,
    ) -> None:
        # ``name`` and ``peer_cred`` are accepted so the Unix keyword set still
        # type-checks. The pipe name is the user SID, and the identity check is
        # the impersonated token, not ``peer_cred``.
        del name, peer_cred
        if _is_remote_pipe(directory):
            raise RemoteBindRefused(f"remote_bind_refused: {directory!r}")
        if pipe is None:
            raise PlatformNotSupported(WINDOWS_STUB_MESSAGE)
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        if reserved_connections < 0 or (reserved_connections and priority is None):
            raise ValueError("reserved_connections needs a priority test and cannot be negative")
        idle_timeout_s = _seconds("idle_timeout_s", idle_timeout_s)
        if reserved_connections:
            reserved_timeout_s = _seconds("reserved_timeout_s", reserved_timeout_s)
        self._pipe = pipe
        try:
            sid = pipe.current_user_sid()
            sddl = protected_sddl(sid)
        except (Win32Refused, OSError) as exc:
            raise InsecureSocketPath("pipe_dacl_unprotected") from exc
        if not isinstance(sid, str) or not user_only_dacl(sddl, sid):
            raise InsecureSocketPath("pipe_dacl_unprotected")
        self._sid = sid
        self._sddl = sddl
        self._pipe_name = pipe_name(sid)
        self._handler = handler
        self._slots = threading.BoundedSemaphore(max_connections)
        self._admit = admit
        self._priority = priority
        self._reserved = threading.BoundedSemaphore(reserved_connections) if reserved_connections else None
        self._reserved_timeout_s = reserved_timeout_s
        self._idle_timeout_s = idle_timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listener: int | None = None
        self._thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._streams: set[PipeStream] = set()
        self._next_conn_id = 0
        self.refused: list[str] = []
        self.accepted = 0

    @property
    def path(self) -> str:
        return self._pipe_name

    def start(self) -> None:
        with self._lock:
            if self._listener is not None or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("server already started")
            if self._stop.is_set():
                raise RuntimeError("server already closed")
            try:
                handle = self._pipe.create_named_pipe(
                    self._pipe_name,
                    access=PIPE_ACCESS_DUPLEX,
                    mode=_PIPE_MODE,
                    flags=_PIPE_FLAGS,
                    sddl=self._sddl,
                )
            except PipeNameTaken as exc:
                raise InsecureSocketPath("pipe_name_taken") from exc
            except OSError as exc:
                # Any other create failure means this process does not serve.
                if getattr(exc, "errno", None) == 5:
                    raise InsecureSocketPath("pipe_name_taken") from exc
                raise InsecureSocketPath("pipe_dacl_unprotected") from exc
            self._listener = handle
            self._thread = threading.Thread(
                target=self._accept_loop, name="windows-pipe-accept", daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                self._listener = None
                self._thread = None
                try:
                    self._pipe.close_handle(handle)
                except OSError:
                    pass
                raise

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            listener, self._listener = self._listener, None
            workers = [
                worker for worker in self._workers
                if worker is not threading.current_thread() and worker.ident is not None
            ]
            streams = list(self._streams)
        if listener is not None:
            try:
                self._pipe.close_handle(listener)
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.ident is not None:
            thread.join(timeout=5.0)
        for stream in streams:
            try:
                stream.shutdown(None)
            except OSError:
                pass
        for worker in workers:
            worker.join(timeout=self._idle_timeout_s + 1.0)

    def __enter__(self) -> WindowsControlServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _refuse(self, reason: str) -> None:
        with self._lock:
            self.refused.append(reason)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                handle = self._pipe.wait_for_client(listener, _ACCEPT_POLL_S)
            except PipeClosed:
                return
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                if self._stop.is_set():
                    return
                continue
            if handle is None:
                continue
            stream = PipeStream(self._pipe, handle)
            try:
                self._accept_one(stream)
            except (KeyboardInterrupt, SystemExit):
                stream.close()
                raise
            except Exception:
                self._refuse("accept_failed")
                stream.close()

    def _accept_one(self, stream: PipeStream) -> None:
        try:
            pid = self._pipe.client_process_id(stream.handle)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._refuse(f"peer credentials unreadable: {exc}")
            stream.close()
            return
        try:
            sid = self._impersonated_sid(stream.handle)
        except PeerRefused as exc:
            self._refuse(str(exc))
            stream.close()
            return
        cred = PeerCred(pid=pid, uid=None, gid=None, sid=sid)
        if self._admit is not None and not self._admitted(cred):
            self._refuse("not_admitted")
            _send_nowait(stream, NOT_ADMITTED_FRAME)
            stream.close()
            return
        slots = self._slots
        if not slots.acquire(blocking=False):
            slots = self._reserved
            if slots is None or not slots.acquire(blocking=False):
                self._refuse("max_connections")
                _send_nowait(stream, BUSY_FRAME)
                stream.close()
                return
        with self._lock:
            self._next_conn_id += 1
            connection = Connection(conn_id=self._next_conn_id, peer=cred)
        worker = threading.Thread(
            target=self._serve,
            args=(stream, connection, slots),
            name="windows-pipe-conn",
            daemon=True,
        )
        with self._lock:
            self.accepted += 1
            self._workers.add(worker)
            self._streams.add(stream)
        try:
            worker.start()
        except RuntimeError:
            self._refuse("worker_start_failed")
            with self._lock:
                self.accepted -= 1
                self._workers.discard(worker)
                self._streams.discard(stream)
            stream.close()
            slots.release()

    def _impersonated_sid(self, handle: int) -> str:
        started = False
        try:
            self._pipe.impersonate_named_pipe_client(handle)
            started = True
            sid = self._pipe.thread_user_sid()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise PeerRefused(f"peer credentials unreadable: {exc}") from exc
        finally:
            if started:
                try:
                    self._pipe.revert_to_self()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    raise PeerRefused(f"peer credentials unreadable: {exc}") from exc
        if not isinstance(sid, str) or _sid_key(sid) != _sid_key(self._sid):
            raise PeerRefused(f"peer sid {sid} is not {self._sid}")
        return sid

    def _admitted(self, cred: PeerCred) -> bool:
        try:
            return self._admit(cred) is True  # type: ignore[misc]
        except Exception:
            return False

    def _is_priority(self, request: Message) -> bool:
        try:
            return self._priority(request) is True  # type: ignore[misc]
        except Exception:
            return False

    def _serve(self, stream: PipeStream, connection: Connection, slots: threading.BoundedSemaphore) -> None:
        reserved = slots is not self._slots
        try:
            while not self._stop.is_set():
                deadline = time.monotonic() + (self._reserved_timeout_s if reserved else self._idle_timeout_s)
                try:
                    request = read_frame(stream, max_bytes=self._max_frame_bytes, deadline=deadline)
                except FrameError as exc:
                    _send_quietly(stream, {"ok": False, "error": {"code": "bad_frame", "detail": str(exc)}})
                    return
                except OSError:
                    return
                if request is None:
                    return
                if reserved and not self._is_priority(request):
                    self._refuse("max_connections")
                    _send_nowait(stream, BUSY_FRAME)
                    return
                try:
                    response = self._handler(request, connection)
                    if not isinstance(response, dict):
                        raise TypeError("handler returned a non-object")
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    response = {"ok": False, "error": {"code": "internal_error"}}
                response = _handler_reply(response)
                if "id" in request and "id" not in response:
                    response = {**response, "id": request["id"]}
                try:
                    stream.settimeout(self._idle_timeout_s)
                    write_frame(stream, response, max_bytes=self._max_frame_bytes)
                except FrameError:
                    _send_quietly(stream, {"ok": False, "error": {"code": "response_too_large"}})
                    return
                except OSError:
                    return
                if reserved:
                    return
        finally:
            slots.release()
            try:
                stream.close()
            except OSError:
                self._refuse("conn_close_failed")
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())
                    self._streams.discard(stream)


def _send_quietly(stream: PipeStream, message: Message) -> None:
    try:
        write_frame(stream, message)
    except (OSError, FrameError):
        pass


class WindowsControlClient:
    """Named-pipe client for the control door (F40 follow-on).

    Without a pipe backend this remains the labelled stub and raises
    ``PlatformNotSupported``. With a backend it opens ``path`` and refuses,
    before any request is written, unless the server process token SID is ours.
    A call that fails refuses too.
    """

    def __init__(
        self,
        path: object,
        *,
        timeout_s: float = 5.0,
        peer_cred: Callable[..., PeerCred] = get_peer_cred,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        pipe: Any = None,
    ) -> None:
        del peer_cred
        if _is_remote_pipe(path):
            raise RemoteBindRefused(f"remote_bind_refused: {path!r}")
        if pipe is None:
            raise PlatformNotSupported(WINDOWS_STUB_MESSAGE)
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if not isinstance(path, str):
            raise PeerRefused("server credentials unreadable: not a local pipe")
        self.path = path
        self._timeout_s = timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        try:
            handle = pipe.open_client(path)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise PeerRefused(f"server credentials unreadable: {exc}") from exc
        stream = PipeStream(pipe, handle)
        try:
            self._check_server(pipe, handle)
        except BaseException:
            stream.close()
            raise
        self._stream: PipeStream | None = stream

    def _check_server(self, pipe: Any, handle: int) -> None:
        try:
            pid = pipe.server_process_id(handle)
            sid = pipe.process_user_sid(pid)
            ours = pipe.current_user_sid()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise PeerRefused(f"server credentials unreadable: {exc}") from exc
        if not isinstance(sid, str) or not isinstance(ours, str) or _sid_key(sid) != _sid_key(ours):
            raise PeerRefused(f"server sid {sid} is not {ours}")

    def request(self, message: Message) -> Message:
        with self._lock:
            stream = self._stream
            if stream is None:
                raise ConnectionError("client closed")
            try:
                deadline = time.monotonic() + self._timeout_s
                stream.settimeout(self._timeout_s)
                try:
                    write_frame(stream, message, max_bytes=self._max_frame_bytes)
                except OSError:
                    refusal = _pending_refusal(stream)
                    if refusal is not None:
                        raise refusal from None
                    raise
                response = read_frame(stream, max_bytes=self._max_frame_bytes, deadline=deadline)
                if response is None:
                    raise ConnectionError("server closed the connection")
                refusal = _transport_refusal(response)
                if refusal is not None:
                    raise refusal
                if "id" in message and response.get("id") != message["id"]:
                    raise FrameError("reply_id_mismatch")
            except BaseException:
                self._stream = None
                stream.close()
                raise
        return response

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def __enter__(self) -> WindowsControlClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
