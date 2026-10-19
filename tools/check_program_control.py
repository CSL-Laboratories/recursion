#!/usr/bin/env python3
"""Deterministic, provider-free validator for the CSL-204 program ledger.

Run from the repository root:

    python -X utf8 -B tools/check_program_control.py

A pass proves structural closure only. It is not Owner acceptance, independent
verification, product qualification, or release approval.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/v3/program-control/program.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CSL_ISSUE = re.compile(r"^CSL-[0-9]+$")
MATURITY_ORDER = [
    "unresearched", "researched", "human_decided", "contract_frozen", "prototyped",
    "implemented", "provider_free_verified", "locally_qualified", "release_qualified",
]
PLAN_FIELDS = {"question", "source_class", "quota_class", "stop_condition", "evidence_artifact"}
CONTINUATION_FIELDS = {"evidence_obtained", "remaining_uncertainty", "expected_quota_class",
                       "options", "recommendation", "next_stop_condition"}
DONE = {"done", "accepted", "verified"}
STAGES = {"intake": 0, "research": 1, "decision": 2, "development": 3,
          "verification": 4, "integration": 5, "release": 6}
SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")
# A pinned-manifest entry naming an absolute path (POSIX, Windows-drive, or
# UNC); such an entry is rejected as out-of-repository rather than joined.
ABSOLUTE_ENTRY = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\)")

# sha256-pinned text paths that landed before this guard and still lack
# `.gitattributes` `text eol=lf` coverage (`.gitattributes` is a protected
# path, so the missing rules are flagged for the repo owner on CSL-226).
# Entries must leave this set as the rules land: a covered or unpinned
# entry is stale and fails the check.
PINNED_EOL_KNOWN_GAPS = frozenset({
    "docs/v3/research-lane/overnight-20261002/semantic-guard-research/SEMANTIC-GUARD-COVERAGE.md",
    "docs/v3/research-lane/overnight-20261002/semantic-guard-research/coverage-matrix.json",
    "docs/v3/research-lane/overnight-20261002/semantic-validation-hardening/SEMANTIC-VALIDATION-HARDENING.md",
    "docs/v3/research-lane/overnight-20261002/semantic-validation-hardening/SOURCE-REPORT-PINS.md",
    "docs/v3/research-lane/shared-contracts-20261002/README.md",
    "docs/v3/research-lane/shared-contracts-20261002/Recursion_Harness_Orchestration_Performance.md",
    "docs/v3/research-lane/shared-contracts-20261002/Recursion_Prompt_Plugin_Skill_Architecture.md",
    "docs/v3/research-lane/shared-contracts-20261002/Recursion_Shared_Contract_Reconciliation.md",
    "docs/v3/research-lane/shared-contracts-20261002/synthetic_benchmark_corpus.json",
})

# These exact MANIFEST.json payloads were uncovered on the CSL-226 baseline.
# Keep the snapshot path-specific: a new pin in one of these packets must
# satisfy eol=lf or -text before it can pass.
PINNED_EOL_KNOWN_GAPS = PINNED_EOL_KNOWN_GAPS | frozenset("""
    docs/v3/research-lane/dot-ra-rd-20261002/RETURN-STATE.txt
    docs/v3/research-lane/dot-ra-rd-20261002/START-HERE.txt
    docs/v3/research-lane/dot-ra-rd-20261002/raw/00-assessment/report.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/example-manifest.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/plugin-manifest.schema.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/proposed-contract.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/report.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/source-ledger.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-B/report.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-B/sources-delivery.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-C/rc-counsel-question-checklist.csv
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-C/rc-public-evidence.csv
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-C/rc-source-license-manifest.json
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-C/report.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/R-D/report.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/R-A-plugin-portability.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/R-B-cross-harness-delegation.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/R-C-route-legal-and-licensing.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/R-D-quota-efficiency-measurement.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/README.md
    docs/v3/research-lane/dot-ra-rd-20261002/raw/input-as-received/check_research_stack.py
    docs/v3/research-lane/followups-20261002/START-HERE.txt
    docs/v3/research-lane/followups-20261002/machine/negative-traces.json
    docs/v3/research-lane/followups-20261002/machine/negative-traces.schema.json
    docs/v3/research-lane/followups-20261002/machine/platform-manifest.json
    docs/v3/research-lane/followups-20261002/machine/platform-manifest.schema.json
    docs/v3/research-lane/followups-20261002/machine/profile-fields.json
    docs/v3/research-lane/followups-20261002/machine/source-register.json
    docs/v3/research-lane/overnight-20261002/CHANGELOG.md
    docs/v3/research-lane/overnight-20261002/COVERAGE.json
    docs/v3/research-lane/overnight-20261002/DECISION-READOUT.md
    docs/v3/research-lane/overnight-20261002/FIXTURE-INDEX.json
    docs/v3/research-lane/overnight-20261002/LOCAL-SESSION-HANDOFF.md
    docs/v3/research-lane/overnight-20261002/QUALITY-NOTES.md
    docs/v3/research-lane/overnight-20261002/READ_FIRST.md
    docs/v3/research-lane/overnight-20261002/additional-kernel-research/SOURCE-MANIFEST.json
    docs/v3/research-lane/overnight-20261002/additional-kernel-research/SUPPLEMENTAL-KERNEL-AUDIT.md
    docs/v3/research-lane/overnight-20261002/gaps/architecture-coverage-source-rows.json
    docs/v3/research-lane/overnight-20261002/gaps/architecture-ledger.csv
    docs/v3/research-lane/overnight-20261002/gaps/architecture-ledger.json
    docs/v3/research-lane/overnight-20261002/gaps/comparison-source-manifest.json
    docs/v3/research-lane/overnight-20261002/gaps/complete-ledger.md
    docs/v3/research-lane/overnight-20261002/gaps/fu03-candidate-witness.json
    docs/v3/research-lane/overnight-20261002/gaps/fu03-static-evidence.json
    docs/v3/research-lane/overnight-20261002/gaps/fu04-static-evidence.json
    docs/v3/research-lane/overnight-20261002/gaps/gap-ledger.csv
    docs/v3/research-lane/overnight-20261002/gaps/gap-ledger.json
    docs/v3/research-lane/overnight-20261002/gaps/gaps-source-rows.json
    docs/v3/research-lane/overnight-20261002/gaps/harness-coverage-source-rows.json
    docs/v3/research-lane/overnight-20261002/gaps/harness-ledger.csv
    docs/v3/research-lane/overnight-20261002/gaps/harness-ledger.json
    docs/v3/research-lane/overnight-20261002/gaps/historical-interface-evidence.json
    docs/v3/research-lane/overnight-20261002/gaps/packet-mapping.csv
    docs/v3/research-lane/overnight-20261002/gaps/packet-mapping.json
    docs/v3/research-lane/overnight-20261002/gaps/report.md
    docs/v3/research-lane/overnight-20261002/gaps/source-manifest.json
    docs/v3/research-lane/overnight-20261002/kernel-local-handoff/LOCAL-KERNEL-REPRODUCTION-HANDOFF.md
    docs/v3/research-lane/overnight-20261002/kernel-local-handoff/cases.json
    docs/v3/research-lane/overnight-20261002/m1b-semantic-research/M1B-SEMANTIC-COVERAGE.md
    docs/v3/research-lane/overnight-20261002/m1b-semantic-research/coverage-matrix.json
    docs/v3/research-lane/overnight-20261002/m1b-semantic-research/source-pins.json
    docs/v3/research-lane/overnight-20261002/m1b-semantic-research/synthetic-inputs.json
    docs/v3/research-lane/overnight-20261002/m1c-classifier-research/M1C-CLASSIFIER-REVIEW.md
    docs/v3/research-lane/overnight-20261002/m1c-classifier-research/future-synthetic-vectors.json
    docs/v3/research-lane/overnight-20261002/m1c-classifier-research/semantic-matrix.json
    docs/v3/research-lane/overnight-20261002/m1c-classifier-research/source-pins.json
    docs/v3/research-lane/overnight-20261002/model-architecture/report.md
    docs/v3/research-lane/overnight-20261002/model-architecture/source-register.json
    docs/v3/research-lane/overnight-20261002/model-architecture/test-handovers.md
    docs/v3/research-lane/overnight-20261002/protocol-edges/local-test-handoff.json
    docs/v3/research-lane/overnight-20261002/protocol-edges/report.md
    docs/v3/research-lane/overnight-20261002/protocol-edges/sources.json
    docs/v3/research-lane/overnight-20261002/re/local-input-register.md
    docs/v3/research-lane/overnight-20261002/re/question-coverage.json
    docs/v3/research-lane/overnight-20261002/re/report.md
    docs/v3/research-lane/overnight-20261002/re/source-register.json
    docs/v3/research-lane/overnight-20261002/re/source-register.md
    docs/v3/research-lane/overnight-20261002/rf/question-coverage.json
    docs/v3/research-lane/overnight-20261002/rf/question-coverage.md
    docs/v3/research-lane/overnight-20261002/rf/report.md
    docs/v3/research-lane/overnight-20261002/rf/source-register.md
    docs/v3/research-lane/overnight-20261002/rf/sources.json
    docs/v3/research-lane/overnight-20261002/rf/unexecuted-local-test-handoff.md
    docs/v3/research-lane/overnight-20261002/rg/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rg/proposed-import-contract.json
    docs/v3/research-lane/overnight-20261002/rg/question-coverage.json
    docs/v3/research-lane/overnight-20261002/rg/report.md
    docs/v3/research-lane/overnight-20261002/rg/source-register.json
    docs/v3/research-lane/overnight-20261002/rg/source-register.md
    docs/v3/research-lane/overnight-20261002/rh/coverage.json
    docs/v3/research-lane/overnight-20261002/rh/report.md
    docs/v3/research-lane/overnight-20261002/rh/source-register.json
    docs/v3/research-lane/overnight-20261002/ri/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/ri/adoption-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/architecture-map.json
    docs/v3/research-lane/overnight-20261002/ri/coverage.json
    docs/v3/research-lane/overnight-20261002/ri/dependency-lock-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/extension-mapping.json
    docs/v3/research-lane/overnight-20261002/ri/extension-surface-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/gaps-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/input-disposition-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/licensing-provenance-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/ri/report.md
    docs/v3/research-lane/overnight-20261002/ri/source-drift-ledger.json
    docs/v3/research-lane/overnight-20261002/ri/source-register.json
    docs/v3/research-lane/overnight-20261002/ri/source-register.md
    docs/v3/research-lane/overnight-20261002/ri/static-provenance-checks.json
    docs/v3/research-lane/overnight-20261002/ri/ui-comparison.json
    docs/v3/research-lane/overnight-20261002/ri/workspace-inventory.json
    docs/v3/research-lane/overnight-20261002/rj/local-test-handoff.json
    docs/v3/research-lane/overnight-20261002/rj/question-coverage.json
    docs/v3/research-lane/overnight-20261002/rj/recommendations.json
    docs/v3/research-lane/overnight-20261002/rj/report.md
    docs/v3/research-lane/overnight-20261002/rj/source-register.json
    docs/v3/research-lane/overnight-20261002/rj/source-register.md
    docs/v3/research-lane/overnight-20261002/rj/unexecuted-local-test-handoff.md
    docs/v3/research-lane/overnight-20261002/rk/question-coverage.json
    docs/v3/research-lane/overnight-20261002/rk/report.md
    docs/v3/research-lane/overnight-20261002/rk/source-register.json
    docs/v3/research-lane/overnight-20261002/rk/source-register.md
    docs/v3/research-lane/overnight-20261002/rk/unexecuted-local-handoffs.json
    docs/v3/research-lane/overnight-20261002/rk/unexecuted-local-handoffs.md
    docs/v3/research-lane/overnight-20261002/rl/interface-contract.schema.json
    docs/v3/research-lane/overnight-20261002/rl/performance-profile.json
    docs/v3/research-lane/overnight-20261002/rl/proposed-contracts.json
    docs/v3/research-lane/overnight-20261002/rl/question-coverage.json
    docs/v3/research-lane/overnight-20261002/rl/recommendation-register.json
    docs/v3/research-lane/overnight-20261002/rl/report.md
    docs/v3/research-lane/overnight-20261002/rl/source-register.json
    docs/v3/research-lane/overnight-20261002/rl/source-register.md
    docs/v3/research-lane/overnight-20261002/rl/unexecuted-local-handoffs.json
    docs/v3/research-lane/overnight-20261002/rl/unexecuted-local-handoffs.md
    docs/v3/research-lane/overnight-20261002/rm/UNEXECUTED-acceptance-handoffs.md
    docs/v3/research-lane/overnight-20261002/rm/acceptance-handoffs.json
    docs/v3/research-lane/overnight-20261002/rm/coverage.json
    docs/v3/research-lane/overnight-20261002/rm/disposition-ledger.json
    docs/v3/research-lane/overnight-20261002/rm/disposition-ledger.md
    docs/v3/research-lane/overnight-20261002/rm/license-terms-ledger.json
    docs/v3/research-lane/overnight-20261002/rm/local-input-register.json
    docs/v3/research-lane/overnight-20261002/rm/port-candidates.json
    docs/v3/research-lane/overnight-20261002/rm/relationship-ledger.json
    docs/v3/research-lane/overnight-20261002/rm/report.md
    docs/v3/research-lane/overnight-20261002/rm/security-ledger.json
    docs/v3/research-lane/overnight-20261002/rm/source-file-pins.json
    docs/v3/research-lane/overnight-20261002/rm/source-register.json
    docs/v3/research-lane/overnight-20261002/rm/source-register.md
    docs/v3/research-lane/overnight-20261002/rm/surface-map.json
    docs/v3/research-lane/overnight-20261002/rm/version-drift-ledger.json
    docs/v3/research-lane/overnight-20261002/rn/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rn/accessibility-ledger.json
    docs/v3/research-lane/overnight-20261002/rn/accessibility-ledger.md
    docs/v3/research-lane/overnight-20261002/rn/candidate-comparison.json
    docs/v3/research-lane/overnight-20261002/rn/coverage.json
    docs/v3/research-lane/overnight-20261002/rn/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rn/performance-budget-template.json
    docs/v3/research-lane/overnight-20261002/rn/recommendations.json
    docs/v3/research-lane/overnight-20261002/rn/report.md
    docs/v3/research-lane/overnight-20261002/rn/requirements.json
    docs/v3/research-lane/overnight-20261002/rn/source-register.json
    docs/v3/research-lane/overnight-20261002/rn/source-register.md
    docs/v3/research-lane/overnight-20261002/ro/budget-register.json
    docs/v3/research-lane/overnight-20261002/ro/coverage.json
    docs/v3/research-lane/overnight-20261002/ro/fixtures.json
    docs/v3/research-lane/overnight-20261002/ro/report.md
    docs/v3/research-lane/overnight-20261002/ro/repository-source-manifest.json
    docs/v3/research-lane/overnight-20261002/ro/source-register.json
    docs/v3/research-lane/overnight-20261002/ro/source-register.md
    docs/v3/research-lane/overnight-20261002/ro/workload-manifest-template.json
    docs/v3/research-lane/overnight-20261002/rp/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rp/coverage.json
    docs/v3/research-lane/overnight-20261002/rp/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rp/proposed-contracts.json
    docs/v3/research-lane/overnight-20261002/rp/recommendations.json
    docs/v3/research-lane/overnight-20261002/rp/report.md
    docs/v3/research-lane/overnight-20261002/rp/source-register.json
    docs/v3/research-lane/overnight-20261002/rp/source-register.md
    docs/v3/research-lane/overnight-20261002/rq/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rq/candidate-contracts.json
    docs/v3/research-lane/overnight-20261002/rq/coverage.json
    docs/v3/research-lane/overnight-20261002/rq/input-pins.json
    docs/v3/research-lane/overnight-20261002/rq/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rq/recommendations.json
    docs/v3/research-lane/overnight-20261002/rq/report.md
    docs/v3/research-lane/overnight-20261002/rq/source-register.json
    docs/v3/research-lane/overnight-20261002/rq/source-register.md
    docs/v3/research-lane/overnight-20261002/rq/synthetic-fixtures.json
    docs/v3/research-lane/overnight-20261002/rr/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rr/coverage.json
    docs/v3/research-lane/overnight-20261002/rr/local-input-register.json
    docs/v3/research-lane/overnight-20261002/rr/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rr/proposed-contracts.json
    docs/v3/research-lane/overnight-20261002/rr/recommendations.json
    docs/v3/research-lane/overnight-20261002/rr/report.md
    docs/v3/research-lane/overnight-20261002/rr/source-register.json
    docs/v3/research-lane/overnight-20261002/rr/source-register.md
    docs/v3/research-lane/overnight-20261002/rs/coverage.json
    docs/v3/research-lane/overnight-20261002/rs/decision-contract.json
    docs/v3/research-lane/overnight-20261002/rs/fixtures.json
    docs/v3/research-lane/overnight-20261002/rs/pinned-inputs.json
    docs/v3/research-lane/overnight-20261002/rs/report.md
    docs/v3/research-lane/overnight-20261002/rs/source-register.json
    docs/v3/research-lane/overnight-20261002/rt/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rt/artifact-contract.json
    docs/v3/research-lane/overnight-20261002/rt/coverage.json
    docs/v3/research-lane/overnight-20261002/rt/field-map.json
    docs/v3/research-lane/overnight-20261002/rt/input-pins.json
    docs/v3/research-lane/overnight-20261002/rt/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rt/recommendations.json
    docs/v3/research-lane/overnight-20261002/rt/report.md
    docs/v3/research-lane/overnight-20261002/rt/source-register.json
    docs/v3/research-lane/overnight-20261002/rt/source-register.md
    docs/v3/research-lane/overnight-20261002/ru/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/ru/candidate-contracts.json
    docs/v3/research-lane/overnight-20261002/ru/coverage.json
    docs/v3/research-lane/overnight-20261002/ru/input-pins.json
    docs/v3/research-lane/overnight-20261002/ru/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/ru/recommendations.json
    docs/v3/research-lane/overnight-20261002/ru/recommendations.md
    docs/v3/research-lane/overnight-20261002/ru/report.md
    docs/v3/research-lane/overnight-20261002/ru/source-register.json
    docs/v3/research-lane/overnight-20261002/ru/source-register.md
    docs/v3/research-lane/overnight-20261002/rv/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rv/candidate-contracts.json
    docs/v3/research-lane/overnight-20261002/rv/coverage.json
    docs/v3/research-lane/overnight-20261002/rv/input-pins.json
    docs/v3/research-lane/overnight-20261002/rv/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rv/recommendations.json
    docs/v3/research-lane/overnight-20261002/rv/report.md
    docs/v3/research-lane/overnight-20261002/rv/source-register.json
    docs/v3/research-lane/overnight-20261002/rv/source-register.md
    docs/v3/research-lane/overnight-20261002/rv/synthetic-fixtures.json
    docs/v3/research-lane/overnight-20261002/rw/budget-register.json
    docs/v3/research-lane/overnight-20261002/rw/coverage.json
    docs/v3/research-lane/overnight-20261002/rw/endpoint-contract.json
    docs/v3/research-lane/overnight-20261002/rw/local-handoffs.json
    docs/v3/research-lane/overnight-20261002/rw/preregistration-template.json
    docs/v3/research-lane/overnight-20261002/rw/report.md
    docs/v3/research-lane/overnight-20261002/rw/repository-source-manifest.json
    docs/v3/research-lane/overnight-20261002/rw/source-register.json
    docs/v3/research-lane/overnight-20261002/rw/source-register.md
    docs/v3/research-lane/overnight-20261002/rx/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/rx/candidate-contracts.json
    docs/v3/research-lane/overnight-20261002/rx/companion-contract-pins.json
    docs/v3/research-lane/overnight-20261002/rx/coverage.json
    docs/v3/research-lane/overnight-20261002/rx/input-pins.json
    docs/v3/research-lane/overnight-20261002/rx/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/rx/recommendations.json
    docs/v3/research-lane/overnight-20261002/rx/report.md
    docs/v3/research-lane/overnight-20261002/rx/source-register.json
    docs/v3/research-lane/overnight-20261002/rx/source-register.md
    docs/v3/research-lane/overnight-20261002/rx/synthetic-fixtures.json
    docs/v3/research-lane/overnight-20261002/ry/UNEXECUTED-local-test-handoffs.md
    docs/v3/research-lane/overnight-20261002/ry/candidate-contracts.json
    docs/v3/research-lane/overnight-20261002/ry/coverage.json
    docs/v3/research-lane/overnight-20261002/ry/input-pins.json
    docs/v3/research-lane/overnight-20261002/ry/local-test-handoffs.json
    docs/v3/research-lane/overnight-20261002/ry/recommendations.json
    docs/v3/research-lane/overnight-20261002/ry/report.md
    docs/v3/research-lane/overnight-20261002/ry/source-register.json
    docs/v3/research-lane/overnight-20261002/ry/source-register.md
    docs/v3/research-lane/overnight-20261002/ry/synthetic-fixtures.json
    docs/v3/research-lane/overnight-20261002/semantic-guard-research/SEMANTIC-GUARD-COVERAGE.md
    docs/v3/research-lane/overnight-20261002/semantic-guard-research/SHA256SUMS.txt
    docs/v3/research-lane/overnight-20261002/semantic-guard-research/coverage-matrix.json
    docs/v3/research-lane/overnight-20261002/semantic-validation-hardening/SEMANTIC-VALIDATION-HARDENING.md
    docs/v3/research-lane/overnight-20261002/semantic-validation-hardening/SHA256SUMS.txt
    docs/v3/research-lane/overnight-20261002/semantic-validation-hardening/SOURCE-REPORT-PINS.md
    docs/v3/research-lane/overnight-20261002/shared-evidence/source-facts.md
    docs/v3/research-lane/overnight-20261002/synthesis/INTEGRATION-STATUS.md
    docs/v3/research-lane/overnight-20261002/synthesis/INTEGRATION-TEST-HANDOFF.md
    docs/v3/research-lane/overnight-20261002/synthesis/MODEL-ARCHITECTURE.md
    docs/v3/research-lane/overnight-20261002/synthesis/OWNERSHIP-AND-FIELD-MAP.md
    docs/v3/research-lane/overnight-20261002/synthesis/field-compatibility.json
    docs/v3/research-lane/overnight-20261002/synthesis/input-register.json
    docs/v3/research-lane/scope-reframe-20261002/RETURN-STATE.txt
    docs/v3/research-lane/scope-reframe-20261002/START-HERE.txt
    docs/v3/research-lane/scope-reframe-20261002/raw/01-current-strategy/Recursion_Interface_and_Product_Strategy.md
    docs/v3/research-lane/scope-reframe-20261002/raw/01-current-strategy/figures/focused-task.png
    docs/v3/research-lane/scope-reframe-20261002/raw/01-current-strategy/figures/handoff-inspector.png
    docs/v3/research-lane/scope-reframe-20261002/raw/02-comparison-evidence/Recursion_Harness_Path_Evidence_20261002.json
    docs/v3/research-lane/scope-reframe-20261002/raw/03-prior-baseline/Architecture_Audit.md
    docs/v3/research-lane/scope-reframe-20261002/raw/03-prior-baseline/Research_Directions.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU01-report.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU02-report.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU03-report.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/negative-traces.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/negative-traces.schema.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/platform-manifest.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/platform-manifest.schema.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/profile-fields.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-machine-specifications/source-register.json
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-report.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU05-report.md
    docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/Supporting_Review_and_Local_Handoff.md
