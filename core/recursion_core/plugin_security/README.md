# core/recursion_core/plugin_security (build-order item 9, candidate)

In-process plugin loader (lane G2), the core approval gate and connection-keyed engine
breaker (O11), and installer-signed authority profiles (lane K3). Candidate code: not
yet called by a running daemon, and not reviewed or accepted.

- `loader.PluginLoader` checks a signed `csl-plugin-manifest/vNext` and the artifact
  bytes, grants requested ∩ granted permissions bound to the artifact digest and signer
  key, refuses reserved (effect-owner) credentials and destinations, and reports
  findings at `verify`, `admit`, `stage` and `active`. A bad or untrusted signature loads
  as unsigned; verified is a label, not a gate. After every load attempt it reads
  `sys._is_gil_enabled()` and poisons the core if the GIL is on. A key revoked while
  plugins run (host op `key.revoke`) turns off `order` for every active plugin under
  that key at once (PRODUCT.md 4.4); turning it back on is a fresh grant and load. The
  revocation is written to an installer-created SQLite store
  (`create_revocation_store`) and read back when the trust store is built, so a restart
  keeps the key revoked; without a store `key.revoke` still turns order off but refuses
  `revocation_not_durable`.
- `approval.ApprovalGate` draws every approval from the effect's canonical bytes,
  accepts only the `operator_ui` principal, binds the decision to the digest and allows
  one use.
- `authority.Authority` verifies installer-signed `csl-authority-profile/vNext`
  profiles against an injected public anchor and enforces scope (account, venue, contract
  family, adapter, environment), provider and model version, permitted actions, window
  and budgets on a durable SQLite ledger (split, replayed and restarted spending cannot
  reset a cap). Every field matches exactly; unknown fails closed. Not built from the 4.1
  field set: legal perimeter, information restrictions, stop and recovery owners,
  evidence dossier, human receipt.
- `breaker.ConnectionBreaker` trips per billed connection on typed limits only and
  clears only through a one-use, expiring probe lease. Run bindings (connection, owning
  plugin, provider, model version), faults and trips are kept in an installer-created
  SQLite store and survive a restart. An effect may cite only a run its own plugin owns.
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

**Nov 11 same-uid fallback (O69, decided).** Lucas, Nov 8 (t1418u), O69 correction; not yet in PRODUCT.md 055cc7a5:

> "it should have permission to run paper testing nov 11 regardless"

Implementation explanation (not Lucas's quoted words): under the same-uid fallback the signed authority profile is tamper evidence, not a
boundary: it binds honest plugins and records what happened, and the mutants above
still rewrite it. The in-process Cockpit plugin holds paper-order permission on Nov 11.
Paper only. This fallback grants no live-order permission; live capital stays
separately authorised (PRODUCT.md 5.2). The code has one `order` permission.
`scope.environment` is signed into the profile and the approval digest and must match
exactly, so a `paper` profile cannot charge a `live` effect; but the core does not
refuse any environment name itself, so paper only is what the installer signs, not a
runtime check. The quote is frozen in
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
