from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from chunk_mcp.embeddings import Embedder, SentenceTransformerEmbedder
from chunk_mcp.index import CategoryPath, ChunkIndex


def create_app(
    root: Path | None = None,
    db_path: Path | None = None,
    paths: Sequence[CategoryPath] | None = None,
    embedder: Embedder | None = None,
    vector: bool = False,
    index_on_startup: bool = True,
    async_startup: bool = True,
    warmup: bool = True,
    watch: bool = True,
    watch_interval: float = 1.0,
) -> FastMCP:
    root_path = (root or Path.cwd()).expanduser().resolve()
    database_path = db_path or root_path / ".chunk-mcp" / "index.sqlite3"
    vector_embedder = embedder or (SentenceTransformerEmbedder() if vector else None)
    chunk_index = ChunkIndex(root_path, database_path, vector_embedder, paths, use_vector=vector)
    if index_on_startup and async_startup:
        chunk_index.start_background_reindex(warmup=warmup)
    elif index_on_startup:
        chunk_index.reindex()
        if vector and warmup:
            chunk_index.warmup()
    elif vector and warmup:
        chunk_index.warmup()
    if watch:
        chunk_index.start_watcher(interval=watch_interval)

    mcp = FastMCP(name="chunk-mcp")

    @mcp.tool
    def search(query: str, category: str | None = None, limit: int = 1, offset: int = 0) -> dict[str, Any]:
        """Search indexed chunks."""
        return chunk_index.search(query=query, category=category, limit=limit, offset=offset)

    @mcp.tool
    def get_chunk(file: str, chunk_name: str) -> dict[str, Any]:
        """Fetch a specific indexed chunk by file path and chunk name."""
        return chunk_index.get_chunk(file=file, chunk_name=chunk_name)

    @mcp.tool
    def list_files(category: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """List indexed files and their index metadata."""
        return chunk_index.list_files(category=category, limit=limit, offset=offset)

    @mcp.tool
    def stats() -> dict[str, Any]:
        """Return index totals and configured category status."""
        return chunk_index.stats()

    @mcp.tool
    def reindex(category: str | None = None) -> dict[str, int]:
        """Re-scan indexed folders and update changed files."""
        return chunk_index.reindex(category=category).as_dict()

    mcp.chunk_index = chunk_index  # type: ignore[attr-defined]
    return mcp