""".split())

# Source originals listed by the existing intake manifests but intentionally
# absent from this checkout. These exact mapped paths are tolerated only while
# they remain absent and listed; adding a file or removing an inventory entry
# makes the allowlist stale.
MANIFEST_KNOWN_MISSING_SOURCE_ORIGINALS = frozenset({
    "docs/v3/research-lane/dot-ra-rd-20261002/raw/R-A/R-A-plugin-portability.docx",
    "docs/v3/research-lane/dot-ra-rd-20261002/raw/R-B/R-B-cross-harness-delegation.docx",
    "docs/v3/research-lane/dot-ra-rd-20261002/raw/R-C/recursion-route-terms-counsel-packet.docx",
    "docs/v3/research-lane/dot-ra-rd-20261002/raw/R-D/rd-quota-efficiency-measurement.docx",
    "docs/v3/research-lane/dot-ra-rd-20261002/raw/00-assessment/Recursion_Packet_Assessment_and_Handoff.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/01-current-strategy/Recursion_Interface_and_Product_Strategy.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/03-prior-baseline/Architecture_Audit.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/03-prior-baseline/Research_Directions.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU01-runtime-evidence-and-experiment.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU02_Canonical_State_Store_Crash_Replay.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU03-formal-model-and-byte-conformance.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU04-operating-system-platform-conformance.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/FU05_Broker_Protocol_and_Native_Chaos.docx",
    "docs/v3/research-lane/scope-reframe-20261002/raw/04-foundation-research/Supporting_Review_and_Local_Handoff.docx",
    "docs/v3/research-lane/followups-20261002/reports/FU01-runtime-evidence-and-experiment.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/FU01-report.md",
    "docs/v3/research-lane/followups-20261002/reports/FU02_Canonical_State_Store_Crash_Replay.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/FU02-report.md",
    "docs/v3/research-lane/followups-20261002/reports/FU03-formal-model-and-byte-conformance.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/FU03-report.md",
    "docs/v3/research-lane/followups-20261002/reports/FU04-operating-system-platform-conformance.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/FU04-report.md",
    "docs/v3/research-lane/followups-20261002/reports/FU05_Broker_Protocol_and_Native_Chaos.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/FU05-report.md",
    "docs/v3/research-lane/followups-20261002/reports/Followup_Batch_Supporting_Review_and_Local_Handoff.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/Batch-supporting-review.md",
    "docs/v3/research-lane/followups-20261002/reports/Recursion_v3_Architecture_Audit.docx",
    "docs/v3/research-lane/followups-20261002/editable-text/Recursion-architecture-audit.md",
})

# Packet check_intake.py layouts: (base for nested paths, prefix rewrites).
# Root entries stay local. Unlisted packets use manifest-local paths only.
MANIFEST_PATH_MAPPINGS = {
    "dot-ra-rd-20261002": ("raw", {}),
    "scope-reframe-20261002": ("raw", {}),
    "overnight-20261002": ("", {}),
    "followups-20261002": ("", {"FU04-machine-specifications": "machine"}),
}


def digest(value: Any) -> str:
    """Digest the complete JSON proposal, including identity, scope and bindings."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def text_present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


