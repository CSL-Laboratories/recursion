"""Core product code imports nothing from an optional lane, and the client surface is declared.

Two lists at the top are the decision. Every product package under ``recursion_core`` is in
exactly one of them, so a new package fails this test until someone classifies it. The core
packages are the ones the lean-core assessment marks as staying in the core (``gui`` is core
today; ``cgroup_leaf`` is part of that core and is not a lane to move out). ``routing`` and
``tui`` are the optional packages that already live here. Other optional lanes are named in
``OPTIONAL_ROOTS`` even when their package is not installed yet.

The one exception is an import in ``control/hooks.py`` of a module named in ``REGISTRANTS``,
in a function that returns when ``find_spec`` of that module, or of a parent package, is None.
``control/cli.py`` and ``control/tui.py`` already import ``tui``; that
ceiling is data here and is not widened. The engine table (``EngineTable`` and its catalogue)
is core. An adapter module under ``routes.engines`` is an optional lane. The package module
``recursion_core/__init__.py`` is core product code and is scanned with the packages.

``__all__`` on the four modules the remote package imports is exactly the names its product
sources import on branch ``csl-238-remote-relay-20261114``. When ``remote/`` is present, its
product sources are read and may import only those names.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

# Product packages. Every directory under recursion_core, other than this test package, is in
# exactly one set.
CORE_PACKAGES = frozenset({
    "cgroup_leaf",
    "child_env",
    "control",
    "control_transport",
    "daemon",
    "events",
    "gui",
    "plugin_security",
    "routes",
    "subscription_routes",
})

OPTIONAL_PACKAGES = frozenset({
    "routing",
    "tui",
})

# This directory holds the cross-package tests. It is not a product package.
TEST_PACKAGE = "tests"

# Optional lanes. A match is the whole module or a dotted child, so ``control.mcp_tools`` is
# not ``control.mcp``.
OPTIONAL_ROOTS = (
    "desktop",
    "eval",
    "loop",
    "recursion_core.control.mcp",
    "recursion_core.desktop",
    "recursion_core.eval",
    "recursion_core.loop",
    "recursion_core.remote",
    "recursion_core.routing",
    "recursion_core.tui",
)

# The table in routes/engines is core. Anything under it, or any other name imported from it,
# is an adapter lane.
ENGINE_TABLE = "recursion_core.routes.engines"
ENGINE_TABLE_NAMES = frozenset({"CATALOGUE", "EngineTable", "EngineTableError", "SEEDED"})

# Existing core imports of tui. A file may import these names and no others.
ALLOWED_IMPORTS = {
    ("control/cli.py", "recursion_core.tui.protocol"): frozenset({"answer_conditions", "list_all_runs"}),
    ("control/tui.py", "recursion_core.tui"): frozenset({"term"}),
}

HOOKS_REL = "control/hooks.py"

# Non-test imports on csl-238-remote-relay-20261114 (25e245ed). Each __all__ is this set.
REMOTE_CLIENT_IMPORTS = {
    "recursion_core.control.client": (
        "DEFAULT_TIMEOUT_S",
        "socket_path",
    ),
    "recursion_core.control_transport.errors": (
        "FrameError",
        "InsecureSocketPath",
        "PeerNotAdmitted",
        "PlatformNotSupported",
        "RemoteBindRefused",
        "TransportBusy",
        "TransportRefused",
    ),
    "recursion_core.control_transport.framing": (
        "HEADER",
        "MAX_FRAME_BYTES",
        "decode_body",
        "encode_frame",
    ),
    "recursion_core.control_transport.unix": (
        "BUSY_FRAME",
        "NOT_ADMITTED_FRAME",
        "UnixControlClient",
        "refuse_remote_bind",
        "require_unix_transport",
    ),
}


def _core_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _optional(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in OPTIONAL_ROOTS)


def _control_family(module: str) -> bool:
    return module in {"recursion_core.control", "recursion_core.control_transport"} or module.startswith(
        ("recursion_core.control.", "recursion_core.control_transport.")
    )


def package_of(rel: str) -> str:
    """Package of a file under ``recursion_core``. ``control/cli.py`` is ``recursion_core.control``."""
    return ".".join(["recursion_core", *rel.split("/")[:-1]])


def absolute_module(package: str, level: int, module: str | None) -> str:
    """The absolute module an import names. ``level`` is the number of dots."""
    if level <= 0:
        return module or ""
    parts = package.split(".")
    keep = len(parts) - (level - 1)
    if keep < 1:
        return ""
    base = parts[:keep]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _iter_import_nodes(tree: ast.AST):
    found: list[tuple[ast.AST, ast.AST | None]] = []

    def walk(node: ast.AST, func: ast.AST | None) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append((node, func))
        enclosing = node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
        for child in ast.iter_child_nodes(node):
            walk(child, enclosing)

    walk(tree, None)
    return found


def iter_bindings(tree: ast.AST, package: str):
    """Yield ``(module, names, is_from, lineno, function)`` for each static import."""
    for node, func in _iter_import_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, frozenset(), False, node.lineno, func
        elif isinstance(node, ast.ImportFrom):
            module = absolute_module(package, node.level or 0, node.module)
            if not module:
                continue
            names = frozenset(alias.name for alias in node.names)
            yield module, names, True, node.lineno, func


def _iter_dynamic(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
            isinstance(func, ast.Attribute) and func.attr == "import_module"
        )
        if not dynamic:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield arg.value, node.lineno


def _registrants(tree: ast.AST) -> frozenset[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id != "REGISTRANTS":
                continue
            if not isinstance(value, (ast.Tuple, ast.List)):
                raise AssertionError("REGISTRANTS is not a list of literal module names")
            names = []
            for elt in value.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    raise AssertionError("REGISTRANTS has a non-literal entry")
                names.append(elt.value)
            return frozenset(names)
    raise AssertionError("control/hooks.py does not assign REGISTRANTS")


def _find_spec_absent(test: ast.AST):
    """Module names for which ``test`` contains ``importlib.util.find_spec(name) is None``."""
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Is):
            continue
        if len(node.comparators) != 1:
            continue
        none = node.comparators[0]
        if not isinstance(none, ast.Constant) or none.value is not None:
            continue
        call = node.left
        if not isinstance(call, ast.Call) or not call.args:
            continue
        called = call.func
        if not isinstance(called, ast.Attribute) or called.attr != "find_spec":
            continue
        util = called.value
        if not isinstance(util, ast.Attribute) or util.attr != "util":
            continue
        base = util.value
        if not isinstance(base, ast.Name) or base.id != "importlib":
            continue
        spec = call.args[0]
        if isinstance(spec, ast.Constant) and isinstance(spec.value, str):
            yield spec.value


def _guarded(func: ast.AST | None, module: str, lineno: int, registrants: frozenset[str]) -> bool:
    """True when a registrant import follows a return on ``find_spec(...) is None``.

    The import has to be a direct statement of that function. A bare ``find_spec`` call, a probe of
    a child module, or an ``if`` that does not return, does not count: the import still runs when
    the lane is absent.
    """
    if func is None or module not in registrants or not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    direct = any(
        isinstance(stmt, (ast.Import, ast.ImportFrom)) and stmt.lineno == lineno for stmt in func.body
    )
    if not direct:
        return False
    for stmt in func.body:
        if not isinstance(stmt, ast.If) or stmt.lineno >= lineno:
            continue
        if not stmt.body or not all(isinstance(item, (ast.Return, ast.Raise)) for item in stmt.body):
            continue
        for probed in _find_spec_absent(stmt.test):
            if probed == module or module.startswith(probed + "."):
                return True
    return False


def _forbidden_engine(module: str, names: frozenset[str], is_from: bool) -> bool:
    if module == ENGINE_TABLE:
        if not is_from:
            return False
        return "*" in names or not names or not names <= ENGINE_TABLE_NAMES
    return module.startswith(ENGINE_TABLE + ".")


def _allowed(rel: str, module: str, names: frozenset[str], is_from: bool) -> bool:
    ceiling = ALLOWED_IMPORTS.get((rel, module))
    if ceiling is None or not is_from or "*" in names or not names:
        return False
    return names <= ceiling


def _fmt(rel: str, lineno: int, module: str, names: frozenset[str], is_from: bool) -> str:
    if is_from and names:
        return f"{rel}:{lineno}: from {module} import {', '.join(sorted(names))}"
    return f"{rel}:{lineno}: import {module}"


def violations_in(rel: str, source: str, registrants: frozenset[str]) -> list[str]:
    """Import lines in one core file that reach an optional lane."""
    tree = ast.parse(source)
    package = package_of(rel)
    found: list[str] = []

    def add(module: str, names: frozenset[str], is_from: bool, lineno: int, func: ast.AST | None) -> None:
        if rel == HOOKS_REL and _guarded(func, module, lineno, registrants):
            return
        if _forbidden_engine(module, names, is_from) or (_optional(module) and not _allowed(rel, module, names, is_from)):
            found.append(_fmt(rel, lineno, module, names, is_from))
            return
        if not is_from or _optional(module) or module == ENGINE_TABLE or module.startswith(ENGINE_TABLE + "."):
            return
        for name in sorted(names):
            if name == "*":
                continue
            add(f"{module}.{name}", frozenset(), False, lineno, func)

    for module, names, is_from, lineno, func in iter_bindings(tree, package):
        add(module, names, is_from, lineno, func)
    for module, lineno in _iter_dynamic(tree):
        add(module, frozenset(), False, lineno, None)
    return found


def _product_packages(core: Path) -> set[str]:
    found = set()
    for child in core.iterdir():
        if not child.is_dir() or child.name in {TEST_PACKAGE, "__pycache__"}:
            continue
        if (child / "__init__.py").is_file():
            found.add(child.name)
    return found


def _core_files(core: Path):
    # The package module sits beside the packages. A forbidden import there is still core product code.
    for path in sorted(p for p in core.glob("*.py") if p.is_file()):
        yield path.relative_to(core).as_posix(), path
    for pkg in sorted(CORE_PACKAGES):
        base = core / pkg
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(core)
            if "tests" in rel.parts or "__pycache__" in rel.parts:
                continue
            if rel.parts[:2] == ("routes", "engines") and path.name != "__init__.py":
                continue
            if rel.parts[0] == "control" and "mcp" in rel.parts:
                continue
            yield rel.as_posix(), path


def _remote_import_problems(rel: str, source: str) -> list[str]:
    """Product imports in ``remote`` that are outside the published client names."""
    tree = ast.parse(source)
    problems = []
    for module, names, is_from, lineno, _func in iter_bindings(tree, package_of(rel)):
        targets = []
        if _control_family(module):
            targets.append((module, names, is_from))
        elif is_from:
            for name in names:
                if name != "*":
                    targets.append((f"{module}.{name}", frozenset(), False))
        for target, target_names, target_from in targets:
            if not _control_family(target):
                continue
            expected = REMOTE_CLIENT_IMPORTS.get(target)
            if expected is None or not target_from:
                problems.append(_fmt(rel, lineno, target, target_names, target_from))
                continue
            extra = set(target_names) - set(expected)
            if extra or "*" in target_names:
                problems.append(_fmt(rel, lineno, target, frozenset(extra or target_names), True))
    return problems


class ImportDirectionTests(unittest.TestCase):
    def test_every_product_package_is_in_exactly_one_list(self) -> None:
        self.assertFalse(CORE_PACKAGES & OPTIONAL_PACKAGES)
        self.assertNotIn(TEST_PACKAGE, CORE_PACKAGES | OPTIONAL_PACKAGES)
        for name in OPTIONAL_PACKAGES:
            self.assertIn(f"recursion_core.{name}", OPTIONAL_ROOTS)
        self.assertEqual(_product_packages(_core_dir()), CORE_PACKAGES | OPTIONAL_PACKAGES)

    def test_the_core_package_module_is_scanned(self) -> None:
        rels = [rel for rel, _path in _core_files(_core_dir())]
        self.assertIn("__init__.py", rels)

    def test_core_product_code_does_not_import_an_optional_lane(self) -> None:
        core = _core_dir()
        registrants = _registrants(ast.parse((core / HOOKS_REL).read_text(encoding="utf-8")))
        self.assertIn("recursion_core.remote.hooks", registrants)
        self.assertIn("recursion_core.routing.hooks", registrants)
        self.assertIn("recursion_core.control.mcp.hooks", registrants)
        files = list(_core_files(core))
        self.assertGreater(len(files), 20)
        bad = []
        for rel, path in files:
            bad.extend(violations_in(rel, path.read_text(encoding="utf-8"), registrants))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_recorded_tui_imports_are_exactly_what_those_files_import(self) -> None:
        core = _core_dir()
        for (rel, module), ceiling in sorted(ALLOWED_IMPORTS.items()):
            self.assertIn(rel.split("/")[0], CORE_PACKAGES)
            self.assertTrue(_optional(module), module)
            tree = ast.parse((core / rel).read_text(encoding="utf-8"))
            got: set[str] = set()
            for imported, names, is_from, _lineno, _func in iter_bindings(tree, package_of(rel)):
                if is_from and imported == module:
                    got |= set(names)
            self.assertEqual(got, set(ceiling), rel)

    def test_a_scratch_import_of_an_optional_lane_is_reported(self) -> None:
        bad = violations_in("daemon/__init__.py", "import recursion_core.remote\n", frozenset())
        self.assertEqual(bad, ["daemon/__init__.py:1: import recursion_core.remote"])
        bad = violations_in(
            "daemon/launcher.py",
            "from recursion_core.routes.engines.opencode import Adapter\n",
            frozenset(),
        )
        self.assertEqual(bad, ["daemon/launcher.py:1: from recursion_core.routes.engines.opencode import Adapter"])
        bad = violations_in("control/cli.py", "from ..tui import app\n", frozenset())
        self.assertEqual(bad, ["control/cli.py:1: from recursion_core.tui import app"])

    def test_a_find_spec_guarded_registrant_import_is_not_reported(self) -> None:
        source = (
            "REGISTRANTS = ('recursion_core.remote.hooks',)\n"
            "def _load_remote_hooks():\n"
            "    if importlib.util.find_spec('recursion_core.remote') is None:\n"
            "        return None\n"
            "    import recursion_core.remote.hooks as module\n"
            "    return module\n"
        )
        registrants = _registrants(ast.parse(source))
        self.assertEqual(violations_in(HOOKS_REL, source, registrants), [])
        unguarded = "import recursion_core.remote.hooks\n"
        bad = violations_in(HOOKS_REL, unguarded, registrants)
        self.assertEqual(bad, ["control/hooks.py:1: import recursion_core.remote.hooks"])

    def test_a_registrant_import_without_an_absence_return_is_reported(self) -> None:
        registrants = frozenset({"recursion_core.remote.hooks"})
        ignored = (
            "def _load_remote_hooks():\n"
            "    importlib.util.find_spec('recursion_core.remote.hooks')\n"
            "    import recursion_core.remote.hooks as module\n"
        )
        bad = violations_in(HOOKS_REL, ignored, registrants)
        self.assertEqual(bad, ["control/hooks.py:3: import recursion_core.remote.hooks"])
        child = (
            "def _load_remote_hooks():\n"
            "    if importlib.util.find_spec('recursion_core.remote.hooks.extra') is None:\n"
            "        return None\n"
            "    import recursion_core.remote.hooks as module\n"
        )
        bad = violations_in(HOOKS_REL, child, registrants)
        self.assertEqual(bad, ["control/hooks.py:4: import recursion_core.remote.hooks"])
        no_return = (
            "def _load_remote_hooks():\n"
            "    if importlib.util.find_spec('recursion_core.remote.hooks') is None:\n"
            "        marker = 1\n"
            "    import recursion_core.remote.hooks as module\n"
        )
        bad = violations_in(HOOKS_REL, no_return, registrants)
        self.assertEqual(bad, ["control/hooks.py:4: import recursion_core.remote.hooks"])
        real = (
            "def _load_remote_hooks():\n"
            "    if (importlib.util.find_spec('recursion_core.remote') is None\n"
            "            or importlib.util.find_spec('recursion_core.remote.hooks') is None):\n"
            "        return None\n"
            "    import recursion_core.remote.hooks as module\n"
            "    return module\n"
        )
        self.assertEqual(violations_in(HOOKS_REL, real, registrants), [])


class PublicClientApiTests(unittest.TestCase):
    def test_published_names_are_exactly_what_remote_imports(self) -> None:
        self.assertEqual(set(REMOTE_CLIENT_IMPORTS), {
            "recursion_core.control.client",
            "recursion_core.control_transport.errors",
            "recursion_core.control_transport.framing",
            "recursion_core.control_transport.unix",
        })
        for module_name, expected in REMOTE_CLIENT_IMPORTS.items():
            module = importlib.import_module(module_name)
            published = getattr(module, "__all__", None)
            self.assertIsInstance(published, (list, tuple), module_name)
            self.assertEqual(list(published), list(dict.fromkeys(published)), module_name)
            self.assertEqual(sorted(published), sorted(expected), module_name)
            for name in expected:
                self.assertIn(name, vars(module), f"{module_name}.{name} is not bound")

    def test_remote_product_code_imports_only_published_names_when_the_package_is_present(self) -> None:
        remote = _core_dir() / "remote"
        if not (remote / "__init__.py").is_file():
            self.assertTrue(REMOTE_CLIENT_IMPORTS)
            return
        bad = []
        for path in sorted(remote.rglob("*.py")):
            rel = path.relative_to(_core_dir())
            if "tests" in rel.parts or "__pycache__" in rel.parts:
                continue
            bad.extend(_remote_import_problems(rel.as_posix(), path.read_text(encoding="utf-8")))
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
