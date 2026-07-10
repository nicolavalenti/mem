# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["fastembed", "sqlite-vec", "numpy"]
# ///
"""mem — local, system-wide semantic memory shared across harnesses.

One SQLite file (~/.mem/store.db) with sqlite-vec for vector search, embedded
locally with fastembed (no server, no cloud). Any harness — Claude Code, Codex,
Pi, a shell, a cron — shells out to this CLI; nothing leaves the machine.

The TOOL owns the embedder, so every caller writes into the SAME vector space:
callers send text, never vectors. (Pinned to Python <3.13 so uv provisions an
interpreter onnxruntime has wheels for, regardless of the system Python.)

  mem add    "<text>" [--source S --kind K --tags t1,t2]
  mem query  "<q>" [-k 5] [--max-distance 0.55] [--json]
  mem ingest <path-or-glob> ... [--kind memory]   # incremental, skips unchanged
  mem stats
  mem forget --source S
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("MEM_DB", str(Path.home() / ".mem" / "store.db")))
MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, ONNX/CPU, ~100MB one-time download
DIM = 384
DEFAULT_MAX_DISTANCE = 0.55        # cosine distance; lower = more similar (gate)

# ── embedder: lazy, owned by the tool so all callers share one vector space ──
_emb = None


def _embedder():
    global _emb
    if _emb is None:
        from fastembed import TextEmbedding
        _emb = TextEmbedding(MODEL)
    return _emb


def embed(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in _embedder().embed(list(texts))]


# ── storage ──────────────────────────────────────────────────────────────
def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    import sqlite_vec
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")   # safe concurrent access across harnesses
    db.execute(
        """CREATE TABLE IF NOT EXISTS items(
             id INTEGER PRIMARY KEY,
             text TEXT NOT NULL, source TEXT, kind TEXT, tags TEXT,
             sha TEXT, created_ts REAL)""")
    db.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(
              embedding float[{DIM}] distance_metric=cosine)""")
    return db


def _ser(vec: list[float]):
    import sqlite_vec
    return sqlite_vec.serialize_float32(vec)


def _insert(db, text, source, kind, tags, sha, vec) -> int:
    cur = db.execute(
        "INSERT INTO items(text,source,kind,tags,sha,created_ts) VALUES(?,?,?,?,?,?)",
        (text, source, kind, tags, sha, time.time()))
    db.execute("INSERT INTO vec_items(rowid, embedding) VALUES(?,?)",
               (cur.lastrowid, _ser(vec)))
    return cur.lastrowid


def _drop_source(db, source: str) -> None:
    for (rid,) in db.execute("SELECT id FROM items WHERE source=?", (source,)).fetchall():
        db.execute("DELETE FROM vec_items WHERE rowid=?", (rid,))
    db.execute("DELETE FROM items WHERE source=?", (source,))


def chunk(text: str, size: int = 800) -> list[str]:
    """Pack paragraphs into ~size-char chunks; hard-split very long paragraphs."""
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if buf and len(buf) + len(para) + 2 > size:
            parts.append(buf.strip())
            buf = ""
        buf += para + "\n\n"
        while len(buf) > size * 1.5:
            parts.append(buf[:size].strip())
            buf = buf[size:]
    if buf.strip():
        parts.append(buf.strip())
    return parts


# ── commands ─────────────────────────────────────────────────────────────
def cmd_add(a):
    db = connect()
    sha = hashlib.sha1(a.text.encode()).hexdigest()[:12]
    _insert(db, a.text, a.source or "manual", a.kind or "note",
            a.tags or "", sha, embed([a.text])[0])
    db.commit()
    print(f"added 1 item (source={a.source or 'manual'})")


