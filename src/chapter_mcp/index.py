from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typing_extensions import NotRequired, TypedDict

from chapter_mcp.chunks import Chunk, is_probably_text, parse_file
from chapter_mcp.ignore_rules import IgnoreMatcher


logger = logging.getLogger(__name__)

CategoryPath = Path | str | tuple[str, Path | str]
ChapterColumnField = str
DEFAULT_CHAPTER_COLUMN_FIELDS: tuple[ChapterColumnField, ...] = ("name", "start_line", "end_line")
ALLOWED_CHAPTER_COLUMN_FIELDS: tuple[ChapterColumnField, ...] = (
    "name",
    "start_line",
    "end_line",
)
FORMAL_CHUNK_TYPES: tuple[str, ...] = (
    "javascript_class",
    "javascript_function",
    "javascript_type",
    "python_class",
    "python_function",
    "rust_function",
    "rust_impl",
    "rust_type",
    "toml_section",
    "yaml_section",
)


class IndexStatsDict(TypedDict):
    scanned: int
    indexed: int
    skipped: int
    deleted: int


class IndexingSummaryDict(TypedDict):
    chapters_loaded: int
    indexing_time_seconds: float


class ChapterRecord(TypedDict):
    name: str
    start_line: int
    end_line: int
    content: NotRequired[str]
    content_offset: NotRequired[int]
    content_total_lines: NotRequired[int]
    content_truncated: NotRequired[bool]
    snippet: NotRequired[str]
    score: NotRequired[float]


class FileChapterRecord(TypedDict):
    file: str
    chapters: list[ChapterRecord]


class CategoryChapterRecord(TypedDict):
    category: str
    files: list[FileChapterRecord]


class FileRecord(TypedDict):
    file: str
    category: str
    mtime_ns: int
    mtime: str | None
    byte_count: int
    line_count: int
    chunk_count: int
    indexed_at: str | None


class SearchResponse(TypedDict):
    count: int
    results: list[CategoryChapterRecord]


class ChapterListResponse(TypedDict):
    count: int
    offset: int
    results: list[CategoryChapterRecord]


class ReadChapterResponse(TypedDict):
    count: int
    results: list[CategoryChapterRecord]


class ChapterColumnsResponse(TypedDict):
    count: int
    offset: int
    results: list["CategoryChapterColumnsRecord"]
    truncated: bool


class FileChapterColumnsRecord(TypedDict):
    file: str
    columns: list[str]
    rows: list[list[str | int]]


class CategoryChapterColumnsRecord(TypedDict):
    category: str
    files: list[FileChapterColumnsRecord]


class FileListResponse(TypedDict):
    count: int
    limit: int
    offset: int
    files: list[FileRecord]


class CategoryStats(TypedDict):
    paths: list[str]
    path: NotRequired[str]
    exists: bool
    file_count: int
    chunk_count: int
    byte_count: int
    line_count: int
    latest_mtime_ns: int | None
    latest_mtime: str | None
    latest_indexed_at: str | None


class StatsResponse(TypedDict):
    root: str
    db_path: str
    watching: bool
    indexing: bool
    startup_index_running: bool
    last_reindex: IndexStatsDict | None
    last_indexing_summary: IndexingSummaryDict | None
    last_reindex_started_at: str | None
    last_reindex_finished_at: str | None
    last_background_error: str | None
    category_count: int
    file_count: int
    chunk_count: int
    byte_count: int
    line_count: int
    categories: dict[str, CategoryStats]


@dataclass(frozen=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0

    def as_dict(self) -> IndexStatsDict:
        """Return the counters as a small result dict for CLI or tool responses."""
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }

    def plus(self, *, scanned: int = 0, indexed: int = 0, skipped: int = 0, deleted: int = 0) -> "IndexStats":
        """Return a new stats object with the provided counters added in."""
        return IndexStats(
            scanned=self.scanned + scanned,
            indexed=self.indexed + indexed,
            skipped=self.skipped + skipped,
            deleted=self.deleted + deleted,
        )


@dataclass(frozen=True)
class IndexingSummary:
    chapters_loaded: int = 0
    indexing_time_seconds: float = 0.0

    def as_dict(self) -> IndexingSummaryDict:
        """Return a compact summary dict with chapter count and elapsed indexing time."""
        return {
            "chapters_loaded": self.chapters_loaded,
            "indexing_time_seconds": round(self.indexing_time_seconds, 6),
        }

    def plus(self, *, chapters_loaded: int = 0) -> "IndexingSummary":
        """Return a new summary with additional loaded-chapter counts applied."""
        return IndexingSummary(
            chapters_loaded=self.chapters_loaded + chapters_loaded,
            indexing_time_seconds=self.indexing_time_seconds,
        )


