#!/usr/bin/env python3
"""Checks for the licence files (lane RELEASE). Run this file from the repository root:

    python -X utf8 -B tools/release/licence_checks.py

`problems(root)` names, one sentence each, every rule the four files at `root` (a path or a string)
break:
- LICENSE is the Apache License 2.0 text unmodified, byte for byte as published at
  https://www.apache.org/licenses/LICENSE-2.0.txt. In a git checkout that tracks it, its blob in
  the index and its blob in HEAD (when there is one) are each held byte for byte, and the file on
  disk may differ from them only in line endings, which a Windows checkout changes; anywhere else
  the file on disk is held byte for byte. In a checkout where git cannot be run at all the disk is
  read alone, as outside one; a git that does not answer within GIT_TIMEOUT seconds is named.
- NOTICE is NOTICE_TEXT, up to line endings: the copyright holder stays a placeholder until Lucas
  names one, and no other name, address or line is added.
- CONTRIBUTING.md has one sign-off section, held word for word up to line endings (SIGN_OFF).
  Text elsewhere in the file is not read.
- SECURITY.md keeps its draft note and its four sections outside HTML comments and fenced blocks,
  each section with a line of its own; says that reports go privately to CONTACT and not to a
  public issue; and holds no e-mail address but CONTACT. What its sections say is not judged.
A file larger than SIZE_LIMIT bytes is named without being read, and a missing file as missing.
The tests check the committed files, then break, in a scratch copy of the four files, each rule
that they name, and check that it is named.

The file is named *_checks.py, not test_*.py: the frozen gate (tools/ci_gate.py) fails on a
test-named file outside its globs, and tools/release/ is not among them, so hosted CI does not run
it. `python -m unittest discover -s tools/release -p '*_checks.py'` finds it. Stdlib only and
offline; git is run only when the tree is a git checkout.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FILES = ("LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md")
SIZE_LIMIT = 1 << 20  # bytes; no licence file is near it, so a larger one is named, never read
GIT_TIMEOUT = 60  # seconds for one git call

# https://www.apache.org/licenses/LICENSE-2.0.txt, fetched Nov 14, 2026: 11358 bytes. The same
# bytes ship as /usr/share/common-licenses/Apache-2.0 on Ubuntu 24.04.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

# Until Lucas names the copyright holder (counsel item K13), NOTICE carries this placeholder and
# no name. Naming the holder is one deliberate edit of HOLDER and of NOTICE together.
HOLDER = "<holder: Lucas to name>"
NOTICE_TEXT = ("Recursion\n"
               f"Copyright 2026 {HOLDER}\n"
               "\n"
               'Licensed under the Apache License, Version 2.0 (the "License"); you may not use\n'
               "the files in this repository except in compliance with the License. A copy of\n"
               "the License is in the file LICENSE.\n")

SIGN_OFF_HEADING = "## Sign-off (Developer Certificate of Origin)\n"
SIGN_OFF = """
<!-- tools/release/licence_checks.py holds this section word for word (SIGN_OFF): change both together. -->

