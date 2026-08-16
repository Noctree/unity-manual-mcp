# AGENTS.md

Guidance for agents (and humans) working in this repo.

## What this is

A local **stdio MCP server** that serves a local Unity documentation install —
the `en` folder from a Unity editor's `Data\Documentation` — as MCP tools. Two
content trees:

- `Manual/` — how-to articles, organized in chapters (~3.5k pages)
- `ScriptReference/` — full scripting API reference (~42k pages)

It extracts article text (Mozilla-Readability-style: boilerplate stripped, code
examples preserved) and keeps a SQLite full-text index in the OS temp dir so
repeated starts are instant.

## Layout

- **`core.py`** — all the logic, **no MCP knowledge**: docs-root resolution,
  HTML extraction, `IndexStore` (SQLite index build/cache/search), and the
  cache manifest (`CACHE_ROOT`, `manifest.json`, stale/orphaned helpers).
  Depends only on beautifulsoup4 + lxml.
- **`server.py`** — thin **FastMCP** stdio wrapper: the 4 read-only tools, the
  argparse CLI (`--selftest`, `--rebuild`, `--prune`), and the `_selftest`
  runner. `mcp = FastMCP(...)` is at module level; `main()` sets the global
  `_store` before `mcp.run(transport="stdio")`.
- **`requirements.txt`** — `fastmcp>=3,<4`, `beautifulsoup4`, `lxml`.

Keep the split: anything that can run without MCP belongs in `core.py`.
`server.py` should stay a thin wrapper.

## Running

The venv is `venv`, created with **uv** (not pip). Use `venv\Scripts\python`.

```powershell
# one-time index build + sample queries, then exit (no server); ~12 min on a fresh cache
venv\Scripts\python server.py --selftest "C:\Program Files\Unity\Hub\Editor\<ver>\Editor\Data\Documentation\en"

# the MCP server itself (stdio; Claude Desktop / Claude Code spawn this)
venv\Scripts\python server.py "C:\...\Editor\Data\Documentation\en"

# drop stale manifest entries (db file missing) and exit
venv\Scripts\python server.py --prune
```

The docs path may be the `en` folder, or a wrapper (`Documentation` or
`Editor\Data`) — `core.resolve_docs_root` auto-discovers the `en` folder. It can
also come from the `UNITY_DOCS_PATH` env var.

## The 4 tools (all `readOnlyHint`)

| Tool | Purpose |
|---|---|
| `search_unity_docs(query, scope, limit)` | Full-text search; scope `all` / `manual` / `api` |
| `get_page(path, max_chars)` | Read one page as clean text (pass a result's `path`) |
| `list_sections(category)` | Browse manual chapters, or pages inside a chapter |
| `find_api(name, include_members)` | Find a scripting API page by C# name |

## Index cache

Flat dir `%TEMP%\unity-manual-mcp\` — one db file per editor (e.g.
`<sha1-16>.db`) plus `manifest.json` mapping each docs path → its db file.
Switching editors reuses its cached db (no re-index). Validated by HTML file
count. `--selftest` reports **stale** (db file missing) and **orphaned**
(docs folder gone) entries; `--prune` drops the stale ones. Delete a db file
(or `--rebuild`) to force a rebuild.

## Conventions & gotchas

- **Keep stdout clean.** The server speaks JSON-RPC over stdout. Log anything
  (index progress, errors) to **stderr** — see `IndexStore._log`. Never `print`
  to stdout in the server path. The `_selftest` runner does print, but it only
  runs under `--selftest`, never during `mcp.run(...)`.
- **The index build is slow** (~12 min; ~48k files; extraction fanned out over
  8 threads, inserted in batches). Don't trigger a fresh build in quick
  iterations — rely on the cache. The background build is guarded by
  `wait_ready(timeout=900)`; `--rebuild` deletes the db to force it.
- **Windows file locks:** adopting a cached db uses `os.replace` with a
  `shutil.copy2` fallback (a locked db can't be renamed but can be read). Don't
  remove the fallback.
- **Search is LIKE-based, not FTS** — every query term must appear (AND),
  ranked by term frequency. Don't add an FTS5 dependency without asking.
- **Extraction** is a Readability-style scorer: strip `BOILERPLATE_SELECTORS`,
  pick the highest-scoring article node (`_node_score`), convert to
  markdown-ish text (`<pre>` → fenced code, tables, lists). Tune
  `BOILERPLATE_SELECTORS` / `_node_score` / `_CONTAINER_SELECTORS` if a page
  extracts wrong.

## Repo rules (from global config)

- Never `git commit` — stage and report; the user commits.
- Never start the MCP (or other) servers — the user runs and manages them.
- Never install packages without asking — deps are in `requirements.txt`.
