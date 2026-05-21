from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite_vec
from sqlite_vec import serialize_float32

from chunk_mcp.chunks import Chunk, is_probably_text, parse_file
from chunk_mcp.embeddings import Embedder


EMBEDDING_DIMENSIONS = 384
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


class ChunkIndex:
    def __init__(
        self,
        root: Path,
        db_path: Path,
        embedder: Embedder,
        category_paths: Sequence[CategoryPath] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.db_path = db_path if db_path.is_absolute() else self.root / db_path
        self.embedder = embedder
        self.category_dirs = self._resolve_category_dirs(category_paths)
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self._ensure_schema()

    def close(self) -> None:
        self.stop_watcher()
        with self._lock:
            self.db.close()

    def start_watcher(self, interval: float = 1.0) -> None:
        if interval <= 0:
            raise ValueError("watch interval must be greater than zero")
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(interval,),
            name="chunk-mcp-index-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def stop_watcher(self) -> None:
        thread = self._watch_thread
        if thread is None:
            return
        self._watch_stop.set()
        thread.join(timeout=5)
        self._watch_thread = None

    def _ensure_schema(self) -> None:
        with self._lock:
            self.db.executescript(
                f"""
                create table if not exists files (
                    path text primary key,
                    category text not null,
                    mtime_ns integer not null,
                    size integer not null,
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
                create virtual table if not exists vec_chunks using vec0(
                    embedding float[{EMBEDDING_DIMENSIONS}]
                );
                create index if not exists idx_chunks_file_name on chunks(file_path, chunk_name);
                create index if not exists idx_chunks_category on chunks(category);
                """
            )
            self.db.commit()

    def reindex(self, category: str | None = None) -> IndexStats:
        with self._lock:
            categories = self._selected_categories(category)
            stats = IndexStats()
            seen_paths: set[str] = set()

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
                    if self._file_unchanged(rel_path, path):
                        stats = stats.plus(skipped=1)
                        continue
                    self._index_file(path, rel_path, selected)
                    stats = stats.plus(indexed=1)

            stats = stats.plus(deleted=self._delete_removed_files(categories, seen_paths))
            self.db.commit()
            return stats

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

        query_embedding = self.embedder.encode([query])[0]
        with self._lock:
            count = self._count_chunks(category)
            k = self._search_k(category, limit + offset)
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
                    vec_chunks.distance
                from vec_chunks
                join chunks on chunks.id = vec_chunks.rowid
                where vec_chunks.embedding match ?
                  and k = ?
                  and (? is null or chunks.category = ?)
                order by vec_chunks.distance
                """,
                [serialize_float32(query_embedding), k, category, category],
            ).fetchall()
            return {
                "count": count,
                "limit": limit,
                "offset": offset,
                "results": [_row_to_result(row) for row in rows[offset : offset + limit]],
            }

    def get_chunk(self, file: str, chunk_name: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                """
                select
                    file_path,
                    category,
                    chunk_type,
                    chunk_name,
                    content,
                    start_line,
                    end_line
                from chunks
                where file_path = ? and chunk_name = ?
                """,
                [file, chunk_name],
            ).fetchone()
            if row is None:
                return {"found": False, "file": file, "chunk_name": chunk_name}
            result = _row_to_result(row)
            result["found"] = True
            return result

    def _index_file(self, path: Path, rel_path: str, category: str) -> None:
        stat = path.stat()
        text = path.read_text(encoding="utf-8")
        chunks = parse_file(path, text)

        self._delete_file_chunks(rel_path)
        self.db.execute(
            """
            insert into files(path, category, mtime_ns, size, indexed_at)
            values (?, ?, ?, ?, ?)
            on conflict(path) do update set
                category = excluded.category,
                mtime_ns = excluded.mtime_ns,
                size = excluded.size,
                indexed_at = excluded.indexed_at
            """,
            [rel_path, category, stat.st_mtime_ns, stat.st_size, time.time()],
        )

        if not chunks:
            return

        embeddings = self.embedder.encode([_embedding_text(chunk) for chunk in chunks])
        for chunk, embedding in zip(chunks, embeddings, strict=True):
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
                    rel_path,
                    category,
                    chunk.chunk_type,
                    chunk.name,
                    chunk.content,
                    chunk.start_line,
                    chunk.end_line,
                ],
            )
            chunk_id = cursor.lastrowid
            self.db.execute(
                "insert into vec_chunks(rowid, embedding) values (?, ?)",
                [chunk_id, serialize_float32(embedding)],
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

    def _search_k(self, category: str | None, requested: int) -> int:
        if category is None:
            return requested
        return max(requested, self._count_chunks())

    def _count_chunks(self, category: str | None = None) -> int:
        if category is None:
            row = self.db.execute("select count(*) as count from chunks").fetchone()
        else:
            row = self.db.execute("select count(*) as count from chunks where category = ?", [category]).fetchone()
        return row["count"] if row is not None else 0

    def _delete_removed_files(self, categories: Iterable[str], seen_paths: set[str]) -> int:
        deleted = 0
        placeholders = ",".join("?" for _ in categories)
        selected_categories = list(categories)
        if not selected_categories:
            return 0
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
            self.db.execute("delete from vec_chunks where rowid = ?", [row["id"]])
        self.db.execute("delete from chunks where file_path = ?", [rel_path])
        self.db.execute("delete from files where path = ?", [rel_path])

    def _resolve_category_dirs(self, category_paths: Sequence[CategoryPath] | None) -> dict[str, Path]:
        configured_paths = category_paths or ()
        category_dirs: dict[str, Path] = {}
        for configured_path in configured_paths:
            category, path = _parse_category_path(configured_path)
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


def _embedding_text(chunk: Chunk) -> str:
    return f"{chunk.name}\n{chunk.content}"


def _row_to_result(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "file": row["file_path"],
        "category": row["category"],
        "chunk_type": row["chunk_type"],
        "chunk_name": row["chunk_name"],
        "content": row["content"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
    }
    if "distance" in row.keys():
        result["distance"] = row["distance"]
    return result
