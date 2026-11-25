"""The filter is an allow list read from data: what it names is let through, everything else is refused.

Walls are applied in CHILD processes only, each in a temporary folder of the test's own; every allow list a test
gives the wall is a copy of the shipped one that the test wrote in its own temporary folder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, _interpreter, _why_not_here
from recursion_core.plugin_host.tests.test_wall_linux_proof import _run_program

CHILD_TIMEOUT_S = 120  # a bound on one child process of this test; test-side only, not a product figure
# Ten calls the refusals by name do not name, that neither the wall's tests nor the interpreter need, each harmless
# with these arguments and answered without EPERM where nothing refuses it (x86_64 number, aarch64 number, arguments).
TEN = {
    "personality": (135, 92, (0xFFFFFFFF,)),          # read the persona only
    "kcmp": (312, 272, ("pid", "pid", 1, 0, 0)),      # KCMP_VM of this process with itself
    "statmount": (457, 457, (0, 0, 0, 0)),            # a mount-listing call below the tables' top: EFAULT (ENOSYS before 6.8)
    "listmount": (458, 458, (0, 0, 0, 0)),            # the other one: EFAULT (ENOSYS before 6.8)
    "memfd_create": (319, 279, (0, 0)),               # no name: EFAULT
    "set_mempolicy": (238, 237, (0, 0, 0)),           # MPOL_DEFAULT: what the process has already
    "munlockall": (152, 231, ()),                     # the process locks nothing
    "sched_rr_get_interval": (148, 127, (0, 0)),      # no place for the answer: EFAULT
    "ioprio_get": (252, 31, (1, 0)),                  # the caller's own priority
    "cachestat": (451, 451, (-1, 0, 0, 0)),           # no descriptor: EBADF (ENOSYS before Linux 6.5)
}
PLANTED = {"call": "kcmp", "x86_64": 312, "aarch64": 272, "reason": "planted by the test: a row the shipped list does not hold",
           "recorded": [], "beyond_recording": "planted by the test"}

CHILD = r'''
import ctypes, json, os, socket, sys, threading, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
ARCH = 0 if w._arch() == "x86_64" else 1

def call(numbers, args):
    args = [os.getpid() if a == "pid" else a for a in args]
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(numbers[ARCH]), *(ctypes.c_long(a) for a in args))
    return 0 if result >= 0 else ctypes.get_errno()

def step(act):
    try:
        act()
        return 0
    except OSError as exc:
        return exc.errno or -1

out = {}
here, there = socket.socketpair()  # the channel, made before the wall and kept
if spec["mode"] != "control":
    try:
        w.apply_wall(spec["paths"], keep_descriptors=(1, 2, here.fileno(), there.fileno()),
                     **({"allow_list": spec["list"]} if spec.get("list") else {}))
        out["apply"] = "applied"
        out["allow_list"] = w._STATE.get("allow_list")
    except w.WallRefused as exc:
        out["apply"], out["detail"], out["applied"] = exc.code, exc.detail, list(w._STATE["applied"])
        with open("/proc/self/status") as handle:
            out["status"] = {k: v.strip() for k, v in (l.split(":", 1) for l in handle) if k in ("NoNewPrivs", "Seccomp")}
        print(json.dumps(out))
        sys.exit(0)
out["ten"] = {name: call(numbers, args) for name, numbers, args in spec["ten"]}
if spec.get("planted"):
    out["planted"] = call(spec["planted"], ("pid", "pid", 1, 0, 0))

def imports():
    import decimal, email.parser, fractions, json as _j, statistics, xml.dom.minidom  # noqa: F401
    xml.dom.minidom.parseString("<a><b/></a>")
    str(decimal.Decimal(1) / 7)

def threads():
    done = threading.Event()
    worker = threading.Thread(target=done.set)
    worker.start()
    worker.join(5)
    if not done.is_set():
        raise OSError(-2, "the thread did not run")

def fork():
    pid = os.fork()
    if pid == 0:
        os._exit(7)
    if os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) != 7:
        raise OSError(-3, "the child did not end as asked")

def pipe():
    r, wr = os.pipe()
    os.write(wr, b"through")
    if os.read(r, 16) != b"through":
        raise OSError(-4, "the pipe lost its bytes")
    os.close(r)
    os.close(wr)

def channel():
    here.sendall(b"question")
    if there.recv(16) != b"question":
        raise OSError(-5, "the channel lost its bytes")

def waits():
    import selectors
    time.sleep(0.01)
    with selectors.DefaultSelector() as sel:
        sel.register(there, selectors.EVENT_READ)
        sel.select(0)
    time.monotonic()
    os.getrandom(8)

out["steps"] = {name: step(act) for name, act in (("imports", imports), ("threads", threads), ("fork", fork),
                                                     ("pipe", pipe), ("channel", channel), ("waits", waits))}
print(json.dumps(out))
'''


def _shipped() -> dict:
    with open(wall_linux.ALLOW_LIST_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def run_child(mode: str, rows=None, planted=False) -> dict:
    """Run CHILD in ``mode`` (control: no wall), with the allow list ``rows`` written to a file of the test's own
    (None: the shipped list); the child's last line of output (JSON)."""
    with tempfile.TemporaryDirectory() as top:
        spec = {"core": str(CORE), "mode": mode, "paths": sorted({sys.base_prefix, sys.prefix}),
                "ten": [[name, [x, a], list(args)] for name, (x, a, args) in TEN.items()]}
        if rows is not None:
            spec["list"] = os.path.join(top, "allow.json")
            with open(spec["list"], "w", encoding="utf-8") as handle:
                json.dump(dict(_shipped(), rows=rows), handle)
        if planted:
            spec["planted"] = [PLANTED["x86_64"], PLANTED["aarch64"]]
        child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=CHILD_TIMEOUT_S, cwd=top, env={"HOME": top, "TMPDIR": top})
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1])


