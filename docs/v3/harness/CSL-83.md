# CSL-83 — weights, cooldowns, fallback, and caps

Date: 2026-10-15. Parent issue: CSL-27. This note covers the optimization
slice on top of the CSL-27 harness. It does not select a language, store,
framework, or sandbox, and it does not change the default token ceiling.

Gate, from the repository root:

```text
python -X utf8 -B tools/ci_gate.py --force
```

The tests stub every provider. They do not start Claude, Codex, or Grok.

## What was already decided

OR-024 ranks responsiveness, then subscription efficiency, then completion,
under correctness, authorization, resource limits, and recoverability. The
ranking is not a hidden score. `routing.weights` is the user's explicit
order: a higher weight is tried first, and equal weights keep
`routing.preference`. Missing usage stays unknown. The default ceilings stay
50,000,000 input tokens and 8,000,000 output tokens (CSL-81 did not choose
a different meter).

## Behavior

1. **Weights.** Enabled providers that have the required capabilities are
   ordered by weight. An explicit `--provider` still runs first. If that
   provider is rate-limited, out of quota, or unauthenticated, the next
   provider of the same class runs, and the run prints a notice that the
   requested provider was replaced. A weight that is not a positive integer
   is rejected.
2. **Cooldown.** A rate limit, exhausted quota, or expired auth is recorded
   in `<state-dir>/cooldowns.json` and copied onto the run record. The file
   stores a reason and a reset time, not process output. A reported reset
   time wins, then a retry delay, then `routing.cooldown_seconds` (60).
   Later runs in that state directory skip the provider until the reset
   time. Other failures do not cool down and do not fall back.
3. **Fallback.** When the selected provider fails for one of those three
   reasons, the next capable provider of the same class runs. An offline
   provider falls back only to another offline provider, and a live provider
   falls back only to another live provider. `--allow-live` does not let an
   offline failure start a paid CLI. A cooled provider is not called. A
   review uses a different provider than the worker. If every reviewer is
   cooled or fails the same way, the run is unreviewed unless
   `accept_unreviewed` is set.
4. **Caps.** Review output is clipped at 8,000 characters, a review diff at
   2,000, and the whole review prompt at 16,000. Dependency context clips
   each summary and diff, then the whole section at 6,000 characters. The
   clip happens before the adapter receives the prompt.
5. **Pre-call budget.** Before each adapter call, a known figure already at
   its ceiling refuses the call. A nonempty prompt reserves one input token.
   If the remaining known input allowance is below one, the call is refused,
   including when prior usage is still unknown. Character length is not a
   token estimate and is not added to usage. Missing prior usage is not
   stored as zero. Provider-reported usage is still checked after the call
   returns.

## What this does not prove

- No live Claude, Codex, or Grok process is started.
- Reserving one input token is not a token meter. It only refuses a
  nonempty prompt when the remaining known input allowance is below one.
- Passing the gate is not v3 release acceptance.
