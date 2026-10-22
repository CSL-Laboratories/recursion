#!/usr/bin/env python3
"""Scenario tests S1-S31 for the CSL-357 agent work protocol simulator (CSL-360)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "tools/agent_protocol_sim.py"

spec = importlib.util.spec_from_file_location("agent_protocol_sim", SIM)
assert spec and spec.loader
sim = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sim
spec.loader.exec_module(sim)

BASE = datetime(2026, 10, 20, 10, 0, 0, tzinfo=timezone.utc)
U1 = "00000000-0000-4000-8000-000000000001"
U2 = "00000000-0000-4000-8000-000000000002"
U3 = "00000000-0000-4000-8000-000000000003"


def ts(minutes: float = 0, seconds: float = 0) -> str:
    return (BASE + timedelta(minutes=minutes, seconds=seconds)).isoformat().replace("+00:00", "Z")


def claim_body(uuid, session="claude/s1", branch="csl-1-work", confirmed=None, extra=()):
    lines = [
        f"Agent ({session}, for Lucas): claim v1",
        f"claim: {uuid}",
        "scope: whole issue",
        f"branch: {branch} @ abc123, base def456",
        f"confirmed-at: {confirmed or '(pending read-back)'}",
        *extra,
    ]
    return "\n".join(lines)


def claim(cid, issue, uuid, at=0, confirm_after=70, session="claude/s1", branch="csl-1-work",
          extra=(), more_edits=(), deleted=False):
    """A claim comment posted at `at` minutes, confirmed `confirm_after` seconds later."""
    edits = []
    if confirm_after is not None:
        confirmed = ts(at, confirm_after)
        edits.append({"at": confirmed, "body": claim_body(uuid, session, branch, confirmed, extra)})
    edits.extend(more_edits)
    return {
        "id": cid,
        "issue": issue,
        "created_at": ts(at),
        "body": claim_body(uuid, session, branch, None, extra),
        "edits": edits,
        "deleted": deleted,
    }


def edit(at_minutes, uuid, *extra, session="claude/s1", branch="csl-1-work", confirmed=None):
    confirmed = confirmed or ts(0, 70)
    return {"at": ts(at_minutes), "body": claim_body(uuid, session, branch, confirmed, extra)}


def comment(cid, issue, at, body, deleted=False):
    return {"id": cid, "issue": issue, "created_at": ts(at), "body": body, "edits": [], "deleted": deleted}


RECORD_FIELDS = {
    "reason": "done",
    "done": "the change",
    "evidence": "\n- tests pass",
    "open": "none",
    "next": "none",
    "files": "tools/agent_protocol_sim.py",
}


def release_record(cid, issue_id, uuid, at, session="claude/s1", omit=(), **fields):
    """An agent's `release v1` record for `uuid`, posted at `at` minutes (CSL-445)."""
    values = {"claim": uuid, **RECORD_FIELDS, **fields}
    lines = [f"Agent ({session}, for Lucas): release v1"]
    lines += [f"{key}: {value}" for key, value in values.items() if key not in omit]
    return comment(cid, issue_id, at, "\n".join(lines))


def issue(iid, status="Todo", admitted=True, next_in_order=True, blocked_by=(), decision=None, parent=None):
    return {
        "id": iid,
        "status": status,
        "admitted": admitted,
        "next_in_order": next_in_order,
        "blocked_by": list(blocked_by),
        "decision": decision,
        "parent": parent,
    }


def decision(options=("A", "B"), pings=(1,), options_changed_at=(), deadline=None):
    return {
        "options": list(options),
        "pings": [ts(p) for p in pings],
        "options_changed_at": [ts(p) for p in options_changed_at],
        "deadline": ts(deadline) if deadline is not None else None,
    }


def snapshot(now, issues, comments, **rest):
    # BASE is before the real agent-prefix cutoff, so tests turn that check off unless they set it (CSL-417).
    rest["config"] = {"agent_prefix_since": None, **rest.get("config", {})}
    return {"now": ts(now), "repo": "recursion", "issues": issues, "comments": comments, **rest}


def codes(result):
    return sorted(v["code"] for v in result["violations"])


class ClaimingTests(unittest.TestCase):
    def test_s1_single_claim_on_admitted_issue(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)]))
        state = result["issues"]["CSL-1"]["claims"]["recursion"]
        self.assertEqual(state["live_claims"], [U1])
        self.assertEqual(state["winner"], U1)
        self.assertEqual(codes(result), [])
        self.assertFalse(result["issues"]["CSL-1"]["claimable"])

    def test_s2_earliest_claim_wins_and_loser_withdraws(self):
        loser = claim("c1", "CSL-1", U1, at=0.3, confirm_after=None,
                      more_edits=[{"at": ts(1.5), "body": claim_body(U1, "cursor/s2", extra=(f"withdrawn: superseded by {U2}",))}],
                      session="cursor/s2")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c2", "CSL-1", U2), loser]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [U2])
        self.assertEqual(codes(result), [])

    def test_s2_loser_that_writes_is_flagged(self):
        loser = claim("c1", "CSL-1", U1, at=0.3, session="cursor/s2")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c2", "CSL-1", U2), loser],
                                       writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(3)}]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn", "write_by_non_winner"])

    def test_s2_loser_that_never_withdraws_is_flagged(self):
        loser = claim("c1", "CSL-1", U1, at=0.3, confirm_after=None, session="cursor/s2")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c2", "CSL-1", U2), loser]))
        self.assertEqual(codes(result), ["loser_not_withdrawn"])

    def test_s3_exact_tie_goes_to_lower_uuid(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [
            claim("c2", "CSL-1", U2, session="cursor/s2"), claim("c1", "CSL-1", U1),
        ]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn"])

    def test_s4_claim_on_unadmitted_or_out_of_order_issue(self):
        result = sim.evaluate(snapshot(10, [
            issue("CSL-1", status="Triage", admitted=False), issue("CSL-2", next_in_order=False),
        ], [
            claim("c1", "CSL-1", U1), claim("c2", "CSL-2", U2, session="claude/s2", branch="csl-2-work"),
        ]))
        self.assertEqual(codes(result), ["claim_not_admitted", "claim_out_of_order"])

    def test_s5_write_before_confirm(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)],
                                       writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(0, 30)}]))
        self.assertEqual(codes(result), ["write_before_confirm"])

    def test_declared_future_confirmation_is_not_reached_early(self):
        early = claim("c1", "CSL-1", U1, confirm_after=None,
                      more_edits=[{"at": ts(2), "body": claim_body(U1, confirmed=ts(9))}])
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(3)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [early], writes=writes))
        self.assertEqual(codes(result), ["write_before_confirm"])

    def test_backdated_confirmation_counts_from_the_edit(self):
        backdated = claim("c1", "CSL-1", U1, confirm_after=None,
                          more_edits=[{"at": ts(5), "body": claim_body(U1, confirmed=ts(0, 1))}])
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(3)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [backdated], writes=writes))
        self.assertEqual(codes(result), ["write_before_confirm"])

    def test_early_edit_declaring_a_later_time_is_too_early(self):
        early = claim("c1", "CSL-1", U1, confirm_after=None,
                      more_edits=[{"at": ts(0, 20), "body": claim_body(U1, confirmed=ts(5))}])
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [early]))
        self.assertEqual(codes(result), ["confirmed_too_early"])

    def test_confirm_before_read_back_window(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1, confirm_after=20)]))
        self.assertEqual(codes(result), ["confirmed_too_early"])

    def test_s22_one_claim_per_session_per_repository(self):
        other_repo = claim("c2", "CSL-1", U2, at=1, branch="csl-1-other", extra=("repo: other-repo",))
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), other_repo]))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["other-repo"]["winner"], U2)
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)

    def test_s22_second_claim_by_same_session_on_same_issue_and_repository(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), claim("c2", "CSL-1", U2, at=1)]))
        self.assertIn("multiple_claims_per_session", codes(result))

    def test_loser_is_not_judged_before_the_read_back_window_ends(self):
        fresh = claim("c1", "CSL-1", U1, at=9.5, confirm_after=None, session="cursor/s2")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c2", "CSL-1", U2), fresh]))
        self.assertEqual(codes(result), [])

    def test_s22_claims_on_two_issues_in_same_repository_are_allowed(self):
        # Several claims per session are allowed (CSL-357 comment 0cdfd465).
        result = sim.evaluate(snapshot(10, [issue("CSL-1"), issue("CSL-4")], [
            claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, branch="csl-4-work"),
        ]))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], U2)

    def test_s22_own_expired_unreleased_claim_still_counts_on_another_issue(self):
        # Codex P2 on #276: release before re-claiming (CSL-379); the same
        # session's lapsed claim on CSL-1 still blocks a claim on CSL-4 that
        # shares its branch.
        result = sim.evaluate(snapshot(210, [issue("CSL-1"), issue("CSL-4")], [
            claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=200),
        ]))
        self.assertEqual(codes(result), ["multiple_claims_per_session"])

    def test_s22_own_expired_claim_released_first_is_clean(self):
        result = sim.evaluate(snapshot(210, [issue("CSL-1"), issue("CSL-4")], [
            claim("c1", "CSL-1", U1, more_edits=[edit(190, U1, "released")]), release_record("r1", "CSL-1", U1, 190),
            claim("c2", "CSL-4", U2, at=200),
        ]))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], U2)

    def test_s22_parent_scope_and_direct_child_claim_overlap(self):
        # The parent claim's `scope: sub-issue CSL-2` already covers CSL-2.
        family = [issue("CSL-1"), issue("CSL-2", status="In Progress", parent="CSL-1")]
        result = sim.evaluate(snapshot(10, family, [
            scoped("c1", "CSL-1", U1, "CSL-2"), claim("c2", "CSL-2", U2, at=1, branch="csl-2-work"),
        ]))
        self.assertEqual(codes(result), ["multiple_claims_per_session"])
        # Codex 4343345426: the session's later child claim is not published
        # as CSL-2's holder; the parent claim, whose scope covers CSL-2, holds
        # both (Codex 4343468463).
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(result["issues"]["CSL-2"]["claims"]["recursion"]["winner"], U1)

    def test_s22_claims_on_two_issues_sharing_a_branch_are_flagged(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1"), issue("CSL-4")], [
            claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, branch="csl-1-work"),
        ]))
        self.assertEqual(codes(result), ["multiple_claims_per_session"])
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], None)

    def test_human_revoke_ends_claim_and_agent_revoke_is_void(self):
        human = comment("h1", "CSL-1", 5, f"REVOKE CLAIM {U1}")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), human],
                                       writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(6)}]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])
        self.assertEqual(codes(result), ["write_after_revoke"])
        forged = comment("a1", "CSL-1", 5, f"Agent (grok/s9, for Lucas): note\nREVOKE CLAIM {U1}")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), forged]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual([v["comment"] for v in result["void_lines"]], ["a1"])


