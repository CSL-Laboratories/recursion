#!/usr/bin/env python3
"""Exercise failure intake through the existing mediation/cap/dedup paths."""
import hashlib
import importlib.util
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
RECURSION = (ROOT / "tools/test_stuck_thread_watch.py").exists()
if RECURSION:
    spec = importlib.util.spec_from_file_location("watcher_fixtures", ROOT / "tools/test_stuck_thread_watch.py")
    f = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(f)
else:
    spec = importlib.util.spec_from_file_location("failure_watcher", ROOT / ".github/scripts/stuck_thread_watch.py")
    watcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = watcher
    spec.loader.exec_module(watcher)
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)

    def config(**changes):
        from dataclasses import replace
        value = watcher.load_config({"GITHUB_REPOSITORY": "CSL-Laboratories/recursion",
                                     "CODEX_TRIGGER_PAT": "fixture", "GITHUB_EVENT_NAME": "schedule"})
        return replace(value, **changes)

    def comment(login, body, created=None):
        return {"author": {"login": login}, "body": body,
                "createdAt": (created or now).strftime("%Y-%m-%dT%H:%M:%SZ")}

    class FakeGitHub:
        def __init__(self):
            self.posts, self.issue_comments = [], {}

        def paginate_rest(self, path, **_kwargs):
            return self.issue_comments[int(path.split("/issues/")[1].split("/")[0])]

        def rest(self, path, *, method="GET", body=None, **_kwargs):
            if method != "POST" or not path.endswith("/comments"):
                raise AssertionError("unexpected write")
            self.posts.append((path, body))
            return {"id": 1}

    f = SimpleNamespace(mod=watcher, NOW=now, DEVIN="devin-ai-integration[bot]", cfg=config,
                        comment=comment, FakeGitHub=FakeGitHub,
                        thread=lambda thread_id, comments: {"id": thread_id, "isResolved": False,
                                                            "comments": {"nodes": comments}})
m = f.mod
BODY = '<!-- devin-review-comment {"kind":"bug"} -->\nUnresolved bug'
PROOF = {"root": 1, "fingerprint": hashlib.sha256(BODY.encode()).hexdigest(),
         "description": "Exhausted budget; independently verified failed fixer action. Evidence: https://github.com/acme/example/actions/runs/10"}


