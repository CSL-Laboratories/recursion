#!/usr/bin/env python3
"""Controls for scripts/verify-branch-protection.py — the esc-0001 close-out verifier.

The verifier's whole value is that it will not say "protected" unless it checked. So the
controls that matter most are the ones where it learned nothing:

  * no token          -> exit 3, not 0. An unauthenticated run knows nothing about protection.
  * non-GitHub remote -> UNVERIFIABLE for that row, and no crash.
  * unprotected       -> exit 1, from a real 404 + empty ruleset list.
  * nothing checkable -> exit 3. A portfolio that is empty, all-comment, all-inactive or
                         all-malformed checked zero rows, which is not a clean bill of health.

And two that matter for a different reason. No credential may appear in stdout, stderr, or the
evidence file — neither the env token nor one embedded in a portfolio remote, which scrub()
cannot see — because this tool is the thing a human pastes into a ticket. And the Bearer header
must never follow a redirect: a 3xx is refused, so the redirect target sees no request at all.

The GitHub API is stubbed by two local http.servers bound to 127.0.0.1 (one API stub, one sink
for the redirect control) and reached through the RECURSION_GH_API_BASE test hook. Nothing here
touches the network.

  run: python scripts/tests/verify-branch-protection.test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
VERIFIER = SOURCE / "scripts" / "verify-branch-protection.py"
TOKEN = "ghp_TESTONLYtoken0000000000000000000000"
# Deliberately NOT the env token and not a substring of it: a credential that only ever exists
# inside a portfolio remote URL, so scrub() has no way to recognize it.
URL_TOKEN = "ghp_INURLONLYcredential11111111111111111"

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


# --- stub API -------------------------------------------------------------------------

# owner/repo -> {"classic": status, "rules": [...] or None (=> non-200),
#                "redirect": base URL to 302 to}
REPOS: dict[str, dict] = {}
SEEN_AUTH: list[str] = []

# Every request the redirect target received. It must stay empty.
SINK_REQUESTS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # keep the test output clean
        return

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        SEEN_AUTH.append(self.headers.get("Authorization", ""))
        parts = [segment for segment in self.path.split("/") if segment]
        # /repos/{owner}/{repo}/branches/{branch}/protection
        # /repos/{owner}/{repo}/rules/branches/{branch}
        if len(parts) < 5 or parts[0] != "repos":
            self._send(404, {"message": "Not Found"})
            return
        slug = f"{parts[1]}/{parts[2]}"
        config = REPOS.get(slug)
        if config is None:
            self._send(404, {"message": "Not Found"})
            return
        target = config.get("redirect")
        if target:
            # A 302 to a different origin. The sink answers 200 "protected", so a verifier that
            # followed the redirect would come back CLEAN — and would have handed over the
            # Bearer header to get there.
            self.send_response(302)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parts[3] == "branches" and parts[-1] == "protection":
            status = config.get("classic", 404)
            self._send(status, {"enabled": True} if status == 200 else {"message": "Branch not protected"})
            return
        if parts[3] == "rules" and parts[4] == "branches":
            rules = config.get("rules")
            if rules is None:
                self._send(500, {"message": "boom"})
                return
            self._send(200, rules)
            return
        self._send(404, {"message": "Not Found"})


class SinkHandler(BaseHTTPRequestHandler):
    """The redirect target. Records what reached it and claims the branch is protected."""

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        SINK_REQUESTS.append({"path": self.path,
                              "auth": self.headers.get("Authorization", "")})
        body = json.dumps({"enabled": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(handler) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}"


# --- fixture root ---------------------------------------------------------------------


def make_root(tmp: Path, rows: list[str], tasks: dict[str, str] | None = None) -> Path:
    root = tmp / "root"
    (root / "state" / "tasks").mkdir(parents=True, exist_ok=True)
    header = "#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus"
    body = "\n".join([
        "# portfolio.tsv — fixture",
        header,
        *rows,
    ]) + "\n"
    (root / "state" / "portfolio.tsv").write_text(body, encoding="utf-8", newline="\n")
    for name, text in (tasks or {}).items():
        (root / "state" / "tasks" / name).write_text(text, encoding="utf-8", newline="\n")
    return root


def row(project: str, remote: str, branch: str = "main", status: str = "active") -> str:
    return "\t".join([project, f"/opt/{project}", remote, branch, "-", "-", "-", status])


def clean_env() -> dict:
    """A caller's real credentials must never leak into a fixture run."""
    env = dict(os.environ)
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "RECURSION_ALLOW_AUTOMERGE",
                 "RECURSION_GH_API_BASE", "RECURSION_ROOT"):
        env.pop(name, None)
    env["PYTHONIOENCODING"] = "utf-8"  # the box is UTF-8; Windows pipes are not
    return env


