schema_version: 1
id: guerra-004-phase5-lenis-wire-canary
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
status: passed
lease_owner: work.sh:728850@gs-recursion-nbg
lease_expires: 0
attempts: 3
started_at: 2026-09-02T21:04:39Z
event_seq: 47
last_transition_id: transition-90e787d0d5c8faac02630e0f35b4eb96
finished_at: 2026-09-02T21:08:46Z
evidence: passed -> loop/work/guerra-004-phase5-lenis-wire-canary at e5b0514a4fe2155a22c4b7290c029c9e372eefc3 (independently verified)

Starting from current clean main, create a minimal client-side LenisProvider that renders children without Lenis when prefers-reduced-motion is enabled, and wrap the application layout with it. Implement the change directly; do not cherry-pick or copy a prior loop/work branch. Touch only allowed_paths. Do not run npm—the controller owns installation and the build gate.

RESULT SCHEMA NOTE (worker-result-v1): When writing .recursion-result.json, every field must match types. Especially blocker MUST always be a JSON string — never null, never omitted. For status "done" or "no-op" use "blocker": "". For status "blocked" use "blocker": "<precise question>". Example done payload shape: {"status":"done","summary":"...","files_changed":["apps/web/..."],"tests_run":"...","risks":[],"blocker":""}.
