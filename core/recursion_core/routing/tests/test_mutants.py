"""Mutation check: break the routing code one way at a time and show the routing tests catch each break.

Ported from the vault prototype's mutants.py (20261107-csl729-routing-config-q45-01, section 8.4). Kept:
M1, M2, M4 and M6 as the prototype wrote them, against this code. M3 ("a limit result retried on a second
login of the same vendor") mutated the prototype's ``run_task``, which dispatched; dispatch is not ported
(the daemon launches), so M3 is re-expressed against the rule that now carries it: a fallback the user
listed is billed only through a configured login of its engine. M5 and M7 mutated the loopback gate of
``dispatch`` and are not ported. M8 and M9 are new: the two loader rules this port adds or keeps alone.

Each mutant runs in a child interpreter (the same executable and ``-X`` options): the module is imported,
its source with the one patch is executed into the module's own namespace, and the routing test modules
run against it. A control run with no patch must pass, so a failure comes from the patch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import recursion_core

CORE = Path(recursion_core.__file__).resolve().parents[1]
TEST_MODULES = ("recursion_core.routing.tests.test_config", "recursion_core.routing.tests.test_router",
                "recursion_core.routing.tests.test_detect", "recursion_core.routing.tests.test_explain_and_limits")
CONFIG, ROUTER, DETECT = "recursion_core.routing.config", "recursion_core.routing.router", "recursion_core.routing.detect"

MUTANTS = {
    "M1 rules evaluated before the hand pick": (
        ROUTER,
        "    if hp is not None:\n",
        "    if hp is not None and not any(_matches(r, kind, tags) for r in config.rules):\n",
        {"test_hand_pick_overrides_matching_rule_and_default", "test_hand_pick_beats_every_rule_order_and_default",
         "test_exhaustive_every_routable_subset_routes_only_inside_it"}),
    "M2 an unavailable default falls back to any routable engine": (
        ROUTER,
        '    return refused("default_engine_unavailable"',
        '    for alt in config.engine_login:\n'
        '        if _state(config, entries, err, alt) is None:\n'
        '            return chosen("default", None, "default", alt, model_of(None, alt))\n'
        '    return refused("default_engine_unavailable"',
        {"test_undetected_default_refuses_and_never_picks_another_engine"}),
    "M3 a limit fallback may name a login the user did not configure (re-expressed)": (
        CONFIG,
        "            if lg is None or _connection(other) != (lg.kind, lg.connection_id):\n",
        "            if False:\n",
        {"test_fallback_routes_are_only_configured_logins_of_their_engine", "test_loader_cross_field_rules"}),
    "M4 the snapshot filtered entry by entry, not validated as a whole": (
        ROUTER,
        '        if not _entry_ok(eng, entry):\n            return None, "detection_malformed"\n',
        '        if not _entry_ok(eng, entry):\n            continue\n',
        {"test_adversarial_snapshots_refuse_everything_without_raising"}),
    "M6 vendor and kind taken from the operator's label, the catalog not checked": (
        CONFIG,
        '        if (meta["vendor"], meta["kind"]) != (lg.vendor, lg.kind):\n',
        "        if False:\n",
        {"test_claude_subscription_relabelled_as_another_vendor_is_refused",
         "test_second_login_under_another_label_is_refused"}),
    "M8 the login bound to an engine is not checked against the connection routes.json launches": (
        CONFIG,
        '        if _connection(entry.get("route") if isinstance(entry, Mapping) else None) != (lg.kind, lg.connection_id):\n',
        "        if False:\n",
        {"test_loader_cross_field_rules"}),
    "M9 the Claude subscription allowed outside claude_code": (
        CONFIG,
        '        if lg.vendor == "anthropic" and lg.kind == "subscription" and set(lg.engines) != {"claude_code"}:\n',
        "        if False:\n",
        {"test_engine_vendor_compatibility", "test_loader_cross_field_rules"}),
    # Slice 2.
    "M10 a hand-picked run switched to a listed fallback at a limit": (
        ROUTER,
        '    if decision["reason"] == "hand_pick":\n        return {"action": "ask", "reason": "hand_pick", "choices": choices}\n',
        "",
        {"test_a_hand_pick_is_never_switched_away_from"}),
    "M11 any failure treated as a limit (a blind retry)": (
        ROUTER,
        "    if code not in route_limits.LIMIT_CODES:\n",
        "    if code is None:\n",
        {"test_only_a_limit_leads_anywhere"}),
    "M12 a route already tried offered again": (
        ROUTER,
        "        if any(_same_route(route, seen) for seen in attempts):\n",
        "        if False:\n",
        {"test_the_next_listed_route_in_the_users_order", "test_never_a_route_the_user_did_not_list"}),
    "M13 an open breaker read as routable": (
        DETECT,
        '        if state == "open":\n            return "breaker_open"\n',
        "",
        {"test_an_open_or_unreadable_breaker_fences_its_connection", "test_decide_on_a_detected_snapshot_skips_a_fenced_rule"}),
    "M14 a key route routable with no key present or opted in": (
        DETECT,
        '    if kind == "api_key":\n',
        "    if False:\n",
        {"test_a_key_route_needs_its_key_present_and_opted_in"}),
}

CHILD = r"""
import importlib, io, json, sys, unittest
job = json.loads(sys.stdin.read())
module = importlib.import_module(job["module"])
with open(module.__file__, encoding="utf-8") as fh:
    source = fh.read()
assert source.count(job["old"]) == 1, "the mutant's anchor is not in the source exactly once"
exec(compile(source.replace(job["old"], job["new"]), module.__file__, "exec"), module.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromNames(job["tests"])
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
print(json.dumps({"ran": result.testsRun, "failed": sorted({t.id().rsplit(".", 1)[-1] for t, _ in result.failures + result.errors})}))
"""


def run_child(module: str, old: str, new: str) -> dict:
    options = [arg for key, value in sys._xoptions.items() for arg in ("-X", key if value is True else f"{key}={value}")]
    env = dict(os.environ, PYTHONPATH=str(CORE))
    job = json.dumps({"module": module, "old": old, "new": new, "tests": list(TEST_MODULES)})
    proc = subprocess.run([sys.executable, *options, "-B", "-c", CHILD], input=job, capture_output=True, text=True, env=env,
                          cwd=str(CORE))
    if proc.returncode != 0:
        raise AssertionError(f"the child run failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class MutantTests(unittest.TestCase):
    def test_control_run_with_no_patch_passes(self) -> None:
        anchor = MUTANTS["M1 rules evaluated before the hand pick"][1]
        out = run_child(ROUTER, anchor, anchor)
        self.assertGreater(out["ran"], 20)
        self.assertEqual(out["failed"], [])

    def test_every_kept_mutant_is_caught(self) -> None:
        for name, (module, old, new, killers) in MUTANTS.items():
            with self.subTest(name):
                out = run_child(module, old, new)
                self.assertTrue(out["failed"], f"{name}: no test failed")
                self.assertTrue(killers <= set(out["failed"]), f"{name}: failed {out['failed']}, expected {sorted(killers)}")


if __name__ == "__main__":
    unittest.main()
