#!/usr/bin/env bash
# intake.sh — the inbound half of the dashboard: what Lucas SAYS becomes durable state.
#
# Until now Telegram was one-directional. notify.sh could page him; nothing could hear him back.
# So every instruction — "set up this project", "deprioritise that", "yes, do it" — had to be
# typed into a session like this one, which makes the human the transport and means Hermes can
# only ever choose among work someone else wrote down.
#
# ★ WHY IT DOES NOT POLL TELEGRAM. A hermes gateway is already running `gateway run` and
# long-polling getUpdates on this bot token. Telegram deletes an update once a consumer confirms
# it, so TWO pollers steal from each other: some messages would reach the gateway, some would
# reach us, nondeterministically, and the rest would vanish. That is unrecoverable and silent —
# the worst shape of bug this system has. Verified before building: our own getUpdates returned
# 0 updates with pending=0, because the gateway had already taken them.
#
# The gateway persists what it receives, so intake READS that store instead of competing for it:
#   $HERMES_HOME/state.db  messages(role,content,timestamp) JOIN sessions(source,chat_id,thread_id,user_id)
# Read-only, no second bot, no metered call, nothing taken away from the gateway.
#
# ★ AND THE TRAP INSIDE THAT TABLE: most role='user' rows are NOT from a human. The heartbeat,
# the daily brief and hermes-decide all write their prompts there with source='cli', and
# subagents with source='subagent'. Ingesting those would feed the system its own output back as
# instructions from Lucas — a loop that reads as him becoming very talkative. Only
# source='telegram' from an ALLOWED user id is intake.
#
# ★ INTAKE IS NOT A COMMAND CHANNEL. A message becomes a durable INSTRUCTION that Hermes reads
# in its brief and must act on or escalate; the actuator whitelist still governs what may run.
# There is no executable shortcut, including for assist answers. Every authenticated message is
# durable input; only a schema-checked Head Hermes decision and the sole actuator may change state.
#
#   intake.sh poll [--dry-run] [--backfill]   ingest new messages; advance the watermark
#                                (a first run SEEDS the watermark; --backfill takes history too)
#   intake.sh list [n]           the most recent instructions
#   intake.sh open               instructions not yet acknowledged
#   intake.sh consume <id> <action-id>...      couple consumption to durable actions
#   intake.sh acknowledge <id> <action-id>...  close only when those actions succeeded
#   intake.sh release <id> <action-id>...      reopen only after those actions failed
set -uo pipefail
HOME="${HOME:-/root}"   # systemd gives a service no HOME; `set -u` then kills the script
export LC_ALL=C
ROOT="${RECURSION_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" 2>/dev/null || { echo "intake: RECURSION_ROOT '$ROOT' is not a directory" >&2; exit 3; }
[ -f .env ] && { set -a; . ./.env; set +a; }
# .env pins RECURSION_ROOT for the deployed checkout. Sourcing it must never RETARGET a
# caller who passed a different root: that silently made a "sandbox" enqueue ten decisions
# into production on 2026-08-12. ROOT and RECURSION_ROOT agree from here on.
RECURSION_ROOT="$ROOT"; export RECURSION_ROOT

# The systemd gateway has a dedicated HOME and writes state.db at HERMES_HOME itself. Root cron
# intentionally has a different HOME and does not inherit the gateway service environment, so
# deriving this path from cron's HOME silently reads a stale, unrelated CLI store. Keep the
# explicit test/operator override, otherwise follow the checked-in gateway service contract.
HERMES_STATE_HOME="${HERMES_HOME:-/var/lib/hermes-gateway}"
DB="${RECURSION_HERMES_DB:-$HERMES_STATE_HOME/state.db}"
DIR="$ROOT/state/instructions"; mkdir -p "$DIR"
MARK="$ROOT/state/.intake-watermark"
REG="${RECURSION_PORTFOLIO:-$ROOT/state/portfolio.tsv}"
ALLOW="${RECURSION_INTAKE_ALLOW:-8913126684}"     # Lucas. A chat is not an authorisation.
CMD="${1:-poll}"; shift 2>/dev/null || true
now(){ date -u +%FT%TZ; }

