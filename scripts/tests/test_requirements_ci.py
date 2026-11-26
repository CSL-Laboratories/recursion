"""requirements-ci.txt keeps the signature backend, so the tests that sign a plugin package run on the hosted jobs.

The hosted jobs install only requirements-ci.txt. Without ``cryptography`` there, every test that
signs or checks a plugin package skips on them. The file is pinned by hash, and pip's
hash-checking mode refuses the whole install when any line lacks an exact pin or a hash, so each
line is checked for both. The pinned backend must also meet the floor the product itself names in
core/recursion_core/plugin_security/requirements.txt.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_CI = ROOT / "requirements-ci.txt"
PRODUCT_REQUIREMENTS = ROOT / "core" / "recursion_core" / "plugin_security" / "requirements.txt"
BACKEND = "cryptography"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][^\s;\\]*)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?=\s|$)")
_FLOOR = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*>=\s*([0-9][^\s;,]*)")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def logical_lines(text: str) -> list[str]:
    """The requirement lines of a pip requirements file: comments dropped, continuation lines joined."""
    lines: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending.append(line[:-1].strip())
            continue
        pending.append(line)
        lines.append(" ".join(part for part in pending if part))
        pending = []
    if pending:
        lines.append(" ".join(part for part in pending if part))
    return lines


def parse(text: str) -> dict[str, dict[str, object]]:
    """{normalized name: {"pin": version or None, "hashes": [...], "line": text}} for each requirement line."""
    found: dict[str, dict[str, object]] = {}
    for line in logical_lines(text):
        pin = _PIN.match(line)
        name_match = _NAME.match(line)
        name = _normalized(pin.group(1) if pin else name_match.group(0) if name_match else line)
        if name in found:
            raise AssertionError(f"requirements-ci.txt names {name} twice; keep one line per package")
        found[name] = {"pin": pin.group(2) if pin else None, "hashes": _HASH.findall(line), "line": line}
    return found


class RequirementsCiKeepsTheSignatureBackend(unittest.TestCase):
    def setUp(self) -> None:
        self.requirements = parse(REQUIREMENTS_CI.read_text(encoding="utf-8"))

    def test_the_signature_backend_is_pinned_with_its_hashes(self) -> None:
        entry = self.requirements.get(BACKEND)
        self.assertIsNotNone(
            entry,
            "requirements-ci.txt has no cryptography line, so the hosted jobs skip every test that signs a "
            "plugin package. Add cryptography==<version> with the --hash=sha256 lines of that release.",
        )
        self.assertIsNotNone(
            entry["pin"],
            f"requirements-ci.txt names cryptography without an exact version ({entry['line']!r}). "
            "Pin it with == so every hosted job installs the same backend.",
        )
        self.assertTrue(
            entry["hashes"],
            "requirements-ci.txt pins cryptography without a --hash=sha256 line. "
            "Add the sha256 of each file the package index serves for that release.",
        )

    def test_the_pinned_backend_meets_the_floor_the_product_names(self) -> None:
        entry = self.requirements.get(BACKEND)
        self.assertIsNotNone(entry, "requirements-ci.txt has no cryptography line; see the test above")
        floors = {_normalized(m.group(1)): m.group(2)
                  for m in map(_FLOOR.match, logical_lines(PRODUCT_REQUIREMENTS.read_text(encoding="utf-8"))) if m}
        self.assertIn(BACKEND, floors, "plugin_security/requirements.txt no longer names a cryptography floor")
        self.assertIsNotNone(entry["pin"], "requirements-ci.txt names cryptography without an exact version")
        self.assertGreaterEqual(
            _version(str(entry["pin"])), _version(floors[BACKEND]),
            f"requirements-ci.txt pins cryptography {entry['pin']}, below the {floors[BACKEND]} the product "
            "requires. Pin a release at or above the product's floor.",
        )

    def test_every_line_is_pinned_and_hashed_so_hash_checking_mode_installs_it(self) -> None:
        self.assertIn("jsonschema", self.requirements, "requirements-ci.txt lost jsonschema, which the M1 schema tests import")
        for name, entry in sorted(self.requirements.items()):
            with self.subTest(package=name):
                self.assertIsNotNone(
                    entry["pin"],
                    f"requirements-ci.txt names {name} without an exact == version ({entry['line']!r}); pip's "
                    "hash-checking mode refuses the whole install then. Pin it with == and its hashes.",
                )
                self.assertTrue(
                    entry["hashes"],
                    f"requirements-ci.txt pins {name} without a --hash=sha256 line; pip's hash-checking mode "
                    "refuses the whole install then. Add the sha256 of each file of that release.",
                )


class TheParserReadsContinuationLines(unittest.TestCase):
    def test_joined_lines_comments_and_names(self) -> None:
        text = (
            "# a comment\n\nAlpha_Pkg==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + " \\\n"
            "    --hash=sha256:" + "b" * 64 + "\nbeta>=2\n"
        )
        parsed = parse(text)
        self.assertEqual(sorted(parsed), ["alpha-pkg", "beta"])
        self.assertEqual(parsed["alpha-pkg"]["pin"], "1.2.3")
        self.assertEqual(parsed["alpha-pkg"]["hashes"], ["a" * 64, "b" * 64])
        self.assertIsNone(parsed["beta"]["pin"])
        self.assertEqual(parsed["beta"]["hashes"], [])

    def test_a_short_hash_is_not_a_hash(self) -> None:
        self.assertEqual(parse("gamma==1 --hash=sha256:abc\n")["gamma"]["hashes"], [])


if __name__ == "__main__":
    unittest.main()
