# CSL-81 — upstream CLI contracts and the Codex usage meter

Date: 2026-10-15. Parent: CSL-81, follow-up to [CSL-27](CSL-27.md).
This note pins the contracts that were checked and names the Codex meter.
It does not select a language, store, framework, or sandbox. It does not
change the product ceilings: 50,000,000 input tokens and 8,000,000 output
tokens.

Checked on 2026-10-15 with the Parallel Search MCP against public docs.
No `claude`, `codex`, `grok`, `agent`, `gemini`, `opencode`, or `hermes`
binary was on `PATH`. None was started. Every auth probe, argv execution,
and live stream below is **locally unmeasured**.

## Pins

| CLI | Version checked | Class |
|---|---|---|
| Claude Code | Changelog label **2.1.300**, dated 2026-10-14 ([changelog](https://code.claude.com/docs/en/changelog)). The npm page for `@anthropic-ai/claude-code` showed **2.1.296** (page date 2026-10-13) ([npm](https://www.npmjs.com/package/@anthropic-ai/claude-code)). The headless and CLI pages are unversioned living docs. | Source-documented. The two version numbers are different publications, not one install. |
| Codex | npm `@openai/codex` **0.162.1** (page date 2026-10-14) ([npm](https://www.npmjs.com/package/@openai/codex)). Usage reports name earlier builds: [openai/codex#17539](https://github.com/openai/codex/issues/17539) (2026-04-12; no build in the excerpt) and [openai/codex#50586](https://github.com/openai/codex/issues/50586) (2026-10-03, source `rust-v0.158.0`). | Source-documented. Behavior of 0.162.1 was not re-measured. |
| Grok | No build number on the pages checked. Headless page date 2026-06-10, reference page date 2026-07-21 ([headless](https://docs.x.ai/build/cli/headless-scripting), [reference](https://docs.x.ai/build/cli/reference)). `grok version` is the documented command. | Version **unmeasured**. |

## Claude Code

Auth probe. Source-documented: `claude auth status` prints JSON. `authMethod`
is one of `none`, `claude.ai`, `oauth_token`, `api_key`, `api_key_helper`, or
`third_party`. The docs say exit 0 when logged in and 1 when not.
`configDirectory` requires v2.1.268 or later
([CLI reference](https://code.claude.com/docs/en/cli-reference)). The harness
treats `loggedIn: true` as signed in even when the process exits 1. That rule
comes from the CSL-27 fork report. It was not re-observed here.

Headless argv. Source-documented: `claude -p` (`--print`) with
`--output-format stream-json` and `--verbose`. The prompt is the argument or
stdin ([headless](https://code.claude.com/docs/en/headless)). The adapter argv
is `claude -p --output-format stream-json --verbose --dangerously-skip-permissions`,
and the prompt is stdin. The first three flags match that page.
`--dangerously-skip-permissions` is quoted on v2.1.117 in
[anthropics/claude-code#52506](https://github.com/anthropics/claude-code/issues/52506)
(2026-04-23) as bypassing permission checks, with a report that a dialog still
appeared. It was not re-checked on 2.1.300.

Stream events. Source-documented names on the headless page: `system`
(subtypes include `init`, `api_retry`, `plugin_install`), `assistant`, `user`,
`stream_event`, and `result`. The last stream-json line is a `result` message
with the final text, cost, and session metadata. The cost page says that
result carries `total_cost_usd` and cumulative `usage`, and that a resumed
session counts earlier spend
([cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)).
Headless `json` output includes `total_cost_usd`. The token field names inside
`usage` were not quoted on those pages. Locally observed: not run.

## Codex

Auth probe. Source-documented: `codex login status` exits 0 when credentials
are present ([developer commands](https://learn.chatgpt.com/docs/developer-commands.md)).
`codex login` with no flags opens a browser for ChatGPT OAuth. The doctor
runs `login status` and does not read the credential file. Locally observed:
not run.

Headless argv. Source-documented: `codex exec --json` writes JSONL.
`codex exec -` reads the prompt from stdin. `resume` and `fork` exist and were
not used by this adapter
([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md),
[developer commands](https://learn.chatgpt.com/docs/developer-commands.md)).
Event names: `thread.started`, `turn.started`, `turn.completed`,
`turn.failed`, `item.*`, `error`. The official sample line is:

```json
{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":0}}
```

`--cd` appears in `docs/getting-started.md` at commit `88abbf58` as the
working-root flag
([getting started](https://github.com/openai/codex/blob/88abbf58cec9843a7cebe250d71146314ebbcf1a/docs/getting-started.md)).
It was not in the non-interactive excerpt. `--approve-for-me` is on the
adapter argv and was not in the extracted text of the pages above.
Unverified on 0.162.1. The adapter argv is `codex exec --json --approve-for-me`,
plus `--cd` when a workspace is set, and `-` for the stdin prompt.

## Codex meter

Recommend the **cumulative session total**: the last `turn.completed` usage
in one `codex exec` stream. `codex_session_meter` in
`scripts/v3/harness/usage.py` implements that. The label is
`cumulative-session-total`.

Evidence class: **source-documented**, not a measurement from this repo.

- [openai/codex#17539](https://github.com/openai/codex/issues/17539) (2026-04-12)
  says `turn.completed` reports cumulative session totals. The interactive
  CLI keeps per-request `ThreadTokenUsage.last`. The exec JSONL discards
  `last` and emits `total`. The reported example is 6.9M `input_tokens`
  against a 272k window.
- [openai/codex#50586](https://github.com/openai/codex/issues/50586)
  (2026-10-03, `rust-v0.158.0`) says a successful turn emits `turn.completed`
  from the stored total, and `turn.failed` omits usage. The issue asks OpenAI
  to document cumulative versus increment. That request is not evidence the
  field became per-turn.
- The non-interactive sample names `input_tokens`, `cached_input_tokens`,
  `output_tokens`, and `reasoning_output_tokens` on `turn.completed`. One
  sample line cannot show whether the numbers grow across turns.

The meter does not use per-turn `last` usage. That field is not in the JSONL.
It does not sum `turn.completed` events inside one stream: they are snapshots
of one session. A missing usage stays unknown, including a `turn.failed`
with no usage object. `cached_input_tokens` and `reasoning_output_tokens` are
not added into `input_tokens` or `output_tokens`. Whether `input_tokens`
already includes the cached count is **unmeasured**.

Separate `codex exec` processes in this adapter do not pass `resume` or
`fork`. Their invocation totals are added. That addition is **inferred**
from this adapter's argv. It is not a Codex measurement. A later resume or
fork of the same session must not be added as if it were a new session
([EXECUTION-FABRIC](../EXECUTION-FABRIC.md) IPR-003).

The product ceilings stay 50,000,000 input tokens and 8,000,000 output
tokens. A configured 1,000,000 input ceiling still stops the 6.9M example.
The 6.9M figure remains the public report in issue 17539.

## Grok

Auth probe. Source-documented: `grok login` (with `--device-auth` for
headless) and `grok logout`. ACP can use `cached_token` or `XAI_API_KEY`.
Sessions are under `~/.grok/sessions`
([reference](https://docs.x.ai/build/cli/reference),
[headless](https://docs.x.ai/build/cli/headless-scripting)). The doctor treats
a non-empty `~/.grok/auth.json` as signed in. That path was not on the
official pages checked. A public gist states it
([gist](https://gist.github.com/ben-vargas/579a41b91849c354d9c32d8dac590f1f)).
Locally observed: not run.

Headless argv. Source-documented: `-p` / `--single`, `--output-format` of
`plain`, `json`, or `streaming-json`, `--always-approve`, and `--cwd`.
Scripts are told to pass `--no-auto-update`
([headless](https://docs.x.ai/build/cli/headless-scripting.md)). The adapter
uses `--output-format streaming-json`, `--always-approve`, `--no-auto-update`,
`--cwd`, and `--prompt-file`. `--prompt-file` was not on those official
pages. A third-party wiki lists it
([grok-wiki](https://grok-wiki.com/public/docs/xai-org-grok-build-90205de50458)).
The adapter argv was not changed.

Stream events. The official page says streaming-json is newline-delimited
JSON and does not name event types. The harness joins `type: text` chunks
and skips thoughts and tool calls. Those names are the CSL-27 rule, not a
local observation of the current CLI.

## Gaps

These five were not qualified. No adapter was added, and none was run.

- **Cursor.** Headless print mode is published: `agent -p`, with
  `--output-format` `text`, `json`, or `stream-json`
  ([headless](https://cursor.com/docs/cli/headless),
  [output format](https://cursor.com/docs/cli/reference/output-format)).
  Named events include `assistant`, `tool_call`, and `result`. The quoted
  `result` object has no usage fields. No version pin.
- **Gemini.** A headless page exists at tag `v0.62.0`: `-p` / `--prompt`,
  `--output-format`, and events `init`, `message`, `tool_use`,
  `tool_result`, `error`, and `result`
  ([headless.md](https://github.com/google-gemini/gemini-cli/blob/v0.62.0/docs/cli/headless.md)).
  `v0.62.0` is the tag on that page, not a check that it is current.
- **Hermes.** The CLI reference dated 2026-10-14 documents `hermes chat -q`,
  `--format stream-json`, and `--usage-file` for a JSON usage report
  ([CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)).
  No release pin. Stream event names were not in the excerpt.
- **OpenCode.** `opencode run` takes `--format json` for raw JSON events.
  `opencode auth list` lists providers. `opencode stats` reports token and
  cost statistics. `opencode upgrade v0.1.48` is an example command, not a
  current pin ([CLI](https://opencode.ai/docs/cli/)). Event names inside
  `--format json` were not in the excerpt.
- **ChatGPT.** No separate headless CLI argv or usage stream was in the
  pages checked. Codex login uses ChatGPT OAuth. The Codex changelog for
  2026-07-09 says Codex joined the ChatGPT desktop app 26.707
  ([changelog](https://developers.openai.com/codex/changelog)). That is a
  desktop app version, not a headless contract.

## What this does not prove

- No live Claude, Codex, or Grok process ran.
- Documentation versions are not `cli --version` output.
- A passing gate is not v3 release acceptance.

Gate, from the repository root. It does not start a provider:

```text
python -X utf8 -B tools/ci_gate.py --force
```

## Sources

- [Claude Code changelog](https://code.claude.com/docs/en/changelog) (2.1.300, 2026-10-14)
- [@anthropic-ai/claude-code](https://www.npmjs.com/package/@anthropic-ai/claude-code) (2.1.296, page date 2026-10-13)
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Track cost and usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- [anthropics/claude-code#52506](https://github.com/anthropics/claude-code/issues/52506) (2026-04-23)
- [@openai/codex](https://www.npmjs.com/package/@openai/codex) (0.162.1, page date 2026-10-14)
- [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md)
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands.md)
- [openai/codex getting-started.md at 88abbf58](https://github.com/openai/codex/blob/88abbf58cec9843a7cebe250d71146314ebbcf1a/docs/getting-started.md)
- [openai/codex#17539](https://github.com/openai/codex/issues/17539) (2026-04-12)
- [openai/codex#50586](https://github.com/openai/codex/issues/50586) (2026-10-03)
- [ChatGPT and Codex changelog](https://developers.openai.com/codex/changelog)
- [Grok headless and scripting](https://docs.x.ai/build/cli/headless-scripting)
- [Grok CLI reference](https://docs.x.ai/build/cli/reference)
- [Cursor headless CLI](https://cursor.com/docs/cli/headless)
- [Cursor output format](https://cursor.com/docs/cli/reference/output-format)
- [Gemini CLI headless.md at v0.62.0](https://github.com/google-gemini/gemini-cli/blob/v0.62.0/docs/cli/headless.md)
- [Hermes CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands) (2026-10-14)
- [OpenCode CLI](https://opencode.ai/docs/cli/)