def launch(root: Path, env: dict, json_path: Path | None = None):
    argv = [sys.executable, str(VERIFIER), "--root", str(root)]
    if json_path is not None:
        argv += ["--json", str(json_path)]
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace")


def run(root: Path, base: str | None, token: str | None, json_path: Path | None = None):
    env = clean_env()
    if base is not None:
        env["RECURSION_GH_API_BASE"] = base
    if token is not None:
        env["GITHUB_TOKEN"] = token
    return launch(root, env, json_path)


def main() -> int:
    server, base = start_server(Handler)
    sink, sink_base = start_server(SinkHandler)
    captured: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)

            REPOS.clear()
            REPOS["own/classic"] = {"classic": 200}
            REPOS["own/ruleset"] = {"classic": 404, "rules": [
                {"type": "non_fast_forward"},
                {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
            ]}
            REPOS["own/bare"] = {"classic": 404, "rules": []}
            REPOS["own/bypassable"] = {"classic": 404, "rules": [
                {"type": "pull_request", "current_user_can_bypass": "always"},
            ]}
            REPOS["own/forbidden"] = {"classic": 403}
            REPOS["own/redirect"] = {"redirect": sink_base}

            # 1. env violation ---------------------------------------------------------
            root = make_root(tmp / "c1", [row("classic", "git@github.com:own/classic.git")])
            env = clean_env()
            env.update({"RECURSION_ALLOW_AUTOMERGE": "1", "GITHUB_TOKEN": TOKEN,
                        "RECURSION_GH_API_BASE": base})
            done = launch(root, env)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 1,
                  f"RECURSION_ALLOW_AUTOMERGE=1 is a violation, exit 1 (got {done.returncode})")
            check("RECURSION_ALLOW_AUTOMERGE" in done.stdout and "VIOLATION" in done.stdout,
                  "the env violation line names the flag")

            # 2. task automerge violation, naming the file -----------------------------
            root = make_root(
                tmp / "c2", [row("classic", "git@github.com:own/classic.git")],
                tasks={
                    "example-ops-001.md": "id: example-ops-001\nautomerge: yes\nautomerge_scope: apps/example-ops\n"
                                     "status: passed\n\nbody mentioning automerge: yes harmlessly\n",
                    "example-ops-002.md": "id: example-ops-002\nautomerge: no\nstatus: passed\n\nbody\n",
                    "example-ops-003.md": "id: example-ops-003\nstatus: queued\n\nautomerge: yes\n",
                })
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 1,
                  f"a task carrying automerge is a violation, exit 1 (got {done.returncode})")
            check("state/tasks/example-ops-001.md" in done.stdout,
                  "the task violation names the offending file")
            check("example-ops-002" not in done.stdout,
                  "`automerge: no` is not reported as a violation")
            check("example-ops-003" not in done.stdout,
                  "* an automerge line in the BODY is not a header grant (header ends at the "
                  "first blank line)")

            # 3. protected via classic protection --------------------------------------
            root = make_root(tmp / "c3", [row("classic", "git@github.com:own/classic.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 0,
                  f"classic branch protection verifies clean, exit 0 (got {done.returncode})")
            check("PROTECTED" in done.stdout and "classic" in done.stdout,
                  "the protected line records classic protection as the evidence")
            check("verdict: CLEAN" in done.stdout,
                  "* exit 0 is still reachable — a row that really was checked and is protected")

            # 4. protected via a ruleset ------------------------------------------------
            root = make_root(tmp / "c4", [row("ruleset", "https://github.com/own/ruleset")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 0,
                  f"a pull_request ruleset verifies clean, exit 0 (got {done.returncode})")
            check("pull_request" in done.stdout,
                  "the protected line records which ruleset rule blocks the push")

            # 5. unprotected -> violation ----------------------------------------------
            root = make_root(tmp / "c5", [row("bare", "git@github.com:own/bare.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 1,
                  f"* 404 classic + empty rulesets is UNPROTECTED, exit 1 (got {done.returncode})")
            check("UNPROTECTED" in done.stdout, "the unprotected row is labelled UNPROTECTED")

            # 5b. a ruleset the caller can bypass is not protection ---------------------
            root = make_root(tmp / "c5b", [row("bypassable", "git@github.com:own/bypassable.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 1,
                  f"a bypassable pull_request rule is not protection, exit 1 "
                  f"(got {done.returncode})")

            # 6. no token -> fail closed, exit 3 ---------------------------------------
            root = make_root(tmp / "c6", [row("classic", "git@github.com:own/classic.git")])
            done = run(root, base, None)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3,
                  f"* no token exits 3, never 0 — an unchecked branch is not a clean one "
                  f"(got {done.returncode})")
            check("UNVERIFIABLE" in done.stdout and "verdict: UNVERIFIABLE" in done.stdout,
                  "the tokenless verdict is UNVERIFIABLE, not clean")

            # 7. non-GitHub remote -> unverifiable, no crash ----------------------------
            root = make_root(tmp / "c7", [row("selfhosted", "git@gitlab.example.com:own/x.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3,
                  f"a non-GitHub remote is UNVERIFIABLE, exit 3 (got {done.returncode})")
            check("Traceback" not in done.stderr,
                  "a non-GitHub remote does not crash the verifier")
            check("git@gitlab.example.com:own/x.git" in done.stdout,
                  "* a conventional `git@` ssh remote is echoed intact — redaction is for "
                  "credentials, not for every '@'")

            # 7b. an HTTP error is UNVERIFIABLE, not UNPROTECTED ------------------------
            root = make_root(tmp / "c7b", [row("forbidden", "git@github.com:own/forbidden.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3 and "UNPROTECTED" not in done.stdout,
                  f"* HTTP 403 is UNVERIFIABLE, never UNPROTECTED (got {done.returncode})")

            # 8. malformed portfolio row: skipped and counted ---------------------------
            root = make_root(tmp / "c8", [
                row("classic", "git@github.com:own/classic.git"),
                "truncated\t/opt/truncated\tgit@github.com:own/classic.git\tmain",
            ])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check("SKIPPED" in done.stdout and "1 malformed row" in done.stdout,
                  "a short portfolio row is skipped and counted, not silently dropped")
            check(done.returncode == 3,
                  f"* a skipped row is a row nobody checked: exit 3, not 0 — even alongside a "
                  f"protected row (got {done.returncode})")

            # 8b. an all-malformed portfolio checks nothing -----------------------------
            # portfolio.tsv is hand-edited. One lost tab used to turn the honest exit 3 into
            # exit 0 with zero rows verified, retiring esc-0001 on a fact nobody checked.
            root = make_root(tmp / "c8b", [
                "truncated\t/opt/truncated\tgit@github.com:own/classic.git\tmain",
                "alsoshort\t/opt/alsoshort\tgit@github.com:own/bare.git",
            ])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3,
                  f"* an all-malformed portfolio is UNVERIFIABLE, never CLEAN "
                  f"(got {done.returncode})")
            check("no ACTIVE portfolio row" in done.stdout,
                  "the run says out loud that no active row could be checked")

            # 8c. a comments-only portfolio checks nothing ------------------------------
            root = make_root(tmp / "c8c", [])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3,
                  f"* a portfolio with no data rows at all is UNVERIFIABLE, never CLEAN "
                  f"(got {done.returncode})")
            check("verdict: UNVERIFIABLE" in done.stdout,
                  "the empty-portfolio verdict is UNVERIFIABLE, not clean")

            # 8d. an all-inactive portfolio checks nothing ------------------------------
            root = make_root(tmp / "c8d", [
                row("classic", "git@github.com:own/classic.git", status="pending"),
                row("bare", "git@github.com:own/bare.git", status="archived"),
            ])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3,
                  f"* an all-inactive portfolio is UNVERIFIABLE, never CLEAN "
                  f"(got {done.returncode})")

            # 9. evidence file shape ----------------------------------------------------
            evidence_path = tmp / "evidence" / "esc-0001.json"
            root = make_root(tmp / "c9", [
                row("classic", "git@github.com:own/classic.git"),
                row("bare", "git@github.com:own/bare.git"),
                row("pendingrepo", "git@github.com:own/bare.git", status="pending"),
            ])
            done = run(root, base, TOKEN, evidence_path)
            captured += [done.stdout, done.stderr]
            record = json.loads(evidence_path.read_text(encoding="utf-8"))
            check(set(record) == {"schema_version", "checked_at", "results", "verdict"},
                  f"evidence record has exactly the contract keys (got {sorted(record)})")
            check(record["schema_version"] == 1 and record["verdict"] == "violation",
                  "evidence records schema_version 1 and the run's verdict")
            check(record["checked_at"].endswith("Z") and len(record["checked_at"]) == 20,
                  f"checked_at is a UTC instant (got {record['checked_at']!r})")
            remotes = [item for item in record["results"] if item["check"] == "remote"]
            check(len(remotes) == 2,
                  f"only ACTIVE portfolio rows are checked (got {len(remotes)} remote results)")
            check(all({"check", "status", "subject", "detail"} <= set(item)
                      for item in record["results"]),
                  "every evidence result carries check/status/subject/detail")
            raw = evidence_path.read_text(encoding="utf-8")
            check("\r\n" not in raw and raw.endswith("\n"), "evidence is LF-terminated")
            check(raw == json.dumps(record, indent=2, sort_keys=True) + "\n",
                  "evidence is written sorted and stable (byte-identical on re-dump)")
            check(not any(name.startswith(".esc-0001.json.tmp")
                          for name in os.listdir(evidence_path.parent)),
                  "the atomic write leaves no temp file behind")

            # 10. no credential appears anywhere ---------------------------------------
            # A credential-in-URL remote is the realistic leak: the row is unrecognized, so the
            # verifier echoes it for the human to fix — and that echo must be redacted. The
            # embedded secret is URL_TOKEN, which the environment does NOT carry, so scrub()
            # cannot catch it; only redaction of the userinfo can. (Embedding the env token
            # here would make the case pass for the wrong reason.) The third row keeps scrub()
            # itself honest: a token outside any userinfo is still caught by value.
            leak_path = tmp / "evidence" / "leak.json"
            root = make_root(tmp / "c10", [
                row("credleak", f"https://x-access-token:{URL_TOKEN}@github.com/own/classic.git"),
                row("sshcred", f"deploy:{URL_TOKEN}@github.com:own/classic.git"),
                row("envleak", f"weird://{TOKEN}-not-userinfo/own/classic.git"),
            ])
            done = run(root, base, TOKEN, leak_path)
            leaked = leak_path.read_text(encoding="utf-8")
            captured += [done.stdout, done.stderr, leaked]
            check(all(URL_TOKEN not in text for text in (done.stdout, done.stderr, leaked)),
                  "* an in-URL credential the environment never knew reaches no output: not "
                  "stdout, not stderr, not the evidence file")
            check("<redacted>@github.com/own/classic.git" in done.stdout
                  and "<redacted>@github.com/own/classic.git" in leaked,
                  "* the https userinfo is replaced, not the whole remote")
            check("<redacted>@github.com:own/classic.git" in done.stdout,
                  "* ssh-form userinfo carrying a ':' is redacted too")
            check("github.com/own/classic.git" in done.stdout,
                  "the redacted echo still shows the human which remote to fix")
            check("***" in done.stdout and "***" in leaked,
                  "the env token is still scrubbed by value wherever it appears")

            # 11. an authenticated call never follows a redirect ------------------------
            # urllib's default opener copies Authorization onto the redirect target, so one
            # redirecting (or hijacked) endpoint harvests the token. The stub 302s to a sink
            # that would answer 200 "protected": exit 0 here would mean the hop happened.
            SINK_REQUESTS.clear()
            root = make_root(tmp / "c11", [row("redirect", "git@github.com:own/redirect.git")])
            done = run(root, base, TOKEN)
            captured += [done.stdout, done.stderr]
            check(done.returncode == 3 and "UNVERIFIABLE" in done.stdout,
                  f"* a redirected API call is UNVERIFIABLE for that row, exit 3 "
                  f"(got {done.returncode})")
            check("redirect" in done.stdout and "PROTECTED" not in done.stdout,
                  "the refused redirect is reported, and never as protection")
            check(SINK_REQUESTS == [],
                  "* the redirect target receives no request at all")
            check(not any(entry["auth"] for entry in SINK_REQUESTS),
                  "* the Authorization header never travels to the redirect target")

            captured.append(raw)
            check(all(TOKEN not in text for text in captured),
                  "* the token never appears in stdout, stderr, or the evidence file")
            check(all(URL_TOKEN not in text for text in captured),
                  "* no in-URL credential appears in any output of any case")
            check(any(item == f"Bearer {TOKEN}" for item in SEEN_AUTH),
                  "the token IS actually sent as a Bearer header (the scrub is not a no-op "
                  "hiding a tokenless request)")
    finally:
        for httpd in (server, sink):
            httpd.shutdown()
            httpd.server_close()

    if failures:
        print(f"verify-branch-protection.test: {len(failures)} FAILED")
        return 1
    print("verify-branch-protection.test: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
