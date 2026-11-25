"""What the wall's record proves, and that the probe itself never harms a file it was not made for.

Walls are applied in CHILD processes only (forked children, or child interpreters), each in a temporary folder of
the test's own; every file the probe or a child may touch is made by the test for the purpose. The tests in this
process run the probe's attempts unwalled on such a folder, or read the filter's program without loading it.
"""
from __future__ import annotations

import errno
import json
import os
import random
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, SCHEMA, _interpreter, _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_reach import _run_program

CHILD_TIMEOUT_S = 120  # a bound on one child process of this test; test-side only, not a product figure
TRIES = 400            # how many swaps and raced signals the tests try; test-side only, not a product figure
OTHER_TEXT = b"another file of the test's own, forty b\n"  # 40 bytes, as in the reproduction


def _schema_errors(record) -> list:
    try:
        import jsonschema
    except ImportError:
        return []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return [error.message for error in jsonschema.Draft202012Validator(schema).iter_errors(record)]


def _fixture(name: str) -> dict:
    return json.loads((SCHEMA.parent / "fixtures" / "good" / "wall.v1" / name).read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform.startswith("linux"), "the probe is Linux-only")
class ProbeNeverWritesThroughANameTests(unittest.TestCase):
    """The probe's sizing and opening of its file follow no link swapped in after its checks (unwalled, here)."""

    def swap_round(self, mode: int, top: str, number: int) -> tuple:
        folder = os.path.join(top, f"w{number}")
        os.mkdir(folder)
        wall_linux.prepare_write_folder(folder)
        own = os.path.join(folder, "write-me")
        if not os.path.exists(own):  # an earlier form made no file of its own: the test gives it one by name
            with open(own, "wb") as handle:
                handle.write(b"four")  # shorter than the other file, so that a cut through a link would show
        other = os.path.join(top, f"other{number}")
        with open(other, "wb") as handle:
            handle.write(OTHER_TEXT)
        swapped = []

        def swap():
            if not swapped:
                swapped.append(True)
                os.rename(own, os.path.join(top, f"moved{number}"))
                os.symlink(other, own)
        real_open, real_truncate = os.open, os.truncate

        def opening(path, *args, **kwargs):
            if mode == 1 and isinstance(path, str) and (path == own or path.startswith("/proc/self/fd/")):
                swap()
            return real_open(path, *args, **kwargs)

        def truncating(path, *args, **kwargs):
            if mode == 2:
                swap()
            return real_truncate(path, *args, **kwargs)
        if mode == 0:
            swap()
        with mock.patch.object(wall_linux.os, "open", opening), mock.patch.object(wall_linux.os, "truncate", truncating):
            outcomes = wall_linux._write_outcomes(own, folder)
        with open(other, "rb") as handle:
            return handle.read(), outcomes, bool(swapped)

    def test_a_link_swapped_in_after_the_checks_never_cuts_another_file(self):
        cut, seen_modes = [], set()
        with tempfile.TemporaryDirectory() as top:
            for number in range(TRIES):
                mode = number % 3
                text, outcomes, swapped = self.swap_round(mode, top, number)
                seen_modes.add((mode, swapped))
                if text != OTHER_TEXT:
                    cut.append((number, mode, len(text)))
                self.assertIn(outcomes["truncate"], ("let_through", "not_tried", "error"))
        self.assertEqual(cut, [], "no byte of another file changed in any try")
        self.assertEqual({mode for mode, swapped in seen_modes if swapped}, {0, 1, 2}, "each swap really took place")

    def test_the_probe_works_only_on_the_folder_and_file_it_made(self):
        with tempfile.TemporaryDirectory() as top:
            folder = os.path.join(top, "w")
            os.mkdir(folder)
            os.chmod(folder, 0o755)
            wall_linux.prepare_write_folder(folder)
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700, "only the user holds the folder")
            own = os.path.join(folder, wall_linux.WRITE_FILE_NAME)
            self.assertEqual(stat.S_IMODE(os.lstat(own).st_mode), 0o600)
            with open(own, "rb") as handle:
                made = handle.read()
            outcomes = wall_linux._write_outcomes(None, folder)
            self.assertEqual((outcomes["open_for_write"], outcomes["truncate"]), ("let_through", "let_through"))
            with open(own, "rb") as handle:
                self.assertEqual(handle.read(), made, "cut to its own length: nothing changed")
            self.assertGreater(len(made), 0)
            for case in ("another file in its place", "the folder's mode opened"):
                with self.subTest(case=case):
                    again = os.path.join(top, case.replace(" ", "-").replace("'", ""))
                    os.mkdir(again)
                    wall_linux.prepare_write_folder(again)
                    if case.startswith("another"):
                        stranger = os.path.join(top, "stranger")
                        with open(stranger, "wb") as handle:
                            handle.write(OTHER_TEXT)
                        os.chmod(stranger, 0o600)
                        os.rename(stranger, os.path.join(again, wall_linux.WRITE_FILE_NAME))
                    else:
                        os.chmod(again, 0o755)
                    outcomes = wall_linux._write_outcomes(None, again)
                    self.assertEqual((outcomes["open_for_write"], outcomes["truncate"]), ("not_tried", "not_tried"))
            given = os.path.join(top, "given")
            with open(given, "wb") as handle:
                handle.write(OTHER_TEXT)
            wall_linux._write_outcomes(given, None)
            with open(given, "rb") as handle:
                self.assertEqual(handle.read(), OTHER_TEXT, "a file handed by name is never opened or cut")


