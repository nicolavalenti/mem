# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Nightly mem ingest — refresh memory files + vector recent Claude Code sessions.

Run by launchd (com.mem.ingest) at 06:30. Unlike cron, launchd's
StartCalendarInterval runs a MISSED job when the Mac next wakes / logs in, so it
still runs if the machine was asleep or off at 6:30.

Pure stdlib: extracts the readable turns from recent session transcripts into
~/.mem/sessions/*.md, then shells out to the `mem` CLI to embed everything
(incremental — unchanged files are skipped). Absolute paths throughout because
launchd starts with a bare environment.
"""
import json
import subprocess
import time
from pathlib import Path

HOME = Path.home()
UV = str(HOME / ".local" / "bin" / "uv")
MEM = str(HOME / "Projects" / "mem" / "mem.py")
MEMORY_GLOB = str(HOME / ".claude" / "projects" / "-Users-nickvalenti" / "memory" / "*.md")
PROJECTS = HOME / ".claude" / "projects"
SESSIONS_DIR = HOME / ".mem" / "sessions"
RECENT_DAYS = 3
MIN_TURNS = 4   # skip trivial/empty sessions


def run_mem(args: list[str]) -> None:
    subprocess.run([UV, "run", MEM, *args], check=False)


def extract_session(jsonl: Path) -> list[tuple[str, str]]:
    turns = []
    with open(jsonl, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            msg = rec.get("message") or {}
            if t == "user":
                c = msg.get("content")
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    turns.append(("User", c.strip()[:2000]))
            elif t == "assistant":
                for blk in (msg.get("content") or []):
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text", "").strip():
                        turns.append(("Assistant", blk["text"].strip()[:2000]))
    return turns


def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] nightly ingest")

    # 1) refresh the memory files
    run_mem(["ingest", MEMORY_GLOB, "--kind", "memory"])

    # 2) extract recent session transcripts → markdown (stable name per session)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - RECENT_DAYS * 86400
    n = 0
    for jsonl in PROJECTS.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        turns = extract_session(jsonl)
        if len(turns) < MIN_TURNS:
            continue
        proj = jsonl.parent.name
        out = SESSIONS_DIR / f"{proj}__{jsonl.stem[:8]}.md"
        lines = [f"# Session — {proj} ({jsonl.stem[:8]})", ""]
        for role, txt in turns:
            lines.append(f"## {role}\n{txt}\n")
        out.write_text("\n".join(lines))
        n += 1
    print(f"extracted {n} recent session(s) (last {RECENT_DAYS}d) → {SESSIONS_DIR}")

    # 3) ingest the sessions (incremental: only changed files re-embed)
    run_mem(["ingest", str(SESSIONS_DIR / "*.md"), "--kind", "session"])


if __name__ == "__main__":
    main()
