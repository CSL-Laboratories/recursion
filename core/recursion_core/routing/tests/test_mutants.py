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
        '            if not _entry_ok(eng, entry):\n                return None, "detection_malformed"\n',
        '            if not _entry_ok(eng, entry):\n                continue\n',
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
    # From the first cross-family read of #371 (two Codex reviews): each guard a removal left green.
    "M15 the routing file's owner not checked": (
        CONFIG,
        "if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:",
        "if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:",
        {"test_a_file_of_another_user_is_refused"}),
    "M16 tags_all read as any of the tags": (
        ROUTER,
        "    if rule.tags_all is not None and not rule.tags_all <= tags:\n",
        "    if rule.tags_all is not None and not (rule.tags_all & tags):\n",
        {"test_tags_all_needs_every_tag"}),
    "M17 the default's explicit model ignored": (
        ROUTER,
        "eng, model = config.default.engine, model_of(config.default.model, config.default.engine)",
        "eng, model = config.default.engine, model_of(None, config.default.engine)",
        {"test_an_explicit_model_wins_over_the_configured_one"}),
    "M18 an empty condition list accepted": (
        CONFIG,
        "return isinstance(value, list) and len(value) > 0 and all(",
        "return isinstance(value, list) and all(",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M19 an unknown condition accepted": (
        CONFIG,
        "if not isinstance(when, Mapping) or not when or set(when) - WHEN_KEYS:",
        "if not isinstance(when, Mapping) or not when:",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M20 a connection id of any length accepted": (
        CONFIG,
        "return isinstance(value, str) and 0 < len(value) <= 200 and",
        "return isinstance(value, str) and 0 < len(value) and",
        {"test_every_shape_defect_is_refused_by_the_loader_with_its_code"}),
    "M21 a version of true or 1.0 accepted": (
        CONFIG,
        'if type(raw.get("version")) is not int or raw["version"] != VERSION:',
        'if raw.get("version") != VERSION:',
        {"test_values_a_schema_cannot_tell_apart_are_refused_by_the_loader"}),
    "M22 a present null routing file read as absent": (
        CONFIG,
        "    if parsed is None:\n        raise ConfigError(",
        "    if parsed is None:\n        return None\n        raise ConfigError(",
        {"test_a_present_null_file_is_refused_never_read_as_absent",
         "test_load_reads_routes_json_with_load_routes_and_routing_json_beside_it"}),
    "M23 loaded limits left shared with the caller": (
        CONFIG,
        'limits = copy.deepcopy(route_limits.validate_limits(raw["limits"]))',
        'limits = route_limits.validate_limits(raw["limits"])',
        {"test_loaded_limits_share_nothing_with_the_callers_objects"}),
    "M24 a member named twice read as the last one": (
        CONFIG,
        "parsed = json.loads(text, object_pairs_hook=_no_duplicate_members)",
        "parsed = json.loads(text)",
        {"test_a_member_named_twice_is_refused"}),
    # Slice 2, after #371 passed its reads: route.explain's published answer, and a key store that keeps keys of
    # providers routing does not list.
    "M25 an engine with a hand pick of another engine: the hand pick taken silently": (
        ROUTER,
        '        if hand["engine"] != params["engine"]:\n'
        '            return {"code": "request_invalid", "detail": "engine_and_hand_pick_differ"}\n',
        "",
        {"test_an_engine_and_a_hand_pick"}),
    "M26 explain's rule outcomes returned beside the decision": (
        ROUTER,
        '    return {"code": "ok", "decision": route_decision(decision)}',
        '    return {"code": "ok", "decision": route_decision(decision), "rules": explain(config, detection, task)["rules"]}',
        {"test_a_routed_task_answers_ok_and_the_route_decision_only"}),
    "M27 a refused hand pick answered with the config's route": (
        ROUTER,
        '    if not decision["ok"]:\n        detail = ',
        '    if not decision["ok"] and "hand_pick" in task:\n'
        '        decision = decide(config, detection, {k: v for k, v in task.items() if k != "hand_pick"})\n'
        '    if not decision["ok"]:\n        detail = ',
        {"test_the_h33_example_is_refused_never_answered_by_the_config",
         "test_a_refused_decision_answers_request_invalid_with_routings_code"}),
    "M28 a key of a provider outside the vendor list refused": (
        CONFIG,
        '        put(key_id, meta["provider"], "api_key", "key store")\n',
        '        if meta["provider"] not in VENDORS:\n'
        '            raise ConfigError("key_metadata_invalid", key_id)\n'
        '        put(key_id, meta["provider"], "api_key", "key store")\n',
        {"test_a_key_of_a_provider_outside_the_vendor_list_leaves_loading_alone"}),
    # The first reads of #399 (05a5d13d): each guard they found unprotected or missing.
    "M34 a null vendor reset read as no deadline": (
        ROUTER,
        '        until = signal.get("reset_at")\n        if until is None:\n            until = limit.get("retry_at")\n',
        '        until = signal.get("reset_at", limit.get("retry_at"))\n',
        {"test_wait_uses_breaker_retry_when_vendor_reset_is_null"}),
    "M35 the decided primary not counted as tried": (
        ROUTER,
        '    attempts.append({"engine": decision["engine"]',
        '    (lambda _: None)({"engine": decision["engine"]',
        {"test_primary_not_reoffered_without_notice"}),
    "M36 the model left out of a tried route's identity": (
        ROUTER,
        '    mine = (route["engine"], route["model"], contract.connection_key(route))\n'
        '    return mine == (seen.get("engine"), seen.get("model"), seen.get("connection"))\n',
        '    mine = (route["engine"], contract.connection_key(route))\n'
        '    return mine == (seen.get("engine"), seen.get("connection"))\n',
        {"test_same_engine_and_connection_other_model_still_allowed"}),
    "M37 a route with no model anywhere routed": (
        ROUTER,
        '        return "route_no_model" if why is None and model is None else why\n',
        "        return why\n",
        {"test_a_route_with_no_model_anywhere_is_not_routable"}),
    "M38 a bad hand pick explained as rule rows": (
        ROUTER,
        'bad_task = decision.get("code") in ("bad_task", "bad_hand_pick")',
        'bad_task = decision.get("code") in ("bad_task",)',
        {"test_a_bad_hand_pick_is_explained_as_not_reached"}),
    "M39 explain reading the task a second time": (
        ROUTER,
        "    else:\n        decision = _decide(config, detection, kind, tags, hp)\n",
        "    else:\n        decision = decide(config, detection, task)\n",
        {"test_explain_reads_the_task_once"}),
    "M40 a present null tags read as absent": (
        ROUTER,
        '    if present["tags"] and not (',
        "    if tags is not None and not (",
        {"test_a_present_null_condition_is_refused_not_read_as_absent"}),
    "M41 a hand pick without its model member taken": (
        ROUTER,
        'shaped = set(hand) == {"engine", "model"} and',
        'shaped = set(hand) <= {"engine", "model"} and',
        {"test_bad_task_or_hand_pick_is_refused"}),
    "M42 a snapshot that cannot be walked raising instead of refusing": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a snapshot that cannot be read is malformed, never partly used\n",
        "    except ZeroDivisionError:\n",
        {"test_a_snapshot_that_cannot_be_walked_is_malformed"}),
    "M43 a decision that is not decide's answered as no route": (
        ROUTER,
        '    if not isinstance(decision, Mapping) or "ok" not in decision:\n'
        '        return {"action": "none", "reason": "no_decision"}\n'
        '    if not decision["ok"]:\n',
        '    if not decision.get("ok"):\n',
        {"test_next_after_limit_takes_decides_answer_only"}),
    "M44 a refusal detail that is not a code word": (
        ROUTER,
        'raise _Refuse("bad_hand_pick", "hand_pick_model")',
        'raise _Refuse("bad_hand_pick", "hand_pick.model")',
        {"test_every_refusal_detail_is_code_words"}),
    # The second reads of #399 (d857ebb5): sol C1-C3 (their M45-M47 as proposed), DeepSeek R1-R7.
    "M45 a matching rule with no model routed": (
        ROUTER,
        "        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)\n"
        "        why = why_not(eng, model)\n",
        "        eng, model = rule.route.engine, model_of(rule.route.model, rule.route.engine)\n"
        "        why = _state(config, entries, err, eng)\n",
        {"test_a_rule_without_model_is_skipped_or_refused"}),
    "M46 bytes accepted as active engine ids": (
        DETECT,
        "    if isinstance(active, (str, bytes)):",
        "    if isinstance(active, str):",
        {"test_small_interface_gaps"}),
    "M47 EXPLAIN_PARAMS missing from star exports": (
        "recursion_core.routing",
        '    "EXPLAIN_PARAMS",\n',
        "",
        {"test_small_interface_gaps"}),
    "M48 a present null hand pick read as none": (
        ROUTER,
        '    if not present["hand_pick"]:\n',
        "    if hand is None:\n",
        {"test_a_null_hand_pick_is_refused_alike_everywhere", "test_a_present_null_condition_is_refused_not_read_as_absent"}),
    "M49 a null attempts list raising": (
        ROUTER,
        'attempts = [a for a in (notice.get("attempts") or ()) if isinstance(a, Mapping)]',
        'attempts = [a for a in notice.get("attempts", ()) if isinstance(a, Mapping)]',
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M50 a decision routed in name only used": (
        ROUTER,
        '        return {"action": "none", "reason": "no_decision"}  # routed in name only: not decide\'s answer\n',
        "        pass\n",
        {"test_a_null_attempts_list_and_a_decision_in_name_only"}),
    "M51 an invalid routes.json limits block used as it is": (
        ROUTER,
        '            return route_limits.validate_limits(raw), "routes"\n',
        '            return raw, "routes"\n',
        {"test_an_invalid_routes_json_limits_block_falls_back_to_ask"}),
    "M52 a listed route walked twice": (
        ROUTER,
        '    for place, route in enumerate(limits["fallback"], 1):\n',
        '    for place, route in enumerate(limits["fallback"] + limits["fallback"][:1], 1):\n',
        {"test_an_exhausted_list_names_each_listed_route_once_in_order"}),
    "M53 a task that cannot be read raising instead of refusing": (
        ROUTER,
        "    except Exception:  # noqa: BLE001 - a task that cannot be read is refused, never raised through\n",
        "    except ZeroDivisionError:\n",
        {"test_a_task_that_cannot_be_read_is_refused"}),
    "M54 route_decision of None raising": (
        ROUTER,
        '    if not isinstance(decision, Mapping) or not decision.get("ok"):\n        return None\n',
        '    if not decision.get("ok"):\n        return None\n',
        {"test_route_decision_of_anything_but_a_routed_decision_is_none"}),
}

