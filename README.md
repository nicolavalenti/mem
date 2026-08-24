# mem

Local, cross-harness semantic memory. One SQLite file plus a local CPU embedder, with **no server and no cloud**. Any agent harness (Claude Code, Codex, Pi) or plain shell can read and write the **same** store, via a CLI or an MCP server.

## Why

Each harness has its own memory, or none. `mem` gives them one shared brain that never leaves the machine. The tool owns the embedder, so every caller writes into the same vector space, and callers send text, never vectors.

## Stack

- [`sqlite-vec`](https://github.com/asg017/sqlite-vec): vector search inside SQLite
- [`fastembed`](https://github.com/qdrant/fastembed): `BAAI/bge-small-en-v1.5` (384-dim, ONNX/CPU, ~100MB one-time download)
- Store at `~/.mem/store.db` (override with `MEM_DB`)

Requires [`uv`](https://github.com/astral-sh/uv). Both scripts are self-contained PEP-723 (deps declared inline); `uv` provisions a pinned Python and the deps on first run.

## CLI

```sh
# install: a wrapper on PATH that runs the script via uv
printf '#!/bin/sh\nexec uv run %s/mem.py "$@"\n' "$PWD" > ~/.local/bin/mem && chmod +x ~/.local/bin/mem

mem add "<fact>" --source <ctx> --kind note
mem query "<question>" -k 5 [--max-distance 0.55] [--json]
mem ingest "<glob>" --kind memory     # incremental: skips unchanged files
mem stats
mem forget --source <ctx>
```

## MCP

`mem_mcp.py` exposes `memory_search` / `memory_add` over the same store:

```sh
# Claude Code
claude mcp add mem --scope user -- uv run "$PWD/mem_mcp.py"
# Codex: add to ~/.codex/config.toml
#   [mcp_servers.mem]
#   command = "uv"
#   args = ["run", "/abs/path/mem_mcp.py"]
```

## Freshness

Re-ingest changed memory files on a schedule (cron):

```
25 6 * * * uv run /abs/path/mem.py ingest "<glob>" --kind memory
```
