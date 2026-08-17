# Unity Manual MCP

Local MCP server (stdio) that serves a local Unity documentation install to your AI assistant.

The documentation folder is usually located in your Unity Editors install directory, under `Data\Documentation\en`.

- **`Manual/`** — how-to articles, organized in chapters (~3.5k pages)
- **`ScriptReference/`** — full scripting API reference, one page per
  class / property / method (~42k pages)

On first start the server builds a SQLite full-text index of every page
(Mozilla-Readability-style extraction of the article body, boilerplate
stripped, code examples preserved) and caches it in the OS temp dir.
Subsequent starts reuse it; it is rebuilt automatically if the docs file
count changes.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.10+ — declared in `pyproject.toml` (required by `fastmcp` 3.x).
  uv reuses any already-installed Python that meets this and only downloads
  a new one if no compatible interpreter exists.

## Setup

```powershell
uv sync
```

Creates `.venv` and installs the dependencies from `pyproject.toml`.

## Run

```powershell
uv run server.py "C:\Program Files\Unity\Hub\Editor\<ver>\Editor\Data\Documentation\en"
```

The `en` folder is auto-detected if you point at `...\Documentation` or
`...\Editor\Data` instead.

Flags:

- `--selftest` — build the index and run sample queries, then exit (no MCP
  server; good for verifying a fresh setup)
- `--rebuild` — force rebuilding the index
- `--prune` — drop stale cache entries (db file missing) from the manifest
- `UNITY_DOCS_PATH` env var — alternative to the positional argument
  <br>
> ℹ Run the server manually once after a fresh install to pre-index the docs and avoid waiting during first use
> ```powershell
> uv run server.py --selftest "C:\Program Files\Unity\Hub\Editor\<ver>\Editor\Data\Documentation\en"
> ```

## Adding the MCP Server

This is a stdio server, so any client that can launch a local process works.
Each client runs the same command:

```text
uv run --directory "<repo>" server.py "<docs path>"
```

where `<repo>` is this repo's folder and `<docs path>` is your Unity docs
`en` folder (auto-detected if you point at `...\Documentation` or
`...\Editor\Data` instead). `uv run` creates and syncs the `.venv`
environment automatically, so the client only needs uv on the PATH.

### Claude Desktop

`claude_desktop_config.json` (substitute your own paths):

```json
{
  "mcpServers": {
    "unity-manual": {
      "command": "uv",
      "args": ["run", "--directory", "<repo>", "server.py", "<docs path>"]
    }
  }
}
```

### Claude Code

```powershell
claude mcp add unity-manual -- uv run --directory "<repo>" server.py "<docs path>"
```

### OpenCode

`opencode.json` in the project, or the global config
(`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "unity-manual": {
      "type": "local",
      "command": ["uv", "run", "--directory", "<repo>", "server.py", "<docs path>"],
      "enabled": true
    }
  }
}
```

### Codex

```powershell
codex mcp add unity-manual -- uv run --directory "<repo>" server.py "<docs path>"
```

Or edit `~/.codex/config.toml` (shared with the ChatGPT desktop app and IDE
extension; a project-scoped `.codex/config.toml` works in trusted projects):

```toml
[mcp_servers.unity-manual]
command = "uv"
args = ["run", "--directory", "<repo>", "server.py", "<docs path>"]
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

### Credits

Fully written by Qwen 3.8 27b 🙂