# Removals from the published schema, each caught by the schema tests (which need jsonschema: hosted CI and
# the 3.12 interpreter have it, the free-threaded one does not).
SCHEMA = "schema"
SCHEMA_MUTANTS = {
    "S1 the top-level members not required": (
        SCHEMA, '  "required": ["version", "logins", "default", "rules"],\n', "",
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S2 a login's members not required": (
        SCHEMA, '"required": ["id", "vendor", "kind", "connection_id", "engines"],', '"required": [],',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S3 an unknown condition allowed": (
        SCHEMA, '"additionalProperties": false,\n          "minProperties": 1,', '"minProperties": 1,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S4 an empty tags_all allowed": (
        SCHEMA, '"tags_all": {"type": "array", "minItems": 1,', '"tags_all": {"type": "array", "minItems": 0,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S5 a login binding one engine twice allowed": (
        SCHEMA, '"uniqueItems": true,', '"uniqueItems": false,',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S6 a fallback connection's kind and id not required": (
        SCHEMA, '"required": ["kind", "id"],', '"required": [],',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S7 a line break allowed at the end of an id": (
        SCHEMA, r'"pattern": "^[a-z0-9][a-z0-9._-]{0,63}$",' + "\n      " + r'"not": {"pattern": "[\\r\\n]"}',
        r'"pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S8 a profile engine's fallback with no profile allowed": (
        SCHEMA, '"then": {"required": ["profile"], "properties": {"profile": {"type": "object"}}}', '"then": {}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S9 a custody key fallback with no credential_env allowed": (
        SCHEMA, '"required": ["credential_env"], ', "",
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    # S10 to S15 answer the second reads of #371 (b3bdb121): each removal had left every lane test green.
    "S10 an api_key_helper allowed on a subscription connection": (
        SCHEMA, '"connection": {"properties": {"kind": {"const": "api_key"}}}', '"connection": {"properties": {}}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S11 a credential_env that is not a string allowed": (
        SCHEMA, '"credential_env": {"type": "string", "minLength": 1}', '"credential_env": {"minLength": 1}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S12 an explicit custody key source exempt from credential_env": (
        SCHEMA, '"not": {"required": ["key_source"], "properties": {"key_source": {"const": "api_key_helper"}}}',
        '"not": {"required": ["key_source"]}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S13 a passthrough method that is not a string allowed": (
        SCHEMA, '"method": {"type": "string", "minLength": 1}', '"method": {"minLength": 1}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S14 an empty passthrough method allowed": (
        SCHEMA, '"method": {"type": "string", "minLength": 1}', '"method": {"type": "string"}',
        {"test_json_schema_accepts_example_and_rejects_bad_shapes"}),
    "S15 a helper key fallback made to name a credential_env": (
        SCHEMA,
        ',\n            "not": {"required": ["key_source"], "properties": {"key_source": {"const": "api_key_helper"}}}',
        "",
        {"test_a_helper_key_fallback_needs_no_credential_env"}),
}

try:
    import jsonschema  # noqa: F401 - only whether it is there
except ImportError:  # pragma: no cover - depends on the interpreter
    HAVE_JSONSCHEMA = False
else:
    HAVE_JSONSCHEMA = True

CHILD = r"""
import importlib, io, json, os, sys, tempfile, unittest
from pathlib import Path
job = json.loads(sys.stdin.read())
if job["module"] == "schema":
    from recursion_core.routing import config
    source = config.SCHEMA_FILE.read_text(encoding="utf-8")
    assert source.count(job["old"]) == 1, "the mutant's anchor is not in the schema exactly once"
    fd, name = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(source.replace(job["old"], job["new"]))
    json.loads(Path(name).read_text(encoding="utf-8"))  # the mutant is still a JSON document
    config.SCHEMA_FILE = Path(name)
else:
    module = importlib.import_module(job["module"])
    with open(module.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert source.count(job["old"]) == 1, "the mutant's anchor is not in the source exactly once"
    exec(compile(source.replace(job["old"], job["new"]), module.__file__, "exec"), module.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromNames(job["tests"])
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
if job["module"] == "schema":
    os.unlink(name)
failed = {t.id().split(" ")[0].rsplit(".", 1)[-1] for t, _ in result.failures + result.errors}
print(json.dumps({"ran": result.testsRun, "failed": sorted(failed)}))
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

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_every_schema_mutant_is_caught(self) -> None:
        anchor = SCHEMA_MUTANTS["S1 the top-level members not required"][1]
        self.assertEqual(run_child(SCHEMA, anchor, anchor)["failed"], [])  # the schema path's own control
        for name, (module, old, new, killers) in SCHEMA_MUTANTS.items():
            with self.subTest(name):
                out = run_child(module, old, new)
                self.assertTrue(killers <= set(out["failed"]), f"{name}: failed {out['failed']}, expected {sorted(killers)}")


if __name__ == "__main__":
    unittest.main()
