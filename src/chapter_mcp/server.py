from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastmcp import FastMCP

from chapter_mcp.index import (
    CategoryPath,
    ChapterIndex,
    ChapterColumnsResponse,
    FileListResponse,
    IndexStatsDict,
    ReadChapterResponse,
    SearchResponse,
    StatsResponse,
    ChapterListResponse,
)


def create_app(
    root: Path | None = None,
    db_path: Path | None = None,
    paths: Sequence[CategoryPath] | None = None,
    index_on_startup: bool = True,
    async_startup: bool = True,
    watch: bool = True,
    watch_interval: float = 1.0,
) -> FastMCP:
    """Create a FastMCP app wired to a ChapterIndex and the standard search/list/reindex tools."""
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
    def search(
        query: str,
        category: str | None = None,
        limit: int = 5,
        offset: int = 0,
        include_snippet: bool = False,
        exact_code_matches: bool = False,
    ) -> SearchResponse:
        """Search indexed chapter names and content before choosing what to read."""
        return chapter_index.search(
            query=query,
            category=category,
            limit=limit,
            offset=offset,
            include_snippet=include_snippet,
            exact_code_matches=exact_code_matches,
        )

    @mcp.tool
    def search_chapter(query: str, category: str | None = None, limit: int = 5, offset: int = 0) -> SearchResponse:
        """Search indexed chapter names only; useful when the section title is known or likely."""
        return chapter_index.search_chapter(query=query, category=category, limit=limit, offset=offset)

    @mcp.tool
    def read_search(
        query: str,
        category: str | None = None,
        offset: int = 0,
        content_limit: int | None = None,
        exact_code_matches: bool = False,
    ) -> ReadChapterResponse:
        """Read the best search match directly; use content_limit for a small first read."""
        return chapter_index.read_search(
            query=query,
            category=category,
            offset=offset,
            content_limit=content_limit,
            exact_code_matches=exact_code_matches,
        )

    @mcp.tool
    def read_chapter_at(
        file: str,
        line: int,
        category: str | None = None,
        content_limit: int | None = None,
    ) -> ReadChapterResponse:
        """Read the indexed chapter containing a known file line before falling back to a raw range read."""
        return chapter_index.read_chapter_at(
            file=file,
            line=line,
            category=category,
            content_limit=content_limit,
        )

    @mcp.tool
    def read_chapter(
        chapter_name: str,
        file: str | None = None,
        category: str | None = None,
        count: int = 5,
        offset: int = 0,
        content_offset: int = 0,
        content_limit: int | None = None,
    ) -> ReadChapterResponse:
        """Read indexed chapters by exact chapter name, optionally narrowed by file and category.

        `content_offset` and `content_limit` slice the stored chapter content by lines.
        """
        return chapter_index.read_chapter(
            chapter_name=chapter_name,
            file=file,
            category=category,
            count=count,
            offset=offset,
            content_offset=content_offset,
            content_limit=content_limit,
        )

    @mcp.tool
    def list_chapters(
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
    ) -> ChapterListResponse:
        """List indexed chapter names and line ranges before reading content."""
        return chapter_index.list_chapters(category=category, file=file, count=count, offset=offset)

    @mcp.tool
    def list_chapters_as_columns(
        category: str | None = None,
        file: str | None = None,
        count: int = 5,
        offset: int = 0,
        fields: Sequence[str] | None = None,
    ) -> ChapterColumnsResponse:
        """List indexed chapters as compact rows; use this when only names or line ranges are needed."""
        return chapter_index.list_chapters_as_columns(
            category=category,
            file=file,
            count=count,
            offset=offset,
            fields=fields,
        )

    @mcp.tool
    def list_files(category: str | None = None, limit: int = 100, offset: int = 0) -> FileListResponse:
        """List indexed files with their number of chapters."""
        return chapter_index.list_files(category=category, limit=limit, offset=offset)

    @mcp.tool
    def stats() -> StatsResponse:
        """Return index totals and configured category status."""
        return chapter_index.stats()

    @mcp.tool
    def reindex(category: str | None = None) -> IndexStatsDict:
        """Re-scan indexed folders and update changed files."""
        return chapter_index.reindex(category=category).as_dict()

    mcp.chapter_index = chapter_index  # type: ignore[attr-defined]
    return mcp
