---
name: recursion-status
description: "Answer 'what is the Recursion box doing?' from ground truth via the recursion CLI — never guess."
version: 1.0.0
author: Recursion (agent-created)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Recursion, Status, Supervision, Ground-Truth, Ops]
    related_skills: []
---

# Recursion box status

The Recursion cron/operator control plane runs as root on `/opt/recursion`, but the Telegram gateway
deliberately runs as the unprivileged `hermes-gateway` user. When the user (Lucas) asks
anything like *"what's the box doing?"*, *"status"*, *"how are the workstreams going?"*,
*"any escalations?"*, *"what did you build?"* — you answer from **live CLI output,
never from memory and never a guess.** A wrong status costs trust; an honest "the command returned X"
never does.

## When to Use

- Lucas asks for the status of the Recursion box, the build-loop, or a registered workstream.
- Lucas asks what's queued, what's building, what needs a decision, or what was recently learned.
- Any "is it still running / did it work / what happened overnight" question about this box.

## How (run these with the terminal tool, then REPORT THE ACTUAL OUTPUT)

The `recursion` CLI is on PATH. Run what the question needs; do not run all of them blindly:

- `recursion board` — the live build-loop registry: what's running now, needs a decision, awaiting review.
- `recursion inbox` — escalations waiting on Lucas (the "needs a decision" queue).
- `recursion proposals` — the self-improvement queue (things reflection proposed; a human applies).
- `recursion lessons` — durable operational lessons the box has learned.
- `recursion brief` — the latest daily brief (one page).
- `recursion tasks` — the task queue, optionally `recursion tasks <status>`.
- `recursion pilot status <project>` — scoped live task/run/git evidence for any registered project.

## Rules

- **Report the command's real output.** If a command errors or is empty, say exactly that.
- **STATUS UNAVAILABLE:** a nonzero status command or an unavailable/unreadable control-state
  source never proves that plans, goals, tasks, or windows are absent. Do not translate a
  permission error or incomplete snapshot into an empty authoritative state.
- State-changing requests are durable intake, not status questions. Do not use this skill to
  adjudicate or reject them; `recursion-ops` acknowledges receipt for the root-owned pipeline.
- Is the box paused? `ls /opt/recursion/state/PAUSE` (present = halted). Mention it if so.
- Keep it short — a few lines, like the brief. Lead with anything that needs Lucas's decision.
- Do **not** run `recursion pause/resume/work/drain/reflect/dispatch` or edit any file when merely
  **reporting** status. Reporting is read-only. Acting is a separate, explicit request.