_INTAKE_LOCK_HELD=0; _INTAKE_TMP=""
# The witnessed lock resolves the owner via its OS pid. Under Git Bash $$ is the MSYS pid,
# which Win32 OpenProcess cannot see; /proc/$$/winpid never exists on Linux, so the
# fallback is byte-identical on the box.
_INTAKE_OWNER_PID="$(cat /proc/$$/winpid 2>/dev/null || echo $$)"
intake_unlock(){
  [ "$_INTAKE_LOCK_HELD" = 1 ] || return 0
  python3 "$ROOT/scripts/runtime-state.py" lock-release \
    "$ROOT/state/.locks/taskctl.lock" "$_INTAKE_OWNER_PID" >/dev/null || return 74
  _INTAKE_LOCK_HELD=0
}
intake_cleanup(){
  [ -z "$_INTAKE_TMP" ] || rm -f -- "$_INTAKE_TMP" 2>/dev/null || true
  intake_unlock || true
}
intake_lock(){
  local lock="$ROOT/state/.locks/taskctl.lock"
  mkdir -p "$ROOT/state/.locks" || return 70
  [ -x "$ROOT/scripts/runtime-state.py" ] \
    || { echo "intake: witnessed lock helper is absent" >&2; return 73; }
  python3 "$ROOT/scripts/runtime-state.py" lock-acquire "$lock" "$_INTAKE_OWNER_PID" >/dev/null \
    || { echo "intake: state lock is contended; refusing mutation" >&2; return 73; }
  _INTAKE_LOCK_HELD=1
  trap 'intake_cleanup' EXIT
  return 0
}

atomic_instruction_transition(){ # file status action_csv note
  local file="$1" status="$2" actions="$3" note="$4" d b tmp rc
  [ ! -d "$ROOT/state/.locks/runtime-state.lock" ] \
    || { echo "intake: runtime-state maintenance in progress; refusing concurrent update" >&2; return 73; }
  d="$(dirname "$file")"; b="$(basename "$file")"
  tmp="$(mktemp "$d/.${b}.tmp.XXXXXX")" || return 70
  cp -p -- "$file" "$tmp" || { rm -f -- "$tmp"; return 70; }
  python3 - "$tmp" "$status" "$actions" "$note" "$(now)" <<'PY'
import os,sys
path,status,actions,note,stamp=sys.argv[1:6]
lines=open(path,encoding='utf-8',errors='surrogateescape').read().split('\n')
updates={'schema_version':'1','status':status,'action_ids':actions}
for key,value in updates.items():
    hit=False
    for i,line in enumerate(lines):
        if line.startswith(key+':'): lines[i]=key+': '+value; hit=True; break
    if not hit:
        for i,line in enumerate(lines):
            if line.strip()=='': lines.insert(i,key+': '+value); hit=True; break
    if not hit: lines.append(key+': '+value)
lines.extend(['','## '+status.upper()+' '+stamp,note])
with open(path,'w',encoding='utf-8',errors='surrogateescape',newline='\n') as out:
    out.write('\n'.join(lines)); out.flush(); os.fsync(out.fileno())
PY
  rc=$?
  if [ "$rc" -ne 0 ]; then rm -f -- "$tmp"; return "$rc"; fi
  mv -f -- "$tmp" "$file" || { rm -f -- "$tmp"; return 71; }
  python3 - "$d" <<'PY' >/dev/null 2>&1 || return 72
import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PY
}

