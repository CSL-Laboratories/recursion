"""Product code calls the library doors that start an engine only for an engine routed at the release.

Three doors start an engine without passing ``validate_request``, so they never answer ``engine_not_routed``:
``ProfileRouteAdapter(...).run``, ``profile_adapters(...)[...]`` and ``subscription_routes.guard_then_spawn``.
The doors stay open (tests and embedders use them). This guard reads every product module of
``recursion_core`` (no test module, no ``tests`` or ``fixtures`` folder) and fails when one of them calls a
door for an engine that is not in ``RELEASE_ROUTED``. An engine the guard cannot read as a plain name is
accepted only in the doors' own bodies, which take the engine their caller gave; anywhere else it fails, so a
new caller is read by a person before it can start an engine the route check would refuse.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from recursion_core.routes.engines import DETECTED_NOT_ROUTED, RELEASE_ROUTED

ROOT = pathlib.Path(__file__).resolve().parents[2]   # core/recursion_core
DOORS = ("guard_then_spawn", "ProfileRouteAdapter", "profile_adapters")
# (module, function, door): the doors' own bodies. Each passes on the engine its caller named.
OWN_BODIES = frozenset({
    ("routes/profile_route.py", "ProfileRouteAdapter.run", "guard_then_spawn"),
    ("routes/profile_route.py", "profile_adapters", "ProfileRouteAdapter"),
})


def product_modules(root: pathlib.Path = ROOT):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if any(p in ("tests", "fixtures") for p in parts[:-1]) or parts[-1].startswith("test_"):
            continue
        yield rel, path.read_text(encoding="utf-8")


class _Calls(ast.NodeVisitor):
    """Every call of a door in one module: (function, door, engine node or None for a dict lookup key)."""

    def __init__(self) -> None:
        self.alias: dict[str, str] = {d: d for d in DOORS}
        self.scope: list[str] = []
        self.dicts: set[str] = set()   # names bound to a profile_adapters(...) result
        self.found: list[tuple[str, str, ast.expr | None, int]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for a in node.names:
            if a.name in DOORS:
                self.alias[a.asname or a.name] = a.name
        self.generic_visit(node)

    def _scoped(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def door(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return self.alias.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in DOORS:
            return func.attr
        return None

    def is_adapters(self, value: ast.expr) -> bool:
        return (isinstance(value, ast.Call) and self.door(value.func) == "profile_adapters") or \
            (isinstance(value, ast.Name) and value.id in self.dicts)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.is_adapters(node.value):
            self.dicts.update(t.id for t in node.targets if isinstance(t, ast.Name))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self.is_adapters(node.value):
            self.found.append((".".join(self.scope), "profile_adapters", node.slice, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        door = self.door(node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and self.is_adapters(node.func.value):
            self.found.append((".".join(self.scope), "profile_adapters", node.args[0] if node.args else None,
                               node.lineno))
        elif door in ("guard_then_spawn", "ProfileRouteAdapter"):
            at = 2 if door == "guard_then_spawn" else 0
            engine = node.args[at] if len(node.args) > at else \
                next((k.value for k in node.keywords if k.arg == "engine"), None)
            self.found.append((".".join(self.scope), door, engine, node.lineno))
        self.generic_visit(node)


def door_calls(rel: str, source: str) -> tuple[set, list[str]]:
    """The non-literal door calls seen (as (module, function, door)) and the calls that break the rule."""
    seen, bad = set(), []
    calls = _Calls()
    calls.visit(ast.parse(source, rel))
    for function, door, engine, line in calls.found:
        if isinstance(engine, ast.Constant) and isinstance(engine.value, str):
            if engine.value not in RELEASE_ROUTED:
                bad.append(f"{rel}:{line} {function or '<module>'} calls {door} for {engine.value!r}, "
                           "which is not routed at the release")
        elif (rel, function, door) in OWN_BODIES:
            seen.add((rel, function, door))
        else:
            bad.append(f"{rel}:{line} {function or '<module>'} calls {door} with an engine that is not a "
                       "plain routed name; start it through RouteService, where validate_request checks it")
    return seen, bad


class ProductDoorsTest(unittest.TestCase):
    def test_no_product_code_calls_a_door_for_an_engine_that_is_not_routed(self) -> None:
        seen, bad = set(), []
        for rel, source in product_modules():
            s, b = door_calls(rel, source)
            seen |= s
            bad += b
        self.assertEqual(bad, [])
        # The reader is not blind: it found the doors' own bodies where they are.
        self.assertEqual(seen, set(OWN_BODIES))


class GuardReadsTest(unittest.TestCase):
    """The guard's reader on small sources, so a change in how a door is called cannot slip past it."""

    def bad(self, source: str) -> list[str]:
        return door_calls("x/caller.py", source)[1]

    def test_each_door_named_with_a_not_routed_engine_is_caught(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            for source in (
                f"from recursion_core.subscription_routes import guard_then_spawn\n"
                f"def go(l):\n    guard_then_spawn(l, ['x'], {engine!r}, {{}}, {{}}, route_home='')\n",
                f"import recursion_core.subscription_routes as sr\n"
                f"def go(l):\n    sr.guard_then_spawn(l, ['x'], engine={engine!r}, profile={{}}, child_env={{}})\n",
                f"from recursion_core.subscription_routes import guard_then_spawn as start\n"
                f"def go(l):\n    start(l, ['x'], {engine!r}, {{}}, {{}}, route_home='')\n",
                f"from recursion_core.routes import ProfileRouteAdapter\n"
                f"def go(*a):\n    ProfileRouteAdapter({engine!r}, *a, argv=None, driver=None).run({{}}, 0)\n",
                f"from recursion_core.routes import profile_adapters\n"
                f"def go(*a):\n    profile_adapters(*a, argv=None, driver=None)[{engine!r}].run({{}}, 0)\n",
                f"from recursion_core.routes import profile_adapters\n"
                f"def go(*a):\n    m = profile_adapters(*a, argv=None, driver=None)\n    m[{engine!r}].run({{}}, 0)\n",
                f"from recursion_core.routes import profile_adapters\n"
                f"def go(*a):\n    m = profile_adapters(*a, argv=None, driver=None)\n    m.get({engine!r}).run({{}}, 0)\n",
            ):
                self.assertEqual(len(self.bad(source)), 1, source)

    def test_an_engine_that_is_not_a_plain_name_is_caught_outside_the_doors_own_bodies(self) -> None:
        source = ("from recursion_core.subscription_routes import guard_then_spawn\n"
                  "def go(l, e):\n    guard_then_spawn(l, ['x'], e, {}, {}, route_home='')\n")
        self.assertEqual(len(self.bad(source)), 1)

    def test_a_routed_engine_and_the_service_map_pass(self) -> None:
        source = ("from recursion_core.subscription_routes import guard_then_spawn\n"
                  "from recursion_core.routes import RouteService, profile_adapters\n"
                  "def go(l, *a):\n    guard_then_spawn(l, ['x'], 'opencode', {}, {}, route_home='')\n"
                  "    RouteService(profile_adapters(*a, argv=None, driver=None), None, None)\n")
        self.assertEqual(self.bad(source), [])


if __name__ == "__main__":
    unittest.main()
