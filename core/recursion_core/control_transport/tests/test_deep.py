"""Defects past the close-deadline and reply-correlation work (CSL-719).

Each case failed on 7cfe77d8 before the fix in this file's sibling modules:
permission swaps around bind, a mode-000 stale socket, lock-fd leaks (failed
socket() and fork), a handler reply that cannot be encoded, a local encode
error closing a healthy client, and close() that cannot cancel.
"""

from __future__ import annotations

import errno
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

    def test_mode_000_stale_socket_is_replaced(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()
        os.chmod(path, 0)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        self.addCleanup(srv.close)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")

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


if __name__ == "__main__":
    unittest.main()
