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
status: escalated
lease_expires: 0
finished_at: 2026-09-02T07:00:18Z
evidence: a required engine (engine=cursor, verifier_engine=cursor) bills as metered — a human sets RECURSION_ALLOW_METERED=1 in .env to allow it
event_seq: 1
last_transition_id: transition-7204b678b72870012febc23665c92e9a

Create ONLY apps/web/src/components/motion/LenisProvider.tsx — client component wrapping children with Lenis smooth scroll; if prefers-reduced-motion, render children without Lenis. Do not edit layout yet unless required to export; prefer standalone provider file only. Do not run npm yourself. Minimal correct > fancy.
