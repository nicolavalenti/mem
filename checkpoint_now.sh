#!/bin/bash
# Immediate checkpoint for ONE session, invoked by the SessionEnd and PreCompact
# hooks in ~/.claude/settings.json.
#
# The 5-minute launchd sweep (com.mem.idle) already catches everything eventually
# — mtime is the source of truth and it is correct even when a session is killed.
# These two hooks only remove the wait in the two moments where the wait is the
# whole problem:
#
#   SessionEnd  — you cleared the chat and want to start fresh RIGHT NOW. Waiting
#                 12 minutes for the sweep is exactly the friction that makes
#                 leaving 500k-token sessions open feel safer than clearing them.
#   PreCompact  — auto-compaction is about to discard detail (this machine fires
#                 it at 65%, per CLAUDE_AUTOCOMPACT_PCT_OVERRIDE). This is the
#                 last moment the full transcript still exists to summarise.
#
# IMPORTANT — why this script backgrounds itself instead of being invoked as
# `nohup checkpoint_now.sh &` the way the SessionStart hook invokes
# session_catchup.sh: a backgrounded command in a non-interactive shell has its
# stdin redirected from /dev/null, so the hook's JSON payload never arrives.
# Verified: `echo '{...}' | bash -c 'nohup cat >out &'` writes an empty file.
# session_catchup.sh gets away with it only because it reads no stdin. So the
# payload is read in the FOREGROUND (instant) and only the slow haiku call is
# detached, which keeps the hook non-blocking without starving it.
#
# Exits 0 unconditionally: a checkpoint is a nice-to-have and must never block or
# fail a session lifecycle event.
set -u

PAYLOAD=$(cat 2>/dev/null || true)
SESSION=$(printf '%s' "$PAYLOAD" | python3 -c \
  'import json,sys;print((json.load(sys.stdin).get("session_id") or "")[:36])' 2>/dev/null)

# No id, nothing to force — the sweep will get it on mtime like everything else.
case "$SESSION" in
  [0-9a-fA-F]*) ;;
  *) exit 0 ;;
esac

nohup /Users/nickvalenti/Projects/mem/bin/"Mem Checkpointer" --force "$SESSION" \
  >> "$HOME/.mem/idle.log" 2>&1 &
exit 0
