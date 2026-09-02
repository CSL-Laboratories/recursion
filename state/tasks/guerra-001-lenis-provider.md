schema_version: 1
id: guerra-001-lenis-provider
workstream: guerra-construction-group
goal_id: goal-guerra-phase2-motion
engine: cursor
verifier_engine: cursor
repo: /opt/guerra-construction-group
sshkey: ~/.ssh/guerra-construction-group_deploy
acceptance: npm ci && npm run build
workdir: apps/web
allowed_paths: apps/web/src/components/motion
non_goals: auth, portal, API, GSAP pinning, content rewrites, dependency upgrades beyond what is required to import lenis (lenis already in package.json)
autonomy: supervised
status: failed
lease_expires: 0
finished_at: 2026-09-02T07:27:33Z
evidence: acceptance test failed after retry
event_seq: 15
last_transition_id: transition-3f9cca445a61b1571f65eb1ee5d2d386
lease_owner: work.sh:378282@gs-recursion-nbg
attempts: 7
started_at: 2026-09-02T07:22:22Z

Create ONLY apps/web/src/components/motion/LenisProvider.tsx — client component wrapping children with Lenis smooth scroll; if prefers-reduced-motion, render children without Lenis. Do not edit layout yet unless required to export; prefer standalone provider file only. Do not run npm yourself. Minimal correct > fancy.
