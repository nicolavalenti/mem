# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Idle session checkpointer — turn a session you walked away from into a
handoff note you can start a FRESH session from.

Run by launchd (com.mem.idle) every 5 minutes. For every transcript whose mtime
says it has been idle for >= IDLE_MIN minutes and that has not been checkpointed
at that mtime yet, it does two things:

  1. refreshes ~/.mem/sessions/<id>.md (raw turns, kind=session) so semantic
     recall is minutes-fresh instead of next-morning-fresh
  2. writes ~/.mem/handoffs/<id>.md (kind=handoff) — a structured note of what
     was being done, what was decided, and what is still open

Why this exists: resuming a long session after the prompt cache has expired
re-sends the whole conversation at cache-WRITE rates. Measured over 7 days on
this machine: 80 resumes after a >1h gap, 8.77M cache-write tokens paid on those
first turns alone, single resumes as large as 649k tokens. A handoff note is
~1-2k tokens. Starting fresh from one is roughly 30x cheaper than resuming, and
it only feels safe if the note is good — hence a real summariser, not a regex.

Design rules borrowed from the numen/mem codebase, each one load-bearing:
  - mtime is the idle signal, NOT a SessionEnd hook. mtime is still correct when
    a session is killed, the terminal crashes, or the laptop lid closes — which
    is exactly when a hook fires least reliably.
  - a checkpoint is BOUND to the artifact_mtime it summarised. The session gets
    re-checkpointed the moment it grows again, and never re-summarised while it
    has not changed. Same binding the numen verify loop uses for verdicts.
  - the model NEVER writes the file or stamps the clock. It returns JSON; Python
    writes the markdown and stamps the time.
  - never head-slice a `claude -p --output-format json` payload; parse it and
    take `result`. Field order is not API.
  - extractors are IMPORTED from nightly_ingest, never forked. One
    implementation of "what is a turn in this harness".

  uv run idle_checkpoint.py            # the every-5-minutes sweep
  uv run idle_checkpoint.py --dry-run  # list what it WOULD checkpoint, spend $0
  uv run idle_checkpoint.py --force <substring>   # re-checkpoint one session now
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly_ingest import (  # noqa: E402  — same-dir sibling, stdlib-only
    HARNESSES, MAX_TURN_CHARS, MIN_TURNS, SESSIONS_DIR, _json, _lines, run_mem,
)

HOME = Path.home()
CLAUDE = str(HOME / ".local" / "bin" / "claude")   # absolute: launchd starts bare
HANDOFFS = HOME / ".mem" / "handoffs"
STATE = HANDOFFS / ".state.json"
# Set when notes were written but the sqlite ingest could not run (another
# ingest held the lock). Without it, state records the session as checkpointed
# while the note was never embedded — written, findable on disk, and invisible
# to every `mem query`. Caught on the very first launchd fire. Cleared only
# after a clean ingest.
PENDING = HANDOFFS / ".pending"
LOG = HOME / ".mem" / "idle.log"
IDLE_LOCK = Path("/tmp/mem-idle.lock")
INGEST_LOCK = Path("/tmp/mem-ingest.lock")   # shared with session_catchup.sh

IDLE_MIN = 12 * 60          # idle this long before a session is "left"
MAX_AGE = 3 * 86400         # older than this, the nightly ingest owns it
# Two separate budgets, because they bound two different things. MAX_SUMMARIES
# bounds SPEND (haiku calls). MAX_SCAN bounds TIME (transcripts read). Capping
# candidates alone conflated them: subagent and headless transcripts touch their
# mtime constantly, so the newest 6 candidates are routinely all junk, one sweep
# would spend nothing and drain the backlog at 6 dead files per 5 minutes.
MAX_SUMMARIES = 4           # haiku calls per sweep — the money cap
MAX_SCAN = 40               # transcripts examined per sweep — the time cap
# What counts as a session worth summarising. Both dimensions are needed, and
# the thresholds are measured, not guessed — over the last 3 days of transcripts:
#
#   1 user turn : 129 files, median   2 total turns   ← headless -p / subagents
#   2 user turns:   4 files, median  27 total turns   ← 2 real sessions + 2 agents
#   5+ user turns:  6 files, median  96 total turns   ← real sessions
#
# The first cut used MIN_USER_TURNS=3 alone and threw away a 26-turn session
# where Nick had sent only two prompts — a long, substantive session is not the
# same thing as a chatty one, and prompt COUNT does not measure work done. User
# turns >= 2 excludes every headless run (they have exactly 1 by construction);
# total turns >= 8 excludes the 3-turn `agent-*` transcripts that slip past it.
MIN_USER_TURNS = 2
MIN_TOTAL_TURNS = 8         # overrides nightly_ingest's MIN_TURNS (4) for handoffs
PROMPT_CHAR_CAP = 40_000    # ~10k tokens of haiku input, ~$0.01 a checkpoint
HEAD_TURNS = 3              # the opening frames the project…
TAIL_TURNS = 30             # …the tail is where we actually are

