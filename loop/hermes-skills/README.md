# Hermes agent skills — repo mirror (durability + review)

These are the **agent-authored / authored Hermes skills** that give the box's root Hermes agent
(the Telegram gateway, cron agents, and any `hermes -t skills …` run) its Recursion-specific
capabilities. The agent loads a skill on demand via its `skills_list` / `skill_view` tools (these
tools ship in the `_HERMES_CORE_TOOLS` set that the `hermes-telegram` bundle uses, so the gateway
can load them in the live chat channel).

## Source of truth vs this mirror

- **Authoritative copy lives on the box:** `/root/.hermes/skills/<name>/SKILL.md` — that is what the
  running agent reads, and what the Hermes **curator** manages (snapshots before each run, prunes
  stale, never hard-deletes, never touches the 70 bundled skills; consolidation is OFF so it will not
  merge these).
- **This directory is a mirror for git review + rebuild durability.** If the box is rebuilt, redeploy
  with: `scp loop/hermes-skills/<name>/SKILL.md gs-recursion:/root/.hermes/skills/<name>/SKILL.md`
  then `ssh gs-recursion 'hermes curator run'` to re-adopt.
- **Editing a skill is a STRUCTURAL change** (it alters what the agent can do) → author it here in a
  reviewed session, deploy to the box, keep the two in sync. Never let the agent rewrite its own ops
  skill ad-hoc — that is exactly the drift `recursion-ops` itself warns against.

## Skills

| Skill | Role | Read-only? |
|---|---|---|
| `recursion-status` | REPORT box/build-loop status from ground truth via the `recursion` CLI | yes (reporting only) |
| `recursion-project-pilot` | Resolve one Telegram topic to one chartered pilot and read only scoped live project evidence | yes (reporting only; mutations remain intake) |
| `recursion-ops`    | ACT on Lucas's ops requests (resolve/requeue/plan/drain/pause/publish) with the loop's guardrails; defer STRUCTURAL changes to an authoring session | no (acts, but fenced by discipline) |

The pair is deliberate: `recursion-status` answers "what is it doing?", `recursion-ops` does "make it
do X" — with the same never-`main` / never-`--force` / no-ad-hoc-structural-change discipline the
autonomous loop follows.
