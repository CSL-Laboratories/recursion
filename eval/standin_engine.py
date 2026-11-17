#!/usr/bin/env python3
"""STAND-IN ENGINE for lane EVAL's runner. Not Claude Code: no model, no network, no credential.

It speaks the stream-json lines the Claude Code adapter reads (``system``, ``assistant``, ``result``;
the same shape as core/recursion_core/daemon/evidence_standin_engine.py, every line marked
``"stand_in_engine": true``) and it EDITS THE WORK TREE as a scripted plan says, so that the runner,
the grader and the calculator can be driven end to end offline. It is an oracle: the plan carries the
task's reference solution. Nothing it produces is a measurement of any engine.

Input: one stream-json user message on stdin (what the adapter sends), then EOF.
Plan: the JSON file named by EVAL_STANDIN_PLAN (written by the runner, one task per file); the trial
number is EVAL_STANDIN_TRIAL (1-based). The plan's ``outcomes`` list picks the behaviour of each trial
(cycling when shorter than the trial number):

  solve    write the reference solution, finish with a success result
  wrong    write the plan's ``wrong`` files, finish with a success result (a false "done")
  idle     change nothing, finish with a success result (a false "done")
  give_up  change nothing, finish with an error result (no claim of being done)
  crash    exit 3 with no result line
  limit    a typed rate-limit error result (as a vendor limit reads)
  auth     a typed sign-in failure result
  hang     start a helper in the same session and sleep until killed

``usage`` in the plan, when present, is put on the result line as Claude Code puts its own (with
``total_cost_usd`` beside it); without it the result carries no usage, so every token count is
unknown (null), never 0.
"""
import base64
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time

VERSION = "eval-standin/1"
LABEL = "stand-in engine (lane EVAL): not Claude Code; no model, no network, no credential"
BEHAVIOURS = ("solve", "wrong", "idle", "give_up", "crash", "limit", "auth", "hang")


def emit(**fields):
    sys.stdout.write(json.dumps({"stand_in_engine": True, **fields}) + "\n")
    sys.stdout.flush()


def _remove(name):
    if os.path.isdir(name) and not os.path.islink(name):
        shutil.rmtree(name)
    else:
        os.remove(name)


def _plain_above(name):
    """True when every folder above ``name`` is a real folder (no file, no link)."""
    parts = name.split("/")
    for i in range(1, len(parts)):
        above = "/".join(parts[:i])
        if os.path.islink(above) or not os.path.isdir(above):
            return False
    return True


def _material(content):
    """Bytes to write, and a mode when the plan names one.

    A plain string is the ordinary plan entry. A base64 object carries bytes that are not text, or a
    mode the string form cannot name.
    """
    if isinstance(content, str):
        return content.encode("utf-8"), None
    if isinstance(content, dict) and content.get("encoding") == "base64":
        raw = base64.b64decode(content.get("data") or "")
        mode = {"100644": 0o644, "100755": 0o755}.get(content.get("mode"))
        return raw, mode
    return None, None


def write_files(files):
    """Deletions first, then the files written, so a file replaced by a folder (or the reverse) is laid out
    whatever the order of the plan; what stands in a written path's way is removed, a link never followed."""
    good = {}
    for name, content in (files or {}).items():
        if name.startswith("/") or ".." in name.split("/") or posixpath.normpath(name) != name:
            emit(type="system", subtype="stand_in_bad_path", path=name)
            continue
        good[name] = content
    for name, content in good.items():
        if content is None and _plain_above(name) and os.path.lexists(name):
            _remove(name)
    for name, content in good.items():
        if content is None:
            continue
        parts = name.split("/")
        for i in range(1, len(parts)):
            above = "/".join(parts[:i])
            if os.path.islink(above) or (os.path.lexists(above) and not os.path.isdir(above)):
                _remove(above)
        if os.path.islink(name) or os.path.isdir(name):
            _remove(name)
        raw, mode = _material(content)
        if raw is None:
            emit(type="system", subtype="stand_in_bad_file", path=name)
            continue
        folder = os.path.dirname(name)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(name, "wb") as handle:
            handle.write(raw)
        if mode is not None:
            os.chmod(name, mode)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(VERSION)
        return 0
    emit(type="system", subtype="init", note=LABEL, version=VERSION, pid=os.getpid(), cwd=os.getcwd(),
         tmpdir=os.environ.get("TMPDIR"), xdg_runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
         dbus_session_bus_address_set="DBUS_SESSION_BUS_ADDRESS" in os.environ,
         effort=os.environ.get("EVAL_STANDIN_EFFORT"))
    prompt = None
    for line in sys.stdin:
        try:
            prompt = json.loads(line)["message"]["content"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            emit(type="system", subtype="stand_in_bad_input")
    if prompt is None:
        emit(type="result", subtype="error_during_execution", is_error=True, result="stand-in engine got no prompt")
        return 2
    plan = {}
    plan_path = os.environ.get("EVAL_STANDIN_PLAN")
    if plan_path:
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
    trial = int(os.environ.get("EVAL_STANDIN_TRIAL") or "1")
    outcomes = plan.get("outcomes") or ["idle"]
    behaviour = outcomes[(trial - 1) % len(outcomes)]
    if behaviour not in BEHAVIOURS:
        emit(type="system", subtype="stand_in_unknown_behaviour", behaviour=behaviour)
        behaviour = "give_up"
    emit(type="assistant", message={"content": [{"type": "text",
                                                 "text": f"stand-in engine: trial {trial}, behaviour {behaviour}"}]})
    if behaviour == "crash":
        return 3
    if behaviour == "hang":
        helper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(3600)"],
                                  stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        emit(type="system", subtype="stand_in_helper", helper_pid=helper.pid,
             note="helper in the engine's own session; it holds stdout until it is killed")
        while True:
            time.sleep(3600)
    if behaviour == "limit":
        emit(type="result", subtype="error_during_execution", is_error=True, error="rate_limit",
             result="stand-in engine: simulated typed limit, no vendor was contacted")
        return 1
    if behaviour == "auth":
        emit(type="result", subtype="error_during_execution", is_error=True, error="authentication_failed",
             result="stand-in engine: simulated sign-in failure, no vendor was contacted")
        return 1
    if behaviour == "give_up":
        emit(type="result", subtype="error_during_execution", is_error=True,
             result="stand-in engine: gave up without changing anything")
        return 1
    if behaviour == "solve":
        write_files(plan.get("solution"))
    elif behaviour == "wrong":
        write_files(plan.get("wrong"))
    result = {"type": "result", "subtype": "success", "is_error": False,
              "result": f"stand-in engine: done ({behaviour})", "num_turns": 1}
    usage = plan.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {k: v for k, v in usage.items() if k != "total_cost_usd"}
        if "total_cost_usd" in usage:
            result["total_cost_usd"] = usage["total_cost_usd"]
    emit(**result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
