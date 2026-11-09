# approval_scope

Domain-neutral approval prompt text and approval scope for the Recursion core (CSL-722;
PRODUCT.md 4.2, I-42, O74).

- `ApprovalRequest.create(effect, display, requester, expires_at)` takes the SHA-256
  digest over the effect's canonical bytes (`csl-cj/v0`, same form as `plugin_security`).
  Callers cannot pass a digest in, and display fields never change it.
- `render_prompt(request)` writes a fixed header (request id, digest, requester, expiry),
  then the caller's labelled fields as untrusted text behind a `| ` gutter, with control,
  bidi and zero-width characters shown as `<U+XXXX>`. Nothing defaults to approve.
- `ApprovalAuthority` opens requests, grants an `ApprovalScope` to an `operator_ui` or
  `paired_device` credential holding the `approve` scope (`manage` not needed; pairing
  never grants it), and checks `approve`/`deny`. Scopes are non-delegable, bound to fixed
  `(request_id, digest)` pairs, each used once, and expire no later than their requests.
  `agent` and `plugin` credentials are always refused.

The core names no consumer domain here. Consumers filter their own domain reads (O74).

Tests:

    PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/approval_scope/tests -t core -v
