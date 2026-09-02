schema_version: 1
id: guerra-005-phase5-lenis-wire-canary
project_id: guerra-construction-group
workstream: guerra-construction-group
goal_id: goal-guerra-phase2-motion
goal: Add the minimal reduced-motion-aware Lenis provider and wire it into the application layout.
repo: /opt/guerra-construction-group
sshkey: ~/.ssh/guerra-construction-group_deploy
workdir: apps/web
allowed_paths: apps/web/src/components/motion/LenisProvider.tsx,apps/web/src/app/layout.tsx
non_goals: package.json changes,dependency upgrades,GSAP effects,page choreography,content,auth,portal,API,merge,deploy,cherry-picking prior loop/work branches
worker_class: web-implementation
engine: cursor
verifier_engine: cursor
model_alias: cursor-primary
verifier_model_alias: opus-review
deadline_seconds: 3600
verifier_deadline_seconds: 1800
network_policy: provider-only
evidence_contract: worker-result-v1
verification: independent
expected_change: required
gate_class: light
controller_gate_files: scripts/gates/guerra-lenis-wire-v1.sh,scripts/gates/guerra-next-font-mock-v1.cjs
acceptance: npm ci --offline --cache /opt/recursion/cache/npm && /opt/recursion/scripts/gates/guerra-lenis-wire-v1.sh
autonomy: supervised
status: escalated
lease_owner: work.sh:722824@gs-recursion-nbg
lease_expires: 0
attempts: 1
started_at: 2026-09-02T20:59:58Z
event_seq: 10
last_transition_id: transition-f88f95ab8a43a4ec32aed8e1b9a605d6
finished_at: 2026-09-02T21:04:09Z
evidence: killed: contended canary; env had been restored to composer-2.5-fast; verifier used pre-evidence-fix prompt

Starting from current clean main, create a minimal client-side LenisProvider that renders children without Lenis when prefers-reduced-motion is enabled, and wrap the application layout with it. Implement the change directly; do not cherry-pick or copy a prior loop/work branch. Touch only allowed_paths. Do not run npm—the controller owns installation and the build gate.

RESULT SCHEMA NOTE (worker-result-v1): When writing .recursion-result.json, every field must match types. Especially blocker MUST always be a JSON string — never null, never omitted. For status "done" or "no-op" use "blocker": "". For status "blocked" use "blocker": "<precise question>". Example done payload shape: {"status":"done","summary":"...","files_changed":["apps/web/..."],"tests_run":"...","risks":[],"blocker":""}.
