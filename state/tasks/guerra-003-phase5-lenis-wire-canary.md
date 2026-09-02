schema_version: 1
id: guerra-003-phase5-lenis-wire-canary
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
lease_owner: work.sh:699635@gs-recursion-nbg
lease_expires: 0
attempts: 1
started_at: 2026-09-02T20:32:08Z
event_seq: 11
last_transition_id: transition-101cf91551cc37640bf43a86159e69b2
finished_at: 2026-09-02T20:32:54Z
evidence: RESULT CONTRACT VIOLATION — ERR blocker must be a string

Starting from current clean main, create a minimal client-side LenisProvider that renders children without Lenis when prefers-reduced-motion is enabled, and wrap the application layout with it. Implement the change directly; do not cherry-pick or copy a prior loop/work branch. Touch only allowed_paths. Do not run npm—the controller owns installation and the build gate.
