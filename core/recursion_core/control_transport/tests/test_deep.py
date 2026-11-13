"""Defects past the close-deadline and reply-correlation work (CSL-719).

Each case failed on 7cfe77d8 before the fix in this file's sibling modules:
permission swaps around bind, lock-fd leaks (failed socket() and fork), a
handler reply that cannot be encoded, a local encode error closing a healthy
client, and close() that cannot cancel. Unconnectable-socket cases and a
close() that skips shutdown after a failed unlink failed on 1d0649c7.
An ECONNREFUSED probe that unlinks a listener bound on the reused inode, and
a full-backlog EAGAIN left as BlockingIOError, failed on e212728e.
Unlink-by-path after a matching lstat, close() with no inode pin, and a
forked child that inherits that pin, failed on ef816f7c.
A connect that follows a final-component symlink or a directory round-trip
undone inside connect, and a reap of dead sockets the quarantine generator
cannot name, failed on a4c6d121.
A recheck that trusts a non-procfs ``/proc/self/fd`` and unlinks a live
listener, failed on 57bacd18.
A procfs fd entry that names a different socket, including another pid's
fd directory bind-mounted on ``/proc/<pid>/fd``, failed on 881a32e2.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import warnings

from recursion_core import control_transport as ct
from recursion_core.control_transport import unix
from recursion_core.control_transport.unix import UnixControlClient, UnixControlServer

LINUX = os.uname().sysname == "Linux" if hasattr(os, "uname") else False


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ctd-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self._rm)
        self.sock_dir = os.path.join(self.base, "run")

    def _rm(self) -> None:
        for root, dirs, files in os.walk(self.base, topdown=False):
            for name in files:
                path = os.path.join(root, name)
                try:
                    os.unlink(path)
                except OSError:
                    pass
            for name in dirs:
                path = os.path.join(root, name)
                try:
                    if os.path.islink(path):
                        os.unlink(path)
                    else:
                        os.chmod(path, 0o700)
                        os.rmdir(path)
                except OSError:
                    pass
        try:
            os.rmdir(self.base)
        except OSError:
            pass


def _fds_pointing_at(path: str) -> int:
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target == path:
            count += 1
    return count


def _reap(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    os.waitpid(pid, 0)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class EncodeAndCancelTests(_TmpCase):
    def test_unencodable_handler_reply_is_internal_error_and_the_worker_lives(self) -> None:
        crashes: list[BaseException] = []

        def hook(args: threading.ExceptHookArgs) -> None:
            if args.thread is not None and args.thread.name == "control-transport-conn" and args.exc_value is not None:
                crashes.append(args.exc_value)

        old = threading.excepthook
        threading.excepthook = hook
        self.addCleanup(lambda: setattr(threading, "excepthook", old))

        def handler(message: dict, conn: ct.Connection) -> dict:
            if message.get("op") == "nan":
                return {"ok": True, "n": float("nan")}
            return {"ok": True, "blob": b"\x00"}

        srv = UnixControlServer(self.sock_dir, handler)
        srv.start()
        self.addCleanup(srv.close)
        with UnixControlClient(srv.path) as client:
            reply = client.request({"op": "stop", "id": "s1"})
            self.assertEqual(reply["error"]["code"], "internal_error")
            self.assertNotIn("blob", reply)
            self.assertEqual(reply.get("id"), "s1")
            # Same connection: a handler error must not drop the stream.
            again = client.request({"op": "nan", "id": "s2"})
            self.assertEqual(again["error"]["code"], "internal_error")
            self.assertEqual(again.get("id"), "s2")
            self.assertNotIn("nan", repr(again).lower())
        self.assertEqual(crashes, [])
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["error"]["code"], "internal_error")

    def test_local_frame_error_does_not_close_the_client(self) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        with self.assertRaises(ct.FrameError) as caught:
            client.request({"op": "ping", "pad": "x" * (1 << 20)})
        self.assertEqual(str(caught.exception), "frame_too_large")
        self.assertEqual(client.request({"op": "ping", "id": "p1"}), {"ok": True, "op": "pong", "id": "p1"})

    def test_close_unblocks_an_in_flight_request(self) -> None:
        entered = threading.Event()

        def handler(message: dict, conn: ct.Connection) -> dict:
            entered.set()
            time.sleep(5)
            return {"ok": True, "op": "stop", "state": "stopped"}

        srv = UnixControlServer(self.sock_dir, handler, idle_timeout_s=30)
        srv.start()
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path, timeout_s=5)
        box: dict = {}

        def run() -> None:
            try:
                box["reply"] = client.request({"op": "stop", "id": "s1"})
            except BaseException as exc:  # noqa: BLE001 - the assertion is below
                box["err"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 1.0)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn("err", box)
        self.assertNotIn("reply", box)

    def test_one_client_serializes_concurrent_requests(self) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker(i: int) -> None:
            try:
                barrier.wait(2)
                for j in range(8):
                    reply = client.request({"op": "ping", "id": f"{i}-{j}"})
                    if reply.get("id") != f"{i}-{j}":
                        raise AssertionError(reply)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])


@unittest.skipUnless(LINUX, "Linux is the tested path")
class LockAndPermissionTests(_TmpCase):
    def test_socket_constructor_failure_releases_the_lock(self) -> None:
        orig = socket.socket
        calls = {"n": 0}

        def fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EMFILE, "Too many open files")
            return orig(*args, **kwargs)

        socket.socket = fail_first  # type: ignore[misc]
        try:
            srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
            with self.assertRaises(OSError) as caught:
                srv.start()
            self.assertEqual(caught.exception.errno, errno.EMFILE)
        finally:
            socket.socket = orig  # type: ignore[misc]
        lock_path = os.path.join(self.sock_dir, "control.sock.lock")
        self.assertEqual(_fds_pointing_at(lock_path), 0)
        srv2 = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv2.start()
        self.addCleanup(srv2.close)
        with UnixControlClient(srv2.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")

    def test_forked_child_does_not_keep_the_lock_after_close(self) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd))
        # The server's accept thread is already running; fork is the bug, not a surprise.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*multi-threaded.*", category=DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            # Past this point the after-fork hook has already closed the child's lock fd.
            os.write(write_fd, b"x")
            os.close(write_fd)
            time.sleep(60)
            os._exit(0)
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        got = b""
        deadline = time.monotonic() + 2.0
        while not got and time.monotonic() < deadline:
            try:
                got = os.read(read_fd, 1)
            except BlockingIOError:
                time.sleep(0.01)
        self.assertEqual(got, b"x")
        self.addCleanup(_reap, pid)
        # The child must not steal the live server's lock.
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        srv.close()
        srv2 = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv2.start()
        self.addCleanup(srv2.close)
        with UnixControlClient(srv2.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")

    def test_close_from_the_handler_releases_the_lock(self) -> None:
        holder: dict = {}
        errors: list[BaseException] = []

        def handler(message: dict, conn: ct.Connection) -> dict:
            try:
                holder["srv"].close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            return {"ok": True, "op": "stopped"}

        srv = UnixControlServer(self.sock_dir, handler)
        holder["srv"] = srv
        srv.start()
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path, timeout_s=2)
        self.addCleanup(client.close)
        try:
            client.request({"op": "stop"})
        except (ConnectionError, OSError, ct.FrameError):
            pass
        self.assertEqual(errors, [])
        srv2 = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv2.start()
        self.addCleanup(srv2.close)
        with UnixControlClient(srv2.path) as client2:
            self.assertEqual(client2.request({"op": "ping"})["op"], "pong")

    def test_lock_is_free_before_peer_shutdown(self) -> None:
        """``shutdown`` wakes the client, so the flock must already be gone.

        Checked inside ``socket.shutdown``, which is before ``close`` returns.
        On a build that drops the lock only after shutdown, this flock fails
        every time instead of about twice in twenty.
        """
        import fcntl

        lock_path = os.path.join(self.sock_dir, "control.sock.lock")
        seen: dict[str, bool] = {}
        orig_shutdown = socket.socket.shutdown

        def spy(sock: socket.socket, how: int) -> None:
            if "free" not in seen and os.path.exists(lock_path):
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        seen["free"] = False
                    else:
                        seen["free"] = True
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            orig_shutdown(sock, how)

        socket.socket.shutdown = spy  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "shutdown", orig_shutdown)

        def handler(message: dict, conn: ct.Connection) -> dict:
            holder["srv"].close()
            return {"ok": True, "op": "stopped"}

        holder: dict = {}
        srv = UnixControlServer(self.sock_dir, handler)
        holder["srv"] = srv
        srv.start()
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path, timeout_s=2)
        self.addCleanup(client.close)
        try:
            client.request({"op": "stop"})
        except (ConnectionError, OSError, ct.FrameError):
            pass
        self.assertIs(seen.get("free"), True)

    def test_unlink_error_still_shuts_peers_down(self) -> None:
        """A failed unlink must not skip shutdown, the worker join, or abandon.

        The directory is mode 0500, so ``os.unlink`` raises ``PermissionError``.
        The flock is still released before that shutdown.
        """
        import fcntl

        entered = threading.Event()
        done = threading.Event()

        def handler(message: dict, conn: ct.Connection) -> dict:
            entered.set()
            conn.cancelled.wait(3)
            done.set()
            return {"ok": True}

        srv = UnixControlServer(self.sock_dir, handler, close_timeout_s=0.3)
        srv.start()
        self.addCleanup(os.chmod, self.sock_dir, 0o700)
        self.addCleanup(srv.close)
        client = UnixControlClient(srv.path, timeout_s=1)
        self.addCleanup(client.close)

        def run() -> None:
            try:
                client.request({"op": "hold", "id": "h1"})
            except (ConnectionError, OSError, ct.FrameError):
                pass

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(entered.wait(2))
        with srv._lock:
            self.assertTrue(srv._workers)
            self.assertTrue(srv._conns)

        lock_path = os.path.join(self.sock_dir, "control.sock.lock")
        seen: dict = {}
        orig_shutdown = socket.socket.shutdown

        def spy(sock: socket.socket, how: int) -> None:
            if "free" not in seen and os.path.exists(lock_path):
                fd = os.open(lock_path, os.O_RDWR)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        seen["free"] = False
                    else:
                        seen["free"] = True
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            seen["n"] = seen.get("n", 0) + 1
            orig_shutdown(sock, how)

        socket.socket.shutdown = spy  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "shutdown", orig_shutdown)
        os.chmod(self.sock_dir, 0o500)
        with self.assertRaises(PermissionError) as caught:
            srv.close()
        self.assertEqual(caught.exception.errno, errno.EACCES)
        self.assertGreaterEqual(seen.get("n", 0), 1)
        self.assertIs(seen.get("free"), True)
        self.assertTrue(done.wait(1))

    def _refuse_and_keep(self, directory: str, path: str, inode: int, mode: int, *, name: str = "control.sock") -> None:
        """Start must raise, and ``path`` must still be that socket inode and mode."""
        srv = UnixControlServer(directory, ct.with_ping_health(), name=name)
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            srv.start()
        self.assertIn("Remove this socket if it is stale.", str(caught.exception))
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, inode)
        self.assertEqual(stat.S_IMODE(listed.st_mode), mode)

    def test_mode_000_dead_socket_is_left_for_the_operator(self) -> None:
        """Owner decision: a dead mode-000 socket is not reclaimed.

        ``connect`` returns EACCES for it and for a live mode-000 listener.
        The operator removes the file. Nothing here unlinks or chmods it.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        self._refuse_and_keep(self.sock_dir, path, inode, 0)

    def test_live_mode_000_listener_is_not_removed(self) -> None:
        """A same-uid listener at mode 000 is not a stale socket.

        ``connect`` returns EACCES for that listener and for a closed socket
        at mode 000. Start must refuse without chmod and without unlink.
        A later connect is not evidence: chmod 0600 would make it succeed
        while leaving the listener's mode changed. The proc Inode column is
        the sockfs inode, not this ``st_ino``, so a match on that column
        would miss the listener and unlink it.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(socket.SOMAXCONN)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, inode)
        self.assertEqual(stat.S_IMODE(listed.st_mode), 0)

    def test_live_mode_070_listener_keeps_its_mode(self) -> None:
        """Owner bits of 000 with group rwx still cannot be connected by us."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(socket.SOMAXCONN)
        os.chmod(path, 0o070)
        inode = os.lstat(path).st_ino
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, inode)
        self.assertEqual(stat.S_IMODE(listed.st_mode), 0o070)

    def test_open_unlistening_mode_000_socket_is_not_removed(self) -> None:
        """A bound socket we still hold is not stale, even when connect is EACCES."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(path)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, inode)
        self.assertEqual(stat.S_IMODE(listed.st_mode), 0)

    def test_lingering_client_mode_000_is_not_removed(self) -> None:
        """A connected client with the listener already closed is not stale.

        ``connect`` is EACCES at mode 000. The file stays whether or not a
        proc table still names this path.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(path)
        accepted, _ = listener.accept()
        self.addCleanup(accepted.close)
        listener.close()
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        self._refuse_and_keep(self.sock_dir, path, inode, 0)

    def test_unreadable_proc_does_not_reclaim(self) -> None:
        """An unreadable ``/proc/net/unix`` is not proof the socket is dead."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        real_open = builtins.open

        def fake_open(file, *args, **kwargs):
            if file == "/proc/net/unix":
                raise OSError(errno.EIO, "unreadable")
            return real_open(file, *args, **kwargs)

        builtins.open = fake_open
        self.addCleanup(setattr, builtins, "open", real_open)
        try:
            self._refuse_and_keep(self.sock_dir, path, inode, 0)
        finally:
            builtins.open = real_open

    def test_poison_proc_row_does_not_reclaim(self) -> None:
        """A row ``/proc/net/unix`` cannot parse must not be treated as absent."""
        header = b"Num RefCount Protocol Flags Type St Inode Path\n"
        os.mkdir(self.sock_dir, 0o700)
        for label, extra in (
            ("hidden", b"garbage-row\n"),
            ("names-path", None),
        ):
            with self.subTest(row=label):
                path = os.path.join(self.sock_dir, label + ".sock")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(listener.close)
                listener.bind(path)
                listener.listen(1)
                os.chmod(path, 0)
                inode = os.lstat(path).st_ino
                if extra is None:
                    payload = header + b"garbage " + os.fsencode(path) + b"\n"
                else:
                    payload = header + extra
                real_open = builtins.open

                def fake_open(file, *args, **kwargs):
                    if file == "/proc/net/unix":
                        return io.BytesIO(payload)
                    return real_open(file, *args, **kwargs)

                builtins.open = fake_open
                try:
                    self._refuse_and_keep(self.sock_dir, path, inode, 0, name=os.path.basename(path))
                finally:
                    builtins.open = real_open

    def test_symlink_alias_mode_000_listener_is_not_removed(self) -> None:
        """A live listener bound on the real path survives a start through a parent symlink.

        ``/proc/net/unix`` records the bind string. The alias is a different string
        for the same inode, so a path compare would call the listener absent.
        """
        real_holder = os.path.join(self.base, "real_holder")
        os.mkdir(real_holder, 0o700)
        real = os.path.join(real_holder, "run")
        os.mkdir(real, 0o700)
        os.symlink(real_holder, os.path.join(self.base, "link"))
        alias = os.path.join(self.base, "link", "run")
        path = os.path.join(real, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        self.assertNotEqual(os.path.join(alias, "control.sock"), path)
        self._refuse_and_keep(alias, path, inode, 0)

    def test_dotdot_alias_mode_070_listener_is_not_removed(self) -> None:
        """A ``..`` directory alias is a different string for the same listener."""
        real = os.path.join(self.base, "run")
        os.mkdir(real, 0o700)
        alias = os.path.join(real, "..", "run")
        path = os.path.join(real, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        os.chmod(path, 0o070)
        inode = os.lstat(path).st_ino
        self.assertNotEqual(os.path.join(alias, "control.sock"), path)
        self._refuse_and_keep(alias, path, inode, 0o070)

    def _odd_name_listener_is_not_removed(self, name: str) -> None:
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, name)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        self._refuse_and_keep(self.sock_dir, path, inode, 0, name=name)

    def test_newline_path_listener_is_not_removed(self) -> None:
        """A newline in the path is a live socket. ``str.splitlines`` would hide it."""
        self._odd_name_listener_is_not_removed("control\nsock")

    def test_vertical_tab_path_listener_is_not_removed(self) -> None:
        """A vertical tab in the path is a live socket. ``str.splitlines`` would hide it."""
        self._odd_name_listener_is_not_removed("control\vsock")

    def test_mocked_permission_error_leaves_a_live_listener(self) -> None:
        """EACCES/EPERM from connect must not unlink, even when no proc row names the path.

        Stands in for a listener in another net or mount namespace: this process
        cannot connect, and this namespace's unix table does not list the path.
        """
        header = b"Num RefCount Protocol Flags Type St Inode Path\n"
        for err in (errno.EACCES, errno.EPERM):
            with self.subTest(err=errno.errorcode[err]):
                directory = os.path.join(self.base, errno.errorcode[err])
                os.mkdir(directory, 0o700)
                path = os.path.join(directory, "control.sock")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(listener.close)
                listener.bind(path)
                listener.listen(1)
                listed = os.lstat(path)
                inode = listed.st_ino
                mode = stat.S_IMODE(listed.st_mode)
                orig_connect = socket.socket.connect
                real_open = builtins.open

                def fake_connect(sock, addr, _path=path, _err=err, _orig=orig_connect):
                    if addr == _path:
                        raise OSError(_err, os.strerror(_err))
                    return _orig(sock, addr)

                def fake_open(file, *args, **kwargs):
                    if file == "/proc/net/unix":
                        return io.BytesIO(header)
                    return real_open(file, *args, **kwargs)

                socket.socket.connect = fake_connect  # type: ignore[method-assign]
                builtins.open = fake_open
                try:
                    self._refuse_and_keep(directory, path, inode, mode)
                finally:
                    socket.socket.connect = orig_connect  # type: ignore[method-assign]
                    builtins.open = real_open

    def test_eacces_does_not_chmod_a_swapped_file(self) -> None:
        """The EACCES path must not chmod. A file swapped in under that chmod used to land at 0600."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(socket.SOMAXCONN)
        os.chmod(path, 0)
        inode = os.lstat(path).st_ino
        planted: dict = {}
        orig_chmod = unix.os.chmod

        def fake_chmod(target, mode, follow_symlinks=True, **kwargs):
            if target == path and "ino" not in planted:
                os.unlink(path)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
                os.write(fd, b"keep")
                os.close(fd)
                planted["ino"] = os.lstat(path).st_ino
            return orig_chmod(target, mode, follow_symlinks=follow_symlinks, **kwargs)

        unix.os.chmod = fake_chmod
        self.addCleanup(setattr, unix.os, "chmod", orig_chmod)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        if planted:
            listed = os.lstat(path)
            self.assertEqual(listed.st_ino, planted["ino"])
            self.assertEqual(stat.S_IMODE(listed.st_mode), 0o700)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"keep")
        else:
            listed = os.lstat(path)
            self.assertTrue(stat.S_ISSOCK(listed.st_mode))
            self.assertEqual(listed.st_ino, inode)
            self.assertEqual(stat.S_IMODE(listed.st_mode), 0)

    def test_eacces_does_not_chmod_through_a_parent_symlink(self) -> None:
        """``chmod(..., follow_symlinks=False)`` still walks a parent symlink."""
        real_holder = os.path.join(self.base, "real_holder")
        victim_holder = os.path.join(self.base, "victim_holder")
        os.mkdir(real_holder, 0o700)
        os.mkdir(victim_holder, 0o700)
        real_run = os.path.join(real_holder, "run")
        victim_run = os.path.join(victim_holder, "run")
        os.mkdir(real_run, 0o700)
        os.mkdir(victim_run, 0o700)
        link_holder = os.path.join(self.base, "link_holder")
        os.symlink(real_holder, link_holder)
        run = os.path.join(link_holder, "run")
        path = os.path.join(run, "control.sock")
        victim = os.path.join(victim_run, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(socket.SOMAXCONN)
        os.chmod(path, 0)
        fd = os.open(victim, os.O_CREAT | os.O_WRONLY, 0o700)
        os.write(fd, b"keep")
        os.close(fd)
        swapped = {"n": 0}
        orig_chmod = unix.os.chmod

        def fake_chmod(target, mode, follow_symlinks=True, **kwargs):
            if target == path and not swapped["n"]:
                swapped["n"] += 1
                os.unlink(link_holder)
                os.symlink(victim_holder, link_holder)
            return orig_chmod(target, mode, follow_symlinks=follow_symlinks, **kwargs)

        unix.os.chmod = fake_chmod
        self.addCleanup(setattr, unix.os, "chmod", orig_chmod)
        srv = UnixControlServer(run, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        self.assertEqual(stat.S_IMODE(os.lstat(victim).st_mode), 0o700)
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0)
        self.assertTrue(stat.S_ISSOCK(os.lstat(path).st_mode))

    def test_refused_unlink_keeps_a_swapped_file(self) -> None:
        """ECONNREFUSED must not unlink by path: a file swapped in after the probe stays."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        planted: dict = {}
        orig_connect = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            try:
                orig_connect(sock, addr)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED and addr == path and "ino" not in planted:
                    os.unlink(path)
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
                    os.write(fd, b"keep")
                    os.close(fd)
                    planted["ino"] = os.lstat(path).st_ino
                raise

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig_connect)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises((OSError, ct.InsecureSocketPath)):
            srv.start()
        self.assertIn("ino", planted)
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, planted["ino"])
        self.assertEqual(stat.S_IMODE(listed.st_mode), 0o700)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")

    def test_refused_unlink_does_not_remove_a_swapped_symlink(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        victim = os.path.join(self.base, "victim")
        with open(victim, "wb") as fh:
            fh.write(b"secret")
        os.chmod(victim, 0o644)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        planted = {"n": 0}
        orig_connect = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            try:
                orig_connect(sock, addr)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED and addr == path and not planted["n"]:
                    planted["n"] += 1
                    os.unlink(path)
                    os.symlink(victim, path)
                raise

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig_connect)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises((OSError, ct.InsecureSocketPath)):
            srv.start()
        self.assertTrue(os.path.islink(path))
        self.assertEqual(os.readlink(path), victim)
        self.assertEqual(stat.S_IMODE(os.lstat(victim).st_mode), 0o644)
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"secret")

    def test_refused_unlink_does_not_drop_a_listener_on_a_reused_inode(self) -> None:
        """A live socket bound in the ECONNREFUSED window must keep its name.

        ext4 reuses the inode of a closed unix socket as soon as the name is
        unlinked. A saved ``(st_dev, st_ino)`` then matches the replacement,
        and ``S_ISSOCK`` does not reject it. A swapped regular file does not
        catch this.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        planted: dict = {}
        orig_connect = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            try:
                orig_connect(sock, addr)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED and addr == path and "sock" not in planted:
                    os.unlink(path)
                    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    live.bind(path)
                    live.listen(1)
                    planted["sock"] = live
                    planted["ino"] = os.lstat(path).st_ino
                raise

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig_connect)
        self.addCleanup(lambda: planted["sock"].close() if "sock" in planted else None)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises((OSError, ct.InsecureSocketPath)):
            srv.start()
        self.assertIn("sock", planted)
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, planted["ino"])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        planted["sock"].settimeout(2)
        client.connect(path)
        conn, _ = planted["sock"].accept()
        conn.close()

    def test_full_backlog_eagain_is_live_and_not_unlinked(self) -> None:
        """A full accept queue is a live listener. ``EAGAIN`` must not unlink it."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        listed = os.lstat(path)
        inode = listed.st_ino
        mode = stat.S_IMODE(listed.st_mode)
        fillers: list[socket.socket] = []
        self.addCleanup(lambda: [client.close() for client in fillers])
        while len(fillers) < 64:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.setblocking(False)
            try:
                client.connect(path)
            except BlockingIOError as exc:
                self.assertEqual(exc.errno, errno.EAGAIN)
                client.close()
                break
            fillers.append(client)
        else:
            self.fail("accept queue never returned EAGAIN")
        self.assertGreaterEqual(len(fillers), 1)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            srv.start()
        self.assertIn("is live", str(caught.exception))
        kept = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(kept.st_mode))
        self.assertEqual(kept.st_ino, inode)
        self.assertEqual(stat.S_IMODE(kept.st_mode), mode)
        again = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(again.close)
        again.setblocking(False)
        with self.assertRaises(BlockingIOError) as blocked:
            again.connect(path)
        self.assertEqual(blocked.exception.errno, errno.EAGAIN)

    def _swap_before_mode_change(self, plant) -> None:
        """Run ``plant(path)`` once, inside the chmod that follows bind."""
        path = os.path.join(self.sock_dir, "control.sock")
        done = {"n": 0}
        orig_chmod = unix.os.chmod

        def fake_chmod(target, mode, follow_symlinks=True, **kwargs):
            if target == path and not done["n"]:
                done["n"] += 1
                plant(path)
            return orig_chmod(target, mode, follow_symlinks=follow_symlinks, **kwargs)

        unix.os.chmod = fake_chmod
        self.addCleanup(setattr, unix.os, "chmod", orig_chmod)

    def test_chmod_does_not_follow_a_swapped_symlink(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        victim = os.path.join(self.base, "victim")
        with open(victim, "w", encoding="utf-8") as fh:
            fh.write("secret")
        os.chmod(victim, 0o644)

        def plant(path: str) -> None:
            os.unlink(path)
            os.symlink(victim, path)

        self._swap_before_mode_change(plant)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        self.assertEqual(stat.S_IMODE(os.lstat(victim).st_mode), 0o644)
        self.assertTrue(os.path.islink(os.path.join(self.sock_dir, "control.sock")))

    def test_swapped_socket_is_not_adopted_or_unlinked(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        planted: dict = {}

        def plant(path: str) -> None:
            os.unlink(path)
            other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            other.bind(path)
            planted["sock"] = other
            planted["ino"] = os.lstat(path).st_ino

        self.addCleanup(lambda: planted["sock"].close() if "sock" in planted else None)
        self._swap_before_mode_change(plant)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        path = os.path.join(self.sock_dir, "control.sock")
        self.assertEqual(os.lstat(path).st_ino, planted["ino"])
        srv.close()
        self.assertEqual(os.lstat(path).st_ino, planted["ino"])


def _at_path(target: object, dir_fd: int | None) -> str:
    """Path ``target`` names, relative to ``dir_fd`` when that fd is set."""
    if isinstance(target, bytes):
        target = os.fsdecode(target)
    if not isinstance(target, str):
        return ""
    if dir_fd is None:
        return target
    try:
        directory = os.readlink(f"/proc/self/fd/{dir_fd}")
    except OSError:
        return target
    return os.path.join(directory, target)


def _opath_pin_count(path: str) -> int:
    """Open ``O_PATH`` fds in this process whose path is ``path``."""
    count = 0
    opath = getattr(os, "O_PATH", 0)
    for name in os.listdir("/proc/self/fd"):
        if not name.isdigit():
            continue
        try:
            if os.readlink(f"/proc/self/fd/{name}") != path:
                continue
            with open(f"/proc/self/fdinfo/{name}", encoding="ascii") as info:
                for line in info:
                    if line.startswith("flags:"):
                        if int(line.split()[1], 8) & opath:
                            count += 1
                        break
        except OSError:
            continue
    return count


def _name_held(path: str, inode: int, wait_s: float) -> bool:
    """True when ``path`` names ``inode`` within ``wait_s``.

    A quarantine rename moves the name aside and puts it back. The wait covers
    that gap. A path unlink does not put the name back.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            listed = os.lstat(path)
        except FileNotFoundError:
            listed = None
        if listed is not None and stat.S_ISSOCK(listed.st_mode) and listed.st_ino == inode:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.0002)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class InodeQuarantineTests(_TmpCase):
    def _plant_hooks(self, path: str, planted: dict) -> None:
        """Hook ``os.unlink`` and ``os.rename`` so a listener appears at removal.

        On a path unlink the hook binds the listener and then performs the
        caller's unlink, which drops that name. On a rename of the socket the
        hook binds after the name has moved and leaves the listener in place.
        """
        orig_unlink = unix.os.unlink
        orig_rename = unix.os.rename
        orig_rn = unix._rename_noreplace

        def plant() -> None:
            if "sock" in planted:
                return
            live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live.bind(path)
            live.listen(1)
            planted["sock"] = live
            planted["ino"] = os.lstat(path).st_ino

        def fake_unlink(target: object, *args: object, **kwargs: object):
            full = _at_path(target, kwargs.get("dir_fd"))  # type: ignore[arg-type]
            if full == path and "sock" not in planted:
                orig_unlink(target, *args, **kwargs)
                plant()
                return orig_unlink(target, *args, **kwargs)
            return orig_unlink(target, *args, **kwargs)

        def moved_public(src: object, dfd: int | None, moved: bool) -> None:
            if moved and _at_path(src, dfd) == path and "sock" not in planted:
                plant()

        def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
            full = _at_path(src, kwargs.get("src_dir_fd"))  # type: ignore[arg-type]
            result = orig_rename(src, dst, *args, **kwargs)
            moved_public(src, kwargs.get("src_dir_fd"), full == path)  # type: ignore[arg-type]
            return result

        def fake_rn(dfd: int, src: str, dst: str):
            result = orig_rn(dfd, src, dst)
            moved_public(src, dfd, result == "moved" or result is True)
            return result

        unix.os.unlink = fake_unlink  # type: ignore[assignment]
        unix.os.rename = fake_rename  # type: ignore[assignment]
        unix._rename_noreplace = fake_rn  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "unlink", orig_unlink)
        self.addCleanup(setattr, unix.os, "rename", orig_rename)
        self.addCleanup(setattr, unix, "_rename_noreplace", orig_rn)
        self.addCleanup(lambda: planted["sock"].close() if "sock" in planted else None)

    def _assert_plant_keeps_its_name(self, path: str, planted: dict) -> None:
        self.assertIn("sock", planted)
        listed = os.lstat(path)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, planted["ino"])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        planted["sock"].settimeout(2)
        client.connect(path)
        conn, _ = planted["sock"].accept()
        conn.close()

    def test_removal_hook_keeps_the_listener_it_plants(self) -> None:
        """A listener planted inside unlink/rename at the moment of removal keeps its name.

        ``lstat`` matching the pin used to be followed by ``unlink(path)``. The
        hook binds in that call, and the path unlink removes the new listener.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        planted: dict = {}
        self._plant_hooks(path, planted)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        try:
            srv.start()
        except (OSError, ct.InsecureSocketPath):
            pass
        self._assert_plant_keeps_its_name(path, planted)

    def test_close_hook_replacement_keeps_its_name(self) -> None:
        """close() must not drop a socket planted in its unlink, even if the inode is reused.

        The listen fd is already closed, so without an ``O_PATH`` pin the
        replacement can reuse ``(st_dev, st_ino)`` and a path unlink removes it.
        """
        os.mkdir(self.sock_dir, 0o700)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        planted: dict = {}
        self._plant_hooks(srv.path, planted)
        try:
            srv.close()
        except ct.InsecureSocketPath:
            pass
        self._assert_plant_keeps_its_name(srv.path, planted)

    def test_unlistening_socket_reusing_an_inode_before_the_pin_is_not_reclaimed(self) -> None:
        """A bound socket that has not listen()ed must keep its name.

        The old order lstat'd, then opened the pin. A socket bound in that gap
        reused the inode, connect returned ECONNREFUSED, and the name was removed.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        planted: dict = {}
        orig_lstat = unix.os.lstat
        orig_open = unix.os.open

        def plant_unlistening() -> None:
            if "sock" in planted:
                return
            os.unlink(path)
            live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live.bind(path)
            planted["sock"] = live
            planted["ino"] = orig_lstat(path).st_ino

        def fake_lstat(target: object, *args: object, **kwargs: object):
            if target == path and "sock" not in planted:
                current = orig_lstat(target, *args, **kwargs)
                if stat.S_ISSOCK(current.st_mode):
                    plant_unlistening()
                    return orig_lstat(target, *args, **kwargs)
            return orig_lstat(target, *args, **kwargs)

        def fake_open(target: object, flags: int, *args: object, **kwargs: object):
            fd = orig_open(target, flags, *args, **kwargs)
            if (
                target == path
                and flags & os.O_PATH
                and not flags & getattr(os, "O_DIRECTORY", 0)
                and "sock" not in planted
            ):
                plant_unlistening()
            return fd

        unix.os.lstat = fake_lstat  # type: ignore[assignment]
        unix.os.open = fake_open  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "lstat", orig_lstat)
        self.addCleanup(setattr, unix.os, "open", orig_open)
        self.addCleanup(lambda: planted["sock"].close() if "sock" in planted else None)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        try:
            srv.start()
        except (OSError, ct.InsecureSocketPath):
            pass
        self.assertIn("sock", planted)
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, planted["ino"])
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        planted["sock"].listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        planted["sock"].settimeout(2)
        client.connect(path)
        conn, _ = planted["sock"].accept()
        conn.close()

    def test_forked_child_has_no_inode_pin(self) -> None:
        """The server holds an O_PATH pin, and a forked child does not inherit it."""
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        self.assertEqual(_opath_pin_count(srv.path), 1)
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd) if read_fd >= 0 else None)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*multi-threaded.*", category=DeprecationWarning)
            pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                os.write(write_fd, f"{_opath_pin_count(srv.path)}\n".encode())
                os.close(write_fd)
            finally:
                os._exit(0)
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        got = b""
        deadline = time.monotonic() + 2.0
        while b"\n" not in got and time.monotonic() < deadline:
            try:
                chunk = os.read(read_fd, 64)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            if not chunk:
                break
            got += chunk
        self.addCleanup(_reap, pid)
        self.assertEqual(got, b"0\n")
        # The child's close of its dup must leave the parent's pin in place.
        self.assertEqual(_opath_pin_count(srv.path), 1)
        srv.close()
        self.assertEqual(_opath_pin_count(srv.path), 0)
        self.assertEqual(_opath_pin_count(self.sock_dir), 0)

    def test_racing_reclaim_loses_no_listener(self) -> None:
        """A listener bound while a stale socket is reclaimed keeps its name.

        About two seconds, one reclaimer and one listener. A socket counts
        only after a client has connected, so a name removed before listen
        is not a lost listener. On ef816f7c the path unlink after lstat
        removed listeners that had replaced the stale inode.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        deadline = time.monotonic() + 2.0
        stats = {"lost": 0, "armed": 0}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def reclaimer() -> None:
            while time.monotonic() < deadline:
                try:
                    unix._clear_stale_socket(path)
                except (ct.InsecureSocketPath, OSError):
                    pass
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        def listener() -> None:
            while time.monotonic() < deadline:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client: socket.socket | None = None
                try:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                    try:
                        sock.bind(path)
                    except OSError:
                        continue
                    sock.listen(1)
                    try:
                        inode = os.lstat(path).st_ino
                    except FileNotFoundError:
                        continue
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.settimeout(0.05)
                    try:
                        client.connect(path)
                    except OSError:
                        continue
                    time.sleep(0)
                    kept = _name_held(path, inode, 0.02)
                    with lock:
                        stats["armed"] += 1
                        if not kept:
                            stats["lost"] += 1
                except OSError:
                    continue
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return
                finally:
                    if client is not None:
                        client.close()
                    sock.close()

        threads = [
            threading.Thread(target=reclaimer, name="reclaim-race"),
            threading.Thread(target=listener, name="listener-race"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertGreater(stats["armed"], 100, stats)
        self.assertEqual(stats["lost"], 0, stats)


def _q_prefix(socket_name: str) -> str:
    """The quarantine prefix ``unix`` reaps. Duplicated so the test runs before the helper exists."""
    import hashlib

    return ".q" + hashlib.sha256(os.fsencode(socket_name)).hexdigest()[:4]


def _resolved_connect_path(addr: object) -> str | None:
    """Directory entry a pathname connect looks up.

    ``/proc/self/fd/<dirfd>/<name>`` is that directory entry.
    ``/proc/self/fd/<fd>`` with no further component is an inode the fd
    already holds; ``readlink`` is only where that inode is linked, and
    connect does not follow a swap of the link.
    """
    if not isinstance(addr, str) or not addr.startswith("/"):
        return None
    marker = "/proc/self/fd/"
    if not addr.startswith(marker):
        return addr
    rest = addr[len(marker):]
    if rest.isdigit():
        try:
            link = os.readlink(addr)
        except OSError:
            return None
        return link if link.startswith("/") else None
    if "/" not in rest:
        return None
    fd, name = rest.split("/", 1)
    if not fd.isdigit() or not name or "/" in name:
        return None
    try:
        directory = os.readlink(marker + fd)
    except OSError:
        return None
    if not directory.startswith("/"):
        return None
    return os.path.join(directory, name)


def _linked_socket(directory: str, inode: int) -> str | None:
    """Path in ``directory`` that is the socket inode ``inode``, if it is still linked."""
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            listed = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISSOCK(listed.st_mode) and not stat.S_ISLNK(listed.st_mode) and listed.st_ino == inode:
            return path
    return None


def _socket_entries(directory: str) -> list[str]:
    found = []
    for name in os.listdir(directory):
        try:
            st = os.lstat(os.path.join(directory, name))
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            found.append(name)
    return found


def _refused_sockets(directory: str) -> list[str]:
    """Sockets in ``directory`` whose connect fails with ``ECONNREFUSED``."""
    refused = []
    for name in _socket_entries(directory):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            try:
                probe.connect(os.path.join(directory, name))
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    refused.append(name)
        finally:
            probe.close()
    return refused


@unittest.skipUnless(LINUX, "Linux is the tested path")
class QuarantineRestoreTests(_TmpCase):
    """Failures after the quarantine rename must not abandon the inode.

    Each case failed on 56af0d69: ``os.rename`` replaced a name that appeared
    after ``lstat``, a failing ``unlink`` or a too-long recheck path left the
    hidden socket in place, and ``ENOENT`` from a path connect was treated as
    a finished reclaim.
    """

    def _stale(self, directory: str, name: str = "control.sock") -> tuple[str, int]:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        path = os.path.join(directory, name)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        return path, os.lstat(path).st_ino

    def _exact_dir(self, name: str, total: int = 107) -> str:
        parent = tempfile.mkdtemp(prefix="q", dir="/tmp")
        self.addCleanup(self._rm_tree, parent)
        need = total - len(name) - 1
        self.assertGreaterEqual(need, len(parent))
        if need == len(parent):
            directory = parent
        else:
            directory = os.path.join(parent, "d" * (need - len(parent) - 1))
            os.makedirs(directory, mode=0o700)
        path = os.path.join(directory, name)
        self.assertEqual(len(os.fsencode(path)), total)
        os.chmod(directory, 0o700)
        return directory

    def _rm_tree(self, root: str) -> None:
        for current, dirs, files in os.walk(root, topdown=False):
            for name in files:
                try:
                    os.unlink(os.path.join(current, name))
                except OSError:
                    pass
            for name in dirs:
                path = os.path.join(current, name)
                try:
                    if os.path.islink(path):
                        os.unlink(path)
                    else:
                        os.chmod(path, 0o700)
                        os.rmdir(path)
                except OSError:
                    pass
        try:
            os.rmdir(root)
        except OSError:
            pass

    def test_restore_failure_is_reaped_on_the_next_start(self) -> None:
        """A hidden socket left when restore cannot move back is removed by the next start().

        Non-sockets and live listeners that share the quarantine prefix stay.
        """
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        prefix = _q_prefix("control.sock")
        kept_file = os.path.join(self.sock_dir, prefix + "file!!")
        with open(kept_file, "wb") as fh:
            fh.write(b"keep")
        live_name = prefix + "live!!"
        live_path = os.path.join(self.sock_dir, live_name)
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(live.close)
        live.bind(live_path)
        live.listen(1)
        live_ino = os.lstat(live_path).st_ino
        dead_name = prefix + "dead!!"
        dead_path = os.path.join(self.sock_dir, dead_name)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(dead_path)
        dead.close()
        os.chmod(dead_path, 0)
        dead_ino = os.lstat(dead_path).st_ino

        def abandon_one() -> str | None:
            if os.path.lexists(path):
                os.unlink(path)
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()
            occupied = {"n": 0}
            orig_rename = unix.os.rename
            orig_rn = unix._rename_noreplace
            orig_connect = socket.socket.connect

            def occupy(src: object, dfd: int | None) -> None:
                if occupied["n"] or _at_path(src, dfd) != path:
                    return
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, b"block")
                os.close(fd)
                occupied["n"] = 1

            def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
                result = orig_rename(src, dst, *args, **kwargs)
                occupy(src, kwargs.get("src_dir_fd"))  # type: ignore[arg-type]
                return result

            def fake_rn(dfd: int, src: str, dst: str):
                result = orig_rn(dfd, src, dst)
                if result is True or result == "moved":
                    occupy(src, dfd)
                return result

            def fake_connect(sock: socket.socket, addr: object) -> None:
                if occupied["n"] and addr != path:
                    raise OSError(errno.EACCES, "Permission denied")
                orig_connect(sock, addr)

            unix.os.rename = fake_rename  # type: ignore[assignment]
            unix._rename_noreplace = fake_rn  # type: ignore[assignment]
            socket.socket.connect = fake_connect  # type: ignore[method-assign]
            try:
                srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
                try:
                    srv.start()
                except (OSError, ct.InsecureSocketPath):
                    pass
                finally:
                    srv.close()
            finally:
                unix.os.rename = orig_rename  # type: ignore[assignment]
                unix._rename_noreplace = orig_rn  # type: ignore[assignment]
                socket.socket.connect = orig_connect  # type: ignore[method-assign]
            if os.path.lexists(path) and not stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)
            hidden = [name for name in _refused_sockets(self.sock_dir) if name not in (live_name, dead_name, "control.sock")]
            return hidden[0] if hidden else None

        left = [name for _ in range(4) if (name := abandon_one())]
        self.assertGreaterEqual(len(left), 1, os.listdir(self.sock_dir))
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        self.assertEqual(
            [name for name in _refused_sockets(self.sock_dir) if name != "control.sock"],
            [],
            os.listdir(self.sock_dir),
        )
        with open(kept_file, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")
        self.assertEqual(os.lstat(live_path).st_ino, live_ino)
        self.assertEqual(os.lstat(dead_path).st_ino, dead_ino)
        self.assertEqual(stat.S_IMODE(os.lstat(dead_path).st_mode), 0)

    def test_listener_planted_on_a_quarantine_name_is_not_replaced(self) -> None:
        """A listener bound on the quarantine name before the move keeps that name."""
        os.mkdir(self.sock_dir, 0o700)
        path, _inode = self._stale(self.sock_dir)
        planted: dict = {}
        orig_rename = unix.os.rename
        orig_rn = unix._rename_noreplace

        def plant(dst: object, dfd: int | None) -> None:
            if planted or not isinstance(dst, str) or not dst.startswith("."):
                return
            full = _at_path(dst, dfd)
            if os.path.lexists(full):
                return
            live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live.bind(full)
            live.listen(1)
            planted["sock"] = live
            planted["path"] = full
            planted["ino"] = os.lstat(full).st_ino

        def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
            plant(dst, kwargs.get("dst_dir_fd"))  # type: ignore[arg-type]
            return orig_rename(src, dst, *args, **kwargs)

        def fake_rn(dfd: int, src: str, dst: str):
            plant(dst, dfd)
            return orig_rn(dfd, src, dst)

        unix.os.rename = fake_rename  # type: ignore[assignment]
        unix._rename_noreplace = fake_rn  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "rename", orig_rename)
        self.addCleanup(setattr, unix, "_rename_noreplace", orig_rn)
        self.addCleanup(lambda: planted["sock"].close() if "sock" in planted else None)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        srv.start()
        self.assertIn("sock", planted, os.listdir(self.sock_dir))
        listed = os.lstat(planted["path"])
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(listed.st_ino, planted["ino"])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        planted["sock"].settimeout(2)
        client.connect(planted["path"])
        conn, _ = planted["sock"].accept()
        conn.close()
        with UnixControlClient(srv.path) as control:
            self.assertEqual(control.request({"op": "ping"})["op"], "pong")

    def test_directory_at_a_quarantine_name_is_not_a_hard_error(self) -> None:
        """A directory created on the quarantine name is left alone and another name is used."""
        os.mkdir(self.sock_dir, 0o700)
        self._stale(self.sock_dir)
        planted: dict = {}
        orig_rename = unix.os.rename
        orig_rn = unix._rename_noreplace

        def plant(dst: object, dfd: int | None) -> None:
            if planted or not isinstance(dst, str) or not dst.startswith("."):
                return
            full = _at_path(dst, dfd)
            if os.path.lexists(full):
                return
            os.mkdir(full, 0o700)
            planted["path"] = full
            planted["ino"] = os.lstat(full).st_ino

        def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
            plant(dst, kwargs.get("dst_dir_fd"))  # type: ignore[arg-type]
            return orig_rename(src, dst, *args, **kwargs)

        def fake_rn(dfd: int, src: str, dst: str):
            plant(dst, dfd)
            return orig_rn(dfd, src, dst)

        unix.os.rename = fake_rename  # type: ignore[assignment]
        unix._rename_noreplace = fake_rn  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "rename", orig_rename)
        self.addCleanup(setattr, unix, "_rename_noreplace", orig_rn)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        srv.start()
        self.assertIn("path", planted)
        listed = os.lstat(planted["path"])
        self.assertTrue(stat.S_ISDIR(listed.st_mode))
        self.assertEqual(listed.st_ino, planted["ino"])
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")

    def test_unlink_permission_error_restores_the_public_name(self) -> None:
        """``PermissionError`` from unlink after the rename puts the public name back."""
        os.mkdir(self.sock_dir, 0o700)
        path, inode = self._stale(self.sock_dir)
        orig_unlink = unix.os.unlink

        def fake_unlink(target: object, *args: object, **kwargs: object):
            full = _at_path(target, kwargs.get("dir_fd"))  # type: ignore[arg-type]
            if full.startswith(self.sock_dir + os.sep) and os.path.basename(full) not in ("control.sock", "control.sock.lock"):
                raise PermissionError(errno.EACCES, "Permission denied", full)
            return orig_unlink(target, *args, **kwargs)

        unix.os.unlink = fake_unlink  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "unlink", orig_unlink)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(PermissionError) as caught:
            srv.start()
        self.assertEqual(caught.exception.errno, errno.EACCES)
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual(_refused_sockets(self.sock_dir), ["control.sock"])

    def test_close_unlink_permission_error_restores_the_public_name(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        inode = os.lstat(srv.path).st_ino
        orig_unlink = unix.os.unlink

        def fake_unlink(target: object, *args: object, **kwargs: object):
            full = _at_path(target, kwargs.get("dir_fd"))  # type: ignore[arg-type]
            if os.path.basename(str(full)).startswith("."):
                raise PermissionError(errno.EACCES, "Permission denied", full)
            return orig_unlink(target, *args, **kwargs)

        unix.os.unlink = fake_unlink  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "unlink", orig_unlink)
        with self.assertRaises(PermissionError) as caught:
            srv.close()
        self.assertEqual(caught.exception.errno, errno.EACCES)
        listed = os.lstat(srv.path)
        self.assertEqual(listed.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual([name for name in _socket_entries(self.sock_dir) if name.startswith(".")], [])

    def test_long_path_does_not_abandon_the_socket(self) -> None:
        """A 107-byte socket path is not moved aside when the quarantine name cannot fit."""
        name = "s"
        directory = self._exact_dir(name, 107)
        path, inode = self._stale(directory, name)
        self.assertEqual(len(os.fsencode(path)), 107)
        srv = UnixControlServer(directory, ct.with_ping_health(), name=name)
        self.addCleanup(srv.close)
        exc: BaseException | None = None
        try:
            srv.start()
        except BaseException as err:  # noqa: BLE001 - the assertion reports the type and the directory
            exc = err
        entries = os.listdir(directory)
        self.assertIsInstance(exc, ct.InsecureSocketPath, (type(exc).__name__, exc, entries))
        self.assertIn("Left in place", str(exc))
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual([entry for entry in entries if entry.startswith(".")], [])

    def test_long_path_that_fits_a_quarantine_name_is_reclaimed(self) -> None:
        name = "control.sock"
        directory = self._exact_dir(name, 107)
        path, _inode = self._stale(directory, name)
        self.assertEqual(len(os.fsencode(path)), 107)
        self.assertLessEqual(len(os.fsencode(path)), 107)
        srv = UnixControlServer(directory, ct.with_ping_health(), name=name)
        self.addCleanup(srv.close)
        srv.start()
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        srv.close()
        self.assertFalse(os.path.lexists(path))
        self.assertEqual([entry for entry in os.listdir(directory) if entry.startswith(".")], [])

    def _flip_during_recheck(self, flip) -> tuple[BaseException | None, bool]:
        orig_connect = socket.socket.connect
        flipped = {"n": 0}

        def fake_connect(sock: socket.socket, addr: object) -> None:
            if flipped["n"] == 0 and addr != flip["path"]:
                flipped["n"] = 1
                flip["do"]()
            orig_connect(sock, addr)

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        srv = UnixControlServer(flip["directory"], ct.with_ping_health(), name=flip["name"])
        started = False
        exc: BaseException | None = None
        try:
            try:
                srv.start()
                started = True
            except BaseException as err:  # noqa: BLE001
                exc = err
        finally:
            socket.socket.connect = orig_connect  # type: ignore[method-assign]
            if started:
                srv.close()
            else:
                srv.close()
        return exc, started

    def test_parent_symlink_flip_during_recheck_does_not_report_success(self) -> None:
        """Flipping a parent symlink during the recheck must not look like a finished reclaim.

        The socket directory itself is real. ``link`` is a parent of that directory, so
        ``O_NOFOLLOW`` still opens it, and the connect path changes after the dir fd is held.
        """
        real_holder = os.path.join(self.base, "real_holder")
        empty_holder = os.path.join(self.base, "empty_holder")
        os.mkdir(real_holder, 0o700)
        os.mkdir(empty_holder, 0o700)
        real = os.path.join(real_holder, "run")
        empty = os.path.join(empty_holder, "run")
        os.mkdir(real, 0o700)
        os.mkdir(empty, 0o700)
        link = os.path.join(self.base, "link")
        os.symlink(real_holder, link)
        directory = os.path.join(link, "run")
        path, inode = self._stale(directory, "control.sock")
        real_path = os.path.join(real, "control.sock")
        self.assertEqual(os.lstat(real_path).st_ino, inode)

        def do_flip() -> None:
            os.unlink(link)
            os.symlink(empty_holder, link)

        exc, started = self._flip_during_recheck(
            {"path": path, "directory": directory, "name": "control.sock", "do": do_flip}
        )
        self.assertFalse(started, (exc, os.listdir(real), os.listdir(empty)))
        self.assertIsInstance(exc, ct.InsecureSocketPath, exc)
        restored = os.lstat(real_path)
        self.assertEqual(restored.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(restored.st_mode))
        self.assertFalse(os.path.lexists(os.path.join(empty, "control.sock")))
        self.assertEqual([name for name in os.listdir(real) if name.startswith(".")], [])

    def test_directory_replacement_during_recheck_does_not_report_success(self) -> None:
        """A replaced parent makes a path connect return ENOENT while the dir fd still holds the socket.

        The socket name is long enough that ``/proc/self/fd/<dirfd>/<name>`` does not fit in
        ``sun_path``, so the recheck has to notice the parent inode changed.
        """
        parent = tempfile.mkdtemp(prefix="p", dir="/tmp")
        self.addCleanup(self._rm_tree, parent)
        name = "n" * (107 - len(parent) - 1)
        directory = parent
        path, inode = self._stale(directory, name)
        self.assertEqual(len(os.fsencode(path)), 107)
        self.assertGreater(len(os.fsencode(f"/proc/self/fd/0/{name}")), 107)
        old = directory + "_old"

        def do_replace() -> None:
            os.rename(directory, old)
            os.mkdir(directory, 0o700)

        exc, started = self._flip_during_recheck(
            {"path": path, "directory": directory, "name": name, "do": do_replace}
        )
        self.assertFalse(started, (exc, os.listdir(old) if os.path.isdir(old) else None))
        self.assertIsInstance(exc, ct.InsecureSocketPath, exc)
        restored_path = os.path.join(old, name)
        restored = os.lstat(restored_path)
        self.assertEqual(restored.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(restored.st_mode))
        self.assertFalse(any(stat.S_ISSOCK(os.lstat(os.path.join(directory, entry)).st_mode) for entry in os.listdir(directory)))
        self.assertEqual([entry for entry in os.listdir(old) if entry.startswith(".")], [])

    def test_quarantine_name_race_replaces_no_listener(self) -> None:
        """Listeners planted on the quarantine name across the move are not replaced."""
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stats = {"lost": 0, "armed": 0}
        orig_rename = unix.os.rename
        orig_rn = unix._rename_noreplace
        for _ in range(40):
            if os.path.lexists(path):
                os.unlink(path)
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()
            planted: dict = {}

            def plant(dst: object, dfd: int | None, planted: dict = planted) -> None:
                if planted or not isinstance(dst, str) or not dst.startswith("."):
                    return
                full = _at_path(dst, dfd)
                if os.path.lexists(full):
                    return
                live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    live.bind(full)
                    live.listen(1)
                except OSError:
                    live.close()
                    return
                planted["sock"] = live
                planted["path"] = full
                planted["ino"] = os.lstat(full).st_ino

            def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
                plant(dst, kwargs.get("dst_dir_fd"))  # type: ignore[arg-type]
                return orig_rename(src, dst, *args, **kwargs)

            def fake_rn(dfd: int, src: str, dst: str):
                plant(dst, dfd)
                return orig_rn(dfd, src, dst)

            unix.os.rename = fake_rename  # type: ignore[assignment]
            unix._rename_noreplace = fake_rn  # type: ignore[assignment]
            try:
                try:
                    unix._clear_stale_socket(path)
                except (OSError, ct.InsecureSocketPath):
                    pass
            finally:
                unix.os.rename = orig_rename  # type: ignore[assignment]
                unix._rename_noreplace = orig_rn  # type: ignore[assignment]
            if "sock" not in planted:
                continue
            stats["armed"] += 1
            try:
                listed = os.lstat(planted["path"])
                kept = stat.S_ISSOCK(listed.st_mode) and listed.st_ino == planted["ino"]
            except OSError:
                kept = False
            if kept:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.settimeout(0.2)
                    planted["sock"].settimeout(0.2)
                    client.connect(planted["path"])
                    conn, _ = planted["sock"].accept()
                    conn.close()
                except OSError:
                    kept = False
                finally:
                    client.close()
            planted["sock"].close()
            if not kept:
                stats["lost"] += 1
        self.assertGreaterEqual(stats["armed"], 30, stats)
        self.assertEqual(stats["lost"], 0, stats)

    def test_without_renameat2_a_refused_socket_stays_in_place(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        path, inode = self._stale(self.sock_dir)
        orig = unix._load_renameat2
        unix._load_renameat2 = lambda: None  # type: ignore[method-assign]
        self.addCleanup(setattr, unix, "_load_renameat2", orig)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            srv.start()
        self.assertIn("Left in place", str(caught.exception))
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, inode)
        self.assertEqual([name for name in os.listdir(self.sock_dir) if name.startswith(".")], [])

    def _assert_refused(self, path: str) -> None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.settimeout(0.2)
        with self.assertRaises(OSError) as caught:
            probe.connect(path)
        self.assertEqual(caught.exception.errno, errno.ECONNREFUSED)

    def test_symlink_swap_during_connect_does_not_unlink_a_live_listener(self) -> None:
        """A final-component symlink undone inside connect must not look like ECONNREFUSED.

        Failed on a4c6d121: the recheck connected to ``/proc/self/fd/<dirfd>/<qname>``,
        followed the symlink to a dead socket, and ``start`` unlinked the live inode
        after the symlink was removed.
        """
        os.mkdir(self.sock_dir, 0o700)
        pub = "control.sock"
        prefix = _q_prefix(pub)
        self.assertEqual(prefix, ".q0f9a")
        qname = prefix + "abcdef"
        self.assertEqual(len(qname), len(pub))
        live, live_path, live_ino = self._listen(self.sock_dir, qname)
        public = os.path.join(self.sock_dir, pub)
        dead_path = os.path.join(self.base, "dead.sock")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(dead.close)
        dead.bind(dead_path)
        dead.close()
        swaps = {"n": 0}
        orig = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            path = _resolved_connect_path(addr)
            aside = None
            if (
                path
                and path != public
                and path != dead_path
                and os.path.lexists(path)
                and not os.path.islink(path)
                and stat.S_ISSOCK(os.lstat(path).st_mode)
            ):
                aside = path + ".aside"
                os.rename(path, aside)
                os.symlink(dead_path, path)
                swaps["n"] += 1
            try:
                orig(sock, addr)
            finally:
                if aside is not None:
                    if os.path.islink(path):
                        os.unlink(path)
                    if os.path.lexists(aside) and not os.path.lexists(path):
                        os.rename(aside, path)

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health(), name=pub)
        self.addCleanup(srv.close)
        try:
            srv.start()
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        self.assertGreaterEqual(swaps["n"], 1)
        listed = os.lstat(live_path)
        self.assertEqual(listed.st_ino, live_ino)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertFalse(stat.S_ISLNK(listed.st_mode))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        live.settimeout(2)
        client.connect(live_path)
        conn, _ = live.accept()
        conn.close()
        self.assertFalse(any(os.path.islink(os.path.join(self.sock_dir, name)) for name in os.listdir(self.sock_dir)))

    def test_directory_round_trip_during_connect_does_not_unlink_a_live_listener(self) -> None:
        """A directory rename undone inside connect must not look like ECONNREFUSED.

        The socket name is long enough that ``/proc/self/fd/<dirfd>/<name>`` does not
        fit in ``sun_path``. Failed on a4c6d121: the recheck connected to the
        directory path, which a round-trip pointed at a dead socket, then ``start``
        unlinked the live inode after the directory was put back.
        """
        directory = tempfile.mkdtemp(prefix="d", dir="/tmp")
        away = directory + "_away"
        self.addCleanup(self._rm_tree, directory)
        self.addCleanup(lambda: self._rm_tree(away) if os.path.isdir(away) else None)
        os.chmod(directory, 0o700)
        name_len = 107 - len(directory) - 1
        self.assertGreaterEqual(name_len, 92, directory)
        pub = "p" * name_len
        prefix = _q_prefix(pub)
        qname = (prefix + ("ab" * name_len))[:name_len]
        self.assertEqual(len(qname), name_len)
        self.assertTrue(qname.startswith(prefix))
        self.assertTrue(all(ch in "0123456789abcdef" for ch in qname[len(prefix):]))
        self.assertGreater(len(f"/proc/self/fd/0/{qname}"), 107)
        self.assertEqual(len(os.path.join(directory, pub)), 107)
        live, live_path, live_ino = self._listen(directory, qname)
        public = os.path.join(directory, pub)
        moves = {"n": 0}
        orig = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            path = _resolved_connect_path(addr)
            moved = False
            base = None
            if (
                path
                and path != public
                and os.path.dirname(path) == directory
                and os.path.basename(path) not in (pub, os.path.basename(pub) + ".lock")
            ):
                base = os.path.basename(path)
                os.rename(directory, away)
                moved = True
                os.mkdir(directory, 0o700)
                planted = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    planted.bind(os.path.join(directory, base))
                finally:
                    planted.close()
                moves["n"] += 1
            try:
                orig(sock, addr)
            finally:
                if moved and base is not None:
                    planted_path = os.path.join(directory, base)
                    if os.path.lexists(planted_path):
                        os.unlink(planted_path)
                    if os.path.isdir(directory):
                        os.rmdir(directory)
                    if not os.path.exists(directory) and os.path.isdir(away):
                        os.rename(away, directory)

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig)
        srv = UnixControlServer(directory, ct.with_ping_health(), name=pub)
        self.addCleanup(srv.close)
        try:
            srv.start()
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        self.assertGreaterEqual(moves["n"], 1)
        listed = os.lstat(live_path)
        self.assertEqual(listed.st_ino, live_ino)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        live.settimeout(2)
        client.connect(live_path)
        conn, _ = live.accept()
        conn.close()

    def test_refused_recheck_does_not_unlink_a_different_inode(self) -> None:
        """ECONNREFUSED on the verified inode must not unlink a socket planted at the name.

        The hook points the name at a dead socket for the real connect, then leaves a
        live listener there. Failed on a4c6d121: ``start`` unlinked that listener.
        """
        os.mkdir(self.sock_dir, 0o700)
        public, _inode = self._stale(self.sock_dir)
        dead_path = os.path.join(self.base, "dead.sock")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(dead.close)
        dead.bind(dead_path)
        dead.close()
        planted: dict = {}
        orig = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            path = _resolved_connect_path(addr)
            aside = None
            if path and path != public and path != dead_path and os.path.lexists(path) and not os.path.islink(path):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    aside = path + ".aside"
                    os.rename(path, aside)
                    os.symlink(dead_path, path)
            try:
                orig(sock, addr)
            finally:
                if aside is not None:
                    if os.path.islink(path):
                        os.unlink(path)
                    if path is not None and os.path.lexists(aside):
                        os.unlink(aside)
                    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.addCleanup(live.close)
                    live.bind(path)
                    live.listen(1)
                    planted["sock"] = live
                    planted["ino"] = os.lstat(path).st_ino

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        started = False
        exc: BaseException | None = None
        try:
            try:
                srv.start()
                started = True
            except BaseException as err:  # noqa: BLE001 - the assertion names the type
                exc = err
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        self.assertIn("ino", planted)
        linked = _linked_socket(self.sock_dir, planted["ino"])
        self.assertIsNotNone(linked, (started, exc, os.listdir(self.sock_dir)))
        self.assertFalse(started)
        self.assertIsInstance(exc, ct.InsecureSocketPath)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        planted["sock"].settimeout(2)
        client.connect(linked)
        conn, _ = planted["sock"].accept()
        conn.close()

    def test_reap_keeps_dead_sockets_the_generator_cannot_name(self) -> None:
        """Reap only names ``_quarantine_name`` can produce.

        Failed on a4c6d121: a dead ``.q0f9a`` (6 bytes) and ``.q0f9az`` (7 bytes)
        were unlinked, as was any owned refused socket whose name started with
        the prefix and was no longer than the public name.
        """
        os.mkdir(self.sock_dir, 0o700)
        pub = "control.sock"
        prefix = _q_prefix(pub)
        self.assertEqual(prefix, ".q0f9a")
        self.assertEqual(len(prefix), 6)
        kept = {
            prefix: None,  # .q0f9a
            prefix + "z": None,  # .q0f9az
            prefix + "abcdez": None,
            prefix + "ABCDEF": None,
            prefix + "abcdefx": None,
        }
        self.assertEqual(len(prefix + "abcdez"), len(pub))
        self.assertEqual(len(prefix + "ABCDEF"), len(pub))
        self.assertGreater(len(prefix + "abcdefx"), len(pub))
        removed = prefix + "abcdef"
        self.assertEqual(len(removed), len(pub))
        self.assertTrue(all(ch in "0123456789abcdef" for ch in removed[len(prefix):]))
        inodes = {}
        for name in list(kept) + [removed]:
            path, inode = self._stale(self.sock_dir, name)
            inodes[name] = inode
            self._assert_refused(path)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health(), name=pub)
        self.addCleanup(srv.close)
        srv.start()
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        self.assertNotIn(removed, os.listdir(self.sock_dir))
        for name, inode in inodes.items():
            if name == removed:
                continue
            path = os.path.join(self.sock_dir, name)
            listed = os.lstat(path)
            self.assertEqual(listed.st_ino, inode, name)
            self.assertTrue(stat.S_ISSOCK(listed.st_mode), name)
            self._assert_refused(path)

    def _listen(self, directory: str, name: str) -> tuple[socket.socket, str, int]:
        path = os.path.join(directory, name)
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(live.close)
        live.bind(path)
        live.listen(1)
        return live, path, os.lstat(path).st_ino


def _fd_target_counts() -> dict[str, int]:
    """How many open fds in this process resolve to each path."""
    counts: dict[str, int] = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        counts[target] = counts.get(target, 0) + 1
    return counts


def _bind_dead_named(directory: str, names: list[str]) -> None:
    """Bind and close a socket at each ``directory/name``. The name stays, nothing listens."""
    for name in names:
        path = os.path.join(directory, name)
        if os.path.lexists(path):
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
        finally:
            sock.close()


def _proc_inode_fd(addr: object) -> str | None:
    """Fd number when ``addr`` is exactly ``/proc/self/fd/<digits>``."""
    if not isinstance(addr, str) or not addr.startswith("/proc/self/fd/"):
        return None
    rest = addr[len("/proc/self/fd/"):]
    if rest.isdigit():
        return rest
    return None


@unittest.skipUnless(LINUX, "Linux is the tested path")
class ProcFsGuardTests(_TmpCase):
    """``/proc/self/fd`` is trusted only when it is this process's procfs.

    Each case failed on 57bacd18: a directory at that path was enough, so a
    tmpfs stand-in full of dead sockets made the recheck return
    ``ECONNREFUSED`` and ``start`` unlinked a live listener.
    A symlink to a different socket, and another pid's fd directory mounted
    on this one, failed on 881a32e2: procfs magic and a symlink entry were
    enough, and ``start`` replaced the live listener.
    """

    def _patch(self, name: str, value: object) -> None:
        missing = object()
        orig = getattr(unix, name, missing)
        setattr(unix, name, value)

        def restore() -> None:
            if orig is missing:
                try:
                    delattr(unix, name)
                except AttributeError:
                    pass
            else:
                setattr(unix, name, orig)

        self.addCleanup(restore)

    def _stale(self, name: str = "control.sock") -> tuple[str, int]:
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, name)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        return path, os.lstat(path).st_ino

    def _expect_left(self, path: str, inode: int) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health(), name=os.path.basename(path))
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            srv.start()
        self.assertIn("Left in place", str(caught.exception))
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertFalse(stat.S_ISLNK(listed.st_mode))
        self.assertEqual([entry for entry in os.listdir(self.sock_dir) if entry.startswith(".")], [])
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(probe.close)
        probe.settimeout(0.2)
        with self.assertRaises(OSError) as refused:
            probe.connect(path)
        self.assertEqual(refused.exception.errno, errno.ECONNREFUSED)

    def _plant_fd_sockets(self, directory: str) -> None:
        os.mkdir(directory, 0o700)
        _bind_dead_named(directory, [str(n) for n in range(256)])

    def test_non_procfs_fd_directory_does_not_unlink_a_live_listener(self) -> None:
        """A tmpfs stand-in for ``/proc/self/fd`` must not prove a listener dead.

        The path connect is swapped to a dead socket for the duration of
        ``connect``, which is what made the public name look like
        ``ECONNREFUSED``. Dead sockets named by fd number sit in the
        stand-in directory. On 57bacd18 the recheck connected to
        ``/proc/self/fd/<fd>`` (hooked here onto that stand-in), ``start``
        returned, and the live inode was gone.
        """
        os.mkdir(self.sock_dir, 0o700)
        public = os.path.join(self.sock_dir, "control.sock")
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(live.close)
        live.bind(public)
        live.listen(1)
        live_ino = os.lstat(public).st_ino
        fake = os.path.join(self.base, "procfd")
        self._plant_fd_sockets(fake)
        dead_path = os.path.join(self.base, "dead.sock")
        _bind_dead_named(self.base, ["dead.sock"])
        self._patch("_proc_fd_dir", lambda: fake)
        swaps = {"n": 0}
        orig = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            planted = _proc_inode_fd(addr)
            if planted is not None:
                path = os.path.join(fake, planted)
                if not os.path.lexists(path):
                    _bind_dead_named(fake, [planted])
                orig(sock, path)
                return
            aside = None
            if addr == public and os.path.lexists(public) and not os.path.islink(public):
                aside = public + ".aside"
                os.rename(public, aside)
                os.symlink(dead_path, public)
                swaps["n"] += 1
            try:
                orig(sock, addr)
            finally:
                if aside is not None and addr == public:
                    if os.path.islink(public):
                        os.unlink(public)
                    if os.path.lexists(aside) and not os.path.lexists(public):
                        os.rename(aside, public)

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig)
        before = _fd_target_counts()
        pins = set(unix._PIN_FDS)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        started = False
        exc: BaseException | None = None
        try:
            try:
                srv.start()
                started = True
            except BaseException as err:  # noqa: BLE001 - the assertion names the type
                exc = err
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        self.assertGreaterEqual(swaps["n"], 1)
        self.assertFalse(started, (exc, os.listdir(self.sock_dir)))
        self.assertIsInstance(exc, ct.InsecureSocketPath)
        self.assertIn("Left in place", str(exc))
        self.assertIsNone(srv._sock)
        self.assertEqual(_fd_target_counts(), before)
        self.assertEqual(set(unix._PIN_FDS), pins)
        listed = os.lstat(public)
        self.assertEqual(listed.st_ino, live_ino)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertFalse(stat.S_ISLNK(listed.st_mode))
        self.assertEqual([entry for entry in os.listdir(self.sock_dir) if entry.startswith(".")], [])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        live.settimeout(2)
        client.connect(public)
        conn, _ = live.accept()
        conn.close()

    def test_wrong_proc_magic_leaves_the_socket_in_place(self) -> None:
        """``fstatfs`` reporting anything but procfs leaves the socket in place.

        Failed on 57bacd18: there was no magic check, so a stale socket was
        reclaimed while the helper returned ``TMPFS_MAGIC``.
        """
        path, inode = self._stale()
        self._patch("_fstatfs_type", lambda _fd: 0x01021994)
        self._expect_left(path, inode)

    def test_proc_statfs_error_leaves_the_socket_in_place(self) -> None:
        """``fstatfs`` failing, or libc having no ``fstatfs``, leaves the socket in place.

        Failed on 57bacd18: the recheck did not call ``fstatfs``, so both
        failures were ignored and the stale socket was removed.
        """
        path, inode = self._stale()
        real = getattr(unix, "_fstatfs_type", None)

        def boom(_fd: int) -> int:
            raise OSError(errno.EIO, "fstatfs failed")

        self._patch("_fstatfs_type", boom)
        self._expect_left(path, inode)
        if real is not None:
            unix._fstatfs_type = real  # type: ignore[method-assign]
        self._patch("_load_fstatfs", lambda: None)
        self._expect_left(path, inode)

    def test_procfs_check_failure_does_not_leak_fds(self) -> None:
        """A refused procfs check closes the ``O_PATH`` directory fd it opened.

        Failed on 57bacd18: ``start`` reclaimed the socket instead of raising,
        so this loop did not stay on the failure path.
        """
        path, inode = self._stale()
        fake = os.path.join(self.base, "procfd")
        self._plant_fd_sockets(fake)
        real = getattr(unix, "_fstatfs_type", None)
        before = _fd_target_counts()
        pins = set(unix._PIN_FDS)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)

        def once() -> None:
            with self.assertRaises(ct.InsecureSocketPath) as caught:
                srv.start()
            self.assertIn("Left in place", str(caught.exception))
            self.assertEqual(os.lstat(path).st_ino, inode)
            self.assertEqual(_fd_target_counts(), before)
            self.assertEqual(set(unix._PIN_FDS), pins)

        self._patch("_fstatfs_type", lambda _fd: 0x01021994)
        for _ in range(8):
            once()

        def boom(_fd: int) -> int:
            raise OSError(errno.EIO, "fstatfs failed")

        unix._fstatfs_type = boom  # type: ignore[method-assign]
        for _ in range(8):
            once()
        if real is not None:
            unix._fstatfs_type = real  # type: ignore[method-assign]
        self._patch("_proc_fd_dir", lambda: fake)
        for _ in range(8):
            once()

    def test_fd_symlink_to_another_socket_does_not_unlink_a_live_listener(self) -> None:
        """A proc stand-in whose entry names another socket must not unlink.

        No mount: ``_proc_fd_dir`` is a directory of symlinks to a second
        bound socket, and ``fstatfs`` is reported as procfs so the magic
        check is not what refuses. Failed on 881a32e2: that symlink was
        enough, connect returned ``ECONNREFUSED``, and ``start`` replaced
        the live listener.
        """
        os.mkdir(self.sock_dir, 0o700)
        public = os.path.join(self.sock_dir, "control.sock")
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(live.close)
        live.bind(public)
        live.listen(1)
        live_ino = os.lstat(public).st_ino
        dead_path = os.path.join(self.base, "dead.sock")
        _bind_dead_named(self.base, ["dead.sock"])
        self.assertNotEqual(os.lstat(dead_path).st_ino, live_ino)
        fake = os.path.join(self.base, "procfd")
        os.mkdir(fake, 0o700)
        highest = 64
        for name in os.listdir("/proc/self/fd"):
            if name.isdigit():
                highest = max(highest, int(name))
        for number in range(highest + 128):
            os.symlink(dead_path, os.path.join(fake, str(number)))
        self._patch("_proc_fd_dir", lambda: fake)
        self._patch("_fstatfs_type", lambda _fd: unix._PROC_SUPER_MAGIC)
        swaps = {"n": 0}
        orig = socket.socket.connect

        def fake_connect(sock: socket.socket, addr: object) -> None:
            aside = None
            if addr == public and os.path.lexists(public) and not os.path.islink(public):
                aside = public + ".aside"
                os.rename(public, aside)
                os.symlink(dead_path, public)
                swaps["n"] += 1
            try:
                orig(sock, addr)
            finally:
                if aside is not None and addr == public:
                    if os.path.islink(public):
                        os.unlink(public)
                    if os.path.lexists(aside) and not os.path.lexists(public):
                        os.rename(aside, public)

        socket.socket.connect = fake_connect  # type: ignore[method-assign]
        self.addCleanup(setattr, socket.socket, "connect", orig)
        before = _fd_target_counts()
        pins = set(unix._PIN_FDS)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        started = False
        exc: BaseException | None = None
        try:
            try:
                srv.start()
                started = True
            except BaseException as err:  # noqa: BLE001 - the assertion names the type
                exc = err
        finally:
            socket.socket.connect = orig  # type: ignore[method-assign]
        self.assertGreaterEqual(swaps["n"], 1)
        self.assertFalse(started, (exc, os.listdir(self.sock_dir)))
        self.assertIsInstance(exc, ct.InsecureSocketPath)
        self.assertIn("Left in place", str(exc))
        self.assertIsNone(srv._sock)
        self.assertEqual(_fd_target_counts(), before)
        self.assertEqual(set(unix._PIN_FDS), pins)
        listed = os.lstat(public)
        self.assertEqual(listed.st_ino, live_ino)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertFalse(stat.S_ISLNK(listed.st_mode))
        self.assertEqual([entry for entry in os.listdir(self.sock_dir) if entry.startswith(".")], [])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.settimeout(2)
        live.settimeout(2)
        client.connect(public)
        conn, _ = live.accept()
        conn.close()

    def test_proc_identity_emfile_propagates_and_leaves_the_socket(self) -> None:
        """``EMFILE`` while identifying the fd directory is not a stale socket.

        Failed on 881a32e2: nothing opened ``..`` from the proc directory, so
        this ``os.open`` error never fired and ``start`` removed the socket.
        """
        path, inode = self._stale()
        real_open = os.open

        def fail_parent(file: object, flags: int, *args: object, **kwargs: object) -> int:
            if file == "..":
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(file, flags, *args, **kwargs)  # type: ignore[arg-type]

        os.open = fail_parent  # type: ignore[method-assign]
        self.addCleanup(setattr, os, "open", real_open)
        before = _fd_target_counts()
        pins = set(unix._PIN_FDS)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        self.addCleanup(srv.close)
        try:
            with self.assertRaises(OSError) as caught:
                srv.start()
            self.assertEqual(caught.exception.errno, errno.EMFILE)
        finally:
            os.open = real_open  # type: ignore[method-assign]
        self.assertIsNone(srv._sock)
        self.assertEqual(_fd_target_counts(), before)
        self.assertEqual(set(unix._PIN_FDS), pins)
        listed = os.lstat(path)
        self.assertEqual(listed.st_ino, inode)
        self.assertTrue(stat.S_ISSOCK(listed.st_mode))
        self.assertEqual([entry for entry in os.listdir(self.sock_dir) if entry.startswith(".")], [])

    def test_mounted_foreign_fd_directory_does_not_unlink_a_live_listener(self) -> None:
        """Another pid's procfs fd directory mounted on ours must not unlink.

        ``unshare -Ur -m`` bind-mounts a helper's ``/proc/<pid>/fd`` onto
        this process's fd directory. Magic, the path and a procfs symlink
        all pass, and ``..`` is still this pid. Failed on 881a32e2:
        connect hit the helper's socket and ``start`` replaced the listener.
        Skipped when user namespaces are unavailable.
        """
        binary = _unshare_user_mount()
        if binary is None:
            self.skipTest("unshare -Ur -m unavailable")
        env = dict(os.environ)
        core_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        previous = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = core_root + (os.pathsep + previous if previous else "")
        result = subprocess.run(
            [binary, "-Ur", "-m", sys.executable, "-B", "-c", _FOREIGN_FD_MOUNT],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            "stdout:\n" + result.stdout + "\nstderr:\n" + result.stderr,
        )
        self.assertIn("Left in place", result.stdout)
        self.assertIn("intact", result.stdout)


def _unshare_user_mount() -> str | None:
    """``unshare`` when ``unshare -Ur -m`` runs, else None."""
    binary = shutil.which("unshare")
    if not binary:
        return None
    try:
        probe = subprocess.run(
            [binary, "-Ur", "-m", "true"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    return binary


_FOREIGN_FD_MOUNT = textwrap.dedent(
    r"""
    import ctypes
    import os
    import signal
    import socket
    import stat
    import sys
    import tempfile

    from recursion_core import control_transport as ct
    from recursion_core.control_transport import unix
    from recursion_core.control_transport.unix import UnixControlServer

    def main() -> int:
        base = tempfile.mkdtemp(prefix="ct-foreign-fd-", dir="/tmp")
        os.chmod(base, 0o700)
        sock_dir = os.path.join(base, "run")
        os.mkdir(sock_dir, 0o700)
        public = os.path.join(sock_dir, "control.sock")
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(public)
        live.listen(1)
        live_ino = os.lstat(public).st_ino
        dead_path = os.path.join(base, "dead.sock")
        ready_r, ready_w = os.pipe()
        helper = os.fork()
        if helper == 0:
            os.close(ready_r)
            held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            held.bind(dead_path)
            covered = 0
            for number in range(3, 1024):
                if number in (held.fileno(), ready_w):
                    continue
                try:
                    os.dup2(held.fileno(), number)
                except OSError:
                    break
                covered += 1
            os.write(ready_w, b"1" if covered >= 64 else b"0")
            if covered < 64:
                os.close(ready_w)
                os._exit(2)
            # The pipe fd must also name the dead socket. Stop only after that.
            os.dup2(held.fileno(), ready_w)
            os.kill(os.getpid(), signal.SIGSTOP)
            os._exit(0)
        os.close(ready_w)
        try:
            got = os.read(ready_r, 1)
            os.close(ready_r)
            if got != b"1":
                print("helper did not cover fds")
                return 2
            _pid, status = os.waitpid(helper, os.WUNTRACED)
            if not os.WIFSTOPPED(status):
                print("helper was not stopped", status)
                return 2
            foreign = "/proc/%d/fd/10" % helper
            link = os.readlink(foreign)
            if not link.startswith("socket:"):
                print("helper fd is", link)
                return 2
            self_fd = "/proc/%d/fd" % os.getpid()
            libc = ctypes.CDLL(None, use_errno=True)
            libc.mount.argtypes = [
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            libc.mount.restype = ctypes.c_int
            rc = libc.mount(("/proc/%d/fd" % helper).encode(), self_fd.encode(), None, 4096, None)
            if rc != 0:
                err = ctypes.get_errno()
                print("mount failed", err, os.strerror(err))
                return 2
            orig = socket.socket.connect
            swaps = {"n": 0}

            def fake_connect(sock, addr):
                aside = None
                if addr == public and os.path.lexists(public) and not os.path.islink(public):
                    aside = public + ".aside"
                    os.rename(public, aside)
                    os.symlink(dead_path, public)
                    swaps["n"] += 1
                try:
                    return orig(sock, addr)
                finally:
                    if aside is not None and addr == public:
                        if os.path.islink(public):
                            os.unlink(public)
                        if os.path.lexists(aside) and not os.path.lexists(public):
                            os.rename(aside, public)

            socket.socket.connect = fake_connect
            pins = set(unix._PIN_FDS)
            srv = UnixControlServer(sock_dir, ct.with_ping_health())
            started = False
            exc = None
            try:
                try:
                    srv.start()
                    started = True
                except BaseException as err:
                    exc = err
            finally:
                socket.socket.connect = orig
            print("swaps", swaps["n"])
            print("started", started)
            print("exc", type(exc).__name__ if exc else None, exc)
            print("pins", set(unix._PIN_FDS) == pins, sorted(unix._PIN_FDS))
            listed = os.lstat(public)
            same = (
                listed.st_ino == live_ino
                and stat.S_ISSOCK(listed.st_mode)
                and not stat.S_ISLNK(listed.st_mode)
            )
            hidden = [entry for entry in os.listdir(sock_dir) if entry.startswith(".")]
            print("same", same, "hidden", hidden)
            ok = (
                swaps["n"] >= 1
                and not started
                and exc is not None
                and type(exc) is ct.InsecureSocketPath
                and "Left in place" in str(exc)
                and srv._sock is None
                and set(unix._PIN_FDS) == pins
                and same
                and hidden == []
            )
            if ok:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2)
                live.settimeout(2)
                client.connect(public)
                conn, _ = live.accept()
                conn.close()
                client.close()
                print("intact")
            srv.close()
            live.close()
            return 0 if ok else 1
        finally:
            try:
                os.kill(helper, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(helper, 0)
            except OSError:
                pass

    sys.exit(main())
    """
)


if __name__ == "__main__":
    unittest.main()
