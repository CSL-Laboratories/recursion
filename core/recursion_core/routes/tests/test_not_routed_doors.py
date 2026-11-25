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


class _Scope:
    """The names one scope binds, each with the kinds it can hold: ``adapters`` (a ``profile_adapters(...)``
    result) or ``door:<name>`` (an alias of a door). A name bound to anything else holds the empty set, so it
    still hides a module-level name of the same spelling."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.names: dict[str, set[str]] = {}


class _Calls(ast.NodeVisitor):
    """Every call of a door in one module: (function, door, engine node or None for a dict lookup key).

    Names are read per scope, as Python does: each module, class and function is read once before its body is
    visited, so a name bound anywhere in it (a plain, annotated or named-expression assignment, each name of a
    tuple target, a door imported under another name) is known to every statement of that scope and of the
    functions inside it; a function drops its own names on leaving."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.frames: list[_Scope] = []
        self.found: list[tuple[str, str, ast.expr | None, int]] = []

    # --- names ---
    def lookup(self, name: str) -> set[str] | None:
        for depth, frame in enumerate(reversed(self.frames)):
            if frame.kind == "class" and depth:   # a class body's names are not seen inside its methods
                continue
            if name in frame.names:
                return frame.names[name]
        return None

    def doors(self, func: ast.expr) -> set[str]:
        if isinstance(func, ast.Name):
            kinds = self.lookup(func.id)
            if kinds is None:
                return {func.id} if func.id in DOORS else set()
            return {k[5:] for k in kinds if k.startswith("door:")}
        if isinstance(func, ast.Attribute) and func.attr in DOORS:
            return {func.attr}
        return set()

    def kinds(self, value: ast.expr) -> set[str]:
        """What a name bound to ``value`` can hold."""
        if isinstance(value, ast.Call):
            return {"adapters"} if "profile_adapters" in self.doors(value.func) else set()
        if isinstance(value, ast.NamedExpr):
            return self.kinds(value.value)
        if isinstance(value, ast.IfExp):
            return self.kinds(value.body) | self.kinds(value.orelse)
        if isinstance(value, ast.BoolOp):
            return set().union(*(self.kinds(v) for v in value.values))
        if isinstance(value, ast.Name):
            return set(self.lookup(value.id) or ()) | {f"door:{d}" for d in self.doors(value)}
        if isinstance(value, ast.Attribute):
            return {f"door:{d}" for d in self.doors(value)}
        return set()

    def is_adapters(self, value: ast.expr) -> bool:
        return "adapters" in self.kinds(value)

    def _pairs(self, target: ast.expr, value: ast.expr):
        """(name, value) for each name a target binds; a tuple target holding one value binds every name."""
        if isinstance(target, ast.Name):
            yield target.id, value
        elif isinstance(target, ast.Starred):
            yield from self._pairs(target.value, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            paired = isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) and not any(
                isinstance(e, ast.Starred) for e in (*value.elts, *target.elts))
            for i, t in enumerate(target.elts):
                yield from self._pairs(t, value.elts[i] if paired else value)

    def _bound(self, body: list[ast.stmt]):
        """The bindings of one scope, nested scopes left out: (name, value node), or (name, door) for an import."""
        stack = list(reversed(body))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    yield from self._pairs(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                yield from self._pairs(node.target, node.value)
            elif isinstance(node, ast.NamedExpr):
                yield from self._pairs(node.target, node.value)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in DOORS:
                        yield a.asname or a.name, a.name
            stack.extend(reversed(list(ast.iter_child_nodes(node))))

    def _enter(self, kind: str, body: list[ast.stmt], params: tuple[str, ...] = ()) -> None:
        frame = _Scope(kind)
        self.frames.append(frame)
        for name in params:
            frame.names.setdefault(name, set())
        pairs = []
        for name, value in self._bound(body):
            frame.names.setdefault(name, set())
            if isinstance(value, str):   # an imported door
                frame.names[name].add(f"door:{value}")
            else:
                pairs.append((name, value))
        grew = True
        while grew:   # a name bound from another name needs that name known first
            grew = False
            for name, value in pairs:
                kinds = self.kinds(value)
                if not kinds <= frame.names[name]:
                    frame.names[name] |= kinds
                    grew = True

    def visit_Module(self, node: ast.Module) -> None:
        self._enter("module", node.body)
        self.generic_visit(node)
        self.frames.pop()

    def _scoped(self, node) -> None:
        a = getattr(node, "args", None)
        params = () if a is None else tuple(
            x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, *filter(None, (a.vararg, a.kwarg))))
        self.scope.append(node.name)
        self._enter("class" if isinstance(node, ast.ClassDef) else "function", node.body, params)
        self.generic_visit(node)
        self.frames.pop()
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    # --- calls ---
    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self.is_adapters(node.value):
            self.found.append((".".join(self.scope), "profile_adapters", node.slice, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and self.is_adapters(node.func.value):
            self.found.append((".".join(self.scope), "profile_adapters", node.args[0] if node.args else None,
                               node.lineno))
        else:
            for door in sorted(self.doors(node.func) & {"guard_then_spawn", "ProfileRouteAdapter"}):
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

    # --- names bound by an annotated assignment, a named expression or a tuple target ---
    HEAD = "from recursion_core.routes import profile_adapters\n"
    MAP = "profile_adapters(*a, argv=None, driver=None)"

    def test_a_name_bound_by_an_annotated_assignment_holds_the_adapter_map(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            for use in (f"m[{engine!r}].run({{}}, 0)", f"m.get({engine!r}).run({{}}, 0)"):
                source = f"{self.HEAD}def go(*a):\n    m: dict = {self.MAP}\n    {use}\n"
                self.assertEqual(len(self.bad(source)), 1, source)
            module_level = f"{self.HEAD}a = ()\nm: dict = {self.MAP}\ndef go():\n    m[{engine!r}].run({{}}, 0)\n"
            self.assertEqual(len(self.bad(module_level)), 1, module_level)

    def test_a_name_bound_by_a_named_expression_holds_the_adapter_map(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            for body in (
                f"    if (m := {self.MAP}):\n        m[{engine!r}].run({{}}, 0)\n",
                f"    (m := {self.MAP})[{engine!r}].run({{}}, 0)\n",
                f"    print([(m := {self.MAP}), m.get({engine!r})])\n",
            ):
                source = f"{self.HEAD}def go(*a):\n{body}"
                self.assertEqual(len(self.bad(source)), 1, source)

    def test_each_name_of_a_tuple_target_holding_the_adapter_map_binds(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            for line in (f"m, n = {self.MAP}", f"(n, m) = {self.MAP}", f"n, *m = {self.MAP}",
                         f"n, m = 1, {self.MAP}", f"m = n = {self.MAP}", f"n = {self.MAP}\n    m = n",
                         f"m = n\n    n = {self.MAP}"):
                source = f"{self.HEAD}def go(*a):\n    {line}\n    m[{engine!r}].run({{}}, 0)\n"
                self.assertEqual(len(self.bad(source)), 1, source)

    def test_a_name_that_holds_something_else_is_not_the_adapter_map(self) -> None:
        for line in ("m: dict", "m: dict = {}", "m, n = 1, 2"):
            source = f"{self.HEAD}def go(*a):\n    {line}\n    m['pi']\n"
            self.assertEqual(self.bad(source), [], source)
        paired = f"{self.HEAD}def go(*a):\n    n, m = {self.MAP}, 1\n    m['pi']\n"
        self.assertEqual(self.bad(paired), [], paired)

    # --- names are read per scope ---
    def test_a_name_bound_in_one_function_does_not_mark_another_functions_lookup(self) -> None:
        source = (f"{self.HEAD}def first(*a):\n    m = {self.MAP}\n    return m['opencode']\n"
                  "def second(m):\n    return m['pi']\n"
                  "def third():\n    return m['pi']\n")
        self.assertEqual(self.bad(source), [])

    def test_a_function_defined_above_its_module_level_map_is_checked(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            source = (f"{self.HEAD}def go():\n    return ADAPTERS[{engine!r}]\n"
                      f"def go_get():\n    return ADAPTERS.get({engine!r})\n"
                      f"ADAPTERS = profile_adapters(None, None, argv=None, driver=None)\n")
            self.assertEqual(len(self.bad(source)), 2, source)

    def test_a_name_a_function_binds_itself_hides_the_module_level_map(self) -> None:
        source = (f"{self.HEAD}ADAPTERS = profile_adapters(None, None, argv=None, driver=None)\n"
                  "def go():\n    ADAPTERS = {}\n    return ADAPTERS['pi']\n")
        self.assertEqual(self.bad(source), [])

    def test_a_parameter_hides_the_module_level_map(self) -> None:
        source = (f"{self.HEAD}ADAPTERS = profile_adapters(None, None, argv=None, driver=None)\n"
                  "def go(ADAPTERS):\n    return ADAPTERS['pi']\n")
        self.assertEqual(self.bad(source), [])

    def test_a_function_inside_a_function_sees_the_outer_functions_map(self) -> None:
        source = (f"{self.HEAD}def outer(*a):\n    m = {self.MAP}\n    def inner():\n        return m['pi']\n"
                  "    return inner\n")
        self.assertEqual(len(self.bad(source)), 1)

    def test_a_class_bodys_name_is_not_seen_inside_its_methods(self) -> None:
        source = (f"{self.HEAD}class K:\n    m = profile_adapters(None, None, argv=None, driver=None)\n"
                  "    def go(self):\n        return m['pi']\n")
        self.assertEqual(self.bad(source), [])

    # --- an assignment that names a door makes the target an alias of it ---
    START = "from recursion_core.subscription_routes import guard_then_spawn\n"

    def test_a_name_assigned_a_door_is_an_alias_of_that_door(self) -> None:
        for engine in DETECTED_NOT_ROUTED:
            call = f"start(l, ['x'], {engine!r}, {{}}, {{}})"
            for source in (
                f"{self.START}def go(l):\n    start = guard_then_spawn\n    {call}\n",
                f"import recursion_core.subscription_routes as sr\ndef go(l):\n    start = sr.guard_then_spawn\n    {call}\n",
                f"{self.START}start = guard_then_spawn\ndef go(l):\n    {call}\n",
                f"{self.START}def go(l):\n    first = guard_then_spawn\n    start = first\n    {call}\n",
                f"{self.START}def go(l):\n    start: object = guard_then_spawn\n    {call}\n",
                f"{self.START}def go(l):\n    (start := guard_then_spawn)\n    {call}\n",
                f"{self.START}def go(l, c):\n    start = guard_then_spawn if c else None\n    {call}\n",
                f"{self.START}def go(l, c):\n    start = c or guard_then_spawn\n    {call}\n",
            ):
                self.assertEqual(len(self.bad(source)), 1, source)
            klass = ("from recursion_core.routes import ProfileRouteAdapter\n"
                     f"def go(*a):\n    make = ProfileRouteAdapter\n    make({engine!r}, *a, argv=None, driver=None)\n")
            self.assertEqual(len(self.bad(klass)), 1, klass)
            maker = (f"{self.HEAD}def go(*a):\n    make = profile_adapters\n    make(*a, argv=None, driver=None)[{engine!r}]\n")
            self.assertEqual(len(self.bad(maker)), 1, maker)

    def test_an_alias_is_read_per_scope(self) -> None:
        source = (f"{self.START}def first(l):\n    start = guard_then_spawn\n    start(l, ['x'], 'opencode', {{}}, {{}})\n"
                  "def second(l, start):\n    start(l, ['x'], 'pi', {}, {})\n"
                  "def third(l):\n    start(l, ['x'], 'pi', {}, {})\n")
        self.assertEqual(self.bad(source), [])

    def test_an_alias_with_a_routed_engine_passes(self) -> None:
        source = f"{self.START}def go(l):\n    start = guard_then_spawn\n    start(l, ['x'], 'opencode', {{}}, {{}})\n"
        self.assertEqual(self.bad(source), [])


if __name__ == "__main__":
    unittest.main()
