"""Unity Manual MCP server (stdio transport).

Serves a local Unity documentation install (Manual + Scripting API) as MCP
tools. On first start it builds a full-text index in the background; see
core.py for details.

Usage:
    python server.py "C:\\Program Files\\Unity\\Hub\\Editor\\<ver>\\Editor\\Data\\Documentation\\en"
    python server.py --selftest <docs path>
    python server.py --rebuild <docs path>
    python server.py --prune

The docs path may also come from the UNITY_DOCS_PATH environment variable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from fastmcp import FastMCP

import core

mcp = FastMCP(
    name="unity-manual",
    instructions=(
        "Local Unity documentation: the Manual (how-to articles) and the "
        "Scripting API reference. Start with search_unity_docs for concepts, "
        "find_api for classes/members, list_sections to browse chapters, then "
        "get_page (pass a result's path) to read full pages."
    ),
)

_store: core.IndexStore | None = None


def _require_store() -> core.IndexStore:
    if _store is None:
        raise RuntimeError("Server not initialized")
    return _store


@mcp.tool(annotations={"readOnlyHint": True})
def search_unity_docs(query: str, scope: str = "all", limit: int = 10) -> dict:
    """Full-text search across the local Unity docs. Returns ranked pages with
    title, path, and snippet; pass the path to get_page. scope: 'all', 'manual'
    (how-to) or 'api' (scripting reference)."""
    return _require_store().search(query, scope, limit)


@mcp.tool(annotations={"readOnlyHint": True})
def get_page(path: str, max_chars: int = 60000) -> dict:
    """Read a docs page as clean text (code examples included). path is relative
    to the docs root, e.g. 'ScriptReference/MonoBehaviour.html' or
    'Manual/urp/index.html' — use the path from search results."""
    return _require_store().get_page(path, max_chars)


@mcp.tool(annotations={"readOnlyHint": True})
def list_sections(category: str | None = None) -> dict:
    """Browse the manual's chapter structure. No argument: top-level chapters
    with page counts. With a chapter name (e.g. 'urp'): pages inside it."""
    return _require_store().list_sections(category)


@mcp.tool(annotations={"readOnlyHint": True})
def find_api(name: str, include_members: bool = True) -> dict:
    """Find a scripting API page by C# name: 'MonoBehaviour',
    'WheelCollider.brakeTorque', 'Video.VideoPlayer.StepForward'. For a class
    the result includes its member list. Falls back to suggestions if not found."""
    return _require_store().find_api(name, include_members)


def _selftest(store: core.IndexStore) -> None:
    print(f"Docs root: {store.docs_root}")
    print(f"Index db:  {store.db_path}")
    print("Building/validating index...")
    t0 = time.time()
    store.wait_ready()
    print(f"Index ready in {time.time() - t0:.1f}s")

    print("\n--- find_api('MonoBehaviour') ---")
    r = store.find_api("MonoBehaviour")
    print(r.get("title", r.get("error")), r.get("path", ""),
          f"({r.get('chars')} chars, {len(r.get('members', []))} members)" if "title" in r else "")
    for m in r.get("members", [])[:5]:
        print("   ", m["member"])

    print("\n--- search_unity_docs('lighting settings', scope=manual) ---")
    r = store.search("lighting settings", scope="manual", limit=3)
    for x in r["results"]:
        print(f"  [{x['score']}] {x['title']}  {x['path']}")
        print(f"      {x['snippet'][:120]}")

    print("\n--- get_page('ScriptReference/WaitForSeconds.html') ---")
    r = store.get_page("ScriptReference/WaitForSeconds.html")
    if "error" in r:
        print("ERROR:", r["error"])
    else:
        print(r["title"], f"({r['chars']} chars)")
        print(r["text"])

    print("\n--- list_sections() ---")
    r = store.list_sections()
    for ch in r["chapters"][:8]:
        print(f"  {ch['name']} ({ch['pages']} pages)")
    print(f"  ... {len(r['chapters'])} chapters total, "
          f"{r['root_articles']} root articles, "
          f"{r['script_reference_pages']} API pages")

    print("\n--- manifest health ---")
    stale = core.find_stale_entries()
    orphaned = core.find_orphaned_entries()
    if not stale and not orphaned:
        print("  no stale or orphaned entries")
    for root, name in stale:
        print(f"  stale (db missing):    {root} -> {name}")
    for root, name in orphaned:
        print(f"  orphaned (docs gone):  {root} -> {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP server for local Unity documentation")
    ap.add_argument(
        "docs",
        nargs="?",
        help="Path to the docs 'en' folder (or the Documentation dir, or Editor/Data)",
    )
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="Build the index and run sample queries, then exit (no MCP server)",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the search index and exit (no MCP server)",
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="Remove stale cache entries (db file missing) and exit",
    )
    args = ap.parse_args()

    if args.prune:
        pruned = core.prune_stale_entries()
        if not pruned:
            print("No stale manifest entries to prune.")
        else:
            for root, name in pruned:
                print(f"Pruned: {root} -> {name}")
        return

    docs = args.docs or os.environ.get("UNITY_DOCS_PATH")
    if not docs:
        ap.error("docs path required (positional argument, or UNITY_DOCS_PATH env var)")
    docs_root = core.resolve_docs_root(docs)

    global _store
    _store = core.IndexStore(docs_root, rebuild=args.rebuild)

    if args.rebuild:
        t0 = time.time()
        try:
            _store.wait_ready()
        except RuntimeError as e:
            raise SystemExit(f"Index rebuild failed: {e}")
        print(f"Rebuilt index in {time.time() - t0:.1f}s -> {_store.db_path}")
        if not args.selftest:
            return

    if args.selftest:
        _selftest(_store)
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
