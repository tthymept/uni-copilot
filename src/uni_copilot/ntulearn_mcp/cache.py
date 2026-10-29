"""
The content cache: course trees, page text and file text kept on your PC,
plus the search table. One SQLite file: .data/cache/content.sqlite.

In plain words:
  Tools read from here first and only go to NTULearn when a copy is too old,
  missing, or you ask for refresh=true:
  - a course's tree (its items) and a page's text count as fresh for 6 hours
    (CONTENT_FRESH_S);
  - a file's text is kept until NTULearn says the item changed (its
    `modified` time), so each file is downloaded and read once, not per
    question.
  Search (step 11) looks through the `search_text` table here, so it finds
  things without downloading anything.

What is never stored: file bytes (PDFs themselves), raw page HTML, and
signed links (the HTML contains them). An embedded file's link is always
taken from a page fetched just now. As a last safety net, _scrub() masks any
signed file link that reaches a save function anyway.

Deleting the file is always safe: everything in it comes back on the next
read. It's separate from the Phase 2 event store (.data/uni-copilot.sqlite),
which this never touches.

Like the event store: schema versions in PRAGMA user_version (MIGRATIONS,
only ever appended), WAL mode so readers don't block the writer, a 5-second
busy timeout because several programs may use the file at once (a terminal
session, the Telegram session, the indexer), and times as UTC text in one
fixed format. Every function opens its own short connection, so tools in
different threads never share one.

Tables:
  courses      one row per course site: when its tree was fetched
  items        the tree: one ContentItem (as JSON) per content item
  attachments  a file item's attachments (id, name, type), to download it
  pages        a page's text as light Markdown, its links, when fetched
  files        a file's text: which file, its type, the item's `modified`
  file_pages   the text of each page / slide of a file
  search_text  FTS5 full-text table: titles, page text, file pages (and,
               from step 11, announcements and messages)
  state        small key -> value notes (e.g. when the indexer last finished)
"""

import argparse
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uni_copilot.ntulearn_mcp.config import CONTENT_DB, CONTENT_FRESH_S
from uni_copilot.ntulearn_mcp.models import ContentItem, FileType, Link
from uni_copilot.ntulearn_mcp.parsers.content import is_signed_file_link

URL_RE = re.compile(r"https?://[^\s)\]>\"']+")

