#!/usr/bin/env python3
"""Tests for the CI hash lock (tools/release/hash_lock.py). Run this file from the root:

    python -X utf8 -B tools/release/hash_lock_checks.py

It is named hash_lock_checks.py until tools/release/proposals/ci-gate-test-discovery.patch
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
from unittest import mock

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
        for bad in ("md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64, "sha256:" + "0" * 65,
                    "sha256:" + "0" * 64 + ",x"):
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

    def test_a_hash_glued_to_the_pin_fails(self) -> None:
        # pip reads `attrs==26.1.0--hash=...` as one (invalid) requirement; so must the check.
        text = self.tree.read(CI)
        self.tree.write(CI, text.replace("attrs==26.1.0 \\\n    --hash=", "attrs==26.1.0--hash=", 1))
        self.assertProblem(r"a --hash must be its own token")
        with self.assertRaisesRegex(hl.LockError, "its own token"):
            hl.parse_requirements("example==1.0--hash=sha256:" + "0" * 64)

    def test_a_hash_option_split_from_its_value_fails(self) -> None:
        with self.assertRaisesRegex(hl.LockError, "unsupported hash"):
            hl.parse_requirements("example==1.0 --hash sha256:" + "0" * 64)

    def test_requirement_text_after_a_hash_fails(self) -> None:
        with self.assertRaisesRegex(hl.LockError, "unsupported hash"):
            hl.parse_requirements("example==1.0 --hash=sha256:" + "0" * 64 + " other==2.0")

    def test_a_file_ending_inside_a_continued_pin_fails(self) -> None:
        self.tree.write(CI, self.tree.read(CI).rstrip("\n") + " \\\n")
        self.assertProblem(r"the file ends inside a continued line")

    def test_a_comment_on_a_continued_line_fails(self) -> None:
        self.tree.write(CI, self.tree.read(CI).replace("attrs==26.1.0 \\\n", "attrs==26.1.0 # pinned \\\n"))
        self.assertProblem(r"a comment on a continued line")

    def test_a_file_with_no_pins_fails(self) -> None:
        self.tree.write(CI, "# nothing pinned\n")
        self.assertProblem(r"requirements-ci\.txt: no pins")


class RecordTests(TreeCase):
    def test_an_unknown_record_format_fails(self) -> None:
        for change in ({"format": 999}, {"files": []}):
            with self.subTest(change=change):
                record = json.loads(ROOT.joinpath(hl.RECORD).read_text(encoding="utf-8"))
                record.update(change)
                self.tree.write_record(record)
                self.assertProblem(r"ci-lock\.json: .*expected 1 with a files map")

    def test_a_marker_differing_from_the_record_fails(self) -> None:
        record = self.tree.record()
        record["files"][CI]["pins"]["typing-extensions"]["marker"] = 'python_version < "3.14"'
        self.tree.write_record(record)
        self.assertProblem(r"typing-extensions==4\.16\.0 ; python_version < \"3\.13\" differs from the record")

    def test_a_pin_only_in_the_record_fails(self) -> None:
        record = self.tree.record()
        record["files"][CI]["pins"]["extra-package"] = record["files"][CI]["pins"]["attrs"]
        self.tree.write_record(record)
        self.assertProblem(r"requirements-ci\.txt: pins .* differ from the record's .*'extra-package'")

    def test_a_distribution_that_refuses_the_python_gives_no_coverage(self) -> None:
        record = self.tree.record()
        for dist in record["files"][THREADED]["pins"]["cffi"]["dists"]:
            dist["requires_python"] = ">=99"
        self.tree.write_record(record)
        self.assertProblem(r"cffi==\S+ on ubuntu-24\.04/python 3\.14t: no hashed wheel installs there")

    def test_a_record_filename_of_another_release_fails(self) -> None:
        record = self.tree.record()
        for dist in record["files"][THREADED]["pins"]["cffi"]["dists"]:
            dist["filename"] = dist["filename"].replace("cffi-", "unrelated-", 1)
        self.tree.write_record(record)
        self.assertProblem(r"the record lists 'unrelated-[^']*' for cffi==2\.1\.1; its name says another release")

    def test_the_record_keeps_every_published_dependency_entry(self) -> None:
        # Relock no longer filters entries; extras' entries are in the record and do not apply.
        pins = self.tree.record()["files"][CI]["pins"]
        self.assertTrue(any('extra ==' in r for r in pins["jsonschema"]["requires_dist"]))
        self.assertEqual(self.tree.problems(), [])

    def test_relock_keeps_entries_whose_marker_mentions_an_extra(self) -> None:
        import relock
        requirement = 'base-dependency>=1; extra != "optional"'
        metadata = {"info": {"version": "1.0", "requires_dist": [requirement], "requires_python": ""},
                    "urls": [{"filename": "fixture-1.0-py3-none-any.whl", "digests": {"sha256": "0" * 64}}]}
        with mock.patch.object(relock, "fetch", return_value=metadata):
            entry = relock.lock_pin("fixture", "1.0", None)
        self.assertEqual(entry["requires_dist"], [requirement])


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

    def test_a_dependency_that_requests_extras_fails(self) -> None:
        record = self.tree.record()
        record["files"][CI]["pins"]["jsonschema"]["requires_dist"].append("attrs[tests]>=22.2.0")
        self.tree.write_record(record)
        self.assertProblem(r"jsonschema==\S+ on .*: dependency 'attrs\[tests\]>=22\.2\.0' requests extras")
        with self.assertRaisesRegex(hl.LockError, "requests extras"):
            hl.parse_dependency("requests[socks]>=2")

    def test_a_base_active_marker_naming_an_extra_applies(self) -> None:
        record = self.tree.record()
        record["files"][CI]["pins"]["attrs"]["requires_dist"].append('not-pinned>=1; extra != "optional"')
        self.tree.write_record(record)
        self.assertProblem(r"attrs==\S+ on ubuntu-24\.04/python 3\.12: needs 'not-pinned>=1; extra != \"optional\"'")

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

    def test_every_command_on_a_line_is_read(self) -> None:
        for operator in ("&&", "||", ";", "|"):
            with self.subTest(operator=operator):
                self.add_workflow(f"python -m pip install -r requirements-ci.txt {operator} "
                                  "python -m pip install -r requirements-dev.txt")
                self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")
                self.add_workflow(f"python -m pip install -r requirements-ci.txt {operator} pip install requests")
                self.assertProblem(r"installs 'requests'")

    def test_the_attached_module_spelling_is_read(self) -> None:
        self.add_workflow("python -mpip install -r requirements-dev.txt")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_an_install_inside_a_command_substitution_is_read(self) -> None:
        self.add_workflow("out=$(pip install requests)")
        self.assertProblem(r"installs 'requests'")

    def test_jobs_at_another_indentation_are_read(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="python -m pip install -r requirements-dev.txt")
        head, body = text.split("jobs:\n", 1)
        self.tree.write(rel, head + "jobs:\n" + "".join("  " + line + "\n" for line in body.splitlines()))
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_a_commented_jobs_line_is_read(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="python -m pip install -r requirements-dev.txt")
        self.tree.write(rel, text.replace("jobs:\n", "jobs: # the jobs\n"))
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_inline_jobs_fail(self) -> None:
        self.tree.write(".github/workflows/extra.yml", 'name: x\non: push\njobs: {build: {runs-on: ubuntu-24.04, '
                        'steps: [{run: "python -m pip install -r requirements-dev.txt"}]}}\n')
        self.assertProblem(r"extra\.yml:3: a flow mapping, which this check does not read")

    # Round-2 reads (GPT-6 Astra change 1, GPT-6.1 Sol changes 1-2): values are decoded as YAML
    # reads them before any matching, and every installer the check cannot read is refused.
    def test_a_folded_install_cannot_disappear(self) -> None:
        self.add_workflow(">-\n          python -m pip\n          install -r requirements-dev.txt")
        self.assertTrue(self.tree.problems(), "folded run silently hides an unlocked install")

    def test_folded_installs_are_read_as_installs(self) -> None:
        for command in (">-\n          python -m pip\n          install -r requirements-dev.txt",
                        ">\n          python -m pip\n          install -r requirements-dev.txt"):
            with self.subTest(command=command):
                self.add_workflow(command)
                self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\] but is not hash-locked")

    def test_a_literal_block_is_read_line_by_line(self) -> None:
        self.add_workflow("|\n          python -m pip install -r requirements-ci.txt\n"
                          "          python -m pip install -r requirements-dev.txt")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_a_folded_locked_install_passes(self) -> None:
        self.add_workflow(">-\n          python -m pip\n          install --only-binary=:all: -r requirements-ci.txt")
        self.assertEqual(self.tree.problems(), [])
        installs, _ = hl.all_installs(self.tree.root)
        self.assertIn(("extra.yml", "build", CI), {(i.source, i.job, i.file) for i in installs})

    def test_an_unread_second_installer_is_refused(self) -> None:
        self.add_workflow("pip install -r requirements-ci.txt; uv sync")
        self.assertProblem(r"an installer this check does not read: 'uv sync'")

    def test_installer_text_in_another_value_fails(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="echo")
        self.tree.write(rel, text + "      - uses: some/action@v1\n        with:\n          script: >-\n"
                                    "            pip\n            install requests\n")
        self.assertProblem(r"extra\.yml:\d+: installer text in a `script:` value this check does not read")

    def test_an_installer_line_that_is_no_key_value_fails(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="echo")
        self.tree.write(rel, text + "      - pip install requests\n")
        self.assertProblem(r"extra\.yml:\d+: installer text in a list entry this check does not read")

    def test_a_run_step_outside_a_readable_job_fails(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="echo ok")
        self.tree.write(rel, text.replace("jobs:\n", "run: >-\n  python -m pip\n  install -r requirements-dev.txt\njobs:\n"))
        self.assertProblem(r"extra\.yml:3: a run step that may install packages sits outside any job")

    # Round-3 reads (GPT-6 Astra changes 1-3, GPT-6.1 Sol changes 1-2): their supplied cases,
    # as written; each also names the reason, so its problem cannot come from elsewhere.
    def test_escaped_quote_before_install_is_not_truncated(self) -> None:
        self.add_workflow(json.dumps('echo "preparing"; python -m pip install -r requirements-dev.txt'))
        self.assertTrue(self.tree.problems(), "escaped quote silently hides an unlocked install")
        self.assertProblem(r"extra\.yml:10: a backslash escape in a double-quoted value")

    def test_escaped_newline_after_comment_is_not_ignored(self) -> None:
        self.add_workflow(json.dumps('# preparing\npython -m pip install -r requirements-dev.txt'))
        self.assertTrue(self.tree.problems(), "escaped newline silently hides an unlocked install")
        self.assertProblem(r"extra\.yml:10: a backslash escape in a double-quoted value")

    def test_folded_explicit_indent_preserves_more_indented_lines(self) -> None:
        self.add_workflow('>2-\n            # preparing\n            python -m pip install -r requirements-dev.txt')
        self.assertTrue(self.tree.problems(), "explicit indent silently hides an unlocked install")
        self.assertProblem(r"extra\.yml:10: an explicit block indentation indicator")

    def test_anchored_block_with_header_comment_is_not_ignored(self) -> None:
        self.add_workflow('&review_anchor | # note\n          python -m pip install -r requirements-dev.txt')
        self.assertTrue(self.tree.problems(), "anchored block silently hides an unlocked install")
        self.assertProblem(r"extra\.yml:10: an anchor, alias or tag")

    def test_folded_blank_line_preserves_second_command(self) -> None:
        self.add_workflow('>-\n          # preparing\n\n          python -m pip install -r requirements-dev.txt')
        self.assertTrue(self.tree.problems(), "blank-line boundary was lost")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_folded_more_indented_line_after_comment_is_checked(self) -> None:
        self.add_workflow('>-\n          # preparing\n            python -m pip install -r requirements-dev.txt')
        self.assertTrue(self.tree.problems(), "more-indented boundary was lost")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_escaped_yaml_quotes_cannot_hide_a_later_install(self) -> None:
        self.add_workflow('"echo \\"ready\\"; pip install -r requirements-dev.txt"')
        self.assertTrue(self.tree.problems(), "escaped YAML quotes truncate the run before its install")
        self.assertProblem(r"extra\.yml:10: a backslash escape in a double-quoted value")

    def test_a_shallow_quoted_continuation_cannot_hide_an_install(self) -> None:
        self.add_workflow('"python -m pip\n        install -r requirements-dev.txt"')
        self.assertTrue(self.tree.problems(), "valid quoted continuation silently hides an install")
        self.assertProblem(r"extra\.yml:10: a quoted value that continues on the next line")

    def test_an_anchored_literal_preserves_shell_comment_boundaries(self) -> None:
        self.add_workflow("&ci |-\n          pip install -r requirements-ci.txt # first install\n"
                          "          pip install -r requirements-dev.txt")
        self.assertTrue(self.tree.problems(), "anchored literal folds the second install into a comment")
        self.assertProblem(r"extra\.yml:10: an anchor, alias or tag")

    def test_a_quoted_escaped_line_break_cannot_hide_an_install(self) -> None:
        self.add_workflow('"python -m pi\\\n          p install -r requirements-dev.txt"')
        self.assertTrue(self.tree.problems(), "quoted YAML continuation silently hides an unlocked install")
        self.assertProblem(r"extra\.yml:10: a backslash escape in a double-quoted value")

    def test_a_quoted_escaped_line_break_cannot_hide_a_second_installer(self) -> None:
        self.add_workflow('"pip install -r requirements-ci.txt; u\\\n          v sync"')
        self.assertTrue(self.tree.problems(), "quoted YAML continuation silently hides an unsupported installer")
        self.assertProblem(r"extra\.yml:10: a backslash escape in a double-quoted value")

    def test_a_chomped_literal_block_preserves_shell_comment_boundaries(self) -> None:
        self.add_workflow("|-\n          pip install -r requirements-ci.txt # first install\n"
                          "          pip install -r requirements-dev.txt")
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    # The reader is closed: a YAML form it does not decode is a problem wherever it appears,
    # with or without installer text, since what the value would decode to is unknown.
    def test_every_yaml_form_this_check_does_not_read_is_refused(self) -> None:
        rel, template = self.WORKFLOW
        base = template.format(runner="ubuntu-24.04", python="3.12", command="echo ok")
        cases = {
            "alias": ("      - run: *ready\n", r"an anchor, alias or tag"),
            "anchor": ("      - run: &ready echo ok\n", r"an anchor, alias or tag"),
            "tag": ("      - run: !!str echo ok\n", r"an anchor, alias or tag"),
            "merge key": ("      - <<: base\n", r"a merge key"),
            "flow mapping": ("      - with: {a: b}\n", r"a flow mapping"),
            "flow list it cannot read": ("      - with: ['a']\n", r"a flow list this check cannot read"),
            "plain continuation": ("      - run: echo\n          ok\n", r"a line deeper than the value above it"),
            "quoted continuation": ("      - run: 'echo\n          ok'\n", r"a quoted value that continues on the next line"),
            "escape": ('      - run: "echo \\t ok"\n', r"a backslash escape in a double-quoted value"),
            "indentation indicator": ("      - run: |2\n          echo ok\n", r"an explicit block indentation indicator"),
            "tab in indentation": ("\t- run: echo ok\n", r"a tab in the indentation"),
            "document marker": ("---\n", r"neither `key: value` nor a list entry"),
            "directive": ("%YAML 1.2\n", r"neither `key: value` nor a list entry"),
            "complex key": ("      - ? run\n", r"a value starting with '\?'"),
            "duplicate key": ("        run: echo again\n", r"the key 'run' twice in one mapping"),
            "next line (NEL)": ("      - run: echo ok" + chr(0x85) + "# x\n", r"a line break other than LF"),
            "line separator": ("      - run: echo ok" + chr(0x2028) + "\n", r"a line break other than LF"),
            "control character": ("      - run: echo" + chr(1) + "ok\n", r"a control character, which YAML does not allow"),
            "text after a quote": ("      - run: 'echo' ok\n", r"text after a quoted value"),
            "colon in a plain value": ("      - run: echo a: b\n", r"a `: ` inside a plain value"),
            "backtick value": ("      - run: `echo`\n", r"a value starting with '`'"),
            "block line less indented": ("      - run: |\n            echo a\n          echo b\n",
                                         r"less indented than its first line"),
            "blank block line with extra spaces": ("      - run: |\n          echo a\n             \n          echo b\n",
                                                   r"a blank line of a block value with more spaces"),
            "entry where a mapping continues": ("    - x\n", r"a list entry where its mapping continues"),
            "line that is no key or entry": ("    just words\n", r"neither `key: value` nor a list entry"),
        }
        for name, (extra, reason) in cases.items():
            with self.subTest(form=name):
                self.tree.write(rel, base + extra)
                self.assertProblem(r"extra\.yml:\d+: .*" + reason)

    def test_values_that_continue_on_the_next_line_are_refused(self) -> None:
        self.add_workflow("python -m pip\n          install -r requirements-dev.txt")
        self.assertProblem(r"extra\.yml:11: a line deeper than the value above it")
        self.add_workflow("\"python -m pip\n          install -r requirements-dev.txt\"")
        self.assertProblem(r"extra\.yml:10: a quoted value that continues on the next line")

    def test_a_quoted_jobs_key_is_read_as_jobs(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12",
                               command=">-\n          python -m pip\n          install -r requirements-dev.txt")
        self.tree.write(rel, text.replace("jobs:\n", '"jobs":\n'))
        self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    # Bash: each of these hid an install at e5adf594, which matched shell lines as raw text and
    # split them with shlex (a `#` inside a word started a comment there).
    def test_shell_forms_cannot_hide_an_install(self) -> None:
        locked = "pip install -r requirements-ci.txt"
        for script in (f"{locked}; echo a#b; pip install -r requirements-dev.txt",
                       f"{locked} # note \\\n          pip install -r requirements-dev.txt",
                       'p"i"p install -r requirements-dev.txt',
                       "p\\ip install -r requirements-dev.txt",
                       "$'\\x70ip' install -r requirements-dev.txt",
                       "pip >log install -r requirements-dev.txt",
                       "out=$(pip install -r requirements-dev.txt)",
                       "x=`pip install -r requirements-dev.txt`",
                       "tee >(pip install -r requirements-dev.txt)"):
            with self.subTest(script=script):
                self.add_workflow("|\n          " + script)
                self.assertProblem(r"requirements-dev\.txt is installed by \['extra\.yml job build'\]")

    def test_installers_run_in_ways_this_check_does_not_read_are_refused(self) -> None:
        cases = {
            "P=pip\n          $P install -r requirements-dev.txt": r"a pip install through a variable",
            "$TOOL install -r requirements-dev.txt": r"an installer verb after an expansion this check cannot resolve",
            "/usr/bin/p?p install -r requirements-dev.txt": r"an installer verb after an expansion this check cannot",
            "echo install -r requirements-dev.txt | xargs pip": r"an installer program run in a way",
            "pip $VERB -r requirements-dev.txt": r"an installer program run in a way",
            "uvx ruff check": r"an installer program run in a way",
            "alias get='pip'": r"an installer program run in a way",
            "eval \"$CMD\"": r"eval of text built at run time",
            "bash -c \"$CMD\"": r"bash of text built at run time",
            ". ./steps.sh": r"\. runs a file's text as the script",
            "source steps.sh": r"source runs a file's text as the script",
            "bash <<'EOF'\n          pip install -r requirements-dev.txt\n          EOF": r"an installer this check does not read",
            "bash <<< 'pip install -r requirements-dev.txt'": r"an installer this check does not read",
            "cat <<EOF\n          $(date)\n          EOF": r"a command substitution in an unquoted here-document",
            "echo 'never closed": r"cannot read as bash: an unterminated single quote",
        }
        for script, reason in cases.items():
            with self.subTest(script=script):
                self.add_workflow("|\n          " + script)
                self.assertProblem(r"extra\.yml(?::\d+| job build): .*" + reason)

    def test_a_pip_install_runs_in_bash(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="windows-2025", python="3.12", command="python -m pip install -r requirements-ci.txt")
        self.tree.write(rel, text)
        self.assertProblem(r"a pip install in a step whose shell is 'pwsh'; this check reads bash")
        self.tree.write(rel, text + "        shell: bash\n")
        self.assertEqual(self.tree.problems(), [])
        self.tree.write(rel, text.replace("windows-2025", "ubuntu-24.04") + "        shell: sh\n")
        self.assertProblem(r"a pip install in a step whose shell is 'sh'")

    def test_a_step_using_an_action_this_check_has_not_read_fails(self) -> None:
        rel, template = self.WORKFLOW
        base = template.format(runner="ubuntu-24.04", python="3.12", command="echo ok")
        for uses in ("some/pip-install-action@v1", "docker://python:3.12", "./.github/actions/setup"):
            with self.subTest(uses=uses):
                self.tree.write(rel, base + f"      - uses: {uses}\n")
                self.assertProblem(rf"extra\.yml:11: uses {re.escape(repr(uses))}, which this check has not read")
        self.tree.write(rel, base + "  call:\n    uses: ./.github/workflows/extra.yml\n")
        self.assertEqual(self.tree.problems(), [])

    def test_job_settings_are_read_from_yaml_values_only(self) -> None:
        # A run block's text belongs to the shell: a line in it that looks like a setting is not one.
        self.add_workflow("|\n          cat > x.yml <<'EOF'\n          python-version: \"3.10\"\n          EOF\n"
                          "          python -m pip install -r requirements-ci.txt")
        self.assertEqual(self.tree.problems(), [])

    def test_a_crlf_workflow_is_read(self) -> None:
        rel, template = self.WORKFLOW
        text = template.format(runner="ubuntu-24.04", python="3.12", command="python -m pip install -r requirements-ci.txt")
        found, problems = hl.workflow_installs("extra.yml", text.replace("\n", "\r\n"))
        self.assertEqual(problems, [])
        self.assertEqual([(i.file, str(i.target)) for i in found], [(CI, "ubuntu-24.04/python 3.12")])

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


class YamlReadingTests(unittest.TestCase):
    """The values the reader decodes. Each expected value is PyYAML 6.0.1's reading of the same
    text (BaseLoader), taken once; the check itself stays stdlib only. A flow list is kept as
    its text: only installer text is looked for in it."""

    def test_values_are_decoded_as_yaml_decodes_them(self) -> None:
        cases = [
            ("a: |\n  x\n  y\n", {("a",): "x\ny\n"}),
            ("a: |-\n  x\n\n", {("a",): "x"}),
            ("a: |+\n  x\n\n", {("a",): "x\n\n"}),
            ("a: | # note\n  x\n", {("a",): "x\n"}),
            ("a: |\n   x\n    y\n", {("a",): "x\n y\n"}),
            ("a: >\n  x\n  y\n\n  z\n", {("a",): "x y\nz\n"}),
            ("a: >-\n  x\n    y\n  z\n", {("a",): "x\n  y\nz"}),
            ("a: >\n\n  x\n", {("a",): "\nx\n"}),
            ("a: >\n  x\n\n    y\n", {("a",): "x\n\n  y\n"}),
            ("a: >+\n  x\n\n", {("a",): "x\n\n"}),
            ("a: 'it''s'  # c\n", {("a",): "it's"}),
            ('a: "x # y"\n', {("a",): "x # y"}),
            ("a: x #y\n", {("a",): "x"}),
            ("a: x#y\n", {("a",): "x#y"}),
            ("a:\n- x\n- 'y'\nb: [p, q]\n", {("a", 0): "x", ("a", 1): "y", ("b",): "[p, q]"}),
            ("s:\n  - k: v\n    l: |\n      t\n  - - n\n", {("s", 0, "k"): "v", ("s", 0, "l"): "t\n", ("s", 1, 0): "n"}),
            ("s:\n  -\n    k: v\n", {("s", 0, "k"): "v"}),
            ('"q k": v\n\'r\': w\n', {("q k",): "v", ("r",): "w"}),
            ("a:\n  b:\n\n    # c\n    c: d\ne: f\n", {("a", "b", "c"): "d", ("e",): "f"}),
            ("a: x\nb:\n", {("a",): "x"}),
            ("a: |\n  x", {("a",): "x"}),
            ("a: >+\n  x\n\n  ", {("a",): "x\n\n"}),
            ("'it''s': w\n", {("it's",): "w"}),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                found, problems = hl.read_yaml(text)
                self.assertEqual(problems, [])
                self.assertEqual({s.path: s.text for s in found}, expected)


class BashReadingTests(unittest.TestCase):
    """How a `run:` value is split into commands and words: as bash splits it."""

    def words(self, script: str) -> list[tuple[str, ...]]:
        return sorted(tuple(w.text for w in command) for command in hl.shell_commands(script))

    def test_commands_and_words_are_read_as_bash_forms_them(self) -> None:
        cases = [
            ("echo a#b; pip install x", [("echo", "a#b"), ("pip", "install", "x")]),
            ("echo a # note \\\npip install x", [("echo", "a"), ("pip", "install", "x")]),
            ("echo '#'; pip install x", [("echo", "#"), ("pip", "install", "x")]),
            ('p"i"p install x', [("pip", "install", "x")]),
            ("p\\ip install x", [("pip", "install", "x")]),
            ("$'\\x70i\\160' install x", [("pip", "install", "x")]),
            ("pip install \\\n  -r x", [("pip", "install", "-r", "x")]),
            ("echo \"a;b\" 'c|d' \"e\\\"f\"", [("echo", "a;b", "c|d", 'e"f')]),
            ("pip >log install -r x 2>&1", [("pip", "install", "-r", "x")]),
            ("out=$(pip install x)", [("out=$(pip install x)",), ("pip", "install", "x")]),
            ("x=`pip install y`", [("pip", "install", "y"), ("x=`pip install y`",)]),
            ("tee >(pip install x)", [("pip", "install", "x"), ("tee",)]),
            ("cat <<'EOF'\npip install x\nEOF\necho done", [("cat", "pip install x"), ("echo", "done")]),
            ("cat <<-EOF\n\tbody\n\tEOF\necho done", [("cat", "\tbody"), ("echo", "done")]),
            ("echo ${{ github.event_name }} done", [("echo", "${{ github.event_name }}", "done")]),
            ("echo '${{ a == 'b' }}'", [("echo", "${{ a == 'b' }}")]),
            ("a && b || c | d & e ; f", [("a",), ("b",), ("c",), ("d",), ("e",), ("f",)]),
        ]
        for script, expected in cases:
            with self.subTest(script=script):
                self.assertEqual(self.words(script), sorted(expected))

    def test_values_known_only_at_run_time_are_marked(self) -> None:
        for script, word, dynamic in (("$X install", "$X", True), ('"${Y}" a', "${Y}", True),
                                      ("echo $(date)", "$(date)", True), ("ls *.txt", "*.txt", True),
                                      ("echo {a,b}", "{a,b}", True), ("echo ${{ x }}", "${{ x }}", True),
                                      ("echo '$X'", "$X", False), ("echo \\$X", "$X", False)):
            with self.subTest(script=script):
                words = {w.text: w.dynamic for command in hl.shell_commands(script) for w in command}
                self.assertEqual(words.get(word), dynamic)

    def test_scripts_bash_cannot_finish_reading_are_refused(self) -> None:
        for script in ("echo 'a", 'echo "a', "echo $(a", "cat <<EOF\nnever ends", "echo `a", "echo ${a",
                       "echo ${{ a", "echo " + chr(0) + "0" + chr(1)):
            with self.subTest(script=script):
                with self.assertRaises(hl.LockError):
                    hl.shell_commands(script)


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
