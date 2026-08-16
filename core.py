"""Core logic for the Unity docs MCP server (no MCP knowledge here).

Reads a local Unity documentation tree (the ``en`` folder from a Unity editor
install), extracts article text with a Mozilla-Readability-style "focus mode"
algorithm (BeautifulSoup), and keeps a SQLite full-text index in the OS temp
dir so repeated starts are instant.

Dependencies: beautifulsoup4 (+ lxml parser, optional but much faster).
"""
from __future__ import annotations

import copy
import hashlib
import html as html_mod
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

try:
    from bs4 import BeautifulSoup, NavigableString

    _BS4 = True
except ImportError:
    _BS4 = False

# Top-level content directories inside the docs `en` folder.
CONTENT_DIRS = ("Manual", "ScriptReference")

# ---------------------------------------------------------------------------
# Docs root resolution
# ---------------------------------------------------------------------------


def resolve_docs_root(raw: str) -> Path:
    """Resolve the docs ``en`` folder, auto-discovering common wrapper paths."""
    p = Path(raw)
    if not p.exists():
        raise SystemExit(f"Docs path does not exist: {p}")
    if not p.is_dir():
        raise SystemExit(f"Docs path must be a directory, not a file: {p}")
    for cand in (p, p / "en", p / "Documentation" / "en"):
        if (cand / "Manual").is_dir() and (cand / "ScriptReference").is_dir():
            return cand.resolve()
    raise SystemExit(
        f"No Unity docs layout found at '{p}' "
        "(expected a folder containing both 'Manual/' and 'ScriptReference/', "
        "possibly under 'Documentation/en')."
    )


# ---------------------------------------------------------------------------
# HTML extraction (Mozilla-Readability-style "focus mode")
# ---------------------------------------------------------------------------

DROP_TAGS = {
    "script", "style", "noscript", "svg", "iframe", "form",
    "input", "button", "select", "option", "meta", "link",
}

# Boilerplate present in Unity doc templates: header/nav/sidebar/footer,
# feedback forms, next/prev links, search forms, version/language switchers.
BOILERPLATE_SELECTORS = [
    ".header-wrapper",
    ".toolbar",
    ".sidebar",
    ".footer-wrapper",
    ".suggest",
    ".suggest-wrap",
    ".scrollToFeedback",
    ".nextprev",
    ".search-form",
    ".lang-switcher",
    ".mobileLogo",
    "#DocsAnalyticsData",
    "#_leavefeedback",
]


def _require_bs4():
    if not _BS4:
        raise SystemExit(
            "beautifulsoup4 is not installed — run: uv pip install -r requirements.txt"
        )


def make_soup(html_text: str):
    _require_bs4()
    try:
        return BeautifulSoup(html_text, "lxml")
    except Exception:
        return BeautifulSoup(html_text, "html.parser")


def _strip_boilerplate(soup):
    for el in soup.select(", ".join(BOILERPLATE_SELECTORS)):
        el.decompose()
    for el in soup.find_all("known_issues"):
        el.decompose()


_CONTAINER_SELECTORS = ("#content-wrap", ".content-wrap", ".content-block")
_IDENT_BONUS = re.compile(r"\b(content|article|entry|body|main|section|subsection)\b")


def _precompute_scores(root):
    """One bottom-up pass computing per-node (chars, pieces, uplink, upsum).

    chars/pieces  -> the article's text as get_text(" ", strip=True) would join
                     it (chars = sum of stripped fragment lengths, pieces = count
                     of non-empty fragments; ln = chars + pieces - 1).
    uplink/upsum  -> sum of that text's length over <a>/<p> elements in the
                     subtree, INCLUDING the node itself if it is an <a>/<p>.

    These are additive over the subtree, so this single O(n) traversal replaces
    the per-element get_text / find_all walks the old _node_score did, which made
    the article-node search quadratic in DOM size.
    """
    cache = {}
    order = []
    stack = [root]
    while stack:
        node = stack.pop()
        order.append(node)
        for ch in node.children:
            if not isinstance(ch, NavigableString):
                stack.append(ch)
    for node in reversed(order):
        chars = pieces = uplink = upsum = 0
        for ch in node.children:
            if isinstance(ch, NavigableString):
                t = str(ch).strip()
                if t:
                    chars += len(t)
                    pieces += 1
            else:
                c, p, l, ps = cache[id(ch)]
                chars += c
                pieces += p
                uplink += l
                upsum += ps
        ln = chars + (pieces - 1) if pieces else 0
        if node.name == "a":
            uplink += ln
        if node.name == "p":
            upsum += ln
        cache[id(node)] = (chars, pieces, uplink, upsum)
    return cache