MIGRATIONS: list[str] = [
    # Version 1 (Phase 4 step 8).
    """
    CREATE TABLE courses (
        site_id         TEXT PRIMARY KEY,
        course_code     TEXT NOT NULL,
        tree_fetched_at TEXT NOT NULL
    );
    CREATE TABLE items (
        site_id     TEXT NOT NULL,
        content_id  TEXT NOT NULL,
        parent_id   TEXT,
        position    INTEGER NOT NULL,          -- order in the tree as NTULearn sent it
        modified_at TEXT,
        item_json   TEXT NOT NULL,             -- a ContentItem (no links); titles UNTRUSTED
        PRIMARY KEY (site_id, content_id)
    );
    CREATE INDEX items_parent ON items (site_id, parent_id);
    CREATE TABLE attachments (
        site_id       TEXT NOT NULL,
        content_id    TEXT NOT NULL,
        attachment_id TEXT NOT NULL,
        file_name     TEXT NOT NULL,           -- UNTRUSTED
        mime_type     TEXT,
        fetched_at    TEXT NOT NULL,
        PRIMARY KEY (site_id, content_id, attachment_id)
    );
    CREATE TABLE pages (
        site_id     TEXT NOT NULL,
        content_id  TEXT NOT NULL,
        course_code TEXT NOT NULL,
        title       TEXT NOT NULL,             -- UNTRUSTED
        text        TEXT NOT NULL,             -- UNTRUSTED light Markdown, signed links masked
        links_json  TEXT NOT NULL,             -- [[text, url], ...], never file links
        modified_at TEXT,
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY (site_id, content_id)
    );
    CREATE TABLE files (
        site_id      TEXT NOT NULL,
        content_id   TEXT NOT NULL,            -- the file item, or the page it's embedded in
        file_ref     TEXT NOT NULL,            -- attachment id or xid
        course_code  TEXT NOT NULL,
        file_name    TEXT NOT NULL,            -- UNTRUSTED
        file_type    TEXT NOT NULL,            -- pdf / pptx / ipynb / text
        modified_at  TEXT,                     -- the item's `modified` when this text was made
        extracted_at TEXT NOT NULL,
        total_pages  INTEGER NOT NULL,
        empty_pages  INTEGER NOT NULL,
        PRIMARY KEY (site_id, content_id, file_ref)
    );
    CREATE TABLE file_pages (
        site_id    TEXT NOT NULL,
        content_id TEXT NOT NULL,
        file_ref   TEXT NOT NULL,
        number     INTEGER NOT NULL,           -- page / slide number, from 1
        text       TEXT NOT NULL,              -- UNTRUSTED
        PRIMARY KEY (site_id, content_id, file_ref, number)
    );
    CREATE VIRTUAL TABLE search_text USING fts5(
        title, body,
        source UNINDEXED, course_code UNINDEXED, site_id UNINDEXED, content_id UNINDEXED,
        file_ref UNINDEXED, page UNINDEXED, event_id UNINDEXED,
        tokenize = 'porter unicode61'
    );
    CREATE TABLE state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


# ---------- Times ----------

# A UTC datetime -> fixed-format text ("2026-10-30T02:00:00.000000+00:00"),
# the same format as the event store. None stays None.
def time_to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


# The reverse of time_to_db.
def time_from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# True if something fetched at `fetched_at` still counts as fresh at `now`
# (younger than CONTENT_FRESH_S, 6 hours).
def is_fresh(fetched_at: datetime | None, now: datetime, max_age_s: int = CONTENT_FRESH_S) -> bool:
    return fetched_at is not None and now - fetched_at < timedelta(seconds=max_age_s)


# ---------- The safety net ----------

# Masks every signed file link in a text before it's saved (normally there
# are none: parse_html and parsers/content.page_text already hide them).
def _scrub(text: str) -> str:
    return URL_RE.sub(lambda m: "[file link hidden]" if is_signed_file_link(m.group(0)) else m.group(0), text)


# ---------- What reads return ----------

# A cached course tree: its items, and when it was fetched from NTULearn.
@dataclass(frozen=True)
class CachedTree:
    course_code: str
    items: list[ContentItem]
    fetched_at: datetime


# A cached page: its Markdown text and links, and when it was fetched.
@dataclass(frozen=True)
class CachedPage:
    course_code: str
    title: str
    text: str
    links: list[Link]
    modified_at: datetime | None
    fetched_at: datetime


# A file item's attachment: what's needed to download it (no link).
@dataclass(frozen=True)
class CachedAttachment:
    attachment_id: str
    file_name: str
    mime_type: str | None


# A cached file's text: every page, and when and from which version it was made.
@dataclass(frozen=True)
class CachedFile:
    course_code: str
    file_name: str
    file_type: FileType
    pages: list[str]
    empty_pages: int
    modified_at: datetime | None
    extracted_at: datetime


# Counts for search's "how much is indexed" (SearchCoverage, step 11).
@dataclass(frozen=True)
class CacheStats:
    courses: int
    items: int
    pages: int
    files: int
    file_pages: int
    search_rows: int


# ---------- The cache ----------

class ContentCache:
    # Remembers where the file is; creates the folder and the tables on first
    # use (`path` can be a temporary file in tests).
    def __init__(self, path: Path | str = CONTENT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            self._migrate(conn)

    # Opens a short connection for one piece of work and closes it afterwards.
    # Rows come back as sqlite3.Row (read by column name).
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")   # wait up to 5 s if another program is writing
        conn.execute("PRAGMA journal_mode = WAL")    # readers don't block the writer
        try:
            yield conn
        finally:
            conn.close()

    # Runs every migration the file hasn't had yet (see MIGRATIONS).
    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")

    # ----- Trees -----

    # Replaces one course site's tree with a freshly fetched one, in one
    # transaction (all or nothing). Items that disappeared from NTULearn lose
    # their cached page and file text too, so search never finds removed things.
    # Each item's title (and description) also goes into the search table.
    def save_tree(self, site_id: str, course_code: str, items: list[ContentItem], fetched_at: datetime) -> None:
        keep = {item.id for item in items}
        with self._connect() as conn, conn:
            gone = [r["content_id"] for r in conn.execute("SELECT content_id FROM items WHERE site_id = ?", (site_id,))
                    if r["content_id"] not in keep]
            for content_id in gone:
                self._forget_item(conn, site_id, content_id)
            conn.execute("DELETE FROM items WHERE site_id = ?", (site_id,))
            conn.execute("DELETE FROM search_text WHERE site_id = ? AND source = 'title'", (site_id,))
            for position, item in enumerate(items):
                item_json = _scrub(item.model_dump_json(exclude={"url"}))
                conn.execute(
                    "INSERT INTO items (site_id, content_id, parent_id, position, modified_at, item_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (site_id, item.id, item.parent_id, position, time_to_db(item.modified_at), item_json),
                )
                conn.execute(
                    "INSERT INTO search_text (title, body, source, course_code, site_id, content_id) "
                    "VALUES (?, ?, 'title', ?, ?, ?)",
                    (item.title, _scrub(item.description or ""), course_code, site_id, item.id),
                )
            conn.execute(
                "INSERT INTO courses (site_id, course_code, tree_fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT (site_id) DO UPDATE SET course_code = excluded.course_code, "
                "tree_fetched_at = excluded.tree_fetched_at",
                (site_id, course_code, time_to_db(fetched_at)),
            )

    # One course site's cached tree, in NTULearn's order, or None if it was
    # never fetched. Freshness is the caller's choice (is_fresh).
    def load_tree(self, site_id: str) -> CachedTree | None:
        with self._connect() as conn:
            course = conn.execute("SELECT * FROM courses WHERE site_id = ?", (site_id,)).fetchone()
            if course is None:
                return None
            rows = conn.execute("SELECT item_json FROM items WHERE site_id = ? ORDER BY position", (site_id,))
            items = [ContentItem.model_validate_json(r["item_json"]) for r in rows]
        return CachedTree(course["course_code"], items, time_from_db(course["tree_fetched_at"]))

    # The cached `modified` time of one item, or None (used to see whether a
    # file's cached text is still from the current version).
    def item_modified(self, site_id: str, content_id: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute("SELECT modified_at FROM items WHERE site_id = ? AND content_id = ?",
                               (site_id, content_id)).fetchone()
        return time_from_db(row["modified_at"]) if row else None

    # ----- Attachments (file items) -----

    # Replaces a file item's list of attachments (ids, names, types; no links).
    def save_attachments(self, site_id: str, content_id: str, attachments: list[CachedAttachment],
                         fetched_at: datetime) -> None:
        with self._connect() as conn, conn:
            conn.execute("DELETE FROM attachments WHERE site_id = ? AND content_id = ?", (site_id, content_id))
            conn.executemany(
                "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?)",
                [(site_id, content_id, a.attachment_id, a.file_name, a.mime_type, time_to_db(fetched_at))
                 for a in attachments],
            )

    # A file item's cached attachments and when they were fetched, or None.
    def load_attachments(self, site_id: str, content_id: str) -> tuple[list[CachedAttachment], datetime] | None:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM attachments WHERE site_id = ? AND content_id = ?",
                                (site_id, content_id)).fetchall()
        if not rows:
            return None
        return ([CachedAttachment(r["attachment_id"], r["file_name"], r["mime_type"]) for r in rows],
                time_from_db(rows[0]["fetched_at"]))

    # ----- Pages -----

    # Saves a page's Markdown text and links (signed links masked once more),
    # and puts its text in the search table in place of any older copy.
    def save_page(self, site_id: str, course_code: str, content_id: str, title: str, text: str,
                  links: list[Link], modified_at: datetime | None, fetched_at: datetime) -> None:
        text = _scrub(text)
        safe_links = [[link.text, link.url] for link in links if not is_signed_file_link(link.url)]
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, content_id, course_code, title, text, json.dumps(safe_links),
                 time_to_db(modified_at), time_to_db(fetched_at)),
            )
            conn.execute("DELETE FROM search_text WHERE site_id = ? AND content_id = ? AND source = 'page'",
                         (site_id, content_id))
            conn.execute(
                "INSERT INTO search_text (title, body, source, course_code, site_id, content_id) "
                "VALUES (?, ?, 'page', ?, ?, ?)",
                (title, text, course_code, site_id, content_id),
            )

    # A cached page, or None. Freshness is the caller's choice (is_fresh).
    def load_page(self, site_id: str, content_id: str) -> CachedPage | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM pages WHERE site_id = ? AND content_id = ?",
                             (site_id, content_id)).fetchone()
        if r is None:
            return None
        links = [Link(text=t, url=u) for t, u in json.loads(r["links_json"])]
        return CachedPage(r["course_code"], r["title"], r["text"], links,
                          time_from_db(r["modified_at"]), time_from_db(r["fetched_at"]))

    # ----- File text -----

    # Saves a file's text (every page, signed links masked once more) with the
    # item's `modified` time it was made from, replacing an older version, and
    # puts each page in the search table (one row per page, so a hit can say
    # "slide 14").
    def save_file_text(self, site_id: str, course_code: str, content_id: str, file_ref: str, file_name: str,
                       file_type: FileType, pages: list[str], modified_at: datetime | None,
                       extracted_at: datetime) -> None:
        pages = [_scrub(p) for p in pages]
        empty = sum(1 for p in pages if not p.strip())
        with self._connect() as conn, conn:
            self._forget_file(conn, site_id, content_id, file_ref)
            conn.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, content_id, file_ref, course_code, file_name, file_type,
                 time_to_db(modified_at), time_to_db(extracted_at), len(pages), empty),
            )
            conn.executemany(
                "INSERT INTO file_pages VALUES (?, ?, ?, ?, ?)",
                [(site_id, content_id, file_ref, n, text) for n, text in enumerate(pages, 1)],
            )
            conn.executemany(
                "INSERT INTO search_text (title, body, source, course_code, site_id, content_id, file_ref, page) "
                "VALUES (?, ?, 'file', ?, ?, ?, ?, ?)",
                [(file_name, text, course_code, site_id, content_id, file_ref, n)
                 for n, text in enumerate(pages, 1) if text.strip()],
            )

    # A file's cached text, but only if it was made from the item's current
    # version: pass the `modified` time NTULearn shows now. A different time
    # (the prof replaced the file) gives None, so the caller reads it again.
    def load_file_text(self, site_id: str, content_id: str, file_ref: str,
                       current_modified: datetime | None) -> CachedFile | None:
        with self._connect() as conn:
            f = conn.execute("SELECT * FROM files WHERE site_id = ? AND content_id = ? AND file_ref = ?",
                             (site_id, content_id, file_ref)).fetchone()
            if f is None or time_from_db(f["modified_at"]) != current_modified:
                return None
            pages = [r["text"] for r in conn.execute(
                "SELECT text FROM file_pages WHERE site_id = ? AND content_id = ? AND file_ref = ? ORDER BY number",
                (site_id, content_id, file_ref))]
        return CachedFile(f["course_code"], f["file_name"], f["file_type"], pages, f["empty_pages"],
                          time_from_db(f["modified_at"]), time_from_db(f["extracted_at"]))

    # Drops the cached text of an item's files that it no longer has (e.g. a
    # page whose embedded slides were replaced: the new upload gets a new
    # xid, and the old text mustn't stay findable). `keep` = the file_refs
    # the item has now. Called by the contents client after every tree fetch.
    def prune_files(self, site_id: str, content_id: str, keep: set[str]) -> int:
        with self._connect() as conn, conn:
            old = [r["file_ref"] for r in conn.execute(
                "SELECT file_ref FROM files WHERE site_id = ? AND content_id = ?", (site_id, content_id))]
            gone = [ref for ref in old if ref not in keep]
            for ref in gone:
                self._forget_file(conn, site_id, content_id, ref)
        return len(gone)

    # ----- Forgetting -----

    # Removes one file's text everywhere (files, file_pages, search_text).
    @staticmethod
    def _forget_file(conn: sqlite3.Connection, site_id: str, content_id: str, file_ref: str) -> None:
        for table in ("files", "file_pages"):
            conn.execute(f"DELETE FROM {table} WHERE site_id = ? AND content_id = ? AND file_ref = ?",
                         (site_id, content_id, file_ref))
        conn.execute("DELETE FROM search_text WHERE site_id = ? AND content_id = ? AND file_ref = ?",
                     (site_id, content_id, file_ref))

    # Removes everything cached about one item (it disappeared from NTULearn).
    @staticmethod
    def _forget_item(conn: sqlite3.Connection, site_id: str, content_id: str) -> None:
        for table in ("pages", "attachments", "files", "file_pages", "search_text"):
            conn.execute(f"DELETE FROM {table} WHERE site_id = ? AND content_id = ?", (site_id, content_id))

    # ----- Notes and counts -----

    # A small note, e.g. "indexer_finished_at". None if not set.
    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # Sets a small note.
    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn, conn:
            conn.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                         (key, value))

    # How much is cached: for search's coverage line and the self-tests.
    def stats(self) -> CacheStats:
        with self._connect() as conn:
            n = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("courses", "items", "pages", "files", "file_pages", "search_text")}
        return CacheStats(courses=n["courses"], items=n["items"], pages=n["pages"], files=n["files"],
                          file_pages=n["file_pages"], search_rows=n["search_text"])


# ---------- Self-test ----------

# Offline: uv run python -m uni_copilot.ntulearn_mcp.cache [--real]
# Without --real: a temporary file with made-up content, every save and read,
# freshness, "file changed -> read again", removal, the signed-link net, and
# one FTS5 search. With --real: only the real cache file's version and counts.
def _main() -> None:
    import tempfile

    from uni_copilot.ntulearn_mcp.models import Release

    ap = argparse.ArgumentParser(description="Self-test of the content cache.")
    ap.add_argument("--real", action="store_true", help="show the real cache file's version and counts")
    if ap.parse_args().real:
        cache = ContentCache()
        with cache._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        print(f"{cache.path}: schema version {version}, {cache.stats()}")
        return

    now = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
    v1 = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        cache = ContentCache(Path(tmp) / "content.sqlite")
        page = ContentItem(id="_10_1", course_code="XX1000", course_site_id="_1_1", title="Week 10: Graphs",
                           kind="page", release=Release(state="open"), modified_at=v1)
        quiz = ContentItem(id="_13_1", course_code="XX1000", course_site_id="_1_1", title="Quiz 10",
                           kind="assessment", release=Release(state="conditional"), column_id="_99_1")
        cache.save_tree("_1_1", "XX1000", [page, quiz], now)
        tree = cache.load_tree("_1_1")
        print(f"1) tree: {[i.title for i in tree.items]}, fetched {tree.fetched_at:%H:%M}, "
              f"fresh 1 h later: {is_fresh(tree.fetched_at, now + timedelta(hours=1))}, "
              f"7 h later: {is_fresh(tree.fetched_at, now + timedelta(hours=7))}")

        signed = "https://ntulearn.ntu.edu.sg/bbcswebdav/pid-1/xid-5_1?sig=SECRET"
        cache.save_page("_1_1", "XX1000", "_10_1", "Week 10: Graphs",
                        f"Read Dijkstra's notes. Pasted by hand: {signed}",
                        [Link(text="a site", url="https://example.org"), Link(text="raw", url=signed)], v1, now)
        p = cache.load_page("_1_1", "_10_1")
        print(f"2) page: {p.text!r}, links {[(link.text, link.url) for link in p.links]}")

        cache.save_file_text("_1_1", "XX1000", "_10_1", "xid-555_1", "week10.pdf", "pdf",
                             ["Shortest paths", "", "Dijkstra uses a priority queue"], v1, now)
        same = cache.load_file_text("_1_1", "_10_1", "xid-555_1", v1)
        changed = cache.load_file_text("_1_1", "_10_1", "xid-555_1", v1 + timedelta(days=3))
        print(f"3) file text, same version: {len(same.pages)} pages ({same.empty_pages} empty); "
              f"after the prof replaced it: {changed}")

        with cache._connect() as conn:
            hits = conn.execute(
                "SELECT source, title, page, snippet(search_text, 1, '[', ']', '...', 6) AS snip "
                "FROM search_text WHERE search_text MATCH ? ORDER BY bm25(search_text)", ("dijkstra",)).fetchall()
        print("4) FTS5 search 'dijkstra':", [(h["source"], h["title"], h["page"], h["snip"]) for h in hits])

        cache.save_tree("_1_1", "XX1000", [quiz], now)   # the page disappeared from NTULearn
        print(f"5) after the page was removed: page {cache.load_page('_1_1', '_10_1')}, "
              f"file {cache.load_file_text('_1_1', '_10_1', 'xid-555_1', v1)}, {cache.stats()}")

        with cache._connect() as conn:
            everything = " ".join(str(tuple(r)) for t in ("items", "pages", "file_pages", "search_text")
                                  for r in conn.execute(f"SELECT * FROM {t}"))
        print(f"6) 'SECRET' anywhere in the file: {'SECRET' in everything}")


if __name__ == "__main__":
    _main()