coupled_actions(){ # instruction id required_status action ids...
  local iid="$1" required="$2" aid file owner status csv=""
  shift 2
  [ "$#" -gt 0 ] || { echo "intake: at least one resulting action id is required" >&2; return 2; }
  for aid in "$@"; do
    printf '%s' "$aid" | grep -qE '^action-[0-9a-f]{24}$' \
      || { echo "intake: invalid action id: $aid" >&2; return 2; }
    file="$ROOT/state/actions/$aid.json"; [ -f "$file" ] \
      || { echo "intake: no durable action $aid" >&2; return 2; }
    read -r owner status < <(python3 - "$file" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
print(d.get('instruction_id') or '-',d.get('status') or '-')
PY
)
    [ "$owner" = "$iid" ] \
      || { echo "intake: action $aid is not coupled to instruction $iid" >&2; return 2; }
    if [ "$required" = succeeded ] && [ "$status" != succeeded ]; then
      echo "intake: action $aid is $status, not succeeded" >&2; return 2
    fi
    if [ "$required" = failed ] && [ "$status" != failed ] && [ "$status" != cancelled ]; then
      echo "intake: action $aid is $status, not failed/cancelled" >&2; return 2
    fi
    if [ "$required" = releasable ] && [ "$status" != pending ] \
       && [ "$status" != failed ] && [ "$status" != cancelled ]; then
      echo "intake: action $aid is $status, not pending/failed/cancelled" >&2; return 2
    fi
    csv="${csv:+$csv,}$aid"
  done
  printf '%s' "$csv"
}

case "$CMD" in

poll)
  DRY=0; BACKFILL=0
  for a in "$@"; do case "$a" in --dry-run) DRY=1 ;; --backfill) BACKFILL=1 ;; esac; done
  [ -f "$DB" ] || { echo "intake: no gateway store at $DB — nothing to read"; exit 0; }
  intake_lock || exit $?
  if [ -e "$MARK" ]; then
    INITIALIZED=1
    [ -f "$MARK" ] || { echo "intake: watermark is not a regular file; refusing intake" >&2; exit 74; }
    LAST="$(cat "$MARK" 2>/dev/null)" || { echo "intake: unreadable watermark; refusing intake" >&2; exit 74; }
    case "$LAST" in
      ''|*[!0-9]*)
        mark_sha="$(sha256sum "$MARK" 2>/dev/null | awk '{print $1}')"
        intake_unlock || exit $?
        RECURSION_ROOT="$ROOT" bash "$ROOT/scripts/taskctl.sh" esc-record \
          "intake-watermark-corrupt-${mark_sha:-unknown}" \
          "Intake watermark is corrupt; authenticated instructions are held" \
          infra "state/.intake-watermark sha256=${mark_sha:-unknown}" >/dev/null 2>&1 || true
        echo "intake: existing watermark is empty/corrupt; refusing first-run reseed" >&2
        exit 74
        ;;
    esac
  else
    INITIALIZED=0
    LAST=0
  fi

  # The watermark advances only after every instruction and the source-store anchor are durable.
  # A crash re-reads the batch; an existing same-id instruction is verified and preserved, never
  # overwritten, so lifecycle state written between the crash and retry cannot be reopened.
  TMPOUT="$(mktemp)"; _INTAKE_TMP="$TMPOUT"
  python3 - "$DB" "$LAST" "$ALLOW" "$DIR" "$REG" "$DRY" "$(now)" "$BACKFILL" "$INITIALIZED" \
    "$ROOT/scripts" >"$TMPOUT" <<'PY'
import hashlib, io, json, math, os, re, sqlite3, sys, shutil, tempfile

db, last, allow, outdir, reg, dry, stamp, backfill, initialized, scripts = sys.argv[1:11]
last = int(last); dry = dry == "1"; backfill = backfill == "1"; initialized = initialized == "1"
allowed = set(x for x in allow.replace(",", " ").split() if x)
sys.path.insert(0, scripts)
from instruction_routing import route_for

def connect(path):
    # The gateway holds this in WAL mode. Read-only is enough and never blocks it; if the
    # reader cannot attach (a -shm it may not create), fall back to a snapshot copy rather
    # than opening read-write and risking the gateway's own state.
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        c.execute("select count(*) from sqlite_master").fetchone()
        return c, None
    except Exception:
        tmp = tempfile.mkdtemp(prefix="intake-")
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(path + suf):
                shutil.copy2(path + suf, os.path.join(tmp, os.path.basename(path) + suf))
        return sqlite3.connect(os.path.join(tmp, os.path.basename(path)), timeout=5), tmp

