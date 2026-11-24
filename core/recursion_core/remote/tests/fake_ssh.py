"""A stand-in for ssh in the remote tests. It connects to no host and reads no key, agent or known_hosts file.

The client runs it as ``<python> fake_ssh.py <ssh options> -- <host> <command>``. It appends its argv to
``$FAKE_SSH_LOG`` (one JSON line per start) and then, by ``$FAKE_SSH_MODE``:

  relay           be the far side: run the real relay in this process, as sshd runs the owner's forced
                  command (``SSH_ORIGINAL_COMMAND`` is the client's command, ``SSH_CONNECTION`` holds
                  documentation addresses, ``--key-label`` comes from ``$FAKE_SSH_KEY_LABEL`` and
                  ``--rights`` from ``$FAKE_SSH_RIGHTS``)
  refuse          write what ssh writes for a host key it does not accept, exit 255, read nothing
  banner          write a login banner on stdout (a shell that prints), then run the relay
  hello_then_die  answer the hello as the relay does, read one more frame, exit 255 with no answer
  wrong_id        answer the hello, then answer every frame with another id
  old_relay       answer the hello with relay version 0, then answer every frame ``ok`` (so that a client
                  that skipped the version check would fail its test at once instead of hanging)
  misused_key     run the relay as if the client had asked the key for another command
  stall           answer the hello, then stop reading stdin until killed (a far side that stalls)
  small_relay     run the real relay with a 256-byte frame limit
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # the repository's core/ folder

from recursion_core.remote import codec  # noqa: E402
from recursion_core.remote.relay import Relay  # noqa: E402
from recursion_core.remote.relay import main as relay_main  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    if "--" not in argv or len(argv) < argv.index("--") + 3:
        sys.stderr.write("fake ssh: expected <options> -- <host> <command>\n")
        return 255
    command = " ".join(argv[argv.index("--") + 2:])
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "pid": os.getpid()}) + "\n")
    mode = os.environ.get("FAKE_SSH_MODE", "relay")
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    environ = dict(os.environ)
    environ["SSH_ORIGINAL_COMMAND"] = command
    environ["SSH_CONNECTION"] = "198.51.100.7 50022 192.0.2.10 22"
    label = os.environ.get("FAKE_SSH_KEY_LABEL")
    relay_args = ["relay"] + (["--key-label", label] if label else [])
    if os.environ.get("FAKE_SSH_RIGHTS"):
        relay_args += ["--rights", os.environ["FAKE_SSH_RIGHTS"]]

    if mode == "refuse":
        sys.stderr.write("Host key verification failed.\r\n")
        sys.stderr.flush()
        return 255
    if mode == "banner":
        stdout.write(b"Welcome to the far host\n")
        stdout.flush()
        return relay_main(relay_args, environ=environ, stdin=stdin, stdout=stdout)
    if mode == "misused_key":
        environ["SSH_ORIGINAL_COMMAND"] = "scp -t /tmp"
        return relay_main(relay_args, environ=environ, stdin=stdin, stdout=stdout)
    if mode == "small_relay":
        return Relay(environ=environ, key_label=label, max_frame_bytes=256).serve(stdin, stdout)
    if mode in ("hello_then_die", "wrong_id", "old_relay", "stall"):
        relay = Relay(environ=environ, key_label=label)
        hello = codec.read_message(stdin)
        if hello is None:
            return 0
        reply = relay.hello(hello)
        if mode == "old_relay":
            reply["relay"]["version"] = 0
        codec.write_message(stdout, reply)
        if mode == "stall":
            # Read nothing more. The client's deadline kills this process; should the client never do so
            # (a broken client), leave when the parent does, so that no stray process outlives the test.
            parent = os.getppid()
            while os.getppid() == parent:
                threading.Event().wait(0.5)
            return 0
        while True:
            frame = codec.read_message(stdin)
            if frame is None:
                return 0
            if mode == "hello_then_die":
                return 255
            if mode == "wrong_id":
                codec.write_message(stdout, {"ok": True, "result": {"code": "ok"}, "id": f"not-{frame.get('id')}"})
            if mode == "old_relay":
                answer = {"ok": True, "result": {"code": "ok"}}
                if "id" in frame:
                    answer["id"] = frame["id"]
                codec.write_message(stdout, answer)
    return relay_main(relay_args, environ=environ, stdin=stdin, stdout=stdout)


if __name__ == "__main__":
    sys.exit(main())
