"""The eleven hostile-reading labels stay: one in the original method, one per other gate file.

Each moved file keeps the builder that left, one timed method, the clock read, and the
100000-unit sample. A label match alone does not keep the shape.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORIGINAL = (
    "one long spaced item",
    "one long space run",
    "one long BOM run",
    "BOM and star",
    "fullwidth",
    "fullwidth spaced",
    "dashes",
    "dashes in a range",
    "spaced dashes",
    "many ranges",
    "mixed",
)


def _kept_labels() -> list[str]:
    tree = ast.parse((HERE / "test_no_proxy_readings.py").read_text(encoding="utf-8"))
    method = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ReadingTimeTests"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "test_hostile_values_are_decided_in_bounded_time"
    )
    loops = [node for node in ast.walk(method) if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)]
    if len(loops) != 1:
        raise AssertionError(f"expected one tuple loop, found {len(loops)}")
    labels = []
    for elt in loops[0].iter.elts:
        label = elt.elts[0]
        if not isinstance(label, ast.Constant) or not isinstance(label.value, str):
            raise AssertionError("shape label is not a string")
        labels.append(label.value)
    return labels


def _moved_labels() -> list[str]:
    labels = []
    for path in sorted(HERE.glob("test_no_proxy_hostile_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "LABEL" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        if len(found) != 1:
            raise AssertionError(f"{path.name} has {found!r}")
        labels.append(found[0])
    return labels


# ast.unparse of each moved ``_make``. A label match alone does not keep the shape.
MAKE_BY_LABEL = {
    "one long space run": "def _make(n: int) -> str:\n    return 'a' + ' ' * n + 'b'",
    "one long BOM run": "def _make(n: int) -> str:\n    return 'a' + '\\ufeff' * n + 'b'",
    "BOM and star": "def _make(n: int) -> str:\n    return '\\ufeff*a' * (n // 3) + 'q'",
    "fullwidth": "def _make(n: int) -> str:\n    return 'ａ' * n + '．example'",
    "fullwidth spaced": "def _make(n: int) -> str:\n    return 'ａ ' * (n // 2)",
    "dashes": "def _make(n: int) -> str:\n    return '-' * n",
    "dashes in a range": "def _make(n: int) -> str:\n    return '10.0.0.0-' + '-' * n + '10.0.0.1'",
    "spaced dashes": "def _make(n: int) -> str:\n    return '1.0.0.0 - ' * (n // 10)",
    "many ranges": (
        "def _make(n: int) -> str:\n"
        "    return ','.join((f'10.{i // 256}.{i % 256}.1-10.{i // 256}.{i % 256}.254' for i in range(n // 25)))"
    ),
    "mixed": "def _make(n: int) -> str:\n    return '\\ufeff ．*.\\xad-1 0.0.0.0/1 ,' * (n // 20)",
}
KEPT_MAKE = "lambda n: '*.a ' * (n // 8) + 'q'"


def _calls_timed(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_timed":
            return True
        if isinstance(func, ast.Name) and func.id == "_timed":
            return True
    return False


def hostile_faults(source: str) -> list[str]:
    """What is wrong with one hostile-shape module. Empty means the shape is the one that moved."""
    faults: list[str] = []
    tree = ast.parse(source)
    labels = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "LABEL" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(labels) != 1:
        return [f"label count {len(labels)}"]
    label = labels[0]
    makes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_make"]
    if len(makes) != 1:
        faults.append(f"_make count {len(makes)}")
    else:
        expected = MAKE_BY_LABEL.get(label)
        rendered = ast.unparse(makes[0])
        if expected is None:
            faults.append(f"unknown label {label}")
        elif rendered != expected:
            faults.append("make does not match the moved shape")
    timed_fns: list[ast.FunctionDef] = []
    timed_tests: list[str] = []
    saw_unit = False
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for item in cls.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name == "_timed":
                timed_fns.append(item)
            if item.name.startswith("test_") and _calls_timed(item):
                timed_tests.append(item.name)
                if any(isinstance(node, ast.Constant) and node.value == 100_000 for node in ast.walk(item)):
                    saw_unit = True
    if len(timed_fns) != 1 or not any(
        isinstance(node, ast.Attribute) and node.attr == "perf_counter" for node in ast.walk(timed_fns[0])
    ):
        faults.append("the clock read is missing")
    if len(timed_tests) != 1:
        faults.append(f"timed tests {timed_tests}")
    if not saw_unit:
        faults.append("unit is not 100000")
    return faults


class HostileShapePartitionTests(unittest.TestCase):
    def test_moved_labels_match_the_original_eleven(self) -> None:
        kept = _kept_labels()
        moved = _moved_labels()
        self.assertEqual(kept + moved, list(ORIGINAL))
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(set(ORIGINAL)), 11)

    def test_kept_shape_is_the_spaced_item(self) -> None:
        tree = ast.parse((HERE / "test_no_proxy_readings.py").read_text(encoding="utf-8"))
        method = next(
            item
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ReadingTimeTests"
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "test_hostile_values_are_decided_in_bounded_time"
        )
        lambdas = [ast.unparse(node) for node in ast.walk(method) if isinstance(node, ast.Lambda)]
        self.assertIn(KEPT_MAKE, lambdas)

    def test_each_moved_shape_matches_its_pin(self) -> None:
        renders = []
        for path in sorted(HERE.glob("test_no_proxy_hostile_*.py")):
            source = path.read_text(encoding="utf-8")
            faults = hostile_faults(source)
            self.assertEqual(faults, [], path.name)
            makes = [
                node
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "_make"
            ]
            renders.append(ast.unparse(makes[0]))
        self.assertEqual(len(renders), len(set(renders)))

    def test_a_replaced_make_is_a_fault(self) -> None:
        source = (HERE / "test_no_proxy_hostile_07_dashes.py").read_text(encoding="utf-8")
        broken = source.replace('return "-" * n', 'return "a"', 1)
        self.assertNotEqual(source, broken)
        self.assertIn("make does not match the moved shape", hostile_faults(broken))

    def test_a_second_timed_method_is_a_fault(self) -> None:
        source = (HERE / "test_no_proxy_hostile_07_dashes.py").read_text(encoding="utf-8")
        extra = source.replace(
            "    def test_hostile_value_is_decided_in_bounded_time",
            "    def test_a_second_shape(self) -> None:\n        self._timed(lambda: None)\n\n"
            "    def test_hostile_value_is_decided_in_bounded_time",
            1,
        )
        self.assertNotEqual(source, extra)
        ast.parse(extra)
        self.assertTrue(any(fault.startswith("timed tests") for fault in hostile_faults(extra)))

    def test_a_neutered_clock_and_a_smaller_unit_are_faults(self) -> None:
        source = (HERE / "test_no_proxy_hostile_07_dashes.py").read_text(encoding="utf-8")
        neutered = source.replace("time.perf_counter()", "0.0")
        smaller = source.replace("100_000", "10", 1)
        self.assertNotEqual(source, neutered)
        self.assertNotEqual(source, smaller)
        self.assertIn("the clock read is missing", hostile_faults(neutered))
        self.assertIn("unit is not 100000", hostile_faults(smaller))


if __name__ == "__main__":
    unittest.main()
