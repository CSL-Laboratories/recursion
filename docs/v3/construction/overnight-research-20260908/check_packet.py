"""Read-only preparation gate. Does not execute the proposed research campaign."""
import hashlib
import json
from pathlib import Path
import subprocess


PACKET = Path(__file__).resolve().parent
ROOT = PACKET.parents[3]
PREFIX = PACKET.relative_to(ROOT).as_posix() + "/"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)


def main():
    goal = read(PACKET / "goal-proposal.json")
    context = read(PACKET / "context.json")
    queue = read(PACKET / "research-queue.json")
    prior = ROOT / "docs/v3/construction/cut-publication-20260908"
    inventory = read(prior / "validation-inventory.json")
    require(goal["status"] == "proposed-awaiting-owner-activation", "proposal status")
    require(all(goal[k] is None for k in ("activation_start_commit", "started_at", "deadline_utc", "owner_approval")), "no invented activation")
    require(goal["parent_goal"] == "goal-recursion-v2-execution-fabric", "parent")
    require((goal["maximum_elapsed_seconds"], goal["discovery_seconds"], goal["finalization_reserve_seconds"]) == (43200, 32400, 10800), "time envelope")
    require((goal["model"], goal["reasoning"], goal["speed"]) == ("gpt-6-astra", "high", "standard"), "settings")
    require(goal["new_metered_spend"] == 0, "spend")
    require(all(goal[k] is False for k in ("automatic_settings_changes", "usage_resets_allowed", "resume_after_quota_reset", "nested_delegation", "formal_freeze", "runtime_migration_authorized", "new_campaigns_authorized")), "authority limits")
    require((goal["max_concurrent_read_only_workers"], goal["max_active_agents"], goal["worker_packet_minutes"]) == (4, 10, 20), "delegation limits")
    require((goal["checkpoint_minutes"], goal["usage_check_minutes"], goal["drift_audit_minutes"]) == (20, 15, 60), "checkpoint cadence")
    require((goal["usage_finalize_at_remaining_percent"], goal["no_new_workers_at_remaining_percent"], goal["checkpoint_stop_at_remaining_percent"]) == (10, 6, 3), "usage controls")
    require(goal["experiment_limits"] == {"states_per_representation": 500000, "seconds_per_experiment": 600, "memory_bytes": 2147483648, "artifact_bytes": 268435456, "owned_model_processes": 1, "model_network": False}, "experiment admission")
    require(goal["allowed_paths"] == read(prior / "goal.json")["allowed_paths"][:8] + ["docs/v3/construction/cut-source-overnight-20260908/**", "experiments/v3/cut-source-overnight/**"], "allowlist")
    require((goal["full_gates_required"], goal["independent_exact_reviews_required"], goal["minimum_frozen_predecessor_gates"], goal["unchanged_regression_commands"]) == (2, 2, 16, 25), "acceptance obligations")
    require(context["regression_commands"] == inventory["regression_commands"] and len(context["regression_commands"]) == 25, "unchanged regression inventory")
    require(context["frozen_gates"][1:] == inventory["frozen_gates"] and len(context["frozen_gates"]) == 16, "frozen predecessor inventory")
    require(context["frozen_gates"][0] == {"commit": "e4b844b1af6c309263812c6961ccb50b3e5b8a82", "branch": "codex/v3-cut-publication-20260908", "script": "experiments/v3/cut-publication/check.py"}, "immediate predecessor gate")
    require(queue["campaign"] == goal["campaign"] == "SC-CUT-SOURCE-01", "one campaign")
    require(queue["all_work_within_one_campaign"] is True, "queue scope")
    require(queue["order"] == [x["id"] for x in queue["items"]] == ["source_authority", "coupled_adversarial", "bounded_scaling"], "queue order")
    require([x["depends_on"] for x in queue["items"]] == [[], ["source_authority"], ["coupled_adversarial"]], "queue dependencies")
    base = context["preparation_start_commit"]
    require(base == goal["preparation_start_commit"] == "ec4232e95722d58dc0eff03289fb629b58d29247", "preparation baseline")
    git("merge-base", "--is-ancestor", base, "HEAD")
    require(not git("status", "--porcelain", "--untracked-files=all"), "clean committed packet required")
    changed = git("diff", "--name-only", base, "HEAD").splitlines()
    require(changed and all(x.startswith(PREFIX) for x in changed), "preparation changes outside packet")
    git("diff", "--check", base, "HEAD")
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    evidence = common.resolve() / "research-attestations/cut-publication/e4b844b1af6c309263812c6961ccb50b3e5b8a82"
    completion_path = evidence / "completion.json"
    require(hashlib.sha256(completion_path.read_bytes()).hexdigest() == context["predecessor_completion_sha256"], "predecessor completion hash")
    files = read(completion_path)["all_external_files_before_completion"]
    require(len(files) == context["verified_external_files"] == 245, "predecessor file count")
    for name, digest in files.items():
        path = (evidence / name).resolve()
        require(path.is_relative_to(evidence), "receipt path containment")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, "receipt hash: " + name)
    for name in ("handoff.md", "owner-approval.md"):
        require((PACKET / name).is_file(), "missing handoff document")
    print(json.dumps({"status": "proposal-ready-not-activated", "candidate": git("rev-parse", "HEAD"), "changed_files": len(changed), "predecessor_files_verified": len(files), "frozen_gate_references": 16, "regression_command_references": 25, "campaign_executed": False}))


if __name__ == "__main__":
    main()