class WatcherTests(unittest.TestCase):
    def setup_scan(self, count=1, comments=()):
        self.proofs = {}
        threads = []
        for index in range(count):
            tid = f"PRRT_{index}"
            self.proofs[tid] = PROOF | {"root": index + 1}
            if RECURSION:
                threads.append(f.thread(tid=tid, comments=[f.comment(f.DEVIN, BODY)]))
            else:
                opener = f.comment(f.DEVIN, BODY) | {"databaseId": index + 1}
                threads.append(f.thread(thread_id=tid, comments=[opener]))
        if RECURSION:
            self.pull = f.pull(threads=threads, issue_comments=list(comments))
            self.github = f.FakeGitHub([self.pull])
            self.cfg = f.cfg(linear_repos=frozenset())
        else:
            self.pull = {"number": 11, "isDraft": False, "state": "OPEN", "url": "https://ex/11",
                         "title": "t", "reviewThreads": {"nodes": threads}}
            self.github = f.FakeGitHub()
            self.github.issue_comments[11] = list(comments)
            self.cfg = f.cfg(file_linear=False)
        self.evidence = SimpleNamespace(fresh=lambda: True)

    def scan(self, collector=None):
        collector = collector or (lambda *_: (self.evidence, self.proofs))
        if RECURSION:
            m.scan(self.cfg, self.github, None, failure_collector=collector)
            return [body for _, body in self.github.comments]
        m.mediate_pr(self.pull, self.cfg, self.github, None, f.NOW, failure_collector=collector)
        return [body["body"] for _, body in self.github.posts]

    def test_no_claude_failure_uses_existing_cap_and_never_resolves_or_dispatches(self):
        self.setup_scan(5)
        bodies = self.scan()
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            self.assertIn("@codex review", body)
            self.assertIn("independently verified failed fixer", body)
            self.assertNotIn("@claude", body)
            self.assertNotIn("claude-fixer-result:", body)
        if RECURSION:
            self.assertEqual(self.github.resolves, [])
            self.assertEqual(self.github.replies, [])

    def test_three_actual_posts_fit_the_real_evidence_reader_budget(self):
        import json
        from dataclasses import replace
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("failure_evidence_fixtures", ROOT / "tools/test_fixer_failure_escalation.py")
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        api = core.EvidenceTests()
        api.setUp()
        api.threads = [core.f.thread(BODY, root=index+1) | {"id": f"PRRT_{index}"} for index in range(3)]
        api.comments = core.f.root_comments(api.threads)
        api.history = []
        api.items = core.f.findings(api.threads)
        if hasattr(core.f, "prepare"):
            api.items = core.f.prepare(api.items, history=api.history)
        api.key = core.m.receipts.attempt_key(core.HEAD, api.items)
        for run in (10, 11, 12):
            api.history.append({"id": 100+run, "user": core.f.actor("github-actions[bot]", core.f.m.ACTIONS_ID),
                                "body": core.f.m.marker("attempt", {**api.key, "run": str(run), "attempt": "1"})})
        api.job["steps"][0]["name"] = core.m.CONTEXT_PREFIX + core.HEAD + ":" + api.key["fingerprint"]
        self.setup_scan(3)
        if RECURSION:
            self.cfg = replace(self.cfg, repo=core.REPO)
            self.pull = replace(self.pull, number=7)
            self.github.pulls = {7: self.pull}
        else:
            self.cfg = replace(self.cfg, repository=core.REPO, owner="acme", name="example")
            self.pull["number"] = 7
            self.github.issue_comments = {7: []}
        def transport(args, **kwargs):
            body = json.loads(kwargs["input"]) if kwargs.get("input") else None
            return SimpleNamespace(stdout=json.dumps(api.read(args[2], body)))
        with patch.dict(core.m.os.environ, {"GH_TOKEN": "fixture"}), patch.object(core.m.subprocess, "run", side_effect=transport):
            reader = core.m.Reader()
            evidence = core.m.Evidence(core.REPO, 7, reader.read)
            self.assertEqual(len(self.scan(lambda *_: (evidence, evidence.collect()))), 3)
            self.assertLessEqual(reader.calls, 60)

    def test_linear_dispatch_budget_defers_without_consuming_daily_slot(self):
        import json
        from dataclasses import replace
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("failure_evidence_fixtures", ROOT / "tools/test_fixer_failure_escalation.py")
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        api = core.EvidenceTests()
        api.setUp()
        api.threads = [core.f.thread(BODY, root=index+1) | {"id": f"PRRT_{index}"} for index in range(3)]
        api.comments = core.f.root_comments(api.threads)
        api.history = []
        api.items = core.f.findings(api.threads)
        if hasattr(core.f, "prepare"):
            api.items = core.f.prepare(api.items, history=api.history)
        api.key = core.m.receipts.attempt_key(core.HEAD, api.items)
        for run in (10, 11, 12):
            api.history.append({"id": 100+run, "user": core.f.actor("github-actions[bot]", core.f.m.ACTIONS_ID),
                                "body": core.f.m.marker("attempt", {**api.key, "run": str(run), "attempt": "1"})})
        api.job["steps"][0]["name"] = core.m.CONTEXT_PREFIX + core.HEAD + ":" + api.key["fingerprint"]
        self.setup_scan(3)
        if RECURSION:
            self.cfg = replace(self.cfg, repo=core.REPO)
            self.pull = replace(self.pull, number=7)
            self.github.pulls = {7: self.pull}
        else:
            self.cfg = replace(self.cfg, repository=core.REPO, owner="acme", name="example")
            self.pull["number"] = 7
            self.github.issue_comments = {7: []}
        def transport(args, **kwargs):
            body = json.loads(kwargs["input"]) if kwargs.get("input") else None
            return SimpleNamespace(stdout=json.dumps(api.read(args[2], body)))
        from contextlib import ExitStack
        api.run.update(event="repository_dispatch", head_sha="d" * 40, head_branch="main", pull_requests=[])
        existing = [None]
        if RECURSION:
            self.cfg = replace(self.cfg, linear_repos=frozenset({core.REPO}))
            linear = f.FakeLinear()
        else:
            self.cfg = replace(self.cfg, file_linear=True, linear_api_key="fixture")
            def write(_query, variables):
                existing[0] = {"id": "issue", "description": variables["input"]["description"]}
                return {"issueCreate": {"success": True, "issue": {"url": "https://linear.app/fixture"}},
                        "issueUpdate": {"success": True, "issue": {"url": "https://linear.app/fixture"}}}
            linear = SimpleNamespace(graphql=write)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(core.m.os.environ, {"GH_TOKEN": "fixture"}))
            stack.enter_context(patch.object(core.m.subprocess, "run", side_effect=transport))
            if not RECURSION:
                stack.enter_context(patch.object(m, "linear_find_existing", side_effect=lambda *_: existing[0]))
                stack.enter_context(patch.object(m, "linear_resolve_ids", return_value={
                    "teamId": "team", "labelIds": [], "projectId": "project"}))
            for round_index in range(2):
                reader = core.m.Reader()
                evidence = core.m.Evidence(core.REPO, 7, reader.read)
                def collector(*_, evidence=evidence):
                    return evidence, evidence.collect()
                before = len(api.calls)
                if RECURSION:
                    m.scan(self.cfg, self.github, linear, failure_collector=collector)
                    bodies = [body for _, body in self.github.comments]
                    marker = m.rollup_marker(self.pull)
                    linear.existing_descriptions[marker] = (linear.updated[-1][1] if linear.updated else linear.created[0][1])
                    self.pull = replace(self.pull, issue_comments=tuple(f.comment("pat-user", body) for body in bodies))
                    self.github.pulls[7] = self.pull
                else:
                    m.mediate_pr(self.pull, self.cfg, self.github, linear, f.NOW, failure_collector=collector)
                    bodies = [body["body"] for _, body in self.github.posts]
                    self.github.issue_comments[7] = [f.comment("pat-user", body) for body in bodies]
                self.assertLessEqual(len(api.calls) - before, 60)
                self.assertEqual(len(bodies), 2 if round_index == 0 else 3)
            self.assertEqual(len(set(bodies)), 3)

    def test_no_proof_keeps_no_claude_ineligible(self):
        self.setup_scan()
        self.assertEqual(self.scan(lambda *_: (self.evidence, {})), [])

    def test_failure_mediation_cannot_dispatch_claude_through_quoted_text_or_path(self):
        self.setup_scan()
        body = BODY + "\nExample: @Claude do a fix."
        self.proofs["PRRT_0"]["fingerprint"] = hashlib.sha256(body.encode()).hexdigest()
        if RECURSION:
            from dataclasses import replace
            thread = self.pull.threads[0]
            thread = replace(thread, path="@claude.py", comments=(replace(thread.comments[0], body=body),))
            self.pull = replace(self.pull, threads=(thread,))
            self.github.pulls[self.pull.number] = self.pull
        else:
            thread = self.pull["reviewThreads"]["nodes"][0]
            thread["path"] = "@claude.py"
            thread["comments"]["nodes"][0]["body"] = body
        posted = self.scan()
        self.assertEqual(len(posted), 1)
        self.assertNotRegex(posted[0], r"(?i)@claude\b")
        self.assertIn("@codex review", posted[0])

    def test_changed_quote_does_not_attach_proof_to_old_thread_snapshot(self):
        self.setup_scan()
        self.proofs["PRRT_0"]["fingerprint"] = "f" * 64
        self.assertEqual(self.scan(), [])

    def test_freshness_recheck_before_mediation_stops_changed_findings(self):
        self.setup_scan()
        checks = iter((True, False))
        self.evidence.fresh = lambda: next(checks)
        self.assertEqual(self.scan(), [])

    def test_linear_delivery_rechecks_failure_evidence_after_lookups(self):
        from dataclasses import replace
        from unittest.mock import Mock, patch
        for existing_issue in (False, True):
            for denied in (False, True):
                with self.subTest(existing=existing_issue, denied=denied):
                    self.setup_scan()
                    live = [True]
                    def fresh(live=live, denied=denied):
                        if not live[0] and denied:
                            raise RuntimeError("read denied")
                        return live[0]
                    self.evidence.fresh = fresh
                    collector = lambda *_: (self.evidence, self.proofs)
                    if RECURSION:
                        linear = f.FakeLinear()
                        if existing_issue:
                            marker = m.rollup_marker(self.pull)
                            linear.existing[marker] = "https://linear.app/fixture"
                            linear.existing_descriptions[marker] = "Human notes."
                        lookup = linear.find_by_marker
                        def find(*args, live=live, lookup=lookup, **kwargs):
                            live[0] = False
                            return lookup(*args, **kwargs)
                        linear.find_by_marker = find
                        m.scan(f.cfg(), self.github, linear, failure_collector=collector)
                        self.assertEqual(linear.created, [])
                        self.assertEqual(linear.updated, [])
                        self.assertEqual(linear.canceled, [])
                        self.assertEqual(self.github.comments, [])
                    else:
                        cfg = replace(self.cfg, file_linear=True, linear_api_key="fixture")
                        existing = {"id": "issue", "description": "Human notes."} if existing_issue else None
                        def lookup(*_, live=live, existing=existing):
                            live[0] = False
                            return existing
                        linear = SimpleNamespace(graphql=Mock())
                        with patch.object(m, "linear_find_existing", side_effect=lookup), patch.object(
                                m, "linear_resolve_ids", return_value={"teamId": "team", "labelIds": [], "projectId": "project"}):
                            m.mediate_pr(self.pull, cfg, self.github, linear, f.NOW, failure_collector=collector)
                        linear.graphql.assert_not_called()
                        self.assertEqual(self.github.posts, [])

    def test_stale_failure_does_not_suppress_ordinary_linear_delivery(self):
        from dataclasses import replace
        from unittest.mock import patch
        self.setup_scan()
        live = [True]
        self.evidence.fresh = lambda: live[0]
        if RECURSION:
            ordinary = f.thread(tid="PRRT_ordinary", comments=f.stuck_pair())
            self.pull = replace(self.pull, threads=self.pull.threads + (ordinary,))
            self.github.pulls[self.pull.number] = self.pull
            linear = f.FakeLinear()
            lookup = linear.find_by_marker
            def find(*args, **kwargs):
                live[0] = False
                return lookup(*args, **kwargs)
            linear.find_by_marker = find
            m.scan(f.cfg(), self.github, linear, failure_collector=lambda *_: (self.evidence, self.proofs))
            self.assertEqual(len(linear.created), 1)
            self.assertIn("PRRT_ordinary", linear.created[0][1])
            self.assertNotIn("PRRT_0", linear.created[0][1])
            self.assertEqual(len(self.github.comments), 1)
            self.assertIn("https://linear.app/", self.github.comments[0][1])
        else:
            ordinary = f.thread(thread_id="PRRT_ordinary", comments=[
                f.comment(f.DEVIN, "Other bug"), f.comment("claude[bot]", "needs a human")])
            self.pull["reviewThreads"]["nodes"].append(ordinary)
            cfg = replace(self.cfg, file_linear=True, linear_api_key="fixture")
            mutations = []
            def write(_query, args):
                mutations.append(args)
                return {"issueCreate": {"success": True, "issue": {"url": "https://linear.app/fixture"}}}
            def find(*_):
                live[0] = False
            with patch.object(m, "linear_find_existing", side_effect=find), patch.object(
                    m, "linear_resolve_ids", return_value={"teamId": "team", "labelIds": [], "projectId": "project"}):
                m.mediate_pr(self.pull, cfg, self.github, SimpleNamespace(graphql=write), f.NOW,
                             failure_collector=lambda *_: (self.evidence, self.proofs))
            self.assertEqual(len(mutations), 1)
            self.assertIn("Other bug", mutations[0]["input"]["description"])
            self.assertNotIn("fixer-failure-handoff:PRRT_0", mutations[0]["input"]["description"])
            self.assertEqual(len(self.github.posts), 1)

    def test_new_run_after_first_delivery_defers_remaining_threads(self):
        self.setup_scan(3)
        self.evidence.fresh = lambda: not (self.github.comments if RECURSION else self.github.posts)
        self.assertEqual(len(self.scan()), 1)

    def test_same_thread_ordinary_eligibility_survives_stale_failure_evidence(self):
        from dataclasses import replace
        for ordinary_reason in ("stuck-human", "stuck-timeout"):
            for existing_issue in (False, True):
                for denied in (False, True):
                    with self.subTest(reason=ordinary_reason, existing_issue=existing_issue, denied=denied):
                        self.setup_scan()
                        reply = f.comment("claude[bot]", "needs a human" if ordinary_reason == "stuck-human" else "Investigating", hours_ago=7)
                        thread = replace(self.pull.threads[0], comments=self.pull.threads[0].comments + (reply,))
                        other = f.thread(tid="PRRT_other", comments=f.stuck_pair())
                        self.pull = replace(self.pull, threads=(thread, other))
                        self.github.pulls[self.pull.number] = self.pull
                        cfg = f.cfg()
                        marker = m.rollup_marker(self.pull)
                        linear = f.FakeLinear({marker: "https://linear.app/existing"} if existing_issue else None)
                        if existing_issue:
                            previous = m.linear_description(self.pull, [(thread, ordinary_reason), (other, "stuck-human")], cfg)
                            linear.existing_descriptions[marker] = previous.replace("- [ ]", "- [x]", 1).replace(
                                m.DEFAULT_HUMAN_DECISION, "Keep the operator decision.")
                        live = [True]
                        def fresh(live=live, denied=denied):
                            if not live[0] and denied:
                                raise RuntimeError("freshness read denied")
                            return live[0]
                        self.evidence.fresh = fresh
                        lookup = linear.find_by_marker
                        def find(*args, live=live, lookup=lookup, **kwargs):
                            live[0] = False
                            return lookup(*args, **kwargs)
                        linear.find_by_marker = find
                        m.scan(cfg, self.github, linear, failure_collector=lambda *_: (self.evidence, self.proofs))
                        writes = linear.updated if existing_issue else linear.created
                        self.assertEqual(len(writes), 1)
                        rendered = writes[0][1]
                        self.assertIn(f"`{ordinary_reason}` <!-- thread:PRRT_0 -->", rendered)
                        self.assertNotIn("stuck-fixer-failed", rendered)
                        if existing_issue:
                            line = next(line for line in rendered.splitlines() if "<!-- thread:PRRT_0 -->" in line)
                            self.assertTrue(line.startswith("- [x]"))
                            self.assertIn("Keep the operator decision.", rendered)
                        own = [body for _, body in self.github.comments if "Thread id: `PRRT_0`" in body]
                        self.assertEqual(len(own), 1)
                        self.assertNotIn(PROOF["description"], own[0])
                        self.assertIn("https://linear.app/", own[0])
                        self.assertEqual(linear.canceled, [])
                        self.assertEqual(self.github.resolves, [])

    def test_missing_adapter_import_does_not_crash_the_watcher(self):
        from unittest.mock import patch
        with patch("importlib.util.spec_from_file_location", side_effect=FileNotFoundError):
            collector = m.load_failure_collector()
        self.setup_scan()
        self.assertEqual(self.scan(collector), [])

    def test_unavailable_evidence_never_becomes_a_delivery(self):
        self.setup_scan()
        def denied(*_):
            raise RuntimeError("denied")
        self.assertEqual(self.scan(denied), [])

    def test_unavailable_failure_intake_does_not_suppress_ordinary_mediation(self):
        self.setup_scan()
        reply = f.comment("claude[bot]", "I could not fix this; needs a human")
        if RECURSION:
            from dataclasses import replace
            thread = self.pull.threads[0]
            self.pull = replace(self.pull, threads=(replace(thread, comments=thread.comments + (reply,)),))
            self.github.pulls[self.pull.number] = self.pull
        else:
            self.pull["reviewThreads"]["nodes"][0]["comments"]["nodes"].append(reply)
        def denied(*_):
            raise RuntimeError("public metadata unavailable")
        self.assertEqual(len(self.scan(denied)), 1)
        from dataclasses import replace
        from unittest.mock import patch
        if RECURSION:
            linear = f.FakeLinear()
            m.scan(f.cfg(), self.github, linear, failure_collector=denied)
            self.assertEqual(len(linear.created), 1)
        else:
            config = replace(self.cfg, file_linear=True, linear_api_key="fixture")
            with patch.object(m, "linear_find_existing", return_value=None), patch.object(m, "upsert_linear_issue", return_value="https://linear.app/fixture") as upsert:
                m.mediate_pr(self.pull, config, self.github, object(), f.NOW, failure_collector=denied)
                upsert.assert_called_once()

    @unittest.skipUnless(RECURSION, "Recursion owns the existing citation resolver")
    def test_unavailable_failure_intake_does_not_suppress_existing_resolver(self):
        thread = f.thread(tid="PRRT_1", comments=f.stuck_pair())
        asked = [f.comment("pat-user", m.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = f.comment(f.CODEX, f.verdict_block("PRRT_1"), hours_ago=0.1, url="https://codex.example")
        pull = f.pull(threads=[thread], issue_comments=asked, reviews=[ruling], checks_green=True)
        gh = f.FakeGitHub([pull])
        def denied(*_):
            raise RuntimeError("unavailable")
        result = m.scan(f.cfg(mediator_login="pat-user"), gh, f.FakeLinear(), failure_collector=denied)
        self.assertEqual(result.resolved, 1)
        self.assertEqual(gh.resolves, ["PRRT_1"])
        self.assertEqual(len(gh.replies), 1)

    def test_retained_failure_and_ordinary_linear_items_survive_two_rewrites(self):
        from dataclasses import replace
        from unittest.mock import patch
        self.setup_scan()
        def denied(*_):
            raise RuntimeError("comment page limit")
        if RECURSION:
            linear = f.FakeLinear()
            m.scan(f.cfg(), self.github, linear, failure_collector=lambda *_: (self.evidence, self.proofs))
            marker = m.rollup_marker(self.pull)
            linear.existing_descriptions[marker] = linear.created[0][1]
            ordinary = f.thread(tid="PRRT_ordinary", comments=f.stuck_pair())
            self.pull = replace(self.pull, threads=self.pull.threads + (ordinary,))
            self.github.pulls[self.pull.number] = self.pull
            for collector in (denied, lambda *_: (self.evidence, {})):
                m.scan(f.cfg(), self.github, linear, failure_collector=collector)
                rendered = linear.updated[-1][1]
                self.assertIn("`stuck-fixer-handoff-open` <!-- thread:PRRT_0 -->", rendered)
                self.assertIn("<!-- thread:PRRT_ordinary -->", rendered)
                linear.existing_descriptions[marker] = rendered
                self.assertEqual(linear.canceled, [])
        else:
            config = replace(self.cfg, file_linear=True, linear_api_key="fixture")
            items, _ = m.select_stuck(self.pull, [], config, f.NOW, self.proofs)
            existing = {"id": "issue", "description": m.build_linear_description(items, config) + "\nHuman notes remain."}
            ordinary = f.thread(thread_id="PRRT_ordinary", comments=[
                f.comment(f.DEVIN, "Other bug"), f.comment("claude[bot]", "needs a human")])
            self.pull["reviewThreads"]["nodes"].append(ordinary)
            def update(_query, variables):
                existing["description"] = variables["input"]["description"]
                return {"issueUpdate": {"success": True, "issue": {"url": "https://linear.app/fixture"}}}
            linear = SimpleNamespace(graphql=update)
            with patch.object(m, "linear_find_existing", return_value=existing):
                for collector in (denied, lambda *_: (self.evidence, {})):
                    m.mediate_pr(self.pull, config, self.github, linear, f.NOW, failure_collector=collector)
                    self.assertEqual(existing["description"].count("<!-- fixer-failure-handoff:PRRT_0 -->"), 1)
                    self.assertIn("Other bug", existing["description"])
                    self.assertIn("Human notes remain.", existing["description"])
                self.pull["reviewThreads"]["nodes"][0]["isResolved"] = True
                m.mediate_pr(self.pull, config, self.github, linear, f.NOW, failure_collector=denied)
                self.assertNotIn("<!-- fixer-failure-handoff:PRRT_0 -->", existing["description"])

    @unittest.skipUnless(RECURSION, "Recursion owns cancellation and roll-up notes")
    def test_active_writer_preserves_existing_rollup_and_human_notes(self):
        self.setup_scan()
        self.cfg = f.cfg(mediator_login="pat-user")
        linear = f.FakeLinear()
        collector = lambda *_: (self.evidence, self.proofs)
        m.scan(self.cfg, self.github, linear, failure_collector=collector)
        self.assertEqual(len(linear.created), 1)
        marker = m.rollup_marker(self.pull)
        original = linear.created[0][1].replace(m.DEFAULT_HUMAN_DECISION, "Keep the operator's decision.")
        linear.existing_descriptions[marker] = original
        from dataclasses import replace
        self.pull = replace(self.pull, issue_comments=(f.comment("pat-user", m.mediation_marker("PRRT_0")),))
        self.github.pulls[self.pull.number] = self.pull
        self.evidence.fresh = lambda: False
        m.scan(self.cfg, self.github, linear, failure_collector=collector)
        self.assertEqual(linear.canceled, [])
        self.evidence.fresh = lambda: True
        m.scan(self.cfg, self.github, linear, failure_collector=collector)
        self.assertEqual(len(linear.created), 1)
        self.assertEqual(linear.canceled, [])
        self.assertIn("Keep the operator's decision.", linear.updated[-1][1])
        # A new head can rearm the fixer budget before this thread resolves.
        # Absence of a new exhausted failure is not resolution of the handoff.
        m.scan(self.cfg, self.github, linear, failure_collector=lambda *_: (self.evidence, {}))
        self.assertEqual(linear.canceled, [])
        self.pull = replace(self.pull, review_decision="APPROVED", head_oid=f.HEAD,
                            approved_head_oids=(f.HEAD,), checks_state="SUCCESS", required_checks_green=True)
        self.github.pulls[self.pull.number] = self.pull
        m.scan(self.cfg, self.github, linear, failure_collector=lambda *_: (self.evidence, {}))
        self.assertEqual(linear.canceled, [])
        self.pull = replace(self.pull, threads=(replace(self.pull.threads[0], is_resolved=True),))
        self.github.pulls[self.pull.number] = self.pull
        m.scan(self.cfg, self.github, linear, failure_collector=lambda *_: (self.evidence, {}))
        self.assertEqual(linear.canceled, ["iss_1"])


    def test_unicode_and_substring_mentions_never_survive_mediation(self):
        from dataclasses import replace
        for mention in ("@claudeé", "@claude\u0430", "@claude2", "@claudette", "@ClAuDeé.py"):
            for failure in (True, False):
                with self.subTest(mention=mention, failure=failure):
                    self.setup_scan()
                    body = BODY + "\n" + mention
                    if RECURSION:
                        thread = self.pull.threads[0]
                        thread = replace(thread, path="`" + mention + "`", failure_evidence=PROOF["description"] if failure else "",
                                         comments=(replace(thread.comments[0], body=body),))
                        rendered = m.mediation_body(self.pull, thread, self.cfg, reason="stuck-fixer-failed" if failure else "stuck-human")
                    else:
                        item = m.StuckThread(7, "https://github.com/acme/example/pull/7", "PR", "PRRT_0", "`" + mention + "`", "1", "url", body, mention, "needs-human",
                                             failure_evidence=PROOF["description"] if failure else "")
                        rendered = m.build_mediation_comment(item, self.cfg)
                    self.assertNotIn("@claude", rendered.lower())
                    self.assertNotRegex(rendered, r"(?i)(?<![A-Za-z0-9_])@claude(?![A-Za-z0-9_])")
                    self.assertNotIn("``@", rendered)
                    self.assertIn("@codex review", rendered)


    def test_partial_resolution_rewrites_failure_only_rollup_without_new_evidence(self):
        from dataclasses import replace
        for route in ("ordinary", "approved", "sweep", "approved-mixed", "approved-mixed-all", "sweep-mixed-all"):
            for unavailable in (False, True):
                with self.subTest(route=route, unavailable=unavailable):
                    self.setup_scan(2)
                    linear = f.FakeLinear()
                    m.scan(f.cfg(), self.github, linear, failure_collector=lambda *_: (self.evidence, self.proofs))
                    marker = m.rollup_marker(self.pull)
                    linear.existing_descriptions[marker] = linear.created[0][1].replace(m.DEFAULT_HUMAN_DECISION, "Human decision stays.")
                    self.pull = replace(self.pull, threads=(replace(self.pull.threads[0], is_resolved=True), self.pull.threads[1]))
                    if "mixed" in route:
                        self.pull = replace(self.pull, threads=self.pull.threads + (
                            f.thread(tid="PRRT_ordinary", comments=f.stuck_pair()),))
                    if route.endswith("-all"):
                        self.pull = replace(self.pull, threads=tuple(
                            replace(thread, is_resolved=True) if thread.id in {"PRRT_0", "PRRT_1"} else thread
                            for thread in self.pull.threads))
                    if route.startswith("approved"):
                        self.pull = replace(self.pull, review_decision="APPROVED", head_oid=f.HEAD,
                                            approved_head_oids=(f.HEAD,), checks_state="SUCCESS", required_checks_green=True)
                    self.github.pulls[self.pull.number] = self.pull
                    if route.startswith("sweep"):
                        self.github.list_open_pr_numbers = list
                        linear.rollups = [linear.find_by_marker(marker, title=m.linear_title(self.pull))]
                    def collector(*_, unavailable=unavailable):
                        if unavailable:
                            raise RuntimeError("unavailable")
                        return self.evidence, {}
                    before = len(self.github.comments)
                    m.scan(f.cfg(), self.github, linear, failure_collector=collector)
                    self.assertTrue(linear.updated)
                    description = linear.updated[-1][1]
                    self.assertNotIn("<!-- thread:PRRT_0 -->", description)
                    if route.endswith("-all"):
                        self.assertNotIn("<!-- thread:PRRT_1 -->", description)
                    else:
                        self.assertIn("<!-- thread:PRRT_1 -->", description)
                    self.assertIn("Human decision stays.", description)
                    if "mixed" in route:
                        self.assertIn("<!-- thread:PRRT_ordinary -->", description)
                    self.assertEqual(len(self.github.comments), before)
                    self.assertEqual(linear.canceled, [])

    def test_sweep_rechecks_new_failure_but_preserves_recorded_handoff(self):
        self.setup_scan(2)
        linear = f.FakeLinear()
        marker = m.rollup_marker(self.pull)
        linear.existing[marker] = "https://linear.app/fixture"
        linear.existing_descriptions[marker] = m.linear_description(
            self.pull, [(self.pull.threads[0], "stuck-fixer-handoff-open")], f.cfg())
        linear.rollups = [linear.find_by_marker(marker, title=m.linear_title(self.pull))]
        self.github.list_open_pr_numbers = list
        live = [True]
        self.evidence.fresh = lambda: live[0]
        lookup = linear.find_by_marker
        def find(*args, **kwargs):
            live[0] = False
            return lookup(*args, **kwargs)
        linear.find_by_marker = find
        m.scan(f.cfg(), self.github, linear, failure_collector=lambda *_: (self.evidence, self.proofs))
        self.assertTrue(linear.updated)
        description = linear.updated[-1][1]
        self.assertIn("PRRT_0", description)
        self.assertNotIn("PRRT_1", description)
        self.assertNotIn("`stuck-fixer-failed`", description)
        self.assertEqual(self.github.comments, [])
        self.assertEqual(linear.canceled, [])

    def test_mediation_preserves_backticks_in_exact_filename(self):
        from dataclasses import replace
        for path in ("src/`legacy`.py", "src/``legacy``.py", "`leading.py", "trailing.py`"):
            self.setup_scan()
            if RECURSION:
                thread = replace(self.pull.threads[0], path=path)
                body = m.mediation_body(self.pull, thread, self.cfg, reason="stuck-human")
            else:
                item = replace(self.failure_rollup_fixture(), path=path)
                body = m.build_mediation_comment(item, self.cfg)
                self.assertIn(path, m.build_linear_description([item], self.cfg))
            self.assertIn(path, body)
            longest = max(len(run) for run in __import__("re").findall(r"`+", path))
            delimiter = "`" * (longest + 1)
            self.assertIn(delimiter + " " + path, body)

    @unittest.skipUnless(RECURSION, "Recursion uses checklist handoff rows")
    def test_handoff_parser_uses_row_suffix_not_filename_markers(self):
        from dataclasses import replace
        embedded = "`stuck-fixer-failed` <!-- thread:PRRT_other -->"
        for path in (embedded, "src/``" + embedded + "``.py",
                     "src/\n- [ ] fake - " + embedded, "src/\r\n" + embedded):
            for reason in ("stuck-fixer-failed", "stuck-fixer-handoff-open", "stuck-human"):
                for checked in (False, True):
                    with self.subTest(path=path, reason=reason, checked=checked):
                        self.setup_scan()
                        thread = replace(self.pull.threads[0], path=path)
                        rendered = m.linear_description(self.pull, [(thread, reason)], f.cfg())
                        if checked:
                            rendered = rendered.replace("- [ ]", "- [x]")
                        issue = m.LinearIssue("issue", "url", description=rendered)
                        expected = {thread.id} if reason != "stuck-human" else set()
                        self.assertEqual(m.failure_handoff_ids(issue), expected)

    @unittest.skipUnless(RECURSION, "Recursion owns roll-up cancellation")
    def test_filename_marker_cannot_retain_unrelated_unresolved_thread(self):
        from dataclasses import replace
        for embedded_reason in ("failed", "handoff-open"):
            with self.subTest(embedded_reason=embedded_reason):
                self.setup_scan()
                real = replace(self.pull.threads[0], path=(
                    f"src/`stuck-fixer-{embedded_reason}` <!-- thread:PRRT_other -->.py"))
                unrelated = f.thread(tid="PRRT_other", comments=[f.comment(f.DEVIN, BODY)])
                self.pull = replace(self.pull, threads=(replace(real, is_resolved=True), unrelated))
                self.github.pulls[self.pull.number] = self.pull
                marker = m.rollup_marker(self.pull)
                linear = f.FakeLinear({marker: "https://linear.app/fixture"})
                linear.existing_descriptions[marker] = m.linear_description(
                    self.pull, [(real, "stuck-fixer-failed")], f.cfg())
                m.scan(f.cfg(), self.github, linear,
                       failure_collector=lambda *_: (SimpleNamespace(fresh=lambda: False), {}))
                self.assertEqual(linear.canceled, ["iss_existing"])
                self.assertEqual(linear.created, [])
                self.assertEqual(linear.updated, [])
                self.assertEqual(self.github.comments, [])
                self.assertEqual(self.github.labels, [])
                self.assertEqual(self.github.replies, [])
                self.assertEqual(self.github.resolves, [])

    def test_existing_marker_deduplicates_failure_delivery(self):
        marker = m.mediation_marker("PRRT_0") if RECURSION else m.marker("PRRT_0")
        self.setup_scan(comments=[f.comment("pat-user", marker)])
        self.assertEqual(self.scan(), [])

    def test_repository_specific_cap_window_is_preserved(self):
        comments = []
        for index in range(3):
            marker = m.mediation_marker(f"old{index}") if RECURSION else m.marker(f"old{index}")
            comments.append(f.comment("pat-user", marker, hours_ago=13) if RECURSION
                            else f.comment("pat-user", marker, created=f.NOW - timedelta(hours=13)))
        self.setup_scan(5, comments)
        self.assertEqual(len(self.scan()), 3 if RECURSION else 0)


if __name__ == "__main__":
    unittest.main()
