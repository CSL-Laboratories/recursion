"""Load core test files the way a Windows host does, on a POSIX host (CSL-785).

Hosted run 42923670078, job ``windows``, master a70e9e40: test_child_env.py ran 80 tests,
``FAILED (errors=18, skipped=58)``. Every error was ``AttributeError: module 'os' has no attribute
'geteuid'`` from ``policy._owned_chain`` through ``cells.owned_tmpdir`` through ``ProfileTests``.
The windows job runs on master pushes and ``ci:full`` PRs only, and the gate stops at the first
failing file, so this harness lets the Linux gate see the same thing first.

Each simulation is a child interpreter. Before any ``recursion_core`` import it applies:

* ``os.name = "nt"`` and ``sys.platform = "win32"``, which the skips and the platform guards read;
* the ``os`` and ``socket`` names Windows CPython lacks that core/ reads are deleted;
* POSIX-only modules (``fcntl``, ``termios``, ``pwd``, ``grp``, ``resource``) are hidden through
  ``sys.modules``, so importing one raises ImportError as on Windows;
* ``Path(...)`` keeps making ``PosixPath``. With ``os.name`` set to ``"nt"`` it would make
  ``WindowsPath``, which this host cannot build or parses wrongly: a host artifact.

The stdlib modules that pick an implementation at import time are imported first, as this host has
them. Four things run that way: test_child_env.py, each package's test_windows_refusal.py (whose
``WindowsRefusalTests`` otherwise run only on a real Windows host), an import of every core test
module, and a run of every other core test file (the hosted windows job stops at the first failing
file, so it never reached them). ``HOST_LIMITED`` names the few cases this host cannot run as
Windows would.

Limits: path syntax, file modes, links, sockets, signals and process handling stay POSIX. This
checks what the code decides from ``os.name``, the ``os`` names present and the importable modules;
it is not a Windows run. Every value is fake; no engine, provider or network is used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_FLAG = "--simulate-windows"
_MARK = "SIMULATED_WINDOWS_RESULT "
# Names Windows CPython 3.12 has no ``os`` attribute for, among those core/ and its tests read.
POSIX_ONLY_OS_NAMES = (
    "geteuid", "getuid", "getegid", "getgid", "getgroups", "getresuid", "getresgid", "seteuid", "setuid",
    "setegid", "setgid", "setreuid", "setregid", "setresuid", "setresgid", "setgroups", "initgroups", "chown",
    "lchown", "fchown", "fork", "setsid", "getsid", "killpg", "getpgid", "setpgid", "getpgrp", "mkfifo",
    "statvfs", "sysconf", "uname", "pidfd_open", "pipe2", "O_NOFOLLOW", "O_DIRECTORY", "O_NOCTTY",
)  # O_NONBLOCK and O_CLOEXEC stay: this host's shutil and tempfile read them when called; Windows' never do
POSIX_ONLY_SOCKET_NAMES = ("AF_UNIX", "SO_PEERCRED", "SO_PASSCRED", "SCM_CREDENTIALS", "SCM_RIGHTS")
POSIX_ONLY_MODULES = ("fcntl", "grp", "posix", "pwd", "resource", "termios")
HERE = Path(__file__).resolve().parent
CORE = HERE.parents[2]
TARGET = HERE / "test_child_env.py"
REFUSAL_AREAS = ("cgroup_leaf", "child_env", "control", "control_transport", "daemon", "routes", "subscription_routes",
                 "tui")
# Cases where this host's standard library, not the code under test, decides the outcome under the simulation.
# Excused by exact test name; any other error or failure in those files still fails.
HOST_LIMITED = {
    "recursion_core/control_transport/tests/test_transport.py": {
        "FrameDeadlineTests.test_read_frame_deadline_bounds_the_whole_frame_not_each_recv":
            "this host's socket.socketpair() is the POSIX one and needs AF_UNIX; Windows' builds a loopback pair",
    },
    "recursion_core/plugin_security/tests/test_authority.py": {
        "AuthorityTests.test_process_concurrency_never_overspends":
            "multiprocessing picks its Windows backend (_winapi) from sys.platform, which this host lacks",
    },
}
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)', re.MULTILINE)


def _apply() -> None:
    import argparse, ast, difflib, importlib, pathlib, pprint, shutil, socket, sysconfig, tempfile  # noqa: E401,F401
    import traceback  # noqa: F401

    # Every module core/ imports anywhere is imported now, as this host has it: one that chose its
    # implementation from os.name at import would otherwise look for nt-only modules.
    wanted: set[str] = set()
    for path in CORE.glob("recursion_core/**/*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                wanted.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                wanted.add(node.module)
    for name in sorted(wanted):
        if name.split(".")[0] != "recursion_core":
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001  an optional or absent module stays absent
                pass
    tempfile.gettempdir()  # cache TMPDIR (the parent's scratch) before os.name changes the candidate list
    sysconfig.get_config_vars()  # likewise cached now: its first call picks the nt reader from os.name
    host_which = shutil.which

    def which(*args, **kwargs):  # this host's shutil.which takes its win32 branch once sys.platform says so
        saved = os.name, sys.platform
        os.name, sys.platform = "posix", "linux"
        try:
            return host_which(*args, **kwargs)
        finally:
            os.name, sys.platform = saved

    shutil.which = which
    posix_path = pathlib.PosixPath

    def _new(cls, *args, **kwargs):
        return object.__new__(posix_path if cls is pathlib.Path else cls)

    pathlib.Path.__new__ = staticmethod(_new)
    os.name = "nt"
    sys.platform = "win32"
    for module, names in ((os, POSIX_ONLY_OS_NAMES), (socket, POSIX_ONLY_SOCKET_NAMES)):
        for name in names:
            if hasattr(module, name):
                delattr(module, name)
    for name in POSIX_ONLY_MODULES:
        sys.modules[name] = None


def _probe() -> dict:
    out: dict = {"os.name": os.name, "sys.platform": sys.platform, "geteuid": hasattr(os, "geteuid"),
                 "getuid": hasattr(os, "getuid")}
    for name in ("fcntl", "pwd", "termios"):
        try:
            __import__(name)
            out[name] = "importable"
        except ImportError:
            out[name] = "ImportError"
    out["Path"] = type(Path("/x")).__name__
    return out


def _describe(test: unittest.TestCase, text: str) -> dict:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    frames = [f"{os.path.basename(m['file'])}:{m['line']}:{m['func']}" for m in _FRAME.finditer(text)]
    exc = next((line for line in reversed(lines) if not line.startswith(" ")), lines[-1] if lines else "")
    return {"test": ".".join(test.id().split(".")[-2:]), "exception": exc, "frames": frames}


def _load(path: str, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_file(path: str) -> dict:
    import importlib

    suite = unittest.defaultTestLoader.loadTestsFromModule(_load(path, "simulated_windows_target"))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    skipped = [(".".join(test.id().split(".")[-2:]), reason) for test, reason in result.skipped]
    return {
        # os.path: Path may be WindowsPath here. import_module: a test may have dropped the package from sys.modules.
        "package": os.path.dirname(importlib.import_module("recursion_core").__file__),
        "run": result.testsRun,
        "errors": [_describe(test, text) for test, text in result.errors],
        "failures": [_describe(test, text) for test, text in result.failures],
        "skipped": len(skipped),
        "skipped_tests": sorted(name for name, _ in skipped),
        "skip_reasons": sorted({reason for _, reason in skipped}),
    }


def _core_test_files(root) -> list:
    """Every ``test_*.py`` directly inside a folder named ``tests`` at any depth under ``<root>/recursion_core/``."""

    return sorted(path for path in Path(root).glob("recursion_core/**/tests/test_*.py") if path.parent.name == "tests")


def _windows_refusal_files(root) -> list:
    """Each gated area's ``test_windows_refusal.py`` and every other one the collector finds, at any depth.

    The other-suites run leaves these files out by name, so a refusal file missing here would be imported by the
    import-all run and its tests would never run.
    """

    base = Path(root) / "recursion_core"
    listed = {base / area / "tests" / "test_windows_refusal.py" for area in REFUSAL_AREAS}
    return sorted(listed | {path for path in _core_test_files(root) if path.name == "test_windows_refusal.py"})


def _import_all(root: str) -> dict:
    """Import every core test module, as the gate's runner does before it can run or skip anything."""

    failed: dict = {}
    paths = _core_test_files(root)
    for index, path in enumerate(paths):
        try:
            _load(str(path), f"simulated_windows_import_{index}")
        except BaseException as exc:  # noqa: BLE001  the finding: what a Windows collector would die on
            failed[path.relative_to(root).as_posix()] = f"{type(exc).__name__}: {exc}"
    return {"imported": len(paths), "failed": failed}


