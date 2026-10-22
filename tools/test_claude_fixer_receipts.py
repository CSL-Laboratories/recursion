#!/usr/bin/env python3
"""Provider-free controls for explicit fixer receipts and bounded attempts."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("receipts", ROOT / "scripts/ci/claude_fixer_receipts.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
A, B, C = "a" * 40, "b" * 40, "c" * 40


def thread(body='<!-- devin-review-comment {"kind":"bug"} -->\nBug', root=1):
    return {"id": f"thread{root}", "isResolved": False, "isOutdated": False,
            "comments": {"nodes": [{"databaseId": root, "body": body,
                "author": {"login": "devin-ai-integration", "__typename": "Bot"}}]}}


def root_comments(threads):
    return [{"id": t["comments"]["nodes"][0]["databaseId"],
             "body": t["comments"]["nodes"][0]["body"],
             "user": actor("devin-ai-integration[bot]", m.DEVIN_ID)} for t in threads]


def findings(threads):
    return m.findings(threads, root_comments(threads))


def actor(login="claude[bot]", identity=m.CLAUDE_ID):
    return {"login": login, "type": "Bot", "id": identity}


def reply(data, user=None):
    return {"id": 900, "in_reply_to_id": 1, "created_at": "2026-10-16T01:00:00Z",
            "user": user or actor(), "body": m.marker("result", data)}


def prepare(items, comments=(), history=None, permission=lambda _: "write"):
    if history is None:
        history = []
    def record(body):
        history.append({"id": len(history) + 1, "body": body,
                        "user": actor("github-actions[bot]", m.ACTIONS_ID)})
    return m.prepare_observations(items, list(comments), history, permission, record)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(m.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.history = []
        self.items = prepare(findings([thread()]), history=self.history)
        self.receipt = {**self.items[0], "root": 1, "fingerprint": self.items[0]["fingerprint"],
                        "status": "addressed", "base": A, "commit": B}

    def pending(self, comments, compare=lambda base, tip: "ahead", manual=False):
        items = prepare(self.items, comments, self.history)
        return m.pending_findings(items, comments, C, compare, lambda user: "write", manual)

    def test_authenticated_abbreviated_push_receipt_cannot_claim_handled(self):
        # Actual #147 producer failure: the push existed, but its 7-char receipt
        # was invalid. Never weaken the consumer to accept ambiguous object IDs.
        short = self.receipt | {"commit": "d5194ed"}
        compared = []
        def compare(base, tip):
            compared.append((base, tip))
            return "ahead"
        self.assertEqual(len(self.pending([reply(short)], compare)), 1)
        self.assertEqual(compared, [])

    def test_codex_mediation_never_handles_or_reserves_claude_findings(self):
        codex = actor("chatgpt-codex-connector[bot]", 199175422)
        key = m.attempt_key(C, self.items)
        bodies = ["CODEX-GATE: MEDIATE", "CODEX-GATE: READY",
                  m.marker("result", self.receipt),
                  m.marker("result", self.receipt | {"status": "needs-human"}),
                  m.marker("attempt", {**key, "run": "1", "attempt": "1"})]
        for body in bodies:
            mediator = {"id": 810, "in_reply_to_id": 1, "user": codex,
                        "created_at": "2026-10-16T01:00:00Z", "body": body}
            self.assertEqual(len(self.pending([mediator])), 1)
            self.assertEqual(m.attempt_count([mediator], key), 0)

    def test_terminal_failure_audit_preserves_fresh_eligibility_and_budget(self):
        # Workflow-owned diagnostic notices are neither result receipts nor
        # reservations. Failed runs remain pending, not freshly authorized work.
        failure = {"id": 800, "in_reply_to_id": 1,
                   "created_at": "2026-10-16T01:00:00Z",
                   "user": actor("github-actions[bot]", m.ACTIONS_ID),
                   "body": "Claude fixer terminal audit: command-denied. No verified pushed fix."}
        self.assertEqual(len(self.pending([failure])), 1)
        key = m.attempt_key(C, self.items)
        attempts = [failure]
        for run in (1, 2, 3):
            attempts.append({"id": run, "user": actor("github-actions[bot]", m.ACTIONS_ID),
                             "body": m.marker("attempt", {**key, "run": str(run), "attempt": "1"})})
        self.assertEqual(m.attempt_count(attempts, key), 3)
        self.assertEqual(m.attempt_count(attempts, m.attempt_key(B, self.items)), 0)
        for flag in ("isResolved", "isOutdated"):
            root = thread()
            root[flag] = True
            self.assertEqual(findings([root]), [])

    def test_rejections_and_head_observations_require_trusted_provenance(self):
        import subprocess
        for status in (400, 401, 403, 404, 409, 422, 429):
            self.assertEqual(m.known_label_rejection(subprocess.CalledProcessError(1, "gh", stderr=f"(HTTP {status})")), status)
        for status in (408, 500, 503):
            self.assertIsNone(m.known_label_rejection(subprocess.CalledProcessError(1, "gh", stderr=f"(HTTP {status})")))
        self.assertIsNone(m.known_label_rejection(OSError("HTTP 403")))
        removal = {"id": 9, "event": "unlabeled", "label": {"name": "ci:full"}}
        observed = {"id": 100, "body": m.marker("ci-removal", {"head": A, "event": 9}),
                    "user": actor("github-actions[bot]", m.ACTIONS_ID)}
        self.assertEqual(m.ci_label_decision([removal], [observed], A), "preserve-human-removal")
        self.assertEqual(m.ci_label_decision([removal], [observed], B), "allow")
        self.assertEqual(m.ci_label_decision([removal], [observed | {"user": actor("other", 9)}], B), "preserve-human-removal")
        self.assertEqual(m.ci_label_decision([removal | {"id": 10}], [observed], B), "preserve-human-removal")

    def test_durable_intent_uncertainty_restart_and_manual_reconciliation(self):
        comments = []
        def post(repo, number, body):
            comments.append({"id": len(comments) + 1, "body": body, "created_at": "2026-10-16T06:00:00Z", "user": actor("github-actions[bot]", m.ACTIONS_ID)})
        intent = m.begin_ci_intent("a/b", 1, A, [], post)
        self.assertEqual(m.pending_ci_intent([], comments), intent["nonce"])
        late = {"id": 2, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": "writer"}}
        # Delayed visibility on a later invocation cannot adopt ambiguous PAT ownership.
        self.assertEqual(m.pending_ci_intent([late], comments), intent["nonce"])
        forged = {"id": 100, "body": m.marker("ci-owned", {"head": A, "event": 2, "nonce": intent["nonce"]}), "user": actor("other", 9)}
        self.assertEqual(m.pending_ci_intent([late], comments + [forged]), intent["nonce"])
        removed = late | {"id": 3, "event": "unlabeled", "created_at": "2026-10-16T06:00:01Z"}
        self.assertEqual(m.pending_ci_intent([late, removed | {"created_at": "2026-10-16T05:59:59Z"}], comments), intent["nonce"])
        self.assertEqual(m.pending_ci_intent([late, removed | {"created_at": "2026-10-16T06:00:00Z"}], comments), intent["nonce"])
        self.assertIsNone(m.pending_ci_intent([late, removed], comments))
        self.assertIsNone(m.pending_ci_intent([late, removed, late | {"id": 4}], comments))
        post("a/b", 1, m.marker("ci-invalidated", {"head": A, "event": 2, "nonce": intent["nonce"]}))
        rollback = removed | {"created_at": "2026-10-16T06:00:00Z", "actor": actor("github-actions[bot]", m.ACTIONS_ID)}
        self.assertIsNone(m.pending_ci_intent([late, rollback], comments))
        self.assertEqual(m.pending_ci_intent([late, rollback | {"actor": actor("other", 9)}], comments), intent["nonce"])
        with patch.object(m, "pages", side_effect=[[], [], [late]]) as fetch:
            m.record_ci_ownership("a/b", 1, A, 0, "writer", fetch, post, intent=intent)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])
        self.assertIsNone(m.pending_ci_intent([late], comments))

    def test_observation_proves_both_equal_second_orderings_and_replay(self):
        follow = {"id": 99, "in_reply_to_id": 1, "created_at": "2026-10-15T01:00:00Z",
                  "updated_at": "2026-10-16T01:00:00Z",
                  "user": actor("devin-ai-integration[bot]", m.DEVIN_ID), "body": "Still fails"}
        preflight = self.pending([follow])[0]
        receipt = reply(self.receipt | {"observation": preflight["observation"], "generation": preflight["generation"]}) | {"id": 100}
        # Edit was observed before preflight: equal-second receipt suppresses it.
        self.assertEqual(self.pending([receipt, follow]), [])
        # Same timestamp, changed body after preflight: retry regardless of ID.
        changed = follow | {"body": "Still fails differently"}
        pending = self.pending([receipt, changed])
        self.assertTrue(pending)
        self.assertEqual(m.attempt_key(C, pending), m.attempt_key(C, self.pending([receipt, changed, changed])))
        self.assertTrue(self.pending([receipt]))  # Deletion changes snapshot.
        self.assertTrue(self.pending([receipt, follow, changed | {"id": 101}]))
        for bad in ({}, {"v": 1}, {"v": True}, {"observation": "bad"}):
            data = self.receipt | {"observation": preflight["observation"], "generation": preflight["generation"]} | bad
            if not bad:
                data.pop("v")
            self.assertTrue(self.pending([reply(data), follow]))
        # Returning to the earlier content never revives that old receipt.
        self.assertTrue(self.pending([receipt, follow]))

    def test_permission_loss_preserves_unchanged_observed_classification(self):
        follow = {"id": 41, "in_reply_to_id": 1, "user": {"id": 12, "login": "writer", "type": "User"}, "body": "Correction"}
        for status in ("addressed", "needs-human"):
            history = []
            first = prepare(findings([thread()]), [follow], history)[0]
            receipt = reply({**self.receipt, **first, "status": status})
            denied = lambda _: "read"
            stable = prepare(findings([thread()]), [follow, receipt], history, denied)
            self.assertEqual(first, stable[0])
            self.assertEqual(m.pending_findings(stable, [follow, receipt], C, lambda b,t: "ahead", denied), [])
            edited = follow | {"body": "Changed without authorization"}
            changed = prepare(findings([thread()]), [edited, receipt], history, denied)
            self.assertEqual(changed[0]["observation"], m.observation_token([], denied))
            self.assertNotEqual(changed[0]["generation"], first["generation"])
            new = follow | {"id": 42}
            self.assertEqual(prepare(findings([thread()]), [new], [], denied)[0]["observation"], m.observation_token([], denied))

    def test_journal_aba_duplicates_conflicts_and_failed_persistence(self):
        follow = {"id": 41, "in_reply_to_id": 1, "user": actor("devin-ai-integration[bot]", m.DEVIN_ID), "body": "Correction"}
        for status in ("addressed", "needs-human"):
            history = []
            original = prepare(findings([thread()]), history=history)[0]
            receipt = reply({**self.receipt, **original, "status": status})
            changed = prepare(findings([thread()]), [follow, receipt], history)[0]
            reverted = prepare(findings([thread()]), [receipt], history)[0]
            self.assertEqual(reverted["observation"], original["observation"])
            self.assertNotEqual(reverted["generation"], original["generation"])
            self.assertTrue(m.pending_findings([reverted], [receipt], C, lambda b,t: "ahead", lambda _: "write"))
            self.assertEqual(reverted, prepare(findings([thread()]), [receipt], history)[0])
            # Identical concurrent transitions from one predecessor deduplicate.
            base = history[:1]
            left, right = list(base), list(base)
            litem = prepare(findings([thread()]), [follow], left)[0]
            ritem = prepare(findings([thread()]), [follow], right)[0]
            self.assertEqual(m.attempt_key(C, [litem]), m.attempt_key(C, [ritem]))
            merged = base + left[1:] + right[1:]
            self.assertEqual(litem, prepare(findings([thread()]), [follow], merged)[0])
            other = list(base)
            prepare(findings([thread()]), [follow | {"body": "different"}], other)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                prepare(findings([thread()]), [follow], merged + other[1:])
        with self.assertRaises(OSError):
            m.prepare_observations(findings([thread()]), [], [], lambda _: "write",
                lambda body: (_ for _ in ()).throw(OSError("cannot persist")))
        # A change never visible in any observation is not recoverable from current API state.
        self.assertEqual(prepare(findings([thread()]), [], list(base))[0], original)

    def test_observed_root_body_aba_advances_the_same_root_chain(self):
        history = []
        roots = findings([thread()])
        original = prepare(roots, history=history)[0]
        changed_roots = findings([thread('<!-- devin-review-comment {"kind":"bug"} --> Changed root')])
        changed = prepare(changed_roots, history=history)[0]
        restored = prepare(roots, history=history)[0]
        self.assertEqual(original["fingerprint"], restored["fingerprint"])
        self.assertNotEqual(original["generation"], restored["generation"])
        self.assertNotEqual(changed["generation"], restored["generation"])
        receipt = reply({**self.receipt, **original})
        self.assertTrue(m.pending_findings([restored], [receipt], C, lambda b,t: "ahead", lambda _: "write"))

    def test_journal_requires_authenticated_complete_history(self):
        follow = {"id": 41, "in_reply_to_id": 1, "user": {"id": 12, "login": "writer", "type": "User"}, "body": "Correction"}
        history = []
        prepare(findings([thread()]), history=history)
        current = prepare(findings([thread()]), [follow], history)[0]
        self.assertEqual(current, prepare(findings([thread()]), [follow], list(reversed(history)), lambda _: "read")[0])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            prepare(findings([thread()]), [follow], history[1:])
        forged = [c | {"user": actor("github-actions[bot]", 1)} for c in history]
        untrusted = prepare(findings([thread()]), [follow], forged, lambda _: "read")[0]
        self.assertNotEqual(current["observation"], untrusted["observation"])
        # Identity changes are new content requiring current permission.
        replaced = follow | {"user": {"id": 13, "login": "other", "type": "User"}}
        self.assertNotEqual(current["observation"], prepare(findings([thread()]), [replaced], history, lambda _: "read")[0]["observation"])

    def test_live_state_journals_ineligible_and_missing_roots_before_filtering(self):
        original_thread = thread()
        variants = [
            thread("metadata removed"), thread('<!-- devin-review-comment {broken} -->'),
            thread('<!-- devin-review-comment {"kind":"analysis"} -->'),
            original_thread | {"isResolved": True}, original_thread | {"isOutdated": True},
            None,
        ]
        for status in ("addressed", "needs-human"):
            for inactive in variants:
                with self.subTest(status=status, inactive=inactive):
                    history = []
                    original = prepare(findings([original_thread]), history=history)[0]
                    receipt = reply({**self.receipt, **original, "status": status})
                    def observe(current, rest_override=None):
                        threads = [] if current is None else [current]
                        data = {"data": {"repository": {"pullRequest": {"reviewThreads": {
                            "nodes": threads, "pageInfo": {"hasNextPage": False}}}}}}
                        rest = root_comments(threads) + ([receipt] if current is not None else []) if rest_override is None else rest_override
                        def record(body):
                            history.append({"id": len(history) + 1, "body": body,
                                "user": actor("github-actions[bot]", m.ACTIONS_ID)})
                        with patch.object(m.subprocess, "check_output", return_value=json.dumps(data)),                                 patch.object(m, "pages", return_value=rest),                                 patch.object(m, "api", return_value={"status": "ahead", "permission": "write"}):
                            return m.live_state("o/r", "1", C, history=history, record=record)
                    self.assertEqual(observe(inactive), ([], []))
                    self.assertEqual(len(history), 2)
                    active, unresolved = observe(original_thread)
                    self.assertTrue(active)
                    self.assertTrue(unresolved)
                    self.assertNotEqual(active[0]["generation"], original["generation"])
                    self.assertEqual(len(history), 3)
                    # Unchanged active replay does not keep consuming generations.
                    self.assertEqual(observe(original_thread), (active, unresolved))
                    self.assertEqual(len(history), 3)
                    # Contradictory views must not invent clean absence.
                    with self.assertRaises(ValueError):
                        observe(None, root_comments([original_thread]) + [receipt])
                    with self.assertRaises(ValueError):
                        observe(original_thread, [receipt])
                    wrong = root_comments([original_thread])
                    wrong[0]["body"] = "inconsistent"
                    with self.assertRaises(ValueError):
                        observe(original_thread, wrong + [receipt])
        # Untracked inactive roots are not enrolled or authorized by this repair.
        inactive = thread("not a bug")
        self.assertEqual(m.observation_roots([inactive], root_comments([inactive]), [], []), [])

    def test_cross_view_consistency_before_all_eligibility_filters(self):
        valid = thread()
        rest = root_comments([valid])
        other_author = {"login": "someone", "__typename": "User"}
        variants = [
            thread("metadata removed"), thread('<!-- devin-review-comment {broken} -->'),
            thread('<!-- devin-review-comment {"kind":"analysis"} -->'),
            valid | {"comments": {"nodes": [valid["comments"]["nodes"][0] | {"author": other_author}]}},
            valid | {"comments": {"nodes": [valid["comments"]["nodes"][0] | {"author": None}]}},
        ]
        for changed in variants:
            for flags in ({}, {"isResolved": True}, {"isOutdated": True}):
                for history in ([], self.history):
                    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
                        "nodes": [changed | flags], "pageInfo": {"hasNextPage": False}}}}}}
                    with self.subTest(changed=changed, flags=flags, tracked=bool(history)), \
                            patch.object(m.subprocess, "check_output", return_value=json.dumps(payload)), \
                            patch.object(m, "pages", return_value=rest), patch.object(m, "api") as api, \
                            patch.object(m, "post") as post:
                        with self.assertRaises(ValueError):
                            m.live_state("o/r", "1", C, history=history)
                        api.assert_not_called()
                        post.assert_not_called()
        for threads, comments in (([], rest), ([valid], []),
                ([valid], [rest[0] | {"in_reply_to_id": 2}]),
                ([valid], rest + [{"id": 2, "in_reply_to_id": 99}]),
                ([valid], rest + [{"id": 2, "in_reply_to_id": True}]),
                ([valid], rest + [{"id": 2, "in_reply_to_id": 0}]),
                ([valid], rest + [{"id": 2, "in_reply_to_id": 2}]),
                ([valid | {"comments": {"nodes": []}}], []),
                ([valid | {"isResolved": None}], rest),
                ([valid | {"isOutdated": 0}], rest)):
            with self.subTest(threads=threads, comments=comments), self.assertRaises(ValueError):
                m.review_snapshot(threads, comments)
        # Matching non-Devin and inactive roots are consistent, but never work.
        untrusted = valid | {"comments": {"nodes": [valid["comments"]["nodes"][0] | {"author": other_author}]}}
        user_rest = [rest[0] | {"user": {"login": "someone", "type": "User", "id": 15}}]
        self.assertEqual(m.findings([untrusted], user_rest), [])
        for inactive in (thread("malformed"), valid | {"isResolved": True}, valid | {"isOutdated": True}):
            self.assertEqual(m.findings([inactive], root_comments([inactive])), [])
        # Suffix normalization applies only to Bot identities.
        suffixed = valid | {"comments": {"nodes": [valid["comments"]["nodes"][0] | {
            "author": {"login": "devin-ai-integration[bot]", "__typename": "Bot"}}]}}
        self.assertTrue(m.findings([suffixed], rest))
        with self.assertRaises(ValueError):
            m.review_snapshot([untrusted], [user_rest[0] | {"user": user_rest[0]["user"] | {"login": "someone[bot]"}}])

    def test_duplicate_identity_validation_precedes_all_filters(self):
        root = thread()
        rest = root_comments([root])
        variants = [
            [root, root | {"id": "different-thread"}],
            [root, root | {"id": "different-thread", "isResolved": True}],
            [root, root | {"isOutdated": True}],
            [root, thread(root=2) | {"id": root["id"]}],
            [root, root | {"comments": {"nodes": []}}],
        ]
        for threads in variants:
            with self.subTest(threads=threads), self.assertRaises(ValueError):
                m.review_snapshot(threads, rest)
        # Identical pagination overlap is safe only for the same thread identity.
        self.assertEqual(m.review_snapshot([root, root], rest + rest), ([root], rest))
        self.assertEqual(m.findings([root, root], rest + rest), findings([root]))
        for changed in (rest[0] | {"body": "edited"}, rest[0] | {"in_reply_to_id": 2},
                        rest[0] | {"user": actor("impostor", 123)},
                        rest[0] | {"user": {**rest[0]["user"], "id": float(m.DEVIN_ID)}}, rest[0] | {"id": True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                m.review_snapshot([root], rest + [changed])
        with self.assertRaises(ValueError):
            m.observation_roots([root], rest, [], findings([root]) + [findings([root])[0] | {"thread": "other"}])

    def test_conflicting_event_ids_never_claim_or_remove_label(self):
        event = {"id": 10, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": "writer"}}
        self.assertEqual(m.label_event_id([event, event]), 10)
        for alternate in (event | {"event": "unlabeled"}, event | {"actor": {"login": "other"}},
                          event | {"label": {"name": "other"}}):
            events = [event, alternate]
            for decision in (lambda: m.label_event_id(events), lambda: m.ci_label_decision(events, []),
                             lambda: m.pending_ci_intent(events, [])):
                with self.assertRaises(ValueError):
                    decision()
            with patch.object(m, "post") as post, patch.object(m, "delete_label") as delete:
                with self.assertRaises(ValueError):
                    m.begin_ci_intent("o/r", "1", A, events, post)
                with self.assertRaises(ValueError):
                    m.record_ci_ownership("o/r", "1", A, 0, "writer", lambda _: events, post)
                with self.assertRaises(ValueError):
                    m.rollback_ci_event("o/r", "1", 10, lambda _: events, delete)
                post.assert_not_called()
                delete.assert_not_called()

    def test_conflicting_issue_comment_ids_do_not_settle_or_revoke(self):
        entry = {"id": 10, "user": actor("github-actions[bot]", m.ACTIONS_ID),
                 "body": m.marker("ci-owned", {"head": A, "event": 7})}
        event = {"id": 7, "event": "labeled", "label": {"name": "ci:full"}}
        for changed in (entry | {"body": "removed receipt"}, entry | {"user": actor("other", 1)}):
            comments = [entry, changed]
            with self.assertRaises(ValueError):
                m.pending_ci_intent([], comments)
            with self.assertRaises(ValueError):
                m.ci_label_decision([], comments)
            with patch.object(m, "post") as post, patch.object(m, "delete_label") as delete:
                with self.assertRaises(ValueError):
                    m.revoke_owned_ci("o/r", "1", A,
                        lambda path: comments if path.endswith("comments") else [event])
                post.assert_not_called()
                delete.assert_not_called()
        self.assertEqual(m.ci_label_decision([], [entry, entry]), "allow")

    def test_live_duplicate_snapshots_abort_before_persistence_or_permissions(self):
        root = thread()
        rest = root_comments([root])
        followup = {"id": 50, "in_reply_to_id": 1, "body": "Correction", "user": {"id": 10, "login": "writer", "type": "User"}}
        cases = [
            ([root, root | {"id": "other"}], rest, []),
            ([root, root | {"id": "other", "isResolved": True}], rest, []),
            ([root], rest + [followup, followup | {"body": ">"}], []),
            ([root], rest + [followup, followup | {"user": actor("other", 12)}], []),
            ([root], rest, [self.history[0], self.history[0] | {"body": "conflicting history"}]),
        ]
        for threads, comments, history in cases:
            payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": threads, "pageInfo": {"hasNextPage": False}}}}}}
            with self.subTest(threads=threads, comments=comments), \
                    patch.object(m.subprocess, "check_output", return_value=json.dumps(payload)), \
                    patch.object(m, "pages", return_value=comments), patch.object(m, "api") as api, \
                    patch.object(m, "post") as post:
                with self.assertRaises(ValueError):
                    m.live_state("o/r", "1", C, history=history)
                api.assert_not_called()
                post.assert_not_called()
        # Simulate overlap across GraphQL pages and REST pages: one journal only,
        # unchanged generation on replay, not a fresh attempt fingerprint.
        history = []
        first = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [root], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}}
        last = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [root], "pageInfo": {"hasNextPage": False}}}}}}
        def record(body):
            history.append({"id": len(history) + 1, "body": body,
                "user": actor("github-actions[bot]", m.ACTIONS_ID)})
        results = []
        for _ in range(2):
            with patch.object(m.subprocess, "check_output", side_effect=[json.dumps(first), json.dumps(last)]), \
                    patch.object(m, "pages", return_value=rest + rest):
                results.append(m.live_state("o/r", "1", C, history=history + history, record=record))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(history), 1)
        self.assertEqual(m.attempt_key(C, results[0][0]), m.attempt_key(C, results[1][0]))

    def test_inactive_state_tracks_body_and_flags_without_authorizing_work(self):
        history = []
        prepare(findings([thread()]), history=history)
        generations = []
        for inactive in (thread() | {"isResolved": True},
                         thread("different inactive body") | {"isResolved": True},
                         thread("different inactive body") | {"isOutdated": True}):
            self.assertEqual(findings([inactive]), [])
            states = m.observation_roots([inactive], root_comments([inactive]), history, [])
            generations.append(prepare(states, history=history)[0]["generation"])
        self.assertEqual(len(set(generations)), 3)

    def test_exact_human_confirmation_only_settles_specific_live_intent(self):
        intent = {"nonce": "a" * 32, "head": A, "after": 0}
        comments = [{"id": 1, "body": m.marker("ci-intent", intent), "user": actor("github-actions[bot]", m.ACTIONS_ID)}]
        command = "/claude-fixer reconcile-label " + intent["nonce"] + " " + B
        human = {"id": 2, "body": command, "user": {"login": "writer", "type": "User", "id": 12}}
        self.assertIsNone(m.pending_ci_intent([], comments + [human], head=B, permission=lambda _: "write"))
        self.assertEqual(m.ci_label_decision([], comments + [human], head=B), "allow")
        self.assertIsNone(m.pending_ci_intent([], comments + [human | {"body": command.replace(B, "b" * 64)}], head="b" * 64, permission=lambda _: "write"))
        for body in ("> " + command, command + "\n", command + "\nconfirmed", command.replace(B, A), command.replace("a" * 32, "b" * 32)):
            self.assertEqual(m.pending_ci_intent([], comments + [human | {"body": body}], head=B, permission=lambda _: "write"), intent["nonce"])
        for user, permission in ((actor("github-actions[bot]", m.ACTIONS_ID), "write"), (human["user"], "read")):
            self.assertEqual(m.pending_ci_intent([], comments + [human | {"user": user}], head=B, permission=lambda _: permission), intent["nonce"])
        self.assertEqual(m.pending_ci_intent([], comments + [human], head=C, permission=lambda _: "write"), intent["nonce"])

    def test_inline_indented_receipts_and_quoted_writer_correction(self):
        for body in ("Fixed. " + m.marker("result", self.receipt), "  " + m.marker("result", self.receipt)):
            self.assertEqual(self.pending([reply(self.receipt) | {"body": body}]), [])
        terminal = reply(self.receipt | {"status": "needs-human"})
        correction = {"id": 201, "in_reply_to_id": 1, "created_at": "2026-10-16T02:00:00Z",
                      "user": {"login": "writer", "type": "User", "id": 12},
                      "body": "> " + terminal["body"] + "\nThis still fails with empty input."}
        self.assertTrue(self.pending([terminal, correction]))
        quote_only = correction | {"body": "> " + terminal["body"]}
        self.assertTrue(self.pending([terminal, quote_only]))  # Observed correction cannot be forgotten.
        self.assertEqual(m.pending_findings(prepare(self.items, [terminal, quote_only]), [terminal, quote_only], C, lambda b,t: "ahead", lambda _: "write"), [])
        self.assertEqual(m.pending_findings(prepare(self.items, [terminal, correction], permission=lambda _: "read"), [terminal, correction], C,
            lambda b,t: "ahead", lambda u: "read"), [])

    def test_root_requires_rest_numeric_bot_identity(self):
        roots = root_comments([thread()])
        self.assertTrue(m.findings([thread()], roots))
        with self.assertRaises(ValueError):
            m.findings([thread()], [])
        for change in ({"id": 9}, {"type": "User"}, {"login": "fake[bot]"}):
            fake = [{**roots[0], "user": roots[0]["user"] | change}]
            with self.assertRaises(ValueError):
                m.findings([thread()], fake)
        with self.assertRaises(ValueError):
            m.findings([thread()], [roots[0] | {"body": "changed"}])
        for snapshot in ([], [roots[0] | {"body": "changed"}]):
            with self.assertRaises(ValueError):
                m.findings([thread()], snapshot)

    def test_late_older_root_survives_prior_push(self):
        old = thread(root=1)
        late = thread(root=2)
        resolved, outdated = thread(root=3), thread(root=4)
        resolved["isResolved"], outdated["isOutdated"] = True, True
        items = findings([old, late, resolved, outdated])
        pending = m.pending_findings(prepare(items), [reply(self.receipt)], B,
            lambda base, tip: "identical" if base == tip else "ahead", lambda user: "write")
        self.assertEqual([item["root"] for item in pending], [2])

    def test_metadata_fails_closed(self):
        for body in ("plain trusted text", '<!-- devin-review-comment broken -->',
                     '<!-- devin-review-comment {"kind":"analysis"} -->',
                     '<!-- devin-review-comment {"kind":"bug"} -->' * 2):
            self.assertEqual(findings([thread(body)]), [])
        for flag in ("isResolved", "isOutdated"):
            value = thread()
            value[flag] = True
            self.assertEqual(findings([value]), [])

    def test_addressed_requires_identity_root_fingerprint_and_ancestry(self):
        self.assertEqual(self.pending([reply(self.receipt)]), [])
        for change in ({"root": True}, {"root": 2}, {"fingerprint": "f" * 64}, {"commit": A}, {"commit": "bad"}):
            self.assertTrue(self.pending([reply(self.receipt | change)],
                compare=lambda base, tip: "identical" if base == tip else "ahead"))
        for user in (actor(identity=1), actor("other[bot]"), actor() | {"type": "User"}):
            self.assertTrue(self.pending([reply(self.receipt, user)]))
        self.assertTrue(self.pending([reply(self.receipt)], compare=lambda b, t: "diverged"))
        with self.assertRaises(OSError):
            self.pending([reply(self.receipt)], compare=lambda b, t: (_ for _ in ()).throw(OSError("API unavailable")))

    def test_prose_failure_no_push_and_changed_findings_retry(self):
        for body in ("Fixed in " + B, "Tests failed; no push", "Needs a human", ""):
            self.assertTrue(self.pending([reply(self.receipt) | {"body": body}]))
        self.assertTrue(self.pending([reply(self.receipt | {"status": "failed"})]))
        self.items = findings([thread('<!-- devin-review-comment {"kind":"bug"} -->\nChanged bug')])
        self.assertTrue(self.pending([reply(self.receipt)]))

    def test_explicit_handoff_manual_retry_and_substantive_followup(self):
        receipt = reply(self.receipt | {"status": "needs-human"})
        self.assertEqual(self.pending([receipt]), [])
        self.assertTrue(self.pending([receipt], manual=True))
        follow = {"id": 202, "in_reply_to_id": 1, "created_at": "2026-10-16T02:00:00Z",
                  "user": actor("devin-ai-integration[bot]", m.DEVIN_ID), "body": "This still fails for empty input"}
        pending = self.pending([receipt, follow])
        self.assertTrue(pending)
        self.assertIn("observation", pending[0])
        structured = follow | {"body": '<!-- devin-review-comment {"kind":"bug"} -->\nStill broken'}
        self.assertTrue(self.pending([receipt, structured]))
        analysis = follow | {"body": '<!-- devin-review-comment {"kind":"analysis"} -->\nStill reviewing'}
        self.assertTrue(self.pending([receipt, analysis]))  # Removing a previously observed bug remains new work.
        self.assertEqual(m.pending_findings(prepare(self.items, [receipt, analysis]), [receipt, analysis], C, lambda b,t: "ahead", lambda _: "write"), [])
        noise = follow | {"user": actor("other[bot]", 1)}
        self.assertTrue(self.pending([receipt, noise]))


class AttemptTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(m.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.history = []
        self.items = prepare(findings([thread()]), history=self.history)
        self.key = m.attempt_key(A, self.items)

    def attempts(self, count=3, user=None):
        return [{"user": user or actor("github-actions[bot]", m.ACTIONS_ID),
                 "body": m.marker("attempt", self.key | {"run": str(i + 1), "attempt": "1"})}
                for i in range(count)]

    def test_forged_duplicate_and_changed_key_controls(self):
        self.assertEqual(m.attempt_count(self.attempts(), self.key), 3)
        self.assertEqual(m.attempt_count(self.attempts() * 2, self.key), 3)
        self.assertEqual(m.attempt_count(self.attempts(user=actor("github-actions[bot]", 1)), self.key), 0)
        self.assertEqual(m.attempt_count(self.attempts(), m.attempt_key(B, self.items)), 0)
        self.assertEqual(m.attempt_count(self.attempts(), m.attempt_key(A, findings([thread(root=2)]))), 0)

    def runtime(self, count, manual=False, post_error=False, loop_count="0", clean=False, compare_error=None):
        threads = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [thread() | {"isResolved": True}] if clean else [thread()], "pageInfo": {"hasNextPage": False}}}}}}
        def api_response(path):
            if "/compare/" in path and compare_error:
                raise compare_error
            return {"state": "open", "head": {"sha": A, "repo": {"full_name": "o/r"}}}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            "REPO": "o/r", "PR_NUMBER": "1", "FIXER_HEAD_SHA": A,
            "LOOP_COMMIT_COUNT": loop_count, "MANUAL_RETRY": "true" if manual else "false", "GITHUB_OUTPUT": str(Path(d) / "out"),
            "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1"}), \
                patch.object(m.subprocess, "check_output", return_value=json.dumps(threads)), \
                patch.object(m, "pages", side_effect=[root_comments([thread()]) + ([reply({**self.items[0], "root": 1, "fingerprint": self.items[0]["fingerprint"], "base": A, "commit": B, "status": "addressed"})] if compare_error else []), self.history, self.attempts(count)]), \
                patch.object(m, "api", side_effect=api_response), \
                patch.object(m, "revoke_owned_ci"), \
                patch.object(m, "post", side_effect=OSError("write failed") if post_error else None) as post:
            if post_error:
                with self.assertRaises(OSError):
                    m.main()
                self.assertFalse((Path(d) / "out").exists())
                return "", post.call_count
            m.main()
            if clean:
                self.assertTrue(all("claude-fixer-observation:" in call.args[2] for call in post.call_args_list))
            return (Path(d) / "out").read_text(), post.call_count

    def test_clean_findings_before_commit_cap_and_manual_cannot_bypass(self):
        output, posts = self.runtime(0, loop_count="8", clean=True)
        self.assertIn("no-pending-findings", output)
        self.assertEqual(posts, 1)  # Persist inactive transition, never a cap or attempt.
        for manual in (False, True):
            output, posts = self.runtime(0, loop_count="8", manual=manual)
            self.assertIn("loop-commit-limit", output)
            self.assertEqual(posts, 1)

    def test_bad_receipt_compare_does_not_poison_manual_or_automatic_runs(self):
        import subprocess
        for status in (404, 422):
            for manual in (False, True):
                error = subprocess.CalledProcessError(1, "gh", stderr=f"gh: invalid (HTTP {status})")
                output, _ = self.runtime(0, manual=manual, compare_error=error)
                self.assertIn("skip=false", output)
        for status in (401, 403, 429, 500):
            with self.assertRaises(subprocess.CalledProcessError):
                self.runtime(0, compare_error=subprocess.CalledProcessError(1, "gh", stderr=f"gh: error (HTTP {status})"))
        with self.assertRaises(OSError):
            self.runtime(0, compare_error=OSError("network failure"))

    def test_third_attempt_runs_fourth_pauses_manual_retries(self):
        output, posts = self.runtime(2)
        self.assertIn("skip=false", output)
        self.assertEqual(posts, 1)
        output, posts = self.runtime(3)
        self.assertIn("skip=true", output)
        self.assertIn("attempt-limit", output)
        output, posts = self.runtime(3, manual=True)
        self.assertIn("skip=false", output)
        self.assertEqual(posts, 0)

    def test_failed_reservation_never_authorizes_model(self):
        self.runtime(0, post_error=True)

    def test_paginated_comments_and_partial_failure(self):
        with patch.object(m, "api", side_effect=[[{}] * 100, [{"id": 101}]]):
            self.assertEqual(len(m.pages("repos/o/r/pulls/1/comments")), 101)
        with patch.object(m, "api", side_effect=[[{}] * 100, OSError("page2 failed")]):
            with self.assertRaises(OSError):
                m.pages("repos/o/r/pulls/1/comments")


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(m.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.items = findings([thread()])
        self.pause = {"id": 10, "user": actor("github-actions[bot]", m.ACTIONS_ID),
                      "body": m.marker("paused", m.attempt_key(A, self.items))}
        self.pr = {"state": "open", "draft": False, "head": {"sha": A, "repo": {"full_name": "o/r"}}}

    def test_active_pause_changed_findings_resolved_handoff_and_forgery(self):
        with patch.object(m, "live_state", return_value=(self.items, self.items)) as live:
            self.assertTrue(m.readiness_blocked("o/r", "1", A, [self.pause], lambda p: self.pr))
            self.assertTrue(m.readiness_blocked("o/r", "1", A, [], lambda p: self.pr))
            live.return_value = (findings([thread(root=2)]), findings([thread(root=2)]))
            self.assertTrue(m.readiness_blocked("o/r", "1", A, [self.pause], lambda p: self.pr))
            live.return_value = ([], [])
            self.assertFalse(m.readiness_blocked("o/r", "1", A, [self.pause], lambda p: self.pr))
            live.return_value = ([], self.items)
            self.assertTrue(m.readiness_blocked("o/r", "1", A, [self.pause], lambda p: self.pr))
        self.assertTrue(m.readiness_blocked("o/r", "1", A, [], lambda p: self.pr | {"state": "closed"}))
        with self.assertRaises(OSError):
            m.readiness_blocked("o/r", "1", A, [], lambda p: (_ for _ in ()).throw(OSError("transport")))

    def test_third_attempt_exhaustion_blocks_before_pause_notice(self):
        attempts = [{"id": i+1, "user": actor("github-actions[bot]", m.ACTIONS_ID),
                     "body": m.marker("attempt", {**m.attempt_key(A, self.items), "run": str(i+1), "attempt": "1"})} for i in range(3)]
        self.assertEqual(m.latest_pause_id(attempts, A), 3)
        self.assertEqual(m.latest_pause_id(attempts + [attempts[0] | {"id": 20}], A), 3)
        self.assertEqual(m.latest_pause_id(attempts[:2], A), 0)
        with patch.object(m, "live_state", return_value=(self.items, self.items)):
            self.assertTrue(m.readiness_blocked("o/r", "1", A, attempts, lambda p: self.pr))
            self.assertTrue(m.readiness_blocked("o/r", "1", A, attempts[:2], lambda p: self.pr))

    def test_exact_label_event_ownership_preserves_human_override(self):
        event = {"id": 7, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": "writer"}}
        posts = []
        self.assertTrue(m.record_ci_ownership("o/r", "1", A, 0, "writer", lambda p: [event], lambda r,n,b: posts.append(b)))
        owned = {"id": 11, "user": actor("github-actions[bot]", m.ACTIONS_ID), "body": posts[0]}
        deletes = []
        read = lambda p: [owned] if p.endswith("comments") else [event]
        self.assertTrue(m.revoke_owned_ci("o/r", "1", A, read, lambda r,n: deletes.append(n), lambda *args: None))
        read = lambda p: [owned] if p.endswith("comments") else [event, event | {"id": 8, "actor": {"login": "human"}}]
        self.assertFalse(m.revoke_owned_ci("o/r", "1", A, read, lambda r,n: deletes.append(n), lambda *args: None))
        self.assertEqual(deletes, ["1"])
        self.assertFalse(m.record_ci_ownership("o/r", "1", A, 7, "writer", lambda p: [event], lambda r,n,b: posts.append(b)))
        with self.assertRaises(ValueError):
            m.record_ci_ownership("o/r", "1", A, 0, "writer", lambda p: [event | {"actor": {"login": "human"}}], lambda *args: None)
        with self.assertRaises(OSError):
            m.revoke_owned_ci("o/r", "1", A, lambda p: (_ for _ in ()).throw(OSError("page failure")))

    def test_event_lag_is_unknown_and_cross_head_ownership_revokes(self):
        with self.assertRaises(ValueError):
            m.record_ci_ownership("o/r", "1", A, 0, "writer", lambda p: [], lambda *args: self.fail("must not post"))
        event = {"id": 7, "event": "labeled", "label": {"name": "ci:full"}}
        owned = {"id": 100, "user": actor("github-actions[bot]", m.ACTIONS_ID), "body": m.marker("ci-owned", {"head": A, "event": 7})}
        deleted = []
        self.assertTrue(m.revoke_owned_ci("o/r", "1", B,
            lambda p: [owned] if p.endswith("comments") else [event], lambda *args: deleted.append(True), lambda *args: None))
        self.assertEqual(deleted, [True])

    def test_label_removal_classification_preserves_human_and_unknown(self):
        labeled = {"id": 7, "event": "labeled", "label": {"name": "ci:full"}}
        removed = {"id": 8, "event": "unlabeled", "label": {"name": "ci:full"},
                   "actor": actor("github-actions[bot]", m.ACTIONS_ID)}
        invalidated = {"id": 100, "user": actor("github-actions[bot]", m.ACTIONS_ID), "body": m.marker("ci-invalidated", {"head": A, "event": 7})}
        self.assertEqual(m.ci_label_decision([], []), "allow")
        self.assertEqual(m.ci_label_decision([labeled], []), "present")
        self.assertEqual(m.ci_label_decision([labeled, removed], [invalidated]), "allow")
        self.assertEqual(m.ci_label_decision([labeled, removed], []), "preserve-human-removal")
        for user in ({"login": "writer", "type": "User"}, {}, actor("github-actions[bot]", 1)):
            self.assertEqual(m.ci_label_decision([labeled, removed | {"actor": user}], [invalidated]), "preserve-human-removal")

    def test_live_pending_revokes_before_handoff_only_early_return(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                "REPO": "o/r", "PR_NUMBER": "1", "FIXER_HEAD_SHA": A,
                "MANUAL_RETRY": "false", "GITHUB_OUTPUT": str(Path(d)/"out")}), \
                patch.object(m, "live_state", return_value=([], self.items)), \
                patch.object(m, "api", return_value=self.pr), patch.object(m, "revoke_owned_ci") as revoke:
            m.main()
            self.assertIn("skip=true", (Path(d)/"out").read_text())
            revoke.assert_called_once_with("o/r", "1", A)

    def test_ready_check_requires_trusted_exact_pause_generation(self):
        checked = {"id": 12, "user": actor("github-actions[bot]", m.ACTIONS_ID),
                   "body": m.marker("ci-checked", {"head": A, "pause": 10})}
        self.assertEqual(m.latest_ready_check([checked], A, 10), 12)
        self.assertEqual(m.latest_ready_check([checked], A, 13), 0)
        self.assertEqual(m.latest_ready_check([checked | {"user": actor("github-actions[bot]", 1)}], A, 10), 0)


if __name__ == "__main__":
    unittest.main()
