schema_version: 1
id: guerra-001-lenis-provider
workstream: guerra-construction-group
goal_id: goal-guerra-phase2-motion
engine: cursor
verifier_engine: cursor
repo: /opt/guerra-construction-group
sshkey: ~/.ssh/guerra-construction-group_deploy
acceptance: npm ci --offline --cache /opt/recursion/cache/npm && NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/opt/recursion/cache/guerra-web/next-font-google-mocked-responses.js npm run build
workdir: apps/web
allowed_paths: apps/web/src/components/motion
non_goals: auth, portal, API, GSAP pinning, content rewrites, dependency upgrades beyond what is required to import lenis (lenis already in package.json)
autonomy: supervised
status: running
lease_expires: 1788340961
finished_at: 2026-09-02T07:27:33Z
evidence: acceptance test failed after retry
event_seq: 16
last_transition_id: transition-ac96df9615533d5c9b9661411f009916
lease_owner: work.sh:397236@gs-recursion-nbg
attempts: 8
started_at: 2026-09-02T07:52:41Z

Create ONLY apps/web/src/components/motion/LenisProvider.tsx — client component wrapping children with Lenis smooth scroll; if prefers-reduced-motion, render children without Lenis. Do not edit layout yet unless required to export; prefer standalone provider file only. Do not run npm yourself. Minimal correct > fancy.
