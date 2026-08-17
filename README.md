# Unity Manual MCP

Local MCP server (stdio) that serves a Unity documentation install — the
`en` folder from a Unity editor's `Data\Documentation` directory — to Claude.

- **`Manual/`** — how-to articles, organized in chapters (~3.5k pages)
- **`ScriptReference/`** — full scripting API reference, one page per
  class / property / method (~42k pages)

On first start the server builds a SQLite full-text index of every page
(Mozilla-Readability-style extraction of the article body, boilerplate
stripped, code examples preserved) and caches it in the OS temp dir.
Subsequent starts reuse it; it is rebuilt automatically if the docs file
count changes.

## Requirements

- [uv](https://docs.astral.sh/uv/) (or pip)
- Python 3.10+ — required by `fastmcp` 3.x. uv will reuse any Python
  already installed that meets this and only download a new one if no
  compatible interpreter exists.

## Setup

```powershell
uv venv venv --python ">=3.10,<4"
uv pip install -r requirements.txt
```

`--python ">=3.10,<4"` is a version range, not a pinned version, so uv
uses whatever Python 3.10+ you already have (system-installed or
previously downloaded by uv) instead of fetching a specific one.

## Run

```powershell
venv\Scripts\python server.py "C:\Program Files\Unity\Hub\Editor\<ver>\Editor\Data\Documentation\en"
```

The `en` folder is auto-detected if you point at `...\Documentation` or
`...\Editor\Data` instead.

Flags:

- `--selftest` — build the index and run sample queries, then exit (no MCP
  server; good for verifying a fresh setup)
- `--rebuild` — force rebuilding the index
- `--prune` — drop stale cache entries (db file missing) from the manifest
- `UNITY_DOCS_PATH` env var — alternative to the positional argument

## Claude Desktop

`claude_desktop_config.json` (substitute your own paths):

```json
{
  "mcpServers": {
    "unity-manual": {
      "command": "<path to this repo>\\venv\\Scripts\\python.exe",
      "args": [
        "<path to this repo>\\server.py",
        "C:\\Program Files\\Unity\\Hub\\Editor\\<ver>\\Editor\\Data\\Documentation\\en"
      ]
    }
  }
}
```

## Claude Code

```powershell
claude mcp add unity-manual -- venv\Scripts\python.exe server.py "C:\Program Files\Unity\Hub\Editor\<ver>\Editor\Data\Documentation\en"
```

## Tools

All read-only (`readOnlyHint`):

| Tool | Purpose |
|---|---|
| `search_unity_docs` | Full-text search (all / manual / api) |
| `get_page` | Read one page as clean text (path from search results) |
| `list_sections` | Browse manual chapters, or pages inside a chapter |
| `find_api` | Find a class / member API page by C# name |

## Index cache

`%TEMP%\unity-manual-mcp\` holds one db file per editor (e.g. `<hash>.db`)
plus a `manifest.json` mapping each docs path to its db file. Switching editors
reuses the cached index; it is validated by HTML file count. Delete a db file
(or use `--rebuild`) to force a rebuild. `--selftest` reports **stale** entries
(db file missing) and **orphaned** entries (docs folder gone, db still there);
`--prune` drops the stale ones.

## Distributing

This is a local-stdio server, which is fine for personal use. If you later
want to ship it to others, repackage it as an **MCPB** (bundled local server
with runtime) — the stdio server can be bundled as-is.