Recursion is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`), applied
once its copyright holder is named. From the commit that applies the licence on,
every commit must carry a sign-off line naming the person who submits it:

```text
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it from your Git name and e-mail. By signing off you certify
the Developer Certificate of Origin, version 1.1
(<https://developercertificate.org/>): that you wrote the change, or otherwise have
the right to submit it under the project's licence, and that you understand the
change and your sign-off are public and kept for good. Use your real name or the
name you are known by, and one identity per person.

A sign-off is a person's certification. An automated agent or bot does not add one;
a commit an agent prepares is signed off by the person who submits it, and that
person makes the certification. A pull request with a commit that lacks a sign-off
is not merged. Commits made before the licence was applied carry no sign-off; they
are not re-signed.
"""

# Naming the security contact is one edit of CONTACT and of SECURITY.md together; CONTACT may then
# be the one address SECURITY.md holds.
CONTACT = "<contact: Lucas to name>"
SECURITY_PARTS = ("> **Draft.**", "## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                  "## What the code defends, and what it does not yet\n")
PUBLIC_ISSUE = "Please do not report a suspected vulnerability in a public issue"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")
HIDDEN = re.compile(r"<!--.*?-->|^```.*?^```[^\n]*$", re.S | re.M)  # what a reader of the page does not see as text


def _read(root: Path, rel: str, found: list[str]) -> bytes | None:
    path = root / rel
    if not path.is_file():
        found.append(f"{rel} is missing")
        return None
    if path.stat().st_size > SIZE_LIMIT:
        found.append(f"{rel} is larger than {SIZE_LIMIT} bytes, which no licence file is")
        return None
    return path.read_bytes()


def _text(data: bytes) -> str:
    """A text file's content, line endings as committed (a Windows checkout may add CRs)."""
    return data.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")


def _committed(root: Path, rel: str, found: list[str]) -> list[tuple[str, bytes]]:
    """The file's blobs in git's index and in HEAD, as (where, bytes), when `root` itself is a git
    checkout that holds it. [] when it is not a checkout, when git cannot be run, or when neither
    holds the file. The environment's GIT_* settings are not used: the checkout's own repository is
    read. A blob larger than SIZE_LIMIT, and a git that does not answer, are named."""
    if not (root / ".git").exists():
        return []
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    blobs: list[tuple[str, bytes]] = []
    for where, name in (("in the index", f":{rel}"), ("in HEAD", f"HEAD:{rel}")):
        try:
            size = subprocess.run(["git", "-C", str(root), "cat-file", "-s", name],
                                  capture_output=True, text=True, env=env, timeout=GIT_TIMEOUT)
            if size.returncode != 0:
                continue  # not there (an untracked file, or no commit yet)
            if not size.stdout.strip().isdigit():
                found.append(f"git gave no size for {rel} {where}, so it was not read")
                continue
            if int(size.stdout) > SIZE_LIMIT:
                found.append(f"{rel} {where} is larger than {SIZE_LIMIT} bytes, which no licence file is")
                continue
            blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", name],
                                  capture_output=True, env=env, timeout=GIT_TIMEOUT)
        except OSError:
            return []  # no git here: the file on disk is all there is
        except subprocess.SubprocessError:
            found.append(f"git did not answer within {GIT_TIMEOUT} s, so {rel} {where} was not read")
            return blobs
        blobs.append((where, blob.stdout))
    return blobs


def license_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "LICENSE", found)
    if data is None:
        return found
    blobs = _committed(root, "LICENSE", found)
    for where, blob in blobs:
        if hashlib.sha256(blob).hexdigest() != APACHE_2_0_SHA256:
            found.append(f"LICENSE {where} is not the published Apache License 2.0 text, byte for byte")
    if blobs:
        data = data.replace(b"\r\n", b"\n")  # a Windows checkout of the committed file
    if hashlib.sha256(data).hexdigest() != APACHE_2_0_SHA256:
        found.append("LICENSE is not the published Apache License 2.0 text, byte for byte")
    return found


def notice_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "NOTICE", found)
    if data is not None and _text(data) != NOTICE_TEXT:
        have, want = _text(data).splitlines(), NOTICE_TEXT.splitlines()
        line = next((i for i, (a, b) in enumerate(zip(have, want)) if a != b), min(len(have), len(want)))
        found.append(f"NOTICE is not NOTICE_TEXT (from line {line + 1}): the holder stays unnamed and nothing is added")
    return found


def contributing_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "CONTRIBUTING.md", found)
    if data is None:
        return found
    text = _text(data)
    if text.count(SIGN_OFF_HEADING) != 1:
        found.append("CONTRIBUTING.md does not have exactly one sign-off section")
        return found
    if text.split(SIGN_OFF_HEADING, 1)[1].split("\n## ", 1)[0] != SIGN_OFF:
        found.append("CONTRIBUTING.md's sign-off section is not SIGN_OFF word for word")
    return found


def security_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "SECURITY.md", found)
    if data is None:
        return found
    text = "\n" + HIDDEN.sub("", _text(data))
    for part in SECURITY_PARTS:
        if "\n" + part not in text:
            found.append(f"SECURITY.md has no {part.strip()!r}")
        elif part.startswith("## ") and not text.split("\n" + part, 1)[1].split("\n## ", 1)[0].strip():
            found.append(f"SECURITY.md's section {part.strip()!r} is empty")
    found += [f"SECURITY.md does not say {sentence!r}"
              for sentence in (f"Report it privately to {CONTACT}.", PUBLIC_ISSUE) if sentence not in text]
    if set(EMAIL.findall(text)) - {CONTACT}:
        found.append("SECURITY.md holds an e-mail address that is not its contact (CONTACT)")
    return found