conn, tmp = connect(db)

def stop(message):
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    raise SystemExit(message)

message_columns = {row[1] for row in conn.execute("pragma table_info(messages)")}
platform_expr = "m.platform_message_id" if "platform_message_id" in message_columns else "NULL"

def source_row(mid):
    sql = """
        select m.id, m.content, m.timestamp, s.chat_id, s.thread_id, s.user_id,
               s.source, %s
          from messages m join sessions s on s.id = m.session_id
         where m.id = ? and m.role = 'user' and s.source = 'telegram'
    """ % platform_expr
    return conn.execute(sql, (mid,)).fetchone()

def display_body(content):
    text = (content or "").strip()
    return re.sub(r"^\[[A-Za-z][A-Za-z .'-]{0,40}\]\s*", "", text)

def source_sha256(row):
    mid, content, timestamp, chat, thread, uid, source, platform_message_id = row
    identity = {
        "chat_id": str(chat or ""), "content": content or "", "id": int(mid),
        "platform_message_id": str(platform_message_id or ""), "source": str(source or ""),
        "thread_id": str(thread or ""), "timestamp": timestamp, "user_id": str(uid or ""),
    }
    raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def anchor_bytes(row):
    mid, content, timestamp, chat, thread, uid, source, platform_message_id = row
    anchor = {
        "chat_id": str(chat or ""), "id": int(mid), "schema_version": 1,
        "source_sha256": source_sha256(row), "thread_id": str(thread or ""),
        "user_id": str(uid or ""),
    }
    return (json.dumps(anchor, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

def anchor_path(mid):
    return os.path.join(outdir, ".intake-anchor-%08d.json" % int(mid))

def persist_anchor(row):
    path = anchor_path(row[0]); payload = anchor_bytes(row)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isfile(path):
            stop("intake: source anchor %d is not a regular file" % row[0])
        if open(path, "rb").read() != payload:
            stop("intake: conflicting source anchor %d" % row[0])
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".intake-anchor.tmp.", dir=outdir)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        dfd = os.open(outdir, os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise

def durable_instruction_source(path):
    raw = io.open(path, encoding="utf-8", errors="surrogateescape").read()
    header, marker, section = raw.partition("\n## INSTRUCTION\n")
    if not marker:
        stop("intake: existing instruction lacks its source section: %s" % path)
    fields = {}
    for line in header.splitlines():
        if ":" in line:
            key, value = line.split(":", 1); fields[key.strip()] = value.strip()
    transition = re.search(
        r"\n## (?:OPEN|CONSUMED|ACKNOWLEDGED) \d{4}-\d{2}-\d{2}T[^\n]*Z\n", section)
    body = section[:transition.start() if transition else len(section)].rstrip("\n")
    return fields, body

def verify_instruction_source(path, mid, uid, chat, thread, body):
    if os.path.islink(path) or not os.path.isfile(path):
        stop("intake: existing instruction %d is not a regular file" % mid)
    fields, durable_body = durable_instruction_source(path)
    correlation = "telegram:%s:%s:%s" % (chat, thread or "root", mid)
    expected = {
        "id": str(mid), "principal": str(uid or ""), "from": str(uid or ""),
        "chat": str(chat or ""), "thread": str(thread or "-"),
        "reply_correlation": correlation, "deduplication_key": correlation,
    }
    if any(fields.get(key) != value for key, value in expected.items()) or durable_body != body:
        stop("intake: conflicting pre-existing instruction %d" % mid)

def validate_store_anchor(mid):
    row = source_row(mid)
    if row is None:
        stop("intake: gateway store does not contain Telegram watermark row %d" % mid)
    witnessed = anchor_path(mid)
    if os.path.lexists(witnessed):
        if os.path.islink(witnessed) or not os.path.isfile(witnessed):
            stop("intake: source anchor %d is not a regular file" % mid)
        if open(witnessed, "rb").read() != anchor_bytes(row):
            stop("intake: gateway store watermark source does not match anchor %d" % mid)
        return row
    instruction = os.path.join(outdir, "%08d.md" % mid)
    if os.path.lexists(instruction):
        verify_instruction_source(instruction, mid, row[5], row[3], row[4], display_body(row[1]))
    return row

watermark_row = validate_store_anchor(last) if last else None

# Thread id -> durable project pilot. The Telegram session history is a cache; these files are
# the authority for which logical pilot, memory namespace, and project own a topic.
topics = {}
project_ids = []
try:
    for line in io.open(reg, encoding="utf-8", newline="").read().split("\n"):
        c = line.split("\t")
        if len(c) >= 8 and not c[0].startswith("#") and c[6] not in ("-", ""):
            project_ids.append(c[0])
            binding = {"project": c[0], "pilot_id": "", "memory_key": ""}
            try:
                state = os.path.dirname(outdir)
                cfg = json.load(io.open(os.path.join(state, "project-config", c[0]+".json"), encoding="utf-8"))
                pilot = json.load(io.open(os.path.join(state, "pilots", cfg["pilot_id"]+".json"), encoding="utf-8"))
                if (c[7] == "active" and cfg.get("status") == "active" and pilot.get("status") == "active"
                        and str(cfg.get("topic")) == c[6] and str(pilot.get("topic")) == c[6]):
                    binding.update(pilot_id=pilot["id"], memory_key=pilot["memory_key"])
            except Exception:
                pass
            topics[c[6]] = binding
except Exception:
    pass

def telegram_identities(content, timestamp, chat, thread, uid, platform_message_id):
    # New gateway schemas expose the transport id directly. Legacy rows do not, but genuine
    # Telegram update timestamps are integral Unix seconds. Compaction copies preserve that
    # timestamp. Emit both aliases when possible so a NULL/non-NULL schema transition cannot
    # duplicate one update. Internal rehydration with only a fractional process clock has no key.
    platform_key = None
    if platform_message_id not in (None, ""):
        platform_key = (str(chat or ""), str(thread or ""), str(uid or ""),
                        str(platform_message_id))
    legacy_key = None
    try:
        stamp = float(timestamp)
    except (TypeError, ValueError):
        return platform_key, legacy_key
    if math.isfinite(stamp) and stamp.is_integer():
        legacy_key = (str(chat or ""), str(thread or ""), str(uid or ""),
                      int(stamp), content or "")
    return platform_key, legacy_key

seen_platform = set()
seen_legacy_without_platform = set()
seen_legacy_with_platform = set()

def remember_identity(platform_key, legacy_key):
    if platform_key is not None:
        seen_platform.add(platform_key)
        if legacy_key is not None:
            seen_legacy_with_platform.add(legacy_key)
    elif legacy_key is not None:
        seen_legacy_without_platform.add(legacy_key)

def identity_seen(platform_key, legacy_key):
    if platform_key is not None:
        # Distinct authoritative platform ids stay distinct. The legacy alias is consulted only
        # against rows that lacked a platform id, which preserves mixed-schema replay matching.
        return platform_key in seen_platform \
            or (legacy_key is not None and legacy_key in seen_legacy_without_platform)
    if legacy_key is None:
        return True
    return legacy_key in seen_legacy_without_platform or legacy_key in seen_legacy_with_platform

prior_sql = """
    select m.content, m.timestamp, s.chat_id, s.thread_id, s.user_id, %s
      from messages m join sessions s on s.id = m.session_id
     where m.role = 'user' and s.source = 'telegram' and m.id <= ?
""" % platform_expr
for content, timestamp, chat, thread, uid, platform_message_id in conn.execute(prior_sql, (last,)):
    remember_identity(*telegram_identities(content, timestamp, chat, thread, uid,
                                           platform_message_id))

rows_sql = """
    select m.id, m.content, m.timestamp, s.chat_id, s.thread_id, s.user_id, s.source, %s
      from messages m join sessions s on s.id = m.session_id
     where m.role = 'user' and s.source = 'telegram' and m.id > ?
     order by m.id asc
""" % platform_expr
rows = list(conn.execute(rows_sql, (last,)))

# ★ FIRST RUN SEEDS, IT DOES NOT BACKFILL. The gateway store predates intake by days, and
# ingesting that history would place stale DMs in front of Hermes as instructions to act on now —
# old context wearing a current timestamp. Seed the watermark to the present and say what was
# skipped; `--backfill` is there when the history is actually wanted.
if not initialized and not backfill:
    high = max([r[0] for r in rows], default=0)
    sys.stderr.write("intake: FIRST RUN — seeding the watermark at %d, skipping %d historical "
                     "message(s). Use --backfill to ingest them.\n" % (high, len(rows)))
    if not dry and rows:
        persist_anchor(rows[-1])
    print("__WATERMARK__%d" % high)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    raise SystemExit(0)

written, skipped, high = 0, 0, last
high_row = rows[-1] if rows else watermark_row
for mid, content, ts, chat, thread, uid, source, platform_message_id in rows:
    high = max(high, mid)
    if str(uid) not in allowed:
        skipped += 1
        continue
    text = (content or "").strip()
    if not text:
        skipped += 1
        continue
    # ★ ORDER MATTERS HERE. The gateway's own async-delivery notices arrive as user messages
    # shaped "[ASYNC DELEGATION BATCH COMPLETE - deleg_x]\n...", and the display-name strip below
    # happily ate that bracket, after which the marker check found nothing. Message 275 sailed
    # straight through the dry run. So: system markers are tested on the RAW text, first.
    if re.match(r"^\[(ASYNC|SYSTEM|AUTO)\b", text):
        skipped += 1
        continue
    platform_key, legacy_key = telegram_identities(content, ts, chat, thread, uid,
                                                    platform_message_id)
    if identity_seen(platform_key, legacy_key):
        skipped += 1
        continue
    remember_identity(platform_key, legacy_key)
    # Only strip a leading bracket that actually looks like a sender name: a few words, no
    # SHOUTING marker. A greedy strip is what caused the bug above.
    body = re.sub(r"^\[[A-Za-z][A-Za-z .'-]{0,40}\]\s*", "", text)
    binding = topics.get(str(thread or ""), {})
    project = binding.get("project", "")
    pilot_id = binding.get("pilot_id", "")
    memory_key = binding.get("memory_key", "")
    routing_target = route_for(body, binding, project_ids)
    path = os.path.join(outdir, "%08d.md" % mid)
    if os.path.lexists(path):
        verify_instruction_source(path, mid, uid, chat, thread, body)
        print(("WOULD preserve" if dry else "PRESERVED") +
              " %s project=%s %r" % (mid, project or "-", body[:70]))
        written += 1
        continue
    if dry:
        print("WOULD ingest %s thread=%s project=%s %r" % (mid, thread, project or "-", body[:70]))
        written += 1
        continue
    tmppath = path + ".tmp.%d" % os.getpid()
    with io.open(tmppath, "w", encoding="utf-8", newline="\n") as output:
        correlation = "telegram:%s:%s:%s" % (chat, thread or "root", mid)
        output.write(
            "schema_version: 1\nid: %d\nreceived: %s\nprincipal: %s\ntopic: %s\n"
            "project_id: %s\npilot_id: %s\npilot_memory_key: %s\nrouting_target: %s\n"
            "reply_correlation: %s\ndeduplication_key: %s\n"
            "from: %s\nchat: %s\nthread: %s\nproject: %s\nstatus: open\n"
            "action_ids: \n\n## INSTRUCTION\n%s\n" %
            (mid, stamp, uid, thread or "-", project or "", pilot_id, memory_key, routing_target,
             correlation, correlation,
             uid, chat, thread or "-", project or "-", body))
        output.flush(); os.fsync(output.fileno())
    os.replace(tmppath, path)
    dfd = os.open(outdir, os.O_RDONLY)
    try: os.fsync(dfd)
    finally: os.close(dfd)
    print("INGESTED %d project=%s %r" % (mid, project or "-", body[:70]))
    written += 1
    if os.environ.get("RECURSION_INTAKE_FAILPOINT") == "after-instruction-fsync":
        stop("intake: failpoint after-instruction-fsync")

if not dry and high_row is not None:
    persist_anchor(high_row)
if tmp:
    shutil.rmtree(tmp, ignore_errors=True)
sys.stderr.write("intake: %d ingested, %d skipped, watermark %d -> %d\n" % (written, skipped, last, high))
print("__WATERMARK__%d" % high)
PY
  rc=$?
  [ "$rc" -eq 0 ] || { echo "intake: reader failed (rc=$rc)" >&2; exit "$rc"; }

  # The reader's last line carries the high-water mark; everything else is the report.
  WM="$(grep '^__WATERMARK__' "$TMPOUT" | tail -1 | sed 's/^__WATERMARK__//')"
  grep -v '^__WATERMARK__' "$TMPOUT" || true
  if [ "$DRY" = "0" ] && [ -n "$WM" ]; then
    mark_tmp="$(mktemp "$(dirname "$MARK")/.$(basename "$MARK").tmp.XXXXXX")" || exit 70
    printf '%s\n' "$WM" > "$mark_tmp" || { rm -f -- "$mark_tmp"; exit 71; }
    python3 - "$mark_tmp" <<'PY' >/dev/null 2>&1 || { rm -f -- "$mark_tmp"; exit 72; }
import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PY
    mv -f -- "$mark_tmp" "$MARK" || { rm -f -- "$mark_tmp"; exit 71; }
    python3 - "$(dirname "$MARK")" <<'PY' >/dev/null 2>&1 || exit 72
import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PY
  fi

  # Message text never executes here. An assist answer is another durable instruction; the
  # planner may emit assist-answer and the actuator will couple it to the resulting action.
  # A quiet poll is a SUCCESS. Without this the trailing grep-for-ingested-lines finds nothing,
  # its exit status becomes the script's, and cycle.sh logs "intake failed" on every normal
  # cycle — an error message that means "nothing happened", which is how real errors get ignored.
  rm -f -- "$TMPOUT"; _INTAKE_TMP=""
  intake_unlock || exit $?
  trap - EXIT
  true
  ;;

list) n="${1:-10}"; ls -1 "$DIR"/*.md 2>/dev/null | tail -"$n" | while read -r f; do
        project="$(sed -n 's/^project_id: *//p' "$f" | head -1)"; [ -n "$project" ] || project="$(sed -n 's/^project: *//p' "$f" | head -1)"
        printf '%-10s %-26s %s\n' "$(sed -n 's/^id: *//p' "$f")" "$project" \
          "$(awk '/^## INSTRUCTION/{f=1;next} f' "$f" | tr '\n' ' ' | cut -c1-80)"; done
      [ -n "$(ls -A "$DIR" 2>/dev/null)" ] || echo "(no instructions yet)" ;;

open) found=0; TARGET="${1:-}"
      for f in "$DIR"/*.md; do [ -f "$f" ] || continue
        [ "$(sed -n 's/^status: *//p' "$f" | head -1)" = "open" ] || continue
        if [ -n "$TARGET" ]; then
          route="$(sed -n 's/^routing_target: *//p' "$f" | head -1)"
          [ "${route:-head-hermes}" = "$TARGET" ] || continue
        fi
        found=1
        project="$(sed -n 's/^project_id: *//p' "$f" | head -1)"; [ -n "$project" ] || project="$(sed -n 's/^project: *//p' "$f" | head -1)"
        printf '%-10s %-26s %s\n' "$(sed -n 's/^id: *//p' "$f")" "$project" \
          "$(awk '/^## INSTRUCTION/{f=1;next} f' "$f" | tr '\n' ' ' | cut -c1-80)"; done
      [ "$found" = "1" ] || echo "(no open instructions)" ;;

