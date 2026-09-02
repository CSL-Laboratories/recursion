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
finished_at: 2026-09-02T07:15:15Z
evidence: SCOPE VIOLATION — the arm went outside its boundary, so the acceptance result is not trusted and was not run: modified paths outside allowed scope: .cursor-runner-state/agent-cli-state.json, .cursor-runner-state/ai-tracking/ai-code-tracking.db, .cursor-runner-state/chats/eab0d61a99b6696edb3d2aff87b585e8/2cc98ba2-229a-45b9-8df4-df48823cda16/meta.json, .cursor-runner-state/chats/eab0d61a99b6696edb3d2aff87b585e8/2cc98ba2-229a-45b9-8df4-df48823cda16/store.db, .cursor-runner-state/cli-config.json, .cursor-runner-state/projects/workspace/.workspace-trusted, .cursor-runner-state/projects/workspace/agent-tools/71c5c10c-8dfa-40bb-a91d-fd7067f8ce65.txt, .cursor-runner-state/projects/workspace/agent-transcripts/2cc98ba2-229a-45b9-8df4-df48823cda16/2cc98ba2-229a-45b9-8df4-df48823cda16.jsonl, .cursor-runner-state/skills-cursor/.sync-manifest.json, .cursor-runner-state/skills-cursor/autopilot/SKILL.md
event_seq: 11
last_transition_id: transition-521612b0961bf3e4dee16a15d2415cce
lease_owner: work.sh:367083@gs-recursion-nbg
attempts: 5
started_at: 2026-09-02T07:13:28Z

Create ONLY apps/web/src/components/motion/LenisProvider.tsx — client component wrapping children with Lenis smooth scroll; if prefers-reduced-motion, render children without Lenis. Do not edit layout yet unless required to export; prefer standalone provider file only. Do not run npm yourself. Minimal correct > fancy.
