#!/usr/bin/env python3
"""Tests for the CI hash lock (tools/release/hash_lock.py). Run this file from the root:

    python -X utf8 -B tools/release/hash_lock_checks.py

It is named hash_lock_checks.py until tools/release/proposals/ci-gate-release-tests.patch
is applied: the frozen gate (tools/ci_gate.py) fails on a test_*.py file outside its globs,
and tools/release/ is not among them. That patch adds the glob and renames this file to
tools/release/test_hash_lock.py, so hosted CI runs it from then on. Stdlib only, offline.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hash_lock as hl  # noqa: E402

ROOT = HERE.parents[1]
CI = "requirements-ci.txt"
THREADED = "tools/release/requirements-ci-314t.txt"


class Tree:
    """A scratch copy of everything the check reads, to break one thing at a time."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="hash-lock-checks-")
        self.root = Path(self._dir.name)
        paths = {CI, hl.GATE, hl.RECORD}
        paths |= {p.relative_to(ROOT).as_posix() for p in (ROOT / hl.WORKFLOWS).glob("*.y*ml")}
        paths |= {p.relative_to(ROOT).as_posix() for p in (ROOT / "tools/release").rglob("*") if p.is_file()}
        for patch in (ROOT / hl.PROPOSALS).glob("*.patch"):
            paths |= set(hl.patch_paths(patch.read_text(encoding="utf-8")))
        hl.lf_copy(ROOT, sorted(paths), self.root)

    def close(self) -> None:
        self._dir.cleanup()

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_text(text, encoding="utf-8", newline="\n")

    def record(self) -> dict:
        return json.loads(self.read(hl.RECORD))

    def write_record(self, record: dict) -> None:
        self.write(hl.RECORD, json.dumps(record, indent=2, sort_keys=True) + "\n")

    def problems(self) -> list[str]:
        return hl.check(self.root).problems


class TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = Tree()
        self.addCleanup(self.tree.close)

    def assertProblem(self, pattern: str) -> None:
        problems = self.tree.problems()
        self.assertTrue(any(re.search(pattern, p) for p in problems),
                        f"no problem matches {pattern!r}; problems: {problems}")


def drop_hash(text: str, sha: str) -> str:
    """Remove one --hash continuation line and keep the backslashes consistent."""
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if sha in line)
    if not lines[index].rstrip().endswith("\\"):  # the pin's last hash: the line before ends the pin now
        lines[index - 1] = lines[index - 1].rstrip().removesuffix("\\").rstrip()
    del lines[index]
    return "\n".join(lines) + "\n"


def dist(record: dict, rel: str, pin: str, needle: str) -> dict:
    return next(d for d in record["files"][rel]["pins"][pin]["dists"] if needle in d["filename"])


class CommittedLockTests(unittest.TestCase):
    def test_the_committed_files_pass(self) -> None:
        report = hl.check(ROOT)
        self.assertEqual(report.problems, [])
        self.assertEqual(report.files, 2)

    def test_every_ci_target_is_checked(self) -> None:
        targets = {str(t) for t in hl.check(ROOT).targets}
        self.assertTrue({"ubuntu-24.04/python 3.12", "windows-2025/python 3.12",
                         "macos-latest/python 3.12", "ubuntu-24.04/python 3.14t"} <= targets, targets)

    def test_the_real_installs_are_found(self) -> None:
        installs, problems = hl.all_installs(ROOT)
        self.assertEqual(problems, [])
        found = {(i.source.split(" ")[0], i.job, i.file) for i in installs}
        for job in ("linux-tests", "windows", "macos"):
            self.assertIn(("ci.yml", job, CI), found)
        self.assertIn(("ci-macos-nightly.yml", "macos", CI), found)
        self.assertIn(("ci-full-matrix.yml", "windows", CI), found)
        self.assertIn(("ci.yml", "core-314t", THREADED), found)  # through proposals/ci-314t.patch


