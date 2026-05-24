from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from chapter_mcp.index import CategoryPath, ChapterIndex


def create_app(
    root: Path | None = None,
    db_path: Path | None = None,
    paths: Sequence[CategoryPath] | None = None,
    index_on_startup: bool = True,
    async_startup: bool = True,
    watch: bool = True,
    watch_interval: float = 1.0,
) -> FastMCP:
    root_path = (root or Path.cwd()).expanduser().resolve()
    database_path = db_path or root_path / ".chapter-mcp" / "index.sqlite3"
    chapter_index = ChapterIndex(root_path, database_path, paths)
    if index_on_startup and async_startup:
        chapter_index.start_background_reindex()
    elif index_on_startup:
        chapter_index.reindex()
    if watch:
        chapter_index.start_watcher(interval=watch_interval)

    mcp = FastMCP(name="chapter-mcp")

    @mcp.tool
    def search(query: str, category: str | None = None, limit: int = 1, offset: int = 0) -> dict[str, Any]:
        """Search indexed chapter content."""
        return chapter_index.search(query=query, category=category, limit=limit, offset=offset)

    @mcp.tool
    def search_chapter(query: str, category: str | None = None, limit: int = 5, offset: int = 0) -> dict[str, Any]:
        """Search indexed chapter names only."""
        return chapter_index.search_chapter(query=query, category=category, limit=limit, offset=offset)

    @mcp.tool
    def read_chapter(
        chapter_name: str,
        file: str | None = None,
        category: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read chapters by exact chapter name, optionally narrowed by file and category."""
        return chapter_index.read_chapter(
            chapter_name=chapter_name,
            file=file,
            category=category,
            count=count,
            offset=offset,
        )

    @mcp.tool
    def list_chapters(
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List indexed chapter names and line ranges without chapter content."""
        return chapter_index.list_chapters(category=category, file=file, count=count, offset=offset)

    @mcp.tool
    def list_files(category: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """List indexed files and their index metadata."""
        return chapter_index.list_files(category=category, limit=limit, offset=offset)

    @mcp.tool
    def stats() -> dict[str, Any]:
        """Return index totals and configured category status."""
        return chapter_index.stats()

    @mcp.tool
    def reindex(category: str | None = None) -> dict[str, int]:
        """Re-scan indexed folders and update changed files."""
        return chapter_index.reindex(category=category).as_dict()

    mcp.chapter_index = chapter_index  # type: ignore[attr-defined]
    return mcp