TERMINAL_CHILD = r'''
import fcntl, json, os, struct, sys, termios
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
os.setsid()
master, slave = os.openpty()
fcntl.ioctl(slave, termios.TIOCSCTTY, 0)  # the child's own terminal, held as an inherited one would be
out = {}
if spec["wall"]:
    w.apply_wall(spec["paths"])
def attempt(name, act):
    try:
        act()
        out[name] = "let_through"
    except (OSError, termios.error) as exc:
        out[name] = exc.args[0]
attempt("foreground_group", lambda: fcntl.ioctl(slave, termios.TIOCSPGRP, struct.pack("i", os.getpgrp())))
attempt("settings", lambda: termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(slave)))
attempt("size", lambda: fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0)))
attempt("signal_through_master", lambda: fcntl.ioctl(master, 0x40045436, struct.pack("i", 28)))  # TIOCSIG, SIGWINCH
attempt("read_settings", lambda: termios.tcgetattr(slave))
out["isatty"] = os.isatty(slave)
print(json.dumps(out))
'''


def run_terminal_child(wall: bool) -> dict:
    with tempfile.TemporaryDirectory() as top:
        spec = {"core": str(CORE), "paths": sorted({sys.base_prefix, sys.prefix}), "wall": wall}
        child = subprocess.run(_interpreter() + ["-c", TERMINAL_CHILD, json.dumps(spec)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top, env={"HOME": top, "TMPDIR": top})
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1])


