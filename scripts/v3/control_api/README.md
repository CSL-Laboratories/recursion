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
| Recorded run | `record.py`, `scripts/tests/v3/control_api/recordings/spine-fake-run.json` | Fresh run must equal the committed recording byte for byte. |
| Frozen-SHA rerun | `rerun_frozen_sha.sh` | One 40-hex SHA in, one evidence file out. |

Methods: `service.info`, `route.list`, `session.open`, `session.run`,
`session.get`, `session.close`, `events.poll`.

## Not built (unverified or deferred)

- The WP11 spine memo (csl-research#50) was not readable from this run, so no
  spine item beyond the task's stated minimum was built. See `state/INBOX.md` esc-0002.
- No in-flight cancel: `session.run` is synchronous and a busy session is rejected, not queued.
- No QR pairing, E2E relay, non-loopback listener, Control MCP, ACP face, A2A card,
  installer, TUI or GUI. Anything non-loopback needs a fresh admission.
- No rate limit on failed authentication, and tokens live in memory only.
- A thin direct-CLI fallback (Lucas, 2026-10-26) is not built here. When it is, it
  must be labeled as not satisfying X2.
- Windows and macOS are untested. The recording is a Linux run.