PROMPT = """You are writing a HANDOFF NOTE so that a fresh AI coding session can \
pick this work up tomorrow with nothing else to go on. The transcript below is a \
work session that was just left idle.

Write for someone who has forgotten everything. Be concrete and specific: name \
files, functions, commands, numbers and decisions. Never write "various changes" \
or "discussed options" — say WHICH.

Return ONLY a JSON object, no prose around it, with these keys:
  "title":     6-10 words naming what this session was actually about
  "doing":     2-4 sentences on the state of the work when it was left
  "decisions": array of decisions that were MADE and should not be re-litigated, \
each with the reason. [] if none.
  "open":      array of unfinished threads / next steps, most important first. \
Each entry must be actionable on its own. [] if none.
  "files":     array of file paths that were created or modified. [] if none.

Omit anything you are not confident about rather than guessing. An empty array is \
a correct answer; an invented one is not.

--- TRANSCRIPT ---
"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def acquire(lock: Path, stale_min: int = 30) -> bool:
    """Atomic mkdir lock with a staleness reclaim, matching session_catchup.sh."""
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) > stale_min * 60:
            lock.rmdir()
    except OSError:
        pass
    try:
        lock.mkdir()
        return True
    except OSError:
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


def claude_meta(jsonl: Path) -> tuple[str | None, str | None]:
    """cwd + the session's own ai-title, both straight off the transcript.
    Only the first few hundred records are scanned — cwd appears immediately and
    a 116MB transcript must not be read end-to-end just for a header."""
    cwd = title = None
    for i, line in enumerate(_lines(jsonl)):
        rec = _json(line)
        if rec:
            cwd = cwd or rec.get("cwd")
            if rec.get("type") == "ai-title":
                title = rec.get("title") or rec.get("text") or title
        if (cwd and title) or i > 400:
            break
    return cwd, title


def trim(turns: list[tuple[str, str]]) -> str:
    """Head + tail of the conversation, under the char cap. The middle of a long
    session is the least useful part to a handoff: the opening says what this is,
    the tail says where we stopped."""
    if len(turns) > HEAD_TURNS + TAIL_TURNS:
        kept = turns[:HEAD_TURNS] + [("", "[… middle of session elided …]")] + turns[-TAIL_TURNS:]
    else:
        kept = turns
    out, total = [], 0
    for role, txt in reversed(kept):          # fill from the END backwards
        block = f"## {role}\n{txt[:MAX_TURN_CHARS]}\n" if role else f"{txt}\n"
        if total + len(block) > PROMPT_CHAR_CAP:
            break
        out.append(block)
        total += len(block)
    return "\n".join(reversed(out))


EDIT_PAT = re.compile(r'"file_path"\s*:\s*"([^"]+)"')
REPO_PAT = re.compile(r"/(?:Projects|Apps)/([A-Za-z0-9][\w.-]{1,40})")
REPO_SCAN_BYTES = 30 * 1024 * 1024


def repos_touched(jsonl: Path, limit: int = 3) -> list[str]:
    """Which repos this session actually WROTE to, off the Edit/Write tool calls.

    Two wrong sources were tried first and both are recorded here so they are not
    tried again. (1) cwd alone is lossy — a session started from ~ can spend all
    58 of its turns editing Projects/numen, which is what the first note written
    by this script did. (2) Counting repo names in the transcript PROSE is worse
    than lossy, it is actively misleading: the apps-page session is *about* the
    apps under ~/Apps, so it mentions Apps/scraper constantly while editing only
    Projects/numen, and prose-counting duly reported "scraper".

    A tool call's `file_path` is the only one of the three that is a record of
    what was changed rather than what was discussed. Scanned by regex over raw
    lines — no json parse, and byte-capped so a 116MB transcript cannot make a
    5-minute sweep run long."""
    counts: dict[str, int] = {}
    read = 0
    try:
        with open(jsonl, errors="replace") as f:
            for line in f:
                read += len(line)
                if read > REPO_SCAN_BYTES:
                    break
                if '"file_path"' not in line:
                    continue
                for path in EDIT_PAT.findall(line):
                    m = REPO_PAT.search(path)
                    if m:
                        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    except OSError:
        return []
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    floor = max(2, ranked[0][1] // 10) if ranked else 0   # drop incidental one-offs
    return [n for n, c in ranked[:limit] if c >= floor]


def extract_json(text: str) -> dict | None:
    """First balanced {...} in the model's reply. A brace scan, not a regex —
    the note bodies contain braces and a greedy/lazy regex gets both ends wrong."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def summarise(body: str) -> dict | None:
    """One haiku pass. Returns the parsed note, or None on any failure — a failed
    checkpoint must leave the state untouched so the next sweep retries it."""
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", "haiku", "--output-format", "json"],
            input=PROMPT + body, capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  claude failed: {type(e).__name__}")
        return None
    try:
        payload = json.loads(r.stdout)
        result = payload.get("result") or ""
        if payload.get("is_error"):
            log(f"  claude error: {str(result)[:160]}")
            return None
    except json.JSONDecodeError:
        # payload itself truncated — fall back to the raw text, same as numen's
        # claude_reason does. Never slice by character count to find a reason.
        result = r.stdout
    return extract_json(result)


