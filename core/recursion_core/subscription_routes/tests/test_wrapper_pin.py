"""2026-11-09 wrapper pins: Pi/Hermes/Cursor package pins, Antigravity binary pin.

Fakes only. No engine binary is executed. T3 Code has no route here.
"""

from __future__ import annotations

import hashlib
import os
import signal
import unittest
from unittest import mock

from recursion_core.subscription_routes import (
    SubscriptionRouteRefused,
    WrapperPinRefused,
    check_antigravity_route,
    check_cursor_route,
    check_route,
    guard_cursor_then_spawn,
    measure_antigravity_pin,
    measure_package_pin,
)
from recursion_core.subscription_routes import wrapper_pin
from recursion_core.subscription_routes.tests.test_subscription_routes import _Base

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class WrapperPinTests(_Base):
    def bare(self) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}

    def test_cursor_matches_as_beta_and_a_changed_file_refuses(self) -> None:
        decision = check_cursor_route(self.bare(), route_home=str(self.home), wrapper=self.wrapper,
                                      parent_env=self.parent(), operator_home=str(self.operator))
        self.assertTrue(decision.beta)
        self.assertEqual(decision.auth, "wrapper")
        self.assertEqual(dict(decision.child_env), self.bare())
        (self.install / "extra.js").write_text("extra\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_cursor_route(self.bare(), route_home=str(self.home), wrapper=self.wrapper,
                               parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "wrapper_pin_mismatch")

    def test_cursor_refuses_a_claude_subscription_token(self) -> None:
        env = self.bare()
        env["ANTHROPIC_OAUTH_TOKEN"] = "fake-oauth-token-not-a-credential"
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_cursor_then_spawn(mock.Mock(), [self.wrapper.launcher_path], env, route_home=str(self.home), wrapper=self.wrapper,
                                    parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "claude_subscription_env")

    def test_opencode_refuses_a_package_pin(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("opencode", self.profile(), self.env(), route_home=str(self.home),
                        parent_env=self.parent(), operator_home=str(self.operator), wrapper=self.wrapper)
        self.assertEqual(caught.exception.cause, "wrapper_pin_not_applicable")

    def test_pi_without_a_pin_refuses(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("pi", self.profile(), self.env(), route_home=str(self.home),
                        parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "wrapper_pin_missing")

    def test_antigravity_pin_is_version_binary_and_launcher_not_package_files(self) -> None:
        binary = self.install / "agy"
        launcher = self.install / "agy-launch"
        binary.write_text("agy-binary\n", encoding="ascii")
        launcher.write_text("agy-launch\n", encoding="ascii")
        pin = measure_antigravity_pin("1.2.3", str(binary), str(launcher))
        # A package file next to agy is not part of the pin.
        (self.install / "ignored.js").write_text("ignored\n", encoding="ascii")
        decision = check_antigravity_route(
            self.bare(), version="1.2.3", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
            route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
        )
        self.assertTrue(decision.beta)
        self.assertEqual(decision.pin_version, "1.2.3")
        self.assertEqual(decision.engine, "antigravity")
        binary.write_text("agy-binary-2\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="1.2.3", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_pin_mismatch", "binary"))
        binary.write_text("agy-binary\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="9.9.9", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual(caught.exception.detail, "version")

    def test_measure_package_pin_changes_when_the_launcher_changes(self) -> None:
        before = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        (self.install / "launcher.js").write_text("launcher-v2\n", encoding="ascii")
        after = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        self.assertNotEqual(before.launcher_sha256, after.launcher_sha256)
        self.assertNotEqual(before.package_files, after.package_files)

    def sparse(self, name: str, size: int) -> str:
        # truncate leaves a hole: the file reads as zero bytes and uses no disk blocks.
        path = self.install / name
        with open(path, "wb") as handle:
            handle.truncate(size)
        return str(path)

    def small_launcher(self) -> str:
        launcher = self.install / "agy-launch"
        launcher.write_text("agy-launch\n", encoding="ascii")
        return str(launcher)

    def test_antigravity_measures_a_binary_over_the_package_file_cap(self) -> None:
        binary = self.sparse("agy", 40 * _MIB)
        pin = measure_antigravity_pin("1.5.5", binary, self.small_launcher())
        expected = hashlib.sha256()
        for _ in range(40):
            expected.update(bytes(_MIB))
        self.assertEqual(pin.binary_sha256, expected.hexdigest())
        self.assertEqual(pin.launcher_sha256, hashlib.sha256(b"agy-launch\n").hexdigest())

    def test_antigravity_refuses_a_binary_over_one_gib(self) -> None:
        binary = self.sparse("agy", _GIB + 1)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", binary, self.small_launcher())
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "binary"))

    def test_antigravity_refuses_a_launcher_over_one_gib(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-binary\n", encoding="ascii")
        launcher = self.sparse("agy-launch", _GIB + 1)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", str(binary), launcher)
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "launcher"))

    def test_package_file_over_the_old_cap_measures(self) -> None:
        self.sparse("big.bin", 40 * _MIB)
        pin = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        expected = hashlib.sha256()
        for _ in range(40):
            expected.update(bytes(_MIB))
        self.assertEqual(dict(pin.package_files)["big.bin"], expected.hexdigest())

    def test_a_self_update_of_the_pinned_binary_fails_the_pin(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-1.5.5\n", encoding="ascii")
        launcher = self.small_launcher()
        pin = measure_antigravity_pin("1.5.5", str(binary), launcher)
        # agy replaces its own bytes in place: same path, same stated version.
        with open(binary, "r+b") as handle:
            handle.write(b"agy-1.5.6\n")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="1.5.5", binary_path=str(binary), launcher_path=launcher, pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_pin_mismatch", "binary"))

    def test_unreadable_binary_refuses_without_its_path(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-binary\n", encoding="ascii")
        os.chmod(binary, 0)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", str(binary), self.small_launcher())
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_tree_unreadable", "binary"))
        self.assertNotIn(str(binary), str(caught.exception))

    def test_unreadable_binary_over_one_gib_refuses_too_large(self) -> None:
        # The cap is the file's size. Read permission is not required to know it,
        # and the absolute path must not ride out on an OSError.
        binary = self.sparse("agy", _GIB + 1)
        os.chmod(binary, 0)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", binary, self.small_launcher())
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "binary"))
        self.assertNotIn(binary, str(caught.exception))

    def test_oversize_binary_is_refused_without_reading_it(self) -> None:
        binary = self.sparse("agy", _GIB + 1)
        launcher = self.small_launcher()
        read_bytes: list[int] = []
        real_open = open

        def tracking(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if os.fspath(file) == binary:
                original = handle.read

                def read(n=-1, original=original):
                    data = original(n)
                    read_bytes.append(len(data))
                    return data

                handle.read = read
            return handle

        with mock.patch("builtins.open", tracking):
            with self.assertRaises(WrapperPinRefused) as caught:
                measure_antigravity_pin("1.5.5", binary, launcher)
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "binary"))
        self.assertEqual(read_bytes, [])

    def test_bytes_past_the_old_cap_are_in_the_pin(self) -> None:
        binary = self.install / "agy"
        size = 40 * _MIB
        tail = b"TAIL"
        with open(binary, "wb") as handle:
            handle.truncate(size)
            handle.seek(size - len(tail))
            handle.write(tail)
        pin = measure_antigravity_pin("1.5.5", str(binary), self.small_launcher())
        expected = hashlib.sha256()
        zeros = bytes(_MIB)
        remaining = size - len(tail)
        while remaining >= _MIB:
            expected.update(zeros)
            remaining -= _MIB
        if remaining:
            expected.update(bytes(remaining))
        expected.update(tail)
        self.assertEqual(pin.binary_sha256, expected.hexdigest())

    def test_unreadable_package_file_over_one_gib_refuses_too_large(self) -> None:
        path = self.sparse("big.bin", _GIB + 1)
        os.chmod(path, 0)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "big.bin"))
        self.assertNotIn(path, str(caught.exception))

    def test_hash_open_uses_nofollow_nonblock_and_cloexec(self) -> None:
        # Platforms without O_NOFOLLOW (simulated Windows) must not crash: the bit is optional.
        # Where the flag exists, the open has to carry it, plus O_NONBLOCK, O_CLOEXEC and O_NOCTTY.
        binary = self.install / "agy"
        binary.write_bytes(b"ab")
        flags_seen: list[int] = []
        real_open = wrapper_pin.os.open

        def spy(path, flags, *args, **kwargs):
            if os.fspath(path) == str(binary):
                flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(wrapper_pin.os, "open", spy):
            digest = wrapper_pin._hash_regular(str(binary), "binary")
        self.assertEqual(hashlib.sha256(b"ab").hexdigest(), digest)
        self.assertEqual([_read_flags()], flags_seen)

    def test_swapped_oversize_file_is_refused_before_any_read(self) -> None:
        # lstat sees a 2-byte regular file. Before open, the path becomes a symlink to,
        # or a hard link of, a 1 GiB+1 sparse file. The old open() follows that and reads
        # 1,073,741,825 bytes before wrapper_file_too_large.
        cases = (
            ("symlink", "wrapper_tree_unreadable"),  # O_NOFOLLOW: open fails, nothing is read
            ("hardlink", "wrapper_file_missing"),  # fstat inode is not the lstat inode
            ("grown", "wrapper_file_too_large"),  # same inode, fstat size is past the cap
        )
        for kind, cause in cases:
            with self.subTest(kind=kind):
                binary = str(self.install / f"agy-{kind}")
                if kind == "grown":
                    # The file really is 1 GiB+1. lstat is forged to report 2 bytes, same dev/ino.
                    self.sparse(f"agy-{kind}", _GIB + 1)
                    huge = binary
                    swap = None
                else:
                    with open(binary, "wb") as handle:
                        handle.write(b"ab")
                    huge = self.sparse(f"huge-{kind}", _GIB + 1)
                    swap = kind
                with self._swap_on_open(binary, huge, swap) as counter:
                    if kind == "grown":
                        self._lie_about_lstat_size(binary)
                    with self.assertRaises(WrapperPinRefused) as caught:
                        wrapper_pin._hash_regular(binary, "binary")
                self.assertEqual((caught.exception.cause, caught.exception.detail), (cause, "binary"))
                self.assertEqual(
                    0, counter["n"],
                    f"{kind}: read {counter['n']} bytes; a 1 GiB+1 file must refuse before any read",
                )
                self.assertNotIn(binary, _exception_chain_text(caught.exception))
                self.assertNotIn(huge, _exception_chain_text(caught.exception))

    def test_missing_nofollow_refuses_a_swapped_symlink_before_any_read(self) -> None:
        # getattr(os, "O_NOFOLLOW", 0) at the open, as on a platform that has no such flag.
        # The symlink is followed, and the fstat dev/ino check refuses it before the read.
        binary = str(self.install / "agy-nonofollow")
        with open(binary, "wb") as handle:
            handle.write(b"ab")
        huge = self.sparse("huge-nonofollow", _GIB + 1)
        saved = os.O_NOFOLLOW
        try:
            del os.O_NOFOLLOW
            with self._swap_on_open(binary, huge, "symlink") as counter:
                with self.assertRaises(WrapperPinRefused) as caught:
                    wrapper_pin._hash_regular(binary, "binary")
        finally:
            os.O_NOFOLLOW = saved
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_missing", "binary"))
        self.assertEqual(0, counter["n"], f"read {counter['n']} bytes with O_NOFOLLOW absent")
        self.assertNotIn(huge, _exception_chain_text(caught.exception))

    def test_fifo_swapped_in_before_open_does_not_block(self) -> None:
        binary = str(self.install / "agy-fifo")
        with open(binary, "wb") as handle:
            handle.write(b"ab")
        fired = {"yes": False}

        def handler(signum, frame):
            fired["yes"] = True
            raise TimeoutError("fifo open blocked")

        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, handler)
        try:
            signal.alarm(2)
            try:
                with self._swap_on_open(binary, binary, "fifo") as counter:
                    with self.assertRaises(WrapperPinRefused) as caught:
                        wrapper_pin._hash_regular(binary, "binary")
            finally:
                signal.alarm(0)
        finally:
            signal.signal(signal.SIGALRM, previous)
        self.assertFalse(fired["yes"], "open blocked on the FIFO until SIGALRM")
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_missing", "binary"))
        self.assertEqual(0, counter["n"])
        self.assertNotIn(binary, _exception_chain_text(caught.exception))

    def test_timeout_and_interrupt_are_not_relabeled_as_unreadable(self) -> None:
        binary = str(self.install / "agy-signal")
        with open(binary, "wb") as handle:
            handle.write(b"ab")
        for error in (TimeoutError("open timed out"), InterruptedError("open interrupted")):
            with self.subTest(error=type(error).__name__):
                def boom(*_args, **_kwargs):
                    raise error

                with mock.patch("builtins.open", boom), mock.patch.object(wrapper_pin.os, "open", boom):
                    with self.assertRaises(type(error)) as caught:
                        wrapper_pin._hash_regular(binary, "binary")
                self.assertIsInstance(caught.exception, type(error))
                self.assertNotIsInstance(caught.exception, WrapperPinRefused)

    def test_unreadable_binary_path_is_not_on_the_context_chain(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-binary\n", encoding="ascii")
        os.chmod(binary, 0)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", str(binary), self.small_launcher())
        refused = caught.exception
        self.assertEqual((refused.cause, refused.detail), ("wrapper_tree_unreadable", "binary"))
        # from None inside the handler still leaves PermissionError on __context__, filename and all.
        self.assertIsNone(refused.__cause__)
        self.assertIsNone(refused.__context__)
        self.assertNotIn(str(binary), _exception_chain_text(refused))

    def _lie_about_lstat_size(self, binary: str) -> None:
        """Make the lstat inside the hash report 2 bytes for ``binary``. The file itself is unchanged."""

        real_lstat = wrapper_pin.os.lstat

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if os.fspath(path) != binary:
                return info
            return os.stat_result((
                info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid, info.st_gid,
                2, info.st_atime, info.st_mtime, info.st_ctime,
            ))

        patcher = mock.patch.object(wrapper_pin.os, "lstat", lstat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _swap_on_open(self, binary: str, huge: str, kind: str | None):
        """After lstat, the first open of ``binary`` finds ``kind`` instead (symlink, hard link, or FIFO)."""

        counter = {"n": 0}
        state = {"done": False}
        real_open = open
        real_os_open = wrapper_pin.os.open
        real_read = wrapper_pin.os.read

        def maybe_swap(path) -> None:
            if kind is None or state["done"] or os.fspath(path) != binary:
                return
            state["done"] = True
            os.unlink(binary)
            if kind == "symlink":
                os.symlink(huge, binary)
            elif kind == "hardlink":
                os.link(huge, binary)
            elif kind == "fifo":
                os.mkfifo(binary)
            else:
                raise AssertionError(kind)

        def tracking_open(file, mode="r", *args, **kwargs):
            maybe_swap(file)
            handle = real_open(file, mode, *args, **kwargs)
            original = handle.read

            def read(n=-1, original=original):
                data = original(n)
                counter["n"] += len(data)
                return data

            handle.read = read
            return handle

        def tracking_os_open(path, flags, *args, **kwargs):
            maybe_swap(path)
            return real_os_open(path, flags, *args, **kwargs)

        def tracking_read(fd, n, *args, **kwargs):
            data = real_read(fd, n, *args, **kwargs)
            counter["n"] += len(data)
            return data

        return _Swap(counter, tracking_open, tracking_os_open, tracking_read)


class _Swap:
    """Patches open and os.read for one hash, then yields the byte counter."""

    def __init__(self, counter, tracking_open, tracking_os_open, tracking_read) -> None:
        self.counter = counter
        self._patches = (
            mock.patch("builtins.open", tracking_open),
            mock.patch.object(wrapper_pin.os, "open", tracking_os_open),
            mock.patch.object(wrapper_pin.os, "read", tracking_read),
        )

    def __enter__(self):
        for patcher in self._patches:
            patcher.start()
        return self.counter

    def __exit__(self, *exc):
        for patcher in reversed(self._patches):
            patcher.stop()
        return False


def _read_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_NOCTTY"):
        flags |= getattr(os, name, 0)
    return flags


def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(repr(current))
        filename = getattr(current, "filename", None)
        if filename is not None:
            parts.append(repr(filename))
        for link in (current.__cause__, current.__context__):
            if isinstance(link, BaseException):
                pending.append(link)
    return "\n".join(parts)


if __name__ == "__main__":
    unittest.main()
