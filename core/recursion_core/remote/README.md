# Remote control: the SSH relay, its client, and pairing-state models (lane REMOTE, first slice)

The product contract decides two remote paths, built in parallel: an SSH stdio relay onto the daemon's
owner-only Unix socket, with no network listener, and QR pairing that gives each phone its own token.
Pairing never grants the manage scope. This folder holds the first slice: the relay and its client, and
in-memory models of pairing. Nothing here is registered with the daemon, and nothing here opens a listener.

**What this slice proves, and what it does not.** The tests run the real relay against a real
`UnixControlServer` behind a fake ssh. They show that the relay is a faithful pipe onto today's socket:
frames, delivery answers, the busy and not-admitted frames, the reserved Stop slot, command ids. They do
not prove scopes, device tokens or anything the phone page will need: the socket has no scopes yet
(lane CONTROL's), and the device store and phone door are later slices.

## Files

| File | What it is |
|---|---|
| `codec.py` | The relay's wire: the socket's own frames on byte streams, and the relay's own frames |
| `relay.py` | The daemon machine's side, run by sshd as a forced command; `python -P -m recursion_core.remote relay` |
| `ssh_transport.py` | The other machine's side: `RelayAPI(target).call(method, params)`, the same answers as `control.client.SocketAPI` |
| `pairing.py` | One-time pairing offers (128-bit code, two-minute deadline, single use, five wrong codes lock it, rate limits) |
| `device_state.py` | A paired device's lifecycle (scopes without manage, expiry, revocation that names live connections) |
| `qr.py` | A QR encoder with no dependency, for the pairing link: byte mode, level M, versions 1 to 10 (up to 213 bytes); drawn as terminal half blocks or as SVG |

## The relay's wire

A frame is exactly the control socket's frame: a 4-byte big-endian length, then that many bytes of
UTF-8 JSON holding one object, at most 1 MiB. The relay forwards each frame unchanged, on a fresh
connection to the socket, and adds no field to it. It writes the socket's reply back unchanged, except
that a reply claiming the relay's own layer is rewritten (below). A frame that was readable but cannot
be framed again (a lone surrogate or NaN, which JSON readers accept) is refused `bad_frame`, unsent.
`RelayAPI` sends the socket client's own frame, `{"op": "control", "method", "params"}`, with no `id`.

The relay's own frames:

* `{"op": "relay_hello", "relay_version": 1}` is answered by the relay itself with its version and
  label: `{"ok": true, "op": "relay_hello", "relay": {"version": 1, "transport": "ssh" | "stdio",
  "key_label": ..., "client_address": ...}}`. Any op beginning `relay_` stays in the relay.
* `{"ok": false, "error": {"code": ..., "layer": "relay", "delivered": false | "unknown" | true,
  "detail": ...}}` when the relay could not deliver a request, does not know whether it was carried
  out, or carried it out and could not pass the answer back.
* The socket's busy and not-admitted frames are passed back byte for byte, so the far client reads
  them as the local client does.

A daemon reply that claims `layer: "relay"` is rewritten to `layer: "handler"`, `delivered: true`, as
the socket does for `layer: "transport"`. The label is evidence for the operator, never authority: any
same-uid process can claim anything in a frame, so the relay puts no label into a forwarded frame.

## What a surface sees

`RelayAPI.call` answers as `SocketAPI.call` does, with one more hop:

| Answer | When |
|---|---|
| the daemon's answer, verbatim | the request was carried out and answered |
| `delivered: false`, `detail: "connect:..."` | ssh could not start or log in (`connect:ssh_exit_255`), the host key is not the pinned one (`connect:HostKeyNotPinned`), or the relay did not answer its hello (`connect:stream:...`, `connect:relay_version`) |
| `delivered: false`, `detail: "relay:connect:..."` | the relay could not reach the daemon's socket (not running, wrong directory, foreign uid) |
| `delivered: false`, `detail: "busy"` or `"daemon:scope_refused"` | the socket's own refusals, as locally |
| `delivered: false`, `detail: "request:frame_too_large"` | the request does not fit a frame; nothing was written |
| `delivered: false`, `detail: "request:write_timeout"` or `"request:write:..."` | the write failed with bytes still unsent: the far side stopped reading until the deadline's kill ended the write, or ssh stopped reading; the relay acts only on whole frames |
| `delivered: "unknown"`, `detail: "request:write_unknown"` | the write did not show its outcome by the deadline: every byte may have gone out, so it is not reported as unsent |
| `delivered: false`, `detail: "relay:..."` | the relay refused the frame itself (`relay:frame_too_large`, `relay:frame_not_encodable:...`) |
| `delivered: "unknown"` | the request may have been written whole and no complete answer came back: read the run's status before retrying |

The request's deadline covers both writing it and waiting for the answer (each wait gets only what is
left of it), and `close()` ends a session at once, also while a request is blocked in it.