class LeaseTests(unittest.TestCase):
    def takeover(self, at=152, extra=()):
        return claim("c2", "CSL-1", U2, at=at, session="cursor/s2", extra=(f"takeover of {U1}", *extra))

    def test_s6_takeover_after_lease_and_grace(self):
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"], True)
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(codes(result), [])

    def test_s6_coordinator_ack_required_by_config(self):
        comments = [claim("c1", "CSL-1", U1), self.takeover()]
        config = {"coordinator_ack_required": True}
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config=config))
        self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], None)
        comments.append(comment("h1", "CSL-1", 152, f"ACK TAKEOVER {U1}"))
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config=config))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_takeover_before_grace_is_refused(self):
        result = sim.evaluate(snapshot(140, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover(at=125)]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "lease plus grace not passed")
        refused = [v for v in result["violations"] if v["code"] == "takeover_refused"]
        self.assertEqual([(v["claim"], v["detail"]) for v in refused], [(U2, "lease plus grace not passed")])

    def test_premature_takeover_is_reported_whatever_happens_later(self):
        renewing = [edit(100, U1, "heartbeat-at: 100"), edit(130, U1, "heartbeat-at: 130")]
        cases = {
            "during the wait": (130, renewing),
            "original renewed": (300, renewing),
            "original released later": (300, renewing + [edit(135, U1, "released")]),
        }
        for name, (now, edits) in cases.items():
            with self.subTest(case=name):
                comments = [claim("c1", "CSL-1", U1, more_edits=edits), self.takeover(at=125)]
                if name == "original released later":
                    comments.append(release_record("r1", "CSL-1", U1, 135))
                result = sim.evaluate(snapshot(now, [issue("CSL-1")], comments))
                refused = [v["detail"] for v in result["violations"] if v["code"] == "takeover_refused"]
                self.assertEqual(refused, ["lease plus grace not passed"])

    def test_takeover_of_an_unknown_claim_names_it(self):
        ghost = "99999999-9999-4999-8999-999999999999"
        stray = claim("c2", "CSL-1", U2, at=152, session="cursor/s2", extra=(f"takeover of {ghost}",))
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [stray]))
        refused = [v["detail"] for v in result["violations"] if v["code"] == "takeover_refused"]
        self.assertEqual(refused, [f"unknown claim {ghost}"])

    def test_objecting_then_releasing_does_not_let_a_premature_takeover_win(self):
        objected = [edit(130, U1, "heartbeat-at: 130"), edit(135, U1, "released")]
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U2, "at": ts(150)},
                  {"kind": "pr", "issue": "CSL-1", "claim": U2, "at": ts(190)}]
        released_taker = {"at": ts(195), "body": claim_body(U2, "cursor/s2", confirmed=ts(125, 70),
                                                            extra=(f"takeover of {U1}", "released"))}
        released = self.takeover(at=125)
        released["edits"].append(released_taker)
        for name, taker in (("taker live", self.takeover(at=125)), ("taker released later", released)):
            with self.subTest(case=name):
                comments = [claim("c1", "CSL-1", U1, more_edits=objected), taker, release_record("r1", "CSL-1", U1, 135)]
                if taker is released:
                    comments.append(release_record("r2", "CSL-1", U2, 195, session="cursor/s2"))
                result = sim.evaluate(snapshot(200, [issue("CSL-1")], comments, writes=writes))
                state = result["issues"]["CSL-1"]
                self.assertFalse(state["takeovers"][U2]["allowed"])
                self.assertIsNone(state["claims"]["recursion"]["winner"])
                self.assertEqual([c for c in codes(result) if c == "write_by_non_winner"], ["write_by_non_winner"] * 2)

    def test_objection_then_release_still_needs_the_acknowledgement(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(155, U1, "heartbeat-at: 155"), edit(160, U1, "released")])
        record = release_record("r1", "CSL-1", U1, 160)
        result = sim.evaluate(snapshot(200, [issue("CSL-1")], [original, record, self.takeover()],
                                       config={"coordinator_ack_required": True}))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "coordinator acknowledgement missing")
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        acked = [original, record, self.takeover(), comment("h1", "CSL-1", 156, f"ACK TAKEOVER {U1}")]
        result = sim.evaluate(snapshot(200, [issue("CSL-1")], acked, config={"coordinator_ack_required": True}))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_fresh_claim_over_an_expired_claim_does_not_win(self):
        fresh = claim("c3", "CSL-1", U3, at=160, session="grok/s3")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(165)}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), fresh], writes=writes))
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        self.assertEqual(codes(result), ["claim_without_takeover", "write_by_non_winner"])

    def test_human_revoke_keeps_the_premature_takeover_report(self):
        comments = [claim("c1", "CSL-1", U1), self.takeover(at=125), comment("h1", "CSL-1", 130, f"REVOKE CLAIM {U2}")]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments))
        refused = [v["detail"] for v in result["violations"] if v["code"] == "takeover_refused"]
        self.assertEqual(refused, ["lease plus grace not passed"])

    def test_violations_do_not_depend_on_comment_order(self):
        comments = [claim("c1", "CSL-1", U1), self.takeover(at=125), claim("c3", "CSL-1", U3, at=160, session="grok/s3")]
        forward = sim.evaluate(snapshot(170, [issue("CSL-1")], comments))["violations"]
        backward = sim.evaluate(snapshot(170, [issue("CSL-1")], comments[::-1]))["violations"]
        self.assertEqual(forward, backward)

    def test_sorting_tolerates_mixed_id_types(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": 5, "at": ts(5)},
                  {"kind": "push", "issue": "CSL-1", "claim": "zz", "at": ts(5)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [], writes=writes))
        self.assertEqual(codes(result), ["write_without_claim", "write_without_claim"])

    def test_withdrawn_refused_takeover_is_not_reported(self):
        early = self.takeover(at=125)
        early["edits"].append({"at": ts(127), "body": early["edits"][0]["body"] + "\nwithdrawn: lease not over"})
        result = sim.evaluate(snapshot(140, [issue("CSL-1")], [claim("c1", "CSL-1", U1), early]))
        self.assertNotIn("takeover_refused", codes(result))

    def test_takeover_waiting_on_time_or_an_ack_is_not_refused(self):
        waiting = sim.evaluate(snapshot(155, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()]))
        self.assertNotIn("takeover_refused", codes(waiting))
        no_ack = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()],
                                       config={"coordinator_ack_required": True}))
        self.assertNotIn("takeover_refused", codes(no_ack))

    def test_competing_takeover_during_the_wait(self):
        u3 = "33333333-3333-4333-8333-333333333333"
        rival = claim("c3", "CSL-1", u3, at=160, session="grok/s3", extra=(f"takeover of {U1}",))
        result = sim.evaluate(snapshot(165, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover(), rival]))
        during = [v for v in result["violations"] if v["code"] == "claim_during_takeover_wait"]
        self.assertEqual([(v["claim"], v["detail"]) for v in during], [(u3, f"takeover {U2}")])

    def test_s7_takeover_refused_when_commit_is_newer_than_heartbeat(self):
        commits = [{"sha": "c0ffee", "branch": "csl-1-work", "at": ts(60), "author_kind": "agent", "claim": U1}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], commits=commits))
        status = result["issues"]["CSL-1"]["takeovers"][U2]
        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "commit newer than the last heartbeat")

    def test_commit_before_the_last_heartbeat_does_not_refuse_takeover(self):
        commits = [{"sha": "c0ffee", "branch": "csl-1-work", "at": ts(0, 30), "author_kind": "human", "claim": None}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], commits=commits))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_finished_run_does_not_refuse_takeover(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(20)}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], runs=runs))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_deleted_or_edited_out_acknowledgement_does_not_count(self):
        config = {"coordinator_ack_required": True}
        deleted = comment("h1", "CSL-1", 152, f"ACK TAKEOVER {U1}", deleted=True)
        deleted["deleted_at"] = ts(160)
        edited = comment("h1", "CSL-1", 152, f"ACK TAKEOVER {U1}")
        edited["edits"] = [{"at": ts(160), "body": "never mind"}]
        for ack in (deleted, edited):
            with self.subTest(ack="deleted" if ack["deleted"] else "edited"):
                comments = [claim("c1", "CSL-1", U1), self.takeover(), ack]
                result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config=config))
                self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])
                self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], None)

    def test_explicit_takeover_still_needs_one_for_an_older_expired_claim(self):
        u3 = "33333333-3333-4333-8333-333333333333"
        withdrawn = claim("c2", "CSL-1", U2, at=1, session="cursor/s2", confirm_after=None,
                          more_edits=[{"at": ts(1.5), "body": claim_body(U2, "cursor/s2", extra=(f"withdrawn: superseded by {U1}",))}])
        third = claim("c3", "CSL-1", u3, at=152, session="grok/s3", extra=(f"takeover of {U2}",))
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), withdrawn, third]))
        self.assertEqual(codes(result), ["claim_without_takeover"])
        self.assertEqual(result["violations"][0]["claim"], u3)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": u3, "at": ts(200)}]
        result = sim.evaluate(snapshot(210, [issue("CSL-1")], [claim("c1", "CSL-1", U1), withdrawn, third], writes=writes))
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        self.assertEqual(codes(result), ["claim_without_takeover", "write_by_non_winner"])

    def test_acknowledgement_re_added_after_the_takeover_counts(self):
        ack = comment("h1", "CSL-1", 140, f"ACK TAKEOVER {U1}")
        ack["edits"] = [{"at": ts(145), "body": "hold on"}, {"at": ts(155), "body": f"ACK TAKEOVER {U1}"}]
        comments = [claim("c1", "CSL-1", U1), self.takeover(), ack]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config={"coordinator_ack_required": True}))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_acknowledgement_before_the_takeover_does_not_count(self):
        comments = [claim("c1", "CSL-1", U1), comment("h1", "CSL-1", 140, f"ACK TAKEOVER {U1}"), self.takeover()]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config={"coordinator_ack_required": True}))
        self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_release_after_an_objection_clears_it(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(155, U1, "heartbeat-at: back"), edit(160, U1, "released")])
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U2, "at": ts(170)}]
        record = release_record("r1", "CSL-1", U1, 160)
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, record, self.takeover()], writes=writes))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(codes(result), [])

    def test_objection_without_release_still_refuses(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(155, U1, "heartbeat-at: back")])
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, self.takeover()]))
        self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_release_inside_the_wait_is_not_an_objection(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(160, U1, "released")])
        record = release_record("r1", "CSL-1", U1, 160)
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, record, self.takeover()]))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_takeover_refused_while_claimant_run_is_in_progress(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(100), "ended_at": None}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], runs=runs))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "claimant run in progress")

    def test_s8_write_after_expiry(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(125)}]
        result = sim.evaluate(snapshot(130, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], writes=writes))
        self.assertEqual(codes(result), ["write_after_expiry"])

    def test_lease_and_grace_boundaries_are_measured_from_the_last_update(self):
        # Codex 4343803111: the lease runs 2 hours from the last counted update
        # (here the confirmation edit at 1m10s); a write at exactly that edge is
        # allowed, one second later it is not. A renewal up to exactly 2h30
        # restores the lease; one second later it does not.
        last, second = 70 / 60, 1 / 60

        def push(at):
            return {"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(at)}

        cases = {
            "write at the lease edge": ([], [push(last + 120)], []),
            "write just after the lease": ([], [push(last + 120 + second)], ["write_after_expiry"]),
            "renewal at the grace edge": ([edit(last + 150, U1, "heartbeat-at: x")], [push(last + 155)], []),
            "renewal just after grace": ([edit(last + 150 + second, U1, "heartbeat-at: x")], [push(last + 155)],
                                         ["renewed_after_expiry", "write_after_expiry"]),
        }
        for name, (edits, writes, expected) in cases.items():
            with self.subTest(case=name):
                comment_ = claim("c1", "CSL-1", U1, more_edits=edits)
                result = sim.evaluate(snapshot(160, [issue("CSL-1")], [comment_], writes=writes))
                self.assertEqual(codes(result), expected)

    def test_heartbeat_extends_the_lease(self):
        claim_comment = claim("c1", "CSL-1", U1, more_edits=[edit(100, U1, "heartbeat-at: x")])
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(180)}]
        result = sim.evaluate(snapshot(190, [issue("CSL-1")], [claim_comment], writes=writes))
        self.assertEqual(codes(result), [])

    def test_s23_original_claimant_objects_within_wait(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(160, U1, "heartbeat-at: back")])
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U2, "at": ts(170)}]
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, self.takeover()], writes=writes))
        status = result["issues"]["CSL-1"]["takeovers"][U2]
        self.assertFalse(status["allowed"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(codes(result), ["write_by_non_winner"])

    def test_no_winner_until_the_takeover_wait_ends(self):
        result = sim.evaluate(snapshot(160, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "takeover wait not over")
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])

    def test_takeover_of_a_later_claim_is_refused(self):
        later = claim("c1", "CSL-1", U1, at=160)
        result = sim.evaluate(snapshot(200, [issue("CSL-1")], [later, self.takeover()]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "original posted after the takeover")

    def test_acknowledgement_edited_in_later_does_not_validate_earlier_writes(self):
        ack = comment("h1", "CSL-1", 153, "noted")
        ack["edits"] = [{"at": ts(180), "body": f"noted\nACK TAKEOVER {U1}"}]
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U2, "at": ts(170)}]
        result = sim.evaluate(snapshot(190, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover(), ack],
                                       writes=writes, config={"coordinator_ack_required": True}))
        self.assertEqual(codes(result), ["write_by_non_winner"])
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_fresh_claim_on_an_expired_claim_needs_a_takeover(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(100), "ended_at": None}]
        fresh = claim("c2", "CSL-1", U2, at=160, session="cursor/s2")
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), fresh], runs=runs))
        self.assertEqual(codes(result), ["claim_without_takeover"])
        inside_grace = claim("c2", "CSL-1", U2, at=125, session="cursor/s2")
        result = sim.evaluate(snapshot(130, [issue("CSL-1")], [claim("c1", "CSL-1", U1), inside_grace]))
        self.assertEqual(codes(result), ["claim_without_takeover"])

    def test_fresh_claim_after_a_released_claim_is_fine(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(30, U1, "released")])
        fresh = claim("c2", "CSL-1", U2, at=160, session="cursor/s2")
        record = release_record("r1", "CSL-1", U1, 30)
        self.assertEqual(codes(sim.evaluate(snapshot(170, [issue("CSL-1")], [released, record, fresh]))), [])

    def test_claim_during_another_takeover_wait(self):
        third = claim("c3", "CSL-1", U3, at=155, session="grok/s3")
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover(), third]))
        self.assertIn("claim_during_takeover_wait", codes(result))

    def test_renewal_after_the_wait_is_not_an_objection(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(172, U1, "heartbeat-at: late")])
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, self.takeover()]))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_takeover_of_a_claim_in_another_repository_is_refused(self):
        other_repo = claim("c1", "CSL-1", U1, extra=("repo: other-repo",))
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [other_repo, self.takeover()]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "claim on another issue or repository")
        refused = [v["detail"] for v in result["violations"] if v["code"] == "takeover_refused"]
        self.assertEqual(refused, [f"claim on another issue or repository {U1}"])

    def test_run_started_after_the_takeover_does_not_refuse_it(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(153), "ended_at": None}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], runs=runs))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_takeover_that_writes_during_the_wait(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U2, "at": ts(160)}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], writes=writes))
        self.assertEqual(codes(result), ["takeover_wrote_too_early"])


class CommitTests(unittest.TestCase):
    def test_s9_human_commit_on_claimed_branch_requires_reread(self):
        commits = [{"sha": "beef", "branch": "csl-1-work", "at": ts(5), "author_kind": "human", "claim": None}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], commits=commits))
        self.assertTrue(result["issues"]["CSL-1"]["claims"]["recursion"]["must_reread"])
        self.assertEqual(codes(result), [])

    def test_own_commit_does_not_require_reread(self):
        commits = [{"sha": "beef", "branch": "csl-1-work", "at": ts(5), "author_kind": "agent", "claim": U1}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], commits=commits))
        self.assertFalse(result["issues"]["CSL-1"]["claims"]["recursion"]["must_reread"])

    def test_s10_bot_commit_without_trailer_is_not_a_violation(self):
        commits = [{"sha": "b0t", "branch": "csl-1-work", "at": ts(5), "author_kind": "bot", "claim": None}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], commits=commits))
        self.assertEqual(codes(result), [])

    def test_agent_commit_without_trailer_is_a_violation(self):
        commits = [{"sha": "a9e", "branch": "csl-1-work", "at": ts(5), "author_kind": "agent", "claim": None}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], commits=commits))
        self.assertEqual(codes(result), ["agent_commit_without_claim"])

    def test_commit_on_another_branch_or_after_expiry(self):
        commits = [
            {"sha": "m1", "branch": "master", "at": ts(5), "author_kind": "agent", "claim": U1},
            {"sha": "x1", "branch": "csl-1-work", "at": ts(125), "author_kind": "agent", "claim": U1},
        ]
        result = sim.evaluate(snapshot(130, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], commits=commits))
        self.assertEqual(codes(result), ["commit_after_expiry", "commit_outside_claim_branch"])

    def test_repeated_writes_are_reported_separately(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(0, s)} for s in (10, 20)]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], writes=writes))
        self.assertEqual(codes(result), ["write_before_confirm", "write_before_confirm"])

    def test_write_after_deletion_has_its_own_code(self):
        deleted = claim("c1", "CSL-1", U1, deleted=True)
        deleted["deleted_at"] = ts(30)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(40)}]
        result = sim.evaluate(snapshot(50, [issue("CSL-1")], [deleted], writes=writes))
        self.assertEqual(codes(result), ["write_after_deletion"])

    def test_deletion_time_bounds_the_claim(self):
        deleted = claim("c1", "CSL-1", U1, deleted=True)
        deleted["deleted_at"] = ts(30)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(10)}]
        result = sim.evaluate(snapshot(40, [issue("CSL-1")], [deleted], writes=writes))
        self.assertEqual(codes(result), [])

    def test_commit_before_its_named_claim(self):
        commits = [{"sha": "e1", "branch": "csl-1-work", "at": ts(2), "author_kind": "agent", "claim": U1}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1, at=5)], commits=commits))
        self.assertEqual(codes(result), ["commit_before_claim"])

    def test_s20_commit_names_deleted_claim(self):
        commits = [{"sha": "a9e", "branch": "csl-1-work", "at": ts(5), "author_kind": "agent", "claim": U1}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1, deleted=True)], commits=commits))
        self.assertEqual(codes(result), ["commit_names_deleted_claim"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])

    def test_s18_release_while_run_in_progress(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(30, U1, "released")])
        record = release_record("r1", "CSL-1", U1, 30)
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(40)}]
        result = sim.evaluate(snapshot(50, [issue("CSL-1")], [released, record], runs=runs))
        self.assertEqual(codes(result), ["release_before_writers_stopped"])
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(20)}]
        result = sim.evaluate(snapshot(50, [issue("CSL-1")], [released, record], runs=runs))
        self.assertEqual(codes(result), [])
        self.assertTrue(result["issues"]["CSL-1"]["claimable"])


