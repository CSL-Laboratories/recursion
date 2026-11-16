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
from recursion_core.routing.tests import test_config  # POSIX_OWNER, RoutingFileTests: where the reader's rules run

CORE = Path(recursion_core.__file__).resolve().parents[1]
TEST_MODULES = ("recursion_core.routing.tests.test_config", "recursion_core.routing.tests.test_router")
CONFIG, ROUTER = "recursion_core.routing.config", "recursion_core.routing.router"

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


# The routing file reader's mutants whose killers need the reader's owner check (test_config's RoutingFileTests,
# which run where os.geteuid exists, on Linux). Without the check (Windows) a present routing file is refused as
# unreadable before it is opened or parsed (test_config's NoOwnerCheckTests), so these rules cannot change an answer
# there and no test can catch them: each is judged where the check exists, and is named here with the reason it is
# not judged elsewhere. Every other mutant is judged on every platform.
NEEDS_OWNER_CHECK = {
    "M15 the routing file's owner not checked": "there is no owner to check: os.geteuid does not exist",
    "M22 a present null routing file read as absent": "a present routing file is refused before it is parsed",
    "M24 a member named twice read as the last one": "a present routing file is refused before it is parsed",
}


class MutantTests(unittest.TestCase):
    def test_control_run_with_no_patch_passes(self) -> None:
        anchor = MUTANTS["M1 rules evaluated before the hand pick"][1]
        out = run_child(ROUTER, anchor, anchor)
        self.assertGreater(out["ran"], 20)
        self.assertEqual(out["failed"], [])

    def test_every_kept_mutant_is_caught(self) -> None:
        for name, (module, old, new, killers) in MUTANTS.items():
            with self.subTest(name):
                if name in NEEDS_OWNER_CHECK and not test_config.POSIX_OWNER:
                    self.skipTest(f"{name}: not judged on this platform: {NEEDS_OWNER_CHECK[name]}")
                out = run_child(module, old, new)
                self.assertTrue(out["failed"], f"{name}: no test failed")
                self.assertTrue(killers <= set(out["failed"]), f"{name}: failed {out['failed']}, expected {sorted(killers)}")

    def test_only_the_reader_mutants_that_need_the_owner_check_are_ever_left_unjudged(self) -> None:
        self.assertLessEqual(set(NEEDS_OWNER_CHECK), set(MUTANTS))  # a renamed mutant cannot slip out of judgment
        reader = {name for name in dir(test_config.RoutingFileTests) if name.startswith("test_")}
        for name in NEEDS_OWNER_CHECK:
            module, _, _, killers = MUTANTS[name]
            self.assertEqual(module, CONFIG, name)
            self.assertLessEqual(killers, reader, name)  # each killer is skipped exactly where this one is not judged

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
