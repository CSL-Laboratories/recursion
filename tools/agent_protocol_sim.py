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
        {id, issue, created_at, body, edits: [{at, body}], deleted}
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
prefix. Standing approvals use machine fields: `actions:` and `scope:` are
comma-separated tokens, `limits:` holds `per_day=N` and/or `total=N`, and
`expires:` is a date.
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
BATCH = re.compile(r"^CSL-[0-9]+:\s*\S+(\s*;\s*CSL-[0-9]+:\s*\S+[^;]*)*")
BATCH_PAIR = re.compile(r"(CSL-[0-9]+):\s*([A-Za-z0-9_-]+)")
ACK_TAKEOVER = re.compile(r"^ACK TAKEOVER (\S+)\s*$")
SA_HEADER = re.compile(r"^Standing approval (SA-[0-9]+)\s*$")
REVOKE_SA = re.compile(r"^REVOKE (SA-[0-9]+)\s*$")
TAKEOVER = re.compile(r"^takeover of (\S+)\s*$")
TIMESTAMP_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
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

    @property
    def body(self) -> str:
        return self.revisions[-1].body

    @property
    def agent(self) -> tuple[str, str] | None:
        return agent_header(self.body)

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


class Evaluation:
    def __init__(self, snapshot: dict[str, Any]):
        if not isinstance(snapshot, dict):
            raise SnapshotError("snapshot must be an object")
        self.now = parse_ts(require(snapshot, "now", "snapshot"), "now")
        self.repo = snapshot.get("repo", "default")
        config = snapshot.get("config", {})
        unknown = set(config) - set(DEFAULT_CONFIG)
        if unknown:
            raise SnapshotError(f"config: unknown keys {sorted(unknown)}")
        self.config = {**DEFAULT_CONFIG, **config}
        self.lease = timedelta(minutes=self.config["lease_minutes"])
        self.grace = timedelta(minutes=self.config["grace_minutes"])
        self.readback = timedelta(seconds=self.config["readback_seconds"])
        self.takeover_wait = timedelta(minutes=self.config["takeover_wait_minutes"])
        self.violations: list[dict[str, Any]] = []
        self.void_lines: list[dict[str, str]] = []
        self.issues = self._load_issues(require(snapshot, "issues", "snapshot"))
        self.comments = self._load_comments(require(snapshot, "comments", "snapshot"))
        self.claims = self._load_claims()
        self.commits = self._load_list(snapshot, "commits", ("sha", "branch", "at", "author_kind"))
        self.writes = self._load_list(snapshot, "writes", ("kind", "issue", "claim", "at"))
        self.runs = self._load_list(snapshot, "runs", ("id", "claim", "started_at"))
        self.admissions = self._load_list(snapshot, "admissions", ("issue", "at"))
        self.sa_actions = self._load_list(snapshot, "sa_actions", ("sa", "action", "repo", "at"))
        self.program = snapshot.get("program")
        for commit in self.commits:
            if commit["author_kind"] not in AUTHOR_KINDS:
                raise SnapshotError(f"commit {commit['sha']}: unknown author_kind")
        for write in self.writes:
            if write["kind"] not in WRITE_KINDS:
                raise SnapshotError(f"write: unknown kind {write['kind']!r}")
            self._known_issue(write["issue"], "write")

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
                    "pings": [parse_ts(t, issue_id) for t in decision.get("pings", [])],
                    "options_changed_at": [parse_ts(t, issue_id) for t in decision.get("options_changed_at", [])],
                    "deadline": parse_ts(decision["deadline"], issue_id) if decision.get("deadline") else None,
                }
            issues[issue_id] = {
                "status": status,
                "admitted": bool(item.get("admitted", False)),
                "next_in_order": bool(item.get("next_in_order", False)),
                "blocked_by": list(item.get("blocked_by", [])),
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
            body = require(item, "body", comment_id)
            if not isinstance(body, str):
                raise SnapshotError(f"comment {comment_id}: body must be a string")
            revisions = [Revision(created, body)]
            for edit in item.get("edits", []):
                at = parse_ts(require(edit, "at", comment_id), comment_id)
                if at < revisions[-1].at:
                    raise SnapshotError(f"comment {comment_id}: edits out of order")
                revisions.append(Revision(at, require(edit, "body", comment_id)))
            comments.append(Comment(comment_id, issue, created, revisions, bool(item.get("deleted", False))))
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
                if field in entry and entry[field] is not None:
                    entry[field] = parse_ts(entry[field], key)
            items.append(entry)
        return items

    def _load_claims(self) -> dict[str, Claim]:
        claims: dict[str, Claim] = {}
        for comment in self.comments:
            header = comment.agent
            first = comment.revisions[0].body.split("\n", 1)[0]
            if not header or not CLAIM_HEADER.search(first):
                continue
            fields = claim_fields(comment.revisions[0].body)
            for key in ("claim", "scope", "branch"):
                if not fields.get(key):
                    raise SnapshotError(f"claim comment {comment.id}: missing {key!r}")
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

    def revoked_at(self, claim: Claim) -> datetime | None:
        times = [
            c.created_at
            for c in self.human_comments()
            if c.issue == claim.issue
            and any((m := REVOKE_CLAIM.match(line)) and m.group(1) == claim.uuid for line in human_lines(c))
        ]
        return min(times) if times else None

    def ended(self, claim: Claim, at: datetime) -> str | None:
        """Why the claim is not live at `at`, or None if it is live."""
        if claim.created_at > at:
            return "not yet posted"
        if claim.comment.deleted:
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

    def takeover_status(self, claim: Claim) -> dict[str, Any] | None:
        if claim.takeover_of is None:
            return None
        old = self.claims.get(claim.takeover_of)
        posted = claim.created_at
        if old is None or old.issue != claim.issue or old.repo != claim.repo:
            return {"of": claim.takeover_of, "allowed": False, "reason": "unknown claim"}
        objection = [r.at for r in old.comment.revisions if posted <= r.at <= posted + self.takeover_wait]
        if objection:
            return {"of": old.uuid, "allowed": False, "reason": "original claimant renewed within the wait"}
        ended = self.ended(old, posted)
        if ended in {"withdrawn", "released", "revoked", "deleted"}:
            return {"of": old.uuid, "allowed": True, "reason": f"original {ended}"}
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
        if self.config["coordinator_ack_required"]:
            acked = any(
                (m := ACK_TAKEOVER.match(line)) and m.group(1) == old.uuid
                for c in self.human_comments() if c.issue == claim.issue
                for line in human_lines(c)
            )
            if not acked:
                return {"of": old.uuid, "allowed": False, "reason": "coordinator acknowledgement missing"}
        return {"of": old.uuid, "allowed": True, "reason": "conditions met"}

    def live_claims(self, issue: str, repo: str, at: datetime) -> list[Claim]:
        candidates = [
            c for c in self.claims.values()
            if c.issue == issue and c.repo == repo and self.ended(c, at) is None
        ]
        superseded = set()
        refused = set()
        for claim in candidates:
            status = self.takeover_status(claim)
            if status is None:
                continue
            if status["allowed"]:
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

    def decisions(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for issue_id, issue in self.issues.items():
            spec = issue["decision"]
            if spec is None:
                continue
            answer = None
            needs_followup = False
            for comment in sorted(self.comments, key=lambda c: c.created_at):
                if comment.deleted:
                    continue
                for line in human_lines(comment):
                    value = None
                    decision = DECISION.match(line)
                    if decision and comment.issue == issue_id:
                        value = decision.group(1)
                    elif BATCH.match(line):
                        for named, picked in BATCH_PAIR.findall(line):
                            if named == issue_id:
                                value = picked
                    if value is None:
                        continue
                    if comment.agent is not None:
                        self.void_lines.append({"comment": comment.id, "line": line, "reason": "agent-prefixed"})
                        continue
                    if value.lower() == "hold":
                        continue
                    if value not in spec["options"]:
                        needs_followup = True
                        continue
                    if answer is None:
                        answer = {"option": value, "comment": comment.id, "at": comment.created_at}
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
                "needs_followup": needs_followup and answer is None,
                "deadline_passed": deadline_passed,
                "reping_allowed": deadline_passed and not repings,
            }
        return results

    def holds(self, decisions: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
        held: dict[str, list[str]] = {issue_id: [] for issue_id in self.issues}
        for issue_id, issue in self.issues.items():
            for blocker in issue["blocked_by"]:
                state = decisions.get(blocker)
                if state is None:
                    if self.issues[blocker]["status"] not in {"Done", "Canceled", "Duplicate"}:
                        held[issue_id].append(f"blocked by {blocker}")
                elif not state["answered"]:
                    held[issue_id].append(f"awaiting decision {blocker}")
        for comment in self.human_comments():
            for line in human_lines(comment):
                match = HOLD.match(line)
                if match and match.group(1) in held:
                    held[match.group(1)].append(f"HOLD by comment {comment.id}")
        for comment in self.comments:
            if comment.agent is not None and not comment.deleted:
                for line in human_lines(comment):
                    if HOLD.match(line) or REVOKE_CLAIM.match(line) or ACK_TAKEOVER.match(line):
                        self.void_lines.append({"comment": comment.id, "line": line, "reason": "agent-prefixed"})
        return held

    # Standing approvals ----------------------------------------------------

    def standing_approvals(self) -> tuple[dict[str, StandingApproval], dict[str, datetime]]:
        approvals: dict[str, StandingApproval] = {}
        revoked: dict[str, datetime] = {}
        home = self.config["standing_approvals_issue"]
        for comment in self.comments:
            if comment.deleted or comment.issue != home:
                continue
            lines = human_lines(comment)
            header = SA_HEADER.match(lines[0]) if lines else None
            if comment.agent is not None:
                if any(SA_HEADER.match(line) or REVOKE_SA.match(line) for line in lines):
                    self.void_lines.append({"comment": comment.id, "line": lines[0], "reason": "agent-prefixed"})
                continue
            for line in lines:
                match = REVOKE_SA.match(line)
                if match:
                    revoked.setdefault(match.group(1), comment.created_at)
            if not header:
                continue
            fields = {m.group(1): m.group(2) for line in lines[1:] if (m := FIELD.match(line))}
            for key in ("actions", "scope", "expires"):
                if key not in fields:
                    raise SnapshotError(f"standing approval {header.group(1)}: missing {key!r}")
            limits = dict(part.split("=", 1) for part in fields.get("limits", "").replace(",", " ").split() if "=" in part)
            expires = parse_ts(fields["expires"] + "T23:59:59Z", header.group(1)) if len(fields["expires"]) == 10 else parse_ts(fields["expires"], header.group(1))
            approval = StandingApproval(
                sa=header.group(1),
                comment=comment,
                actions={t.strip() for t in fields["actions"].split(",") if t.strip()},
                scope={t.strip() for t in fields["scope"].split(",") if t.strip()},
                who={t.strip() for t in fields["who"].split(",") if t.strip()} if "who" in fields else None,
                per_day=int(limits["per_day"]) if "per_day" in limits else None,
                total=int(limits["total"]) if "total" in limits else None,
                expires=expires,
            )
            if approval.sa in approvals:
                raise SnapshotError(f"standing approval {approval.sa}: duplicate")
            if expires > comment.created_at + timedelta(days=self.config["sa_max_days"]):
                self.violation("sa_too_long", detail=approval.sa)
            approvals[approval.sa] = approval
        return approvals, revoked

    def check_sa_actions(self) -> None:
        approvals, revoked = self.standing_approvals()
        used: dict[str, list[datetime]] = {}
        for action in sorted(self.sa_actions, key=lambda a: a["at"]):
            sa = approvals.get(action["sa"])
            at = action["at"]
            label = f"{action['sa']} {action['action']} at {at.isoformat()}"
            if sa is None or sa.comment.created_at > at:
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

    def check_claims(self, held: dict[str, list[str]]) -> None:
        for claim in self.claims.values():
            issue = self.issues[claim.issue]
            if not issue["admitted"]:
                self.violation("claim_not_admitted", claim.issue, claim.uuid)
            elif not issue["next_in_order"]:
                self.violation("claim_out_of_order", claim.issue, claim.uuid)
            if held[claim.issue]:
                self.violation("claim_on_held_scope", claim.issue, claim.uuid)
            confirmed = claim.confirmed_at()
            if confirmed is not None and confirmed - claim.created_at < self.readback:
                self.violation("confirmed_too_early", claim.issue, claim.uuid)
            others = [
                other for other in self.claims.values()
                if other.uuid != claim.uuid and other.session == claim.session and other.repo == claim.repo
                and other.issue != claim.issue and self.ended(other, claim.created_at) is None
                and other.created_at <= claim.created_at
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

    def check_writes(self) -> None:
        for write in self.writes:
            at = write["at"]
            claim = self.claims.get(write["claim"]) if write["claim"] else None
            if claim is None:
                self.violation("write_without_claim", write["issue"], write["claim"], write["kind"])
                continue
            if claim.issue != write["issue"]:
                self.violation("write_outside_claim", write["issue"], claim.uuid, write["kind"])
                continue
            confirmed = claim.confirmed_at()
            if confirmed is None or confirmed > at:
                self.violation("write_before_confirm", claim.issue, claim.uuid, write["kind"])
                continue
            ended = self.ended(claim, at)
            if ended is not None:
                code = {"expired": "write_after_expiry", "revoked": "write_after_revoke"}.get(ended, "write_after_release")
                self.violation(code, claim.issue, claim.uuid, write["kind"])
                continue
            if claim.takeover_of is not None and at < claim.created_at + self.takeover_wait:
                self.violation("takeover_wrote_too_early", claim.issue, claim.uuid, write["kind"])
                continue
            winner = self.winner(claim.issue, claim.repo, at)
            if winner is None or winner.uuid != claim.uuid:
                self.violation("write_by_non_winner", claim.issue, claim.uuid, write["kind"])

    def check_commits(self) -> None:
        for commit in self.commits:
            uuid = commit.get("claim")
            if commit["author_kind"] != "agent":
                continue
            if not uuid:
                self.violation("agent_commit_without_claim", detail=commit["sha"])
                continue
            claim = self.claims.get(uuid)
            if claim is None:
                self.violation("commit_names_unknown_claim", claim=uuid, detail=commit["sha"])
            elif claim.comment.deleted:
                self.violation("commit_names_deleted_claim", claim.issue, uuid, commit["sha"])

    def check_done_and_admissions(self, held: dict[str, list[str]]) -> None:
        for issue_id, issue in self.issues.items():
            if issue["status"] == "Done" and held[issue_id]:
                self.violation("done_while_held", issue_id, detail="; ".join(held[issue_id]))
        program = self.program
        if program and self.admissions:
            slots = require(program, "review_slots", "program")
            paused = program.get("occupied_reviews", 0) >= slots and program.get("completed_waiting", 0) > 0
            if paused:
                for admission in self.admissions:
                    self.violation("admission_while_paused", admission["issue"])

    def check_decision_records(self, decisions: dict[str, dict[str, Any]]) -> None:
        for issue_id, issue in self.issues.items():
            for blocker in issue["blocked_by"]:
                state = decisions.get(blocker)
                if not state or not state["answered"]:
                    continue
                for claim in self.claims.values():
                    confirmed = claim.confirmed_at()
                    if claim.issue != issue_id or confirmed is None or confirmed < state["answered_at"]:
                        continue
                    recorded = any(
                        c.issue == blocker and c.agent is not None and not c.deleted
                        and c.created_at <= confirmed and state["answer_comment"] in c.body
                        for c in self.comments
                    )
                    if not recorded:
                        self.violation("decision_not_recorded", blocker, claim.uuid)

    def must_reread(self, claim: Claim) -> bool:
        confirmed = claim.confirmed_at()
        if confirmed is None:
            return False
        own = [c["at"] for c in self.commits if c.get("claim") == claim.uuid]
        seen = max([confirmed, *own])
        return any(
            c.get("repo", self.repo) == claim.repo and c["branch"] == claim.branch
            and c.get("claim") != claim.uuid and c["at"] > seen and c["at"] <= self.now
            for c in self.commits
        )

    def run(self) -> dict[str, Any]:
        decisions = self.decisions()
        held = self.holds(decisions)
        self.check_claims(held)
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
                "claimable": bool(self.issues[issue_id]["admitted"]) and not held[issue_id]
                and not any(v["live_claims"] for v in per_repo.values()),
                "takeovers": takeovers,
            }
        return {
            "issues": issues,
            "decisions": {
                k: {key: value for key, value in v.items() if key != "answered_at"} for k, v in decisions.items()
            },
            "violations": self.violations,
            "void_lines": self.void_lines,
        }


def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a snapshot. Raises SnapshotError if it is malformed."""
    return Evaluation(snapshot).run()


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