class RemovedHashTests(TreeCase):
    def test_one_hash_removed_from_the_requirements_file_fails(self) -> None:
        wheel = dist(self.tree.record(), CI, "rpds-py", "cp312-cp312-win_amd64")
        self.tree.write(CI, drop_hash(self.tree.read(CI), wheel["sha256"]))
        self.assertProblem(r"rpds-py hashes differ from the record \(missing for \['rpds_py-.*-cp312-cp312-win_amd64\.whl'\]")

    def test_one_hash_removed_from_both_file_and_record_fails_on_its_platform(self) -> None:
        record = self.tree.record()
        wheel = dist(record, CI, "rpds-py", "cp312-cp312-macosx_11_0_arm64")
        record["files"][CI]["pins"]["rpds-py"]["dists"].remove(wheel)
        self.tree.write_record(record)
        self.tree.write(CI, drop_hash(self.tree.read(CI), wheel["sha256"]))
        self.assertProblem(r"rpds-py==\S+ on macos-latest/python 3\.12: no hashed wheel installs there")
        self.assertFalse(any("ubuntu" in p for p in self.tree.problems()))

    def test_the_last_hash_of_a_pin_removed_fails(self) -> None:
        text = self.tree.read(CI)
        text = re.sub(r"(attrs==\S+) \\\n(    --hash=\S+ \\\n)*    --hash=\S+\n", r"\1\n", text)
        self.assertIn("attrs==26.1.0\n", text)
        self.tree.write(CI, text)
        self.assertProblem(r"attrs==26\.1\.0 has no --hash")

    def test_a_hash_not_in_the_record_fails(self) -> None:
        text = self.tree.read(CI).replace("attrs==26.1.0 \\\n", "attrs==26.1.0 \\\n    --hash=sha256:" + "0" * 64 + " \\\n")
        self.tree.write(CI, text)
        self.assertProblem(r"attrs hashes differ from the record \(missing for \[\]; not recorded: \['0{64}'\]")


