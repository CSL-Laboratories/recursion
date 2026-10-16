# CSL-82 — land, clean, logs, and the remaining adapters

Date: 2026-10-15. Parent issue: CSL-82, which follows CSL-27. This note records
the slice. It does not select a product language, store, framework, or sandbox,
and it is not OR-022's native loop.

Gate, from the repository root:

```text
python -X utf8 -B tools/ci_gate.py --force
```

That command is provider-free. The new tests are
`scripts/tests/v3/harness/ops.test.py`. They use a temporary git repository
and stub every provider process. They do not start Claude, Codex, Grok,
Gemini, Cursor, or OpenCode.

## Commands

`land <run-id>` merges the commit saved at acceptance into the checked-out
branch with `--no-ff`. It merges that SHA, and it refuses when the branch
tip is different. `master` and `main` are refused. The branch is
`run_branch` on the run record, or `recursion/run/<run-id>` when that field
is absent. A run that is not accepted is refused. A dirty worktree is
refused before git writes, including an ignored file the merge would
overwrite. A conflict aborts the merge and leaves the tree clean. This slice
does not switch branches and does not create a worktree.

`clean` deletes a finished run's record, log, and private workspace folder.
Finished statuses are `completed`, `unreviewed`, `review-rejected`,
`budget-exceeded`, `failed`, and `timed-out`. A named run that is still in
progress is refused and left in place. A sweep removes the finished runs and
lists the others under `refused`.

`logs <run-id>` prints that run's JSONL events. Token-shaped text is replaced
with `[redacted]` when the event is written and again when it is printed.
Ordinary words stay.

`run --branch NAME` stores the branch and, when the run is accepted and that
branch already exists in the working repo, the commit SHA `land` will merge.
It does not create the branch.

Doctor reports `unknown` for Gemini, Cursor, and OpenCode sign-in. That
value stays `unknown`. It does not by itself mark an installed adapter
unusable.

## Launch-critical adapters

The seven names are the accepted list in
[compatibility.json](../foundation-research/mark2-research/compatibility.json).
Flag names below were read from public docs on 2026-10-15. No binary was
executed here, so this is source-documented, not a local probe.

| Name | This slice | Interface |
|---|---|---|
| Claude | already behind `--allow-live` | `claude -p` stream-json |
| Grok | already behind `--allow-live` | `grok` streaming-json |
| Gemini | behind `--allow-live`, disabled by default | `gemini -p --output-format stream-json --approval-mode yolo --skip-trust` |
| Cursor | behind `--allow-live`, disabled by default | `agent -p --output-format stream-json --force --trust` |
| OpenCode | behind `--allow-live`, disabled by default | `opencode run --format json --auto` |
| Hermes | deferred | TUI gateway JSON-RPC |
| ChatGPT | deferred | `codex app-server` JSON-RPC over stdio (`--listen stdio://`) |

`codex exec --json` stays the existing Codex adapter. The matrix does not mark
that entry launch-critical. ChatGPT's accepted interface is the app server,
which this slice does not speak.

Hermes documents three programmatic protocols: ACP, TUI gateway JSON-RPC, and
an OpenAI-compatible HTTP API. `hermes chat -q` is a single-query CLI, and the
fetched page does not document a JSON event stream for it. The missing
interface named here is the TUI gateway JSON-RPC, which those docs describe
as the custom-host surface.

Cursor's documented command is `agent`. `agent acp` is a separate server and
is not this adapter. Gemini has no working-directory flag in this command, so
the process cwd is the run's private folder. The Gemini task is stdin.
`-p` is a single space so the flag has a value, and the task text is not a
command-line argument. OpenCode's `-p` is a password flag and is not used. A
long OpenCode prompt is a private file passed with `--file` and removed when
the process returns.

Shipped config still enables only `fixture` and `fixture-b`.

## Sources

- [Gemini CLI headless mode](https://www.geminicli.com/docs/cli/headless) (retrieved 2026-10-15)
- [Gemini CLI cheatsheet](https://geminicli.com/docs/cli/cli-reference.md) (retrieved 2026-10-15)
- [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters) (retrieved 2026-10-15)
- [Cursor CLI overview](https://cursor.com/docs/cli/overview) (retrieved 2026-10-15)
- [OpenCode CLI](https://docs.opencode.ai/docs/cli) (retrieved 2026-10-15)
- [Hermes programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration) (retrieved 2026-10-15)
- [Hermes CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli) (retrieved 2026-10-15)
- [Codex app-server](https://developers.openai.com/codex/app-server) (retrieved 2026-10-15)

## What this does not prove

- No live provider process was started.
- Passing the gate is not v3 release acceptance.
- A documented flag is not a qualified subscription meter.