class DecisionTests(unittest.TestCase):
    def test_s11_only_affected_lineage_is_held(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision()), issue("CSL-3")]
        result = sim.evaluate(snapshot(10, issues, [claim("c1", "CSL-1", U1, at=5)]))
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        self.assertFalse(result["issues"]["CSL-1"]["claimable"])
        self.assertTrue(result["issues"]["CSL-3"]["claimable"])
        self.assertEqual(codes(result), ["claim_on_held_scope"])

    def test_s11_split_after_claiming_is_not_a_violation(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision(pings=(30,)))]
        work = claim("c1", "CSL-1", U1, more_edits=[edit(35, U1, "released")])
        result = sim.evaluate(snapshot(60, issues, [work, release_record("r1", "CSL-1", U1, 35)]))
        self.assertEqual(codes(result), [])
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        late_hold = comment("h1", "CSL-1", 20, "HOLD CSL-1")
        result = sim.evaluate(snapshot(60, [issue("CSL-1")], [claim("c1", "CSL-1", U1), late_hold]))
        self.assertEqual(codes(result), [])
        self.assertTrue(result["issues"]["CSL-1"]["held"])

    def test_s12_batched_answer_clears_named_decisions(self):
        issues = [
            issue("CSL-1", blocked_by=["CSL-11", "CSL-12"]),
            issue("CSL-11", decision=decision()), issue("CSL-12", decision=decision()), issue("CSL-13", decision=decision()),
        ]
        answer = comment("h1", "CSL-11", 5, "CSL-11: B; CSL-12: A if the budget holds; CSL-13: hold")
        records = [
            comment("r1", "CSL-11", 9, "Agent (claude/s1, for Lucas): recorded\ndecision-record: h1 B"),
            comment("r2", "CSL-12", 9, "Agent (claude/s1, for Lucas): recorded\ndecision-record: h1 A"),
        ]
        work = claim("c1", "CSL-1", U1, at=10)
        result = sim.evaluate(snapshot(20, issues, [answer, *records, work]))
        self.assertEqual(result["decisions"]["CSL-11"]["answer"], "B")
        self.assertEqual(result["decisions"]["CSL-12"]["answer"], "A")
        self.assertFalse(result["decisions"]["CSL-13"]["answered"])
        self.assertFalse(result["issues"]["CSL-1"]["held"])
        self.assertEqual(codes(result), [])
        result = sim.evaluate(snapshot(20, issues, [answer, records[0], work]))
        self.assertEqual(codes(result), ["decision_not_recorded"])
        late = comment("r2", "CSL-12", 15, "Agent (claude/s1, for Lucas): recorded\ndecision-record: h1 A")
        result = sim.evaluate(snapshot(20, issues, [answer, records[0], late, work]))
        self.assertEqual(codes(result), ["decision_not_recorded"])

    def test_decision_record_written_before_the_answer_does_not_count(self):
        issues = [issue("CSL-1", blocked_by=["CSL-11"]), issue("CSL-11", decision=decision())]
        answer = comment("h1", "CSL-11", 5, "DECISION: B")
        work = claim("c1", "CSL-1", U1, at=10)
        early = comment("r1", "CSL-11", 3, "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B")
        result = sim.evaluate(snapshot(20, issues, [answer, early, work]))
        self.assertEqual(codes(result), ["decision_not_recorded"])
        early["edits"] = [{"at": ts(7), "body": "Agent (claude/s1, for Lucas): noted again\ndecision-record: h1 B"}]
        result = sim.evaluate(snapshot(20, issues, [answer, early, work]))
        self.assertEqual(codes(result), ["decision_not_recorded"])
        rewritten = comment("r1", "CSL-11", 3, "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B")
        rewritten["edits"] = [{"at": ts(4), "body": "Agent (claude/s1, for Lucas): noted"},
                              {"at": ts(7), "body": "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B"}]
        result = sim.evaluate(snapshot(20, issues, [answer, rewritten, work]))
        self.assertEqual(codes(result), [])

    def test_decision_record_edited_in_after_confirm_does_not_count(self):
        issues = [issue("CSL-1", blocked_by=["CSL-11"]), issue("CSL-11", decision=decision())]
        answer = comment("h1", "CSL-11", 5, "DECISION: B")
        record = comment("r1", "CSL-11", 9, "Agent (claude/s1, for Lucas): noted")
        record["edits"] = [{"at": ts(15), "body": "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B"}]
        result = sim.evaluate(snapshot(20, issues, [answer, record, claim("c1", "CSL-1", U1, at=10)]))
        self.assertEqual(codes(result), ["decision_not_recorded"])

    def test_condition_in_a_batched_answer_is_not_an_answer(self):
        issues = [issue("CSL-11", decision=decision()), issue("CSL-12", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, [comment("h1", "CSL-11", 5, "CSL-11: B if CSL-12: A")]))
        self.assertEqual(result["decisions"]["CSL-11"]["answer"], "B")
        self.assertFalse(result["decisions"]["CSL-12"]["answered"])

    def test_batched_answer_on_a_work_issue_is_ignored(self):
        issues = [issue("CSL-1"), issue("CSL-2", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, [comment("h1", "CSL-1", 5, "CSL-1: B; CSL-2: A")]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])

    def test_decision_record_must_name_the_answer_and_option_exactly(self):
        issues = [issue("CSL-1", blocked_by=["CSL-11"]), issue("CSL-11", decision=decision())]
        answer = comment("h1", "CSL-11", 5, "DECISION: B")
        work = claim("c1", "CSL-1", U1, at=10)
        for body in ("not recording h1", "decision-record: h10 B", "decision-record: h1 A"):
            with self.subTest(body=body):
                record = comment("r1", "CSL-11", 9, f"Agent (claude/s1, for Lucas): note\n{body}")
                result = sim.evaluate(snapshot(20, issues, [answer, record, work]))
                self.assertEqual(codes(result), ["decision_not_recorded"])

    def test_batched_answer_on_an_unrelated_issue_is_ignored(self):
        issues = [issue("CSL-2", decision=decision()), issue("CSL-5")]
        result = sim.evaluate(snapshot(10, issues, [comment("h1", "CSL-5", 5, "CSL-2: B")]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])

    def test_agent_batched_line_is_voided_once(self):
        issues = [issue("CSL-2", decision=decision()), issue("CSL-3", decision=decision())]
        forged = comment("a1", "CSL-2", 5, "Agent (claude/s1, for Lucas): relaying\nCSL-2: B; CSL-3: A")
        result = sim.evaluate(snapshot(10, issues, [forged]))
        self.assertEqual(len(result["void_lines"]), 1)
        self.assertFalse(result["decisions"]["CSL-3"]["answered"])

    def test_edited_decision_is_flagged_and_original_counts(self):
        answer = comment("h1", "CSL-2", 5, "DECISION: A")
        answer["edits"] = [{"at": ts(8), "body": "DECISION: B"}]
        result = sim.evaluate(snapshot(10, [issue("CSL-2", decision=decision())], [answer]))
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "A")
        self.assertEqual(codes(result), ["decision_edited"])

    def test_answer_outside_the_options_needs_follow_up(self):
        issues = [issue("CSL-2", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, [comment("h1", "CSL-2", 5, "DECISION: C")]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertTrue(result["decisions"]["CSL-2"]["needs_followup"])

    def test_s13_decision_in_agent_comment_is_void(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]
        forged = comment("a1", "CSL-2", 5, "Agent (claude/s1, for Lucas): relaying\nDECISION: B")
        result = sim.evaluate(snapshot(10, issues, [forged]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        self.assertEqual(result["void_lines"][0]["comment"], "a1")

    def test_write_while_held_is_flagged(self):
        hold = comment("h1", "CSL-1", 5, "HOLD CSL-1")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(6)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), hold], writes=writes))
        self.assertEqual(codes(result), ["write_while_held"])

    def test_deleted_hold_holds_until_its_deletion(self):
        hold = comment("h1", "CSL-1", 5, "HOLD CSL-1", deleted=True)
        hold["deleted_at"] = ts(8)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(6)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), hold], writes=writes))
        self.assertEqual(codes(result), ["write_while_held"])
        self.assertFalse(result["issues"]["CSL-1"]["held"])

    def test_hold_removed_by_edit_no_longer_holds(self):
        hold = comment("h1", "CSL-1", 5, "HOLD CSL-1")
        hold["edits"] = [{"at": ts(8), "body": "never mind"}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [hold]))
        self.assertFalse(result["issues"]["CSL-1"]["held"])

    def test_revocation_survives_a_later_deletion(self):
        revoke = comment("h1", "CSL-1", 5, f"REVOKE CLAIM {U1}", deleted=True)
        revoke["deleted_at"] = ts(20)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(10)}]
        result = sim.evaluate(snapshot(30, [issue("CSL-1")], [claim("c1", "CSL-1", U1), revoke], writes=writes))
        self.assertEqual(codes(result), ["write_after_revoke"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])

    def test_revoke_edited_in_later_is_not_backdated(self):
        note = comment("h1", "CSL-1", 1, "looks good")
        note["edits"] = [{"at": ts(20), "body": f"looks good\nREVOKE CLAIM {U1}"}]
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(10)}]
        result = sim.evaluate(snapshot(30, [issue("CSL-1")], [claim("c1", "CSL-1", U1), note], writes=writes))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])

    def test_deleted_decision_keeps_effect_until_deletion(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]
        answer = comment("h1", "CSL-2", 5, "DECISION: B", deleted=True)
        answer["deleted_at"] = ts(30)
        record = comment("r1", "CSL-2", 9, "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(20)}]
        result = sim.evaluate(snapshot(40, issues, [answer, record, claim("c1", "CSL-1", U1, at=10)], writes=writes))
        self.assertEqual(codes(result), [])
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])

    def test_decision_record_deleted_after_confirm_still_counts(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]
        record = comment("r1", "CSL-2", 9, "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 B", deleted=True)
        record["deleted_at"] = ts(30)
        comments = [comment("h1", "CSL-2", 5, "DECISION: B"), record, claim("c1", "CSL-1", U1, at=10)]
        self.assertEqual(codes(sim.evaluate(snapshot(40, issues, comments))), [])

    def test_removing_the_agent_prefix_does_not_make_lines_human(self):
        forged = comment("a1", "CSL-1", 5, "Agent (grok/s9, for Lucas): note")
        forged["edits"] = [{"at": ts(6), "body": f"note\nREVOKE CLAIM {U1}\nHOLD CSL-1"}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), forged]))
        self.assertEqual(codes(result), ["comment_prefix_changed"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertFalse(result["issues"]["CSL-1"]["held"])

    def test_human_hold_widens_the_hold(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1"), issue("CSL-5")], [comment("h1", "CSL-5", 1, "HOLD CSL-1")]))
        self.assertTrue(result["issues"]["CSL-1"]["held"])

    def test_s14_options_changed_after_ping(self):
        issues = [issue("CSL-2", decision=decision(pings=(1,), options_changed_at=(3,)))]
        result = sim.evaluate(snapshot(10, issues, []))
        self.assertEqual(codes(result), ["options_changed_after_ping"])
        issues = [issue("CSL-2", decision=decision(pings=(5,), options_changed_at=(3,)))]
        self.assertEqual(codes(sim.evaluate(snapshot(10, issues, []))), [])

    def test_s17_done_while_held(self):
        issues = [issue("CSL-1", status="Done", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, []))
        self.assertEqual(codes(result), ["done_while_held"])

    def test_decision_closed_without_answer(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-2", status="Done", decision=decision())], []))
        self.assertEqual(codes(result), ["decision_closed_without_answer"])

    def test_s21_deadline_passes_without_answer(self):
        issues = [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision(pings=(1,), deadline=60))]
        result = sim.evaluate(snapshot(90, issues, []))
        self.assertTrue(result["decisions"]["CSL-2"]["deadline_passed"])
        self.assertTrue(result["decisions"]["CSL-2"]["reping_allowed"])
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        issues[1]["decision"] = decision(pings=(1, 70), deadline=60)
        result = sim.evaluate(snapshot(90, issues, []))
        self.assertFalse(result["decisions"]["CSL-2"]["reping_allowed"])
        self.assertEqual(codes(result), [])
        issues[1]["decision"] = decision(pings=(1, 30), deadline=60)
        self.assertEqual(codes(sim.evaluate(snapshot(90, issues, []))), ["repeat_ping"])


class CapacityTests(unittest.TestCase):
    FULL = {"review_slots": 2, "occupied_reviews": 2, "completed_waiting": 1}

    def test_s15_new_admission_while_review_slots_are_full(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [], program=self.FULL,
                                       admissions=[{"issue": "CSL-1", "at": ts(5)}]))
        self.assertEqual(codes(result), ["admission_while_paused"])

    def test_s16_claim_on_admitted_work_continues(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], program=self.FULL))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)