def write_handoff(name: str, note: dict, meta: dict) -> Path:
    """Python writes the file and stamps the clock. The model supplied only the
    fields; every timestamp and count here is measured, never model-reported."""
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    title = str(note.get("title") or meta.get("title") or "session").strip()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    project = meta.get("cwd") or "?"
    short = Path(project).name if project != "?" else "?"

    def bullets(key: str) -> str:
        """Entries arrive as strings OR as {"decision": …, "reason": …} objects —
        the prompt asks for "each with the reason" and haiku reasonably reads that
        as a shape. Both are rendered; a repr() of a dict on the page is the kind
        of detail that makes a note read as machine output and stop being trusted."""
        vals = note.get(key)
        if not isinstance(vals, list) or not vals:
            return "_none recorded_\n"
        out = []
        for v in vals:
            if isinstance(v, dict):
                head = str(v.get("decision") or v.get("step") or v.get("item")
                           or v.get("title") or v.get("text") or "").strip()
                why = str(v.get("reason") or v.get("why") or v.get("detail") or "").strip()
                if not head:                       # unknown shape — keep the values
                    head = " — ".join(str(x).strip() for x in v.values() if str(x).strip())
                    why = ""
                line = f"**{head}** — {why}" if why else f"**{head}**" if head else ""
            else:
                line = str(v).strip()
            if line:
                out.append(f"- {line}\n")
        return "".join(out) or "_none recorded_\n"

    repos = meta.get("repos") or []

    body = f"""---
project: {project}
repos: {', '.join(repos) if repos else '-'}
harness: {meta['harness']}
session: {meta['session']}
title: {title}
checkpointed: {stamp}
turns: {meta['turns']}
---

# handoff — {short} — {time.strftime('%Y-%m-%d %H:%M')}

**{title}**

## Where the work was left
{str(note.get('doing') or '').strip() or '_not recorded_'}

## Decided (do not re-litigate)
{bullets('decisions')}
## Open threads
{bullets('open')}
## Files touched
{bullets('files')}
Resume this session with: `claude --resume {meta['session']}` (expensive on a \
long session — prefer starting fresh from this note).
"""
    path = HANDOFFS / name
    path.write_text(body)
    return path


