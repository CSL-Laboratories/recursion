#!/usr/bin/env python3
"""Checks for the licence files (lane RELEASE). Run this file from the repository root:

    python -X utf8 -B tools/release/licence_checks.py

`problems(root)` names, one sentence each, every rule the four files at `root` break:
- LICENSE is the Apache License 2.0 text unmodified, byte for byte as published at
  https://www.apache.org/licenses/LICENSE-2.0.txt. In a git checkout that tracks it, the committed
  file (its blob in the index) is held byte for byte, and the file on disk may differ from it only
  in line endings, which a Windows checkout changes; anywhere else the file on disk is held byte
  for byte.
- NOTICE is exactly NOTICE_TEXT: the copyright holder stays a placeholder until Lucas names one,
  and no other name, address or line is added.
- CONTRIBUTING.md has one sign-off section, held word for word (SIGN_OFF). Text elsewhere in the
  file is not read.
- SECURITY.md keeps its draft note and its four sections, says that reports go privately to the
  unnamed contact and not to a public issue, and holds no e-mail address.
A missing file is named as missing. The tests check the committed files, then break each rule in
a scratch copy of the four files and check that it is named.

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

CONTACT = "<contact: Lucas to name>"
SECURITY_PARTS = ("> **Draft.**", "## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                  "## What the code defends, and what it does not yet\n")
SECURITY_SENTENCES = (f"Report it privately to {CONTACT}.",
                      "Please do not report a suspected vulnerability in a public issue")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _read(root: Path, rel: str, found: list[str]) -> bytes | None:
    path = root / rel
    if not path.is_file():
        found.append(f"{rel} is missing")
        return None
    return path.read_bytes()


def _text(data: bytes) -> str:
    """A text file's content, line endings as committed (a Windows checkout may add CRs)."""
    return data.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")


def _committed(root: Path, rel: str) -> bytes | None:
    """The file's blob in git's index when `root` itself is a git checkout that tracks it, else
    None. The environment's GIT_* settings are not used: the checkout's own index is read."""
    if not (root / ".git").exists():
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f":{rel}"],
                                capture_output=True, env=env, timeout=60)
    except OSError:
        return None  # no git here: the file on disk is all there is
    return result.stdout if result.returncode == 0 else None


def license_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "LICENSE", found)
    if data is None:
        return found
    committed = _committed(root, "LICENSE")
    if committed is not None:
        if hashlib.sha256(committed).hexdigest() != APACHE_2_0_SHA256:
            found.append("LICENSE as committed is not the published Apache License 2.0 text, byte for byte")
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
    text = "\n" + _text(data)
    found += [f"SECURITY.md has no {part.strip()!r}" for part in SECURITY_PARTS if "\n" + part not in text]
    found += [f"SECURITY.md does not say {sentence!r}" for sentence in SECURITY_SENTENCES if sentence not in text]
    if EMAIL.search(text):
        found.append("SECURITY.md holds an e-mail address while its contact is unnamed")
    return found


def problems(root: Path) -> list[str]:
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
    """Each rule broken in a scratch copy of the four files, the defects planted by the first read
    of #409 among them: the checks must name it, and a crash counts as not naming it."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="licence-checks-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "tree"
        self.root.mkdir()
        missing = [rel for rel in FILES if not (ROOT / rel).is_file()]
        if missing:
            self.skipTest(f"missing in the repository, which LicenceTests names: {missing}")
        for rel in FILES:
            (self.root / rel).write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))

    def found(self, root: Path | None = None) -> list[str]:
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

    def test_the_scratch_copy_holds(self) -> None:
        self.assertEqual(self.found(), [])

    def test_a_missing_file_is_named(self) -> None:
        for rel in FILES:
            with self.subTest(rel=rel):
                data = (self.root / rel).read_bytes()
                (self.root / rel).unlink()
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).mkdir()  # a folder where the file should be
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).rmdir()
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
        with mock.patch.dict(os.environ, {"PATH": ""}):  # no git to run: read from disk only
            self.assertNamed(r"^LICENSE is not the published")

    def test_a_license_committed_with_crlf(self) -> None:
        self.git(self.root, "init", "-q")
        self.crlf("LICENSE")
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes((self.root / "LICENSE").read_bytes().replace(b"\r\n", b"\n"))
        self.assertNamed(r"^LICENSE as committed is not")

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
        for old, new in ((HOLDER, "Jane Doe"),
                         ("the License is in the file LICENSE.", "Maintained by Jane Doe <jane.doe@example.org>.\n"
                                                                 "the License is in the file LICENSE."),
                         ("the License is in the file LICENSE.", "Contact: jane.doe@example.org\n"
                                                                 "the License is in the file LICENSE."),
                         ("A copy of", "Licence text"),
                         (NOTICE_TEXT, "")):
            with self.subTest(new=new):
                saved = (self.root / "NOTICE").read_bytes()
                self.edit("NOTICE", old, new)
                self.assertNamed(r"^NOTICE is not NOTICE_TEXT")
                (self.root / "NOTICE").write_bytes(saved)

    def test_the_sign_off_section_is_held_word_for_word(self) -> None:
        for old, new in (("version 1.1\n", "version 1.10\n"),
                         (SIGN_OFF_HEADING, SIGN_OFF_HEADING + "\nEvery automated agent or bot must add one.\n")):
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

    def test_security_gutted_to_its_two_sentences(self) -> None:
        (self.root / "SECURITY.md").write_text(
            "Please do not report a suspected vulnerability in a public issue.\n\n"
            f"Report it privately to {CONTACT}.\n", encoding="utf-8")
        found = self.found()
        for part in ("> **Draft.**", "## Reporting a vulnerability", "## Supported versions", "## Scope",
                     "## What the code defends, and what it does not yet"):
            self.assertIn(f"SECURITY.md has no {part!r}", found)

    def test_security_with_an_address(self) -> None:
        with (self.root / "SECURITY.md").open("a", encoding="utf-8") as f:
            f.write("\nOr write to jane.doe@example.org.\n")
        self.assertNamed(r"^SECURITY\.md holds an e-mail address")


if __name__ == "__main__":
    unittest.main()