class TerminalCommandsTests(unittest.TestCase):

    MUST_REFUSE = {"TIOCSPGRP": 0x5410, "TIOCSTI": 0x5412, "TIOCSCTTY": 0x540E, "TIOCNOTTY": 0x5422,
                   "TIOCSWINSZ": 0x5414, "TCSETS": 0x5402, "TCSETSW": 0x5403, "TCSETSF": 0x5404, "TCFLSH": 0x540B,
                   "TIOCSIG": 0x40045436, "TIOCVHANGUP": 0x5437, "TIOCCONS": 0x541D, "TIOCSETD": 0x5423}
    MUST_ALLOW = {"TCGETS": 0x5401, "TIOCGPGRP": 0x540F, "TIOCGWINSZ": 0x5413, "FIONREAD": 0x541B,
                  "FIONBIO": 0x5421, "FIOCLEX": 0x5451, "FIONCLEX": 0x5450}

    def test_the_filter_refuses_the_commands_that_act_on_a_terminal_and_keeps_those_that_read(self):
        for arch in ("x86_64", "aarch64"):
            program, ioctl = wall_linux._filter(arch, 3), wall_linux._NR_MORE[arch]["ioctl"]
            for name, value in self.MUST_REFUSE.items():
                with self.subTest(arch=arch, command=name):
                    self.assertEqual(wall_linux.DENIED_TERMINAL_COMMANDS.get(name), value)
                    self.assertEqual(_run_program(program, ioctl, args=(0, value, 0), arch=arch), "eperm")
            for name, value in self.MUST_ALLOW.items():
                with self.subTest(arch=arch, command=name):
                    self.assertEqual(_run_program(program, ioctl, args=(0, value, 0), arch=arch), "allow")
        self.assertEqual(set(wall_linux.DENIED_TERMINAL_COMMANDS.values()) & set(self.MUST_ALLOW.values()), set())

    @unittest.skipIf(_why_not_here(), _why_not_here() or "")
    def test_a_walled_process_cannot_change_the_foreground_group_of_its_terminal(self):
        unwalled, walled = run_terminal_child(False), run_terminal_child(True)
        self.assertEqual(unwalled["foreground_group"], "let_through", "the control: without the wall it goes through")
        self.assertEqual(unwalled["settings"], "let_through")
        for name in ("foreground_group", "settings", "size", "signal_through_master"):
            with self.subTest(name=name):
                self.assertEqual(walled[name], errno.EPERM)
        self.assertEqual((walled["read_settings"], walled["isatty"]), ("let_through", True), "reading stays allowed")


LIST_CHILD = r'''
import ctypes, json, os, resource, sys
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
out, mode = {}, spec["mode"]
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
address = None
if mode == "map":   # a file mapped shared and writable, every descriptor of it closed before the wall
    fd = os.open(spec["mapped"], os.O_RDWR)
    address = libc.mmap(None, 4096, 3, 1, fd, 0)  # PROT_READ|PROT_WRITE, MAP_SHARED
    os.close(fd)
if mode == "high":  # a descriptor numbered above a hard limit lowered after it was opened, with no count from the kernel
    fd = os.open(spec["mapped"], os.O_WRONLY)
    os.dup2(fd, 900)
    os.close(fd)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 512), 512))
    real_stat = os.stat
    class Sized:
        def __init__(self, found):
            self.found = found
        def __getattr__(self, name):
            return 0 if name == "st_size" else getattr(self.found, name)
    w.os.stat = lambda path, *a, **k: Sized(real_stat(path, *a, **k)) if path == "/proc/self/fd" else real_stat(path, *a, **k)
seen_lists = []
real_open_lists = w._open_kernel_lists
def keep(*a, **k):
    lists = real_open_lists(*a, **k)
    seen_lists.append(lists)
    return lists
w._open_kernel_lists = keep
held_now = [int(name) for name in os.listdir("/proc/self/fd")]  # by the kernel's list: above any limit too
w.apply_wall(spec["paths"], keep_descriptors=[n for n in held_now if w._identity(n)])
closed = []
for fd in seen_lists[0].values() if seen_lists else ():
    try:
        os.fstat(fd)
        closed.append(False)
    except OSError:
        closed.append(True)
out["lists_closed"] = closed
record = w.probe_wall(spec["canary"], spec["daemon"], write_folder=spec["folder"])
out.update(record=record, words=w.why_not_proven(record))
if address:
    ctypes.memmove(address, b"WRITTEN", 7)  # through the map, after the wall
    out["wrote_through_map"] = True
print(json.dumps(out))
'''


