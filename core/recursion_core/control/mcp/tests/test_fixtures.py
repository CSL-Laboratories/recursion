"""Replays every framing, negotiation and tools fixture through the real stdio loop (``server.serve`` on a pipe).

Each fixture under ``fixtures/legacy/<group>/`` is the wire, byte for byte where it matters: what the client
writes, the exact answers in order, the calls the control API must receive and nothing more. See the
README in the package folder for the format.
"""

from __future__ import annotations

import unittest

from recursion_core.control.mcp.tests.support import FIXTURES, fixture_files, replay

EXPECTED = {
    "framing": ["01-crlf-and-blank-lines", "02-parse-error-keeps-serving", "03-not-utf8", "04-non-json-constants",
                "05-batches-refused", "06-not-an-object", "07-jsonrpc-version", "08-request-ids",
                "09-params-must-be-an-object", "10-ignored-messages", "11-no-usable-method",
                "12-last-line-without-newline", "13-two-messages-in-one-write"],
    "negotiation": ["01-initialize-2025-06-18", "02-initialize-newer-legacy-revision", "03-initialize-modern-revision",
                    "04-initialize-older-revision", "05-discover-probe-then-initialize",
                    "06-requests-before-initialize", "07-initialize-twice", "08-initialize-bad-params",
                    "09-unknown-methods-after-initialize"],
    "tools": ["01-tools-list", "02-start-status-stop-list", "03-unknown-tool", "04-invalid-arguments",
              "05-arguments-omitted", "06-unavailable-is-never-stopped"],
}


class FixtureInventoryTest(unittest.TestCase):
    def test_every_fixture_is_present_and_no_other(self) -> None:
        found = {}
        for path in fixture_files():
            found.setdefault(path.parent.name, []).append(path.stem)
        self.assertEqual(EXPECTED, {group: sorted(names) for group, names in found.items()}, str(FIXTURES))


class FixtureReplayTest(unittest.TestCase):
    def replay_group(self, group: str) -> None:
        paths = [p for p in fixture_files() if p.parent.name == group]
        self.assertEqual(len(EXPECTED[group]), len(paths))
        for path in paths:
            with self.subTest(fixture=f"{group}/{path.stem}"):
                replay(self, path)

    def test_framing(self) -> None:
        self.replay_group("framing")

    def test_negotiation(self) -> None:
        self.replay_group("negotiation")

    def test_tools(self) -> None:
        self.replay_group("tools")


if __name__ == "__main__":
    unittest.main()
