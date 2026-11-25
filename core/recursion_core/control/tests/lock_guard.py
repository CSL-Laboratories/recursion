"""A static guard for an order lock: what runs inside ``with self.<lock>:`` blocks of one class.

A lock that a stop takes must hold nothing but its own state, so that how long a stop waits never depends on
code the lock's owner does not control: a log handler, a formatter, a callback. ``under_lock`` reads the
class's own source (or a source text given to it), finds every ``with self.<lock>:`` block in its methods and
names what each block does beyond reading and writing names and attributes:

- every call whose callee, written as source (``self._authorizer.attach``), is not in ``allowed``;
- every formatted string (an f-string, or ``%`` on a string constant), which can call ``__format__``,
  ``__str__`` or ``__repr__`` of a value.

It sees what the source spells out. A property or an operator method of a value read under the lock runs code
too; the guard does not follow those, so a lock's blocks should read only plain state of their own class.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import NamedTuple


class Finding(NamedTuple):
    method: str        # the method that holds the block
    line: int          # the line of the finding, counted in the source the guard parsed
    what: str          # "call" or "format"
    source: str        # the callee, or the formatted expression, as source text


class Report(NamedTuple):
    blocks: tuple[tuple[str, int], ...]   # (method, line) of every ``with self.<lock>:`` block found
    findings: tuple[Finding, ...]


def class_tree(cls) -> ast.ClassDef:
    """The class's own source, parsed: the one ``ClassDef`` it holds."""

    return class_tree_from_source(inspect.getsource(cls))


def class_tree_from_source(source: str) -> ast.ClassDef:
    tree = ast.parse(textwrap.dedent(source))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        raise ValueError("the source must hold exactly one class")
    return classes[0]


def _is_lock(expr: ast.expr, lock: str) -> bool:
    return (isinstance(expr, ast.Attribute) and expr.attr == lock
            and isinstance(expr.value, ast.Name) and expr.value.id == "self")


def lock_blocks(node: ast.ClassDef, lock: str):
    """Every (method name, ``With`` node) of the class whose items take ``self.<lock>``."""

    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(method):
            if isinstance(inner, (ast.With, ast.AsyncWith)) and any(_is_lock(item.context_expr, lock)
                                                                    for item in inner.items):
                yield method.name, inner


def under_lock(node: ast.ClassDef, lock: str, allowed: frozenset[str]) -> Report:
    """What the class's ``with self.<lock>:`` blocks do beyond their own state, as a ``Report``."""

    blocks, findings = [], []
    for name, block in lock_blocks(node, lock):
        blocks.append((name, block.lineno))
        for statement in block.body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call):
                    callee = ast.unparse(inner.func)
                    if callee not in allowed:
                        findings.append(Finding(name, inner.lineno, "call", callee))
                elif isinstance(inner, ast.JoinedStr):
                    findings.append(Finding(name, inner.lineno, "format", ast.unparse(inner)))
                elif (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Mod)
                      and isinstance(inner.left, ast.Constant) and isinstance(inner.left.value, str)):
                    findings.append(Finding(name, inner.lineno, "format", ast.unparse(inner)))
    return Report(tuple(blocks), tuple(findings))
