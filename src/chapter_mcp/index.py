from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chapter_mcp.chunks import Chunk, is_probably_text, parse_file


CategoryPath = Path | str | tuple[str, Path | str]


@dataclass(frozen=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "deleted": self.deleted,
        }

    def plus(self, *, scanned: int = 0, indexed: int = 0, skipped: int = 0, deleted: int = 0) -> "IndexStats":
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

    def as_dict(self) -> dict[str, int | float]:
        return {
            "chapters_loaded": self.chapters_loaded,
            "indexing_time_seconds": round(self.indexing_time_seconds, 6),
        }

    def plus(self, *, chapters_loaded: int = 0) -> "IndexingSummary":
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
        self.root = root.expanduser().resolve()
        db_path = db_path.expanduser()
        self.db_path = db_path if db_path.is_absolute() else self.root / db_path
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
        self.stop_watcher()
        self.wait_for_startup(timeout=5)
        with self._lock:
            self.db.close()

    def start_background_reindex(self) -> None:
        if self._startup_thread is not None and self._startup_thread.is_alive():
            return
        self._startup_thread = threading.Thread(
            target=self._background_reindex,
            name="chapter-mcp-startup-index",
            daemon=True,
        )
        self._startup_thread.start()

    def wait_for_startup(self, timeout: float | None = None) -> None:
        thread = self._startup_thread
        if thread is not None:
            thread.join(timeout=timeout)

    def start_watcher(self, interval: float = 1.0) -> None:
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
        thread = self._watch_thread
        if thread is None:
            return
        self._watch_stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            return
        self._watch_thread = None

    def reindex(self, category: str | None = None) -> IndexStats:
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
                directory = self.category_dirs[selected]
                if not directory.exists():
                    continue
                for path in sorted(directory.rglob("*")):
                    if not path.is_file() or self._should_skip_path(path, directory):
                        continue
                    rel_path = self._stored_file_path(path)
                    seen_paths.add(rel_path)
                    stats = stats.plus(scanned=1)
                    with self._lock:
                        unchanged = self._file_unchanged(rel_path, path)
                    if unchanged:
                        stats = stats.plus(skipped=1)
                        continue

                    file_record = self._read_file_record(path, rel_path, selected)
                    with self._lock:
                        self._write_file_record(file_record)
                        self.db.commit()

                    prepared = self._prepare_file(path, rel_path, selected)
                    with self._lock:
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
        live_snapshot = self._filesystem_snapshot(category)
        with self._lock:
            indexed_snapshot = self._indexed_snapshot(category)
        return live_snapshot != indexed_snapshot

    def search(self, query: str, category: str | None = None, limit: int = 1, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        return self._search_fts(query=query, category=category, limit=limit, offset=offset)

    def search_chapter(self, query: str, category: str | None = None, limit: int = 5, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        if category is not None and category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        return self._search_chapter_fts(query=query, category=category, limit=limit, offset=offset)

    def read_chapter(
        self,
        chapter_name: str,
        file: str | None = None,
        category: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> dict[str, Any]:
        count = max(1, min(count, 100))
        offset = max(0, offset)
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
                "chapters": [_row_to_result(row) for row in rows],
            }

    def list_chapters(
        self,
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> dict[str, Any]:
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
                "chapters": [_chapter_row_to_result(row, include_content=False) for row in rows],
            }

    def list_files(self, category: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            categories: dict[str, Any] = {}
            total_files = 0
            total_chunks = 0
            total_bytes = 0
            total_lines = 0
            for category, directory in self.category_dirs.items():
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
                categories[category] = {
                    "path": directory.as_posix(),
                    "exists": directory.exists(),
                    "file_count": file_count,
                    "chunk_count": chunk_count,
                    "byte_count": byte_count,
                    "line_count": line_count,
                    "latest_mtime_ns": file_row["latest_mtime_ns"] if file_row is not None else None,
                    "latest_mtime": _format_mtime_ns(file_row["latest_mtime_ns"]) if file_row is not None else None,
                    "latest_indexed_at": _format_timestamp(file_row["latest_indexed_at"]) if file_row is not None else None,
                }
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

    def _search_fts(self, query: str, category: str | None, limit: int, offset: int) -> dict[str, Any]:
        return self._search_fts_columns(
            query=query,
            category=category,
            limit=limit,
            offset=offset,
            match_column=None,
        )

    def _search_chapter_fts(self, query: str, category: str | None, limit: int, offset: int) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        fts_query = _fts_query(query)
        match_expression = f"{match_column} : {fts_query}" if match_column is not None else fts_query
        with self._lock:
            count_row = self.db.execute(
                """
                select count(*) as count
                from chunks_fts
                join chunks on chunks.id = chunks_fts.rowid
                where chunks_fts match ?
                  and (? is null or chunks.category = ?)
                """,
                [match_expression, category, category],
            ).fetchone()
            rows = self.db.execute(
                """
                select
                    chunks.file_path,
                    chunks.category,
                    chunks.chunk_type,
                    chunks.chunk_name,
                    chunks.content,
                    chunks.start_line,
                    chunks.end_line,
                    bm25(chunks_fts) as score
                from chunks_fts
                join chunks on chunks.id = chunks_fts.rowid
                where chunks_fts match ?
                  and (? is null or chunks.category = ?)
                order by score
                limit ? offset ?
                """,
                [match_expression, category, category, limit, offset],
            ).fetchall()
            return {
                "count": count_row["count"] if count_row is not None else 0,
                "results": [_row_to_result(row) for row in rows],
            }

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
            if self.has_changes():
                self.reindex()

    def _filesystem_snapshot(self, category: str | None = None) -> dict[str, tuple[str, int, int]]:
        snapshot: dict[str, tuple[str, int, int]] = {}
        for selected in self._selected_categories(category):
            directory = self.category_dirs[selected]
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or self._should_skip_path(path, directory):
                    continue
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

    def _resolve_category_dirs(self, category_paths: Sequence[CategoryPath] | None) -> dict[str, Path]:
        configured_paths = category_paths or ()
        category_dirs: dict[str, Path] = {}
        for configured_path in configured_paths:
            category, path = _parse_category_path(configured_path)
            path = path.expanduser()
            directory = path if path.is_absolute() else self.root / path
            category = category or directory.name
            if not category:
                raise ValueError(f"category path must name a directory: {configured_path}")
            if category in category_dirs:
                raise ValueError(f"duplicate category name from path: {configured_path}")
            category_dirs[category] = directory.resolve()
        return category_dirs

    def _selected_categories(self, category: str | None) -> tuple[str, ...]:
        if category is None:
            return tuple(self.category_dirs)
        if category not in self.category_dirs:
            raise ValueError(f"unknown category: {category}")
        return (category,)

    def _should_skip_path(self, path: Path, category_dir: Path) -> bool:
        try:
            rel_parts = path.relative_to(category_dir).parts
        except ValueError:
            return True
        if any(part.startswith(".") for part in rel_parts):
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


def _file_row_to_result(row: sqlite3.Row) -> dict[str, Any]:
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


def _chapter_row_to_result(row: sqlite3.Row, *, include_content: bool) -> dict[str, Any]:
    result = {
        "file": row["file_path"],
        "category": row["category"],
        "type": row["chunk_type"],
        "chapter_name": row["chunk_name"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
    }
    if include_content:
        result["content"] = row["content"]
    if "score" in row.keys():
        result["score"] = row["score"]
    return result


def _row_to_result(row: sqlite3.Row) -> dict[str, Any]:
    return _chapter_row_to_result(row, include_content=True)