class TheAllowListIsDataTests(unittest.TestCase):

    def test_the_shipped_list_loads_and_every_row_carries_its_reason(self):
        allow = wall_linux.load_allow_list()
        self.assertEqual((allow["shipped"], allow["added"], allow["removed"]), (True, [], []))
        self.assertEqual(len(allow["rows"]), 187)
        for row in allow["rows"]:
            with self.subTest(call=row["call"]):
                self.assertTrue(row["reason"].strip())
                self.assertEqual(row.get("check"), wall_linux._required_check(row["call"]))
                self.assertNotIn(row["call"], wall_linux._refused_by_name())

    def test_the_list_is_not_wider_than_the_recording_and_the_rows_with_a_written_reason(self):
        rows = wall_linux.load_allow_list()["rows"]
        recorded = [row["call"] for row in rows if row["recorded"]]
        wider = [row["call"] for row in rows if not row["recorded"]]
        self.assertEqual((len(recorded), len(wider)), (66, 121))
        for row in rows:
            with self.subTest(call=row["call"]):
                if row["recorded"]:
                    self.assertTrue(set(row["recorded"]) <= {"cpython-3.12-x86_64", "cpython-3.14t-x86_64"})
                else:
                    self.assertTrue(row["beyond_recording"].strip(), "a row wider than the recording says why")
        for name in TEN:
            self.assertNotIn(name, {row["call"] for row in rows}, "the ten calls are off the list")

    def test_a_list_that_cannot_be_used_is_refused_with_words(self):
        shipped = _shipped()
        rows = shipped["rows"]
        by = {row["call"]: row for row in rows}
        bad = {
            "missing": None,
            "not json": "{",
            "other form": json.dumps({"format": "something else", "rows": rows}),
            "emptied": json.dumps(dict(shipped, rows=[])),
            "no reason": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, reason="")])),
            "reason left out": json.dumps(dict(shipped, rows=rows + [{k: v for k, v in PLANTED.items() if k != "reason"}])),
            "wider without a written reason": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, beyond_recording="")])),
            "a call refused by name": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, call="socket", x86_64=41, aarch64=198)])),
            "the number of a call refused by name": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, x86_64=41)])),
            "a check dropped": json.dumps(dict(shipped, rows=[dict(r, check=None) if r["call"] == "clone" else r for r in rows])),
            "a number above the tables": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, x86_64=470)])),
            "a number not the table's": json.dumps(dict(shipped, rows=[dict(r, x86_64=73) if r["call"] == "fcntl" else r for r in rows])),
            "a call twice": json.dumps(dict(shipped, rows=rows + [by["read"]])),
            "no number": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, x86_64=None, aarch64=None)])),
            "an unknown key": json.dumps(dict(shipped, rows=rows + [dict(PLANTED, allow_everything=True)])),
        }
        with tempfile.TemporaryDirectory() as top:
            for name, text in bad.items():
                path = os.path.join(top, name.replace(" ", "-") + ".json")
                if text is not None:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(text)
                with self.subTest(case=name):
                    with self.assertRaises(wall_linux.WallRefused) as caught:
                        wall_linux.load_allow_list(path)
                    self.assertEqual(caught.exception.code, "wall_allow_list_refused")
                    self.assertIn(path, caught.exception.detail)
                    self.assertIn("so it changed nothing in this process", caught.exception.detail)
                    self.assertIn("Give the sandbox the list shipped beside it", caught.exception.detail)

    def test_a_widened_list_is_named_as_not_the_shipped_one(self):
        with tempfile.TemporaryDirectory() as top:
            path = os.path.join(top, "allow.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(dict(_shipped(), rows=_shipped()["rows"] + [PLANTED]), handle)
            allow = wall_linux.load_allow_list(path)
        self.assertEqual((allow["shipped"], allow["added"], allow["removed"], len(allow["rows"])), (False, ["kcmp"], [], 188))


class TheProgramIsTheListTests(unittest.TestCase):

    def test_what_the_list_names_is_let_through_and_nothing_else(self):
        rows = wall_linux.load_allow_list()["rows"]
        for arch in ("x86_64", "aarch64"):
            program = wall_linux._filter(arch, 3)
            with self.subTest(arch=arch):
                for row in rows:
                    if row[arch] is not None and row.get("check") in (None, "landlock_truncate"):
                        self.assertEqual(_run_program(program, row[arch], arch=arch), "allow", row["call"])
                for name, (x86, arm, _) in TEN.items():
                    self.assertEqual(_run_program(program, x86 if arch == "x86_64" else arm, arch=arch), "eperm", name)
                read = {"x86_64": 0, "aarch64": 63}[arch]
                self.assertEqual(_run_program(program, read, arch=arch), "allow")
                for other in (0x40000003, 0x40000028, 0xC00000B7 if arch == "x86_64" else 0xC000003E):
                    self.assertEqual(_run_program(program, read, arch=arch, audit_arch=other), "eperm", hex(other))
                top = wall_linux._newest_call(arch)
                for number in (top + 1, 0x3FFFFFFF):
                    self.assertEqual(_run_program(program, number, arch=arch), "eperm", number)

    def test_an_added_row_is_let_through_by_the_program(self):
        rows = wall_linux.load_allow_list()["rows"] + (PLANTED,)
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch):
                self.assertEqual(_run_program(wall_linux._filter(arch, 3, rows), PLANTED[arch], arch=arch), "allow")
                self.assertEqual(_run_program(wall_linux._filter(arch, 3), PLANTED[arch], arch=arch), "eperm")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class TheAllowListBehindTheWallTests(unittest.TestCase):

    def test_ten_calls_off_the_list_are_refused_after_the_wall_and_answered_in_the_control(self):
        control, walled = run_child("control"), run_child("walled")
        self.assertEqual(walled["apply"], "applied")
        self.assertEqual({name: errno_ != 1 for name, errno_ in control["ten"].items()}, dict.fromkeys(TEN, True),
                         f"the control answers without EPERM: {control['ten']}")
        self.assertEqual(walled["ten"], dict.fromkeys(TEN, 1), "each refused EPERM by the wall")

    def test_ordinary_python_works_behind_the_allow_list(self):
        walled = run_child("walled")
        self.assertEqual(walled["apply"], "applied")
        self.assertEqual(walled["steps"], dict.fromkeys(("imports", "threads", "fork", "pipe", "channel", "waits"), 0))
        self.assertEqual(walled["allow_list"], {"file": wall_linux.ALLOW_LIST_FILE, "shipped": True, "added": [],
                                                "removed": [], "rows": 187})

    def test_a_list_without_a_row_the_run_needs_fails_loudly(self):
        rows = [row for row in _shipped()["rows"] if row["call"] not in ("pipe", "pipe2")]
        walled = run_child("walled", rows)
        self.assertEqual(walled["apply"], "applied")
        self.assertEqual(walled["steps"]["pipe"], 1, "the pipe is refused EPERM: the run fails, and says so")
        self.assertEqual((walled["allow_list"]["shipped"], walled["allow_list"]["removed"]), (False, ["pipe", "pipe2"]))

    def test_an_emptied_list_refuses_the_wall_before_any_change(self):
        walled = run_child("walled", [])
        self.assertEqual((walled["apply"], walled["applied"]), ("wall_allow_list_refused", []))
        self.assertEqual(walled["status"], {"NoNewPrivs": "0", "Seccomp": "0"})
        self.assertIn("cannot be used", walled["detail"])

    def test_one_row_added_lets_its_call_through_and_the_list_is_named_as_not_shipped(self):
        walled = run_child("walled", _shipped()["rows"] + [PLANTED], planted=True)
        self.assertEqual(walled["apply"], "applied")
        self.assertEqual(walled["planted"], 0, "the planted call is let through by its row")
        self.assertEqual(walled["ten"]["personality"], 1)
        self.assertEqual((walled["allow_list"]["shipped"], walled["allow_list"]["added"]), (False, ["kcmp"]))


if __name__ == "__main__":
    unittest.main()
