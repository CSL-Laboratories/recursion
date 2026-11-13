# core/recursion_core/plugin_security (build-order item 9, candidate)

In-process plugin loader (lane G2), the core approval gate and connection-keyed engine
breaker (O11), and installer-signed authority profiles (lane K3). Candidate code, not
reviewed or accepted. The user daemon calls it (see "Called by the daemon" below).

- `loader.PluginLoader` checks a signed `csl-plugin-manifest/vNext` and the artifact
  bytes, grants requested ∩ granted permissions bound to the artifact digest and signer
  key, refuses reserved (effect-owner) credentials and destinations, and reports
  findings at `verify`, `admit`, `stage` and `active`. A bad or untrusted signature loads
  as unsigned; verified is a label, not a gate. After every load attempt it reads
  `sys._is_gil_enabled()` and poisons the core if the GIL is on. A key revoked while
  plugins run (host op `key.revoke`) turns off `order` for every active plugin under
  that key at once (PRODUCT.md 4.4). The
  revocation is written to an installer-created SQLite store
  (`create_revocation_store`) and read back when the trust store is built, so a restart
  keeps the key revoked; without a store, or when the SQLite write fails, `key.revoke`
  still turns order off but refuses `revocation_not_durable` (the database error does
  not become `core_internal_error`). Every later load of a plugin under a revoked key,
  signed or manually granted, on the trusted list or not, has no `order` until the user
  grants it again with `Grant.order_after_revoked_key` naming that key (a manual grant
  still needs `unsigned_order_acknowledged`). Naming the key covers the first revocation
  only (`revocation_generation` 1, returned by `key.revoke`). Each later revoke advances
  that generation in the store, and order stays off until a new grant sets
  `Grant.order_after_revocation_generation` to it (PRODUCT.md 4.4).
- `approval.ApprovalGate` draws every approval from the effect's canonical bytes,
  accepts only the `operator_ui` principal, binds the decision to the digest and allows
  one use. A veto is final for that effect digest (CSL-743): a later request for the
  same effect, an approve of another pending approval for it, and a take of one
  approved earlier all refuse `approval_vetoed`; a retry is a new intent with a
  different digest. Expired, used and still-pending approvals do not block a re-request.
  Vetoes are kept in memory only, so a restart forgets them (known limit).
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
  A tripped connection clears through the host ops `breaker.probe_begin` and
  `breaker.probe_reset`, so a consumer holding only `client_for` clients can run the
  leased probe.

**Consumer changes since the first version of this API.** A consumer written against it
must change, or every effect is refused: create the breaker store and the revocation
store at install (`create_breaker_store`, `create_revocation_store`) and pass their
paths (`ConnectionBreaker(store_path)`, `TrustStore(..., revocation_store=...)`); bind
runs with owner, provider and model version (`run.bind` args `connection`,
`model_version`, `plugin_id`, `provider`, `run_id`); send the five-field effect
`scope` (`account`, `venue`, `contract_family`, `adapter`, `environment`), or
`check_effect` refuses `effect_scope`; sign profiles with the matching `scope`,
`provider`, `model_version` and `actions`; clear trips through the leased probe; and,
after a repeated `key.revoke`, copy `revocation_generation` onto
`Grant.order_after_revocation_generation`.

**Not a sandbox.** Plugins run as ordinary Python in the daemon's process and uid.
`tests/test_mutants.py` shows four in-process attacks that succeed (swap the anchor,
patch the verifier, rewrite the ledger, self-approve in memory). Containment needs a
separate OS user or process (F11, F21, F35). Blocked in the same table: a profile forged
with the plugin's own key, a replay after restart, a deleted ledger, and a plugin whose
manifest names itself `host` to reach host-only ops (`host` is a reserved plugin id).
Same-uid is not a security boundary. The real boundary is a separate OS user or process
(F21), and F21 is not done.

