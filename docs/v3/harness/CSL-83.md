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
   ordered by weight. An explicit `--provider` still runs first. A weight
   that is not a positive integer is rejected.
2. **Cooldown.** A rate limit, exhausted quota, or expired auth is recorded
   in `<state-dir>/cooldowns.json` and copied onto the run record. The file
   stores a reason and a reset time, not process output. A reported reset
   time wins, then a retry delay, then `routing.cooldown_seconds` (60).
   Later runs in that state directory skip the provider until the reset
   time. Other failures do not cool down and do not fall back.
3. **Fallback.** When the selected provider fails for one of those three
   reasons, the next capable provider runs. A cooled provider is not called.
   A review uses a different provider than the worker. If every reviewer is
   cooled or fails the same way, the run is unreviewed unless
   `accept_unreviewed` is set.
4. **Caps.** Review output is clipped at 8,000 characters, a review diff at
   2,000, and the whole review prompt at 16,000. Dependency context clips
   each summary and diff, then the whole section at 6,000 characters. The
   clip happens before the adapter receives the prompt.
5. **Pre-call budget.** Before each adapter call, a known figure already at
   its ceiling refuses the call. The prompt's character length is the
   minimum input that call will send. It is added to known input usage.
   Missing prior usage is not stored as zero. The call is not started when
   that minimum would pass `max_input_tokens`. Provider-reported usage is
   still checked after the call returns.

## What this does not prove

- No live Claude, Codex, or Grok process is started.
- Character length is not a token meter. It only stops a prompt that cannot
  fit under the existing ceiling.
- Passing the gate is not v3 release acceptance.
