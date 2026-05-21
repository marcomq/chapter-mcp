from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from typing import Any

from fastmcp import FastMCP

from chunk_mcp.embeddings import Embedder, SentenceTransformerEmbedder
from chunk_mcp.index import CategoryPath, ChunkIndex


def create_app(
    root: Path | None = None,
    db_path: Path | None = None,
    paths: Sequence[CategoryPath] | None = None,
    embedder: Embedder | None = None,
    index_on_startup: bool = True,
    watch: bool = True,
    watch_interval: float = 1.0,
) -> FastMCP:
    root_path = (root or Path.cwd()).resolve()
    database_path = db_path or root_path / ".chunk-mcp" / "index.sqlite3"
    chunk_index = ChunkIndex(root_path, database_path, embedder or SentenceTransformerEmbedder(), paths)
    if index_on_startup:
        chunk_index.reindex()
    if watch:
        chunk_index.start_watcher(interval=watch_interval)

    mcp = FastMCP(name="chunk-mcp")

    @mcp.tool
    def search(query: str, category: str | None = None, limit: int = 1, offset: int = 0) -> dict[str, Any]:
        """Search for semantically similar file chunks."""
        return chunk_index.search(query=query, category=category, limit=limit, offset=offset)

    @mcp.tool
    def get_chunk(file: str, chunk_name: str) -> dict[str, Any]:
        """Fetch a specific indexed chunk by file path and chunk name."""
        return chunk_index.get_chunk(file=file, chunk_name=chunk_name)

    @mcp.tool
    def reindex(category: str | None = None) -> dict[str, int]:
        """Re-scan indexed folders and update changed files."""
        return chunk_index.reindex(category=category).as_dict()

    mcp.chunk_index = chunk_index  # type: ignore[attr-defined]
    return mcp