class UnpinnedLineTests(TreeCase):
    def replace_attrs(self, new_spec: str) -> None:
        text = self.tree.read(CI)
        self.assertIn("attrs==26.1.0 \\\n", text)
        self.tree.write(CI, text.replace("attrs==26.1.0 \\\n", new_spec + " \\\n"))

    def test_a_range_instead_of_a_pin_fails(self) -> None:
        self.replace_attrs("attrs>=26")
        self.assertProblem(r"requirements-ci\.txt: line \d+: not an exact pin \(name==version\): 'attrs>=26'")

    def test_a_wildcard_pin_fails(self) -> None:
        self.replace_attrs("attrs==26.*")
        self.assertProblem(r"not an exact pin")

    def test_a_bare_name_fails(self) -> None:
        self.replace_attrs("attrs")
        self.assertProblem(r"not an exact pin")

    def test_a_direct_url_fails(self) -> None:
        self.replace_attrs("attrs @ https://example.invalid/attrs-26.1.0-py3-none-any.whl")
        self.assertProblem(r"not an exact pin")

    def test_an_unhashed_extra_line_fails(self) -> None:
        self.tree.write(CI, self.tree.read(CI) + "requests==2.32.3\n")
        self.assertProblem(r"requests==2\.32\.3 has no --hash")

    def test_pip_options_fail(self) -> None:
        for line in ("-r other.txt", "--index-url https://example.invalid/simple", "-e .",
                     "--extra-index-url https://example.invalid", "--no-binary :all:", "-c constraints.txt"):
            with self.subTest(line=line):
                self.tree.write(CI, line + "\n" + ROOT.joinpath(CI).read_text(encoding="utf-8"))
                self.assertProblem(r"pip option not allowed in a locked file")

    def test_a_duplicate_pin_fails(self) -> None:
        text = self.tree.read(CI)
        block = re.search(r"attrs==26\.1\.0 \\\n(?:    --hash=\S+ \\\n)*    --hash=\S+\n", text).group(0)
        self.tree.write(CI, text + block.replace("attrs", "Attrs"))
        self.assertProblem(r"attrs is pinned twice")

    def test_a_weak_or_malformed_hash_fails(self) -> None:
        for bad in ("md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64):
            with self.subTest(bad=bad):
                self.tree.write(CI, ROOT.joinpath(CI).read_text(encoding="utf-8").replace(
                    "attrs==26.1.0 \\\n", f"attrs==26.1.0 --hash={bad} \\\n"))
                self.assertProblem(r"unsupported hash")

    def test_a_comment_inside_a_continued_pin_fails(self) -> None:
        self.tree.write(CI, self.tree.read(CI).replace("attrs==26.1.0 \\\n", "attrs==26.1.0 \\\n# note\n"))
        self.assertProblem(r"a comment inside a continued requirement")

    def test_a_version_differing_from_the_record_fails(self) -> None:
        self.replace_attrs("attrs==26.1.1")
        self.assertProblem(r"attrs==26\.1\.1 ; None differs from the record \(26\.1\.0 ; None\)")


class CompletenessTests(TreeCase):
    def drop_pin(self, rel: str, name: str) -> None:
        text = self.tree.read(rel)
        new = re.sub(rf"(?m)^{re.escape(name)}==\S+(?: ;[^\\\n]*)? \\\n(?:    --hash=\S+ \\\n)*    --hash=\S+\n", "", text)
        self.assertNotEqual(new, text)
        self.tree.write(rel, new)
        record = self.tree.record()
        del record["files"][rel]["pins"][hl.canonical(name)]
        self.tree.write_record(record)

    def test_a_missing_dependency_fails_where_it_applies(self) -> None:
        self.drop_pin(CI, "typing-extensions")  # referencing needs it below Python 3.13 only
        self.assertProblem(r"referencing==\S+ on ubuntu-24\.04/python 3\.12: needs 'typing-extensions>=4\.4\.0; "
                           r"python_version < \"3\.13\"', which the file does not pin there")

    def test_a_missing_dependency_of_the_threaded_lock_fails(self) -> None:
        self.drop_pin(THREADED, "pycparser")
        self.assertProblem(r"cffi==\S+ on ubuntu-24\.04/python 3\.14t: needs 'pycparser")

    def test_a_dependency_pinned_too_low_fails(self) -> None:
        record = self.tree.record()
        record["files"][CI]["pins"]["jsonschema"]["requires_dist"].append("attrs>=99")
        self.tree.write_record(record)
        self.assertProblem(r"needs 'attrs>=99'; the file pins attrs==26\.1\.0")

    def test_a_release_that_refuses_the_python_fails(self) -> None:
        record = self.tree.record()
        record["files"][THREADED]["pins"]["cffi"]["requires_python"] = ">=3.15"
        self.tree.write_record(record)
        self.assertProblem(r"cffi==\S+ on ubuntu-24\.04/python 3\.14t: requires Python >=3\.15")


class WorkflowScanTests(TreeCase):
    WORKFLOW = (".github/workflows/extra.yml", "name: extra\non: push\njobs:\n  build:\n    runs-on: {runner}\n"
                "    steps:\n      - uses: actions/setup-python@v5\n        with:\n          python-version: \"{python}\"\n"
                "      - run: {command}\n")

    def add_workflow(self, command: str, runner: str = "ubuntu-24.04", python: str = "3.12") -> None:
        rel, template = self.WORKFLOW
        self.tree.write(rel, template.format(runner=runner, python=python, command=command))

    def test_installing_an_unlocked_file_fails(self) -> None:
        self.add_workflow("python -m pip install -r requirements-dev.txt")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\] but is not hash-locked")

    def test_installing_a_bare_package_fails(self) -> None:
        self.add_workflow("python -m pip install requests")
        self.assertProblem(r"extra\.yml job build: installs 'requests', which is not a hash-locked requirements file")

    def test_an_installer_the_check_cannot_read_fails(self) -> None:
        self.add_workflow("uv sync")
        self.assertProblem(r"extra\.yml job build: an installer this check does not read")

    def test_an_unknown_runner_fails(self) -> None:
        self.add_workflow("python -m pip install -r requirements-ci.txt", runner="self-hosted")
        self.assertProblem(r"runner 'self-hosted' is not in RUNNERS")

    def test_a_new_python_without_wheels_fails(self) -> None:
        self.add_workflow("python -m pip install -r requirements-ci.txt", python="3.10")
        self.assertProblem(r"rpds-py==\S+ on ubuntu-24\.04/python 3\.10: requires Python >=3\.11")

    def test_an_index_option_on_the_command_fails(self) -> None:
        self.add_workflow("python -m pip install --extra-index-url https://example.invalid -r requirements-ci.txt")
        self.assertProblem(r"pip option '--extra-index-url' is not allowed")

    def test_the_gate_starting_to_install_fails(self) -> None:
        self.tree.write(hl.GATE, self.tree.read(hl.GATE) + "\n# python -m pip install requests\n")
        self.assertProblem(r"tools/ci_gate\.py now installs something")

    def test_a_locked_file_nothing_installs_fails(self) -> None:
        unused = "tools/release/requirements-unused.txt"
        self.tree.write(unused, self.tree.read(THREADED))
        record = self.tree.record()
        record["files"][unused] = record["files"][THREADED]
        self.tree.write_record(record)
        self.assertProblem(r"locks tools/release/requirements-unused\.txt, which no workflow or proposal installs")

    def test_a_proposal_that_no_longer_applies_fails(self) -> None:
        ci = ".github/workflows/ci.yml"
        self.tree.write(ci, self.tree.read(ci).replace("  linux:\n    name: linux\n", "  linux:\n    name: linux (moved)\n"))
        self.assertProblem(r"ci-314t\.patch neither applies to the tree nor is applied in it")

    def test_an_applied_proposal_is_read_from_the_tree(self) -> None:
        patch = self.tree.root / hl.PROPOSALS / "ci-314t.patch"
        if hl.proposal_state(self.tree.root, patch) == "applies":  # not yet applied in the repository
            result = hl.git_apply(self.tree.root, patch)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hl.proposal_state(self.tree.root, patch), "applied")
        self.assertEqual(self.tree.problems(), [])
        installs, _ = hl.all_installs(self.tree.root)
        self.assertIn(("ci.yml", "core-314t", THREADED), {(i.source, i.job, i.file) for i in installs})