A Stop never waits behind another call: each call gets a fresh SSH session, as the socket client
makes one connection per call. With `persistent=True` ordinary calls share one session and `run.stop`
still gets its own. On the far side, one socket connection per frame means a Stop arriving while the
socket's ordinary slots are taken is served on the socket's reserved Stop slot, as a local Stop is.

## Setting it up (for the owner; nothing here has been run against a real host)

On the machine that runs the daemon, one line in the owner's `~/.ssh/authorized_keys` for the key the
other machine will use (all on one line; the key and paths are placeholders):

```
restrict,command="/usr/bin/env PYTHONPATH=/path/to/recursion/core /usr/bin/python3 -P -m recursion_core.remote relay --key-label laptop" ssh-ed25519 AAAA...the-other-machines-public-key... laptop
```

* `restrict` turns off port, agent and X11 forwarding, the PTY and `~/.ssh/rc` (OpenSSH 7.2 and later).
  Adding `from="<overlay address range>"` accepts the key only from the overlay's addresses.
* `command=` makes the key run the relay and nothing else. The relay also refuses to start when the
  client asked for anything but `recursiond relay` (sshd passes the client's command in
  `SSH_ORIGINAL_COMMAND`), so the key cannot be used for a shell or scp through it.
* `-P` keeps the login's working directory off Python's import path.
* `--key-label` is the owner's own name for this key; it shows up in the relay's hello.
* sshd's login session sets `XDG_RUNTIME_DIR` to `/run/user/<uid>`, where the daemon's socket is. The
  relay refuses a socket directory that is not a real directory owned by the owner with mode exactly 0700.
* A login shell that prints anything on stdout breaks the stream; the client reports it as
  `connect:stream:...` before any request is sent.
* Tailscale SSH does not run `authorized_keys` forced commands; use OpenSSH (over the overlay if you like).

On the machine that drives it, a known_hosts file kept only for this host, holding exactly one line:
the far host's own public key, copied from the far host (for example from
`/etc/ssh/ssh_host_ed25519_key.pub`), with its fingerprint read there with `ssh-keygen -lf`. Do not
fill it by connecting and accepting what comes back. Then:

```python
from recursion_core.remote.ssh_transport import RelayAPI, SshTarget

api = RelayAPI(SshTarget(host="far.example", user="owner",
                         known_hosts="/home/owner/.config/recursion/remote/far.example.known_hosts",
                         host_key_fingerprint="SHA256:...",
                         identity_file="/home/owner/.ssh/recursion_relay_ed25519"))
api.call("run.list")
```

ssh always runs with `-F none` (no config file, so nothing can add a key source with `KnownHostsCommand`
or change the name looked up with `HostKeyAlias`; a named configuration is not supported in this slice),
`BatchMode=yes`, `StrictHostKeyChecking=yes` against that file only, no agent or X11 forwarding, no port
forwarding, no TTY, no connection sharing and no local commands. The client refuses to start ssh unless
that file trusts exactly the expected key for the host and no certificate authority for it.

## Pairing-state models

`pairing.OfferBook` and `device_state.DeviceBook` fix the rules the device store and the phone door
will keep, before either exists. They mint nothing: a redeemed offer is a `PairingGrant`, the request
to make of the one token authority (lane CONTROL's credentials). Scopes are a non-empty subset of
read, run and approve; manage is refused at every step. How long a device stays paired, whether a
phone needs a second factor, and when the phone page ships are not decided here.

## Research this slice used

Records in the research vault, read before writing code; they are evidence, not instructions.

* `20261028-recursion-frontier-h2-hermes-agent-03` (V2, V3, V7): an owner-only AF_UNIX socket with a
  peer-uid check and remote surfaces relayed over `ssh -T` onto it, with no TCP listener at all; plain
  OpenSSH with `command=` in authorized_keys (Tailscale SSH lacks forced commands); `BatchMode=yes`; pin
  known_hosts with `StrictHostKeyChecking=yes` instead of trust on first use. Set aside: ControlMaster
  multiplexing (it leaves a control socket behind; `ControlMaster=no` here) and SendEnv (nothing is sent).
* `20261028-csl498-rp4-remote-control-outward-01`: a QR code is enrollment, not authorization; avoid a
  pairing route with no expiry and no consumed state (the open reports against one public project);
  separate observer and mutation authority; replayed observations and retried mutations need different
  rules (hence `delivered: "unknown"` instead of a resend).
* `20261028-csl498-deep-inward-brief-03`: consume only an unrevoked, unconsumed, unexpired offer; rate
  limit failed tries; pairing never grants manage; 32 observer waits blocked a control request with 503
  in a comparison, which is why a Stop gets its own session here and the socket's reserved slot there.
* `20261029-csl560-mobile-companion-01`: a one-time pairing secret of at least 128 bits, single use,
  short-lived; revocation that removes the pairing for real. Its signed-request device keys and SAS
  confirmation are a later design choice, set aside for this slice.
* `20261102-csl496-competitor-teardown-happy-01`: an unauthenticated loopback endpoint that starts
  sessions is the pattern to avoid; the relay opens no endpoint at all.