class StandingApprovalTests(unittest.TestCase):
    CONFIG = {"standing_approvals_issue": "CSL-90"}

    def approval(self, cid="h1", at=0, body_prefix="", expires="2026-11-30", limits="per_day=1"):
        body = (f"{body_prefix}Standing approval SA-1\nactions: mark_ready\nwho: claude\n"
                f"scope: recursion\nlimits: {limits}\nexpires: {expires}")
        return comment(cid, "CSL-90", at, body)

    def run_actions(self, actions, approval=None):
        comments = [approval or self.approval()]
        return sim.evaluate(snapshot(600, [issue("CSL-90")], comments, config=self.CONFIG, sa_actions=actions))

    def action(self, at, action="mark_ready", repo="recursion", agent="claude", sa="SA-1"):
        return {"sa": sa, "action": action, "repo": repo, "agent": agent, "at": ts(at)}

    def test_s19_action_within_limits(self):
        self.assertEqual(codes(self.run_actions([self.action(10)])), [])

    def test_s19_over_limit_out_of_scope_and_unknown(self):
        result = self.run_actions([
            self.action(10), self.action(20), self.action(30, action="merge"),
            self.action(40, repo="other-repo"), self.action(50, agent="grok"), self.action(60, sa="SA-9"),
        ])
        self.assertEqual(codes(result), ["sa_out_of_scope"] * 3 + ["sa_over_limit", "sa_unknown"])

    def test_s19_expired_and_revoked(self):
        result = self.run_actions([self.action(10)], self.approval(expires="2026-10-19"))
        self.assertIn("sa_expired", codes(result))
        comments = [self.approval(), comment("h2", "CSL-90", 5, "REVOKE SA-1")]
        result = sim.evaluate(snapshot(600, [issue("CSL-90")], comments, config=self.CONFIG,
                                       sa_actions=[self.action(10)]))
        self.assertEqual(codes(result), ["sa_revoked"])

    def test_standing_approval_longer_than_ninety_days(self):
        result = self.run_actions([], self.approval(expires="2027-03-01"))
        self.assertEqual(codes(result), ["sa_too_long"])
        self.assertEqual(codes(self.run_actions([], self.approval(expires="2027-01-18"))), [])

    def test_lifetime_counts_from_the_defining_revision(self):
        draft = comment("h1", "CSL-90", -200 * 24 * 60, "draft")
        draft["edits"] = [{"at": ts(0), "body": self.approval()["body"]}]
        self.assertEqual(codes(self.run_actions([self.action(10)], draft)), [])
        extended = self.approval()
        extended["edits"] = [{"at": ts(60), "body": self.approval(expires="2027-03-01")["body"]}]
        self.assertEqual(codes(self.run_actions([], extended)), ["sa_too_long"])

    def test_lifetime_survives_editing_the_approval_out_and_back(self):
        day = 24 * 60
        posted = self.approval(expires="2027-01-18")
        extended = self.approval(expires="2027-04-01")["body"]
        interruptions = ("withdrawn", extended.replace("SA-1", "SA-2", 1), extended.replace("scope: recursion\n", ""))
        for interruption in interruptions:
            posted["edits"] = [{"at": ts(80 * day), "body": interruption}, {"at": ts(80 * day), "body": extended}]
            with self.subTest(interruption=interruption.splitlines()[0]):
                result = sim.evaluate(snapshot(81 * day, [issue("CSL-90")], [posted], config=self.CONFIG))
                self.assertIn("sa_too_long", codes(result))

    def test_lookup_skips_a_comment_now_defining_another_approval(self):
        moved = self.approval("h1", at=0)
        moved["edits"] = [{"at": ts(20), "body": moved["body"].replace("SA-1", "SA-2", 1)}]
        comments = [moved, self.approval("h2", at=30)]
        for order in (comments, comments[::-1]):
            with self.subTest(order=[c["id"] for c in order]):
                result = sim.evaluate(snapshot(600, [issue("CSL-90")], order, config=self.CONFIG,
                                               sa_actions=[self.action(40)]))
                self.assertEqual(codes(result), [])

    def test_action_without_an_agent_is_malformed(self):
        open_approval = comment("h1", "CSL-90", 0, self.approval()["body"].replace("who: claude\n", ""))
        for agent in (None, "", "  ", 7):
            action = self.action(10)
            if agent is None:
                del action["agent"]
            else:
                action["agent"] = agent
            with self.subTest(agent=agent), self.assertRaises(sim.SnapshotError):
                self.run_actions([action], open_approval)

    def test_date_only_expiry_covers_the_whole_day(self):
        last_day = self.approval(expires="2026-10-20")
        late = dict(self.action(0), at="2026-10-20T23:59:59.500000Z")
        result = sim.evaluate(snapshot(0, [issue("CSL-90")], [last_day], config=self.CONFIG, sa_actions=[late])
                              | {"now": "2026-10-21T00:00:01Z"})
        self.assertEqual(codes(result), [])
        after = dict(late, at="2026-10-21T00:00:00Z")
        result = sim.evaluate(snapshot(0, [issue("CSL-90")], [last_day], config=self.CONFIG, sa_actions=[after])
                              | {"now": "2026-10-21T00:00:01Z"})
        self.assertEqual(codes(result), ["sa_expired"])

    def test_total_limit(self):
        result = self.run_actions([self.action(10), self.action(500)], self.approval(limits="total=1"))
        self.assertEqual(codes(result), ["sa_over_limit"])

    def test_approval_edited_in_later_does_not_authorise_earlier_actions(self):
        draft = comment("h1", "CSL-90", 0, "draft, not yet")
        draft["edits"] = [{"at": ts(50), "body": self.approval()["body"]}]
        result = sim.evaluate(snapshot(600, [issue("CSL-90")], [draft], config=self.CONFIG,
                                       sa_actions=[self.action(10), self.action(60)]))
        self.assertEqual(codes(result), ["sa_unknown"])
        self.assertIn(ts(10).replace("Z", "+00:00"), result["violations"][0]["detail"])

    def test_widened_approval_applies_only_after_the_edit(self):
        narrow = self.approval()
        narrow["edits"] = [{"at": ts(50), "body": narrow["body"].replace("actions: mark_ready", "actions: mark_ready, label")}]
        result = sim.evaluate(snapshot(600, [issue("CSL-90")], [narrow], config=self.CONFIG,
                                       sa_actions=[self.action(10, action="label"), self.action(60, action="label")]))
        self.assertEqual(codes(result), ["sa_out_of_scope"])

    def test_replacement_approval_is_order_independent(self):
        old = self.approval("h1", at=0, limits="per_day=5")
        old.update(deleted=True, deleted_at=ts(20))
        replacement = self.approval("h2", at=30, limits="per_day=5")
        for comments in ([old, replacement], [replacement, old]):
            with self.subTest(order=[c["id"] for c in comments]):
                result = sim.evaluate(snapshot(600, [issue("CSL-90")], comments, config=self.CONFIG,
                                               sa_actions=[self.action(10), self.action(25), self.action(60 * 6)]))
                self.assertEqual(codes(result), ["sa_unknown"])

    def test_approval_edited_out_can_be_replaced(self):
        old = self.approval("h1", at=0, limits="per_day=5")
        old["edits"] = [{"at": ts(20), "body": "withdrawn"}]
        comments = [old, self.approval("h2", at=30, limits="per_day=5")]
        for order in (comments, comments[::-1]):
            with self.subTest(order=[c["id"] for c in order]):
                result = sim.evaluate(snapshot(600, [issue("CSL-90")], order, config=self.CONFIG,
                                               sa_actions=[self.action(10), self.action(25), self.action(60)]))
                self.assertEqual(codes(result), ["sa_unknown"])

    def test_approval_re_added_in_the_same_comment_overlaps(self):
        old = self.approval("h1", at=0, limits="per_day=5")
        old["edits"] = [{"at": ts(20), "body": "withdrawn"}, {"at": ts(40), "body": old["body"]}]
        comments = [old, self.approval("h2", at=30, limits="per_day=5")]
        for order in (comments, comments[::-1]):
            with self.subTest(order=[c["id"] for c in order]):
                with self.assertRaises(sim.SnapshotError):
                    sim.evaluate(snapshot(600, [issue("CSL-90")], order, config=self.CONFIG))

    def test_approval_re_added_after_the_replacement_ends(self):
        old = self.approval("h1", at=0, limits="per_day=5")
        old["edits"] = [{"at": ts(20), "body": "withdrawn"}, {"at": ts(60), "body": old["body"]}]
        new = self.approval("h2", at=30, limits="per_day=5")
        new["edits"] = [{"at": ts(50), "body": "withdrawn"}]
        result = sim.evaluate(snapshot(600, [issue("CSL-90")], [old, new], config=self.CONFIG,
                                       sa_actions=[self.action(10), self.action(25), self.action(40),
                                                   self.action(55), self.action(70)]))
        self.assertEqual(codes(result), ["sa_unknown", "sa_unknown"])

    def test_overlapping_approval_definitions_fail_closed(self):
        for comments in ([self.approval("h1", at=0), self.approval("h2", at=30)],
                         [self.approval("h2", at=30), self.approval("h1", at=0)]):
            with self.assertRaises(sim.SnapshotError):
                sim.evaluate(snapshot(600, [issue("CSL-90")], comments, config=self.CONFIG))

    def test_earliest_revocation_counts_regardless_of_order(self):
        comments = [comment("h3", "CSL-90", 30, "REVOKE SA-1"), self.approval(), comment("h2", "CSL-90", 5, "REVOKE SA-1")]
        result = sim.evaluate(snapshot(600, [issue("CSL-90")], comments, config=self.CONFIG,
                                       sa_actions=[self.action(10)]))
        self.assertEqual(codes(result), ["sa_revoked"])

    def test_unparseable_limits_fail_closed(self):
        with self.assertRaises(sim.SnapshotError):
            self.run_actions([], self.approval(limits="3 per day"))

    def test_agent_posted_standing_approval_is_void(self):
        forged = self.approval(body_prefix="Agent (claude/s1, for Lucas): posting\n")
        result = self.run_actions([self.action(10)], forged)
        self.assertEqual(codes(result), ["sa_unknown"])
        self.assertEqual(result["void_lines"][0]["comment"], "h1")


class MalformedSnapshotTests(unittest.TestCase):
    def base(self):
        return snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)])

    def assert_rejected(self, mutate):
        snap = self.base()
        mutate(snap)
        with self.assertRaises(sim.SnapshotError):
            sim.evaluate(snap)

    def test_missing_now(self):
        self.assert_rejected(lambda s: s.pop("now"))

    def test_timestamp_without_offset(self):
        self.assert_rejected(lambda s: s.update(now="2026-10-20T10:00:00"))

    def test_duplicate_comment_id(self):
        self.assert_rejected(lambda s: s["comments"].append(comment("c1", "CSL-1", 1, "hi")))

    def test_claim_without_branch(self):
        def mutate(s):
            s["comments"][0]["body"] = s["comments"][0]["body"].replace("branch:", "brunch:")
        self.assert_rejected(mutate)

    def test_duplicate_claim_uuid(self):
        self.assert_rejected(lambda s: s["comments"].append(claim("c9", "CSL-1", U1, at=1)))

    def test_edits_out_of_order(self):
        self.assert_rejected(lambda s: s["comments"][0]["edits"].append({"at": ts(-5), "body": "x"}))

    def test_comment_on_unknown_issue(self):
        self.assert_rejected(lambda s: s["comments"].append(comment("c2", "CSL-404", 1, "hi")))

    def test_unknown_config_key(self):
        self.assert_rejected(lambda s: s.update(config={"lease_hours": 2}))

    def test_unknown_author_kind(self):
        self.assert_rejected(lambda s: s.update(commits=[{"sha": "x", "branch": "b", "at": ts(1), "author_kind": "robot"}]))

    def test_blocked_by_unknown_issue(self):
        self.assert_rejected(lambda s: s["issues"][0].update(blocked_by=["CSL-404"]))

    def test_approval_window_is_capped_at_fifteen(self):
        # Lucas's DECISION on CSL-411 (comment 8cda521b); CSL-418.
        for value in (16, 10**6, -1):
            with self.subTest(value=value):
                self.assert_rejected(lambda s, v=value: s.update(config={"approval_window_minutes": v}))
        for value in (0, 15):
            with self.subTest(value=value):
                snap = self.base()
                snap["config"] = {"approval_window_minutes": value}
                sim.evaluate(snap)

    def test_config_value_of_wrong_type(self):
        self.assert_rejected(lambda s: s.update(config={"lease_minutes": "120"}))
        self.assert_rejected(lambda s: s.update(config={"readback_seconds": None}))

    def test_null_event_timestamp(self):
        self.assert_rejected(lambda s: s.update(writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": None}]))

    def test_edit_body_must_be_text(self):
        self.assert_rejected(lambda s: s["comments"][0]["edits"].append({"at": ts(5), "body": 5}))

    def test_null_lists(self):
        self.assert_rejected(lambda s: s["comments"][0].update(edits=None))
        self.assert_rejected(lambda s: s["issues"][0].update(blocked_by=None))

    def test_program_and_write_kind_types(self):
        self.assert_rejected(lambda s: s.update(program={"review_slots": "2"}))
        self.assert_rejected(lambda s: s.update(writes=[{"kind": ["push"], "issue": "CSL-1", "claim": U1, "at": ts(1)}]))

    def test_flags_must_be_booleans(self):
        self.assert_rejected(lambda s: s["issues"][0].update(admitted="false"))
        self.assert_rejected(lambda s: s["issues"][0].update(next_in_order="no"))

    def test_triage_issue_cannot_be_admitted(self):
        self.assert_rejected(lambda s: s["issues"][0].update(status="Triage"))

    def test_arithmetic_overflow_fails_closed(self):
        self.assert_rejected(lambda s: s.update(config={"lease_minutes": 10**15}))
        snap = snapshot(10, [issue("CSL-1")], [])
        snap["now"] = "9999-12-31T23:00:00Z"
        snap["comments"] = [dict(claim("c1", "CSL-1", U1), created_at="9999-12-31T22:00:00Z", edits=[])]
        with self.assertRaises(sim.SnapshotError):
            sim.evaluate(snap)

    def test_deleted_at_after_the_snapshot_time(self):
        def mutate(s):
            s["comments"][0].update(deleted=True, deleted_at=ts(30))
        self.assert_rejected(mutate)

    def test_repo_must_be_text(self):
        self.assert_rejected(lambda s: s.update(repo=5))

    def test_malformed_confirmation_timestamp(self):
        def mutate(s):
            body = s["comments"][0]["edits"][0]["body"]
            s["comments"][0]["edits"][0]["body"] = body.replace(ts(0, 70), "2026-99-99Tnonsense")
        self.assert_rejected(mutate)

    def test_malformed_confirmation_placeholder(self):
        for value in ("(", "(pending read-back", "pending read-back)", "()", "( )", "(a)(b)", "(()", "())"):
            def mutate(s, value=value):
                body = s["comments"][0]["edits"][0]["body"]
                s["comments"][0]["edits"][0]["body"] = body.replace(ts(0, 70), value)
            with self.subTest(value=value):
                self.assert_rejected(mutate)

    def test_run_ending_before_it_starts(self):
        self.assert_rejected(lambda s: s.update(runs=[{"id": "r", "claim": U1, "started_at": ts(5), "ended_at": ts(4)}]))

    def test_edit_cannot_change_claim_identity(self):
        changes = {
            "claim": lambda b: b.replace(f"claim: {U1}", f"claim: {U2}"),
            "scope": lambda b: b.replace("scope: whole issue", "scope: part of it"),
            "branch": lambda b: b.replace("branch: csl-1-work @", "branch: csl-1-other @"),
            "repo": lambda b: b + "\nrepo: elsewhere",
            "takeover": lambda b: b + f"\ntakeover of {U2}",
        }
        for field, change in changes.items():
            def mutate(s, change=change):
                confirm = s["comments"][0]["edits"][0]
                confirm["body"] = change(confirm["body"])
            with self.subTest(field=field):
                self.assert_rejected(mutate)

    def test_edit_cannot_drop_claim_identity(self):
        drops = {"claim": f"claim: {U1}\n", "scope": "scope: whole issue\n", "branch": "branch: csl-1-work @ abc123, base def456\n"}
        for field, line in drops.items():
            def mutate(s, line=line):
                confirm = s["comments"][0]["edits"][0]
                confirm["body"] = confirm["body"].replace(line, "")
            with self.subTest(field=field):
                self.assert_rejected(mutate)
        takeover = claim("c2", "CSL-1", U2, at=152, session="cursor/s2", extra=(f"takeover of {U1}",))
        takeover["edits"][0]["body"] = takeover["edits"][0]["body"].replace(f"\ntakeover of {U1}", "")
        with self.assertRaises(sim.SnapshotError):
            sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), takeover]))

    def test_claim_with_an_incomplete_agent_prefix(self):
        for prefix in ("Agent (claude/s1):", "Agent (claude/s1, for ):", "Agent (claude, for Lucas):",
                       "Agent (/s1, for Lucas):", "Agent (claude/, for Lucas):"):
            def mutate(s, prefix=prefix):
                c = s["comments"][0]
                for holder in (c, *c["edits"]):
                    holder["body"] = holder["body"].replace("Agent (claude/s1, for Lucas):", prefix)
            with self.subTest(prefix=prefix):
                self.assert_rejected(mutate)

    def test_claim_edit_that_swaps_the_agent_identity(self):
        swapped = claim("c2", "CSL-2", U2, session="cursor/s2")
        swapped["edits"][0]["body"] = swapped["edits"][0]["body"].replace("cursor/s2", "claude/s1")
        result = sim.evaluate(snapshot(10, [issue("CSL-2")], [swapped]))
        self.assertEqual(codes(result), ["comment_prefix_changed"])

    def test_edit_after_deletion(self):
        def mutate(s):
            note = comment("h1", "CSL-1", 1, "a note", deleted=True)
            note["deleted_at"] = ts(5)
            note["edits"] = [{"at": ts(6), "body": "edited later"}]
            s["comments"].append(note)
        self.assert_rejected(mutate)

    def test_edit_may_move_the_branch_head(self):
        renewed = claim("c1", "CSL-1", U1)
        renewed["edits"].append({"at": ts(30), "body": renewed["edits"][0]["body"].replace("@ abc123", "@ fedcba9")})
        self.assertEqual(codes(sim.evaluate(snapshot(40, [issue("CSL-1")], [renewed]))), [])

    def test_admission_naming_an_unknown_issue(self):
        self.assert_rejected(lambda s: s.update(admissions=[{"issue": "CSL-404", "at": ts(5)}]))

    def test_run_naming_an_unknown_claim(self):
        unknown = "99999999-9999-4999-8999-999999999999"
        for claim_id in (unknown, None, 7):
            with self.subTest(claim=claim_id):
                self.assert_rejected(lambda s, c=claim_id: s.update(runs=[{"id": "r", "claim": c, "started_at": ts(5)}]))

    def test_events_after_the_snapshot_time(self):
        self.assert_rejected(lambda s: s["comments"].append(comment("c2", "CSL-1", 20, "later")))
        self.assert_rejected(lambda s: s.update(writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(20)}]))

    def test_unknown_status(self):
        self.assert_rejected(lambda s: s["issues"][0].update(status="Blocked"))


