# approval_scope

Domain-neutral approval prompt text and approval scope for the Recursion core (CSL-722;
PRODUCT.md 4.2, I-42, O74).

- `ApprovalRequest.create(effect, display, expires_at)` takes the SHA-256 digest over the
  effect's canonical bytes (`csl-cj/v0`, same form as `plugin_security`). Callers cannot
  pass a digest or a requester in, display fields never change the digest, and a request
  whose bytes are not canonical (or over 4096 bytes) refuses to construct.
- `render_prompt(request, requester)` writes a fixed header (request id, digest, requester,
  expiry), then the effect itself drawn by the core from the canonical bytes behind a `> `
  gutter, then the caller's labelled fields as untrusted text behind a `| ` gutter, with
  control, bidi and zero-width characters shown as `<U+XXXX>`. Nothing defaults to approve.
- `CredentialRegistry` mints credentials (`issue`, `revoke`). The authority accepts only the
  credential objects its registry issued; a hand-built or copied `Credential` is refused
  with `credential_unknown`. Code in the same process that holds the registry can still
  mint; whether that path is a boundary is open under O69.
- `ApprovalAuthority(clock, registry)` opens requests (`open(credential, request)`, where
  the opener must hold `control` and the prompt's requester is the opener's principal;
  `observe`-only or scope-less credentials are refused with `open_scope_required`),
  serves the approver inbox (`pending(credential)` lists open requests with the prompt the
  core drew; `show(credential, request_id)` returns one) to approver credentials only, grants an `ApprovalScope` to an
  `operator_ui` or `paired_device` credential holding the `approve` scope (`manage` not
  needed; pairing never grants it), and checks `approve`/`deny`. Scopes are non-delegable,
  bound to fixed `(request_id, digest)` pairs, each used once, and expire no later than
  the earliest of their requests. `agent` and `plugin` credentials are always refused.
  `grant` refuses (`prompt_not_shown`) any request whose prompt was not handed to that
  same credential through `pending` or `show`, so a request id and digest relayed by the
  opener are not enough to approve (PRODUCT.md 4.2: bound to what was shown).

The core names no consumer domain here. Consumers filter their own domain reads (O74).

Tests:

    PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/approval_scope/tests -t core -v
