#!/usr/bin/env python3
"""Offline checker for the CSL-357 agent work protocol (spec draft v3).

Run from the repository root:

    python -X utf8 -B tools/agent_protocol_sim.py snapshot.json

`evaluate(snapshot)` reads a recorded snapshot of Linear issues and comments,
commits, agent writes, CI runs and standing-approval actions, and reports live
claims, winners, holds, takeovers, decisions and protocol violations. It makes
no network calls and writes nothing.

A clean result shows the snapshot is consistent with the rules. It does not
show that agents follow them, that a comment's author is who it claims to be,
or that a founder set the work order: authorship is not verifiable while
agents post through developer accounts (spec section 2).

Snapshot format (all timestamps ISO 8601 with a UTC offset):

    now                 required; the evaluation time
    repo                default repository for claims without a `repo:` line
    config              optional overrides of DEFAULT_CONFIG
    issues              required; list of
        {id, status, admitted, next_in_order, blocked_by: [ids],
         decision: {options: [..], pings: [ts..], options_changed_at: [ts..],
                    deadline: ts | null} | null}
    comments            required; list of
        {id, issue, created_at, body, edits: [{at, body}], deleted, deleted_at}
    commits             list of {sha, repo, branch, at, author_kind, claim}
                        author_kind: agent | human | bot; claim: uuid | null
    writes              agent writes: list of {kind, issue, claim, at}
                        kind: push | pr | label | resolve | status
    runs                list of {id, claim, started_at, ended_at | null}
    program             {review_slots, occupied_reviews, completed_waiting}
    admissions          list of {issue, at}
    sa_actions          list of {sa, action, repo, agent, at}

Comment conventions follow the spec. An agent comment starts with
`Agent (<tool>/<session>, for <developer>):`; a claim record is an agent
comment whose first line ends with `claim v1`. Human lines (`REVOKE CLAIM`,
`HOLD`, `DECISION:`, batched `CSL-N: X; ...`, `ACK TAKEOVER`, `Standing
approval SA-n`, `REVOKE SA-n`) count only in comments without the agent
prefix, and keep the effect they had before a known deletion time. Standing
approvals use machine fields: `actions:` and `scope:` are comma-separated
tokens, `limits:` holds `per_day=N` and/or `total=N`, and `expires:` is a date;
each action is checked against the comment revision in effect at its time. An
agent records a decision answer on the decision issue with the line
`decision-record: <answer comment id> <option>`. `confirmed-at:` holds a
timestamp or a placeholder in parentheses.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "lease_minutes": 120,
    "grace_minutes": 30,
    "readback_seconds": 60,
    "takeover_wait_minutes": 15,
    "coordinator_ack_required": False,
    "standing_approvals_issue": None,
    "sa_max_days": 90,
}

AGENT_PREFIX = re.compile(r"^Agent \(([^)]*)\):")
CLAIM_HEADER = re.compile(r"claim v1\s*$")
FIELD = re.compile(r"^([a-z-]+):\s*(.*)$")
ISSUE_ID = re.compile(r"^CSL-[0-9]+$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REVOKE_CLAIM = re.compile(r"^REVOKE CLAIM (\S+)\s*$")
HOLD = re.compile(r"^HOLD (CSL-[0-9]+)\s*$")
DECISION = re.compile(r"^DECISION:\s*([A-Za-z0-9_-]+)")
BATCH = re.compile(r"CSL-[0-9]+:\s*[A-Za-z0-9_-]+[^;]*(?:;\s*CSL-[0-9]+:\s*[A-Za-z0-9_-]+[^;]*)*;?\s*")
BATCH_PAIR = re.compile(r"(CSL-[0-9]+):\s*([A-Za-z0-9_-]+)")
ACK_TAKEOVER = re.compile(r"^ACK TAKEOVER (\S+)\s*$")
SA_HEADER = re.compile(r"^Standing approval (SA-[0-9]+)\s*$")
REVOKE_SA = re.compile(r"^REVOKE (SA-[0-9]+)\s*$")
TAKEOVER = re.compile(r"^takeover of (\S+)\s*$")
TIMESTAMP_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
PLACEHOLDER = re.compile(r"^\([^()\n]*[^()\s][^()\n]*\)$")
DECISION_RECORD = re.compile(r"^decision-record:\s*(\S+)\s+([A-Za-z0-9_-]+)\s*$")
AFTER_END = {
    "deleted": "after_deletion",
    "expired": "after_expiry",
    "revoked": "after_revoke",
    "released": "after_release",
    "withdrawn": "after_withdrawal",
}
WRITE_KINDS = {"push", "pr", "label", "resolve", "status"}
AUTHOR_KINDS = {"agent", "human", "bot"}
STATUSES = {"Triage", "Backlog", "Todo", "In Progress", "In Review", "Done", "Canceled", "Duplicate"}


class SnapshotError(ValueError):
    """The snapshot is malformed; evaluation fails closed."""


def parse_ts(value: Any, where: str) -> datetime:
    if not isinstance(value, str):
        raise SnapshotError(f"{where}: timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"{where}: bad timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise SnapshotError(f"{where}: timestamp {value!r} has no UTC offset")
    return parsed.astimezone(timezone.utc)


def require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise SnapshotError(f"{where}: missing {key!r}")
    return mapping[key]


def expect_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotError(f"{where} must be true or false")
    return value


def expect_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotError(f"{where}: expected a list")
    return value


def agent_header(body: str) -> tuple[str, str] | None:
    """Return (session, developer) for an agent-prefixed body, else None."""
    first = body.split("\n", 1)[0]
    match = AGENT_PREFIX.match(first)
    if not match:
        return None
    inner = match.group(1)
    session, _, developer = inner.partition(", for ")
    return session.strip(), developer.strip()


@dataclasses.dataclass
class Revision:
    at: datetime
    body: str


@dataclasses.dataclass
class Comment:
    id: str
    issue: str
    created_at: datetime
    revisions: list[Revision]
    deleted: bool
    deleted_at: datetime | None = None

    @property
    def body(self) -> str:
        return self.revisions[-1].body

    @property
    def agent(self) -> tuple[str, str] | None:
        """The agent header, if any revision carried one.

        A comment that ever had the agent prefix stays an agent comment, so an
        edit that removes the prefix cannot turn its lines into human
        directives. `prefix_changed` reports the edit.
        """
        for revision in self.revisions:
            header = agent_header(revision.body)
            if header is not None:
                return header
        return None

    @property
    def prefix_changed(self) -> bool:
        # Any change to the (session, developer) identity counts, not only
        # adding or removing the prefix.
        return len({agent_header(r.body) for r in self.revisions}) > 1

    def last_update(self, at: datetime) -> datetime:
        return max(r.at for r in self.revisions if r.at <= at)


@dataclasses.dataclass
class Claim:
    uuid: str
    issue: str
    repo: str
    session: str
    branch: str
    comment: Comment
    takeover_of: str | None

    @property
    def created_at(self) -> datetime:
        return self.comment.created_at

    def _first_revision(self, predicate) -> datetime | None:
        for revision in self.comment.revisions:
            if predicate(claim_fields(revision.body)):
                return revision.at
        return None

    def confirmed_at(self) -> datetime | None:
        """When the claim counts as confirmed: the later of the edit that set
        `confirmed-at` and the time it declares. An edit cannot backdate a
        confirmation, and a declared future time is not reached early."""
        for revision in self.comment.revisions:
            value = claim_fields(revision.body).get("confirmed-at", "")
            if TIMESTAMP_VALUE.match(value):
                return max(revision.at, parse_ts(value, f"claim {self.uuid}: confirmed-at"))
        return None

    def confirm_edit_at(self) -> datetime | None:
        """When the edit that set `confirmed-at` was made (for the read-back check)."""
        return self._first_revision(lambda f: bool(TIMESTAMP_VALUE.match(f.get("confirmed-at", ""))))

    def released_at(self) -> datetime | None:
        return self._first_revision(lambda f: f.get("_released", False))

    def withdrawn_at(self) -> datetime | None:
        return self._first_revision(lambda f: "withdrawn" in f)


def claim_fields(body: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line in body.split("\n")[1:]:
        line = line.strip()
        if line == "released" or line == "state: released":
            fields["_released"] = True
            continue
        takeover = TAKEOVER.match(line)
        if takeover:
            fields["takeover"] = takeover.group(1)
            continue
        match = FIELD.match(line)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    return fields


@dataclasses.dataclass
class StandingApproval:
    sa: str
    comment: Comment
    since: datetime
    actions: set[str]
    scope: set[str]
    who: set[str] | None
    per_day: int | None
    total: int | None
    expires: datetime


def human_lines(comment: Comment) -> list[str]:
    if comment.deleted:
        return []
    return [line.strip() for line in comment.body.split("\n")]


def first_seen(comment: Comment, line: str) -> datetime | None:
    """When `line` first appeared in the comment, from its revision history."""
    for revision in comment.revisions:
        if line in (part.strip() for part in revision.body.split("\n")):
            return revision.at
    return None


def present_at(comment: Comment, line: str, at: datetime) -> bool:
    """Whether `line` is in the revision of the comment in effect at `at`."""
    effective = [r for r in comment.revisions if r.at <= at]
    return bool(effective) and line in (part.strip() for part in effective[-1].body.split("\n"))


def present_since(comment: Comment, line: str, at: datetime) -> datetime | None:
    """When `line` last became present without a break up to `at`, or None
    if the revision in effect at `at` does not contain it."""
    since = None
    for revision in comment.revisions:
        if revision.at > at:
            break
        if line in (part.strip() for part in revision.body.split("\n")):
            since = revision.at if since is None else since
        else:
            since = None
    return since


class Evaluation:
    def __init__(self, snapshot: dict[str, Any]):
        if not isinstance(snapshot, dict):
            raise SnapshotError("snapshot must be an object")
        self.now = parse_ts(require(snapshot, "now", "snapshot"), "now")
        self.repo = snapshot.get("repo", "default")
        if not isinstance(self.repo, str) or not self.repo:
            raise SnapshotError("repo must be a non-empty string")
        config = snapshot.get("config", {})
        unknown = set(config) - set(DEFAULT_CONFIG)
        if unknown:
            raise SnapshotError(f"config: unknown keys {sorted(unknown)}")
        self.config = {**DEFAULT_CONFIG, **config}
        for key in ("lease_minutes", "grace_minutes", "readback_seconds", "takeover_wait_minutes", "sa_max_days"):
            value = self.config[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**6:
                raise SnapshotError(f"config: {key} must be an integer from 0 to 1000000")
        if not isinstance(self.config["coordinator_ack_required"], bool):
            raise SnapshotError("config: coordinator_ack_required must be a boolean")
        if self.config["standing_approvals_issue"] is not None and not isinstance(self.config["standing_approvals_issue"], str):
            raise SnapshotError("config: standing_approvals_issue must be an issue id or null")
        self.lease = timedelta(minutes=self.config["lease_minutes"])
        self.grace = timedelta(minutes=self.config["grace_minutes"])
        self.readback = timedelta(seconds=self.config["readback_seconds"])
        self.takeover_wait = timedelta(minutes=self.config["takeover_wait_minutes"])
        self._revoked: dict[str, datetime | None] = {}
        self.violations: list[dict[str, Any]] = []
        self.void_lines: list[dict[str, str]] = []
        self.issues = self._load_issues(require(snapshot, "issues", "snapshot"))
        self.comments = self._load_comments(require(snapshot, "comments", "snapshot"))
        self.claims = self._load_claims()
        self.commits = self._load_list(snapshot, "commits", ("sha", "branch", "at", "author_kind"))
        self.writes = self._load_list(snapshot, "writes", ("kind", "issue", "claim", "at"))
        self.runs = self._load_list(snapshot, "runs", ("id", "claim", "started_at"))
        self.admissions = self._load_list(snapshot, "admissions", ("issue", "at"))
        self.sa_actions = self._load_list(snapshot, "sa_actions", ("sa", "action", "repo", "agent", "at"))
        self.program = self._load_program(snapshot.get("program"))
        for commit in self.commits:
            if not isinstance(commit["author_kind"], str) or commit["author_kind"] not in AUTHOR_KINDS:
                raise SnapshotError(f"commit {commit['sha']}: unknown author_kind")
        for write in self.writes:
            if not isinstance(write["kind"], str) or write["kind"] not in WRITE_KINDS:
                raise SnapshotError(f"write: unknown kind {write['kind']!r}")
            self._known_issue(write["issue"], "write")
        for admission in self.admissions:
            self._known_issue(admission["issue"], "admission")
        for run in self.runs:
            if not isinstance(run["claim"], str) or run["claim"] not in self.claims:
                raise SnapshotError(f"run {run['id']}: unknown claim {run['claim']!r}")
        for action in self.sa_actions:
            if not isinstance(action["agent"], str) or not action["agent"].strip():
                raise SnapshotError("sa_actions: agent must be a non-empty string")

    # Loading ---------------------------------------------------------------

    def _known_issue(self, issue: str, where: str) -> None:
        if issue not in self.issues:
            raise SnapshotError(f"{where}: unknown issue {issue!r}")

    def _load_issues(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, list):
            raise SnapshotError("issues must be a list")
        issues: dict[str, dict[str, Any]] = {}
        for item in raw:
            issue_id = require(item, "id", "issue")
            if not isinstance(issue_id, str) or not ISSUE_ID.match(issue_id):
                raise SnapshotError(f"issue: bad id {issue_id!r}")
            if issue_id in issues:
                raise SnapshotError(f"issue {issue_id}: duplicate")
            status = require(item, "status", issue_id)
            if status not in STATUSES:
                raise SnapshotError(f"issue {issue_id}: unknown status {status!r}")
            decision = item.get("decision")
            if decision is not None:
                options = require(decision, "options", issue_id)
                if not isinstance(options, list) or not options:
                    raise SnapshotError(f"issue {issue_id}: decision needs options")
                decision = {
                    "options": [str(option) for option in options],
                    "pings": [parse_ts(t, issue_id) for t in expect_list(decision.get("pings", []), issue_id)],
                    "options_changed_at": [
                        parse_ts(t, issue_id) for t in expect_list(decision.get("options_changed_at", []), issue_id)
                    ],
                    "deadline": parse_ts(decision["deadline"], issue_id) if decision.get("deadline") else None,
                }
            admitted = expect_bool(item.get("admitted", False), f"issue {issue_id}: admitted")
            if status == "Triage" and admitted:
                raise SnapshotError(f"issue {issue_id}: a Triage issue cannot be admitted")
            issues[issue_id] = {
                "status": status,
                "admitted": admitted,
                "next_in_order": expect_bool(item.get("next_in_order", False), f"issue {issue_id}: next_in_order"),
                "blocked_by": [str(b) for b in expect_list(item.get("blocked_by", []), issue_id)],
                "decision": decision,
            }
        for issue_id, issue in issues.items():
            for blocker in issue["blocked_by"]:
                self._check_issue_ref(issues, blocker, issue_id)
        return issues

    @staticmethod
    def _check_issue_ref(issues: dict[str, Any], ref: str, where: str) -> None:
        if ref not in issues:
            raise SnapshotError(f"issue {where}: blocked_by unknown issue {ref!r}")

    def _load_comments(self, raw: Any) -> list[Comment]:
        if not isinstance(raw, list):
            raise SnapshotError("comments must be a list")
        seen: set[str] = set()
        comments = []
        for item in raw:
            comment_id = require(item, "id", "comment")
            if comment_id in seen:
                raise SnapshotError(f"comment {comment_id}: duplicate id")
            seen.add(comment_id)
            issue = require(item, "issue", comment_id)
            self._known_issue(issue, f"comment {comment_id}")
            created = parse_ts(require(item, "created_at", comment_id), comment_id)
            if created > self.now:
                raise SnapshotError(f"comment {comment_id}: created after the snapshot time")
            body = require(item, "body", comment_id)
            if not isinstance(body, str):
                raise SnapshotError(f"comment {comment_id}: body must be a string")
            revisions = [Revision(created, body)]
            for edit in expect_list(item.get("edits", []), comment_id):
                at = parse_ts(require(edit, "at", comment_id), comment_id)
                if at < revisions[-1].at:
                    raise SnapshotError(f"comment {comment_id}: edits out of order")
                if at > self.now:
                    raise SnapshotError(f"comment {comment_id}: edited after the snapshot time")
                edited = require(edit, "body", comment_id)
                if not isinstance(edited, str):
                    raise SnapshotError(f"comment {comment_id}: edit body must be a string")
                revisions.append(Revision(at, edited))
            deleted = expect_bool(item.get("deleted", False), f"comment {comment_id}: deleted")
            deleted_at = item.get("deleted_at")
            if deleted_at is not None:
                if not deleted:
                    raise SnapshotError(f"comment {comment_id}: deleted_at without deleted")
                deleted_at = parse_ts(deleted_at, comment_id)
                if deleted_at > self.now or deleted_at < created:
                    raise SnapshotError(f"comment {comment_id}: deleted_at outside the comment's life")
                if revisions[-1].at > deleted_at:
                    raise SnapshotError(f"comment {comment_id}: edited after deleted_at")
            comments.append(Comment(comment_id, issue, created, revisions, deleted, deleted_at))
        return comments

    def _load_list(self, snapshot: dict[str, Any], key: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
        raw = snapshot.get(key, [])
        if not isinstance(raw, list):
            raise SnapshotError(f"{key} must be a list")
        items = []
        for item in raw:
            entry = dict(item) if isinstance(item, dict) else None
            for field in required:
                require(entry, field, key)
            for field in ("at", "started_at", "ended_at"):
                if field not in entry:
                    continue
                if entry[field] is None and field == "ended_at":
                    continue
                entry[field] = parse_ts(entry[field], key)
                if entry[field] > self.now:
                    raise SnapshotError(f"{key}: {field} after the snapshot time")
            if entry.get("ended_at") is not None and "started_at" in entry and entry["ended_at"] < entry["started_at"]:
                raise SnapshotError(f"{key}: ended_at before started_at")
            items.append(entry)
        return items

    @staticmethod
    def _load_program(raw: Any) -> dict[str, int] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise SnapshotError("program must be an object")
        program = {}
        for key in ("review_slots", "occupied_reviews", "completed_waiting"):
            value = raw.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SnapshotError(f"program: {key} must be a non-negative integer")
            program[key] = value
        if program["review_slots"] == 0:
            raise SnapshotError("program: review_slots must be positive")
        return program

    def _load_claims(self) -> dict[str, Claim]:
        claims: dict[str, Claim] = {}
        for comment in self.comments:
            header = comment.agent
            first = comment.revisions[0].body.split("\n", 1)[0]
            if not header or not CLAIM_HEADER.search(first):
                continue
            tool, slash, session = header[0].partition("/")
            if not (tool.strip() and slash and session.strip() and header[1]):
                raise SnapshotError(f"claim comment {comment.id}: incomplete agent prefix")
            fields = claim_fields(comment.revisions[0].body)
            for key in ("claim", "scope", "branch"):
                if not fields.get(key):
                    raise SnapshotError(f"claim comment {comment.id}: missing {key!r}")
            def identity(found: dict[str, Any]) -> dict[str, Any]:
                branch = found.get("branch")
                return {
                    "claim": found.get("claim"),
                    "scope": found.get("scope"),
                    "branch": branch.split(" @", 1)[0].strip() if isinstance(branch, str) else branch,
                    "repo": found.get("repo"),
                    "takeover": found.get("takeover"),
                }

            original = identity(fields)
            for revision in comment.revisions:
                revised = claim_fields(revision.body)
                value = revised.get("confirmed-at", "")
                if value and not PLACEHOLDER.match(value):
                    parse_ts(value, f"claim comment {comment.id}: confirmed-at")
                # A claim's identity is fixed when it is posted; an edit may
                # confirm, renew or release it but not change or drop a field.
                changed = sorted(k for k, v in identity(revised).items() if v != original[k])
                if changed:
                    raise SnapshotError(f"claim comment {comment.id}: edit changes {', '.join(changed)}")
            uuid = fields["claim"]
            if not UUID.match(uuid):
                raise SnapshotError(f"claim comment {comment.id}: bad uuid {uuid!r}")
            if uuid in claims:
                raise SnapshotError(f"claim {uuid}: duplicate")
            branch = fields["branch"].split(" @", 1)[0].strip()
            claims[uuid] = Claim(
                uuid=uuid,
                issue=comment.issue,
                repo=fields.get("repo", self.repo),
                session=header[0],
                branch=branch,
                comment=comment,
                takeover_of=fields.get("takeover"),
            )
        return claims

    # Helpers ---------------------------------------------------------------

    def violation(self, code: str, issue: str | None = None, claim: str | None = None, detail: str = "") -> None:
        entry = {"code": code, "issue": issue, "claim": claim, "detail": detail}
        if entry not in self.violations:
            self.violations.append(entry)

    def human_comments(self) -> list[Comment]:
        return [c for c in self.comments if not c.deleted and c.agent is None]

    def directive_lines(self, issue: str | None = None) -> list[tuple[Comment, str, datetime]]:
        """(comment, line, first seen) for every line ever in a human comment.

        A comment deleted at a known time keeps the lines it had before the
        deletion; one deleted at an unknown time keeps none.
        """
        found = []
        for comment in self.comments:
            if comment.agent is not None or (issue is not None and comment.issue != issue):
                continue
            if comment.deleted and comment.deleted_at is None:
                continue
            lines = {part.strip() for revision in comment.revisions for part in revision.body.split("\n")}
            for line in sorted(lines):
                seen = first_seen(comment, line)
                if seen is not None and (comment.deleted_at is None or seen < comment.deleted_at):
                    found.append((comment, line, seen))
        return found

    def revoked_at(self, claim: Claim) -> datetime | None:
        """When a revoke line naming the claim first appeared (edits are not backdated)."""
        if claim.uuid in self._revoked:
            return self._revoked[claim.uuid]
        times = [
            seen for _, line, seen in self.directive_lines(claim.issue)
            if (m := REVOKE_CLAIM.match(line)) and m.group(1) == claim.uuid
        ]
        self._revoked[claim.uuid] = min(times) if times else None
        return self._revoked[claim.uuid]

    def ended(self, claim: Claim, at: datetime) -> str | None:
        """Why the claim is not live at `at`, or None if it is live."""
        if claim.created_at > at:
            return "not yet posted"
        if claim.comment.deleted and (claim.comment.deleted_at is None or claim.comment.deleted_at <= at):
            return "deleted"
        for label, when in (
            ("withdrawn", claim.withdrawn_at()),
            ("released", claim.released_at()),
            ("revoked", self.revoked_at(claim)),
        ):
            if when is not None and when <= at:
                return label
        if at > claim.comment.last_update(at) + self.lease:
            return "expired"
        return None

    def takeover_status(self, claim: Claim, at: datetime | None = None) -> dict[str, Any] | None:
        if claim.takeover_of is None:
            return None
        at = self.now if at is None else at
        old = self.claims.get(claim.takeover_of)
        posted = claim.created_at
        if old is None or old.issue != claim.issue or old.repo != claim.repo:
            return {"of": claim.takeover_of, "allowed": False, "reason": "unknown claim"}
        if old.created_at >= posted:
            return {"of": old.uuid, "allowed": False, "reason": "original posted after the takeover"}
        if at < posted + self.takeover_wait:
            return {"of": old.uuid, "allowed": False, "reason": "takeover wait not over"}
        objection = [
            r.at for r in old.comment.revisions
            if posted <= r.at <= posted + self.takeover_wait
            and not claim_fields(r.body).get("_released") and "withdrawn" not in claim_fields(r.body)
        ]
        finished = self.ended(old, at)
        if objection and finished not in {"withdrawn", "released", "revoked", "deleted"}:
            # A renewal objects to the takeover, but a later release, withdrawal,
            # revoke or deletion of the original ends that objection.
            return {"of": old.uuid, "allowed": False, "reason": "original claimant renewed within the wait"}
        if objection:
            return {"of": old.uuid, "allowed": True, "reason": f"original {finished} after objecting"}
        ended = self.ended(old, posted)
        if ended in {"withdrawn", "released", "revoked", "deleted"}:
            return {"of": old.uuid, "allowed": True, "reason": f"original {ended}"}
        refusal = self.posting_refusal(claim, old)
        if refusal is not None:
            return refusal
        if self.config["coordinator_ack_required"]:
            # The acknowledgement must still be in the comment, and the comment
            # not deleted, at the time being judged.
            acked = any(
                (m := ACK_TAKEOVER.match(line)) and m.group(1) == old.uuid
                and (since := present_since(c, line, at)) is not None and posted <= since
                and not (c.deleted_at is not None and c.deleted_at <= at)
                for c, line, _ in self.directive_lines(claim.issue)
            )
            if not acked:
                return {"of": old.uuid, "allowed": False, "reason": "coordinator acknowledgement missing"}
        return {"of": old.uuid, "allowed": True, "reason": "conditions met"}

    def posting_refusal(self, claim: Claim, old: Claim) -> dict[str, Any] | None:
        """Why a takeover of a still-held `old` was not allowed when posted, or None."""
        posted = claim.created_at
        last = old.comment.last_update(posted)
        if posted <= last + self.lease + self.grace:
            return {"of": old.uuid, "allowed": False, "reason": "lease plus grace not passed"}
        newer = [
            c for c in self.commits
            if c.get("repo", self.repo) == old.repo and c["branch"] == old.branch and last < c["at"] <= posted
        ]
        if newer:
            return {"of": old.uuid, "allowed": False, "reason": "commit newer than the last heartbeat"}
        running = [
            r for r in self.runs
            if r["claim"] == old.uuid and r["started_at"] <= posted and (r.get("ended_at") is None or r["ended_at"] > posted)
        ]
        if running:
            return {"of": old.uuid, "allowed": False, "reason": "claimant run in progress"}
        return None

    def live_claims(self, issue: str, repo: str, at: datetime) -> list[Claim]:
        candidates = [
            c for c in self.claims.values()
            if c.issue == issue and c.repo == repo and self.ended(c, at) is None
        ]
        superseded = set()
        refused = set()
        for claim in candidates:
            status = self.takeover_status(claim, at)
            if status is None:
                continue
            if status["allowed"] and any(
                other.uuid != claim.uuid and other.uuid != claim.takeover_of
                and other.issue == claim.issue and other.repo == claim.repo
                and other.created_at < claim.created_at and self.bypassed_expired(claim, other)
                for other in self.claims.values()
            ):
                # Bypasses another expired claim, as check_implicit_takeover reports.
                refused.add(claim.uuid)
            elif status["allowed"]:
                superseded.add(status["of"])
            else:
                refused.add(claim.uuid)
        return [c for c in candidates if c.uuid not in superseded and c.uuid not in refused]

    def winner(self, issue: str, repo: str, at: datetime) -> Claim | None:
        live = self.live_claims(issue, repo, at)
        if not live:
            return None
        return min(live, key=lambda c: (c.created_at, c.uuid))

    # Decisions -------------------------------------------------------------

    def answer_pairs(self, comment: Comment, body: str) -> list[tuple[str, str, str]]:
        """(decision issue, option, line) pairs a comment body states."""
        lines = [line.strip() for line in body.split("\n")]
        pairs = []
        for line in lines:
            match = DECISION.match(line)
            if match:
                pairs.append((comment.issue, match.group(1), line))
        # A batched answer is the comment's first line. In an agent comment the
        # first line is the agent prefix, so the batch (to be voided) is line 1.
        first = lines[1] if comment.agent is not None and len(lines) > 1 else lines[0]
        if BATCH.fullmatch(first):
            named = [m.groups() for clause in first.split(";") if (m := BATCH_PAIR.match(clause.strip()))]
            on_decision_issue = self.issues[comment.issue]["decision"] is not None
            if on_decision_issue and comment.issue in {issue for issue, _ in named}:
                pairs.extend((issue, value, first) for issue, value in named)
        return pairs

    def void(self, comment: Comment, line: str) -> None:
        entry = {"comment": comment.id, "line": line, "reason": "agent-prefixed"}
        if entry not in self.void_lines:
            self.void_lines.append(entry)

    def decisions(self) -> dict[str, dict[str, Any]]:
        stated: list[tuple[Comment, list[tuple[str, str, str]]]] = []
        for comment in sorted(self.comments, key=lambda c: (c.created_at, c.id)):
            if comment.deleted and comment.deleted_at is None:
                continue
            original = self.answer_pairs(comment, comment.revisions[0].body)
            if comment.agent is not None:
                for _, _, line in original + self.answer_pairs(comment, comment.body):
                    self.void(comment, line)
                continue
            if any(self.answer_pairs(comment, r.body) != original for r in comment.revisions[1:]):
                self.violation("decision_edited", comment.issue, detail=comment.id)
            stated.append((comment, original))
        results: dict[str, dict[str, Any]] = {}
        for issue_id, issue in self.issues.items():
            spec = issue["decision"]
            if spec is None:
                continue
            answers = []
            needs_followup = False
            for comment, pairs in stated:
                for named, value, _ in pairs:
                    if named != issue_id or value.lower() == "hold":
                        continue
                    if value not in spec["options"]:
                        needs_followup = True
                    else:
                        answers.append(
                            {"option": value, "comment": comment.id, "at": comment.created_at, "until": comment.deleted_at}
                        )
            answer = self.answer_at({"answers": answers}, self.now)
            first_ping = spec["pings"][0] if spec["pings"] else None
            if first_ping is not None and any(t > first_ping for t in spec["options_changed_at"]):
                self.violation("options_changed_after_ping", issue_id)
            if issue["status"] == "Done" and answer is None:
                self.violation("decision_closed_without_answer", issue_id)
            deadline = spec["deadline"]
            deadline_passed = deadline is not None and self.now > deadline and answer is None
            repings = spec["pings"][1:]
            allowed_repings = [t for t in repings if deadline is not None and t > deadline]
            if len(repings) > 1 or len(allowed_repings) != len(repings):
                self.violation("repeat_ping", issue_id)
            results[issue_id] = {
                "answered": answer is not None,
                "answer": answer["option"] if answer else None,
                "answer_comment": answer["comment"] if answer else None,
                "answered_at": answer["at"] if answer else None,
                "answers": answers,
                "held_from": first_ping,
                "needs_followup": needs_followup and answer is None,
                "deadline_passed": deadline_passed,
                "reping_allowed": deadline_passed and not repings,
            }
        return results

    @staticmethod
    def answer_at(state: dict[str, Any], at: datetime) -> dict[str, Any] | None:
        """The earliest answer in effect at `at` (posted, and not yet deleted)."""
        live = [a for a in state["answers"] if a["at"] <= at and (a["until"] is None or at < a["until"])]
        return min(live, key=lambda a: (a["at"], a["comment"])) if live else None

    def held_at(self, issue_id: str, at: datetime) -> list[str]:
        """Reasons the issue is held at `at`.

        A decision holds its blocked work from its first ping until it is
        answered; a decision issue with no recorded ping, or a non-decision
        blocker, has no start time and holds throughout.
        """
        reasons = []
        for blocker in self.issues[issue_id]["blocked_by"]:
            state = self._decisions.get(blocker)
            if state is None:
                if self.issues[blocker]["status"] not in {"Done", "Canceled", "Duplicate"}:
                    reasons.append(f"blocked by {blocker}")
                continue
            started = state["held_from"] is None or state["held_from"] <= at
            cleared = self.answer_at(state, at) is not None
            if started and not cleared:
                reasons.append(f"awaiting decision {blocker}")
        for comment in self.comments:
            if comment.agent is not None or (
                comment.deleted and (comment.deleted_at is None or comment.deleted_at <= at)
            ):
                continue
            for revision in comment.revisions:
                for line in (part.strip() for part in revision.body.split("\n")):
                    match = HOLD.match(line)
                    if match and match.group(1) == issue_id and present_at(comment, line, at):
                        reason = f"HOLD by comment {comment.id}"
                        if reason not in reasons:
                            reasons.append(reason)
        return reasons

    def collect_void_lines(self) -> None:
        for comment in self.comments:
            if comment.agent is not None and not comment.deleted:
                for line in human_lines(comment):
                    if HOLD.match(line) or REVOKE_CLAIM.match(line) or ACK_TAKEOVER.match(line):
                        self.void(comment, line)

    # Standing approvals ----------------------------------------------------

    def parse_approval(self, comment: Comment, revision: Revision) -> StandingApproval | None:
        lines = [line.strip() for line in revision.body.split("\n")]
        header = SA_HEADER.match(lines[0]) if lines else None
        if not header:
            return None
        sa_id = header.group(1)
        fields = {m.group(1): m.group(2) for line in lines[1:] if (m := FIELD.match(line))}
        for key in ("actions", "scope", "expires"):
            if key not in fields:
                raise SnapshotError(f"standing approval {sa_id}: missing {key!r}")
        limits = {}
        for part in fields.get("limits", "").replace(",", " ").split():
            key, _, value = part.partition("=")
            if key not in {"per_day", "total"} or not value.isdigit():
                raise SnapshotError(f"standing approval {sa_id}: limits must be per_day=N and/or total=N")
            limits[key] = int(value)
        raw_expires = fields["expires"]
        date_only = len(raw_expires) == 10
        return StandingApproval(
            sa=sa_id,
            comment=comment,
            since=revision.at,
            actions={t.strip() for t in fields["actions"].split(",") if t.strip()},
            scope={t.strip() for t in fields["scope"].split(",") if t.strip()},
            who={t.strip() for t in fields["who"].split(",") if t.strip()} if "who" in fields else None,
            per_day=limits.get("per_day"),
            total=limits.get("total"),
            expires=parse_ts(raw_expires + "T23:59:59.999999Z" if date_only else raw_expires, sa_id),
        )

    def standing_approvals(self) -> tuple[dict[str, list[Comment]], dict[str, datetime]]:
        """Comments that define each standing approval, and revocation times.

        An approval is read from the comment revision in effect when it is
        used, so text edited in later does not authorise earlier actions.
        """
        homes: dict[str, list[Comment]] = {}
        spans: dict[str, list[tuple[datetime, str, datetime | None]]] = {}
        revoked: dict[str, datetime] = {}
        home = self.config["standing_approvals_issue"]
        for comment in self.comments:
            if comment.issue != home:
                continue
            if comment.agent is not None:
                if not comment.deleted:
                    for line in human_lines(comment):
                        if SA_HEADER.match(line) or REVOKE_SA.match(line):
                            self.void(comment, line)
                continue
            if comment.deleted and comment.deleted_at is None:
                continue
            last = len(comment.revisions) - 1
            # Each run of consecutive revisions defining the same approval is
            # one span; a revision that stops defining it ends the span, and a
            # later re-definition in the same comment starts a new one.
            current: tuple[str, datetime] | None = None
            first: dict[str, datetime] = {}
            for index, revision in enumerate(comment.revisions):
                if comment.deleted and revision.at >= comment.deleted_at:
                    break
                try:
                    approval = self.parse_approval(comment, revision)
                except SnapshotError:
                    if index == last:
                        raise
                    approval = None
                if current is not None and (approval is None or approval.sa != current[0]):
                    spans.setdefault(current[0], []).append((current[1], comment.id, revision.at))
                    current = None
                if approval is None:
                    continue
                if current is None:
                    current = (approval.sa, revision.at)
                    first.setdefault(approval.sa, revision.at)
                    known = homes.setdefault(approval.sa, [])
                    if comment not in known:
                        known.append(comment)
                # The lifetime runs from the first revision of this comment that
                # defined the approval, so editing it out and back in does not
                # restart the clock.
                limit = (first[approval.sa] + timedelta(days=self.config["sa_max_days"])).date()
                if approval.expires.date() > limit:
                    self.violation("sa_too_long", detail=f"{approval.sa} at {revision.at.isoformat()}")
            if current is not None:
                spans.setdefault(current[0], []).append((current[1], comment.id, comment.deleted_at))
        for sa_id, intervals in spans.items():
            # Definitions may follow each other (an old one deleted or edited
            # out, a new one posted later) but may not overlap; compare their
            # live intervals so the result does not depend on snapshot order.
            # A span that ends when it starts authorises nothing.
            ordered = sorted((span for span in intervals if span[2] is None or span[2] > span[0]),
                             key=lambda span: (span[0], span[1]))
            for (_, _, end), (next_start, _, _) in zip(ordered, ordered[1:]):
                if end is None or next_start < end:
                    raise SnapshotError(f"standing approval {sa_id}: defined in two comments at once")
        for _, line, seen in self.directive_lines(home):
            match = REVOKE_SA.match(line)
            if match:
                previous = revoked.get(match.group(1))
                revoked[match.group(1)] = seen if previous is None else min(previous, seen)
        return homes, revoked

    def approval_at(self, comment: Comment, at: datetime) -> StandingApproval | None:
        if comment.deleted and (comment.deleted_at is None or comment.deleted_at <= at):
            return None
        effective = [r for r in comment.revisions if r.at <= at]
        if not effective:
            return None
        try:
            return self.parse_approval(comment, effective[-1])
        except SnapshotError:
            # A malformed draft that was later corrected authorises nothing;
            # a malformed final revision already failed in standing_approvals.
            return None

    def check_sa_actions(self) -> None:
        homes, revoked = self.standing_approvals()
        used: dict[str, list[datetime]] = {}
        for action in sorted(self.sa_actions, key=lambda a: a["at"]):
            at = action["at"]
            label = f"{action['sa']} {action['action']} at {at.isoformat()}"
            sa = next((a for c in homes.get(action["sa"], [])
                       if (a := self.approval_at(c, at)) is not None and a.sa == action["sa"]), None)
            if sa is None or sa.sa != action["sa"]:
                self.violation("sa_unknown", detail=label)
                continue
            if action["sa"] in revoked and revoked[action["sa"]] <= at:
                self.violation("sa_revoked", detail=label)
                continue
            if at > sa.expires:
                self.violation("sa_expired", detail=label)
                continue
            if action["action"] not in sa.actions or action["repo"] not in sa.scope:
                self.violation("sa_out_of_scope", detail=label)
                continue
            if sa.who is not None and action.get("agent") not in sa.who:
                self.violation("sa_out_of_scope", detail=label)
                continue
            history = used.setdefault(sa.sa, [])
            same_day = [t for t in history if t.date() == at.date()]
            if (sa.per_day is not None and len(same_day) >= sa.per_day) or (sa.total is not None and len(history) >= sa.total):
                self.violation("sa_over_limit", detail=label)
                continue
            history.append(at)

    # Rules -----------------------------------------------------------------

    def check_claims(self) -> None:
        for claim in self.claims.values():
            issue = self.issues[claim.issue]
            if not issue["admitted"]:
                self.violation("claim_not_admitted", claim.issue, claim.uuid)
            elif not issue["next_in_order"]:
                self.violation("claim_out_of_order", claim.issue, claim.uuid)
            if self.held_at(claim.issue, claim.created_at):
                self.violation("claim_on_held_scope", claim.issue, claim.uuid)
            confirmed = claim.confirmed_at()
            edited = claim.confirm_edit_at()
            if edited is not None and edited - claim.created_at < self.readback:
                self.violation("confirmed_too_early", claim.issue, claim.uuid)
            deadline = claim.created_at + self.readback
            settled = self.winner(claim.issue, claim.repo, deadline) if deadline <= self.now else None
            if settled is not None and settled.uuid != claim.uuid:
                if confirmed is not None:
                    self.violation("loser_confirmed", claim.issue, claim.uuid, f"winner {settled.uuid}")
                if claim.withdrawn_at() is None:
                    self.violation("loser_not_withdrawn", claim.issue, claim.uuid, f"winner {settled.uuid}")
            self.check_implicit_takeover(claim)
            self.check_takeover_refused(claim)
            others = [
                other for other in self.claims.values()
                if other.uuid != claim.uuid and other.session == claim.session and other.repo == claim.repo
                and (other.created_at, other.uuid) < (claim.created_at, claim.uuid) and self.ended(other, claim.created_at) is None
            ]
            if others:
                self.violation("multiple_claims_per_session", claim.issue, claim.uuid, f"also holds {others[0].uuid}")
            released = claim.released_at()
            if released is not None:
                for run in self.runs:
                    if run["claim"] == claim.uuid and run["started_at"] <= released and (
                        run.get("ended_at") is None or run["ended_at"] > released
                    ):
                        self.violation("release_before_writers_stopped", claim.issue, claim.uuid, run["id"])

    # Refusals that wait on time or a human acknowledgement, or that come from
    # the original claimant objecting afterwards, are not the taker's fault;
    # every other refusal means the takeover was posted when the rules did
    # not allow it.
    PENDING_TAKEOVER = {
        "takeover wait not over",
        "coordinator acknowledgement missing",
        "original claimant renewed within the wait",
    }

    def check_takeover_refused(self, claim: Claim) -> None:
        status = self.takeover_status(claim)
        if status is None or status["allowed"]:
            return
        if status["reason"] == "original claimant renewed within the wait":
            # The renewal hides the posting-time checks; a takeover that was
            # already too early when posted is still refused.
            old = self.claims[status["of"]]
            if self.ended(old, claim.created_at) in {"withdrawn", "released", "revoked", "deleted"}:
                return
            status = self.posting_refusal(claim, old)
            if status is None:
                return
        elif status["reason"] in self.PENDING_TAKEOVER:
            return
        if self.ended(claim, self.now) in {"withdrawn", "released", "revoked", "deleted"}:
            return
        self.violation("takeover_refused", claim.issue, claim.uuid, status["reason"])

    def check_implicit_takeover(self, claim: Claim) -> None:
        """A fresh claim must not bypass the takeover rules (spec section 7).

        It may not land while another claim's takeover wait is running, or on
        an issue whose earlier claim expired without being released,
        withdrawn, revoked, deleted or taken over: that needs a `takeover of`
        line and the takeover conditions.
        """
        at = claim.created_at
        for other in self.claims.values():
            if other.uuid == claim.uuid or other.issue != claim.issue or other.repo != claim.repo:
                continue
            # The claim an explicit takeover names is judged by takeover_status;
            # every other earlier claim still needs its own takeover.
            if other.uuid == claim.takeover_of:
                continue
            if other.created_at >= at:
                continue
            if other.takeover_of is not None and at < other.created_at + self.takeover_wait:
                if self.ended(other, at) is None:
                    self.violation("claim_during_takeover_wait", claim.issue, claim.uuid, f"takeover {other.uuid}")
                    continue
            if self.bypassed_expired(claim, other):
                self.violation("claim_without_takeover", claim.issue, claim.uuid, f"expired {other.uuid}")

    def bypassed_expired(self, claim: Claim, other: Claim) -> bool:
        """True if `claim` landed over `other`, which had expired without a valid takeover."""
        at = claim.created_at
        if self.ended(other, at) != "expired":
            return False
        return not any(
            t.takeover_of == other.uuid and t.created_at <= at
            and (status := self.takeover_status(t, at)) is not None and status["allowed"]
            for t in self.claims.values()
        )

    def check_writes(self) -> None:
        for write in self.writes:
            at = write["at"]
            what = f"{write['kind']} at {at.isoformat()}"
            claim = self.claims.get(write["claim"]) if write["claim"] else None
            if claim is None:
                self.violation("write_without_claim", write["issue"], write["claim"], what)
                continue
            if claim.issue != write["issue"]:
                self.violation("write_outside_claim", write["issue"], claim.uuid, what)
                continue
            confirmed = claim.confirmed_at()
            if confirmed is None or confirmed > at:
                self.violation("write_before_confirm", claim.issue, claim.uuid, what)
                continue
            ended = self.ended(claim, at)
            if ended is not None:
                code = f"write_{AFTER_END.get(ended, 'after_release')}"
                self.violation(code, claim.issue, claim.uuid, what)
                continue
            if claim.takeover_of is not None and at < claim.created_at + self.takeover_wait:
                self.violation("takeover_wrote_too_early", claim.issue, claim.uuid, what)
                continue
            winner = self.winner(claim.issue, claim.repo, at)
            if winner is None or winner.uuid != claim.uuid:
                self.violation("write_by_non_winner", claim.issue, claim.uuid, what)
            elif self.held_at(claim.issue, at):
                self.violation("write_while_held", claim.issue, claim.uuid, what)

    def check_commits(self) -> None:
        for commit in self.commits:
            uuid = commit.get("claim")
            if commit["author_kind"] != "agent":
                continue
            sha = commit["sha"]
            if not uuid:
                self.violation("agent_commit_without_claim", detail=sha)
                continue
            claim = self.claims.get(uuid)
            if claim is None:
                self.violation("commit_names_unknown_claim", claim=uuid, detail=sha)
                continue
            if commit.get("repo", self.repo) != claim.repo or commit["branch"] != claim.branch:
                self.violation("commit_outside_claim_branch", claim.issue, uuid, sha)
            ended = self.ended(claim, commit["at"])
            if ended == "deleted":
                self.violation("commit_names_deleted_claim", claim.issue, uuid, sha)
            elif ended == "not yet posted":
                self.violation("commit_before_claim", claim.issue, uuid, sha)
            elif ended in AFTER_END:
                self.violation(f"commit_{AFTER_END[ended]}", claim.issue, uuid, sha)

    def check_done_and_admissions(self, held: dict[str, list[str]]) -> None:
        for issue_id, issue in self.issues.items():
            if issue["status"] == "Done" and held[issue_id]:
                self.violation("done_while_held", issue_id, detail="; ".join(held[issue_id]))
        program = self.program
        if program and self.admissions:
            paused = program["occupied_reviews"] >= program["review_slots"] and program["completed_waiting"] > 0
            if paused:
                for admission in self.admissions:
                    self.violation("admission_while_paused", admission["issue"])

    def check_decision_records(self, decisions: dict[str, dict[str, Any]]) -> None:
        for issue_id, issue in self.issues.items():
            for blocker in issue["blocked_by"]:
                state = decisions.get(blocker)
                if not state:
                    continue
                for claim in self.claims.values():
                    confirmed = claim.confirmed_at()
                    if claim.issue != issue_id or confirmed is None:
                        continue
                    answer = self.answer_at(state, confirmed)
                    if answer is None:
                        continue
                    expected = (answer["comment"], answer["option"])
                    recorded = any(
                        c.issue == blocker and c.agent is not None
                        and not (c.deleted and (c.deleted_at is None or c.deleted_at <= confirmed))
                        and self.recorded_since(c, expected, answer["at"], confirmed)
                        for c in self.comments
                    )
                    if not recorded:
                        self.violation("decision_not_recorded", blocker, claim.uuid)

    @staticmethod
    def recorded_since(comment: Comment, expected: tuple[str, str], answered: datetime, at: datetime) -> bool:
        """Whether the revision in effect at `at` records `expected`, and the
        record has been present without a break since the answer or later, so
        a record written before the answer does not count."""
        start = None
        for revision in comment.revisions:
            if revision.at > at:
                break
            has = any((m := DECISION_RECORD.match(line.strip())) and m.groups() == expected
                      for line in revision.body.split("\n"))
            if not has:
                start = None
            elif start is None:
                start = revision.at
        return start is not None and start >= answered

    def must_reread(self, claim: Claim) -> bool:
        confirmed = claim.confirmed_at()
        if confirmed is None:
            return False
        seen = confirmed  # a snapshot records no reads, so only confirmation counts as one
        return any(
            c.get("repo", self.repo) == claim.repo and c["branch"] == claim.branch
            and c.get("claim") != claim.uuid and c["at"] > seen and c["at"] <= self.now
            for c in self.commits
        )

    def run(self) -> dict[str, Any]:
        decisions = self._decisions = self.decisions()
        held = {issue_id: self.held_at(issue_id, self.now) for issue_id in self.issues}
        self.collect_void_lines()
        for comment in self.comments:
            if comment.prefix_changed:
                self.violation("comment_prefix_changed", comment.issue, detail=comment.id)
        self.check_claims()
        self.check_writes()
        self.check_commits()
        self.check_done_and_admissions(held)
        self.check_decision_records(decisions)
        self.check_sa_actions()
        issues: dict[str, Any] = {}
        repos = sorted({c.repo for c in self.claims.values()} | {self.repo})
        for issue_id in sorted(self.issues):
            per_repo = {}
            for repo in repos:
                live = self.live_claims(issue_id, repo, self.now)
                winner = self.winner(issue_id, repo, self.now)
                if not live and not any(c.issue == issue_id and c.repo == repo for c in self.claims.values()):
                    continue
                per_repo[repo] = {
                    "live_claims": sorted(c.uuid for c in live),
                    "winner": winner.uuid if winner else None,
                    "must_reread": self.must_reread(winner) if winner else False,
                }
            takeovers = {
                c.uuid: self.takeover_status(c)
                for c in self.claims.values() if c.issue == issue_id and c.takeover_of
            }
            issues[issue_id] = {
                "claims": per_repo,
                "held": bool(held[issue_id]),
                "hold_reasons": held[issue_id],
                "claimable": self.issues[issue_id]["admitted"] and self.issues[issue_id]["next_in_order"]
                and not held[issue_id] and not any(v["live_claims"] for v in per_repo.values()),
                "takeovers": takeovers,
            }
        return {
            "issues": issues,
            "decisions": {
                k: {key: value for key, value in v.items() if key not in {"answered_at", "held_from", "answers"}}
                for k, v in decisions.items()
            },
            "violations": self.violations,
            "void_lines": self.void_lines,
        }


def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a snapshot. Raises SnapshotError if it is malformed.

    Any other exception raised while reading the snapshot is also reported as
    SnapshotError, so an unexpected shape fails closed instead of producing a
    partial result.
    """
    try:
        return Evaluation(snapshot).run()
    except SnapshotError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, ArithmeticError) as exc:
        raise SnapshotError(f"unreadable snapshot: {type(exc).__name__}: {exc}") from exc


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: agent_protocol_sim.py snapshot.json", file=sys.stderr)
        return 2
    try:
        snapshot = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        result = evaluate(snapshot)
    except (OSError, json.JSONDecodeError, SnapshotError) as exc:
        print(f"AGENT_PROTOCOL_SIM_ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 1 if result["violations"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