def scoped(cid, issue_id, uuid, child, **kw):
    """A claim on `issue_id` whose scope is its sub-issue `child`."""
    made = claim(cid, issue_id, uuid, **kw)
    for body in [made] + made["edits"]:
        body["body"] = body["body"].replace("scope: whole issue", f"scope: sub-issue {child}")
    return made


class SubIssueScopeTests(unittest.TestCase):
    """Decision (1), CSL-389 comment 425b223c: `scope: sub-issue CSL-N`."""

    def family(self, child_status="In Progress", blocked_by=()):
        return [issue("CSL-1"), issue("CSL-2", status=child_status, parent="CSL-1", blocked_by=blocked_by)]

    def test_writes_on_the_child_count_under_the_claim(self):
        writes = [{"kind": "push", "issue": "CSL-2", "claim": U1, "at": ts(5)},
                  {"kind": "status", "issue": "CSL-1", "claim": U1, "at": ts(6)}]
        result = sim.evaluate(snapshot(10, self.family(), [scoped("c1", "CSL-1", U1, "CSL-2")], writes=writes))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)

    def test_whole_issue_claim_does_not_cover_a_child(self):
        writes = [{"kind": "push", "issue": "CSL-2", "claim": U1, "at": ts(5)}]
        result = sim.evaluate(snapshot(10, self.family(), [claim("c1", "CSL-1", U1)], writes=writes))
        self.assertEqual(codes(result), ["write_outside_claim"])

    def test_scope_naming_an_issue_that_is_not_a_child(self):
        issues = [issue("CSL-1"), issue("CSL-2")]
        writes = [{"kind": "push", "issue": "CSL-2", "claim": U1, "at": ts(5)}]
        result = sim.evaluate(snapshot(10, issues, [scoped("c1", "CSL-1", U1, "CSL-2")], writes=writes))
        self.assertEqual(codes(result), ["scope_not_a_child", "write_outside_claim"])

    def test_other_scope_forms_are_rejected(self):
        for scope in ("part of it", "sub-issue CSL-9", "sub-issue CSL-2, CSL-3", "sub-issues CSL-2"):
            with self.subTest(scope=scope):
                made = claim("c1", "CSL-1", U1)
                for body in [made] + made["edits"]:
                    body["body"] = body["body"].replace("scope: whole issue", f"scope: {scope}")
                with self.assertRaises(sim.SnapshotError):
                    sim.evaluate(snapshot(10, self.family(), [made]))

    def test_held_child_holds_the_claim_and_its_writes(self):
        family = self.family(blocked_by=["CSL-3"]) + [issue("CSL-3")]
        writes = [{"kind": "push", "issue": "CSL-2", "claim": U1, "at": ts(5)}]
        result = sim.evaluate(snapshot(10, family, [scoped("c1", "CSL-1", U1, "CSL-2")], writes=writes))
        self.assertEqual(codes(result), ["claim_on_held_scope", "write_while_held"])

    def test_child_held_after_the_claim_stops_parent_writes_too(self):
        # Devin and Fable reviews of recursion#260: a hold on the scoped child
        # covers the whole claim.
        family = self.family(blocked_by=["CSL-3"]) + [issue("CSL-3", decision=decision(pings=(3,)))]
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(5)},
                  {"kind": "push", "issue": "CSL-2", "claim": U1, "at": ts(6)}]
        result = sim.evaluate(snapshot(10, family, [scoped("c1", "CSL-1", U1, "CSL-2")], writes=writes))
        self.assertEqual(codes(result), ["write_while_held", "write_while_held"])

    def test_answered_child_blocker_needs_a_decision_record(self):
        family = self.family(blocked_by=["CSL-3"]) + [issue("CSL-3", decision=decision(pings=(0.1,)))]
        answer = comment("h1", "CSL-3", 0.5, "DECISION: A")
        late = scoped("c1", "CSL-1", U1, "CSL-2", at=1)
        result = sim.evaluate(snapshot(10, family, [answer, late]))
        self.assertEqual([(v["code"], v["issue"]) for v in result["violations"]], [("decision_not_recorded", "CSL-3")])
        record = comment("r1", "CSL-3", 1.5, "Agent (claude/s1, for Lucas): noted\ndecision-record: h1 A")
        self.assertEqual(codes(sim.evaluate(snapshot(10, family, [answer, record, late]))), [])

    def test_parent_not_done_while_a_child_is_open_or_held(self):
        issues = [issue("CSL-1", status="Done"), issue("CSL-2", parent="CSL-1")]
        result = sim.evaluate(snapshot(10, issues, []))
        self.assertEqual([(v["code"], v["detail"]) for v in result["violations"]], [("done_with_open_child", "CSL-2 open")])
        held = [issue("CSL-1", status="Done"), issue("CSL-2", status="Done", parent="CSL-1", blocked_by=["CSL-3"]),
                issue("CSL-3")]
        details = [(v["code"], v["issue"], v["detail"]) for v in sim.evaluate(snapshot(10, held, []))["violations"]]
        self.assertIn(("done_with_open_child", "CSL-1", "CSL-2 held"), details)
        closed = [issue("CSL-1", status="Done"), issue("CSL-2", status="Canceled", parent="CSL-1")]
        self.assertEqual(codes(sim.evaluate(snapshot(10, closed, []))), [])

    def test_bad_parents_are_rejected(self):
        for issues in ([issue("CSL-1", parent="CSL-9")],
                       [issue("CSL-1", parent="CSL-2"), issue("CSL-2", parent="CSL-1")],
                       [issue("CSL-1", parent="CSL-1")]):
            with self.subTest(issues=issues):
                with self.assertRaises(sim.SnapshotError):
                    sim.evaluate(snapshot(10, issues, []))


class SeveralTakeoverTests(unittest.TestCase):
    """Decision (2): one claim may take over several expired claims."""

    def two_expired(self, second_heartbeat=None):
        edits = [edit(second_heartbeat, U2, "heartbeat-at: back", session="cursor/s2", confirmed=ts(0.3, 70))] \
            if second_heartbeat is not None else []
        return [claim("c1", "CSL-1", U1), claim("c2", "CSL-1", U2, at=0.3, session="cursor/s2", more_edits=edits)]

    def taker(self, at=152):
        return claim("c3", "CSL-1", U3, at=at, session="grok/s3", extra=(f"takeover of {U1}", f"takeover of {U2}"))

    def test_taking_over_both_expired_claims_wins(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(170)}]
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], self.two_expired() + [self.taker()], writes=writes))
        status = result["issues"]["CSL-1"]["takeovers"][U3]
        self.assertTrue(status["allowed"])
        self.assertEqual(status["of"], [U1, U2])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U3)
        # U2 lost the race to U1 and never withdrew.
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn"])

    def test_one_refused_target_refuses_the_takeover(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(170)}]
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], self.two_expired(second_heartbeat=40) + [self.taker()],
                                       writes=writes))
        status = result["issues"]["CSL-1"]["takeovers"][U3]
        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], f"{U2}: lease plus grace not passed")
        self.assertTrue(status["targets"][U1]["allowed"])
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        refused = [v["detail"] for v in result["violations"] if v["code"] == "takeover_refused"]
        self.assertEqual(refused, [f"lease plus grace not passed ({U2})"])
        self.assertIn("write_by_non_winner", codes(result))

    def test_the_same_claim_twice_is_rejected(self):
        twice = claim("c3", "CSL-1", U3, at=152, session="grok/s3", extra=(f"takeover of {U1}", f"takeover of {U1}"))
        with self.assertRaises(sim.SnapshotError):
            sim.evaluate(snapshot(175, [issue("CSL-1")], self.two_expired() + [twice]))

    def test_an_edit_cannot_drop_one_of_the_takeovers(self):
        taker = self.taker()
        taker["edits"].append({"at": ts(160), "body": claim_body(U3, "grok/s3", confirmed=ts(152, 70),
                                                                   extra=(f"takeover of {U1}",))})
        with self.assertRaises(sim.SnapshotError):
            sim.evaluate(snapshot(175, [issue("CSL-1")], self.two_expired() + [taker]))


