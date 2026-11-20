"""Credentials that a route result carries must not reach result.json.

The daemon writes the result dict with json.dump (control_adapter._record_result).
These tests write that same file and read it back. The values are synthetic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import contract as c

# Nine credential rows. Each marker is the part that must not survive into the file.
_SHAPES = (
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
    ("api_key", "sk-ant-api03-supersecretvalue"),
    ("aws", "AKIA0123456789ABCDEF"),
    ("bearer", "Bearer ab+cd/ef=="),
    ("github", "ghp_" + ("a" * 20)),
    ("github_pat", "github_pat_" + ("b" * 22)),
    ("slack", "xoxb-" + ("c" * 12)),
    ("google", "ya29." + ("d" * 12)),
    ("userinfo", "https://user:s3cret@example.com/v1"),
)


def _result_json(result: dict) -> str:
    """Write the route result the way the daemon writes runs/<id>/result.json."""

    directory = Path(tempfile.mkdtemp(prefix="route-result-"))
    path = directory / "result.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True)
        handle.flush()
    return path.read_text(encoding="utf-8")


class CredentialReachesResultJsonTest(unittest.TestCase):
    def test_url_userinfo_and_each_credential_shape_reach_result_json_redacted(self) -> None:
        for name, secret in _SHAPES:
            with self.subTest(name=name):
                marker = "user:s3cret" if name == "userinfo" else secret
                result = c.result(
                    "r1",
                    c.ENGINE_ERROR,
                    text=f"answer {secret}",
                    detail=f"detail {secret}",
                    limit={"scope": "connection", "model": None, "reset_at": None, "source": secret},
                    events=[c.event("text", "engine", {
                        "note": secret,
                        "nested": {"inner": secret},
                        "rows": [secret],
                        "n": 1,
                    })],
                )
                blob = _result_json(result)
                self.assertNotIn(marker, blob)
                self.assertIn("[redacted]", blob)
                if name == "userinfo":
                    self.assertIn("example.com/v1", blob)

    def test_percent_encoded_userinfo_reaches_result_json_redacted(self) -> None:
        raw = "prefix https:%2F%2Fuser:dummy_password@example.invalid/hook suffix"
        result = c.result(
            "r2", c.ENGINE_ERROR, text=raw, detail=raw,
            events=[c.event("retry", "vendor", {"error": raw})],
        )
        blob = _result_json(result)
        self.assertNotIn("dummy_password", blob)
        self.assertIn("example.invalid", blob)
        self.assertIn("prefix", blob)
        self.assertIn("[redacted]", blob)
        self.assertNotEqual(json.loads(blob)["detail"], "[redacted]")

    def test_a_json_escaped_api_key_reaches_result_json_redacted(self) -> None:
        raw = "key sk-\\u002dabcdefghij end"
        blob = _result_json(c.result("r3", c.ENGINE_ERROR, detail=raw, text=raw))
        self.assertNotIn("abcdefghij", blob)
        self.assertIn("key ", blob)
        self.assertIn(" end", blob)
        self.assertIn("[redacted]", blob)

    def test_a_port_and_ordinary_words_stay_in_result_json(self) -> None:
        raw = "task-12345678 failed; rate 100% of %20 budget; https://example.com:443/a@b"
        blob = _result_json(c.result("r4", None, text=raw, detail=raw))
        self.assertIn("task-12345678 failed", blob)
        self.assertIn("rate 100% of %20 budget", blob)
        self.assertIn("https://example.com:443/a@b", blob)
        self.assertNotIn("[redacted]", blob)

    def test_a_hosted_password_is_redacted_and_the_port_stays(self) -> None:
        raw = "https://user:dummy_password@example.invalid:443/hook"
        blob = _result_json(c.result("r5", c.ENGINE_ERROR, detail=raw))
        self.assertNotIn("dummy_password", blob)
        self.assertIn("example.invalid:443", blob)

    def test_a_numeric_password_is_redacted_and_a_numeric_port_is_not(self) -> None:
        password = _result_json(c.result(
            "r6", c.ENGINE_ERROR, detail="https://user:8080@example.invalid/hook"))
        self.assertNotIn("8080", password)
        self.assertIn("example.invalid", password)
        port = _result_json(c.result(
            "r7", None, detail="https://example.com:8080/files/a@b.txt"))
        self.assertIn("https://example.com:8080/files/a@b.txt", port)
        self.assertNotIn("[redacted]", port)

    def test_a_refusal_still_redacts_and_keeps_its_code(self) -> None:
        blob = _result_json(c.result(
            "r8", c.LAUNCH_REFUSED, detail="https://user:s3cret@example.invalid/hook"))
        parsed = json.loads(blob)
        self.assertEqual(parsed["code"], "launch_refused")
        self.assertNotIn("s3cret", blob)
        self.assertIn("example.invalid", blob)

    def test_a_second_result_does_not_carry_the_first_secret(self) -> None:
        first = _result_json(c.result("r9", c.ENGINE_ERROR, detail="sk-ant-api03-supersecretvalue"))
        second = _result_json(c.result("r10", None, text="plain answer", detail="recursion deadline"))
        self.assertNotIn("sk-ant-api03-supersecretvalue", first)
        self.assertIn("[redacted]", first)
        self.assertIn("plain answer", second)
        self.assertIn("recursion deadline", second)
        self.assertNotIn("sk-ant", second)
        self.assertNotIn("[redacted]", second)

    def test_an_unfinished_escape_replaces_the_field(self) -> None:
        slash = "\\" * 9 + "/"
        raw = "prefix https:" + slash + slash + "user:dummy_password@example.invalid/hook suffix"
        blob = _result_json(c.result("r11", c.ENGINE_ERROR, detail=raw))
        self.assertNotIn("dummy_password", blob)
        self.assertIn("[redacted]", blob)


if __name__ == "__main__":
    unittest.main()