def _node_score(el, cache) -> float:
    """Readability-style score: text length, link-density penalty, name bonus."""
    chars, pieces, uplink, upsum = cache[id(el)]
    ln = chars + (pieces - 1) if pieces else 0
    if ln < 50:
        return -1.0
    link = uplink - (ln if el.name == "a" else 0)
    psum = upsum - (ln if el.name == "p" else 0)
    score = ln - link
    ident = ((el.get("id") or "") + " " + " ".join(el.get("class", []) or [])).lower()
    if _IDENT_BONUS.search(ident):
        score += 150.0
    score += psum * 0.25
    return score


def _find_article_node(soup):
    """Pick the article container: known wrapper if present, else highest score."""
    container = None
    for sel in _CONTAINER_SELECTORS:
        el = soup.select_one(sel)
        if el:
            container = el
            break
    if container is None:
        container = soup.body if soup.body is not None else soup
    cache = _precompute_scores(container)
    best, best_score = container, _node_score(container, cache)
    for el in container.find_all(True):
        s = _node_score(el, cache)
        if s > best_score:
            best, best_score = el, s
    return best


_HEADING_RE = re.compile(r"^h([1-6])$")


def _clean_inline(el) -> str:
    t = html_mod.unescape(el.get_text(" ", strip=True))
    return re.sub(r"\s+", " ", t)


_PRE_INLINE_TAGS = ("a", "code", "span", "em", "strong", "b", "i", "u", "sub", "sup", "font", "kbd")


def _pre_text(node) -> str:
    """Verbatim code text from a ``<pre>`` block.

    Unity code blocks hold line breaks in real newlines AND ``<br>`` tags, and
    wrap identifiers in inline ``<a>`` links; a plain ``get_text(" ")`` with a
    separator would put every linked identifier on its own line. So: ``<br>``
    becomes a newline, inline tags are unwrapped, and the text is read with no
    separator.
    """
    clone = copy.deepcopy(node)
    for br in clone.find_all("br"):
        br.replace_with("\n")
    for tag in clone.find_all(_PRE_INLINE_TAGS):
        tag.unwrap()
    return html_mod.unescape(clone.get_text()).strip("\n")


def _convert_node(node, lines: list[str]) -> None:
    """Convert the article subtree to markdown-ish lines (code preserved)."""
    name = node.name
    if name in DROP_TAGS:
        return
    m = _HEADING_RE.match(name) if name else None
    if m:
        t = _clean_inline(node)
        if t:
            lines.append("")
            lines.append("#" * int(m.group(1)) + " " + t)
            lines.append("")
        return
    if name == "pre":
        code = _pre_text(node)
        if code.strip():
            lines.append("```")
            lines.extend(code.splitlines())
            lines.append("```")
        return
    if name == "p":
        t = _clean_inline(node)
        if t:
            lines.append("")
            lines.append(t)
            lines.append("")
        return
    if name == "li":
        t = _clean_inline(node)
        if t:
            lines.append("- " + t)
        return
    if name in ("ul", "ol"):
        for li in node.find_all("li", recursive=False):
            t = _clean_inline(li)
            if t:
                lines.append("- " + t)
        return
    if name == "table":
        for tr in node.find_all("tr"):
            cells = [_clean_inline(td) for td in tr.find_all(["td", "th"])]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        return
    if name in ("span", "strong", "em", "b", "i", "a", "code"):
        t = _clean_inline(node)
        if t:
            lines.append(t + " ")
        return
    # Unknown container: recurse.
    for child in node.children:
        if isinstance(child, NavigableString):
            t = re.sub(r"\s+", " ", html_mod.unescape(str(child))).strip()
            if t:
                lines.append(t + " ")
        elif child.name not in DROP_TAGS:
            _convert_node(child, lines)


