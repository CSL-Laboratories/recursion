schema_version: 1
id: guerra-001-lenis-provider
workstream: guerra-construction-group
goal_id: goal-guerra-phase2-motion
engine: cursor
verifier_engine: cursor
repo: /opt/guerra-construction-group
sshkey: ~/.ssh/guerra-construction-group_deploy
acceptance: cd apps/web && npm ci && npm run build
workdir: apps/web
allowed_paths: apps/web/src/components/motion
non_goals: auth, portal, API, GSAP pinning, content rewrites, dependency upgrades beyond what is required to import lenis (lenis already in package.json)
autonomy: supervised
status: running
lease_expires: 1788337945
finished_at: 2026-09-02T07:00:18Z
evidence: a required engine (engine=cursor, verifier_engine=cursor) bills as metered — a human sets RECURSION_ALLOW_METERED=1 in .env to allow it
event_seq: 2
last_transition_id: transition-b133a063543693015c6529841df6f62b
lease_owner: work.sh:355349@gs-recursion-nbg
attempts: 1
started_at: 2026-09-02T07:02:25Z

Create ONLY apps/web/src/components/motion/LenisProvider.tsx — client component wrapping children with Lenis smooth scroll; if prefers-reduced-motion, render children without Lenis. Do not edit layout yet unless required to export; prefer standalone provider file only. Do not run npm yourself. Minimal correct > fancy.