class WheelTagTests(unittest.TestCase):
    T312 = hl.Target("ubuntu-24.04", "3.12")
    W312 = hl.Target("windows-2025", "3.12")
    M312 = hl.Target("macos-latest", "3.12")
    T314T = hl.Target("ubuntu-24.04", "3.14t")

    def test_platform_wheels(self) -> None:
        cases = [
            ("x-1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", self.T312, True),
            ("x-1-cp312-cp312-manylinux_2_39_x86_64.whl", self.T312, True),
            ("x-1-cp312-cp312-manylinux_2_40_x86_64.whl", self.T312, False),
            ("x-1-cp312-cp312-musllinux_1_2_x86_64.whl", self.T312, False),
            ("x-1-cp312-cp312-manylinux_2_17_aarch64.whl", self.T312, False),
            ("x-1-cp312-cp312-win_amd64.whl", self.W312, True),
            ("x-1-cp312-cp312-win32.whl", self.W312, False),
            ("x-1-cp312-cp312-macosx_11_0_arm64.whl", self.M312, True),
            ("x-1-cp312-cp312-macosx_10_12_x86_64.whl", self.M312, False),
            ("x-1-cp312-cp312-macosx_10_9_universal2.whl", self.M312, True),
            ("x-1-cp312-cp312-macosx_15_0_arm64.whl", self.M312, False),  # floor 14.0, on purpose
            ("x-1-cp313-cp313-manylinux_2_17_x86_64.whl", self.T312, False),
            ("x-1-cp39-abi3-manylinux_2_17_x86_64.whl", self.T312, True),
            ("x-1-cp39-abi3-manylinux_2_17_x86_64.whl", self.T314T, False),
            ("x-1-cp314-cp314t-manylinux_2_17_x86_64.whl", self.T314T, True),
            ("x-1-cp314-cp314-manylinux_2_17_x86_64.whl", self.T314T, False),
            ("x-1-cp314-cp314t-manylinux_2_17_x86_64.whl", self.T312, False),
            ("x-1-cp315-abi3.abi3t-manylinux_2_17_x86_64.whl", self.T314T, False),
            ("x-1-py3-none-any.whl", self.T314T, True),
            ("x-1-py2.py3-none-any.whl", self.W312, True),
            ("x-1-1build-py3-none-any.whl", self.M312, True),
            ("x-1.tar.gz", self.T312, False),
        ]
        for filename, target, expected in cases:
            with self.subTest(filename=filename, target=str(target)):
                self.assertEqual(hl.wheel_installs(filename, target), expected)


class MarkerAndSpecifierTests(unittest.TestCase):
    def test_markers(self) -> None:
        env312 = hl.Target("ubuntu-24.04", "3.12").env()
        env314 = hl.Target("windows-2025", "3.14t").env()
        self.assertTrue(hl.marker_applies('python_version < "3.13"', env312))
        self.assertFalse(hl.marker_applies('python_version < "3.13"', env314))
        self.assertTrue(hl.marker_applies("implementation_name != 'PyPy' and os_name == 'posix'", env312))
        self.assertFalse(hl.marker_applies("implementation_name != 'PyPy' and os_name == 'posix'", env314))
        self.assertTrue(hl.marker_applies('sys_platform == "win32" or python_full_version < "3.11"', env314))
        self.assertTrue(hl.marker_applies('"3.11" <= python_version', env312))
        self.assertFalse(hl.marker_applies('extra == "ssh"', env312))
        self.assertTrue(hl.marker_applies(None, env312))

    def test_markers_this_check_cannot_read_are_refused(self) -> None:
        for marker in ('python_version in "3.12 3.13"', 'platform_release == "6"', "python_version <",
                       'os_name > "nt"', '(python_version < "3.13"'):
            with self.subTest(marker=marker), self.assertRaises(hl.LockError):
                hl.marker_applies(marker, hl.Target("ubuntu-24.04", "3.12").env())

    def test_specifiers(self) -> None:
        self.assertTrue(hl.satisfies("3.12.0", ">=3.9, !=3.9.0, !=3.9.1"))
        self.assertFalse(hl.satisfies("3.9.0", ">=3.9,!=3.9.0"))
        self.assertTrue(hl.satisfies("2.0.5", "~=2.0"))
        self.assertFalse(hl.satisfies("3.0", "~=2.0"))
        self.assertTrue(hl.satisfies("3.12.0", "==3.*"))
        self.assertFalse(hl.satisfies("3.12.0", "!=3.12.*"))
        self.assertTrue(hl.satisfies("26.1.0", ""))
        with self.assertRaises(hl.LockError):
            hl.satisfies("1.0", ">=1.0rc1")
        with self.assertRaises(hl.LockError):
            hl.parse_version("1.0.post1")


if __name__ == "__main__":
    unittest.main()
