#!/usr/bin/env python3
"""Scenario tests S1-S23 for the CSL-357 agent work protocol simulator (CSL-360)."""
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


def issue(iid, status="Todo", admitted=True, next_in_order=True, blocked_by=(), decision=None):
    return {
        "id": iid,
        "status": status,
        "admitted": admitted,
        "next_in_order": next_in_order,
        "blocked_by": list(blocked_by),
        "decision": decision,
    }


def decision(options=("A", "B"), pings=(1,), options_changed_at=(), deadline=None):
    return {
        "options": list(options),
        "pings": [ts(p) for p in pings],
        "options_changed_at": [ts(p) for p in options_changed_at],
        "deadline": ts(deadline) if deadline is not None else None,
    }


def snapshot(now, issues, comments, **rest):
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

    def test_s22_second_claim_in_same_repository_is_flagged(self):
        result = sim.evaluate(snapshot(10, [issue("CSL-1"), issue("CSL-4")], [
            claim("c1", "CSL-1", U1), claim("c2", "CSL-4", U2, at=1, branch="csl-4-work"),
        ]))
        self.assertEqual(codes(result), ["multiple_claims_per_session"])

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

    def test_acknowledgement_before_the_takeover_does_not_count(self):
        comments = [claim("c1", "CSL-1", U1), comment("h1", "CSL-1", 140, f"ACK TAKEOVER {U1}"), self.takeover()]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], comments, config={"coordinator_ack_required": True}))
        self.assertFalse(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_release_inside_the_wait_is_not_an_objection(self):
        original = claim("c1", "CSL-1", U1, more_edits=[edit(160, U1, "released")])
        result = sim.evaluate(snapshot(175, [issue("CSL-1")], [original, self.takeover()]))
        self.assertTrue(result["issues"]["CSL-1"]["takeovers"][U2]["allowed"])

    def test_takeover_refused_while_claimant_run_is_in_progress(self):
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(100), "ended_at": None}]
        result = sim.evaluate(snapshot(170, [issue("CSL-1")], [claim("c1", "CSL-1", U1), self.takeover()], runs=runs))
        self.assertEqual(result["issues"]["CSL-1"]["takeovers"][U2]["reason"], "claimant run in progress")

    def test_s8_write_after_expiry(self):
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(125)}]
        result = sim.evaluate(snapshot(130, [issue("CSL-1")], [claim("c1", "CSL-1", U1)], writes=writes))
        self.assertEqual(codes(result), ["write_after_expiry"])

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

    def test_deletion_time_bounds_the_claim(self):
        deleted = claim("c1", "CSL-1", U1, deleted=True)
        deleted["deleted_at"] = ts(30)
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(10)}]
        result = sim.evaluate(snapshot(40, [issue("CSL-1")], [deleted], writes=writes))
        self.assertEqual(codes(result), [])

    def test_s20_commit_names_deleted_claim(self):
        commits = [{"sha": "a9e", "branch": "csl-1-work", "at": ts(5), "author_kind": "agent", "claim": U1}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [claim("c1", "CSL-1", U1, deleted=True)], commits=commits))
        self.assertEqual(codes(result), ["commit_names_deleted_claim"])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])

    def test_s18_release_while_run_in_progress(self):
        released = claim("c1", "CSL-1", U1, more_edits=[edit(30, U1, "released")])
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(40)}]
        result = sim.evaluate(snapshot(50, [issue("CSL-1")], [released], runs=runs))
        self.assertEqual(codes(result), ["release_before_writers_stopped"])
        runs = [{"id": "run-1", "claim": U1, "started_at": ts(10), "ended_at": ts(20)}]
        result = sim.evaluate(snapshot(50, [issue("CSL-1")], [released], runs=runs))
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
        result = sim.evaluate(snapshot(60, issues, [work]))
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
            comment("r1", "CSL-11", 9, "Agent (claude/s1, for Lucas): recorded answer h1"),
            comment("r2", "CSL-12", 9, "Agent (claude/s1, for Lucas): recorded answer h1"),
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
        late = comment("r2", "CSL-12", 15, "Agent (claude/s1, for Lucas): recorded answer h1")
        result = sim.evaluate(snapshot(20, issues, [answer, records[0], late, work]))
        self.assertEqual(codes(result), ["decision_not_recorded"])

    def test_decision_record_edited_in_after_confirm_does_not_count(self):
        issues = [issue("CSL-1", blocked_by=["CSL-11"]), issue("CSL-11", decision=decision())]
        answer = comment("h1", "CSL-11", 5, "DECISION: B")
        record = comment("r1", "CSL-11", 9, "Agent (claude/s1, for Lucas): noted")
        record["edits"] = [{"at": ts(15), "body": "Agent (claude/s1, for Lucas): recorded answer h1"}]
        result = sim.evaluate(snapshot(20, issues, [answer, record, claim("c1", "CSL-1", U1, at=10)]))
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

    def test_hold_removed_by_edit_no_longer_holds(self):
        hold = comment("h1", "CSL-1", 5, "HOLD CSL-1")
        hold["edits"] = [{"at": ts(8), "body": "never mind"}]
        result = sim.evaluate(snapshot(10, [issue("CSL-1")], [hold]))
        self.assertFalse(result["issues"]["CSL-1"]["held"])

    def test_revoke_edited_in_later_is_not_backdated(self):
        note = comment("h1", "CSL-1", 1, "looks good")
        note["edits"] = [{"at": ts(20), "body": f"looks good\nREVOKE CLAIM {U1}"}]
        writes = [{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(10)}]
        result = sim.evaluate(snapshot(30, [issue("CSL-1")], [claim("c1", "CSL-1", U1), note], writes=writes))
        self.assertEqual(codes(result), [])
        self.assertEqual(result["issues"]["CSL-1"]["claims"]["recursion"]["live_claims"], [])

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

    def test_total_limit(self):
        result = self.run_actions([self.action(10), self.action(500)], self.approval(limits="total=1"))
        self.assertEqual(codes(result), ["sa_over_limit"])

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

    def test_events_after_the_snapshot_time(self):
        self.assert_rejected(lambda s: s["comments"].append(comment("c2", "CSL-1", 20, "later")))
        self.assert_rejected(lambda s: s.update(writes=[{"kind": "push", "issue": "CSL-1", "claim": U1, "at": ts(20)}]))

    def test_unknown_status(self):
        self.assert_rejected(lambda s: s["issues"][0].update(status="Blocked"))


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


if __name__ == "__main__":
    unittest.main()
