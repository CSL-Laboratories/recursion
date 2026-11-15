"""Remote control (lane REMOTE): the SSH stdio relay, its client, and pairing-state models.

What is here (first slice):

* ``codec``: the relay's wire. A frame is the control socket's own frame (4-byte length, one JSON
  object, at most 1 MiB), read from and written to byte streams (a pipe, stdin and stdout), plus the
  few frames the relay answers itself.
* ``relay``: the daemon machine's side, run by sshd as the owner's forced command. It reads frames on
  stdin, forwards each on a fresh connection to the daemon's owner-only Unix socket and writes the
  reply on stdout. It opens no socket but that one, and no listener at all.
* ``ssh_transport``: the other machine's side: ``RelayAPI.call(method, params)`` gives the same answers
  as the CLI's socket client (``control.client.SocketAPI``), over ``ssh <host> recursiond relay`` with
  the host key pinned to one known_hosts file.
* ``pairing`` and ``device_state``: in-memory models of a one-time pairing offer and of a paired
  device's lifecycle. They mint nothing and store nothing, and no daemon reads them.

What is not here: no network listener, no device store, no token, no phone page, nothing registered
with the daemon. A relay tested against today's socket proves the transport; it does not prove the
later scope and security claims (scopes are lane CONTROL's; the device store and the phone page come in
this lane's later slices). README.md has the authorized_keys line and the design notes.
"""