def cmd_ingest(a):
    db = connect()
    files = []
    for pat in a.paths:
        files += [Path(p) for p in glob.glob(os.path.expanduser(pat))]
    n_files = n_chunks = 0
    for p in files:
        if not p.is_file():
            continue
        raw = p.read_text(errors="replace")
        sha = hashlib.sha1(raw.encode()).hexdigest()[:12]
        src = str(p)
        row = db.execute("SELECT sha FROM items WHERE source=? LIMIT 1", (src,)).fetchone()
        if row and row[0] == sha:        # unchanged → skip (incremental)
            continue
        _drop_source(db, src)            # changed/new → re-ingest
        chunks = chunk(raw)
        for c, v in zip(chunks, embed(chunks) if chunks else []):
            _insert(db, c, src, a.kind or "memory", "", sha, v)
        n_files += 1
        n_chunks += len(chunks)
    db.commit()
    print(f"ingested {n_files} changed file(s), {n_chunks} chunk(s)")


# The course corpus (kind=skool-aiautomations) is ~72% of the store and floods
# every query — and it dates fast (esp. the AI content), so most of it is stale
# within months. Excluded from recall by DEFAULT everywhere (CLI, MCP, every
# harness); opt back in per-query with include_courses / --courses.
DEFAULT_EXCLUDE_KINDS = {"skool-aiautomations"}

def search(query: str, k: int = 5, max_distance: float = DEFAULT_MAX_DISTANCE,
           include_courses: bool = False) -> list[dict]:
    """Semantic search, single source of truth for the CLI and the MCP tool.
    sqlite-vec applies its KNN `k` BEFORE any kind filter, so a store that's 72%
    courses would starve every other kind if we filtered in SQL. Instead over-fetch
    at the vector layer, drop the excluded kinds, and keep the top k."""
    db = connect()
    exclude = set() if include_courses else set(DEFAULT_EXCLUDE_KINDS)
    fetch = k if not exclude else min(600, max(k * 30, 250))
    rows = db.execute(
        """SELECT items.text, items.source, items.kind, vec_items.distance
             FROM vec_items JOIN items ON items.id = vec_items.rowid
            WHERE vec_items.embedding MATCH ? AND k = ?
            ORDER BY vec_items.distance""",
        (_ser(embed([query])[0]), fetch)).fetchall()
    out = []
    for (t, s, kd, d) in rows:
        if d > max_distance or kd in exclude:
            continue
        out.append({"text": t, "source": s, "kind": kd, "score": round(1 - d, 3)})
        if len(out) >= k:
            break
    return out


def cmd_query(a):
    results = search(a.query, a.k, a.max_distance, include_courses=a.courses)
    if a.json:
        print(json.dumps(results, indent=2))
    elif not results:
        print("(nothing relevant)")
    else:
        for r in results:
            print(f"[{r['score']:.2f}] {r['source']}  ({r['kind']})")
            print("    " + r["text"][:200].replace("\n", " "))


def cmd_stats(a):
    db = connect()
    n = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    srcs = db.execute("SELECT COUNT(DISTINCT source) FROM items").fetchone()[0]
    print(f"{n} items across {srcs} sources  ({DB_PATH})")
    for k, c in db.execute("SELECT kind, COUNT(*) FROM items GROUP BY kind ORDER BY 2 DESC"):
        print(f"  {k or '(none)'}: {c}")


def cmd_forget(a):
    db = connect()
    ids = db.execute("SELECT id FROM items WHERE source=?", (a.source,)).fetchall()
    _drop_source(db, a.source)
    db.commit()
    print(f"forgot {len(ids)} item(s) from source={a.source}")


def main():
    ap = argparse.ArgumentParser(prog="mem", description="local semantic memory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="store a single note")
    p.add_argument("text")
    p.add_argument("--source")
    p.add_argument("--kind")
    p.add_argument("--tags")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("query", help="semantic search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--courses", action="store_true",
                   help="include the course corpus (kind=skool-aiautomations, excluded by default)")
    p.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE,
                   dest="max_distance")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("ingest", help="embed files (incremental)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--kind")
    p.set_defaults(fn=cmd_ingest)

    sub.add_parser("stats", help="store summary").set_defaults(fn=cmd_stats)

    p = sub.add_parser("forget", help="delete all items from a source")
    p.add_argument("--source", required=True)
    p.set_defaults(fn=cmd_forget)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
