#!/usr/bin/env python3
"""Control: the immutable assignment contract, digest, and secret screen.

Frozen contract: scripts/remote_assignment.py and docs/plans/remote-execution-adapters.md.

The properties under test are the ones both remote adapters rest on:
  * source_sha is a FULL 40-hex commit -- a branch, tag, short sha or HEAD is refused;
  * the digest is total and order-independent, and changes when any field changes;
  * a credential shape cannot reach a durable record or an outbound envelope;
  * the screen is not so broad that ordinary text ("risk-register") trips it;
  * the error taxonomy maps each code to one retryable flag and one exit code.

Hermetic: no filesystem outside tempdirs, no network, no environment on the box.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))

import remote_assignment as ra  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


def raises(code: str, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except ra.RemoteError as exc:
        if exc.code == code:
            return True
        print(f"     (got {exc.code}, wanted {code}: {exc})")
        return False
    except Exception as exc:  # noqa: BLE001 - a wrong exception type is a failure, not a crash
        print(f"     (got {type(exc).__name__}: {exc})")
        return False
    return False


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"

ASSIGNMENT = {
    "task_id": "t-0042",
    "project_id": "example-ops",
    "source_repository": "https://github.com/NotSkittlz/example-ops",
    "source_branch": "main",
    "source_sha": SHA,
    "engine": "cursor",
    "provider": "cursor",
    "capacity_pool": "cursor-cloud-pool",
    "host": "cursor-cloud",
    "sandbox_profile": "vendor-cloud",
    "publication_policy": "verified-integration-only",
    "created_at": "2026-08-28T12:00:00Z",
}


def variant(**overrides) -> dict:
    out = dict(ASSIGNMENT)
    out.update(overrides)
    return out


# --- normalisation ---------------------------------------------------------------------

normalised = ra.validate_assignment(ASSIGNMENT)
check(set(normalised) == ra.ASSIGNMENT_KEYS,
      "a normalised assignment carries every key, so the digest is total")
check(normalised["allowed_paths"] == [] and normalised["model_parameters"] == {}
      and normalised["model"] == "",
      "absent optional fields are defaulted rather than omitted")
check(ra.validate_assignment(normalised) == normalised,
      "normalisation is idempotent")

# --- digest ----------------------------------------------------------------------------

reordered = dict(reversed(list(ASSIGNMENT.items())))
check(ra.assignment_digest(ASSIGNMENT) == ra.assignment_digest(reordered),
      "the digest is independent of key order")
check(ra.assignment_digest(ASSIGNMENT) != ra.assignment_digest(variant(source_sha=OTHER_SHA)),
      "rebinding the source changes the digest")
check(ra.assignment_digest(ASSIGNMENT)
      != ra.assignment_digest(variant(capacity_pool="grok-bot-weekly")),
      "rebinding the capacity pool changes the digest")
check(ra.assignment_digest(ASSIGNMENT) != ra.assignment_digest(variant(model="grok-4.6")),
      "adding a model changes the digest")
check(len(ra.assignment_digest(ASSIGNMENT)) == 64,
      "the digest is a sha256 hex string")

# --- exact source sha -------------------------------------------------------------------

check(raises("assignment-invalid", ra.validate_assignment, variant(source_sha=SHA[:12])),
      "an abbreviated sha is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(source_sha="main")),
      "a branch name in source_sha is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(source_sha="HEAD")),
      "HEAD is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(source_sha=SHA.upper())),
      "an uppercase sha is refused (one canonical form, so the digest is stable)")

# --- repository references ---------------------------------------------------------------

check(raises("assignment-invalid", ra.validate_assignment,
             variant(source_repository="git@github.com:NotSkittlz/example-ops.git")),
      "an scp-style ssh remote is refused")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(source_repository="https://user:pw@github.com/NotSkittlz/example-ops")),
      "a URL carrying userinfo is refused -- that is a credential in a non-secret field")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(source_repository="http://github.com/NotSkittlz/example-ops")),
      "a plain http remote is refused")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(source_repository="https://github.com")),
      "an https URL with no repository path is refused")
check(ra.validate_https_url("https://api.cursor.com", "endpoint") == "https://api.cursor.com",
      "an endpoint URL with no path is accepted when a path is not required")
check(raises("assignment-invalid", ra.validate_https_url,
             "https://api.cursor.com/x?token=1", "endpoint"),
      "a query string is refused unless the caller allows one")
check(ra.validate_https_url("https://hooks.example.com/r/1?k=2", "endpoint",
                            allow_query=True).endswith("?k=2"),
      "a webhook URL may carry a query string when the caller allows one")
check(raises("assignment-invalid", ra.validate_https_url,
             "https://[::1", "endpoint"),
      "malformed URL authority syntax is a typed assignment refusal, not a ValueError")

# --- structural refusals ------------------------------------------------------------------

check(raises("assignment-invalid", ra.validate_assignment, variant(unexpected="x")),
      "an unknown assignment key is refused")
incomplete = dict(ASSIGNMENT)
del incomplete["host"]
check(raises("assignment-invalid", ra.validate_assignment, incomplete),
      "a missing required key is refused")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(publication_policy="whatever")),
      "an unknown publication policy is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(created_at="2026-08-28")),
      "a non-UTC-ISO timestamp is refused")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(source_branch="bad branch")),
      "a branch name with a space is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(source_branch="a..b")),
      "a branch name with a '..' is refused")
for invalid_ref in (".", "foo.", "foo@{bar}", "foo/.bar"):
    check(raises("assignment-invalid", ra.validate_assignment,
                 variant(source_branch=invalid_ref)),
          f"git-invalid branch ref {invalid_ref!r} is refused")


def git_branch_valid(value: str) -> bool:
    try:
        return subprocess.run(
            ["git", "check-ref-format", "--branch", value],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except (OSError, ValueError):
        return False


# This corpus deliberately mixes boundaries, every ASCII byte class, component rules, and
# ordinary punctuation.  The runtime validator must not grow a second approximation of Git's
# ref parser: its answer is compared directly with the Git 2.46 executable available to CI.
branch_corpus = [
    "", "@", ".", "..", "foo", "foo.", "foo..bar", "foo@{bar", "foo@{bar}",
    "foo/.bar", "foo/.", "foo.lock", "foo/bar.lock", "foo/.lock", "-foo", "foo/-bar",
    "foo//bar", "/foo", "foo/", "foo bar", "foo~bar", "foo^bar", "foo:bar",
    "foo?bar", "foo*bar", "foo[bar", "foo\\bar", "foo./bar", "foo@bar", "@/x",
    "x/@", "x/a.lock-y", "x/a.locked", "refs/heads/main", "é/ß",
    "x" * 255, "x" * 256, "x" * 1024,
]
branch_corpus.extend(chr(code) for code in range(0x00, 0x20))
branch_corpus.extend(f"a{chr(code)}b" for code in range(0x20, 0x80))
branch_corpus.extend(f"a/{chr(code)}b" for code in range(0x80, 0x100))
branch_mismatches = []
for candidate in branch_corpus:
    expected = git_branch_valid(candidate)
    try:
        ra.validate_branch_name(candidate, "corpus")
        actual = True
    except ra.RemoteError:
        actual = False
    if actual != expected:
        branch_mismatches.append((repr(candidate), expected, actual))
check(not branch_mismatches,
      "validate_branch_name matches Git 2.46 check-ref-format --branch across the broad corpus"
      + (f": {branch_mismatches[:3]}" if branch_mismatches else ""))
check(ra.validate_branch_name("@", "branch") == "@",
      "Git-valid single-character @ is accepted")
check(ra.validate_branch_name("x" * 256, "branch") == "x" * 256,
      "Git-valid 256-character branch is accepted without an arbitrary cap")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(allowed_paths=["../etc/passwd"])),
      "an allowed_path escaping the worktree is refused")
check(raises("assignment-invalid", ra.validate_assignment, variant(allowed_paths=["/etc"])),
      "an absolute allowed_path is refused")
hostile_path = "C:/attacker/credential.txt"
try:
    ra.validate_assignment(variant(allowed_paths=[hostile_path]))
except ra.RemoteError as exc:
    path_error = str(exc)
else:
    path_error = ""
check(hostile_path not in path_error and "absolute path" in path_error,
      "an absolute allowed_path refusal uses a stable symbolic message without echoing the path")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(allowed_paths=["a/b", "a/b"])),
      "a duplicate allowed_path is refused")
check(ra.validate_assignment(variant(allowed_paths=["b/c", "a/d"]))["allowed_paths"]
      == ["a/d", "b/c"],
      "allowed_paths are sorted, so the digest does not depend on listing order")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(model_parameters={"nested": {"a": 1}})),
      "a nested model parameter is refused")

# --- secret references ----------------------------------------------------------------------

check(ra.validate_secret_ref("secret:CURSOR_CLOUD_API_KEY", "credential_ref")
      == "CURSOR_CLOUD_API_KEY",
      "a well-formed secret reference resolves to its env var name")
check(raises("manifest-invalid", ra.validate_secret_ref, "sk-not-a-reference-at-all",
             "credential_ref"),
      "a literal value where a reference belongs is refused")
check(raises("manifest-invalid", ra.validate_secret_ref, "secret:lowercase", "credential_ref"),
      "a lowercase env name is refused")
for controller in ("secret:GITHUB_TOKEN", "secret:TELEGRAM_BOT_TOKEN",
                   "secret:OPENROUTER_API_KEY", "secret:RECURSION_ALLOW_METERED",
                   "secret:SSH_AUTH_SOCK", "secret:HERMES_HOME"):
    check(raises("manifest-invalid", ra.validate_secret_ref, controller, "credential_ref"),
          f"a reference to the controller credential {controller} is refused")

probe = "RECURSION_TEST_ABSENT_CREDENTIAL_PROBE"
os.environ.pop(probe, None)
check(raises("credential-absent", ra.resolve_secret, probe, "credential_ref"),
      "an unset credential is 'absent' (exit 3), not a crash")
os.environ[probe] = "   "
check(raises("credential-absent", ra.resolve_secret, probe, "credential_ref"),
      "a whitespace-only credential counts as absent")
os.environ[probe] = "value"
check(ra.resolve_secret(probe, "credential_ref") == "value",
      "a set credential resolves to its value")
os.environ.pop(probe, None)

# --- the secret-material screen -----------------------------------------------------------

clean = {"note": "risk-register", "branch": "recursion/extern/task-sk-1",
         "path": "docs/keystone.md", "idempotency_key": "a" * 64}
try:
    ra.screen_for_secret_material(clean, "clean")
    screened_clean = True
except ra.RemoteError as exc:
    screened_clean = False
    print(f"     (unexpected refusal: {exc})")
check(screened_clean,
      "ordinary text containing 'sk-', 'key' and a key-suffixed field name is NOT refused")

for label, payload in (
    ("an OpenAI-style key", {"note": "sk-" + "a" * 32}),
    ("a GitHub token", {"note": "ghp_" + "b" * 36}),
    ("a GitHub fine-grained PAT", {"note": "github_pat_" + "c" * 40}),
    ("an xAI key", {"note": "xai-" + "d" * 24}),
    ("an AWS access key id", {"note": "AKIA" + "E" * 16}),
    ("a PEM private key", {"note": "-----BEGIN RSA PRIVATE KEY-----"}),
    ("a JWT", {"note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0"}),
    ("an Authorization header", {"headers": {"Authorization": "Bearer abcdefghijkl"}}),
):
    check(raises("redaction-violation", ra.screen_for_secret_material, payload, "payload"),
          f"{label} is refused before it can reach a durable record")

check(raises("redaction-violation", ra.screen_for_secret_material,
             {"api_key": "anything"}, "payload"),
      "a credential-shaped KEY NAME is refused even when the value looks harmless")
check(raises("redaction-violation", ra.screen_for_secret_material,
             {"session_cookie": "x"}, "payload"),
      "a cookie-shaped key name is refused")
try:
    ra.screen_for_secret_material(
        {"credential_ref": "secret:MY_BACKEND_KEY"}, "manifest",
        allowed_reference_paths={("credential_ref",)})
    ref_exempt = True
except ra.RemoteError:
    ref_exempt = False
check(ref_exempt, "only an explicitly authorized schema path may hold a validated reference")
check(raises("redaction-violation", ra.screen_for_secret_material,
             {"credential_ref": "secret:GITHUB_TOKEN"}, "session",
             allowed_reference_paths={("credential_ref",)}),
      "a controller secret is refused even at an otherwise authorized reference path")
check(raises("redaction-violation", ra.screen_for_secret_material,
             {"nested": {"credential_ref": "secret:MY_BACKEND_KEY"}}, "session",
             allowed_reference_paths={("credential_ref",)}),
      "a nested credential_ref cannot borrow a top-level schema exemption")
check(raises("redaction-violation", ra.screen_for_secret_material,
             {"arbitrary_ref": "secret:SOME_BACKEND_KEY"}, "payload"),
      "an arbitrary *_ref field is not an exemption for a secret reference")
check(raises("redaction-violation", ra.screen_for_secret_material,
             {"note": "use secret:MY_KEY here"}, "payload"),
      "a secret reference embedded in free text is refused")
check(raises("assignment-invalid", ra.validate_assignment,
             variant(model_parameters={"note": "ghp_" + "z" * 36})),
      "the assignment validator runs the same screen, reported as assignment-invalid")

leaked = ra.looks_like_secret_material("ghp_" + "q" * 36)
check(leaked == "github-token", "the screen returns a LABEL, never the matched secret")
try:
    ra.screen_for_secret_material({"note": "ghp_" + "q" * 36}, "payload")
    message = ""
except ra.RemoteError as exc:
    message = str(exc)
check("q" * 36 not in message,
      "the refusal message does not echo the credential it caught")

# --- assignment_reference ---------------------------------------------------------------

reference = ra.assignment_reference(variant(allowed_paths=["src/a.py"],
                                            network_profile="provider-only"))
check("sandbox_profile" not in reference and "network_profile" not in reference
      and "allowed_paths" not in reference,
      "the transmitted reference withholds the containment shape from the executor")
check(reference["source_sha"] == SHA and reference["assignment_digest"]
      == ra.assignment_digest(variant(allowed_paths=["src/a.py"],
                                      network_profile="provider-only")),
      "the transmitted reference carries the exact sha and the full assignment digest")

# --- the error taxonomy -------------------------------------------------------------------

expected = {
    "manifest-absent": (False, 3),
    "adapter-disabled": (False, 3),
    "credential-absent": (False, 3),
    "auth-failed": (False, 3),
    "manifest-invalid": (False, 2),
    "assignment-invalid": (False, 2),
    "malformed-response": (False, 2),
    "paused": (False, 4),
    "unauthorized-result": (False, 4),
    "redaction-violation": (False, 4),
    "result-channel-violation": (False, 4),
    "unreconciled-attempt": (False, 4),
    "live-transport-disabled": (False, 4),
    "transport-timeout": (True, 10),
    "rate-limited": (True, 10),
    "provider-error": (True, 10),
    "lease-unavailable": (True, 75),
    "quota-exhausted": (True, 75),
    "provider-unavailable": (True, 75),
}
for code, (retryable, exit_code) in expected.items():
    error = ra.RemoteError(code, "probe")
    check(error.retryable is retryable and error.exit_code == exit_code,
          f"{code} is retryable={retryable} exit={exit_code}")
check(error.as_record()["code"] == "provider-unavailable",
      "an error serialises to a record carrying its code, retryability and exit code")
try:
    ra.RemoteError("invented-code", "probe")
    unknown_rejected = False
except KeyError:
    unknown_rejected = True
check(unknown_rejected, "an error code outside the taxonomy cannot be constructed")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("all checks passed")