def _child_main(argv: list[str]) -> int:
    _apply()
    if argv[0] == "--probe":
        out = _probe()
    elif argv[0] == "--import-all":
        out = _import_all(argv[1])
    else:
        out = _run_file(argv[0])
    sys.stdout.write("\n" + _MARK + json.dumps(out, sort_keys=True) + "\n")
    return 0


def simulate(*args: str) -> dict:
    """Run one child under the simulation (a test file, ``--probe`` or ``--import-all ROOT``); return its JSON."""

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(CORE), env.get("PYTHONPATH", "")) if part)
    scratch = tempfile.mkdtemp(prefix="csl785-simulated-windows-")  # the child's temp files; removed here
    env["TMPDIR"] = scratch
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), _FLAG, *args],
            capture_output=True, text=True, timeout=150, env=env, cwd=str(CORE.parent), check=False,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    line = next((line for line in reversed(proc.stdout.splitlines()) if line.startswith(_MARK)), None)
    if line is None:
        raise AssertionError(f"simulated-Windows child gave no result (exit {proc.returncode}):\n{proc.stderr[-4000:]}")
    return json.loads(line[len(_MARK):])


@unittest.skipIf(os.name == "nt", "simulation for POSIX hosts; a Windows host runs the suites themselves")
class SimulatedWindowsTests(unittest.TestCase):
    maxDiff = None

    def _assert_clean(self, result: dict) -> None:
        bad = [f"{kind}: {row['test']}: {row['exception']} via {' <- '.join(reversed(row['frames'][-3:]))}"
               for kind in ("errors", "failures") for row in result[kind]]
        summary = (f"{len(result['errors'])} errors, {len(result['failures'])} failures, "
                   f"{result['skipped']} skipped of {result['run']} run")
        self.assertEqual([], bad, summary)

    def test_the_simulation_is_in_force(self) -> None:
        self.assertEqual(
            {"os.name": "nt", "sys.platform": "win32", "geteuid": False, "getuid": False, "fcntl": "ImportError",
             "pwd": "ImportError", "termios": "ImportError", "Path": "PosixPath"},
            simulate("--probe"),
        )

    def test_child_env_suite_has_no_error_or_failure_on_simulated_windows(self) -> None:
        # Hosted windows job at a70e9e40: errors=18, skipped=58 of 80.
        result = simulate(str(TARGET))
        self.assertEqual(str(CORE / "recursion_core"), result["package"])
        self._assert_clean(result)
        # The POSIX classes skipped at import; the cases that need no owned directory still ran.
        self.assertIn("CellTests.test_no_false_refusal", result["skipped_tests"])
        self.assertIn("LaunchTests.test_real_child_sees_only_the_validated_env", result["skipped_tests"])
        self.assertLess(result["skipped"], result["run"])

    def test_windows_refusal_tests_pass_and_skip_nothing_on_simulated_windows(self) -> None:
        for path in _windows_refusal_files(CORE):
            area = path.parents[1].relative_to(CORE / "recursion_core").as_posix()
            with self.subTest(area=area):
                self.assertTrue(path.is_file(), path)
                result = simulate(str(path))
                self._assert_clean(result)
                self.assertGreater(result["run"], 0)
                self.assertEqual([], result["skipped_tests"])

    def test_the_other_core_suites_have_no_error_or_failure_on_simulated_windows(self) -> None:
        others = sorted(path for path in _core_test_files(CORE)
                        if path.parent != HERE and path.name != "test_windows_refusal.py")
        self.assertGreater(len(others), len(REFUSAL_AREAS))
        self.assertLessEqual(set(HOST_LIMITED), {path.relative_to(CORE).as_posix() for path in others})
        # The gate gives this whole file 180 s. Each child costs about 0.8 s of interpreter start, so the children
        # run four at a time; each has its own scratch folder, and each is still checked in its own subTest below.
        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
            futures = {path: pool.submit(simulate, str(path)) for path in others}
        for path in others:
            suite = path.relative_to(CORE).as_posix()
            with self.subTest(suite=suite):
                result = futures[path].result()
                for kind in ("errors", "failures"):
                    result[kind] = [row for row in result[kind] if row["test"] not in HOST_LIMITED.get(suite, {})]
                self._assert_clean(result)

    def test_every_gated_area_has_a_windows_refusal_file(self) -> None:
        found = sorted(path.parents[1].name for path in CORE.glob("recursion_core/*/tests/test_windows_refusal.py"))
        self.assertEqual(sorted(REFUSAL_AREAS), found)

    def test_every_core_test_module_imports_on_simulated_windows(self) -> None:
        result = simulate("--import-all", str(CORE))
        self.assertGreater(result["imported"], len(REFUSAL_AREAS))
        self.assertEqual({}, result["failed"])

    def test_every_tests_folder_under_the_core_is_covered(self) -> None:
        # A tests folder deeper than recursion_core/<area>/tests/ (a package's own client/tests, say) is run too.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            made = ("recursion_core/a/tests/test_one.py", "recursion_core/b/client/tests/test_two.py",
                    "recursion_core/c/d/e/tests/test_three.py", "recursion_core/f/test_four.py")
            for name in made:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            self.assertEqual(sorted(root / name for name in made[:3]), _core_test_files(root))
        walked = sorted(Path(folder) / name for folder, _, names in os.walk(CORE / "recursion_core")
                        if Path(folder).name == "tests" for name in names
                        if name.startswith("test_") and name.endswith(".py"))
        self.assertEqual(walked, _core_test_files(CORE))

    def test_every_windows_refusal_file_the_collector_finds_is_run(self) -> None:
        # The other suites leave test_windows_refusal.py out by name, so the refusal runner takes every one the
        # collector finds, a nested one too; otherwise it would be imported and its tests never run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            made = ("recursion_core/a/tests/test_windows_refusal.py",
                    "recursion_core/b/client/tests/test_windows_refusal.py",
                    "recursion_core/c/d/e/tests/test_windows_refusal.py",
                    "recursion_core/b/client/tests/test_two.py", "recursion_core/f/test_windows_refusal.py")
            for name in made:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            listed = {root / "recursion_core" / area / "tests" / "test_windows_refusal.py" for area in REFUSAL_AREAS}
            self.assertEqual(sorted(listed | {root / name for name in made[:3]}), _windows_refusal_files(root))


if __name__ == "__main__":
    if sys.argv[1:2] == [_FLAG]:
        raise SystemExit(_child_main(sys.argv[2:]))
    unittest.main(verbosity=2)