def run_list_child(mode: str) -> tuple:
    with tempfile.TemporaryDirectory() as top:
        canary, folder, mapped = os.path.join(top, "canary"), os.path.join(top, "w"), os.path.join(top, "mapped")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the host's test file, stand-in")
        with open(mapped, "wb") as handle:
            handle.write(b"m" * 4096)
        os.mkdir(folder)
        wall_linux.prepare_write_folder(folder)
        daemon = subprocess.Popen(_interpreter() + ["-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, cwd=top, env={"HOME": top, "TMPDIR": top})
        try:
            spec = {"core": str(CORE), "mode": mode, "canary": canary, "daemon": daemon.pid, "folder": folder,
                    "mapped": mapped, "paths": sorted({sys.base_prefix, sys.prefix})}
            child = subprocess.run(_interpreter() + ["-c", LIST_CHILD, json.dumps(spec)], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top,
                                   env={"HOME": top, "TMPDIR": top})
        finally:
            daemon.kill()
            daemon.wait()
        with open(mapped, "rb") as handle:
            head = handle.read(7)
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1]), head


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class KernelListsAfterTheWallTests(unittest.TestCase):

    def test_a_file_mapped_shared_and_writable_is_named_and_the_record_is_not_proven(self):
        result, head = run_list_child("map")
        record = result["record"]
        self.assertEqual(len(record["memory_maps_open_for_write"]), 1, record["memory_maps_open_for_write"])
        self.assertEqual(record["descriptors_open_for_write"], [], "no descriptor of the map is left")
        self.assertEqual(record["descriptors_seen_by"], "kernel_list_after_wall")
        self.assertIs(record["proven"], False)
        self.assertIn(wall_linux.WORDS["map_open"], result["words"])
        self.assertNotIn("writable_memory_map", record["writes_not_tried"])
        self.assertEqual(head, b"WRITTEN", "what the words say is true: a write through the map still reaches the file")
        self.assertEqual(_schema_errors(record), [])

    def test_the_two_lists_are_closed_before_apply_wall_returns_and_nothing_held_is_proven(self):
        result, head = run_list_child("none")
        self.assertEqual(result["lists_closed"], [True, True], "no plugin code can hold either list")
        record = result["record"]
        self.assertEqual((record["memory_maps_open_for_write"], record["descriptors_open_for_write"]), ([], []))
        self.assertIs(record["proven"], True)
        self.assertEqual(head, b"mmmmmmm")
        self.assertEqual(_schema_errors(record), [])

    def test_a_descriptor_above_a_lowered_limit_is_seen_where_the_kernel_gives_no_count(self):
        result, _ = run_list_child("high")
        record = result["record"]
        self.assertIn({"descriptor": 900, "kind": "regular_file"}, record["descriptors_open_for_write"] or [])
        self.assertIs(record["proven"], False)

    def test_the_maps_list_is_read_by_its_fields(self):
        text = (b"55d0-55d1 r--p 00000000 08:01 123 /usr/bin/python3\n"
                b"7f00-7f01 rw-s 00000000 08:01 456 /home/x/shared\n"
                b"7f02-7f03 rw-s 00000000 00:00 0 \n"
                b"7f04-7f05 rw-p 00000000 08:01 789 /home/x/private\n"
                b"7f06-7f07 r--s 00000000 08:01 790 /home/x/readonly\n"
                b"7f08-7f09 rw-s 00000000 00:01 1024 /dev/zero (deleted)\n"
                b"7f0a-7f0b rw-s 00000000 00:01 7 /SYSV00000000 (deleted)\n"
                b"7f0c-7f0d rw-s 00000000 00:01 9 /memfd:x (deleted)\n")
        # every map shared and writable, of a file or of memory alone (a process forked before the wall shares it)
        self.assertEqual(wall_linux._maps_shared_for_write(text), [{"map": 1}, {"map": 2}, {"map": 4}, {"map": 5}, {"map": 6},
                                                                   {"map": 7}])


