# core/recursion_core/plugin_security (build-order item 9, candidate)

In-process plugin loader (lane G2), the core approval gate and connection-keyed engine
breaker (O11), and installer-signed authority profiles (lane K3). Candidate code: not
yet called by a running daemon, and not reviewed or accepted.

- `loader.PluginLoader` checks a signed `csl-plugin-manifest/vNext` and the artifact
  bytes, grants requested ∩ granted permissions bound to the artifact digest and signer
  key, refuses reserved (effect-owner) credentials and destinations, and reports
  findings at `verify`, `admit`, `stage` and `active`. A bad or untrusted signature loads
  as unsigned; verified is a label, not a gate. After every load attempt it reads
  `sys._is_gil_enabled()` and poisons the core if the GIL is on.
- `approval.ApprovalGate` draws every approval from the effect's canonical bytes,
  accepts only the `operator_ui` principal, binds the decision to the digest and allows
  one use.
- `authority.Authority` verifies installer-signed `csl-authority-profile/vNext`
  profiles against an injected public anchor and enforces scope, window and budgets on a
  durable SQLite ledger (split, replayed and restarted spending cannot reset a cap).
- `breaker.ConnectionBreaker` trips per billed connection on typed limits only.
- `protocol.CoreService` is the one boundary: canonical JSON bytes in and out
  (`csl-core/v0`, ops in `protocol.json`), so a later Rust core can reuse the tests.

**Not a sandbox.** Plugins run as ordinary Python in the daemon's process and uid.
`tests/test_mutants.py` shows four in-process attacks that succeed (swap the anchor,
patch the verifier, rewrite the ledger, self-approve in memory). Containment needs a
separate OS user or process (F11, F21, F35). Blocked in the same table: a profile forged
with the plugin's own key, a replay after restart, a deleted ledger, and a plugin whose
manifest names itself `host` to reach host-only ops (`host` is a reserved plugin id).
Same-uid is not a security boundary. The real boundary is a separate OS user or process
(F21), and F21 is not done.

**Nov 11 same-uid fallback (O69, decided).** Lucas, Nov 8 (t1415u), PRODUCT.md 055cc7a5
O69:

> "the in-process Cockpit plugin holds paper-order permission on Nov 11 under the same-uid fallback. The signed authority profile is tamper evidence there, not a boundary, and the docs say so. Paper only; live capital stays separately authorised (5.2)."

So under the same-uid fallback the signed authority profile is tamper evidence, not a
boundary: it binds honest plugins and records what happened, and the mutants above
still rewrite it. The in-process Cockpit plugin holds paper-order permission on Nov 11.
Paper only. This fallback grants no live-order permission; live capital stays
separately authorised (PRODUCT.md 5.2). The code has one `order` permission and does not
tell paper from live venues (`scope.venue` is an opaque string), so paper only is what
the installer signs, not a runtime check. The quote is frozen in
`policy.SAME_UID_FALLBACK_POLICY` and `tests/test_o69_policy.py` checks this section
against it.

Production runtime: CPython 3.14 free-threaded, started with `-X gil=0`
(`runtime.require_production_runtime`). Signature checks need `cryptography`
(`requirements.txt`); without it every verification refuses.

## Tests

From the repository root:

```sh
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/plugin_security/tests -t core -v
```

`tests/test_runtime.py` also runs a real `python3.14t` in subprocesses when one is
installed, and compiles a small C extension to show the GIL check; it skips otherwise.
`tools/ci_gate.py` does not collect `core/` tests yet (its `TEST_GLOBS` edit is a
separate protected-path change).