class StandingLoserTests(unittest.TestCase):
    """Decision (3): a loser that never withdrew cannot win when the winner expires."""

    def test_standing_loser_does_not_win_when_the_original_expires(self):
        loser = claim("c3", "CSL-1", U3, at=100, session="grok/s3")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(125)},
                  {"kind": "pr", "issue": "CSL-1", "claim": U3, "at": ts(200)}]
        result = sim.evaluate(snapshot(210, [issue("CSL-1")], [claim("c1", "CSL-1", U1), loser], writes=writes))
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn", "write_by_non_winner",
                                         "write_by_non_winner"])

    def test_losing_taker_does_not_inherit_when_the_winning_taker_expires(self):
        # Fable review of recursion#260 F1: two takeovers of one expired claim
        # race at the end of their wait, not at read-back.
        first = claim("c2", "CSL-1", U2, at=152, session="cursor/s2", extra=(f"takeover of {U1}",))
        renew = [{"at": ts(m), "body": claim_body(U3, "grok/s3", confirmed=ts(152, 70),
                                                   extra=(f"takeover of {U1}", f"heartbeat-at: {m}"))} for m in (250, 265)]
        second = claim("c3", "CSL-1", U3, at=152, session="grok/s3", extra=(f"takeover of {U1}",), more_edits=renew)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(278)}]
        result = sim.evaluate(snapshot(280, [issue("CSL-1")], [claim("c1", "CSL-1", U1), first, second], writes=writes))
        self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
        self.assertEqual([(v["code"], v["claim"]) for v in result["violations"]], [("write_by_non_winner", U3)])

    def test_loser_wins_through_its_own_takeover(self):
        loser = claim("c3", "CSL-1", U3, at=100, session="grok/s3", confirm_after=None,
                      more_edits=[{"at": ts(101.5), "body": claim_body(U3, "grok/s3", extra=("withdrawn: lost",))}])
        u4 = "00000000-0000-4000-8000-000000000004"
        retry = claim("c4", "CSL-1", u4, at=152, session="grok/s3", extra=(f"takeover of {U1}",))
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [claim("c1", "CSL-1", U1), loser, retry]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], u4)
        self.assertEqual(codes(result), [])

    def test_loser_may_still_win_after_the_original_releases(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(50, U1, "released")])
        loser = claim("c3", "CSL-1", U3, at=10, session="grok/s3")
        record = release_record("r1", "CSL-1", U1, 50)
        result = sim.evaluate(snapshot(60, [issue("CSL-1")], [original, record, loser]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U3)


class TakeoverEffectTests(unittest.TestCase):
    """Devin's private review of recursion#258 (CSL-389 comment 928ee10b)."""

    def ended_taker(self, at, how):
        body = claim_body(U2, "cursor/s2", extra=(f"takeover of {U1}", how))
        return claim("c2", "CSL-1", U2, at=152, session="cursor/s2", confirm_after=None,
                     extra=(f"takeover of {U1}",), more_edits=[{"at": ts(at), "body": body}])

    def test_cancelled_takeover_does_not_clear_the_original(self):
        fresh = claim("c3", "CSL-1", U3, at=168, session="grok/s3")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(170)}]
        for how in ("withdrawn: cancelled", "released"):
            with self.subTest(how=how):
                comments = [claim("c1", "CSL-1", U1), self.ended_taker(155, how), fresh]
                if how == "released":
                    comments.append(release_record("r1", "CSL-1", U2, 155, session="cursor/s2"))
                result = sim.evaluate(snapshot(175, [issue("CSL-1")], comments, writes=writes))
                self.assertIsNone(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"])
                self.assertEqual(codes(result), ["claim_without_takeover", "write_by_non_winner"])

    def test_takeover_that_took_effect_then_ended_clears_the_original(self):
        taker = claim("c2", "CSL-1", U2, at=152, session="cursor/s2", extra=(f"takeover of {U1}",),
                      more_edits=[{"at": ts(170), "body": claim_body(U2, "cursor/s2", confirmed=ts(152, 70),
                                                                      extra=(f"takeover of {U1}", "released"))}])
        fresh = claim("c3", "CSL-1", U3, at=172, session="grok/s3")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U3, "at": ts(175)}]
        record = release_record("r1", "CSL-1", U2, 170, session="cursor/s2")
        result = sim.evaluate(snapshot(180, [issue("CSL-1")], [claim("c1", "CSL-1", U1), taker, record, fresh],
                                       writes=writes))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U3)
        self.assertEqual(codes(result), [])

    def test_takeover_withdrawn_before_its_acknowledgement_never_took_effect(self):
        config = {"coordinator_ack_required": True}
        fresh = claim("c3", "CSL-1", U3, at=180, session="grok/s3")
        for ack_at, expected in ((175, ["claim_without_takeover"]), (160, [])):
            with self.subTest(ack_at=ack_at):
                comments = [claim("c1", "CSL-1", U1), self.ended_taker(170, "withdrawn: done"),
                            comment("h1", "CSL-1", ack_at, f"ACK TAKEOVER {U1}"), fresh]
                result = sim.evaluate(snapshot(185, [issue("CSL-1")], comments, config=config))
                self.assertEqual(codes(result), expected)

    def test_release_before_an_undated_deletion_still_counts(self):
        # Devin review of recursion#260: an undated deletion must not hide a
        # known earlier release of the original.
        original = claim("c1", "CSL-1", U1, deleted=True, more_edits=[edit(30, U1, "released")])
        early = claim("c2", "CSL-1", U2, at=40, session="cursor/s2", extra=(f"takeover of {U1}",))
        result = sim.evaluate(snapshot(60, [issue("CSL-1")], [original, early]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "original released")
        self.assertNotIn("takeover_refused", codes(result))

    def test_deletion_after_withdrawal_keeps_the_fault_answered(self):
        for how in ("withdrawn: premature", "released"):
            with self.subTest(how=how):
                early = claim("c2", "CSL-1", U2, at=125, session="cursor/s2", extra=(f"takeover of {U1}",),
                              more_edits=[{"at": ts(127), "body": claim_body(U2, "cursor/s2", confirmed=ts(125, 70),
                                                                              extra=(f"takeover of {U1}", how))}])
                early.update(deleted=True, deleted_at=ts(130))
                comments = [claim("c1", "CSL-1", U1), early]
                if how == "released":
                    comments.append(release_record("r1", "CSL-1", U2, 127, session="cursor/s2"))
                result = sim.evaluate(snapshot(140, [issue("CSL-1")], comments))
                self.assertNotIn("takeover_refused", codes(result))

    def test_deletion_alone_keeps_the_fault(self):
        early = claim("c2", "CSL-1", U2, at=125, session="cursor/s2", extra=(f"takeover of {U1}",))
        early.update(deleted=True, deleted_at=ts(130))
        result = sim.evaluate(snapshot(140, [issue("CSL-1")], [claim("c1", "CSL-1", U1), early]))
        self.assertIn("takeover_refused", codes(result))

    def test_original_deleted_at_an_unknown_time_does_not_excuse_a_premature_takeover(self):
        original = claim("c1", "CSL-1", U1, deleted=True)
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [original, LeaseTests().takeover(at=125)]))
        self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])
        self.assertIn("takeover_refused", codes(result))
        timed = claim("c1", "CSL-1", U1, deleted=True)
        timed["deleted_at"] = ts(100)
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [timed, LeaseTests().takeover(at=125)]))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "original deleted")


def approved(cid, issue_id, at, *lines, developer="Lucas", session="claude/s1", confirmed=None, marker=None):
    """An agent comment carrying a developer-approved line (CONTROL_PLANE.md, D-043)."""
    confirmed = confirmed if confirmed is not None else ts(at, -30)
    marker = marker if marker is not None else (
        f"developer-approved: {developer}, exact text confirmed {confirmed}, session {session.partition('/')[2]}")
    return comment(cid, issue_id, at, "\n".join([f"Agent ({session}, for {developer}): approved text", marker, *lines]))


class DeveloperApprovedTests(unittest.TestCase):
    """CSL-402: a valid developer-approved comment speaks as that developer (D-043)."""

    def decision_issues(self):
        return [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]

    def test_approved_decision_counts(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 5, "DECISION: B")]))
        self.assertTrue(result["decisions"]["CSL-2"]["answered"])
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "B")
        self.assertFalse(result["issues"]["CSL-1"]["held"])
        self.assertEqual(result["void_lines"], [])
        self.assertEqual(result["developer_approved"], [{"comment": "a1", "developer": "Lucas", "deleted": False}])
        self.assertEqual(codes(result), [])

    def test_full_session_id_in_the_marker_also_counts(self):
        marker = f"developer-approved: Lucas, exact text confirmed {ts(4)}, session claude/s1"
        result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 5, "DECISION: B", marker=marker)]))
        self.assertTrue(result["decisions"]["CSL-2"]["answered"])

    def test_approved_batched_answer_counts(self):
        issues = [issue("CSL-11", decision=decision()), issue("CSL-12", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, [approved("a1", "CSL-11", 5, "CSL-11: B; CSL-12: A")]))
        self.assertEqual(result["decisions"]["CSL-11"]["answer"], "B")
        self.assertEqual(result["decisions"]["CSL-12"]["answer"], "A")

    def test_approved_hold_and_revoke_count(self):
        held = sim.evaluate(snapshot(10, [issue("CSL-1")], [approved("a1", "CSL-1", 5, "HOLD CSL-1")]))
        self.assertTrue(held["issues"]["CSL-1"]["held"])
        revoke = approved("a1", "CSL-1", 5, f"REVOKE CLAIM {U1}")
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(6)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1), revoke], writes=writes))
        self.assertEqual(codes(result), ["write_after_revoke"])

    def test_invalid_markers_stay_void_and_are_reported(self):
        cases = {
            "approved by 'Samir' but posted for 'Lucas'": dict(
                marker=f"developer-approved: Samir, exact text confirmed {ts(4)}, session s1"),
            "session does not match the agent prefix": dict(
                marker=f"developer-approved: Lucas, exact text confirmed {ts(4)}, session s9"),
            "approval confirmed after posting": dict(confirmed=ts(6)),
            "malformed developer-approved line": dict(marker="developer-approved: Lucas"),
            "bad confirmation time": dict(marker="developer-approved: Lucas, exact text confirmed soon, session s1"),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 5, "DECISION: B", **kwargs)]))
                self.assertFalse(result["decisions"]["CSL-2"]["answered"])
                self.assertEqual([(v["code"], v["detail"]) for v in result["violations"]],
                                 [("developer_approval_invalid", f"a1: {reason}")])
                self.assertEqual([v["comment"] for v in result["void_lines"]], ["a1"])
                self.assertEqual(result["developer_approved"], [])

    def test_two_markers_are_invalid(self):
        extra = f"developer-approved: Lucas, exact text confirmed {ts(4)}, session s1"
        result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 5, extra, "DECISION: B")]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertIn(("developer_approval_invalid", "a1: more than one developer-approved line"),
                      [(v["code"], v["detail"]) for v in result["violations"]])

    def test_an_edit_voids_the_approval(self):
        edited = approved("a1", "CSL-2", 5, "DECISION: B")
        edited["edits"] = [{"at": ts(7), "body": edited["body"] + "\n(typo fixed)"}]
        result = sim.evaluate(snapshot(10, self.decision_issues(), [edited]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertIn(("developer_approval_invalid", "a1: edited after posting"),
                      [(v["code"], v["detail"]) for v in result["violations"]])

    def test_a_claim_record_cannot_be_developer_approved(self):
        made = claim("c1", "CSL-1", U1)
        made["body"] += f"\ndeveloper-approved: Lucas, exact text confirmed {ts(0)}, session s1"
        made["edits"] = []
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [made]))
        self.assertIn(("developer_approval_invalid", "c1: a claim record cannot be developer-approved"),
                      [(v["code"], v["detail"]) for v in result["violations"]])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [U1])

    def test_deleted_approved_answer_counts_until_its_deletion(self):
        answer = approved("a1", "CSL-2", 5, "DECISION: B")
        answer.update(deleted=True, deleted_at=ts(8))
        result = sim.evaluate(snapshot(10, self.decision_issues(), [answer]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertTrue(result["issues"]["CSL-1"]["held"])

    def test_agent_prefix_inside_the_approved_text_is_invalid(self):
        # Fable review of recursion#264: a second prefix must not smuggle in a claim.
        smuggled = approved("a1", "CSL-1", 5, "Agent (cursor/s9, for Samir): claim v1", f"claim: {U2}",
                            "scope: whole issue", "branch: csl-1-x @ abc, base def", "confirmed-at: (pending read-back)")
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [smuggled]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"], {})
        self.assertEqual(result["developer_approved"], [])
        self.assertIn(("developer_approval_invalid", "a1: agent prefix inside the approved text"),
                      [(v["code"], v["detail"]) for v in result["violations"]])

    def test_standing_approvals_stay_unprefixed_founder_comments(self):
        config = {"standing_approvals_issue": "CSL-5"}
        sa = approved("s1", "CSL-5", 0, "Standing approval SA-1", "actions: push", "scope: recursion",
                      "expires: 2026-11-01")
        acts = [{"sa": "SA-1", "action": "push", "repo": "recursion", "agent": "claude", "at": ts(5)}]
        result = sim.evaluate(snapshot(10, [issue("CSL-5")], [sa], config=config, sa_actions=acts))
        self.assertEqual(codes(result), ["sa_unknown"])
        human = comment("h1", "CSL-5", 0, "Standing approval SA-1\nactions: push\nscope: recursion\nexpires: 2026-11-01")
        revoke = approved("a1", "CSL-5", 1, "REVOKE SA-1")
        result = sim.evaluate(snapshot(10, [issue("CSL-5")], [human, revoke], config=config, sa_actions=acts))
        self.assertEqual(codes(result), [])

    def test_out_of_range_confirmation_time_is_reported_not_fatal(self):
        marker = "developer-approved: Lucas, exact text confirmed 0001-01-01T00:00:00+14:00, session s1"
        result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 5, "DECISION: B", marker=marker)]))
        self.assertIn(("developer_approval_invalid", "a1: bad confirmation time"),
                      [(v["code"], v["detail"]) for v in result["violations"]])

    def test_decision_record_in_an_approved_comment_still_counts(self):
        answer = comment("h1", "CSL-2", 1, "DECISION: B")
        record = approved("a1", "CSL-2", 2, "decision-record: h1 B")
        result = sim.evaluate(snapshot(10, self.decision_issues(), [answer, record, claim("c1", "CSL-1", U1, at=3)]))
        self.assertNotIn("decision_not_recorded", codes(result))

    def test_approval_must_be_confirmed_within_fifteen_minutes(self):
        # Lucas's DECISION on CSL-402 (comment b3c5c1b8); CSL-411.
        stale = approved("a1", "CSL-2", 30, "DECISION: B", confirmed=ts(14, 59))
        result = sim.evaluate(snapshot(40, self.decision_issues(), [stale]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertIn(("developer_approval_invalid", "a1: approval confirmed more than 15 minutes before posting"),
                      [(v["code"], v["detail"]) for v in result["violations"]])
        edge = approved("a1", "CSL-2", 30, "DECISION: B", confirmed=ts(15))
        self.assertTrue(sim.evaluate(snapshot(40, self.decision_issues(), [edge]))["decisions"]["CSL-2"]["answered"])
        narrower = sim.evaluate(snapshot(40, self.decision_issues(), [edge], config={"approval_window_minutes": 10}))
        self.assertFalse(narrower["decisions"]["CSL-2"]["answered"])

    def test_incomplete_agent_prefix_is_invalid(self):
        for session, marker_session in (("/s1", "s1"), ("claude/", "claude/"), ("claude", "claude"), (" /s1", "s1")):
            with self.subTest(session=session):
                marker = f"developer-approved: Lucas, exact text confirmed {ts(4)}, session {marker_session}"
                made = comment("a1", "CSL-2", 5, "\n".join([f"Agent ({session}, for Lucas): t", marker, "DECISION: B"]))
                result = sim.evaluate(snapshot(10, self.decision_issues(), [made]))
                self.assertFalse(result["decisions"]["CSL-2"]["answered"])
                self.assertIn(("developer_approval_invalid", "a1: incomplete agent prefix"),
                              [(v["code"], v["detail"]) for v in result["violations"]])

    def test_marker_added_by_a_later_edit_is_reported(self):
        plain = comment("a1", "CSL-2", 5, "Agent (claude/s1, for Lucas): relaying\nDECISION: B")
        plain["edits"] = [{"at": ts(7), "body": "Agent (claude/s1, for Lucas): relaying\n"
                           f"developer-approved: Lucas, exact text confirmed {ts(6)}, session s1\nDECISION: B"}]
        result = sim.evaluate(snapshot(10, self.decision_issues(), [plain]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertIn(("developer_approval_invalid", "a1: edited after posting"),
                      [(v["code"], v["detail"]) for v in result["violations"]])

    def test_unmarked_agent_comment_is_still_void(self):
        plain = comment("a1", "CSL-2", 5, "Agent (claude/s1, for Lucas): relaying\nDECISION: B")
        result = sim.evaluate(snapshot(10, self.decision_issues(), [plain]))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertEqual(codes(result), [])


class CommandLineTests(unittest.TestCase):
    def run_cli(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(payload, encoding="utf-8")
            return subprocess.run([sys.executable, "-X", "utf8", "-B", str(SIM), str(path)],
                                  capture_output=True, text=True, check=False)

    def test_clean_snapshot_exits_zero(self):
        done = self.run_cli(json.dumps(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1)])))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["violations"], [])

    def test_violations_exit_one(self):
        snap = snapshot(10, [issue("CSL-1", admitted=False)], [claim("c1", "CSL-1", U1)])
        self.assertEqual(self.run_cli(json.dumps(snap)).returncode, 1)

    def test_malformed_snapshot_exits_two(self):
        done = self.run_cli("{\"issues\": []}")
        self.assertEqual(done.returncode, 2)
        self.assertIn("AGENT_PROTOCOL_SIM_ERROR", done.stderr)


class PrePrefixDecisionTests(unittest.TestCase):
    """S30 / D17. CSL-417, Lucas's DECISION e6d37489 (option A): an unprefixed answer from before the agent-prefix rule
    is unverified and does not count. Confirmation is not modelled."""

    CUTOFF = {"agent_prefix_since": ts(5)}

    def decision_issues(self):
        return [issue("CSL-1", blocked_by=["CSL-2"]), issue("CSL-2", decision=decision())]

    def test_pre_cutoff_decision_is_unverified(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-2", 3, "DECISION: B")],
                                       config=self.CUTOFF))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertTrue(result["issues"]["CSL-1"]["held"])
        self.assertIn(("decision_unverified", "CSL-2", "h1"),
                      [(v["code"], v["issue"], v["detail"]) for v in result["violations"]])
        self.assertEqual(result["void_lines"],
                         [{"comment": "h1", "line": "DECISION: B", "reason": "before the agent-prefix rule"}])

    def test_post_cutoff_decision_counts(self):
        for at in (5, 7):
            with self.subTest(at=at):
                result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-2", at, "DECISION: B")],
                                               config=self.CUTOFF))
                self.assertEqual(result["decisions"]["CSL-2"]["answer"], "B")
                self.assertNotIn("decision_unverified", codes(result))

    def test_pre_cutoff_batched_answer_is_unverified(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-2", 3, "CSL-2: B")],
                                       config=self.CUTOFF))
        self.assertFalse(result["decisions"]["CSL-2"]["answered"])
        self.assertIn("decision_unverified", codes(result))

    def test_later_answer_still_counts_after_an_unverified_one(self):
        comments = [comment("h1", "CSL-2", 3, "DECISION: A"), comment("h2", "CSL-2", 7, "DECISION: B")]
        result = sim.evaluate(snapshot(10, self.decision_issues(), comments, config=self.CUTOFF))
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "B")
        self.assertIn("decision_unverified", codes(result))

    def test_pre_cutoff_hold_is_not_flagged(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-2", 3, "DECISION: HOLD")],
                                       config=self.CUTOFF))
        self.assertNotIn("decision_unverified", codes(result))
        self.assertEqual(result["void_lines"], [])

    def test_pre_cutoff_decision_line_on_non_decision_issue_is_not_flagged(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-1", 3, "DECISION: B")],
                                       config=self.CUTOFF))
        self.assertNotIn("decision_unverified", codes(result))

    def test_developer_approved_comment_is_not_affected(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [approved("a1", "CSL-2", 3, "DECISION: B")],
                                       config=self.CUTOFF))
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "B")
        self.assertNotIn("decision_unverified", codes(result))

    def test_null_cutoff_turns_the_check_off(self):
        result = sim.evaluate(snapshot(10, self.decision_issues(), [comment("h1", "CSL-2", 3, "DECISION: B")],
                                       config={"agent_prefix_since": None}))
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "B")

    def test_default_cutoff_is_the_recursion_247_merge(self):
        self.assertEqual(sim.DEFAULT_CONFIG["agent_prefix_since"], "2026-10-21T00:39:04Z")
        snap = {"now": "2026-10-21T01:00:00Z", "repo": "recursion", "issues": self.decision_issues(),
                "comments": [comment("h1", "CSL-2", 0, "DECISION: B") | {"created_at": "2026-10-21T00:39:03Z"},
                             comment("h2", "CSL-2", 0, "DECISION: A") | {"created_at": "2026-10-21T00:39:04Z"}]}
        result = sim.evaluate(snap)
        self.assertEqual(result["decisions"]["CSL-2"]["answer"], "A")
        self.assertIn(("decision_unverified", "h1"), [(v["code"], v["detail"]) for v in result["violations"]])

    def test_bad_cutoff_is_rejected(self):
        for value in ("yesterday", 5, "2026-10-21T00:39:04"):
            with self.subTest(value=value), self.assertRaisesRegex(sim.SnapshotError, "agent_prefix_since"):
                sim.evaluate(snapshot(10, self.decision_issues(), [], config={"agent_prefix_since": value}))

    def test_pre_cutoff_batch_is_reported_on_every_named_issue(self):
        issues = self.decision_issues() + [issue("CSL-3", decision=decision())]
        result = sim.evaluate(snapshot(10, issues, [comment("h1", "CSL-2", 3, "CSL-2: B; CSL-3: A")],
                                       config=self.CUTOFF))
        self.assertEqual(sorted(v["issue"] for v in result["violations"] if v["code"] == "decision_unverified"),
                         ["CSL-2", "CSL-3"])
        self.assertFalse(result["decisions"]["CSL-3"]["answered"])


