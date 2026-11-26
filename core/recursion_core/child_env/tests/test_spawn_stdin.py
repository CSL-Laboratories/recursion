"""stdin at the child-launch boundary.

A caller that omits ``stdin`` must not hand the child the caller's own standard input.
An explicit ``stdin=subprocess.PIPE`` still delivers bytes. The refused-option list stays
the set ``launch._FORBIDDEN`` already has, plus ``start_new_session`` other than ``True``.

The omitted-stdin case points this process's fd 0 at a pipe it created. A runner whose
own fd 0 is already ``/dev/null`` would otherwise make an inheriting child look correct.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from unittest import mock

from recursion_core.child_env import EnvRefused, build_child_env, spawn
from recursion_core.child_env import launch

# The refused names on the base this test was written against. stdin is not one of them.
_REFUSED = frozenset({
    "env", "shell", "executable", "preexec_fn", "close_fds", "pass_fds",
    "user", "group", "extra_groups", "process_group",
})

_READLINK = "import os,sys; sys.stdout.write(os.readlink('/proc/self/fd/0'))"


@contextlib.contextmanager
def _pipe_on_fd0() -> Iterator[str]:
    """Point fd 0 at a pipe this test created, and put the previous fd 0 back."""

    read_fd, write_fd = os.pipe()
    saved = os.dup(0)
    try:
        os.dup2(read_fd, 0)
        yield os.readlink("/proc/self/fd/0")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_fd)
        os.close(write_fd)


def _communicate(proc: subprocess.Popen, data: bytes | None = None) -> tuple[bytes, bytes]:
    try:
        return proc.communicate(data, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise


@unittest.skipIf(os.name == "nt", "POSIX spawn; this host refuses platform_unsupported before a child")
class SpawnStdinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        os.chmod(self.home, 0o700)
        bin_dir = os.path.join(self.home, "bin")
        os.mkdir(bin_dir, 0o700)
        self.env = build_child_env(
            "codex", home=self.home, auth_mode="subscription",
            parent={"PATH": bin_dir, "LANG": "C.UTF-8"},
        )

    def _linux_fd0(self) -> None:
        if not os.path.exists("/proc/self/fd/0"):
            self.skipTest("fd 0 readlink is checked on Linux proc only")

    def test_omitted_stdin_child_fd0_is_devnull(self) -> None:
        self._linux_fd0()
        with _pipe_on_fd0() as parent_fd0:
            self.assertNotEqual("/dev/null", parent_fd0)
            proc = spawn(
                [sys.executable, "-c", _READLINK], self.env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = _communicate(proc)
        self.assertEqual(0, proc.returncode, err)
        self.assertEqual("/dev/null", out.decode())

    def test_explicit_pipe_still_delivers_what_the_parent_writes(self) -> None:
        code = (
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "link = b''\n"
            "if sys.platform.startswith('linux'):\n"
            "    import os\n"
            "    link = os.readlink('/proc/self/fd/0').encode()\n"
            "sys.stdout.buffer.write(link + b'\\n' + data)\n"
        )
        proc = spawn(
            [sys.executable, "-c", code], self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out, err = _communicate(proc, b"still-open\n")
        self.assertEqual(0, proc.returncode, err)
        link, _, payload = out.partition(b"\n")
        self.assertEqual(b"still-open\n", payload)
        if os.path.exists("/proc/self/fd/0"):
            self.assertNotEqual(b"/dev/null", link)
            self.assertTrue(link.startswith(b"pipe:"), link)

    def test_refused_option_list_is_unchanged(self) -> None:
        self.assertEqual(_REFUSED, launch._FORBIDDEN)
        self.assertNotIn("stdin", launch._FORBIDDEN)

        def popen(*_args: object, **_kwargs: object) -> subprocess.Popen:
            raise AssertionError("popen was called")

        for key in sorted(_REFUSED - {"env"}):
            with self.subTest(key=key):
                with self.assertRaises(EnvRefused) as caught:
                    spawn(["not-used"], self.env, popen=popen, **{key: None})
                self.assertTrue(caught.exception.cause.startswith("launch_option_denied:"))
                self.assertIn(key, caught.exception.cause)
        with self.subTest(key="start_new_session"):
            with self.assertRaises(EnvRefused) as caught:
                spawn(["not-used"], self.env, popen=popen, start_new_session=False)
            self.assertIn("start_new_session", caught.exception.cause)
        with self.subTest(key="env"):
            with self.assertRaises(TypeError):
                spawn(["not-used"], self.env, popen=popen, env=None)

    def test_a_raised_popen_leaves_the_next_caller_its_own_stdin(self) -> None:
        for exc in (OSError("launch failed"), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__):
                def boom(_argv: object, _exc: BaseException = exc, **_kwargs: object) -> subprocess.Popen:
                    raise _exc

                with self.assertRaises(type(exc)):
                    spawn([sys.executable, "-c", "pass"], self.env, popen=boom)
                seen: dict[str, object] = {}

                def record(_argv: object, **kwargs: object) -> subprocess.Popen:
                    seen.update(kwargs)
                    return mock.Mock(name="Popen")

                spawn([sys.executable, "-c", "pass"], self.env, popen=record)
                self.assertIs(seen.get("stdin"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
