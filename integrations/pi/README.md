# Pi integration

`mem.ts` is a [Pi](https://pi.dev) extension that registers `memory_search` and `memory_add` as **native tools** backed by the shared `~/.mem/store.db`. Giving a model named tools is far more reliable than asking it to craft a `mem` CLI command — verified working with a local 35B model.

## Install

Pi auto-discovers extensions from `~/.pi/agent/extensions/`. Symlink (so updates here propagate) or copy:

```sh
ln -s "$PWD/mem.ts" ~/.pi/agent/extensions/mem.ts
```

Hot-reload inside a Pi session with `/reload`.

## Note

The `UV` and `MEM` constants at the top of `mem.ts` are absolute paths (no PATH dependency, so it works however Pi is launched). Adjust them if your `uv` / `mem.py` live elsewhere.
