# Control API, spine slice (WP16: CSL-495, CSL-498, CSL-503)

Draft, fake-driver only, not accepted and not normative. It stacks on Route
Contract v0 (R#302, CSL-496) and adds no live capability: every live adapter
stays `unsupported` until CSL-460, and nothing in this package spawns a provider.

## What exists

| Piece | File | Notes |
|---|---|---|
| Authenticated JSON-RPC 2.0 core | `service.py`, `auth.py`, `errors.py` | Bearer token bound to one tenant and a closed scope set (`observe`, `control`). Authentication runs before the body is parsed. Closed typed params. |
| Observer journal | `events.py` | Bounded, tenant-filtered, gapless `seq`, `reset` flag on a gap. Digests and fixed tokens only; no timestamps. |
| Fake Claude-Code-style and Codex-style engines | `fake_engines.py` | Inspect the real zero-tools argv, answer in each engine's own output shape, deterministic. |
| Loopback HTTP, client, local CLI | `http_server.py`, `client.py`, `__main__.py` | `POST /v1/rpc`, long-poll `GET /v1/events`. Loopback bind only. Token file is owner-only and never printed. |
| Control MCP (RS2) | `mcp_server.py` | Newline-delimited stdio MCP; one tool per Control API method, all through the same authenticated dispatch. `mcp-fake` serves a fake stack, or proxies to a loopback URL with a token file. |
| Direct-CLI fallback | `direct_cli.py` | `direct-fake --engine claude\|codex --prompt ...`. One fake-driver run that bypasses the Control API. **Does not satisfy X2**; output carries `satisfies_x2: false`. Neutral workload IDs `direct-cli` / `proc-req-direct-cli`. |
| Recorded run | `record.py`, `scripts/tests/v3/control_api/recordings/spine-fake-run.json` | Fresh run must equal the committed recording byte for byte. |
| Frozen-SHA rerun | `rerun_frozen_sha.sh` | One 40-hex SHA in, one evidence file out. |

Methods: `service.info`, `route.list`, `session.open`, `session.run`,
`session.get`, `session.close`, `events.poll`.

## Spine reconciliation (WP11 memo, csl-research#50, supplied 2026-10-26)

| Memo item | Status here |
|---|---|
| RS1 Route Contract v0 and fake-driver conformance | R#302 / R#301 (base of this stack); live route only if CSL-460 lands by Nov 5 |
| RS2 Control API and Control MCP, driven by recorded Codex and Claude Code runs on Linux | Built: core, engines, HTTP, recorded run, Control MCP |
| RS3 Daemon, CLI and a Linux GUI | CLI built (`serve-fake`, `call`, `events`, `mcp-fake`, `direct-fake`). `serve-fake` is a foreground process; no daemonization, no GUI |
| RS4 Frozen-SHA rerun Mon Nov 9; published numbers against matched baselines | Rerun script built; no published numbers or baselines |
| Built but named stretch or deferred by the memo | None. The direct-CLI fallback is outside the memo (Lucas-confirmed, not X2) |

## Not built (unverified or deferred)

- RS3 daemon and Linux GUI, and RS4 published numbers against matched baselines.
- No in-flight cancel: `session.run` is synchronous and a busy session is rejected, not queued.
- No QR pairing, E2E relay, non-loopback listener, ACP face, A2A card,
  installer, TUI or GUI. Anything non-loopback needs a fresh admission.
- No rate limit on failed authentication, and tokens live in memory only.
- The direct-CLI fallback is a labeled fallback only (Lucas, 2026-10-26 12:38 PM PT).
- Windows and macOS are untested. The recording is a Linux run.