@dataclass(frozen=True)
class PreparedFile:
    rel_path: str
    category: str
    mtime_ns: int
    size: int
    line_count: int
    chapters: list[Chunk]


class ChapterIndex:
    def __init__(
        self,
        root: Path,
        db_path: Path,
        category_paths: Sequence[CategoryPath] | None = None,
    ) -> None:
        """Create an index rooted at ``root`` and backed by the SQLite database at ``db_path``."""
        self.root = root.expanduser().resolve()
        db_path = db_path.expanduser()
        self.db_path = db_path if db_path.is_absolute() else self.root / db_path
        self._gitignore_matcher = IgnoreMatcher(self.root, ".gitignore")
        self._aiignore_matcher = IgnoreMatcher(self.root, ".aiignore")
        self.category_dirs = self._resolve_category_dirs(category_paths)
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._startup_thread: threading.Thread | None = None
        self._indexing = False
        self._last_reindex_stats: IndexStats | None = None
        self._last_indexing_summary: IndexingSummary | None = None
        self._last_reindex_started_at: float | None = None
        self._last_reindex_finished_at: float | None = None
        self._last_background_error: str | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        """Stop background work and close the underlying SQLite connection."""
        self.stop_watcher()
        self.wait_for_startup(timeout=5)
        with self._lock:
            self.db.close()

    def start_background_reindex(self) -> None:
        """Kick off startup indexing on a background thread if it is not already running."""
        if self._startup_thread is not None and self._startup_thread.is_alive():
            return
        self._startup_thread = threading.Thread(
            target=self._background_reindex,
            name="chapter-mcp-startup-index",
            daemon=True,
        )
        self._startup_thread.start()

    def wait_for_startup(self, timeout: float | None = None) -> None:
        """Wait for any background startup indexing thread to finish."""
        thread = self._startup_thread
        if thread is not None:
            thread.join(timeout=timeout)

    def start_watcher(self, interval: float = 1.0) -> None:
        """Start the background watcher that periodically reindexes changed files."""
        if interval <= 0:
            raise ValueError("watch interval must be greater than zero")
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(interval,),
            name="chapter-mcp-index-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def stop_watcher(self) -> None:
        """Stop the background watcher thread if one is running."""
        thread = self._watch_thread
        if thread is None:
            return
        self._watch_stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            return
        self._watch_thread = None

    def reindex(self, category: str | None = None) -> IndexStats:
        """Scan configured folders and update changed files, returning scan/index counters."""
        categories = self._selected_categories(category)
        stats = IndexStats()
        indexing_summary = IndexingSummary()
        seen_paths: set[str] = set()
        with self._lock:
            self._indexing = True
            self._last_reindex_started_at = time.time()
            self._last_reindex_finished_at = None
            self._last_background_error = None

        try:
            for selected in categories:
                for path, category_root in self._iter_category_files(selected):
                    rel_path = self._stored_file_path(path)
                    seen_paths.add(rel_path)
                    stats = stats.plus(scanned=1)
                    with self._lock:
                        unchanged = self._file_unchanged(rel_path, path)
                    if unchanged:
                        stats = stats.plus(skipped=1)
                        continue

                    prepared = self._prepare_file(path, rel_path, selected)
                    file_record = self._read_file_record(path, rel_path, selected)
                    with self._lock:
                        self._write_file_record(file_record)
                        self._write_prepared_chapters(prepared)
                        self.db.commit()
                    indexing_summary = indexing_summary.plus(chapters_loaded=len(prepared.chapters))
                    stats = stats.plus(indexed=1)

            with self._lock:
                stats = stats.plus(deleted=self._delete_removed_files(categories, seen_paths))
                self.db.commit()
                self._last_reindex_stats = stats
                self._last_reindex_finished_at = time.time()
                self._last_indexing_summary = IndexingSummary(
                    chapters_loaded=indexing_summary.chapters_loaded,
                    indexing_time_seconds=max(
                        0.0,
                        (self._last_reindex_finished_at or time.time()) - (self._last_reindex_started_at or time.time()),
                    ),
                )
                return stats
        finally:
            with self._lock:
                self._indexing = False

    def has_changes(self, category: str | None = None) -> bool:
        """Return whether the live filesystem differs from what is currently indexed."""
        live_snapshot = self._filesystem_snapshot(category)
        with self._lock:
            indexed_snapshot = self._indexed_snapshot(category)
        return live_snapshot != indexed_snapshot

    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
        offset: int = 0,
        include_snippet: bool = False,
        exact_code_matches: bool = False,
    ) -> SearchResponse:
        """Search chapter names and content and return lightweight matching chapter references."""
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        results = self._search_fts(
            query=query,
            category=category,
            limit=limit,
            offset=offset,
            exact_code_matches=exact_code_matches,
        )
        if include_snippet:
            self._add_search_snippets(results, query=query)
        return results

    def search_chapter(self, query: str, category: str | None = None, limit: int = 5, offset: int = 0) -> SearchResponse:
        """Search chapter names only and return lightweight matching chapter references."""
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        return self._search_chapter_fts(query=query, category=category, limit=limit, offset=offset)

    def read_search(
        self,
        query: str,
        category: str | None = None,
        offset: int = 0,
        exact_code_matches: bool = False,
    ) -> ReadChapterResponse:
        """Return the full chapter content for the ranked search match at the given offset."""
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        matches = self._search_fts(
            query=query,
            category=category,
            limit=1,
            offset=offset,
            exact_code_matches=exact_code_matches,
        )
        if not matches["results"]:
            return {"count": matches["count"], "results": []}

        match = matches["results"][0]
        category = match["category"]
        file_record = match["files"][0]
        chapter_record = file_record["chapters"][0]
        chapter = self.read_chapter(chapter_record["name"], file=file_record["file"], category=category, count=1, offset=0)
        return {
            "count": matches["count"],
            "results": chapter["results"],
        }

    def read_chapter(
        self,
        chapter_name: str,
        file: str | None = None,
        category: str | None = None,
        count: int = 5,
        offset: int = 0,
        content_offset: int = 0,
        content_limit: int | None = None,
    ) -> ReadChapterResponse:
        """Return chapter records for an exact chapter name, optionally narrowed by file or category."""
        count = max(1, min(count, 100))
        offset = max(0, offset)
        content_offset = max(0, content_offset)
        if content_limit is not None and content_limit < 1:
            raise ValueError("content_limit must be greater than zero")
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        clauses = ["chunk_name = ?"]
        params: list[Any] = [chapter_name]
        if file is not None:
            clauses.append("file_path = ?")
            params.append(file)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = " and ".join(clauses)
        with self._lock:
            count_row = self.db.execute(f"select count(*) as count from chunks where {where}", params).fetchone()
            rows = self.db.execute(
                f"""
                select file_path, category, chunk_type, chunk_name, content, start_line, end_line
                from chunks
                where {where}
                order by category, file_path, start_line
                limit ? offset ?
                """,
                [*params, count, offset],
            ).fetchall()
            return {
                "count": count_row["count"] if count_row is not None else 0,
                "results": _group_chapter_rows(
                    rows,
                    include_content=True,
                    content_offset=content_offset,
                    content_limit=content_limit,
                ),
            }

    def list_chapters(
        self,
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> ChapterListResponse:
        """List indexed chapters with names, files, and line ranges but without full content."""
        count = max(1, min(count, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        clauses: list[str] = []
        params: list[Any] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if file is not None:
            clauses.append("file_path = ?")
            params.append(file)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._lock:
            count_row = self.db.execute(f"select count(*) as count from chunks {where}", params).fetchone()
            rows = self.db.execute(
                f"""
                select file_path, category, chunk_type, chunk_name, start_line, end_line
                from chunks
                {where}
                order by category, file_path, start_line
                limit ? offset ?
                """,
                [*params, count, offset],
            ).fetchall()
            return {
                "count": count_row["count"] if count_row is not None else 0,
                "offset": offset,
                "results": _group_chapter_rows(rows, include_content=False),
            }

    def list_chapters_as_columns(
        self,
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
        fields: Sequence[str] | None = None,
    ) -> ChapterColumnsResponse:
        """List indexed chapters as compact column-oriented rows."""
        count = max(1, min(count, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")

        selected_fields = _normalize_chapter_column_fields(fields)
        clauses: list[str] = []
        params: list[Any] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if file is not None:
            clauses.append("file_path = ?")
            params.append(file)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        requested_columns = ", ".join(f"{_chapter_field_sql(field)} as {field}" for field in selected_fields)
        select_columns = ", ".join(
            column
            for column in (
                "category",
                "file_path",
                requested_columns,
            )
            if column
        )

        with self._lock:
            count_row = self.db.execute(f"select count(*) as count from chunks {where}", params).fetchone()
            rows = self.db.execute(
                f"""
                select {select_columns}
                from chunks
                {where}
                order by category, file_path, start_line
                limit ? offset ?
                """,
                [*params, count, offset],
            ).fetchall()
            total_count = count_row["count"] if count_row is not None else 0
            return {
                "count": total_count,
                "offset": offset,
                "results": _group_chapter_column_rows(rows, fields=selected_fields),
                "truncated": offset + len(rows) < total_count,
            }

    def list_files(self, category: str | None = None, limit: int = 100, offset: int = 0) -> FileListResponse:
        """List indexed files and return metadata such as category, size, and chunk counts."""
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self._lock:
            categories = self._selected_categories(category)
            if not categories:
                return {"count": 0, "limit": limit, "offset": offset, "files": []}
            placeholders = ",".join("?" for _ in categories)
            count_row = self.db.execute(
                f"select count(*) as count from files where category in ({placeholders})",
                list(categories),
            ).fetchone()
            rows = self.db.execute(
                f"""
                select
                    files.path,
                    files.category,
                    files.mtime_ns,
                    files.size,
                    files.line_count,
                    files.indexed_at,
                    count(chunks.id) as chunk_count
                from files
                left join chunks on chunks.file_path = files.path
                where files.category in ({placeholders})
                group by files.path
                order by files.category, files.path
                limit ? offset ?
                """,
                [*categories, limit, offset],
            ).fetchall()
            return {
                "count": count_row["count"] if count_row is not None else 0,
                "limit": limit,
                "offset": offset,
                "files": [_file_row_to_result(row) for row in rows],
            }

    def stats(self) -> StatsResponse:
        """Return overall index counts plus per-category status and recent indexing metadata."""
        with self._lock:
            categories: dict[str, CategoryStats] = {}
            total_files = 0
            total_chunks = 0
            total_bytes = 0
            total_lines = 0
            for category, directories in self.category_dirs.items():
                exists = any(directory.exists() for directory in directories)
                paths = [directory.as_posix() for directory in directories]
                file_row = self.db.execute(
                    """
                    select
                        count(*) as file_count,
                        coalesce(sum(size), 0) as byte_count,
                        coalesce(sum(line_count), 0) as line_count,
                        max(files.mtime_ns) as latest_mtime_ns,
                        max(files.indexed_at) as latest_indexed_at
                    from files
                    where files.category = ?
                    """,
                    [category],
                ).fetchone()
                chunk_row = self.db.execute(
                    "select count(*) as chunk_count from chunks where category = ?",
                    [category],
                ).fetchone()
                file_count = file_row["file_count"] if file_row is not None else 0
                chunk_count = chunk_row["chunk_count"] if chunk_row is not None else 0
                byte_count = file_row["byte_count"] if file_row is not None else 0
                line_count = file_row["line_count"] if file_row is not None else 0
                total_files += file_count
                total_chunks += chunk_count
                total_bytes += byte_count
                total_lines += line_count
                category_stats: CategoryStats = {
                    "paths": paths,
                    "exists": exists,
                    "file_count": file_count,
                    "chunk_count": chunk_count,
                    "byte_count": byte_count,
                    "line_count": line_count,
                    "latest_mtime_ns": file_row["latest_mtime_ns"] if file_row is not None else None,
                    "latest_mtime": _format_mtime_ns(file_row["latest_mtime_ns"]) if file_row is not None else None,
                    "latest_indexed_at": _format_timestamp(file_row["latest_indexed_at"]) if file_row is not None else None,
                }
                if len(paths) == 1:
                    category_stats["path"] = paths[0]
                categories[category] = category_stats
            return {
                "root": self.root.as_posix(),
                "db_path": self.db_path.as_posix(),
                "watching": self._watch_thread is not None and self._watch_thread.is_alive(),
                "indexing": self._indexing,
                "startup_index_running": self._startup_thread is not None and self._startup_thread.is_alive(),
                "last_reindex": self._last_reindex_stats.as_dict() if self._last_reindex_stats is not None else None,
                "last_indexing_summary": (
                    self._last_indexing_summary.as_dict() if self._last_indexing_summary is not None else None
                ),
                "last_reindex_started_at": _format_timestamp(self._last_reindex_started_at),
                "last_reindex_finished_at": _format_timestamp(self._last_reindex_finished_at),
                "last_background_error": self._last_background_error,
                "category_count": len(categories),
                "file_count": total_files,
                "chunk_count": total_chunks,
                "byte_count": total_bytes,
                "line_count": total_lines,
                "categories": categories,
            }

    def _ensure_schema(self) -> None:
        with self._lock:
            self.db.executescript(
                """
                create table if not exists files (
                    path text primary key,
                    category text not null,
                    mtime_ns integer not null,
                    size integer not null,
                    line_count integer not null default 0,
                    indexed_at real not null
                );
                create table if not exists chunks (
                    id integer primary key,
                    file_path text not null,
                    category text not null,
                    chunk_type text not null,
                    chunk_name text not null,
                    content text not null,
                    start_line integer not null,
                    end_line integer not null,
                    foreign key(file_path) references files(path) on delete cascade
                );
                create virtual table if not exists chunks_fts using fts5(
                    chunk_name,
                    content,
                    file_path unindexed,
                    category unindexed,
                    content='chunks',
                    content_rowid='id'
                );
                create index if not exists idx_chunks_file_name on chunks(file_path, chunk_name);
                create index if not exists idx_chunks_category on chunks(category);
                """
            )
            self._ensure_file_columns()
            self.db.commit()

    def _ensure_file_columns(self) -> None:
        columns = {row["name"] for row in self.db.execute("pragma table_info(files)").fetchall()}
        if "line_count" not in columns:
            self.db.execute("alter table files add column line_count integer not null default 0")

    def _background_reindex(self) -> None:
        try:
            self.reindex()
        except Exception as error:  # pragma: no cover
            with self._lock:
                self._last_background_error = repr(error)

    def _search_fts(
        self,
        query: str,
        category: str | None,
        limit: int,
        offset: int,
        exact_code_matches: bool = False,
    ) -> SearchResponse:
        return self._search_fts_columns(
            query=query,
            category=category,
            limit=limit,
            offset=offset,
            match_column=None,
            exact_code_matches=exact_code_matches,
        )

    def _search_chapter_fts(self, query: str, category: str | None, limit: int, offset: int) -> SearchResponse:
        return self._search_fts_columns(
            query=query,
            category=category,
            limit=limit,
            offset=offset,
            match_column="chunk_name",
        )

    def _search_fts_columns(
        self,
        *,
        query: str,
        category: str | None,
        limit: int,
        offset: int,
        match_column: str | None,
        exact_code_matches: bool = False,
    ) -> SearchResponse:
        fts_query = _fts_query(query)
        match_expression = f"{match_column} : {fts_query}" if match_column is not None else fts_query
        exact_filter = ""
        exact_params: list[Any] = []
        if exact_code_matches:
            placeholders = ", ".join("?" for _ in FORMAL_CHUNK_TYPES)
            exact_filter = f"""
                  and (
                      chunks.chunk_type not in ({placeholders})
                      or instr(chunks.chunk_name, ?) > 0
                      or instr(chunks.content, ?) > 0
                  )
                """
            exact_params = [*FORMAL_CHUNK_TYPES, query, query]
        with self._lock:
            count_row = self.db.execute(
                f"""
                select count(*) as count
                from chunks_fts
                join chunks on chunks.id = chunks_fts.rowid
                where chunks_fts match ?
                  and (? is null or chunks.category = ?)
                  {exact_filter}
                """,
                [match_expression, category, category, *exact_params],
            ).fetchone()
            rows = self.db.execute(
                f"""
                select
                    chunks.file_path,
                    chunks.category,
                    chunks.chunk_type,
                    chunks.chunk_name,
                    chunks.start_line,
                    chunks.end_line
                from chunks_fts
                join chunks on chunks.id = chunks_fts.rowid
                where chunks_fts match ?
                  and (? is null or chunks.category = ?)
                  {exact_filter}
                order by bm25(chunks_fts)
                limit ? offset ?
                """,
                [match_expression, category, category, *exact_params, limit, offset],
            ).fetchall()
            return {
                "count": count_row["count"] if count_row is not None else 0,
                "results": _group_chapter_rows(rows, include_content=False),
            }

    def _add_search_snippets(self, results: SearchResponse, *, query: str) -> None:
        for category_result in results["results"]:
            category = category_result["category"]
            for file_result in category_result["files"]:
                file = file_result["file"]
                for chapter_result in file_result["chapters"]:
                    chapter = self.read_chapter(chapter_result["name"], file=file, category=category, count=1, offset=0)
                    if not chapter["results"]:
                        continue
                    content = chapter["results"][0]["files"][0]["chapters"][0].get("content")
                    if content:
                        chapter_result["snippet"] = _build_search_snippet(content, query=query)

    def _read_file_record(self, path: Path, rel_path: str, category: str) -> PreparedFile:
        stat = path.stat()
        line_count = 0
        with path.open("r", encoding="utf-8") as file:
            for line_count, _ in enumerate(file, start=1):
                pass
        return PreparedFile(
            rel_path=rel_path,
            category=category,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            line_count=line_count,
            chapters=[],
        )

    def _prepare_file(self, path: Path, rel_path: str, category: str) -> PreparedFile:
        record = self._read_file_record(path, rel_path, category)
        text = path.read_text(encoding="utf-8")
        chapters = parse_file(path, text)
        return PreparedFile(
            rel_path=rel_path,
            category=category,
            mtime_ns=record.mtime_ns,
            size=record.size,
            line_count=record.line_count,
            chapters=chapters,
        )

    def _write_file_record(self, prepared: PreparedFile) -> None:
        self.db.execute(
            """
            insert into files(path, category, mtime_ns, size, line_count, indexed_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(path) do update set
                category = excluded.category,
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                line_count = excluded.line_count,
                indexed_at = excluded.indexed_at
            """,
            [
                prepared.rel_path,
                prepared.category,
                prepared.mtime_ns,
                prepared.size,
                prepared.line_count,
                time.time(),
            ],
        )

    def _write_prepared_chapters(self, prepared: PreparedFile) -> None:
        self._delete_file_chunks(prepared.rel_path)
        self._write_file_record(prepared)
        if not prepared.chapters:
            return

        for chapter in prepared.chapters:
            cursor = self.db.execute(
                """
                insert into chunks(
                    file_path,
                    category,
                    chunk_type,
                    chunk_name,
                    content,
                    start_line,
                    end_line
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    prepared.rel_path,
                    prepared.category,
                    chapter.chunk_type,
                    chapter.name,
                    chapter.content,
                    chapter.start_line,
                    chapter.end_line,
                ],
            )
            chapter_id = cursor.lastrowid
            self.db.execute(
                """
                insert into chunks_fts(rowid, chunk_name, content, file_path, category)
                values (?, ?, ?, ?, ?)
                """,
                [chapter_id, chapter.name, chapter.content, prepared.rel_path, prepared.category],
            )

    def _file_unchanged(self, rel_path: str, path: Path) -> bool:
        stat = path.stat()
        row = self.db.execute("select mtime_ns, size from files where path = ?", [rel_path]).fetchone()
        return row is not None and row["mtime_ns"] == stat.st_mtime_ns and row["size"] == stat.st_size

    def _watch_loop(self, interval: float) -> None:
        while not self._watch_stop.wait(interval):
            try:
                if self.has_changes():
                    self.reindex()
            except Exception:
                logger.exception("watch loop failed while checking for changes or reindexing")

    def _filesystem_snapshot(self, category: str | None = None) -> dict[str, tuple[str, int, int]]:
        snapshot: dict[str, tuple[str, int, int]] = {}
        for selected in self._selected_categories(category):
            for path, _category_root in self._iter_category_files(selected):
                stat = path.stat()
                snapshot[self._stored_file_path(path)] = (selected, stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _indexed_snapshot(self, category: str | None = None) -> dict[str, tuple[str, int, int]]:
        categories = self._selected_categories(category)
        if not categories:
            return {}
        placeholders = ",".join("?" for _ in categories)
        rows = self.db.execute(
            f"select path, category, mtime_ns, size from files where category in ({placeholders})",
            list(categories),
        ).fetchall()
        return {row["path"]: (row["category"], row["mtime_ns"], row["size"]) for row in rows}

    def _delete_removed_files(self, categories: Iterable[str], seen_paths: set[str]) -> int:
        deleted = 0
        selected_categories = list(categories)
        if not selected_categories:
            return 0
        placeholders = ",".join("?" for _ in selected_categories)
        rows = self.db.execute(
            f"select path from files where category in ({placeholders})",
            selected_categories,
        ).fetchall()
        for row in rows:
            if row["path"] in seen_paths:
                continue
            self._delete_file_chunks(row["path"])
            deleted += 1
        return deleted

    def _delete_file_chunks(self, rel_path: str) -> None:
        rows = self.db.execute("select id from chunks where file_path = ?", [rel_path]).fetchall()
        for row in rows:
            self.db.execute("delete from chunks_fts where rowid = ?", [row["id"]])
        self.db.execute("delete from chunks where file_path = ?", [rel_path])
        self.db.execute("delete from files where path = ?", [rel_path])

    def _resolve_category_dirs(self, category_paths: Sequence[CategoryPath] | None) -> dict[str, tuple[Path, ...]]:
        if category_paths is None:
            return self._discover_category_dirs()
        configured_paths = category_paths
        category_dirs: dict[str, list[Path]] = {}
        resolved_paths: list[tuple[str, Path]] = []
        for configured_path in configured_paths:
            category, path = _parse_category_path(configured_path)
            path = path.expanduser()
            directory = path if path.is_absolute() else self.root / path
            category = category or directory.name
            if not category:
                raise ValueError(f"category path must name a directory: {configured_path}")
            directory = directory.resolve()
            for existing_category, existing_directory in resolved_paths:
                if self._configured_paths_overlap(directory, existing_directory):
                    raise ValueError(
                        "configured_path "
                        f"{configured_path!r} for category {category!r} overlaps with "
                        f"configured_paths entry for category {existing_category!r}: {directory} vs {existing_directory}"
                    )
            category_dirs.setdefault(category, []).append(directory)
            resolved_paths.append((category, directory))
        return {category: tuple(paths) for category, paths in category_dirs.items()}

    def _discover_category_dirs(self) -> dict[str, tuple[Path, ...]]:
        discovered: dict[str, Path] = {}
        for path in sorted(self.root.iterdir()):
            if path.name.startswith(".") or path.name == ".chapter-mcp":
                continue
            if self._gitignore_matcher.matches(path, path) or self._aiignore_matcher.matches(path, path):
                continue
            if path.is_file():
                try:
                    sample = path.read_bytes()[:4096]
                except OSError:
                    continue
                if not is_probably_text(path, sample):
                    continue
            discovered[path.name] = path
        return {category: (path,) for category, path in discovered.items()}

    def _iter_category_files(self, category: str) -> list[tuple[Path, Path]]:
        files: list[tuple[Path, Path]] = []
        for category_root in self.category_dirs[category]:
            if not category_root.exists():
                continue
            if category_root.is_file():
                if not self._should_skip_path(category_root, category_root):
                    files.append((category_root, category_root))
                continue
            for path in sorted(category_root.rglob("*")):
                if not path.is_file() or self._should_skip_path(path, category_root):
                    continue
                files.append((path, category_root))
        return files

    def _configured_paths_overlap(self, directory: Path, existing_directory: Path) -> bool:
        if directory == existing_directory:
            return True
        if directory.is_file() and existing_directory.is_file():
            return False
        if directory.is_file():
            return existing_directory in directory.parents
        if existing_directory.is_file():
            return directory in existing_directory.parents
        return existing_directory in directory.parents or directory in existing_directory.parents

    def _selected_categories(self, category: str | None) -> tuple[str, ...]:
        if category is None:
            return tuple(self.category_dirs)
        if category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        return (category,)

    def _should_skip_path(self, path: Path, category_dir: Path) -> bool:
        if category_dir.is_file():
            rel_parts = (path.name,)
        else:
            try:
                rel_parts = path.relative_to(category_dir).parts
            except ValueError:
                return True
        if any(part.startswith(".") for part in rel_parts):
            return True
        if self._gitignore_matcher.matches(path, category_dir):
            return True
        if self._aiignore_matcher.matches(path, category_dir):
            return True
        try:
            if path.resolve() == self.db_path.resolve():
                return True
            sample = path.read_bytes()[:4096]
        except OSError:
            return True
        return not is_probably_text(path, sample)

    def _stored_file_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()


def _parse_category_path(configured_path: CategoryPath) -> tuple[str | None, Path]:
    if isinstance(configured_path, tuple):
        category, path = configured_path
        if not category:
            raise ValueError(f"category path must include a category name: {configured_path}")
        return category, Path(path)
    if isinstance(configured_path, str) and "=" in configured_path:
        category, path = configured_path.split("=", 1)
        if not category or not path:
            raise ValueError(f"category path must use category=path: {configured_path}")
        return category, Path(path)
    return None, Path(configured_path)


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w]+", query)
    if not tokens:
        escaped = query.replace('"', '""')
        return f'"{escaped}"'
    return " OR ".join(f'"{token}"*' for token in tokens)


def _format_mtime_ns(mtime_ns: int | None) -> str | None:
    if mtime_ns is None:
        return None
    return _format_timestamp(mtime_ns / 1_000_000_000)


def _format_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _file_row_to_result(row: sqlite3.Row) -> FileRecord:
    return {
        "file": row["path"],
        "category": row["category"],
        "mtime_ns": row["mtime_ns"],
        "mtime": _format_mtime_ns(row["mtime_ns"]),
        "byte_count": row["size"],
        "line_count": row["line_count"],
        "chunk_count": row["chunk_count"],
        "indexed_at": _format_timestamp(row["indexed_at"]),
    }


def _normalize_chapter_column_fields(fields: Sequence[str] | None) -> tuple[str, ...]:
    selected_fields = tuple(fields) if fields is not None else DEFAULT_CHAPTER_COLUMN_FIELDS
    if not selected_fields:
        raise ValueError("fields must not be empty")

    unknown_fields = [field for field in selected_fields if field not in ALLOWED_CHAPTER_COLUMN_FIELDS]
    if unknown_fields:
        allowed = ", ".join(ALLOWED_CHAPTER_COLUMN_FIELDS)
        unknown = ", ".join(unknown_fields)
        raise ValueError(f"unknown chapter fields: {unknown}. Allowed fields: {allowed}")

    return selected_fields


def _chapter_field_sql(field: str) -> str:
    if field == "name":
        return "chunk_name"
    if field == "start_line":
        return "start_line"
    if field == "end_line":
        return "end_line"
    raise ValueError(f"unsupported chapter field: {field}")


def _chapter_row_to_result(
    row: sqlite3.Row,
    *,
    include_content: bool,
    content_offset: int = 0,
    content_limit: int | None = None,
) -> ChapterRecord:
    result: ChapterRecord = {
        "name": row["chunk_name"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
    }
    if include_content:
        content, total_lines, truncated = _slice_chapter_content(
            row["content"],
            content_offset=content_offset,
            content_limit=content_limit,
        )
        result["content"] = content
        if content_offset != 0 or content_limit is not None:
            result["content_offset"] = content_offset
            result["content_total_lines"] = total_lines
            result["content_truncated"] = truncated
    return result


def _group_chapter_rows(
    rows: Sequence[sqlite3.Row],
    *,
    include_content: bool,
    content_offset: int = 0,
    content_limit: int | None = None,
) -> list[CategoryChapterRecord]:
    grouped: list[CategoryChapterRecord] = []
    current_category: CategoryChapterRecord | None = None
    current_file: FileChapterRecord | None = None
    last_category: str | None = None
    last_file: str | None = None

    for row in rows:
        category = row["category"]
        file_path = row["file_path"]

        if category != last_category:
            current_category = {"category": category, "files": []}
            grouped.append(current_category)
            current_file = None
            last_category = category
            last_file = None

        if file_path != last_file:
            if current_category is None:  # pragma: no cover
                raise RuntimeError("missing category while grouping chapter rows")
            current_file = {"file": file_path, "chapters": []}
            current_category["files"].append(current_file)
            last_file = file_path

        if current_file is None:  # pragma: no cover
            raise RuntimeError("missing file while grouping chapter rows")
        current_file["chapters"].append(
            _chapter_row_to_result(
                row,
                include_content=include_content,
                content_offset=content_offset,
                content_limit=content_limit,
            )
        )

    return grouped


def _group_chapter_column_rows(
    rows: Sequence[sqlite3.Row],
    *,
    fields: Sequence[str],
) -> list[CategoryChapterColumnsRecord]:
    grouped: list[CategoryChapterColumnsRecord] = []
    current_category: CategoryChapterColumnsRecord | None = None
    current_file: FileChapterColumnsRecord | None = None
    last_category: str | None = None
    last_file: str | None = None

    for row in rows:
        category = row["category"]
        file_path = row["file_path"]

        if category != last_category:
            current_category = {"category": category, "files": []}
            grouped.append(current_category)
            current_file = None
            last_category = category
            last_file = None

        if file_path != last_file:
            if current_category is None:  # pragma: no cover
                raise RuntimeError("missing category while grouping chapter column rows")
            current_file = {"file": file_path, "columns": list(fields), "rows": []}
            current_category["files"].append(current_file)
            last_file = file_path

        if current_file is None:  # pragma: no cover
            raise RuntimeError("missing file while grouping chapter column rows")
        current_file["rows"].append([row[field] for field in fields])

    return grouped


def _slice_chapter_content(content: str, *, content_offset: int, content_limit: int | None) -> tuple[str, int, bool]:
    lines = content.splitlines()
    total_lines = len(lines)
    if content_limit is None and content_offset == 0:
        return content, total_lines, False

    start = min(content_offset, total_lines)
    end = total_lines if content_limit is None else min(total_lines, start + content_limit)
    sliced = "\n".join(lines[start:end])
    truncated = start > 0 or end < total_lines
    return sliced, total_lines, truncated


def _build_search_snippet(content: str, *, query: str, min_chars: int = 60, max_chars: int = 120) -> str:
    compact = " ".join(content.split())
    if len(compact) <= max_chars:
        return compact

    terms = [term for term in re.findall(r"\w+", query.lower()) if len(term) > 1]
    lower_compact = compact.lower()
    match_index = -1
    match_length = 0
    for term in terms:
        candidate = lower_compact.find(term)
        if candidate != -1:
            match_index = candidate
            match_length = len(term)
            break

    if match_index == -1:
        match_index = 0

    start = max(0, match_index - (max_chars // 2))
    end = min(len(compact), max_chars + start)
    if end - start < max_chars:
        start = max(0, end - max_chars)

    before = compact.rfind(". ", max(0, start - 40), min(match_index + 1, len(compact)))
    if before != -1:
        start = before + 2
    else:
        before = compact.rfind(": ", max(0, start - 40), min(match_index + 1, len(compact)))
        if before != -1:
            start = before + 2

    after_search_start = max(match_index + match_length, start)
    after = len(compact)
    for marker in (". ", "! ", "? ", "; "):
        candidate = compact.find(marker, after_search_start, min(len(compact), end + 40))
        if candidate != -1:
            after = min(after, candidate + 1)
    end = min(after, len(compact))

    snippet = compact[start:end].strip(" .")
    if len(snippet) < min_chars and end < len(compact):
        snippet = compact[start : min(len(compact), start + max_chars)].strip(" .")
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rsplit(" ", 1)[0].strip(" .")
    return snippet
