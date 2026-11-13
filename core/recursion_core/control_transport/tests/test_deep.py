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
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import socket
import stat
import tempfile
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

        def fake_rename(src: object, dst: object, *args: object, **kwargs: object):
            full = _at_path(src, kwargs.get("src_dir_fd"))  # type: ignore[arg-type]
            result = orig_rename(src, dst, *args, **kwargs)
            if full == path and "sock" not in planted:
                plant()
            return result

        unix.os.unlink = fake_unlink  # type: ignore[assignment]
        unix.os.rename = fake_rename  # type: ignore[assignment]
        self.addCleanup(setattr, unix.os, "unlink", orig_unlink)
        self.addCleanup(setattr, unix.os, "rename", orig_rename)
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


if __name__ == "__main__":
    unittest.main()