handled) echo "intake: handled is forbidden; acknowledge a coupled succeeded action" >&2; exit 2 ;;

consume) intake_lock || exit $?
      ID="${1:?usage: intake.sh consume <id> <action-id>...}"; shift
      F="$DIR/$(printf '%08d' "$ID").md"
      [ -f "$F" ] || { echo "intake: no instruction $ID" >&2; exit 2; }
      ACTIONS="$(coupled_actions "$ID" any "$@")" || exit $?
      atomic_instruction_transition "$F" consumed "$ACTIONS" "resulting actions: $ACTIONS" || exit $?
      echo "consumed $ID -> $ACTIONS" ;;

acknowledge) intake_lock || exit $?
      ID="${1:?usage: intake.sh acknowledge <id> <action-id>...}"; shift
      F="$DIR/$(printf '%08d' "$ID").md"
      [ -f "$F" ] || { echo "intake: no instruction $ID" >&2; exit 2; }
      ACTIONS="$(coupled_actions "$ID" succeeded "$@")" || exit $?
      CURRENT="$(sed -n 's/^status: *//p' "$F" | head -1)"
      if [ "$CURRENT" = acknowledged ]; then
        [ "$(sed -n 's/^action_ids: *//p' "$F" | head -1)" = "$ACTIONS" ] \
          || { echo "intake: acknowledged instruction $ID has different action evidence" >&2; exit 2; }
        echo "acknowledged $ID -> $ACTIONS (already)"; exit 0
      fi
      [ "$CURRENT" = consumed ] \
        || { echo "intake: instruction $ID is not consumed" >&2; exit 2; }
      atomic_instruction_transition "$F" acknowledged "$ACTIONS" "succeeded actions: $ACTIONS" || exit $?
      echo "acknowledged $ID -> $ACTIONS" ;;

