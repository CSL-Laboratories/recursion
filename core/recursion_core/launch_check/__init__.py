"""R2a launch check: exact binary and version per detected engine, failing closed (CSL-732, candidate). See README.md."""

from recursion_core.launch_check.check import launch_check
from recursion_core.launch_check.detect import Detection, Hit, codex_vendored_binary, detect, detect_all
from recursion_core.launch_check.floors import FLOOR_TABLE_VERSION, FLOORS, EngineSpec, ParsedVersion, parse_version, table_record
from recursion_core.launch_check.grant import MAX_ARG_BYTES, MAX_SPAWN_BYTES, Grant, LaunchPlan, authenticate, grant, launch_plan, verify
from recursion_core.launch_check.pins import PIN_TABLE_VERSION, PINS, Pin, tree_sha256
from recursion_core.launch_check.probe import PROBE_ENV_BASE_NAMES, ProbeResult, probe_env, run_version_probe
from recursion_core.launch_check.refusals import REFUSAL_CODES, REFUSAL_CODES_VERSION, LaunchRefused

__all__ = [
    "FLOOR_TABLE_VERSION", "FLOORS", "MAX_ARG_BYTES", "MAX_SPAWN_BYTES", "PINS", "PIN_TABLE_VERSION",
    "PROBE_ENV_BASE_NAMES", "REFUSAL_CODES", "REFUSAL_CODES_VERSION", "Detection", "EngineSpec", "Grant", "Hit",
    "LaunchPlan", "LaunchRefused", "ParsedVersion", "Pin", "ProbeResult", "authenticate",
    "codex_vendored_binary", "detect", "detect_all", "grant", "launch_check", "launch_plan", "parse_version", "probe_env",
    "run_version_probe", "table_record", "tree_sha256", "verify",
]