def _normalize_lines(lines: list[str]) -> str:
    """Collapse whitespace runs; keep code blocks verbatim."""
    out: list[str] = []
    in_code = False
    prev_blank = False
    for ln in lines:
        if ln == "```":
            in_code = not in_code
            out.append("```")
            prev_blank = False
            continue
        if in_code:
            out.append(ln)
            prev_blank = False
            continue
        s = ln.rstrip()
        if not s:
            if not prev_blank:
                out.append("")
            prev_blank = True
            continue
        prev_blank = False
        out.append(s)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def extract_article(path: Path, docs_root: Path) -> tuple[str, str]:
    """Extract ``(title, article text)`` from one docs HTML file."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    soup = make_soup(raw)
    _strip_boilerplate(soup)
    node = _find_article_node(soup)
    lines: list[str] = []
    _convert_node(node, lines)
    text = _normalize_lines(lines)
    h1 = node.find("h1") or soup.find("h1")
    if h1 is not None:
        title = _clean_inline(h1)
    elif soup.title is not None:
        t = _clean_inline(soup.title)
        title = re.sub(r"^Unity\s*[-–]\s*(Scripting API|Manual)\s*:\s*", "", t) or t
    else:
        title = ""
    if not title:
        title = path.stem
    return title, text


def _extract_worker(args: tuple[Path, Path]) -> tuple[str, str, str, str]:
    """Extract one file. Module-level so the index build can farm work to a
    process pool (bs4's extraction is pure Python; threads would serialize
    under the GIL)."""
    docs_root, path = args
    rel = path.relative_to(docs_root).as_posix()
    kind = "manual" if rel.startswith("Manual/") else "api"
    title, text = extract_article(path, docs_root)
    return (rel, kind, title, text)


# ---------------------------------------------------------------------------
# SQLite index
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  path  TEXT PRIMARY KEY,
  kind  TEXT,
  title TEXT,
  text  TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# ---------------------------------------------------------------------------
# Cache layout: a flat dir holding one db file per editor, with manifest.json
# mapping each docs path to its db file name. Switching editors reuses the
# cached db; each editor keeps its own file.
# ---------------------------------------------------------------------------

CACHE_ROOT = Path(tempfile.gettempdir()) / "unity-manual-mcp"
MANIFEST_PATH = CACHE_ROOT / "manifest.json"


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def find_stale_entries() -> list[tuple[str, str]]:
    """Manifest entries whose db file no longer exists: (docs_root, db_name)."""
    stale = []
    for root, name in _load_manifest().items():
        if not (CACHE_ROOT / name).exists():
            stale.append((root, name))
    return stale


def find_orphaned_entries() -> list[tuple[str, str]]:
    """Manifest entries whose docs folder is gone (db still there, reclaimable)."""
    orphaned = []
    for root, name in _load_manifest().items():
        if (CACHE_ROOT / name).exists() and not Path(root).exists():
            orphaned.append((root, name))
    return orphaned


def prune_stale_entries() -> list[tuple[str, str]]:
    """Drop stale (dangling) manifest entries; returns the (docs_root, db) list."""
    manifest = _load_manifest()
    pruned = []
    for root, name in list(manifest.items()):
        if not (CACHE_ROOT / name).exists():
            del manifest[root]
            pruned.append((root, name))
    if pruned:
        _save_manifest(manifest)
    return pruned


class IndexStore:
    """SQLite full-text index over the docs tree.

    Built once (background thread) and cached flat under
    %TEMP%\\unity-manual-mcp, with manifest.json mapping each docs path to its
    db file. Switching editors reuses the cached db; validated by file count.
    """

    def __init__(self, docs_root: Path, rebuild: bool = False):
        self.docs_root = docs_root
        self.cache_dir = CACHE_ROOT
        self.db_path = self._resolve_db_path()
        self._ready = threading.Event()
        self._error: Exception | None = None
        if rebuild:
            try:
                self.db_path.unlink()
            except FileNotFoundError:
                pass
        if self._is_valid():
            self._ready.set()
        else:
            threading.Thread(target=self._build, name="index-builder", daemon=True).start()

    # -- cache location -----------------------------------------------------

    def _resolve_db_path(self) -> Path:
        """Map docs_root -> db file via manifest.json (adopt old sub-dir cache)."""
        key = hashlib.sha1(str(self.docs_root).encode("utf-8")).hexdigest()[:16]
        manifest = _load_manifest()
        name = manifest.get(str(self.docs_root))
        if name:
            return CACHE_ROOT / name
        name = f"{key}.db"
        db_path = CACHE_ROOT / name
        if not db_path.exists():
            old_db = CACHE_ROOT / key / "index.db"
            if old_db.exists() and self._check_db(old_db):
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._adopt_db(old_db, db_path)
        manifest[str(self.docs_root)] = name
        _save_manifest(manifest)
        return db_path

    def _adopt_db(self, old_db: Path, new_db: Path) -> None:
        """Move the old db into the flat layout (copy if the move is locked)."""
        try:
            old_db.replace(new_db)
        except OSError:
            try:
                shutil.copy2(old_db, new_db)
            except OSError:
                return
        try:
            old_db.parent.rmdir()
        except OSError:
            pass

    # -- lifecycle ----------------------------------------------------------

    def _check_db(self, db_path: Path) -> bool:
        """True if this db file is valid for the current docs tree."""
        try:
            count = self._count_html()
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='file_count'").fetchone()
            return row is not None and int(row[0]) == count
        except Exception:
            return False

    def _is_valid(self) -> bool:
        return self.db_path.exists() and self._check_db(self.db_path)

    def _iter_html(self):
        """Yield every content HTML file path in a single pass over both dirs."""
        for d in CONTENT_DIRS:
            base = self.docs_root / d
            if not base.is_dir():
                continue
            for root, _dirs, files in os.walk(base):
                rp = Path(root)
                for f in files:
                    if f.endswith(".html"):
                        yield rp / f

    def _count_html(self) -> int:
        """Number of content HTML files (no per-file stat; cheap for validation)."""
        return sum(1 for _ in self._iter_html())

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def _build(self) -> None:
        t0 = time.time()
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # single pass over the tree: collect (rel, path, size) for every html file
            paths = list(self._iter_html())
            entries = []
            for p in paths:
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue  # unreadable entry; skip indexing it
                entries.append((p.relative_to(self.docs_root).as_posix(), p, sz))
            entries.sort(key=lambda e: e[0])
            files = [p for _rel, p, _sz in entries]
            count = len(paths)  # by-name count, matches _count_html for validation
            total_bytes = sum(sz for _rel, _p, sz in entries)
            if self.db_path.exists():
                self.db_path.unlink()
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA synchronous=OFF")
            conn.executescript(SCHEMA)
            batch: list[tuple] = []
            done = 0
            with ProcessPoolExecutor(max_workers=8) as pool:
                for row in pool.map(
                    _extract_worker,
                    ((self.docs_root, p) for p in files),
                    chunksize=32,
                ):
                    batch.append(row)
                    if len(batch) >= 2500:
                        conn.executemany(
                            "INSERT OR REPLACE INTO pages(path,kind,title,text) VALUES (?,?,?,?)",
                            batch,
                        )
                        done += len(batch)
                        self._log(f"  indexed {done}/{len(files)}")
                        batch = []
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO pages(path,kind,title,text) VALUES (?,?,?,?)",
                    batch,
                )
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("file_count", str(count)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("total_bytes", str(total_bytes)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("docs_root", str(self.docs_root)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("built_at", str(int(time.time()))))
            conn.commit()
            conn.close()
            self._log(f"Index built: {count} pages in {time.time() - t0:.1f}s -> {self.db_path}")
            self._ready.set()
        except Exception as e:
            self._error = e
            self._log(f"Index build FAILED: {e!r}")
            self._ready.set()

    def wait_ready(self) -> None:
        if not self._ready.wait(timeout=900):
            raise RuntimeError("Timed out building the docs index (>15 min)")
        if self._error is not None:
            raise RuntimeError("Docs index build failed") from self._error

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # -- queries ------------------------------------------------------------

    @staticmethod
    def _terms(query: str) -> list[str]:
        parts = re.split(r"\s+", query.lower().strip())
        return [re.sub(r"^[^\w]+|[^\w]+$", "", t) for t in parts if len(t) >= 2]

    @staticmethod
    def _like(v: str) -> str:
        return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _snippet(text: str, terms: list[str], before: int = 90, after: int = 220) -> str:
        low = text.lower()
        pos = -1
        for t in terms:
            p = low.find(t)
            if p != -1 and (pos == -1 or p < pos):
                pos = p
        if pos == -1:
            pos = 0
        start = max(0, pos - before)
        chunk = re.sub(r"\s+", " ", text[start : pos + after]).strip()
        return ("…" if start > 0 else "") + chunk + ("…" if pos + after < len(text) else "")

    def search(self, query: str, scope: str = "all", limit: int = 10) -> dict:
        """Full-text search. Every term must appear in title or body (AND)."""
        self.wait_ready()
        limit = max(1, min(int(limit), 25))
        terms = self._terms(query)
        if not terms:
            return {"query": query, "error": "No searchable terms in query", "results": []}
        kind = scope if scope in ("manual", "api") else None
        conds: list[str] = []
        params: list = []
        if kind:
            conds.append("kind = ?")
            params.append(kind)
        for t in terms:
            esc = self._like(t)
            conds.append("(title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            params += [f"%{esc}%", f"%{esc}%"]
        sql = "SELECT path, kind, title, text FROM pages WHERE " + " AND ".join(conds)
        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        results = []
        for path, k, title, text in rows:
            tl, ttx = title.lower(), text.lower()
            score = sum(40 * tl.count(t) + ttx.count(t) for t in terms)
            results.append(
                {"title": title, "path": path, "kind": k, "score": score, "snippet": self._snippet(text, terms)}
            )
        results.sort(key=lambda r: (-r["score"], r["path"]))
        return {"query": query, "scope": scope, "matched": len(results), "results": results[:limit]}

    def get_page(self, path: str, max_chars: int = 60000) -> dict:
        """Read one page as clean text. Works even before the index finishes."""
        max_chars = max(500, int(max_chars))
        p = Path(path)
        cand = p if p.is_absolute() else self.docs_root / p
        try:
            resolved = cand.resolve()
            resolved.relative_to(self.docs_root)
        except (ValueError, OSError):
            return {"error": f"Path is outside the docs root ({self.docs_root}): {path}"}
        if not resolved.is_file() or resolved.suffix.lower() != ".html":
            result = {"error": f"Page not found: {path}"}
            if self._ready.is_set():
                result["suggestions"] = self.search(resolved.name, scope="all", limit=5)["results"]
            return result
        title, text = extract_article(resolved, self.docs_root)
        rel = resolved.relative_to(self.docs_root).as_posix()
        return {
            "path": rel,
            "title": title,
            "chars": len(text),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
        }

    def list_sections(self, category: str | None = None) -> dict:
        """Browse the manual's chapter structure (ScriptReference is flat)."""
        self.wait_ready()
        if category is None:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT path, title FROM pages WHERE kind='manual'").fetchall()
                api_count = conn.execute("SELECT COUNT(*) FROM pages WHERE kind='api'").fetchone()[0]
            finally:
                conn.close()
            chapters: dict[str, int] = {}
            root: list[tuple[str, str]] = []
            for path, _title in rows:
                parts = path.split("/")
                if len(parts) == 2:
                    root.append((path, _title))
                else:
                    chapters[parts[1]] = chapters.get(parts[1], 0) + 1
            return {
                "chapters": [
                    {"name": n, "pages": c}
                    for n, c in sorted(chapters.items(), key=lambda x: (-x[1], x[0]))
                ],
                "root_articles": len(root),
                "root_sample": [
                    {"path": p, "title": t}
                    for p, t in sorted(root, key=lambda r: r[0])[:30]
                ],
                "script_reference_pages": api_count,
                "note": (
                    "Pass a chapter name (e.g. 'urp') to list its pages. "
                    "ScriptReference has no chapters — use find_api or search_unity_docs."
                ),
            }
        cat = category.strip().lstrip("./")
        if cat.startswith("Manual/"):
            cat = cat[len("Manual/"):]
        esc = self._like(cat)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT path, title FROM pages WHERE kind='manual' AND path LIKE ? ORDER BY path",
                (f"Manual/{esc}/%",),
            ).fetchall()
        finally:
            conn.close()
        pages = [{"path": p, "title": t} for p, t in rows[:300]]
        if not pages:
            return {
                "chapter": cat,
                "pages": [],
                "total": 0,
                "hint": "No pages under this chapter. Call list_sections() to see available chapters.",
            }
        return {"chapter": cat, "pages": pages, "total": len(rows)}

    def _api_row(self, cand: str):
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT title, text FROM pages WHERE path=? OR lower(path)=lower(?)",
                (cand, cand),
            ).fetchone()
        finally:
            conn.close()

    def _members(self, cls: str, cap: int = 300) -> list[dict]:
        esc = self._like(cls)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT path, title FROM pages WHERE path LIKE ? OR path LIKE ? ORDER BY path LIMIT ?",
                (f"ScriptReference/{esc}.%", f"ScriptReference/{esc}-%", cap),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for path, title in rows:
            short = path[len("ScriptReference/"):]
            if short.endswith(".html"):
                short = short[:-5]
            out.append({"member": short, "path": path, "title": title})
        return out

    def find_api(self, name: str, include_members: bool = True) -> dict:
        """Find a scripting API page by C# name (class, Class.member, ...)."""
        self.wait_ready()
        name = name.strip()
        if "." in name:
            cls, _, member = name.rpartition(".")
        else:
            cls, member = name, None
        cands = []
        if member:
            cands += [f"ScriptReference/{cls}.{member}.html", f"ScriptReference/{cls}-{member}.html"]
        else:
            cands.append(f"ScriptReference/{cls}.html")
        for cand in cands:
            row = self._api_row(cand)
            if row is not None:
                title, text = row
                page = {
                    "path": cand,
                    "title": title,
                    "chars": len(text),
                    "truncated": len(text) > 60000,
                    "text": text[:60000],
                }
                if not member and include_members:
                    page["members"] = self._members(cls)
                return page
        found = self.search(name, scope="api", limit=5)
        return {"error": f"No scripting API page for '{name}'.", "suggestions": found["results"]}
