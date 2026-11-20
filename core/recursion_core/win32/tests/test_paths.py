"""Decisions for the Windows state directory and the one-daemon lock, against a recording fake.

The package is absent on master, so importing it fails. After it exists, these cases
decide: the SDDL written for the state directory has no ACE but the current user's
(no Everyone, no Authenticated Users, no inherited ACE); a directory owned by another
SID is refused ``owner_mismatch`` and left untouched; a junction on the chain is
refused; a second lock fails ``already_running``; importing the package on POSIX
loads no Windows DLL.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

USER = "S-1-5-21-111-222-333-1001"
OTHER = "S-1-5-21-111-222-333-9999"
LOCAL = "C:\\Users\\me\\AppData\\Local"
STATE = LOCAL + "\\Recursion"
LOCK = STATE + "\\recursion-daemon.lock"
EVERYONE = {"WD", "S-1-1-0"}
AUTHENTICATED = {"AU", "S-1-5-11"}


def _dacl(sddl: str) -> tuple[str, str]:
    """Return ``(flags, body)`` of the DACL section. Independent of the product parser."""
    start = sddl.find("D:")
    if start < 0:
        raise AssertionError(sddl)
    rest = sddl[start + 2:]
    flags: list[str] = []
    index = 0
    while index < len(rest) and rest[index] != "(":
        flags.append(rest[index])
        index += 1
    body = rest[index:]
    sacl = body.find("S:")
    if sacl >= 0:
        body = body[:sacl]
    return "".join(flags), body


def _flag_pairs(flags: str) -> set[str]:
    if len(flags) % 2:
        raise AssertionError(flags)
    return {flags[i:i + 2] for i in range(0, len(flags), 2)}


def _aces(sddl: str) -> list[tuple[str, str, str, str]]:
    _flags, body = _dacl(sddl)
    found = []
    for raw in re.findall(r"\(([^)]*)\)", body):
        parts = raw.split(";")
        if len(parts) != 6:
            raise AssertionError(raw)
        found.append((parts[0], parts[1], parts[2], parts[5]))
    return found


def _assert_user_only(case: unittest.TestCase, sddl: str, sid: str) -> None:
    flags, _body = _dacl(sddl)
    leftover = flags.replace("AI", "").replace("AR", "")
    case.assertEqual("P", leftover)
    aces = _aces(sddl)
    case.assertEqual([("A", "", "FA", sid)], aces)
    for _kind, ace_flags, _rights, account in aces:
        case.assertNotIn("ID", _flag_pairs(ace_flags))
        case.assertNotIn(account.upper(), EVERYONE | AUTHENTICATED)
        case.assertNotIn(account.upper(), {"WD", "AU"})


class _InterruptAfterOpen:
    """Hands the handle back, then raises on the next read, as a cancelled caller would."""

    def __init__(self, inner: object, exc: BaseException) -> None:
        self.inner = inner
        self.exc = exc

    def open_exclusive(self, path: str, sddl: str) -> object:
        return self.inner.open_exclusive(path, sddl)

    def describe(self, handle: object) -> object:
        raise self.exc

    def release_handle(self, handle: object) -> None:
        self.inner.release_handle(handle)

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


class StateDirectoryTests(unittest.TestCase):
    def test_sddl_has_no_ace_but_the_users(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import ensure_state_dir, protected_sddl

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        path = ensure_state_dir(fake)
        self.assertEqual(STATE, path)
        created = [call for call in fake.calls if call[0] == "create_directory"]
        self.assertEqual(1, len(created))
        self.assertEqual(STATE, created[0][1])
        sddl = created[0][2]
        self.assertEqual(protected_sddl(USER), sddl)
        _assert_user_only(self, sddl, USER)
        stored = fake.security(STATE)
        self.assertEqual(sddl, stored.dacl)
        self.assertEqual(USER, stored.owner)
        self.assertFalse(stored.is_reparse)

    def test_owner_mismatch_is_refused_without_a_rewrite(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import Win32Refused, ensure_state_dir

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        planted = f"O:{OTHER}G:{OTHER}D:P(A;;FA;;;{OTHER})"
        fake.plant(STATE, owner=OTHER, dacl=planted, is_dir=True)
        with self.assertRaises(Win32Refused) as caught:
            ensure_state_dir(fake)
        self.assertEqual("owner_mismatch", caught.exception.reason)
        names = [call[0] for call in fake.calls]
        self.assertNotIn("create_directory", names)
        self.assertNotIn("apply_security", names)
        self.assertEqual(planted, fake.objects[STATE].dacl)
        self.assertEqual(OTHER, fake.objects[STATE].owner)

    def test_a_junction_on_the_chain_is_refused(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import Win32Refused, ensure_state_dir

        for where in (LOCAL, "C:\\Users", "C:\\Users\\me\\AppData"):
            with self.subTest(where=where):
                fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
                fake.plant(where, owner=USER, dacl=f"D:P(A;;FA;;;{USER})", is_dir=True, is_reparse=True)
                with self.assertRaises(Win32Refused) as caught:
                    ensure_state_dir(fake)
                self.assertEqual("reparse_point", caught.exception.reason)
                names = [call[0] for call in fake.calls]
                self.assertNotIn("create_directory", names)
                self.assertNotIn("apply_security", names)
                self.assertNotIn("security", names)

    def test_a_junction_state_directory_is_refused(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import Win32Refused, ensure_state_dir

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        fake.plant(STATE, owner=USER, dacl=f"D:P(A;;FA;;;{USER})", is_dir=True, is_reparse=True)
        with self.assertRaises(Win32Refused) as caught:
            ensure_state_dir(fake)
        self.assertEqual("reparse_point", caught.exception.reason)
        names = [call[0] for call in fake.calls]
        self.assertNotIn("create_directory", names)
        self.assertNotIn("apply_security", names)
        self.assertNotIn("security", names)

    def test_everyone_and_inherited_aces_are_replaced_when_we_own_the_directory(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import ensure_state_dir, protected_sddl

        bad = (
            f"D:(A;ID;FA;;;{USER})(A;;FA;;;WD)(A;;FA;;;AU)"
            "(A;;FA;;;S-1-1-0)(A;;FA;;;S-1-5-11)"
        )
        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        fake.plant(STATE, owner=USER, dacl=bad, is_dir=True)
        path = ensure_state_dir(fake)
        self.assertEqual(STATE, path)
        applied = [call for call in fake.calls if call[0] == "apply_security"]
        self.assertEqual([("apply_security", STATE, protected_sddl(USER))], applied)
        _assert_user_only(self, fake.objects[STATE].dacl, USER)
        self.assertNotIn("WD", fake.objects[STATE].dacl)
        self.assertNotIn("AU", fake.objects[STATE].dacl)
        self.assertNotIn("S-1-1-0", fake.objects[STATE].dacl)
        self.assertNotIn("S-1-5-11", fake.objects[STATE].dacl)
        self.assertNotIn(";ID;", fake.objects[STATE].dacl)

    def test_a_bad_dacl_that_cannot_be_replaced_is_refused(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.paths import Win32Refused, ensure_state_dir

        bad = f"D:(A;;FA;;;WD)(A;;FA;;;{USER})"
        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        fake.plant(STATE, owner=USER, dacl=bad, is_dir=True)
        fake.hold_dacl = True
        with self.assertRaises(Win32Refused) as caught:
            ensure_state_dir(fake)
        self.assertEqual("dacl_refused", caught.exception.reason)
        self.assertEqual(bad, fake.objects[STATE].dacl)

    def test_a_second_lock_fails_already_running(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock
        from recursion_core.win32.paths import Win32Refused

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        first = UserLock(fake)
        first.acquire()
        second = UserLock(fake)
        with self.assertRaises(Win32Refused) as caught:
            second.acquire()
        self.assertEqual("already_running", caught.exception.reason)
        self.assertIsNotNone(first._handle)
        self.assertIsNone(second._handle)
        self.assertTrue(fake.objects[LOCK].exclusive)

    def test_releasing_the_lock_lets_the_next_caller_take_it(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        first = UserLock(fake)
        first.acquire()
        first.release()
        self.assertIsNone(first._handle)
        self.assertFalse(fake.objects[LOCK].exclusive)
        second = UserLock(fake)
        second.acquire()
        second.release()

    def test_interruption_releases_the_lock_handle(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        wrapper = _InterruptAfterOpen(fake, KeyboardInterrupt())
        held = UserLock(wrapper)
        with self.assertRaises(KeyboardInterrupt):
            held.acquire()
        self.assertIsNone(held._handle)
        self.assertFalse(fake.objects[LOCK].exclusive)
        nxt = UserLock(fake)
        nxt.acquire()
        nxt.release()

    def test_an_exception_releases_the_lock_handle(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        wrapper = _InterruptAfterOpen(fake, OSError("security read failed"))
        held = UserLock(wrapper)
        with self.assertRaises(OSError):
            held.acquire()
        self.assertIsNone(held._handle)
        self.assertFalse(fake.objects[LOCK].exclusive)
        nxt = UserLock(fake)
        nxt.acquire()
        nxt.release()

    def test_a_foreign_lock_file_is_refused_and_the_handle_is_released(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock
        from recursion_core.win32.paths import Win32Refused, protected_sddl

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        fake.plant(STATE, owner=USER, dacl=protected_sddl(USER), is_dir=True)
        foreign = f"O:{OTHER}G:{OTHER}D:P(A;;FA;;;{OTHER})"
        fake.plant(LOCK, owner=OTHER, dacl=foreign, is_dir=False)
        held = UserLock(fake)
        with self.assertRaises(Win32Refused) as caught:
            held.acquire()
        self.assertEqual("owner_mismatch", caught.exception.reason)
        self.assertIsNone(held._handle)
        self.assertFalse(fake.objects[LOCK].exclusive)
        self.assertEqual(foreign, fake.objects[LOCK].dacl)
        self.assertNotIn("apply_handle", [call[0] for call in fake.calls])

    def test_a_junction_lock_file_is_refused_before_it_is_opened(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock
        from recursion_core.win32.paths import Win32Refused, protected_sddl

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        fake.plant(STATE, owner=USER, dacl=protected_sddl(USER), is_dir=True)
        fake.plant(LOCK, owner=USER, dacl=protected_sddl(USER), is_dir=False, is_reparse=True)
        held = UserLock(fake)
        with self.assertRaises(Win32Refused) as caught:
            held.acquire()
        self.assertEqual("reparse_point", caught.exception.reason)
        self.assertNotIn("open_exclusive", [call[0] for call in fake.calls])
        self.assertFalse(fake.objects[LOCK].exclusive)

    def test_context_exit_releases_the_handle_and_does_not_swallow(self) -> None:
        from recursion_core.win32.fake import RecordingFake
        from recursion_core.win32.lock import UserLock

        fake = RecordingFake(user_sid=USER, local_appdata=LOCAL)
        with self.assertRaises(RuntimeError):
            with UserLock(fake):
                self.assertTrue(fake.objects[LOCK].exclusive)
                raise RuntimeError("cancelled")
        self.assertFalse(fake.objects[LOCK].exclusive)
        nxt = UserLock(fake)
        nxt.acquire()
        nxt.release()

    def test_import_on_posix_loads_no_windows_dll(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX import check; a Windows host loads the DLL only when the backend is asked for")
        core = Path(__file__).resolve().parents[3]
        code = (
            "import ctypes, sys\n"
            "loaded = []\n"
            "def _guard(name, *args, **kwargs):\n"
            "    loaded.append(str(name))\n"
            "    raise AssertionError('Windows DLL load: %s' % name)\n"
            "ctypes.CDLL = _guard\n"
            "ctypes.WinDLL = _guard\n"
            "ctypes.OleDLL = _guard\n"
            "import recursion_core.win32\n"
            "import recursion_core.win32.paths\n"
            "import recursion_core.win32.lock\n"
            "import recursion_core.win32.fake\n"
            "assert 'recursion_core.win32._api' not in sys.modules, sys.modules.get('recursion_core.win32._api')\n"
            "assert loaded == [], loaded\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(core)
        env["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")
        proc = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=str(core), check=False, capture_output=True, text=True, env=env,
        )
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:] + proc.stdout[-500:])
        self.assertNotIn("recursion_core.win32._api", sys.modules)

    def test_default_backend_on_posix_does_not_import_the_dll_module(self) -> None:
        from recursion_core.win32.paths import Win32Refused, ensure_state_dir

        if os.name == "nt":
            self.skipTest("POSIX default-backend check")
        sys.modules.pop("recursion_core.win32._api", None)
        with self.assertRaises(Win32Refused) as caught:
            ensure_state_dir()
        self.assertEqual("platform_unsupported", caught.exception.reason)
        self.assertNotIn("recursion_core.win32._api", sys.modules)


if __name__ == "__main__":
    unittest.main()