release) intake_lock || exit $?
      ID="${1:?usage: intake.sh release <id> <action-id>...}"; shift
      F="$DIR/$(printf '%08d' "$ID").md"
      [ -f "$F" ] || { echo "intake: no instruction $ID" >&2; exit 2; }
      ACTIONS="$(coupled_actions "$ID" releasable "$@")" || exit $?
      CURRENT="$(sed -n 's/^status: *//p' "$F" | head -1)"
      if [ "$CURRENT" = open ]; then
        EXISTING="$(sed -n 's/^action_ids: *//p' "$F" | head -1)"
        # An open instruction with NO action evidence was never consumed: the action failed
        # before its consume handshake, so there is nothing to reopen. Record the evidence and
        # succeed -- refusing here strands the action, and the caller retries it forever
        # (52 iterations at ~6s in production before the actuator was stopped). Nobody has
        # claimed this instruction, so adopting the first claimant overwrites no evidence, and
        # a release by a DIFFERENT action still mismatches below and still fails closed.
        if [ -z "$EXISTING" ]; then
          atomic_instruction_transition "$F" open "$ACTIONS" \
            "action failed before consume; instruction never left open: $ACTIONS" || exit $?
          echo "released $ID -> $ACTIONS (never consumed)"; exit 0
        fi
        [ "$EXISTING" = "$ACTIONS" ] \
          || { echo "intake: reopened instruction $ID has different action evidence" >&2; exit 2; }
        echo "released $ID -> $ACTIONS (already)"; exit 0
      fi
      [ "$CURRENT" = consumed ] \
        || { echo "intake: instruction $ID is not consumed" >&2; exit 2; }
      # Surface WHY the actions failed: the reopen annotation is the only channel through
      # which the next decide turn can learn the refusal reason, and without it the model
      # re-emits the same refused decision forever (observed on instruction 573, 2026-08-12).
      EVID=""
      for _a in $ACTIONS; do
        _e="$(python3 -c "import json,sys
try:
    v=json.load(open('state/actions/%s.json'%sys.argv[1],encoding='utf-8'))
    print((v.get('result_evidence') or '').replace(chr(10),' ')[:240])
except Exception: pass" "$_a" 2>/dev/null)"
        [ -n "$_e" ] && EVID="$_e"
      done
      atomic_instruction_transition "$F" open "$ACTIONS" "deferred/failed actions reopened instruction: $ACTIONS${EVID:+ | last failure: $EVID}" || exit $?
      echo "released $ID -> $ACTIONS" ;;

*) sed -n '30,34p' "$0"; exit 2 ;;
esac
