# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Print the most recent session handoffs for a project. Backs the /resume
command; also useful standalone.

This is the deliberately DUMB half of the recall story, and that is the point.
mem_recall.py already surfaces handoffs semantically on every prompt, but it
returns the top 3 hits over a whole 36k-item store above a 0.6 cosine floor —
which is a ranking, not a guarantee. "Where were we" needs a guarantee, and the
answer is a directory listing filtered by repo. Free, instant, complete, and it
cannot silently return nothing because an embedding disagreed.

  uv run resume.py                  # handoffs for $PWD's repo
  uv run resume.py --n 5            # more of them
  uv run resume.py apps             # anything matching "apps" in any repo
  uv run resume.py --all            # most recent handoffs, unfiltered
"""
import argparse
import os
import sys
from pathlib import Path

HANDOFFS = Path.home() / ".mem" / "handoffs"


def parse(path: Path) -> dict | None:
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return None
    if not raw.startswith("---"):
        return None
    end = raw.find("\n---", 3)
    if end < 0:
        return None
    meta = {}
    for line in raw[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    meta["_body"] = raw[end + 4:].strip()
    meta["_path"] = str(path)
    meta["_repos"] = [r.strip() for r in (meta.get("repos") or "").split(",") if r.strip() != "-"]
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="*",
                    help="substring matching a repo, a title, or a session id prefix")
    # Default 1, not 3. Nick routinely has 15+ sessions open across 4 repos, so a
    # repo filter alone can match five notes about five unrelated threads.
    # Printing three in full spends context on two he did not ask for; the index
    # below costs one line each and lets him name the one he means.
    ap.add_argument("--n", type=int, default=1, help="how many notes to print IN FULL")
    ap.add_argument("--all", action="store_true", help="ignore the repo filter")
    ap.add_argument("--list", action="store_true", help="index only, no full notes")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()

    if not HANDOFFS.exists():
        print("No handoffs yet. The checkpointer (com.mem.idle) writes one per "
              "session about 12 minutes after you stop working in it.")
        return

    notes = [n for n in (parse(p) for p in HANDOFFS.glob("*.md")) if n]
    # Sort on the `checkpointed` string, which Python wrote in ISO form and so
    # sorts lexicographically. Not on file mtime: re-ingest and a manual --force
    # both touch mtime without the note being any newer than what it describes.
    notes.sort(key=lambda n: n.get("checkpointed", ""), reverse=True)

    needle = " ".join(args.filter).lower()
    repo = Path(args.cwd).name

    if not args.all:
        if needle:
            # Session id first: `/resume 5614d347` must select exactly that note
            # and not fall through to a fuzzy title match on the same string.
            exact = [n for n in notes if n.get("session", "").startswith(needle)]
            notes = exact or [
                n for n in notes
                if needle in (n.get("title", "") + " " + " ".join(n["_repos"])
                              + " " + n.get("project", "")).lower()]
        else:
            notes = [n for n in notes
                     if repo in n["_repos"] or n.get("project", "").endswith("/" + repo)]

    if not notes:
        scope = f'matching "{needle}"' if needle else f"for {repo}"
        print(f"No handoffs {scope}. Try `--all`, or a different search term.")
        return

    where = f'matching "{needle}"' if needle else f"for {repo}"
    print(f"{len(notes)} handoff(s) {where}, newest first:\n")
    # The index always prints in full, however many there are — one line each is
    # cheap, and seeing that four other threads exist is the point when you closed
    # a terminal holding six sessions.
    for i, n in enumerate(notes):
        mark = "▸" if i < args.n and not args.list else " "
        when = n.get("checkpointed", "?")[:16].replace("T", " ")
        print(f" {mark} {when}  {n.get('session', '?')[:8]}  {n.get('title', '(untitled)')}")
    print()

    if args.list:
        print(f"Full note: /resume <session-id>  (e.g. /resume {notes[0].get('session','')[:8]})")
        return

    if len(notes) > args.n:
        print(f"Printing the newest {args.n} in full. For another, "
              f"run /resume <session-id> from the list above.\n")
    for n in notes[:args.n]:
        print("=" * 72)
        print(f"{n.get('checkpointed', '?')}  ·  session {n.get('session', '?')[:8]}"
              f"  ·  {n.get('project', '?')}")
        print(f"file: {n['_path']}")
        print("=" * 72)
        print(n["_body"])
        print()


if __name__ == "__main__":
    main()