class CrossSessionOverlapTests(unittest.TestCase):
    """Codex P1 on #276: claims from different sessions in one repository may
    not overlap by issue scope or branch (Lucas, 18:54Z 2026-10-21, relayed)."""

    def family(self):
        return [issue("CSL-1"), issue("CSL-2", status="In Progress", parent="CSL-1"), issue("CSL-4")]

    def evaluate(self, now, comments, writes=()):
        return sim.evaluate(snapshot(now, self.family(), comments, writes=list(writes)))

    def test_child_claim_after_a_parent_scoped_claim_overlaps(self):
        child = claim("c2", "CSL-2", U2, at=1, session="cursor/s2", branch="csl-2-work")
        result = self.evaluate(10, [scoped("c1", "CSL-1", U1, "CSL-2"), child])
        self.assertEqual(codes(result), ["overlapping_claim"])

    def test_parent_scoped_claim_after_a_child_claim_overlaps(self):
        parent = scoped("c2", "CSL-1", U2, "CSL-2", at=1, session="cursor/s2", branch="csl-1-parent")
        result = self.evaluate(10, [claim("c1", "CSL-2", U1, branch="csl-2-work"), parent])
        self.assertEqual(codes(result), ["overlapping_claim"])

    def test_two_issues_on_one_branch_across_sessions_overlap(self):
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, session="cursor/s2")])
        self.assertEqual(codes(result), ["overlapping_claim"])

    def test_writes_by_the_later_overlapping_claim_are_flagged(self):
        writes = [{"kind": "push", "issue": "CSL-4", "claim": U2, "at": ts(5)},
                  {"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(6)}]
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, session="cursor/s2")], writes)
        self.assertEqual(codes(result), ["overlapping_claim", "write_while_overlapping"])

    def test_withdrawing_at_read_back_is_clean(self):
        withdrawn = claim("c2", "CSL-4", U2, at=1, session="cursor/s2", confirm_after=None,
                          more_edits=[{"at": ts(2.5), "body": claim_body(U2, "cursor/s2",
                                                                          extra=(f"withdrawn: superseded by {U1}",))}])
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), withdrawn])
        self.assertEqual(codes(result), [])

    def test_not_judged_before_the_read_back_window_ends(self):
        fresh = claim("c2", "CSL-4", U2, at=9.5, session="cursor/s2", confirm_after=None)
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), fresh])
        self.assertEqual(codes(result), [])

    def test_other_repositories_are_independent(self):
        elsewhere = claim("c2", "CSL-4", U2, at=1, session="cursor/s2", extra=("repo: other-repo",))
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), elsewhere])
        self.assertEqual(codes(result), [])

    def test_different_issues_and_branches_do_not_overlap(self):
        result = self.evaluate(10, [claim("c1", "CSL-1", U1),
                                    claim("c2", "CSL-4", U2, at=1, session="cursor/s2", branch="csl-4-work")])
        self.assertEqual(codes(result), [])

    def test_a_claim_released_before_the_overlap_does_not_block(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(3, U1, "released")])
        later = claim("c2", "CSL-4", U2, at=5, session="cursor/s2")
        writes = [{"kind": "push", "issue": "CSL-4", "claim": U2, "at": ts(8)}]
        self.assertEqual(codes(self.evaluate(10, [released, release_record("r1", "CSL-1", U1, 3), later], writes)), [])

    def test_an_expired_unreleased_claim_still_blocks(self):
        # As on one issue, an expired claim is not bypassed without a release,
        # revoke or takeover.
        later = claim("c2", "CSL-4", U2, at=200, session="cursor/s2")
        result = self.evaluate(210, [claim("c1", "CSL-1", U1), later])
        self.assertEqual(codes(result), ["overlapping_claim"])

    def test_an_expired_claim_released_later_stops_blocking(self):
        lapsed = claim("c1", "CSL-1", U1, more_edits=[edit(190, U1, "released")])
        later = claim("c2", "CSL-4", U2, at=200, session="cursor/s2")
        result = self.evaluate(210, [lapsed, release_record("r1", "CSL-1", U1, 190), later])
        self.assertEqual(codes(result), [])

    def test_an_overlapping_claim_is_not_published_as_winner(self):
        # Devin 4341055198: the loser stays live (it has not withdrawn) but
        # cannot win its own issue while the earlier claim blocks it.
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, session="cursor/s2")])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], None)
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["live_claims"], [U2])
        self.assertFalse(result["issues"]["CSL-4"]["claimable"])

    def test_a_late_confirmation_after_the_blocker_releases_is_still_a_loss(self):
        # Devin 4341055382: the loss is settled at the read-back deadline, as
        # in a race on one issue; the blocker releasing afterwards does not
        # erase it. Once released, the standing loser can win, as on one issue.
        released = claim("c1", "CSL-1", U1, more_edits=[edit(3, U1, "released")])
        late = claim("c2", "CSL-4", U2, at=1, session="cursor/s2", confirm_after=180)
        result = self.evaluate(10, [released, release_record("r1", "CSL-1", U1, 3), late])
        self.assertEqual(codes(result), ["overlapping_claim"])
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], U2)

    def test_a_blocker_released_within_the_read_back_window_is_no_loss(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(1.5, U1, "released")])
        later = claim("c2", "CSL-4", U2, at=1, session="cursor/s2", confirm_after=180)
        writes = [{"kind": "push", "issue": "CSL-4", "claim": U2, "at": ts(5)}]
        self.assertEqual(codes(self.evaluate(10, [released, release_record("r1", "CSL-1", U1, 1.5), later], writes)), [])

    def test_a_confirmation_before_the_deadline_is_checked_at_its_own_time(self):
        early = claim("c2", "CSL-4", U2, at=1, session="cursor/s2", confirm_after=30)
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), early])
        self.assertEqual(codes(result), ["confirmed_too_early", "overlapping_claim"])

    def test_a_blocked_earliest_claim_still_wins_its_issue_race(self):
        # Devin 4341276529 / Samir 5605467453: C blocks A by branch; B is a
        # later claim on A's issue. B loses the race to A and must withdraw,
        # and nobody holds CSL-4 while C blocks A.
        c = claim("c1", "CSL-1", U1)
        a = claim("c2", "CSL-4", U2, at=1, session="cursor/s2")
        b = claim("c3", "CSL-4", U3, at=1.5, session="grok/s3", branch="csl-4-other")
        writes = [{"kind": "push", "issue": "CSL-4", "claim": U3, "at": ts(5)}]
        result = self.evaluate(10, [c, a, b], writes)
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn", "overlapping_claim",
                                         "write_by_non_winner"])
        found = {(v["code"], v["claim"]) for v in result["violations"]}
        self.assertIn(("loser_confirmed", U3), found)
        self.assertIn(("overlapping_claim", U2), found)
        self.assertEqual(result["issues"]["CSL-4"]["claims"]["recursion"]["winner"], None)

    def test_a_parent_scoped_claim_alone_holds_the_child(self):
        # Codex 4343468463 / Samir 5608007390: with only the parent claim live,
        # its scoped child is held by it and is not claimable.
        result = self.evaluate(10, [scoped("c1", "CSL-1", U1, "CSL-2")])
        self.assertEqual(codes(result), [])
        child = result["issues"]["CSL-2"]
        self.assertEqual(child["claims"]["recursion"], {"live_claims": [U1], "winner": U1, "must_reread": False})
        self.assertFalse(child["claimable"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)

    def test_a_released_parent_claim_no_longer_covers_the_child(self):
        released = scoped("c1", "CSL-1", U1, "CSL-2", more_edits=[edit(5, U1, "released")])
        result = self.evaluate(10, [released, release_record("r1", "CSL-1", U1, 5)])
        self.assertEqual(codes(result), [])
        self.assertNotIn("recursion", result["issues"]["CSL-2"]["claims"])
        self.assertTrue(result["issues"]["CSL-2"]["claimable"])

    def test_an_expired_unreleased_parent_claim_keeps_the_child_unclaimable(self):
        # Codex 4343611603: the expired parent still blocks a child claim, so
        # the child is not claimable; it is not listed as live or as holder.
        result = self.evaluate(210, [scoped("c1", "CSL-1", U1, "CSL-2")])
        self.assertEqual(codes(result), [])
        self.assertNotIn("recursion", result["issues"]["CSL-2"]["claims"])
        self.assertFalse(result["issues"]["CSL-2"]["claimable"])
        released = scoped("c1", "CSL-1", U1, "CSL-2", more_edits=[edit(190, U1, "released")])
        result = self.evaluate(210, [released, release_record("r1", "CSL-1", U1, 190)])
        self.assertEqual(codes(result), [])
        self.assertTrue(result["issues"]["CSL-2"]["claimable"])

    def test_a_later_child_claim_does_not_displace_the_covering_parent(self):
        child = claim("c2", "CSL-2", U2, at=1, session="cursor/s2", branch="csl-2-work")
        result = self.evaluate(10, [scoped("c1", "CSL-1", U1, "CSL-2"), child])
        self.assertEqual(codes(result), ["overlapping_claim"])
        self.assertEqual(result["issues"]["CSL-2"]["claims"]["recursion"]["winner"], U1)
        self.assertEqual(result["issues"]["CSL-2"]["claims"]["recursion"]["live_claims"], [U1, U2])

    def test_same_issue_races_keep_their_own_codes(self):
        result = self.evaluate(10, [claim("c1", "CSL-1", U1), claim("c2", "CSL-1", U2, at=1, session="cursor/s2")])
        self.assertEqual(codes(result), ["loser_confirmed", "loser_not_withdrawn"])


class LapsedClaimResumeTests(unittest.TestCase):
    """S31: session resumes after its claim lapsed (CSL-379). A renewal edit after lease plus grace is reported
    and does not revive the claim; the session releases it on lease expiry and takes a fresh claim."""

    def push(self, uuid, at):
        return {"kind": "push", "issue": "CSL-1", "claim": uuid, "at": ts(at)}

    def test_renewal_after_expiry_is_reported_and_does_not_revive(self):
        lapsed = claim("c1", "CSL-1", U1, more_edits=[edit(600, U1, f"heartbeat-at: {ts(600)}")])
        result = sim.evaluate(snapshot(610, [issue("CSL-1")], [lapsed], writes=[self.push(U1, 605)]))
        state = result["issues"]["CSL-1"]
        self.assertEqual(state["claims"]["recursion"]["live_claims"], [])
        self.assertIsNone(state["claims"]["recursion"]["winner"])
        self.assertTrue(state["claimable"])
        self.assertEqual(codes(result), ["renewed_after_expiry", "write_after_expiry"])
        renewed = [v for v in result["violations"] if v["code"] == "renewed_after_expiry"]
        self.assertEqual([(v["claim"], v["detail"]) for v in renewed], [(U1, "2026-10-20T20:00:00+00:00")])

    def test_lapsed_claim_still_needs_a_fresh_claim_or_takeover(self):
        lapsed = claim("c1", "CSL-1", U1, more_edits=[edit(600, U1, f"heartbeat-at: {ts(600)}")])
        fresh = claim("c2", "CSL-1", U2, at=605, session="cursor/s2")
        result = sim.evaluate(snapshot(610, [issue("CSL-1")], [lapsed, fresh]))
        self.assertEqual(codes(result), ["claim_without_takeover", "renewed_after_expiry"])
        taker = claim("c2", "CSL-1", U2, at=605, session="cursor/s2", extra=(f"takeover of {U1}",))
        result = sim.evaluate(snapshot(625, [issue("CSL-1")], [lapsed, taker]))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(codes(result), ["renewed_after_expiry"])

    def test_renewal_before_expiry_is_fine(self):
        for at in (100, 140):  # inside the lease, and late but inside the grace
            with self.subTest(at=at):
                renewed = claim("c1", "CSL-1", U1, more_edits=[edit(at, U1, f"heartbeat-at: {ts(at)}")])
                result = sim.evaluate(snapshot(at + 100, [issue("CSL-1")], [renewed],
                                               writes=[self.push(U1, at + 90)]))
                self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U1)
                self.assertEqual(codes(result), [])

    def test_release_on_lease_expiry_then_fresh_claim_is_clean(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(600, U1, "released")])
        fresh = claim("c2", "CSL-1", U2, at=601)
        record = release_record("r1", "CSL-1", U1, 600, reason="lease expiry")
        result = sim.evaluate(snapshot(610, [issue("CSL-1")], [released, record, fresh], writes=[self.push(U2, 605)]))
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["winner"], U2)
        self.assertEqual(codes(result), [])

    def test_heartbeat_at_must_be_the_edit_time(self):
        cases = {
            "stale value": (ts(40), ["heartbeat_time_mismatch"]),
            "within tolerance": (ts(100, -30), []),
            "not a timestamp": ("soon", []),
            "malformed timestamp": ("2026-10-20Tsoon", []),
            "out of range in UTC": ("0001-01-01T00:00:00+01:00", []),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(case=name):
                renewed = claim("c1", "CSL-1", U1, more_edits=[edit(100, U1, f"heartbeat-at: {value}"),
                                                               edit(110, U1, f"heartbeat-at: {value}", "released")])
                record = release_record("r1", "CSL-1", U1, 110)
                self.assertEqual(codes(sim.evaluate(snapshot(120, [issue("CSL-1")], [renewed, record]))), expected)

    def test_late_renewal_with_a_stale_heartbeat_reports_both(self):
        lapsed = claim("c1", "CSL-1", U1, more_edits=[edit(600, U1, f"heartbeat-at: {ts(60)}")])
        result = sim.evaluate(snapshot(610, [issue("CSL-1")], [lapsed]))
        self.assertEqual(codes(result), ["heartbeat_time_mismatch", "renewed_after_expiry"])



class ReleaseRecordTests(unittest.TestCase):
    """A released claim needs a `release v1` record (CSL-445, receipts 0d05a0b0 and 12051696)."""

    def released(self, *extra, **kwargs):
        return claim("c1", "CSL-1", U1, more_edits=[edit(30, U1, "released", *extra)], **kwargs)

    def violations(self, *comments, now=50, **rest):
        issues = [issue("CSL-1"), issue("CSL-4")]
        result = sim.evaluate(snapshot(now, issues, [self.released(), *comments], **rest))
        return [(v["code"], v["claim"], v["detail"]) for v in result["violations"]]

    def missing(self, detail):
        return [("release_without_record", U1, detail)]

    def record_with(self, *lines, at=30, session="claude/s1"):
        return comment("r1", "CSL-1", at, "\n".join([f"Agent ({session}, for Lucas): release v1", f"claim: {U1}", *lines]))

    def test_release_without_record(self):
        self.assertEqual(self.violations(), self.missing("no record"))

    def test_record_naming_another_claim(self):
        self.assertEqual(self.violations(release_record("r1", "CSL-1", U2, 31)), self.missing("no record"))

    def test_record_timing(self):
        before = release_record("r1", "CSL-1", U1, 29)
        edited_later = dict(before, edits=[{"at": ts(35), "body": before["body"] + "\nnote: edited"}])
        cases = {
            "before the release": (before, self.missing("no record")),
            "at the release": (release_record("r1", "CSL-1", U1, 30), []),
            "after the release": (release_record("r1", "CSL-1", U1, 45), []),
            "posted before, edited after": (edited_later, self.missing("no record")),
        }
        for name, (record, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.violations(record), expected)

    def test_record_missing_fields(self):
        cases = {
            "two omitted": ({"omit": ("done", "files")}, "missing: done, files"),
            "whitespace only": ({"evidence": "   "}, "missing: evidence"),
            "empty": ({"open": ""}, "missing: open"),
            "all five": ({"omit": ("done", "evidence", "open", "next", "files")},
                         "missing: done, evidence, files, next, open"),
            "no claim line": ({"omit": ("claim",)}, "no record"),
        }
        for name, (fields, detail) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.violations(release_record("r1", "CSL-1", U1, 30, **fields)), self.missing(detail))

    def test_record_reason(self):
        valid = ("done", "handoff", "lease expiry", "abandoned", "decision needed (CSL-5)", "done (recursion part)",
                 "lease expiry (handoff to a fresh claim from this same session)", "decision needed (CSL-5) (waiting)")
        invalid = ("decision needed", "decision needed (CSL-)", "finished", "", "Done", "done.", "done (a) (b)",
                   "done ()", "done(recursion part)", "done (recursion part", "done\nlater text")
        for reason in valid:
            with self.subTest(reason=reason):
                self.assertEqual(self.violations(release_record("r1", "CSL-1", U1, 30, reason=reason)), [])
        for reason in invalid:
            with self.subTest(reason=reason):
                self.assertEqual(self.violations(release_record("r1", "CSL-1", U1, 30, reason=reason)),
                                 self.missing("invalid reason"))
        both = release_record("r1", "CSL-1", U1, 30, reason="", omit=("next",))
        self.assertEqual(self.violations(both), self.missing("missing: next; invalid reason"))

    def test_field_names_may_carry_one_note(self):
        rest = ["reason: done", "done: x", "evidence: x", "next: x", "files: x"]
        self.assertEqual(self.violations(self.record_with(*rest, "open (lessons for D12, CSL-424 and CSL-425): x")), [])
        for line in ("open (a) (b): x", "open (): x", "open(lessons): x", "Open: x", "- open: x", "**open:** x",
                     "**open**: x", "open : x", " open: x", "opened: x"):
            with self.subTest(line=line):
                self.assertEqual(self.violations(self.record_with(*rest, line)), self.missing("missing: open"))

    def test_values_continue_to_the_next_key_line(self):
        cases = {
            "bullets under a key": (["evidence:", "- `linux` passed", "- review 123"], []),
            "unknown keys are value text": (["evidence:", "pr: recursion#1"], []),
            "blank line then the next key": (["evidence:", ""], self.missing("missing: evidence")),
        }
        for name, (lines, expected) in cases.items():
            with self.subTest(case=name):
                record = self.record_with("reason: done", "done: x", *lines, "open: x", "next: x", "files: x")
                self.assertEqual(self.violations(record), expected)

    def test_first_occurrence_of_a_key_counts(self):
        rest = ["reason: done", "evidence: x", "open: x", "next: x", "files: x"]
        self.assertEqual(self.violations(self.record_with("done:", *rest, "done: later")), self.missing("missing: done"))
        self.assertEqual(self.violations(self.record_with("done: x", *rest, "done:")), [])
        self.assertEqual(self.violations(self.record_with("reason: finished", "done: x", *rest[1:], "reason: done")),
                         self.missing("invalid reason"))

    def test_release_on_lease_expiry_needs_a_record(self):
        lapsed = claim("c1", "CSL-1", U1, more_edits=[edit(600, U1, "released")])
        record = release_record("r1", "CSL-1", U1, 600, reason="lease expiry")
        for name, comments, expected in (("no record", [lapsed], ["release_without_record"]),
                                         ("record", [lapsed, record], [])):
            with self.subTest(case=name):
                self.assertEqual(codes(sim.evaluate(snapshot(610, [issue("CSL-1")], comments))), expected)

    def test_withdrawal_needs_no_record(self):
        cases = {
            "withdrawn": ([edit(30, U1, "withdrawn: not needed")], []),
            "withdrawn, then released": ([edit(20, U1, "withdrawn: x"), edit(30, U1, "withdrawn: x", "released")], []),
            "released, then withdrawn": ([edit(30, U1, "released"), edit(40, U1, "released", "withdrawn: x")],
                                         ["release_without_record"]),
        }
        for name, (edits, expected) in cases.items():
            with self.subTest(case=name):
                comments = [claim("c1", "CSL-1", U1, more_edits=edits)]
                self.assertEqual(codes(sim.evaluate(snapshot(50, [issue("CSL-1")], comments))), expected)

    def test_one_valid_record_is_enough(self):
        incomplete = release_record("r1", "CSL-1", U1, 30, omit=("done",))
        self.assertEqual(self.violations(incomplete, release_record("r2", "CSL-1", U1, 31)), [])

    def test_the_most_complete_record_is_described(self):
        worse = release_record("r1", "CSL-1", U1, 30, omit=("done", "files"))
        better = release_record("r2", "CSL-1", U1, 40, omit=("next",))
        self.assertEqual(self.violations(worse, better), self.missing("missing: next"))
        self.assertEqual(self.violations(better, worse), self.missing("missing: next"))
        # On a tie the earlier record is described, whatever the comment order.
        tie = release_record("r3", "CSL-1", U1, 35, omit=("done",))
        self.assertEqual(self.violations(better, tie), self.missing("missing: done"))
        self.assertEqual(self.violations(tie, better), self.missing("missing: done"))
        # Time decides a tie, not the names of the missing fields (Codex audit of 7e02948).
        early = release_record("r3", "CSL-1", U1, 31, omit=("next",))
        late = release_record("r4", "CSL-1", U1, 40, omit=("done",))
        self.assertEqual(self.violations(early, late), self.missing("missing: next"))
        self.assertEqual(self.violations(late, early), self.missing("missing: next"))
        bad_reason = release_record("r4", "CSL-1", U1, 40, reason="finished")
        self.assertEqual(self.violations(early, bad_reason), self.missing("missing: next"))
        self.assertEqual(self.violations(bad_reason, early), self.missing("missing: next"))

    def test_record_on_another_issue(self):
        self.assertEqual(self.violations(release_record("r1", "CSL-4", U1, 30)), self.missing("no record"))

    def test_record_from_another_session(self):
        self.assertEqual(self.violations(release_record("r1", "CSL-1", U1, 30, session="cursor/s2")), [])

    def test_record_needs_a_complete_agent_prefix(self):
        fields = [f"{key}: x" for key in ("done", "evidence", "open", "next", "files")]
        bodies = {
            "no session": "Agent (claude, for Lucas): release v1",
            "no developer": "Agent (claude/s1): release v1",
            "no prefix": "release v1",
        }
        for name, first in bodies.items():
            with self.subTest(case=name):
                body = "\n".join([first, f"claim: {U1}", "reason: done", *fields])
                self.assertEqual(self.violations(comment("r1", "CSL-1", 30, body)), self.missing("no record"))

    def test_quoted_records_do_not_count(self):
        quoted = release_record("x", "CSL-1", U1, 0)["body"]
        cases = {
            "agent comment quoting a record": "Agent (claude/s1, for Lucas): the release v1 record would be\n```\n"
                                              + quoted + "\n```",
            "human comment quoting a record": "Here is the record:\n" + quoted,
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.violations(comment("r1", "CSL-1", 30, body)), self.missing("no record"))

    def test_header_must_be_on_the_posted_first_line(self):
        record = release_record("r1", "CSL-1", U1, 30)
        promoted = comment("r1", "CSL-1", 30, "Agent (claude/s1, for Lucas): notes")
        promoted["edits"] = [{"at": ts(35), "body": record["body"]}]
        self.assertEqual(self.violations(promoted), self.missing("no record"))
        completed = release_record("r1", "CSL-1", U1, 30, omit=("files",))
        completed["edits"] = [{"at": ts(35), "body": record["body"]}]
        self.assertEqual(self.violations(completed), [])

    def test_deleted_record(self):
        for name, deleted_at in (("undated", None), ("dated", ts(40))):
            with self.subTest(case=name):
                record = release_record("r1", "CSL-1", U1, 30)
                record.update(deleted=True, deleted_at=deleted_at)
                self.assertEqual(self.violations(record), self.missing("no record"))

    def test_record_edited_to_drop_a_field(self):
        record = release_record("r1", "CSL-1", U1, 30)
        dropped = release_record("r1", "CSL-1", U1, 30, omit=("evidence",))
        record["edits"] = [{"at": ts(35), "body": dropped["body"]}]
        self.assertEqual(self.violations(record), self.missing("missing: evidence"))

    def test_revoke_and_release(self):
        cases = {
            "revoked before the release": (25, []),
            "revoked at the release": (30, []),
            "revoked after the release": (35, self.missing("no record")),
        }
        for name, (at, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.violations(comment("h1", "CSL-1", at, f"REVOKE CLAIM {U1}")), expected)

    def test_released_then_deleted_claim(self):
        for name, deleted_at, expected in (("dated deletion", ts(40), ["release_without_record"]),
                                           ("undated deletion", None, [])):
            with self.subTest(case=name):
                deleted = self.released()
                deleted.update(deleted=True, deleted_at=deleted_at)
                self.assertEqual(codes(sim.evaluate(snapshot(50, [issue("CSL-1")], [deleted]))), expected)

    def test_developer_approved_comment_is_not_a_record(self):
        record = release_record("r1", "CSL-1", U1, 30)
        record["body"] += f"\ndeveloper-approved: Lucas, exact text confirmed {ts(29)}, session s1"
        self.assertEqual(self.violations(record), self.missing("no record"))

    def test_record_and_run_still_in_progress(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(40)}]
        self.assertEqual(self.violations(runs=runs), [("release_before_writers_stopped", U1, "run-1"),
                                                      ("release_without_record", U1, "no record")])

if __name__ == "__main__":
    unittest.main()
