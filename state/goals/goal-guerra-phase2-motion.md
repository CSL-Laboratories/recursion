schema_version: 1
id: goal-guerra-phase2-motion
project_id: guerra-construction-group
status: active
outcome: Begin Phase 2 motion layer with a minimal Lenis provider wired into the app layout that builds cleanly and respects reduced motion.
acceptance_suite: cd apps/web && npm ci && npm run build
falsifier: Task touches auth/portal/API, adds unrelated features, or breaks the web build.
budget: subscription Cursor/Codex only; one worker; review branch only; no automerge

Phase 2 smoke goal for Recursion first genuine-use pilot.