def problems(root: Path | str) -> list[str]:
    root = Path(root)
    return license_problems(root) + notice_problems(root) + contributing_problems(root) + security_problems(root)


class LicenceTests(unittest.TestCase):
    """The committed files hold every rule."""

    def test_license_is_the_apache_2_0_text_unmodified(self) -> None:
        self.assertEqual(license_problems(ROOT), [])

    def test_notice_names_no_holder_until_lucas_does(self) -> None:
        self.assertEqual(notice_problems(ROOT), [])

    def test_contributing_states_the_sign_off_rule(self) -> None:
        self.assertEqual(contributing_problems(ROOT), [])

    def test_security_policy_has_no_address_until_the_contact_is_named(self) -> None:
        self.assertEqual(security_problems(ROOT), [])


class BrokenRuleTests(unittest.TestCase):
    """Each rule broken in a scratch copy of the four files, the defects planted by the reads of
    #409 among them: the checks must name it, and a crash counts as not naming it."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="licence-checks-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "tree"
        self.root.mkdir()
        unusable = [rel for rel in FILES if not (ROOT / rel).is_file() or (ROOT / rel).stat().st_size > SIZE_LIMIT]
        if unusable:
            self.skipTest(f"missing or oversized in the repository, which LicenceTests names: {unusable}")
        for rel in FILES:
            (self.root / rel).write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))

    def found(self, root: Path | str | None = None) -> list[str]:
        try:
            return problems(root or self.root)
        except Exception as exc:  # noqa: BLE001 - reported through the assertion, not as an error
            return [f"the checks crashed: {exc!r}"]

    def assertNamed(self, pattern: str, root: Path | None = None) -> None:
        found = self.found(root)
        self.assertTrue(any(re.search(pattern, p) for p in found), f"no problem matches {pattern!r}: {found}")

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")

    def git(self, root: Path, *args: str) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(root), *args],
                       check=True, capture_output=True, env=env, timeout=60)

    def crlf(self, rel: str) -> None:
        path = self.root / rel
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    # The harness itself: a rule that names nothing, or a crash, must turn these tests red.
    def test_the_scratch_copy_holds(self) -> None:
        self.assertEqual(self.found(), [])
        self.assertEqual(self.found(str(self.root)), [])  # a string names the same tree

    def test_assert_named_fails_where_nothing_is_named(self) -> None:
        with self.assertRaises(AssertionError):
            self.assertNamed(r"^LICENSE is not the published")

    def test_a_crash_is_reported_as_a_name(self) -> None:
        def crash(root: Path) -> list[str]:
            raise RuntimeError("planted")
        with mock.patch.dict(globals(), {"license_problems": crash}):
            try:
                found = self.found()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"found() let a crash out: {exc!r}")
        self.assertEqual(found, ["the checks crashed: RuntimeError('planted')"])

    def test_git_is_skipped_where_it_is_not_installed(self) -> None:
        with mock.patch.object(shutil, "which", return_value=None):
            try:
                self.git(self.root, "status")
            except unittest.SkipTest:
                return
            except Exception as exc:  # noqa: BLE001 - reported through the assertion
                self.fail(f"git was run where it is not installed: {exc!r}")
        self.fail("git was run where it is not installed")

    def test_a_missing_or_oversized_file_is_named(self) -> None:
        for rel in FILES:
            with self.subTest(rel=rel):
                data = (self.root / rel).read_bytes()
                (self.root / rel).unlink()
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).mkdir()  # a folder where the file should be
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).rmdir()
                (self.root / rel).write_bytes(data * 3)
                with mock.patch.dict(globals(), {"SIZE_LIMIT": len(data) * 2}):
                    self.assertNamed(rf"^{re.escape(rel)} is larger than {len(data) * 2} bytes")
                (self.root / rel).write_bytes(data)

    def test_license_one_byte_changed(self) -> None:
        path = self.root / "LICENSE"
        path.write_bytes(path.read_bytes().replace(b"Apache License", b"Apache Licence", 1))
        self.assertNamed(r"^LICENSE is not the published")

    def test_license_with_crlf_outside_a_checkout(self) -> None:
        self.crlf("LICENSE")
        self.assertNamed(r"^LICENSE is not the published")

    def test_a_windows_checkout_of_the_committed_license_holds(self) -> None:
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "LICENSE")
        self.crlf("LICENSE")
        self.assertEqual(self.found(), [])
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "elsewhere")}):  # the checkout's own git
            self.assertEqual(self.found(), [])
        with mock.patch.dict(os.environ, {"PATH": ""}):  # no git to run: read from disk only
            self.assertNamed(r"^LICENSE is not the published")

    def test_a_license_committed_with_crlf(self) -> None:
        self.git(self.root, "init", "-q")
        self.crlf("LICENSE")
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes((self.root / "LICENSE").read_bytes().replace(b"\r\n", b"\n"))
        self.assertNamed(r"^LICENSE in the index is not")

    def test_a_good_license_staged_over_a_bad_one_in_head(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_text("not the licence\n", encoding="utf-8")
        self.git(self.root, "add", "LICENSE")
        self.git(self.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--no-verify",
                 "-m", "t")
        (self.root / "LICENSE").write_bytes(good)
        self.git(self.root, "add", "LICENSE")
        self.assertEqual([p for p in self.found() if p.startswith("LICENSE")],
                         ["LICENSE in HEAD is not the published Apache License 2.0 text, byte for byte"])

    def test_an_oversized_license_in_the_index(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_bytes(good * 3)
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes(good)
        with mock.patch.dict(globals(), {"SIZE_LIMIT": len(good) * 2}):
            self.assertNamed(rf"^LICENSE in the index is larger than {len(good) * 2} bytes")

    def stand_in_git(self, script: str) -> Path:
        """A folder holding a `git` that runs `script` instead (POSIX only)."""
        if os.name == "nt":
            self.skipTest("the stand-in git is a POSIX shell script")
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "LICENSE")
        stand_in = Path(tempfile.mkdtemp(prefix="bin-", dir=self.root.parent))
        (stand_in / "git").write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        (stand_in / "git").chmod(0o755)
        return stand_in

    def test_a_git_that_does_not_answer_is_named(self) -> None:
        stand_in = self.stand_in_git("exec /bin/sleep 5")
        with mock.patch.dict(os.environ, {"PATH": str(stand_in)}), mock.patch.dict(globals(), {"GIT_TIMEOUT": 1}):
            self.assertNamed(r"^git did not answer within 1 s, so LICENSE in the index was not read")

    def test_a_git_that_answers_nonsense_is_named(self) -> None:
        stand_in = self.stand_in_git("echo 'not a size'")
        with mock.patch.dict(os.environ, {"PATH": str(stand_in)}):
            self.assertNamed(r"^git gave no size for LICENSE in the index, so it was not read")

    def test_an_untracked_license_in_a_checkout_is_read_from_disk(self) -> None:
        self.git(self.root, "init", "-q")
        self.assertEqual(self.found(), [])
        self.crlf("LICENSE")
        self.assertNamed(r"^LICENSE is not the published")

    def test_a_tree_inside_another_checkout_is_read_from_disk(self) -> None:
        outer = self.root.parent
        (outer / "LICENSE").write_text("another licence\n", encoding="utf-8")
        self.git(outer, "init", "-q")
        self.git(outer, "add", "LICENSE")
        self.assertEqual(self.found(), [])

    def test_notice_with_a_holder_a_name_or_an_address(self) -> None:
        for old, new, line in ((HOLDER, "Jane Doe", 2),
                               ("the License is in the file LICENSE.", "Maintained by Jane Doe <jane.doe@example.org>.\n"
                                                                       "the License is in the file LICENSE.", 6),
                               ("the License is in the file LICENSE.", "Contact: jane.doe@example.org\n"
                                                                       "the License is in the file LICENSE.", 6),
                               ("A copy of", "Licence text", 5),
                               (NOTICE_TEXT, "", 1)):
            with self.subTest(new=new):
                saved = (self.root / "NOTICE").read_bytes()
                self.edit("NOTICE", old, new)
                self.assertNamed(rf"^NOTICE is not NOTICE_TEXT \(from line {line}\)")
                (self.root / "NOTICE").write_bytes(saved)

    def test_notice_that_is_not_utf_8_is_named(self) -> None:
        (self.root / "NOTICE").write_bytes(b"Recursion\n\xff\xfe\n")
        self.assertNamed(r"^NOTICE is not NOTICE_TEXT \(from line 2\)")

    def test_the_sign_off_section_is_held_word_for_word(self) -> None:
        for old, new in (("version 1.1\n", "version 1.10\n"),
                         (SIGN_OFF_HEADING, SIGN_OFF_HEADING + "\nEvery automated agent or bot must add one.\n"),
                         (SIGN_OFF_HEADING, SIGN_OFF_HEADING + "\n"),  # a blank line more at its top
                         ("are not re-signed.\n", "are not re-signed.\n\n")):  # and at its end
            with self.subTest(new=new):
                saved = (self.root / "CONTRIBUTING.md").read_bytes()
                self.edit("CONTRIBUTING.md", old, new)
                self.assertNamed(r"^CONTRIBUTING\.md's sign-off section is not SIGN_OFF")
                (self.root / "CONTRIBUTING.md").write_bytes(saved)

    def test_one_sign_off_section(self) -> None:
        for old, new in ((SIGN_OFF_HEADING, "## Sign off\n"), (SIGN_OFF_HEADING, SIGN_OFF_HEADING + SIGN_OFF + SIGN_OFF_HEADING)):
            with self.subTest(new=new[:40]):
                saved = (self.root / "CONTRIBUTING.md").read_bytes()
                self.edit("CONTRIBUTING.md", old, new)
                self.assertNamed(r"^CONTRIBUTING\.md does not have exactly one sign-off section")
                (self.root / "CONTRIBUTING.md").write_bytes(saved)

    def test_security_without_a_part_or_a_sentence(self) -> None:
        for removed in ("> **Draft.**", "## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                        "## What the code defends, and what it does not yet\n",
                        "Report it privately to <contact: Lucas to name>.",
                        "Please do not report a suspected vulnerability in a public issue"):
            with self.subTest(removed=removed):
                saved = (self.root / "SECURITY.md").read_bytes()
                self.edit("SECURITY.md", removed, "")
                self.assertNamed(r"^SECURITY\.md (has no|does not say) " + re.escape(repr(removed.strip())))
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_a_security_heading_starts_its_line(self) -> None:
        self.edit("SECURITY.md", "## Scope\n", "Out of line: ## Scope\n")
        self.assertNamed(r"^SECURITY\.md has no '## Scope'")

    def test_security_parts_hidden_from_the_reader(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for wrapped in ("<!--\n" + text + "-->\n", "```\n" + text + "```\n"):
            with self.subTest(wrapped=wrapped[:4]):
                (self.root / "SECURITY.md").write_text(wrapped, encoding="utf-8")
                for part in ("> **Draft.**", "## Reporting a vulnerability", "## Supported versions", "## Scope",
                             "## What the code defends, and what it does not yet"):
                    self.assertIn(f"SECURITY.md has no {part!r}", self.found())

    def test_an_empty_security_section(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        emptied = re.sub(r"(## Supported versions\n).*?(## Scope\n)", r"\1\n\2", text, flags=re.S)
        self.assertNotEqual(emptied, text)
        (self.root / "SECURITY.md").write_text(emptied, encoding="utf-8")
        self.assertNamed(r"^SECURITY\.md's section '## Supported versions' is empty")

    def test_security_gutted_to_its_two_sentences(self) -> None:
        (self.root / "SECURITY.md").write_text(
            "Please do not report a suspected vulnerability in a public issue.\n\n"
            f"Report it privately to {CONTACT}.\n", encoding="utf-8")
        found = self.found()
        for part in ("> **Draft.**", "## Reporting a vulnerability", "## Supported versions", "## Scope",
                     "## What the code defends, and what it does not yet"):
            self.assertIn(f"SECURITY.md has no {part!r}", found)

    def test_security_with_an_address(self) -> None:
        for address in ("jane.doe@example.org", "triage@localhost"):
            with self.subTest(address=address):
                saved = (self.root / "SECURITY.md").read_bytes()
                with (self.root / "SECURITY.md").open("a", encoding="utf-8") as f:
                    f.write(f"\nOr write to {address}.\n")
                self.assertNamed(r"^SECURITY\.md holds an e-mail address that is not its contact")
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_a_named_contact_may_be_the_one_address(self) -> None:
        with mock.patch.dict(globals(), {"CONTACT": "security@example.org"}):
            self.edit("SECURITY.md", "<contact: Lucas to name>", "security@example.org")
            self.assertEqual(self.found(), [])
            with (self.root / "SECURITY.md").open("a", encoding="utf-8") as f:
                f.write("\nOr write to jane.doe@example.org.\n")
            self.assertNamed(r"^SECURITY\.md holds an e-mail address that is not its contact")


if __name__ == "__main__":
    unittest.main()