def commit_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def proof_environment(value: Any) -> bool:
    return text_present(value) or (isinstance(value, dict)
            and text_present(value.get("id")) and text_present(value.get("execution_class"))
            and sha256(value.get("configuration_sha256")))


def calendar_date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def continuation_declared(value: dict[str, Any]) -> bool:
    return ("continuation" in value and value["continuation"] is not False
            or any(field in value for field in CONTINUATION_FIELDS | {"continuation_packet", "continuation_receipt"}))


def contract_binding(contract: dict[str, Any]) -> dict[str, Any]:
    return {key: contract.get(key) for key in ("id", "revision", "sha256")}


def target_digest(action: str, target: dict[str, Any]) -> str:
    if action == "accept_no_rollback":
        return digest({key: target.get(key) for key in (
            "id", "candidate_commit", "contract_ids", "qualification_requirements", "release_requirements")})
    # Exclude only observation/lifecycle fields, never the approved work boundary.
    excluded = {
        "accept_contract": {"status", "decision_receipt"},
        "develop_work": {"status", "stage", "decision_receipt", "contract_receipts",
                         "candidate_commit", "verification_evidence_ids"},
        "decide_capability": {"current_maturity", "month_target", "evidence_ids",
                              "decision_receipt", "candidate_commit", "no_rollback_receipt"},
        "admit_work": {"decision_receipt", "active_contract_consumers", "active_reviews"},
    }
    return digest({key: value for key, value in target.items() if key not in excluded[action]})


