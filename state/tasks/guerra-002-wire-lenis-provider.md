schema_version: 1
id: guerra-002-wire-lenis-provider
workstream: guerra-construction-group
goal_id: goal-guerra-phase2-motion
engine: cursor
verifier_engine: cursor
repo: /opt/guerra-construction-group
sshkey: ~/.ssh/guerra-construction-group_deploy
acceptance: npm ci --offline --cache /opt/recursion/cache/npm && NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/opt/recursion/cache/guerra-web/next-font-google-mocked-responses.js npm run build && rg -n 'LenisProvider' src/app/layout.tsx
workdir: apps/web
allowed_paths: apps/web/src/components/motion/LenisProvider.tsx, apps/web/src/app/layout.tsx
non_goals: GSAP ScrollTrigger effects, reveals, page choreography, package.json churn unless build proves missing dep, content/copy/brand, auth, portal, API, automerge
autonomy: supervised
status: passed
lease_owner: work.sh:602888@gs-recursion-nbg
lease_expires: 0
attempts: 1
started_at: 2026-09-02T16:53:00Z
event_seq: 2
last_transition_id: transition-be518e26d992a8cfca33bd412fcc71c4
finished_at: 2026-09-02T16:56:03Z
evidence: passed -> loop/work/guerra-002-wire-lenis-provider

Bring Lenis smooth-scroll onto main by ensuring `apps/web/src/components/motion/LenisProvider.tsx` exists (reuse/adapt from branch loop/work/guerra-001-lenis-provider @ 3778854 if helpful) AND wrapping the app in `apps/web/src/app/layout.tsx` with LenisProvider. Honor prefers-reduced-motion (no Lenis when reduced). Do not run npm yourself. Minimal correct. Only touch allowed_paths.
