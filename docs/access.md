# Access — how Lucas drives the loop

The supervisor runs on `gs-recursion` (Hetzner Nuremberg, always-on) as a Hermes engine on
the ChatGPT Pro sub. It is deliberately event-driven and mostly idle. You stay in control
through four levers, lowest-attention first.

Box: `ssh gs-recursion` · repo: `/opt/recursion` (`RECURSION_ROOT`) · times are UTC.

## 1. Async — Telegram (default, low attention)
- While resumed, the daily **brief** arrives through the durable outbox at the scheduled time.
- **Escalations** arrive as a separate `🔔 Recursion needs a decision` message and are also
  appended to `state/INBOX.md` (the durable queue — a ping you miss isn't lost). Under PAUSE,
  only the durable INBOX write occurs; no notification is attempted or queued for replay.
- You can message the bot directly any time; it answers on your $200 sub (`gpt-5.6-terra`).
- **Answer an escalation** by replying in Telegram, or edit `state/INBOX.md` and record the
  call in `state/DECISIONS.md` — the loop reads both next run.

### V2 command surface — source accepted, deployment pending

The v2 control boundary validates a fresh content-addressed snapshot before answering. Read
commands remain available under PAUSE; mutating commands never alter state directly.

| Command | Source behavior |
|---|---|
| `/today` | bounded list of work expected to land today |
| `/status` | PAUSE state and active work |
| `/why` | recorded decision rationale |
| `/next` | next eligible work |
| `/project` | exact canonical ref, source SHA, readiness, and blockers |
| `/budget` | quota, worker/review capacity, observed spend, and no-metered-spend posture |
| `/pause` | authenticated Lucas-only intent for the canonical actuator |
| `/resume` | authenticated, approval-required intent held locally while PAUSE remains present |
| `/approve <id> <digest>` | exact, one-shot approval bound to the current source and snapshot |

The source boundary does not contain Telegram credentials, send messages, remove PAUSE, or run an
actuator. Production auth, transport, durable outbox wiring, and service deployment remain Lucas's
seams. The second-box watchdog similarly verifies signed monotonic heartbeats and emits one
deduplicated controller-death attention intent per episode; key provisioning and delivery remain
external.

## 2. Sync — conductor session (drop in and steer)
```bash
ssh gs-recursion
cd /opt/recursion
hermes chat            # interactive, full tools, on the sub — redirect the loop live
```
Use this to change direction in the moment: reprioritise, resolve several INBOX items,
inspect state. For heavy multi-file reasoning you can also run `claude` here (Claude Max),
but that's the optional heavy tier — not the 24/7 path.

## 3. Declarative — the wheel (config as control)
The loop READS these every run; edit them and the change takes effect next cycle:
- `state/DECISIONS.md` — the decision log. Change a call here and the loop honours it.
- `ANCHOR.md` — scope and success criteria. What's out of scope stops re-entering.
- `loop/config/routing.yaml` — which model each tier uses.
Edit locally, commit, push; the box pulls. (Automated runs only ever touch `loop/auto`.)

## 4. Kill switch — PAUSE (the safety valve)
Halts **execution** instantly and preserves **awareness**: heartbeat, status answering,
durable escalation recording, and a PAUSED-labelled local brief all keep running. Every
human-visible notification enqueue and transport call stops, as do build arms, planning,
drain/conduct, reflection, pushes, and merges. D-012 (2026-08-06) preserved awareness instead of
exiting before the heartbeat; D-014 (2026-08-10) corrected its unsafe equation of awareness with
external delivery after a forced cycle bypassed the Phase 7 outbox under PAUSE.
```bash
ssh gs-recursion "cd /opt/recursion && bash scripts/pause.sh 'reason'"   # stop
ssh gs-recursion "cd /opt/recursion && bash scripts/resume.sh"           # resume
```
Every scheduled job checks `state/PAUSE` before doing anything.

## Health checks
```bash
ssh gs-recursion "crontab -l | grep recursion-"                 # jobs installed?
ssh gs-recursion "systemctl status hermes-gateway --no-pager"   # Telegram front alive?
ssh gs-recursion "ls -t /opt/recursion/loop/logs | head"        # recent run logs
ssh gs-recursion "cat /opt/recursion/state/briefs/\$(date +%F).md"  # today's brief
```

## Change the brief time
Default fires 06:30 UTC. To set it to your local morning (pick the UTC hour):
```bash
ssh gs-recursion "cd /opt/recursion && BRIEF_CRON='0 12 * * *' bash scripts/install-cron.sh"
```

## What the loop can and cannot do on its own (the guardrails)
- **Auto** (act, log to `loop/auto`): reversible, cheap, machine-verifiable work.
- **Notify** (act, then tell you): worth knowing, didn't need approval.
- **Escalate** (block on you): irreversible / spends money or proxies / ambiguous /
  unverifiable / anything mining (DO-NOT-DEPLOY) / any decision not in `DECISIONS.md`.
- Hard rules it never breaks: never `git push --force`; never merge to a protected/default
  branch; never edit its own auth/model/cron/autonomy flags; never fabricate project status.
  Project status comes from live git clones. See `AGENTS.md` and `CONTROL_PLANE.md`.

## V2 review publication

The sole source-authorized push path is the fail-closed review publisher:

```bash
cd /opt/recursion
python3 -u scripts/tests/recursion-v2-mvp.test.py
bash scripts/publish.sh --branch loop/work/recursion-v2-mvp --repo /opt/recursion
```

It refuses under PAUSE, when `RECURSION_ALLOW_PUSH` is not human-enabled, when the repository is
not active in `state/portfolio.tsv`, when the task is not `passed`, when the review ref is invalid,
or when the project publisher key is absent. It has no default-branch merge path.