**2026-11-11 same-uid fallback (O69, Lucas's 2026-11-08 direction; not accepted text).** Lucas, 2026-11-08 (t1418u), O69 correction; not accepted text in CSL-Laboratories/csl-research contracts/PRODUCT.md 055cc7a5c793b7135e5d0565dc4198cf96230531 (SHA-256 e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf); receipt CSLR-D-011; claim is the quoted sentence; limitation is that the sentence is not accepted PRODUCT.md text and paper-only is PRODUCT.md 5.2 / CP0-v2:

> "it should have permission to run paper testing nov 11 regardless"

Implementation explanation (not Lucas's quoted words): under the same-uid fallback the signed authority profile is tamper evidence, not a
boundary: it binds honest plugins and records what happened, and the mutants above
still rewrite it. An in-process paper-order plugin holds paper-order permission on 2026-11-11.
Paper only, by PRODUCT.md 5.2 / CP0-v2 ("order authority stays paper-first"), not by O69: O69 is
the signed-profile item. This fallback grants no live-order permission; live capital stays
separately authorised (PRODUCT.md 5.2). The code has one `order` permission.
`scope.environment` is signed into the profile and the approval digest and must match
exactly, so a `paper` profile cannot charge a `live` effect; but the core does not
refuse any environment name itself, so paper only is what the installer signs, not a
runtime check. The quote is frozen in
`policy.SAME_UID_FALLBACK_POLICY` and `tests/test_o69_policy.py` checks this section
against it.

Production runtime: CPython 3.14 free-threaded, started with `-X gil=0`
(`runtime.require_production_runtime`). Signature checks need `cryptography`
(`requirements.txt`); without it every verification refuses, and the daemon loads no
plugin at all (next section).

## Called by the daemon

Spec: PRODUCT.md d84cdd26 (CSLR-D-012), sections 4.1 to 4.3. `daemon/plugins.py`
(`PluginHost`, built by `daemon/control_adapter.compose_control` at daemon start) is the
only place the daemon loads plugin code, and it does so only through `PluginLoader.load`:
the daemon reads each installed plugin as bytes and never imports one by name.

- Installed plugins are `$XDG_CONFIG_HOME/recursion/plugins/<name>/` (`manifest.json`,
  `manifest.sig`, `plugin.py`) plus every `--plugin DIR`. Trusted keys, grants and reserved
  effects come from `$XDG_CONFIG_HOME/recursion/plugins/install.json`. Every file must be
  operator-owned and not group- or other-writable.
- A plugin the loader refuses (bad manifest, artifact digest mismatch, reserved credential
  or destination, compile or import failure) is kept out. Its typed code and findings are
  in the inventory (`plugin.list` on the control socket, and a `plugin:<id>` row on the
  GUI's route table), and the daemon keeps running without it. A plugin with a bad or
  untrusted signature loads as unsigned, as section 4.3 says, and a grant bound to a signer
  then grants it nothing.
- **No verification, no load.** If `cryptography` cannot be imported, no signature can be
  checked, so the daemon refuses every installed plugin with
  `plugin_verification_unavailable: cryptography missing; install recipe in
  plugin_security/README.md`. Nothing is loaded as "unsigned" in that state and nothing
  is skipped quietly; the refusal is logged and shown on every surface.
- A loaded plugin's `registry()` panels need the `panel` permission and its
  `operator(command)` O7 reads need `read`. `bind_core(call)` receives the plugin's
  `csl-core/v0` client. No surface can approve or veto yet, and every run is bound to
  `host`, so no effect is authorized today. The authority ledger is never created by the
  daemon; without an installer-made ledger an order effect refuses `authority_unconfigured`.
- The breaker store is `$XDG_STATE_HOME/recursion/plugin-security/breaker.sqlite3`. There
  is no installer yet, so the daemon creates it on first start (`create_breaker_store`)
  and opens it on every later one; `daemon/breaker.py` writes typed connection limits, run
  bindings and faults to it, so they survive a daemon restart. A store that was deleted
  is created empty again and the daemon logs that. `breaker.py` here is unchanged.

### Consumer recipe: a free-threaded interpreter with `cryptography`

The venv must be on a filesystem that allows exec (not a `noexec` mount). Checked with
`cryptography` 50.0.2 (its `cp314t` wheel) and `cffi` 2.1.1:

```sh
python3.14t -m venv ~/.local/lib/recursion/venv
~/.local/lib/recursion/venv/bin/python -m pip install --only-binary=:all: -r core/recursion_core/plugin_security/requirements.txt
PYTHONPATH=core ~/.local/lib/recursion/venv/bin/python -X gil=0 -c "import sys, cryptography; assert not sys._is_gil_enabled()"
PYTHONPATH=core ~/.local/lib/recursion/venv/bin/python -X gil=0 -B -m unittest discover -s core -t core
```

The third line fails if `cryptography` is missing or if importing it turned the GIL on.
Run the daemon with that interpreter (`daemon/README.md`).

## Tests

From the repository root:

```sh
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/plugin_security/tests -t core -v
```

`tests/test_runtime.py` also runs a real `python3.14t` in subprocesses when one is
installed, and compiles a small C extension to show the GIL check; it skips otherwise.
`tools/ci_gate.py` does not collect `core/` tests yet (its `TEST_GLOBS` edit is a
separate protected-path change).

The frozen ConnectionBreaker surface and its refusal codes are listed in `BREAKER-CONTRACT.md` and pinned by `tests/test_breaker_contract.py`.
