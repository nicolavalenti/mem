#!/bin/bash
# SessionStart catch-up for mem. Invoked (non-blocking, via `nohup ... &`) by the
# SessionStart hook in ~/.claude/settings.json so a NEW Claude session pulls in
# any transcripts that finished since the last run — closing the back-to-back-
# sessions gap without a flaky session-END hook. Runs the same incremental ingest
# as the 06:30 cron (com.mem.ingest -> nightly_ingest.py).
#
# Internally synchronous but guarded:
#   - atomic mkdir lock so two sessions starting close together (or one near the
#     06:30 cron) never run two ingests into the same sqlite db at once
#   - a 30-min staleness reclaim so a hard-killed run can't wedge the lock forever
# Incremental embedding (local fastembed, no cloud) means unchanged transcripts
# are skipped, so the common "nothing new" case is a few seconds of background CPU.

LOCK="/tmp/mem-ingest.lock"
LOG="$HOME/.mem/ingest.log"
UV="$HOME/.local/bin/uv"
SCRIPT="$HOME/Projects/mem/nightly_ingest.py"

# reclaim a stale lock (older than 30 min = a previous run died before cleanup)
if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
  rmdir "$LOCK" 2>/dev/null
fi

# acquire — if another ingest holds the lock, do nothing
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "=== $(date '+%Y-%m-%d %H:%M:%S') session-start catch-up ===" >> "$LOG"
"$UV" run "$SCRIPT" >> "$LOG" 2>&1