SIGNAL_CHILD = r'''
import json, os, random, signal, sys, threading, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
rows, rng = [], random.Random(spec["seed"])
for number in range(spec["tries"]):
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        result = {}
        try:
            started = []
            def handler(signum, frame):
                worker = threading.Thread(target=lambda: None)
                started.append(worker)
                def late():
                    time.sleep(0.01)
                    try:
                        with open(spec["outside"], "rb") as handle:
                            handle.read(1)
                        result["thread_read"] = "read"
                    except OSError as exc:
                        result["thread_read"] = exc.errno
                worker = threading.Thread(target=late)
                started[-1] = worker
                worker.start()
            signal.signal(signal.SIGALRM, handler)
            signal.setitimer(signal.ITIMER_REAL, rng.random() * spec["window_s"] + 1e-6)
            try:
                w.apply_wall(spec["paths"])
                result["code"] = "applied"
            except w.WallRefused as exc:
                result["code"], result["step"] = exc.code, exc.step
            except BaseException as exc:
                result["code"] = "raised " + type(exc).__name__ + ": " + str(exc)[:200]
            signal.setitimer(signal.ITIMER_REAL, 0)
            result["thread"] = bool(started)
            result["threads_now"] = w._threads_now()
            for worker in started:
                worker.join()
        finally:
            os.write(writer, json.dumps(result).encode())
            os._exit(0)
    os.close(writer)
    data = b""
    while True:
        chunk = os.read(reader, 4096)
        if not chunk:
            break
        data += chunk
    os.close(reader)
    os.waitpid(pid, 0)
    rows.append(json.loads(data or b"{}"))
print(json.dumps(rows))
'''


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class SignalStartedThreadTests(unittest.TestCase):
    """Holds the present behaviour: a thread that a signal handler starts while the wall goes up is never left
    outside the wall with the wall said to stand."""

    def test_no_raced_thread_reads_outside_while_apply_wall_says_applied(self):
        with tempfile.TemporaryDirectory() as top:
            outside = os.path.join(top, "outside")
            with open(outside, "wb") as handle:
                handle.write(b"a file outside the wall, stand-in")
            spec = {"core": str(CORE), "paths": sorted({sys.base_prefix, sys.prefix}), "outside": outside,
                    "tries": TRIES, "seed": random.randrange(1 << 30), "window_s": 0.002}
            child = subprocess.run(_interpreter() + ["-c", SIGNAL_CHILD, json.dumps(spec)], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S * 4, cwd=top,
                                   env={"HOME": top, "TMPDIR": top})
        self.assertEqual(child.returncode, 0, child.stderr[-1500:])
        rows = json.loads(child.stdout.strip().splitlines()[-1])
        self.assertEqual(len(rows), TRIES)
        danger = [row for row in rows if row.get("code") == "applied" and row.get("thread_read") == "read"]
        self.assertEqual(danger, [], f"seed {spec['seed']}")
        unseen = [row for row in rows if row.get("code") == "applied" and row.get("thread") and row["threads_now"] == 1]
        self.assertEqual(unseen, [], "a thread born during the wall is always counted afterwards")
        raised = sorted({row.get("code") for row in rows if str(row.get("code", "")).startswith("raised")})
        self.assertEqual(raised, [], "apply_wall answers every try with its own refusal or applies")
        self.assertTrue(any(row.get("thread") for row in rows), "the signal really landed during the wall")


class FilterCreditForAnOpenTests(unittest.TestCase):
    """Holds the present behaviour: an open for writing is never credited to the filter, by the probe or the schema."""

    def test_an_open_for_writing_refused_by_the_filter_proves_nothing(self):
        with mock.patch.dict(wall_linux._STATE, {"applied": ["landlock_restrict_self", "seccomp_filter"],
                                                 "failed": None, "landlock_handled": (1 << 15) - 1,
                                                 "filter_truncate": True}):
            self.assertEqual(wall_linux._refused_by("open_for_write", errno.EPERM), "refused_elsewhere")
        h09 = _fixture("h09-below-abi-3-the-filter-refuses-truncation.json")
        for record in (_fixture("h01-a-proven-wall.json"), h09):
            with self.subTest(abi=record["landlock_abi"]):
                self.assertEqual(_schema_errors(record), [])
                try:
                    import jsonschema  # noqa: F401
                except ImportError:
                    self.skipTest("jsonschema is not installed for this interpreter")
                bad = dict(record, writes=dict(record["writes"], open_for_write="refused_by_filter"))
                self.assertNotEqual(_schema_errors(bad), [])


if __name__ == "__main__":
    unittest.main()