def work_binding(item: dict[str, Any], work: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def boundary(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items()
                if key not in {"status", "candidate_commit", "verification_evidence_ids"}}

    # Flatten the dependency closure so upstream candidate/boundary changes also
    # invalidate dependent and composed proofs. Cycles are reported separately.
    seen = {item.get("id")}
    pending = list(item.get("hard_dependencies", []))
    dependencies = []
    while pending:
        dependency = pending.pop()
        if dependency in seen or dependency not in work:
            continue
        seen.add(dependency)
        row = work[dependency]
        dependencies.append({"id": dependency, "candidate_commit": row.get("candidate_commit"),
                             "boundary_digest": digest(boundary(row))})
        pending.extend(row.get("hard_dependencies", []))
    return {"id": item.get("id"), "candidate_commit": item.get("candidate_commit"),
            "work_digest": digest({"work": boundary(item), "dependency_bindings":
                                   sorted(dependencies, key=lambda row: row["id"])})}


def checkpoint_binding(checkpoint: dict[str, Any]) -> dict[str, Any]:
    boundary = {key: value for key, value in checkpoint.items()
                if key not in {"status", "evidence_ids"}}
    return {"id": checkpoint.get("id"), "definition_digest": digest(boundary)}


def unknown_binding(unknown: dict[str, Any]) -> dict[str, Any]:
    # Resolving an existing unknown need not change its approved blocking scope.
    return {key: value for key, value in unknown.items()
            if key not in {"status", "resolution_evidence"}}


def _ids(rows: list[dict[str, Any]], name: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get("id")
        if not text_present(value):
            errors.append(f"{name} row missing id")
        elif value in result:
            errors.append(f"duplicate {name} id: {value}")
        else:
            result[value] = row
    return result


def reference_errors(data: dict[str, Any]) -> list[str]:
    """Check reference shapes and issue syntax, including historical bindings.

    External routing/parent references need not be work rows in this ledger.
    Current work cross-links and approval freshness are checked by their consumers.
    """
    errors: list[str] = []

    def issue(value: Any, path: str) -> None:
        if not isinstance(value, str) or not CSL_ISSUE.fullmatch(value):
            errors.append(f"{path} must be a CSL issue")

    scalar_fields = {"parent_issue", "implementation_issue", "decision_issue",
                     "linear_issue", "resolution_issue", "issue", "work_item_id"}
    list_fields = {"hard_dependencies", "work_item_ids", "active_contract_consumers", "active_reviews"}
    resource_lists = {"contract_ids", "unknown_ids", "evidence_ids", "research_ids", "consumes_contracts",
                      "verification_evidence_ids", "recovery_evidence_ids", "rollback_evidence_ids", "requires"}
    required_lists = {
        "capabilities": ("contract_ids", "unknown_ids", "evidence_ids", "research_ids"),
        "work_items": ("hard_dependencies", "consumes_contracts"),
        "integration_checkpoints": ("requires",),
    }

    def walk(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, row in enumerate(value):
                walk(row, f"{path}[{index}]")
        elif isinstance(value, dict):
            for field in ("status", "kind", "stage", "mode", "discretion_class", "current_maturity", "month_target", "blocks", "severity"):
                if field in value and not isinstance(value[field], str):
                    errors.append(f"{path}.{field} must be a string")
            for field in ("decision_receipt", "proposal_id", "no_rollback_receipt", "capability_id", "checkpoint_id"):
                if field in value and value[field] is not None and not text_present(value[field]):
                    errors.append(f"{path}.{field} must be a nonempty reference ID or null")
            for field in ("qualification_requirements", "release_requirements", "acceptance_commands"):
                if field in value and value[field] is not None and not isinstance(value[field], dict):
                    errors.append(f"{path}.{field} must be an object or null")
            for field in ("research_plan", "contract_receipts"):
                if field in value and not isinstance(value[field], dict):
                    errors.append(f"{path}.{field} must be an object")
            if "checkpoint_requirements" in value:
                requirements = value["checkpoint_requirements"]
                if (not isinstance(requirements, list) or not all(isinstance(r, dict) for r in requirements)):
                    errors.append(f"{path}.checkpoint_requirements must be a list of objects")
                else:
                    for requirement in requirements:
                        if not text_present(requirement.get("checkpoint_id")):
                            errors.append(f"{path} checkpoint requires an ID")
                        threshold = requirement.get("required_from_tier")
                        if threshold is not None and not isinstance(threshold, str):
                            errors.append(f"{path} checkpoint threshold must be a string or null")
            if "authorizations" in value:
                grants = value["authorizations"]
                if (not isinstance(grants, list) or not all(isinstance(g, dict)
                        and text_present(g.get("action")) and text_present(g.get("target")) for g in grants)):
                    errors.append(f"{path}.authorizations must be a list of named grants")
            if value.get("action") == "develop_work":
                issue(value.get("target"), f"{path}.target")
            if value.get("kind") == "work_verification" and "work_item_id" not in value:
                issue(None, f"{path}.work_item_id")
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in scalar_fields:
                    issue(child, child_path)
                elif key in list_fields:
                    if not isinstance(child, list):
                        errors.append(f"{child_path} must be a list of CSL issues")
                    else:
                        for index, item in enumerate(child):
                            issue(item, f"{child_path}[{index}]")
                elif key in resource_lists:
                    if (not isinstance(child, list)
                            or not all(isinstance(item, str) and item.strip() for item in child)):
                        errors.append(f"{child_path} must be a list of nonempty reference IDs")
                elif key in {"work_bindings", "unknown_bindings"}:
                    if not isinstance(child, list) or not all(isinstance(row, dict) for row in child):
                        errors.append(f"{child_path} must be a list of binding objects")
                    else:
                        for index, row in enumerate(child):
                            field = "id" if key == "work_bindings" else "resolution_issue"
                            issue(row.get(field), f"{child_path}[{index}].{field}")
                walk(child, child_path)

    # Required routing fields are checked even before a candidate is approved.
    for collection, fields in (("contracts", ("decision_issue",)), ("capabilities", ("decision_issue",)),
                               ("unknowns", ("resolution_issue",)), ("integration_checkpoints", ("issue",)),
                               ("proposals", ("linear_issue",)), ("work_items", ("parent",)),
                               ("decisions", ()), ("evidence", ()), ("research", ()), ("risks", ())):
        rows = data.get(collection)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            errors.append(f"{collection} must be a list of objects")
            continue
        for index, row in enumerate(rows):
            if collection == "work_items":
                issue(row.get("id"), f"work item {row.get('id')}")
            for field in required_lists.get(collection, ()):
                if field not in row:
                    errors.append(f"{collection}[{index}].{field} must be a list of nonempty reference IDs")
            for field in fields:
                if field not in row or field == "parent":
                    issue(row.get(field), f"{collection}[{index}].{field}")
    admission = data.get("admission")
    if not isinstance(admission, dict):
        errors.append("admission must be an object")
    elif "decision_issue" not in admission:
        issue(None, "admission.decision_issue")
    control = data.get("research_control")
    if not isinstance(control, dict):
        errors.append("research_control must be an object")
    else:
        for field in ("required_plan_fields", "continuation_triggers", "continuation_packet_fields", "required_receipt_fields"):
            values = control.get(field)
            if not isinstance(values, list) or not values or not all(text_present(v) for v in values):
                errors.append(f"research control requires non-empty {field} as strings")
    # Pending/historical receipts may omit linear_issue; present values must be
    # valid. Approved receipts additionally require it in receipt_fields below.
    walk(data, "program")
    return errors


def _safe_join(parent: Path, name: str, root: Path,
              errors: list[str], source: str) -> Path | None:
    """Join `name` onto `parent`, or None if `name` is an absolute path
    (POSIX, Windows-drive, or UNC), escapes `root`, or cannot be resolved.
    Append a deterministic diagnostic to `errors` on every rejection.

    `root` must already be resolved by the caller. Joining pathlib.Path with
    an absolute `name` silently discards `parent` instead of raising, and a
    `..`-laden `name` can lexically still look like it is under `root`; both
    are checked here so a malicious or malformed manifest entry is reported
    as a validation error rather than crashing or escaping the repository.
    """
    if ABSOLUTE_ENTRY.match(name):
        errors.append(f"{source} is outside the repository")
        return None
    joined = parent / name
    try:
        candidate = joined.resolve(strict=True)
    except FileNotFoundError:
        # Keep absent paths representable so callers can issue a specific
        # missing-payload diagnostic. Non-strict resolution still follows
        # existing symlinks, so a missing target cannot hide an escape.
        try:
            candidate = joined.resolve(strict=False)
        except (RuntimeError, OSError, ValueError):
            errors.append(f"{source} cannot be resolved")
            return None
    except (RuntimeError, OSError, ValueError):
        # Strict resolution detects symlink loops on Python versions where
        # non-strict resolution may otherwise accept them.
        errors.append(f"{source} cannot be resolved")
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        errors.append(f"{source} is outside the repository")
        return None
    return candidate


def _manifest_payload_paths(manifest: Path, root: Path, errors: list[str],
                            known_missing_seen: set[str] | None = None) -> list[Path]:
    """Resolved, existing, in-root file paths pinned by one MANIFEST.json's
    files[].path + sha256 entries.

    Each entry is resolved the way its own packet's check_intake.py resolves
    it: dot/scope nested paths go unconditionally under raw/, followups'
    FU04-machine-specifications/ prefix becomes machine/, and overnight
    paths stay local. Other manifests default to manifest-local paths.
    There is no existence-based guessing between locations. Existing
    payloads are included regardless of extension. Absent mapped source
    originals must be in MANIFEST_KNOWN_MISSING_SOURCE_ORIGINALS; other
    absent payloads are validation errors. Rejected containment and
    resolution failures are deterministic validation errors.
    """
    manifest_rel = manifest.relative_to(root).as_posix()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        errors.append(f"{manifest_rel}: inventory cannot be read")
        return []
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        errors.append(f"{manifest_rel}: inventory requires a files list")
        return []
    nested_dir, prefixes = MANIFEST_PATH_MAPPINGS.get(manifest.parent.name, ("", {}))
    resolved_paths: list[Path] = []
    for index, entry in enumerate(files):
        if not (isinstance(entry, dict) and text_present(entry.get("path"))
                and sha256(entry.get("sha256"))):
            errors.append(f"{manifest_rel}: files[{index}] requires path and sha256")
            continue
        name = entry["path"]
        parent, mapped_name = manifest.parent, name
        if "/" in name:
            prefix, tail = name.split("/", 1)
            if prefix in prefixes:
                parent, mapped_name = parent / prefixes[prefix], tail
            elif nested_dir:
                parent = parent / nested_dir
        source = f"{manifest_rel}: entry '{name}'"
        candidate = _safe_join(parent, mapped_name, root, errors, source)
        if candidate is None:
            continue
        if candidate.is_file():
            resolved_paths.append(candidate)
            continue
        candidate_rel = candidate.relative_to(root).as_posix()
        if candidate_rel in MANIFEST_KNOWN_MISSING_SOURCE_ORIGINALS:
            if known_missing_seen is not None:
                known_missing_seen.add(candidate_rel)
        else:
            errors.append(f"{source} payload is missing")
    return resolved_paths


def _files_named_exactly(root: Path, filename: str) -> list[Path]:
    """Find files by their actual on-disk names in deterministic order.

    Path.glob can return a pattern-cased basename for a case-insensitive
    match on Windows. os.walk preserves the spelling returned by the
    filesystem, so comparing each filename here is reliable on every OS.
    """
    matches: list[Path] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        matches.extend(Path(directory) / name for name in filenames if name == filename)
    return sorted(matches, key=lambda path: path.relative_to(root).as_posix())


def _filesystem_pinned_path_inventory(root: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Filesystem-derived inventory of sha256-pinned paths, manifest paths,
    and deterministic resolution errors for a resolved root string.
    """
    root_path = Path(root)
    errors: list[str] = []
    paths: list[str] = []
    for manifest in _files_named_exactly(root_path, "SHA256SUMS.txt"):
        if ".git" in manifest.relative_to(root_path).parts:
            continue
        manifest_rel = manifest.relative_to(root_path).as_posix()
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            match = SUM_LINE.fullmatch(line.strip())
            if not match:
                continue
            name = match.group(2).strip()
            source = f"{manifest_rel}: entry '{name}'"
            entry = _safe_join(manifest.parent, name, root_path, errors, source)
            if entry is None:
                continue
            # Manifests written from their parent directory carry the
            # packet directory as a prefix on every entry; use that
            # resolution only when the local one is absent (a packet may
            # legitimately nest a same-named directory).
            if (not entry.exists()
                    and name.startswith(manifest.parent.name + "/")):
                fallback = _safe_join(manifest.parent.parent, name, root_path, errors, source)
                if fallback is None:
                    continue
                if fallback.exists():
                    entry = fallback
            paths.append(entry.relative_to(root_path).as_posix())
    manifest_paths: list[str] = []
    known_missing_seen: set[str] = set()
    for manifest in _files_named_exactly(root_path, "MANIFEST.json"):
        if ".git" in manifest.relative_to(root_path).parts:
            continue
        for resolved in _manifest_payload_paths(manifest, root_path, errors, known_missing_seen):
            manifest_paths.append(resolved.relative_to(root_path).as_posix())
    for path in sorted(MANIFEST_KNOWN_MISSING_SOURCE_ORIGINALS - known_missing_seen):
        packet = "/".join(path.split("/")[:4])
        if (root_path / packet).is_dir():
            errors.append(f"{path} known missing manifest source original is stale: "
                          "remove it from MANIFEST_KNOWN_MISSING_SOURCE_ORIGINALS")
    paths.extend(manifest_paths)
    audits = root_path / "docs/audits"
    if audits.is_dir():
        paths.extend(path.relative_to(root_path).as_posix()
                     for path in sorted(audits.rglob("*")) if path.is_file())
    return tuple(paths), tuple(manifest_paths), tuple(errors)


@dataclasses.dataclass(frozen=True)
class PinnedInventory:
    """One filesystem scan of sha256-pinned paths under a resolved root, owned by the caller that built it.

    git check-attr results are queried lazily on the first check_attr() per
    path tuple and then fixed for this object; callers receive copies. A reused
    inventory never observes later filesystem or attribute changes: passing
    inventory= is a caller trust boundary for a tree the caller guarantees
    unchanged. Validations without inventory= (including main()) scan afresh.
    """
    root: str
    paths: tuple[str, ...]
    manifest_paths: frozenset[str]
    errors: tuple[str, ...]
    _attrs: dict[tuple[str, ...], tuple[dict[str, dict[str, str]], str]] = dataclasses.field(
        default_factory=dict, compare=False, repr=False)

    def check_attr(self, paths: tuple[str, ...]) -> tuple[dict[str, dict[str, str]], str]:
        if paths not in self._attrs:
            self._attrs[paths] = _git_check_attr(self.root, paths)
        resolved, failure = self._attrs[paths]
        return {path: dict(attrs) for path, attrs in resolved.items()}, failure


def pinned_inventory(root: Path) -> PinnedInventory:
    """Scan root now; never reuses an earlier scan."""
    resolved = str(root.resolve())
    paths, manifest_paths, errors = _filesystem_pinned_path_inventory(resolved)
    return PinnedInventory(resolved, paths, frozenset(manifest_paths), errors)


def _inventory_for_root(root: Path, inventory: PinnedInventory | None) -> PinnedInventory:
    resolved = str(root.resolve())
    if inventory is None:
        return pinned_inventory(root)
    if inventory.root != resolved:
        raise ValueError(f"pinned inventory root {inventory.root!r} does not match {resolved!r}")
    return inventory


def _pinned_path_details(data: dict[str, Any], root: Path, *,
                         inventory: PinnedInventory | None = None
                         ) -> tuple[list[str], frozenset[str], list[str]]:
    """Repo-relative paths whose delivered bytes are sha256-pinned, the
    subset of those derived from MANIFEST.json inventories, and any
    deterministic resolution errors.

    Sources: program contracts hashed by this validator, intake SHA256SUMS
    manifest payloads, whole-packet MANIFEST.json inventories (files[].path
    + files[].sha256), and sealed receipts (byte-level evidence under
    docs/audits). MANIFEST.json entries pin ordinary tracked text files too
    (for example overnight-20261002/CHANGELOG.md), not only binary payloads,
    so this enumeration includes them. PINNED_EOL_KNOWN_GAPS is a frozen
    path-by-path snapshot of currently uncovered pins.
    """
    root = root.resolve()
    inventory = _inventory_for_root(root, inventory)
    paths: list[str] = []
    for contract in data.get("contracts", []):
        if isinstance(contract, dict) and text_present(contract.get("path")):
            paths.append(contract["path"])
    paths.extend(inventory.paths)
    return sorted(set(paths)), inventory.manifest_paths, list(inventory.errors)


def sha256_pinned_paths(data: dict[str, Any], root: Path, *,
                        inventory: PinnedInventory | None = None) -> list[str]:
    """Repo-relative paths whose delivered bytes are sha256-pinned.

    See _pinned_path_details for the full source list. An entry that is
    absolute or escapes the repository via `..` is dropped here; call
    pinned_path_errors for the matching deterministic errors.
    """
    paths, _manifest_paths, _errors = _pinned_path_details(data, root, inventory=inventory)
    return paths


def pinned_path_errors(data: dict[str, Any], root: Path, *,
                       inventory: PinnedInventory | None = None) -> list[str]:
    """Deterministic, git-free resolution errors for pinned-path entries:
    a SHA256SUMS.txt or MANIFEST.json entry that is absolute (POSIX,
    Windows-drive, or UNC), escapes the repository, or cannot be resolved
    is reported here instead of raising out of sha256_pinned_paths.
    Unreadable or malformed MANIFEST.json inventories also fail closed.
    """
    _paths, _manifest_paths, errors = _pinned_path_details(data, root, inventory=inventory)
    return errors


def _git_check_attr(root: str, paths: tuple[str, ...]) -> tuple[dict[str, dict[str, str]], str]:
    """One batched `git check-attr` call over every pinned path.

    Returns a per-path attribute map, or an error string when git cannot run
    in the given root (fail closed: the attribute of a pinned path is then
    unverifiable).
    """
    if not paths:
        return {}, ""
    try:
        result = subprocess.run(
            ["git", "-C", root, "-c", "core.quotePath=false",
             "check-attr", "text", "eol", "--", *paths],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"git check-attr failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return {}, f"git check-attr failed: {detail}"
    resolved: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.rsplit(": ", 2)
        if len(parts) == 3 and parts[1] in {"text", "eol"}:
            resolved.setdefault(parts[0], {})[parts[1]] = parts[2]
    return resolved, ""


def pinned_eol_errors(data: dict[str, Any], root: Path, *,
                      inventory: PinnedInventory | None = None) -> list[str]:
    """Assert every sha256-pinned text path resolves `eol=lf` (or is explicitly
    binary via `-text`) so cross-platform checkouts cannot deliver CRLF bytes
    against the recorded digest. CSL-225's Windows-only digest break is the
    failure this guards against.

    Paths listed in the frozen PINNED_EOL_KNOWN_GAPS snapshot are tolerated
    while uncovered; newly uncovered paths fail immediately. Snapshot entries
    become stale when they are covered or no longer pinned.
    An out-of-root pinned-path entry (see pinned_path_errors) is reported
    here too, never raised.
    """
    root = root.resolve()
    inventory = _inventory_for_root(root, inventory)
    paths, manifest_paths, errors = _pinned_path_details(data, root, inventory=inventory)
    resolved, failure = inventory.check_attr(tuple(paths))
    if failure:
        return errors + [failure]
    for path in paths:
        attrs = resolved.get(path)
        if attrs is None:
            errors.append(f"{path} missing from git check-attr output")
            continue
        if not (root / path).exists():
            errors.append(f"{path} pinned path does not exist")
            continue
        covered = attrs.get("eol") == "lf" or attrs.get("text") == "unset"
        if path in PINNED_EOL_KNOWN_GAPS:
            if covered:
                errors.append(f"{path} known eol gap is covered: remove it from PINNED_EOL_KNOWN_GAPS")
        elif not covered:
            errors.append(f"{path} sha256-pinned path lacks eol=lf "
                          f"(git check-attr eol: {attrs.get('eol')})")
    for path in sorted(PINNED_EOL_KNOWN_GAPS - set(paths)):
        if (root / path).parent.is_dir():
            errors.append(f"{path} known eol gap is stale (unpinned or removed): "
                          "remove it from PINNED_EOL_KNOWN_GAPS")
    return errors


def validate_program(data: dict[str, Any], root: Path = ROOT, *,
                     inventory: PinnedInventory | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["program must be an object"]
    required = {
        "schema_version", "program_id", "status", "parent_issue", "implementation_issue",
        "source_commit", "research", "research_control", "maturity_order", "contracts", "decisions", "admission",
        "work_items", "capabilities", "evidence", "unknowns", "risks", "integration_checkpoints", "proposals",
    }
    missing = sorted(required - data.keys())
    if missing:
        errors.append(f"missing top-level fields: {', '.join(missing)}")
        return errors
    errors.extend(reference_errors(data))
    if errors:
        return errors
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        errors.append("schema_version must be 1")
    if not text_present(data["program_id"]):
        return errors + ["program_id must be a nonempty string"]
    if data["status"] != "candidate":
        errors.append("program status must be candidate; program ratification is not supported by this schema")
    if not commit_hash(data["source_commit"]):
        errors.append("source_commit must be a full lowercase commit hash")

    order = data["maturity_order"]
    if order != MATURITY_ORDER:
        errors.append("maturity_order must match the defined maturity tiers in order")
        return errors
    rank = {value: index for index, value in enumerate(order)}
    research_control = data["research_control"]
    if research_control.get("default") != "approved_issue_pass_only":
        errors.append("research must default to the approved issue pass")
    if research_control.get("continuation_requires_human") is not True:
        errors.append("research continuation must require human approval")
    if research_control.get("approval_channel") != "human_comment_on_matching_linear_issue":
        errors.append("research approval must use a human comment on the matching Linear issue")
    if research_control.get("status_is_authority") is not False:
        errors.append("Linear status must not grant research authority")
    for field in ("required_plan_fields", "continuation_triggers", "continuation_packet_fields", "required_receipt_fields"):
        if not isinstance(research_control.get(field), list) or not research_control[field]:
            errors.append(f"research control requires non-empty {field}")
    plan_fields = research_control.get("required_plan_fields", [])
    if not isinstance(plan_fields, list) or not PLAN_FIELDS.issubset(plan_fields):
        errors.append("research required_plan_fields must include question, source_class, quota_class, stop_condition, evidence_artifact")
    packet_fields = research_control["continuation_packet_fields"]
    if len(packet_fields) != len(CONTINUATION_FIELDS) or set(packet_fields) != CONTINUATION_FIELDS:
        errors.append("research continuation_packet_fields must equal the supported field set without duplicates")
    contracts = _ids(data["contracts"], "contract", errors)
    decisions = _ids(data["decisions"], "decision", errors)
    work = _ids(data["work_items"], "work item", errors)
    evidence = _ids(data["evidence"], "evidence", errors)
    unknowns = _ids(data["unknowns"], "unknown", errors)
    capabilities = _ids(data["capabilities"], "capability", errors)
    proposals = _ids(data["proposals"], "proposal", errors)
    checkpoints = _ids(data["integration_checkpoints"], "checkpoint", errors)
    research = _ids(data["research"], "research", errors)
    _ids(data["risks"], "risk", errors)
    if errors:
        return errors
    for rid, row in research.items():
        if row.get("status") not in {"unverified", "current", "superseded"}:
            errors.append(f"{rid} research status must be unverified, current or superseded")
    implementation = work.get(data["implementation_issue"]) if isinstance(data["implementation_issue"], str) else None
    if implementation is None:
        errors.append("implementation_issue must reference a work item")
    elif implementation.get("parent") != data["parent_issue"]:
        errors.append("implementation work parent must match parent_issue")

    for contract_id, contract in contracts.items():
        if not text_present(contract.get("path")):
            errors.append(f"{contract_id} path must be a nonempty string")
            continue
        path = root / contract["path"]
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"{contract_id} path is missing or outside the repository")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if contract.get("sha256") != actual:
            errors.append(f"{contract_id} digest mismatch: expected {actual}")
        if contract.get("status") not in {"candidate", "accepted", "superseded"}:
            errors.append(f"{contract_id} has invalid status")
        if type(contract.get("revision")) is not int or contract["revision"] < 1:
            errors.append(f"{contract_id} revision must be a positive integer")

    inventory = _inventory_for_root(root, inventory)
    errors.extend(pinned_eol_errors(data, root, inventory=inventory))

    def bindings_match(bindings: Any, contract_ids: list[str]) -> bool:
        return (isinstance(bindings, list) and len(bindings) == len(contract_ids)
                and all(isinstance(row, dict) and text_present(row.get("id"))
                        and type(row.get("revision")) is int and row["revision"] >= 1
                        and sha256(row.get("sha256")) for row in bindings)
                and all(cid in contracts and contract_binding(contracts[cid]) in bindings
                        for cid in contract_ids)
                and len(set(contract_ids)) == len(contract_ids))

    def exact_bindings(actual: Any, expected: list[dict[str, Any]]) -> bool:
        return isinstance(actual, list) and len(actual) == len(expected) and all(row in actual for row in expected)

    resolved_unknowns = set()
    for unknown_id, unknown in unknowns.items():
        if unknown.get("status") != "resolved":
            continue
        reference = unknown.get("resolution_evidence")
        row = evidence.get(reference, {}) if text_present(reference) else {}
        linked_contracts = sorted({cid for capability in capabilities.values()
                                   if unknown_id in capability["unknown_ids"]
                                   for cid in capability["contract_ids"]})
        if (row.get("kind") == "unknown_resolution" and row.get("unknown_id") == unknown_id
                and row.get("unknown_digest") == digest(unknown_binding(unknown))
                and row.get("resolution_issue") == unknown["resolution_issue"]
                and row.get("status") == "current" and row.get("result") == "pass"
                and commit_hash(row.get("source_commit")) and row["source_commit"] == data["source_commit"]
                and text_present(row.get("artifact")) and sha256(row.get("artifact_sha256"))
                and bindings_match(row.get("contract_bindings"), linked_contracts)):
            resolved_unknowns.add(unknown_id)
        else:
            errors.append(f"{unknown_id} resolved without resolution evidence bound to its current definition, source and contracts")

    def successful_proof(row: dict[str, Any], candidate: Any) -> bool:
        return (commit_hash(candidate)
                and row.get("candidate_commit") == candidate and row.get("result") == "pass"
                and row.get("status") == "current" and row.get("independent") is True
                and text_present(row.get("acceptance_command"))
                and text_present(row.get("artifact")) and proof_environment(row.get("environment"))
                and sha256(row.get("artifact_sha256")))

    def qualification_matches(row: dict[str, Any], capability: dict[str, Any], tier: str) -> bool:
        classes = {"locally_qualified": ("local_observation", "local_runtime"),
                   "release_qualified": ("release_evidence", "release_runtime")}
        if tier not in classes:
            return True
        profiles = capability.get("qualification_requirements")
        profile = profiles.get(tier) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            return False
        evidence_class, execution_class = classes[tier]
        environment = profile.get("environment")
        return (profile.get("evidence_class") == evidence_class
                and isinstance(environment, dict)
                and isinstance(environment.get("id"), str) and bool(environment["id"].strip())
                and environment.get("execution_class") == execution_class
                and sha256(environment.get("configuration_sha256"))
                and row.get("evidence_class") == evidence_class
                and row.get("environment") == environment)

    targets = {"accept_contract": contracts, "develop_work": work,
               "decide_capability": capabilities, "accept_no_rollback": capabilities,
               "admit_work": {data["program_id"]: data["admission"]}}
    valid_proposals = set()
    active_proposals = {d.get("proposal_id") for d in decisions.values() if d.get("status") == "approved"}
    for proposal_id, proposal in proposals.items():
        # Superseded decisions and their packets are retained as history, not authority.
        if proposal_id not in active_proposals:
            continue
        before = len(errors)
        for field in ("contract_bindings", "unknown_bindings", "checkpoint_bindings", "research_bindings"):
            if not isinstance(proposal.get(field), list):
                errors.append(f"{proposal_id} requires explicit {field} as a list")
        grants = proposal.get("authorizations")
        scope = proposal.get("scope")
        if not isinstance(scope, list) or not scope or not all(text_present(s) for s in scope):
            errors.append(f"{proposal_id} requires explicit scope")
            continue
        if not isinstance(grants, list) or not grants:
            errors.append(f"{proposal_id} requires explicit authorizations")
            continue
        needed_contracts = set()
        needed_unknowns = set()
        needed_checkpoints = set()
        needed_research = set()
        for grant in grants:
            action, target_id = grant.get("action"), grant.get("target")
            target = targets.get(action, {}).get(target_id)
            if target is None:
                errors.append(f"{proposal_id} has unknown authorization target")
                continue
            if target_id not in (scope or []):
                errors.append(f"{proposal_id} authorization is outside proposal scope")
            issue = target_id if action == "develop_work" else target.get("decision_issue")
            if proposal.get("linear_issue") != issue:
                errors.append(f"{proposal_id} authorization requires the matching Linear issue")
            if grant.get("target_digest") != target_digest(action, target):
                errors.append(f"{proposal_id} stale authorization target {target_id}")
            if action == "accept_contract":
                needed_contracts.add(target_id)
            else:
                needed_contracts.update(target.get("contract_ids", target.get("consumes_contracts", [])))
            if action == "decide_capability":
                needed_unknowns.update(target.get("unknown_ids", []))
                needed_research.update(target["research_ids"])
                requirements = target.get("checkpoint_requirements")
                if isinstance(requirements, list):
                    needed_checkpoints.update(req.get("checkpoint_id") for req in requirements
                                              if isinstance(req, dict) and isinstance(req.get("checkpoint_id"), str))
        for checkpoint_id in needed_checkpoints:
            if checkpoint_id in checkpoints:
                needed_contracts.update(checkpoints[checkpoint_id].get("requires", []))
        if not bindings_match(proposal.get("contract_bindings"), sorted(needed_contracts)):
            errors.append(f"{proposal_id} stale or incomplete contract bindings")
        if (not needed_unknowns.issubset(unknowns) or not exact_bindings(
                proposal.get("unknown_bindings", []),
                [unknown_binding(unknowns[uid]) for uid in sorted(needed_unknowns) if uid in unknowns])):
            errors.append(f"{proposal_id} stale or incomplete unknown bindings")
        if (not needed_checkpoints.issubset(checkpoints) or not exact_bindings(
                proposal.get("checkpoint_bindings", []),
                [checkpoint_binding(checkpoints[cid]) for cid in needed_checkpoints if cid in checkpoints])):
            errors.append(f"{proposal_id} stale or incomplete checkpoint bindings")
        if (not needed_research.issubset(research) or not exact_bindings(
                proposal.get("research_bindings"),
                [{"id": rid, "record_digest": digest(research[rid])} for rid in sorted(needed_research) if rid in research])):
            errors.append(f"{proposal_id} stale or incomplete research bindings")
        if len(errors) == before:
            valid_proposals.add(proposal_id)

    receipt_fields = {
        "linear_issue", "approval_comment_id", "human_decider", "authority_basis", "proposal_digest", "selected_option",
        "rejected_options", "rationale", "conditions", "scope", "decided_on",
        "approval_evidence", "revisit_trigger", "proposal_id",
    }
    receipt_arrays = {"rejected_options", "conditions", "scope"}
    valid_decisions = set()
    for decision_id, decision in decisions.items():
        if decision.get("status") == "approved":
            before = len(errors)
            absent = sorted(field for field in receipt_fields if not decision.get(field))
            if absent:
                errors.append(f"{decision_id} incomplete human receipt: {', '.join(absent)}")
            for field in receipt_fields - receipt_arrays:
                if not text_present(decision.get(field)):
                    errors.append(f"{decision_id} receipt {field} must be a nonempty string")
            for field in receipt_arrays:
                value = decision.get(field)
                if not isinstance(value, list) or not value or not all(text_present(v) for v in value):
                    errors.append(f"{decision_id} receipt {field} must be a nonempty list of strings")
            if not calendar_date(decision.get("decided_on")):
                errors.append(f"{decision_id} receipt decided_on must be a valid YYYY-MM-DD date")
            if not sha256(decision.get("proposal_digest")):
                errors.append(f"{decision_id} proposal_digest must be SHA-256")
            proposal = proposals.get(decision.get("proposal_id"))
            if proposal is None or decision.get("proposal_id") not in valid_proposals:
                errors.append(f"{decision_id} lacks a current named proposal")
            elif decision.get("proposal_digest") != digest(proposal):
                errors.append(f"{decision_id} stale proposal digest")
            elif decision.get("scope") != proposal.get("scope"):
                errors.append(f"{decision_id} receipt scope differs from proposal")
            elif decision.get("linear_issue") != proposal.get("linear_issue"):
                errors.append(f"{decision_id} receipt requires the matching Linear issue")
            if len(errors) == before:
                valid_decisions.add(decision_id)
        elif decision.get("status") not in {"pending", "superseded", "rejected"}:
            errors.append(f"{decision_id} has invalid status")

    def authorized(receipt: Any, action: str, target: str) -> bool:
        if receipt not in valid_decisions:
            return False
        proposal = proposals[decisions[receipt]["proposal_id"]]
        return any(g.get("action") == action and g.get("target") == target
                   for g in proposal["authorizations"])

    def release_supported(row: dict[str, Any], capability: dict[str, Any]) -> bool:
        requirements = capability.get("release_requirements")
        if (not isinstance(requirements, dict)
                or row.get("release_requirements_digest") != digest(requirements)):
            return False

        def tested(kind: str) -> bool:
            command = requirements.get(f"{kind}_command")
            proof_ids = row.get(f"{kind}_evidence_ids")
            if not isinstance(command, str) or not command.strip() or not isinstance(proof_ids, list):
                return False
            return any(isinstance(eid, str) and eid in evidence
                       and successful_proof(evidence[eid], capability.get("candidate_commit"))
                       and evidence[eid].get("capability_id") == capability["id"]
                       and evidence[eid].get("tier") == "release_qualified"
                       and evidence[eid].get("kind") == kind
                       and evidence[eid].get("acceptance_command") == command
                       and evidence[eid].get("release_requirements_digest") == digest(requirements)
                       and qualification_matches(evidence[eid], capability, "release_qualified")
                       and bindings_match(evidence[eid].get("contract_bindings"), capability.get("contract_ids", []))
                       for eid in proof_ids)

        boundary = requirements.get("no_rollback_boundary")
        if boundary is None:
            rollback = tested("rollback")
        else:
            rollback = (isinstance(boundary, str) and bool(boundary.strip())
                        and authorized(capability.get("no_rollback_receipt"), "accept_no_rollback", capability["id"]))
        # A no-rollback boundary waives rollback only, never tested recovery.
        return tested("recovery") and rollback

    for contract_id, contract in contracts.items():
        if contract.get("status") == "accepted" and not authorized(
                contract.get("decision_receipt"), "accept_contract", contract_id):
            errors.append(f"{contract_id} accepted contract lacks applicable human receipt")

    admission = data["admission"]
    mode = admission.get("mode")
    if mode == "bounded_autonomy":
        if admission.get("routine_work_default") != "proceed":
            errors.append("bounded autonomy must let routine work proceed")
        if admission.get("human_gate_default") != "block_affected_lineage_only":
            errors.append("bounded autonomy must narrow human-gate blocking")
        for field in ("allowed_agent_actions", "human_decision_triggers"):
            if (not isinstance(admission.get(field), list) or not admission[field]
                    or not all(text_present(value) for value in admission[field])):
                errors.append(f"bounded autonomy requires non-empty {field}")
    elif mode not in {"open", "closed_pending_human_limits"}:
        errors.append("admission mode must be open, bounded_autonomy, or closed_pending_human_limits")

    occupancy = {}
    for field in ("active_contract_consumers", "active_reviews"):
        values = admission.get(field)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            errors.append(f"admission requires {field} as work item IDs")
            values = []
        if len(values) != len(set(values)) or any(v not in work for v in values):
            errors.append(f"admission {field} has duplicate or unknown work item IDs")
        occupancy[field] = set(values)
    # Lists cannot hide active consumers or verification work recorded in the ledger.
    consumers = {wid for wid, item in work.items() if item.get("status") == "in_progress"
                 and STAGES.get(item.get("stage"), -1) >= STAGES["development"]
                 and item.get("consumes_contracts")}
    reviews = {wid for wid, item in work.items() if item.get("status") == "in_progress"
               and item.get("stage") == "verification"}
    for field, inferred in (("active_contract_consumers", consumers), ("active_reviews", reviews)):
        if not inferred.issubset(occupancy[field]):
            errors.append(f"admission {field} omits active work")
        if occupancy[field] - inferred:
            errors.append(f"admission {field} contains stale or wrong-lane work")
        occupancy[field] = inferred
    occupied = any(occupancy.values())
    if mode == "closed_pending_human_limits" and occupied:
        errors.append("closed admission has active work")
    # Empty bounded lanes may remain descriptive while owner limits are pending.
    # Once work occupies capacity, both modes require the same pinned envelope.
    if mode == "open" or (mode == "bounded_autonomy" and occupied):
        if not authorized(admission.get("decision_receipt"), "admit_work", data["program_id"]):
            errors.append(f"{mode} admission lacks an applicable approved human decision receipt")
        for field, active in (("max_active_contract_consumers", "active_contract_consumers"),
                              ("max_concurrent_reviews", "active_reviews")):
            limit = admission.get(field)
            if type(limit) is not int or limit < 1:
                errors.append(f"{mode} admission requires a positive {field}")
            elif len(occupancy[active]) > limit:
                errors.append(f"{mode} admission exceeds {field}")

    done = DONE
    stage_rank = STAGES
    verified_work = set()
    for item_id, item in work.items():
        if item.get("independent_verification_required") is not True:
            errors.append(f"{item_id} must require independent verification")
        if item.get("status") in DONE:
            binding = work_binding(item, work)
            evidence_ids = item.get("verification_evidence_ids", [])
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            for eid in evidence_ids:
                if eid not in evidence:
                    errors.append(f"{item_id} references unknown verification evidence {eid}")
            if not any(successful_proof(evidence[eid], item.get("candidate_commit"))
                       and evidence[eid].get("kind") == "work_verification"
                       and evidence[eid].get("work_item_id") == item_id
                       and evidence[eid].get("work_digest") == binding["work_digest"]
                       and evidence[eid].get("acceptance_command") == item.get("acceptance_command")
                       and bindings_match(evidence[eid].get("contract_bindings"), item.get("consumes_contracts", []))
                       for eid in evidence_ids if eid in evidence):
                errors.append(f"{item_id} completed work lacks exact-candidate independent verification")
            elif item.get("independent_verification_required") is True:
                verified_work.add(item_id)
    for item_id, item in work.items():
        stage = item.get("stage")
        if stage not in stage_rank:
            errors.append(f"{item_id} has invalid stage")
            continue
        discretion = item.get("discretion_class")
        if type(item.get("consequential")) is not bool:
            errors.append(f"{item_id} requires explicit boolean consequential")
        if item.get("status") not in {"todo", "blocked", "in_progress", *DONE}:
            errors.append(f"{item_id} has invalid work status")
        if discretion not in {"bounded_autonomy", "collaborative_judgment", "human_gated"}:
            errors.append(f"{item_id} has invalid discretion class")
        if item.get("consequential") and discretion != "human_gated":
            errors.append(f"{item_id} consequential work must be human_gated")
        for dependency in item.get("hard_dependencies", []):
            if dependency not in work:
                errors.append(f"{item_id} references unknown hard dependency {dependency}")
            elif stage_rank[stage] >= stage_rank["development"] or item.get("status") in done:
                if work[dependency].get("status") not in done:
                    errors.append(f"{item_id} starts before hard dependency {dependency} is complete")
                elif dependency not in verified_work:
                    errors.append(f"{item_id} hard dependency {dependency} lacks independent verification")
        for contract_id in item.get("consumes_contracts", []):
            if contract_id not in contracts:
                errors.append(f"{item_id} references unknown contract {contract_id}")
            elif stage_rank[stage] >= stage_rank["development"] and contracts[contract_id].get("status") != "accepted":
                errors.append(f"{item_id} consumes unaccepted contract {contract_id}")
            elif stage_rank[stage] >= stage_rank["development"]:
                receipt = item.get("contract_receipts", {}).get(contract_id)
                if receipt != contracts[contract_id].get("decision_receipt") or not authorized(receipt, "accept_contract", contract_id):
                    errors.append(f"{item_id} lacks matching contract receipt for {contract_id}")
        if (item.get("consequential") or discretion == "human_gated") and stage_rank[stage] >= stage_rank["development"]:
            receipt = item.get("decision_receipt")
            if not authorized(receipt, "develop_work", item_id):
                errors.append(f"{item_id} lacks an applicable approved human decision receipt")
        if "research_plan" in item or (stage == "research" and item.get("status") in {"in_progress", *DONE}):
            plan = item.get("research_plan")
            if not isinstance(plan, dict) or any(not text_present(plan.get(field)) for field in PLAN_FIELDS):
                errors.append(f"{item_id} research plan is missing mandatory fields")
            if isinstance(plan, dict) and type(plan.get("continuation")) is not bool:
                errors.append(f"{item_id} research plan requires explicit boolean continuation")
        # No accepted research-continuation grant exists in this candidate schema.
        # Refuse every recorded continuation/packet rather than reinterpret a
        # generic work receipt as quota or direction-changing authority.
        for declaration in (item, item.get("research_plan", {})):
            if continuation_declared(declaration):
                errors.append(f"{item_id} research continuation blocked pending owner-defined receipt mapping")
        if not text_present(item.get("acceptance_command")):
            errors.append(f"{item_id} missing acceptance command")
        for field in ("recovery", "rollback"):
            if not text_present(item.get(field)):
                errors.append(f"{item_id} missing {field} as nonempty text")
        stops = item.get("stop_conditions")
        if not isinstance(stops, list) or not stops or not all(text_present(value) for value in stops):
            errors.append(f"{item_id} missing stop_conditions as a nonempty list of text")

    def visit(item_id: str, visiting: set[str], visited: set[str]) -> None:
        if item_id in visiting:
            errors.append(f"hard dependency cycle at {item_id}")
            return
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in work[item_id].get("hard_dependencies", []):
            if dependency in work:
                visit(dependency, visiting, visited)
        visiting.remove(item_id)
        visited.add(item_id)

    visited: set[str] = set()
    for item_id in work:
        visit(item_id, set(), visited)

    for capability in data["capabilities"]:
        capability_id = capability.get("id", "<unknown>")
        current = capability.get("current_maturity")
        target = capability.get("month_target")
        if current not in rank or target not in rank:
            errors.append(f"{capability_id} has unknown maturity tier")
            continue
        # month_target is an aspiration; evidence and authority govern actual maturity.
        for rid in capability["research_ids"]:
            if rid not in research:
                errors.append(f"{capability_id} references unknown research {rid}")
        if rank[current] >= rank["researched"]:
            question = capability.get("research_question")
            if not text_present(question) or not any(
                    row.get("capability_id") == capability_id and row.get("question") == question
                    and row.get("status") == "current" and row.get("result") == "pass"
                    and row.get("continuation") is False and not continuation_declared(row)
                    and all(text_present(row.get(field)) for field in ("source_class", "artifact", "limitations"))
                    and sha256(row.get("artifact_sha256"))
                    and commit_hash(row.get("source_commit"))
                    and bindings_match(row.get("contract_bindings"), capability["contract_ids"])
                    for rid in capability["research_ids"] if rid in research for row in [research[rid]]):
                errors.append(f"{capability_id} researched maturity requires current linked structured research")
        if rank[current] >= rank["human_decided"] and not authorized(
                capability.get("decision_receipt"), "decide_capability", capability_id):
            errors.append(f"{capability_id} lacks applicable human decision authority")
        evidence_ids = capability.get("evidence_ids", [])
        for evidence_id in evidence_ids:
            if evidence_id not in evidence:
                errors.append(f"{capability_id} references unknown evidence {evidence_id}")
        requirements = capability.get("checkpoint_requirements")
        if not isinstance(requirements, list):
            errors.append(f"{capability_id} requires explicit checkpoint requirements")
            requirements = []
        seen_checkpoints = set()
        for requirement in requirements:
            if not isinstance(requirement, dict):
                errors.append(f"{capability_id} has invalid checkpoint requirement")
                continue
            checkpoint_id = requirement.get("checkpoint_id")
            threshold = requirement.get("required_from_tier")
            if checkpoint_id not in checkpoints or checkpoint_id in seen_checkpoints:
                errors.append(f"{capability_id} has unknown or duplicate checkpoint {checkpoint_id}")
                continue
            seen_checkpoints.add(checkpoint_id)
            if threshold is None:
                if rank[current] >= rank["prototyped"]:
                    errors.append(f"{capability_id} checkpoint threshold pending owner decision for {checkpoint_id}")
                continue
            if threshold not in rank:
                errors.append(f"{capability_id} checkpoint has unknown required tier")
                continue
            if rank[current] < rank[threshold]:
                continue
            checkpoint = checkpoints[checkpoint_id]
            for contract_id in checkpoint.get("requires", []):
                if contract_id in contracts and contracts[contract_id].get("status") != "accepted":
                    errors.append(f"{capability_id} checkpoint requires accepted contract {contract_id}")
            inputs = checkpoint.get("work_item_ids", [])
            if not isinstance(inputs, list) or not all(isinstance(wid, str) for wid in inputs):
                inputs = []
            expected_work = [work_binding(work[wid], work) for wid in inputs if wid in work]
            proof_ids = checkpoint.get("evidence_ids", [])
            if not isinstance(proof_ids, list):
                proof_ids = []
            if (checkpoint.get("status") != "closed" or not inputs
                    or any(wid not in verified_work for wid in inputs)
                    or not any(successful_proof(evidence[eid], capability.get("candidate_commit"))
                               and evidence[eid].get("kind") == "composed_checkpoint"
                               and evidence[eid].get("checkpoint_id") == checkpoint_id
                               and evidence[eid].get("checkpoint_digest") == checkpoint_binding(checkpoint)["definition_digest"]
                               and evidence[eid].get("capability_id") == capability_id
                               and evidence[eid].get("tier") == current
                               and qualification_matches(evidence[eid], capability, current)
                               and evidence[eid].get("acceptance_command") == checkpoint.get("acceptance_command")
                               and bindings_match(evidence[eid].get("contract_bindings"),
                                                  sorted(set(checkpoint.get("requires", [])) | set(capability.get("contract_ids", []))))
                               and exact_bindings(evidence[eid].get("work_bindings"), expected_work)
                               for eid in proof_ids if eid in evidence)):
                errors.append(f"{capability_id} requires closed checkpoint {checkpoint_id} with current composed verification")
        if rank[current] >= rank["prototyped"]:
            independent = rank[current] >= rank["provider_free_verified"]
            kinds = {"prototyped": "prototype", "implemented": "implementation",
                     "provider_free_verified": "provider_free", "locally_qualified": "local_qualification",
                     "release_qualified": "release_qualification"}
            candidate = capability.get("candidate_commit")
            valid_candidate = commit_hash(candidate)
            commands = capability.get("acceptance_commands")
            command = commands.get(current) if isinstance(commands, dict) else None
            def supports(row: dict[str, Any]) -> bool:
                return (valid_candidate and row.get("candidate_commit") == candidate
                        and row.get("capability_id") == capability_id
                        and row.get("result") == "pass" and row.get("status") == "current"
                        and row.get("tier") == current and row.get("kind") == kinds[current]
                        and (not independent or row.get("independent") is True)
                        and text_present(command) and row.get("acceptance_command") == command
                        and text_present(row.get("artifact")) and proof_environment(row.get("environment"))
                        and sha256(row.get("artifact_sha256"))
                        and qualification_matches(row, capability, current)
                        and bindings_match(row.get("contract_bindings"), capability.get("contract_ids", [])))
            matching = [evidence[eid] for eid in evidence_ids if eid in evidence and supports(evidence[eid])]
            if not matching:
                label = "independent verification" if independent else "evidence"
                errors.append(f"{capability_id} claims {current} without {label} for the current candidate and tier")
            if current in {"locally_qualified", "release_qualified"} and not any(
                    qualification_matches(evidence[eid], capability, current) for eid in evidence_ids if eid in evidence):
                errors.append(f"{capability_id} qualification requires approved environment/class and matching evidence")
            if current == "release_qualified" and not any(release_supported(row, capability) for row in matching):
                errors.append(f"{capability_id} release requires tested recovery and rollback or an applicable no-rollback receipt")
        if rank[current] >= rank["contract_frozen"] and not capability.get("contract_ids"):
            errors.append(f"{capability_id} frozen maturity requires linked contracts")
        for contract_id in capability.get("contract_ids", []):
            if contract_id not in contracts:
                errors.append(f"{capability_id} references unknown contract {contract_id}")
            elif rank[current] >= rank["contract_frozen"] and contracts[contract_id].get("status") != "accepted":
                errors.append(f"{capability_id} frozen maturity requires accepted contract {contract_id}")
        for unknown_id in capability.get("unknown_ids", []):
            if unknown_id not in unknowns:
                errors.append(f"{capability_id} references unknown unknown {unknown_id}")
            else:
                unknown = unknowns[unknown_id]
                if unknown_id not in resolved_unknowns and unknown.get("blocks") in rank and rank[current] >= rank[unknown["blocks"]]:
                    errors.append(f"{capability_id} maturity blocked by unresolved {unknown_id}")

    for unknown_id, unknown in unknowns.items():
        if unknown.get("status") not in {"open", "resolved"}:
            errors.append(f"{unknown_id} requires open or resolved status")
        if unknown.get("blocks") not in rank:
            errors.append(f"{unknown_id} blocks an unknown maturity tier")
        if not text_present(unknown.get("exposure")):
            errors.append(f"{unknown_id} lacks exposure")

    for risk in data["risks"]:
        if risk.get("severity") not in {"low", "medium", "high", "critical"}:
            errors.append(f"{risk.get('id')} has invalid severity")
        if not text_present(risk.get("mitigation")) or not text_present(risk.get("trigger")):
            errors.append(f"{risk.get('id')} lacks mitigation or trigger")

    for checkpoint in data["integration_checkpoints"]:
        if checkpoint.get("status") not in {"open", "closed"}:
            errors.append(f"{checkpoint.get('id')} has invalid checkpoint status")
        inputs = checkpoint.get("work_item_ids")
        if (not isinstance(inputs, list) or not inputs or not all(isinstance(wid, str) for wid in inputs)
                or len(set(inputs)) != len(inputs) or any(wid not in work for wid in inputs)):
            errors.append(f"{checkpoint.get('id')} requires unique known work inputs")
        proof_ids = checkpoint.get("evidence_ids")
        if not isinstance(proof_ids, list):
            errors.append(f"{checkpoint.get('id')} requires evidence IDs as a list")
            proof_ids = []
        for eid in proof_ids:
            if eid not in evidence:
                errors.append(f"{checkpoint.get('id')} references unknown checkpoint evidence {eid}")
        for contract_id in checkpoint.get("requires", []):
            if contract_id not in contracts:
                errors.append(f"{checkpoint.get('id')} references unknown contract {contract_id}")
        if not text_present(checkpoint.get("acceptance_command")):
            errors.append(f"{checkpoint.get('id')} missing acceptance command")
    return errors


def main() -> int:
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"PROGRAM_CONTROL_FAIL: {exc}", file=sys.stderr)
        return 1
    errors = validate_program(data)
    if errors:
        for error in errors:
            print(f"PROGRAM_CONTROL_FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "PROGRAM_CONTROL_PASS "
        f"contracts={len(data['contracts'])} decisions={len(data['decisions'])} "
        f"work_items={len(data['work_items'])} capabilities={len(data['capabilities'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
