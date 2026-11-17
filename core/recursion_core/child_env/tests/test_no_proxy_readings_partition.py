"""The eleven hostile-reading labels stay: one in the original method, one per other gate file."""

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


class HostileShapePartitionTests(unittest.TestCase):
    def test_moved_labels_match_the_original_eleven(self) -> None:
        kept = _kept_labels()
        moved = _moved_labels()
        self.assertEqual(kept + moved, list(ORIGINAL))
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(set(ORIGINAL)), 11)


if __name__ == "__main__":
    unittest.main()
