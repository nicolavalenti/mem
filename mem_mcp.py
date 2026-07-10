# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mcp", "fastembed", "sqlite-vec", "numpy"]
# ///
"""mem-mcp — exposes the shared `mem` store to MCP-capable harnesses
(Claude Code, Codex) as native tools. Same DB (~/.mem/store.db), same embedder
as the CLI — it imports mem.py so there is exactly one implementation.

Register:
  claude mcp add mem --scope user -- uv run /Users/nickvalenti/Projects/mem/mem_mcp.py
  # Codex: add [mcp_servers.mem] to ~/.codex/config.toml
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mem import _insert, connect, embed, search  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mem")


@mcp.tool()
def memory_search(query: str, k: int = 5, max_distance: float = 0.55,
                  include_courses: bool = False) -> str:
    """Semantic search of Nick's local cross-harness memory. Call this BEFORE
    answering a recall/context question or web-searching — it's free and local.
    Returns the most relevant snippets with their source and a similarity score.
    The course corpus (skool-aiautomations, ~72% of the store and quick to date)
    is excluded by default; pass include_courses=True to search it too."""
    hits = search(query, k, max_distance, include_courses=include_courses)
    return json.dumps(hits, indent=2) if hits else "(nothing relevant in memory)"


@mcp.tool()
def memory_add(text: str, source: str = "agent", kind: str = "note") -> str:
    """Store a durable fact/learning into Nick's shared local memory so any
    harness can recall it later. Use for decisions, preferences, and facts worth
    keeping — not transient chatter."""
    db = connect()
    sha = hashlib.sha1(text.encode()).hexdigest()[:12]
    _insert(db, text, source, kind, "", sha, embed([text])[0])
    db.commit()
    return f"stored 1 item (source={source}, kind={kind})"


if __name__ == "__main__":
    mcp.run()  # stdio transport — what Claude Code / Codex expect
