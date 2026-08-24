# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Nightly mem ingest — refresh memory files + vector recent sessions from ALL
harnesses: Claude Code, Codex, and Pi.

Run by launchd (com.mem.ingest) at 06:30 with catch-up-on-wake. Pure stdlib:
extracts the readable turns from each harness's transcript format into
~/.mem/sessions/*.md, then shells out to `mem` to embed them (incremental —
unchanged files are skipped). Absolute paths because launchd starts bare.

  uv run nightly_ingest.py              # last 3 days (the nightly default)
  uv run nightly_ingest.py --days 3650  # one-time full backfill of all history
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

HOME = Path.home()
UV = str(HOME / ".local" / "bin" / "uv")
MEM = str(HOME / "Projects" / "mem" / "mem.py")
# Claude Code names each project dir after its absolute path with "/" -> "-",
# so the dir for $HOME is derivable rather than hardcoded.
PROJECT_SLUG = str(HOME).replace("/", "-")
MEMORY_GLOB = str(HOME / ".claude" / "projects" / PROJECT_SLUG / "memory" / "*.md")
SESSIONS_DIR = HOME / ".mem" / "sessions"
# Success stamp: its mtime advances ONLY after a clean ingest, so it is the
# honest freshness signal — unlike ingest.log, whose mtime moves the instant the
# SessionStart hook echoes its header, even on a run that then crashes. The numen
# dashboard reads THIS for mem-ingest freshness (see numen CLAUDE.md).
STAMP = HOME / ".mem" / "ingest.ok"
MIN_TURNS = 4
MAX_TURN_CHARS = 2000


def run_mem(args: list[str]) -> bool:
    """Run a mem subcommand; return True only on a clean (exit 0) run. mem.py
    has no try/except around its command fns, so an embed/db failure crashes to
    a non-zero exit — a reliable hard-failure signal worth recording."""
    return subprocess.run([UV, "run", MEM, *args], check=False).returncode == 0


def _lines(path: Path):
    try:
        with open(path, errors="replace") as f:
            yield from f
    except OSError:
        return


def _json(line: str):
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _texts(content) -> list[str]:
    """Readable text out of a content field (str, or list of blocks with .text)."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b["text"] for b in content
                if isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip()]
    return []


# ── per-harness transcript extractors → [(role, text), …] ──────────────────
def extract_claude(jsonl: Path) -> list[tuple[str, str]]:
    turns = []
    for line in _lines(jsonl):
        rec = _json(line)
        if not rec:
            continue
        t = rec.get("type")
        msg = rec.get("message") or {}
        if t == "user":
            c = msg.get("content")
            if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                turns.append(("User", c.strip()))
        elif t == "assistant":
            for txt in _texts(msg.get("content")):
                turns.append(("Assistant", txt.strip()))
    return turns


def extract_pi(jsonl: Path) -> list[tuple[str, str]]:
    turns = []
    for line in _lines(jsonl):
        rec = _json(line)
        if not rec or rec.get("type") != "message":
            continue
        m = rec.get("message") or {}
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = "\n".join(_texts(m.get("content"))).strip()
        if text and not text.startswith("<"):
            turns.append((role.capitalize(), text))
    return turns


def extract_codex(jsonl: Path) -> list[tuple[str, str]]:
    turns = []
    for line in _lines(jsonl):
        rec = _json(line)
        if not rec or rec.get("type") != "response_item":
            continue
        p = rec.get("payload") or {}
        if p.get("type") != "message":
            continue
        role = p.get("role")
        if role not in ("user", "assistant"):   # skip 'developer' (system) blobs
            continue
        text = "\n".join(_texts(p.get("content"))).strip()
        if text and not text.startswith("<"):
            turns.append((role.capitalize(), text))
    return turns


HARNESSES = [
    ("claude", HOME / ".claude" / "projects", extract_claude),
    ("pi", HOME / ".pi" / "agent" / "sessions", extract_pi),
    ("codex", HOME / ".codex" / "sessions", extract_codex),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="ingest sessions modified within N days (use a big N to backfill)")
    args = ap.parse_args()

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] nightly ingest (last {args.days}d)")
    ok = run_mem(["ingest", MEMORY_GLOB, "--kind", "memory"])

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - args.days * 86400
    counts: dict[str, int] = {}
    for harness, root, extractor in HARNESSES:
        if not root.exists():
            continue
        for jsonl in root.rglob("*.jsonl"):
            try:
                if jsonl.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            turns = extractor(jsonl)
            if len(turns) < MIN_TURNS:
                continue
            name = f"{harness}__{jsonl.stem}.md"   # full stem → unique, no collisions
            label = jsonl.parent.name if harness == "claude" else jsonl.stem
            body = [f"# {harness} session — {label[:60]}", ""]
            for role, txt in turns:
                body.append(f"## {role}\n{txt[:MAX_TURN_CHARS]}\n")
            (SESSIONS_DIR / name).write_text("\n".join(body))
            counts[harness] = counts.get(harness, 0) + 1
    print("extracted:", ", ".join(f"{k}={v}" for k, v in counts.items()) or "(none)")
    ok = run_mem(["ingest", str(SESSIONS_DIR / "*.md"), "--kind", "session"]) and ok

    if ok:
        STAMP.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))   # advances mtime only on a clean run
        print("ok — success stamp updated")
    else:
        # Don't touch the stamp: leave its mtime stale so freshness/status can see
        # the failure. Exit non-zero so a cron run records the real exit too.
        print("ingest reported errors — success stamp NOT updated")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