def due(state: dict, force: str | None) -> list:
    """Every transcript that has gone idle and is not already checkpointed at its
    current mtime. Newest first, so a backlog spends its budget on what you most
    likely want back."""
    now = time.time()
    out = []
    for harness, root, extractor in HARNESSES:
        if not root.exists():
            continue
        for jsonl in root.rglob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            key = str(jsonl)
            if force:
                if force not in key:
                    continue
            else:
                idle = now - mtime
                if idle < IDLE_MIN or idle > MAX_AGE:
                    continue
                if state.get(key) == mtime:      # bound to the mtime it graded
                    continue
            out.append((mtime, harness, jsonl, extractor))
    out.sort(reverse=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list candidates, spend nothing")
    ap.add_argument("--force", metavar="SUBSTR", help="re-checkpoint sessions matching SUBSTR")
    args = ap.parse_args()

    # The lock exists to stop two SWEEPS overlapping. A --force run targets one
    # named session and must not be dropped just because a sweep is mid-flight:
    # it is what the SessionEnd hook calls, and that is the one checkpoint whose
    # whole value is being ready before the next session starts. sqlite writes
    # stay serialised by INGEST_LOCK either way, and a double write of the same
    # note is idempotent.
    held = False
    if not args.dry_run and not args.force:
        if not acquire(IDLE_LOCK):
            return                                # a sweep is already running
        held = True
    try:
        state = load_state()
        candidates = due(state, args.force)
        if not candidates and not (PENDING.exists() and not args.dry_run):
            return          # nothing due AND nothing owed to the index
        if not args.force:
            candidates = candidates[:MAX_SCAN]

        wrote = skipped = failed = 0
        for mtime, harness, jsonl, extractor in candidates:
            turns = extractor(jsonl)
            users = sum(1 for role, _ in turns if role == "User")
            if len(turns) < MIN_TOTAL_TURNS or users < MIN_USER_TURNS:
                # Headless `claude -p` runs, subagents and workflow transcripts all
                # land here: exactly one user turn, nothing to hand off. Marked so
                # they are judged once rather than re-read on every 5-min sweep.
                state[str(jsonl)] = mtime
                skipped += 1
                if args.dry_run:
                    log(f"  skip {jsonl.stem[:8]} ({len(turns)} turns, {users} user)")
                continue
            if args.dry_run:
                log(f"would checkpoint {jsonl.stem[:8]} "
                    f"({len(turns)} turns, idle {int((time.time()-mtime)/60)}m)")
                continue
            if not args.force and wrote >= MAX_SUMMARIES:
                break                             # spend cap; state keeps the rest due

            # 1. raw turns → minutes-fresh semantic recall (free, local embeddings)
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            label = jsonl.parent.name if harness == "claude" else jsonl.stem
            raw = [f"# {harness} session — {label[:60]}", ""]
            for role, txt in turns:
                raw.append(f"## {role}\n{txt[:MAX_TURN_CHARS]}\n")
            (SESSIONS_DIR / f"{harness}__{jsonl.stem}.md").write_text("\n".join(raw))

            # 2. the handoff note (one haiku pass)
            cwd, title = claude_meta(jsonl) if harness == "claude" else (None, None)
            note = summarise(trim(turns))
            if note is None:
                log(f"  {jsonl.stem[:8]}: summarise failed, will retry next sweep")
                failed += 1
                continue                          # state untouched → retried
            path = write_handoff(
                f"{harness}__{jsonl.stem}.md", note,
                {"harness": harness, "session": jsonl.stem, "cwd": cwd,
                 "title": title, "turns": len(turns),
                 "repos": repos_touched(jsonl)},
            )
            state[str(jsonl)] = mtime
            wrote += 1
            log(f"  checkpointed {Path(cwd).name if cwd else harness}/{jsonl.stem[:8]} "
                f"→ {path.name} ({len(turns)} turns)")

        if args.dry_run:
            log(f"dry run: {len(candidates)} candidate(s) of {len(due(state, None))} due")
            return
        save_state(state)
        # One line per sweep that did anything. A sweeper that runs 288 times a
        # day and never says what it did is indistinguishable from a dead one —
        # the same trap as numen's silently-failing _notify.
        if wrote or failed:
            log(f"sweep: {wrote} checkpointed, {skipped} skipped, {failed} failed, "
                f"{len(due(state, None))} still due")
        if wrote or PENDING.exists():
            if acquire(INGEST_LOCK):              # serialise sqlite writes
                try:
                    ok = run_mem(["ingest", str(SESSIONS_DIR / "*.md"), "--kind", "session"])
                    ok = run_mem(["ingest", str(HANDOFFS / "*.md"), "--kind", "handoff"]) and ok
                finally:
                    INGEST_LOCK.rmdir()
                if ok:
                    PENDING.unlink(missing_ok=True)
                    log(f"ingested {wrote} checkpoint(s)")
                else:
                    PENDING.touch()
                    log("ingest reported errors; will retry next sweep")
            else:
                PENDING.touch()
                log(f"wrote {wrote} note(s); ingest busy, next sweep picks them up")
    finally:
        if held:
            try:
                IDLE_LOCK.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    main()
