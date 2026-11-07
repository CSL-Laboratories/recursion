"""Required test 8: TUI production modules import nothing from recursion_core beyond the control API
client (``recursion_core.control.api``); today they import none of it, because the API is injected.
No module has process, signal or network access; only term.py may import os (terminal I/O)."""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import unittest

PKG = pathlib.Path(__file__).resolve().parents[1]
CORE = PKG.parents[1]
ALLOWED_RECURSION = {"recursion_core.control.api"}
BANNED_TOP = {"subprocess", "signal", "socket", "ctypes", "multiprocessing", "pty", "resource", "http", "urllib",
              "asyncio", "importlib", "threading_ext"}
OS_ALLOWED_IN = {"term.py"}


def production_files() -> list[pathlib.Path]:
    return sorted(f for f in PKG.glob("*.py"))


def imported(source: str, package: str = "recursion_core.tui") -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(package.split(".")[: len(package.split(".")) - node.level + 1] + ([base] if base else []))
            names.add(base)
            names |= {f"{base}.{a.name}" for a in node.names}  # `from pkg import mod` names pkg.mod
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("__import__", "eval", "exec"):
            names.add(f"<dynamic {node.func.id}>")
    return names


def violations(name: str, source: str) -> list[str]:
    bad = []
    for mod in imported(source):
        if mod.startswith("<dynamic"):
            bad.append(mod)
            continue
        top = mod.split(".")[0]
        if top == "recursion_core":
            if mod.startswith("recursion_core.tui") or mod == "recursion_core":
                continue
            if (mod in ALLOWED_RECURSION or any(mod.startswith(a + ".") for a in ALLOWED_RECURSION)
                    or any(a.startswith(mod + ".") for a in ALLOWED_RECURSION)):
                # `from recursion_core.control import api` lists recursion_core.control too; allowed only
                # when every name it imports is the API module.
                continue
            bad.append(mod)
        elif top in BANNED_TOP:
            bad.append(mod)
        elif top == "os" and name not in OS_ALLOWED_IN:
            bad.append(mod)
    return bad


def check_from_package(source: str) -> list[str]:
    """`from recursion_core.control import X` is allowed only for X == api."""

    bad = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "recursion_core.control":
            bad += [f"recursion_core.control.{a.name}" for a in node.names if a.name != "api"]
    return bad


class ImportBoundaryTest(unittest.TestCase):
    def test_production_modules_import_only_the_control_api_client(self) -> None:
        files = production_files()
        self.assertGreaterEqual({f.name for f in files}, {"__init__.py", "app.py", "protocol.py", "sanitize.py",
                                                          "term.py", "view.py"})
        for f in files:
            src = f.read_text(encoding="utf-8")
            with self.subTest(f.name):
                self.assertEqual(violations(f.name, src) + check_from_package(src), [])

    def test_detector_catches_forbidden_imports(self) -> None:
        mutants = ["from recursion_core.control import cli", "import recursion_core.control.cli",
                   "from recursion_core.control.cli import main", "import recursion_core.daemon",
                   "from recursion_core import child_env", "from recursion_core.plugin_security import x",
                   "from recursion_core.control.registry import InMemoryRunRegistry",
                   "import recursion_core.routes", "import subprocess", "import signal", "import os",
                   "__import__('recursion_core.daemon')", "import importlib",
                   "from recursion_core.control import api, registry"]
        for src in mutants:
            with self.subTest(src):
                self.assertNotEqual(violations("app.py", src) + check_from_package(src), [])
        for ok in ("from recursion_core.control import api", "import recursion_core.control.api",
                   "from recursion_core.control.api import ControlAPI", "from recursion_core.tui import protocol"):
            with self.subTest(ok):
                self.assertEqual(violations("app.py", ok) + check_from_package(ok), [])

    def test_loading_the_tui_loads_no_other_recursion_core_module(self) -> None:
        code = ("import sys, recursion_core.tui, recursion_core.tui.term; "
                "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('recursion_core'))))")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=CORE, capture_output=True, text=True,
                             env={"PYTHONPATH": str(CORE), "PATH": ""}, check=True).stdout.split()
        self.assertEqual([m for m in out if not m.startswith("recursion_core.tui") and m != "recursion_core"], [])


if __name__ == "__main__":
    unittest.main()
