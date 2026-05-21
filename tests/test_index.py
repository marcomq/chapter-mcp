from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from chunk_mcp.index import ChunkIndex
from chunk_mcp.server import create_app


class FakeEmbedder:
    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        text_lower = text.lower()
        values = [0.0] * 384
        if "alpha" in text_lower:
            values[0] = 1.0
        if "beta" in text_lower:
            values[1] = 1.0
        if "gamma" in text_lower:
            values[2] = 1.0
        if not any(values):
            values[3] = 1.0
        return values


class SlowEmbedder(FakeEmbedder):
    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        time.sleep(0.2)
        return super().encode(texts)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_index_reindex_search_get_chunk_and_cleanup(tmp_path: Path) -> None:
    write(tmp_path / "knowledge" / "alpha.md", "# Alpha\nUseful search notes.")
    write(tmp_path / "examples" / "beta.txt", "Beta example paragraph.")
    db_path = tmp_path / ".chunk-mcp" / "index.sqlite3"
    index = ChunkIndex(tmp_path, db_path, None, category_paths=["knowledge", "examples"])

    try:
        first = index.reindex()
        assert first.as_dict() == {"scanned": 2, "indexed": 2, "skipped": 0, "deleted": 0}

        second = index.reindex()
        assert second.as_dict() == {"scanned": 2, "indexed": 0, "skipped": 2, "deleted": 0}

        results = index.search("alpha")
        assert results["count"] == 1
        assert results["limit"] == 1
        assert results["offset"] == 0
        assert results["mode"] == "fts"
        assert results["results"][0]["file"] == "knowledge/alpha.md"
        assert results["results"][0]["chunk_type"] == "markdown_section"
        assert results["results"][0]["chunk_name"] == "Alpha"
        assert results["results"][0]["start_line"] == 1
        assert results["results"][0]["end_line"] == 2

        filtered = index.search("alpha", category="examples", limit=1)
        assert filtered["count"] == 0
        assert filtered["results"] == []

        files = index.list_files()
        assert files["count"] == 2
        alpha_file = next(file for file in files["files"] if file["file"] == "knowledge/alpha.md")
        assert alpha_file["category"] == "knowledge"
        assert alpha_file["line_count"] == 2
        assert alpha_file["byte_count"] == (tmp_path / "knowledge" / "alpha.md").stat().st_size
        assert alpha_file["chunk_count"] == 1
        assert alpha_file["mtime_ns"] == (tmp_path / "knowledge" / "alpha.md").stat().st_mtime_ns
        assert alpha_file["mtime"] is not None
        assert alpha_file["indexed_at"] is not None

        stats = index.stats()
        assert stats["file_count"] == 2
        assert stats["chunk_count"] == 2
        assert stats["line_count"] == 3
        assert stats["byte_count"] == (tmp_path / "knowledge" / "alpha.md").stat().st_size + (
            tmp_path / "examples" / "beta.txt"
        ).stat().st_size
        assert stats["categories"]["knowledge"]["file_count"] == 1
        assert stats["categories"]["knowledge"]["latest_mtime"] is not None

        chunk = index.get_chunk("knowledge/alpha.md", "Alpha")
        assert chunk["found"] is True
        assert chunk["content"] == "# Alpha\nUseful search notes."

        missing = index.get_chunk("knowledge/alpha.md", "Missing")
        assert missing == {"found": False, "file": "knowledge/alpha.md", "chunk_name": "Missing"}

        (tmp_path / "examples" / "beta.txt").unlink()
        cleanup = index.reindex("examples")
        assert cleanup.deleted == 1
        assert index.search("beta", category="examples", limit=1)["results"] == []
    finally:
        index.close()


def test_vector_search_remains_available_when_enabled(tmp_path: Path) -> None:
    write(tmp_path / "knowledge" / "alpha.md", "# Alpha\nUseful search notes.")
    write(tmp_path / "examples" / "beta.txt", "Beta example paragraph.")
    index = ChunkIndex(
        tmp_path,
        tmp_path / ".chunk-mcp" / "index.sqlite3",
        FakeEmbedder(),
        category_paths=["knowledge", "examples"],
        use_vector=True,
    )

    try:
        index.reindex()
        result = index.search("alpha", category="examples", limit=1)
        assert result["mode"] == "vector"
        assert result["count"] == 1
        assert result["results"][0]["file"] == "examples/beta.txt"
    finally:
        index.close()


def test_changed_file_is_reindexed(tmp_path: Path) -> None:
    path = tmp_path / "source" / "sample.py"
    write(path, "def alpha():\n    return 'alpha'\n")
    index = ChunkIndex(tmp_path, tmp_path / ".chunk-mcp" / "index.sqlite3", None, category_paths=["source"])

    try:
        assert index.reindex().indexed == 1
        write(path, "def gamma():\n    return 'gamma'\n")
        stats = index.reindex()
        assert stats.indexed == 1
        assert index.search("gamma", limit=1)["results"][0]["chunk_name"] == "gamma"
    finally:
        index.close()


def test_create_app_exposes_index_for_direct_tool_testing(tmp_path: Path) -> None:
    write(tmp_path / "instructions" / "readme.md", "# Gamma\nGamma instructions.")

    app = create_app(
        root=tmp_path,
        paths=["instructions"],
        index_on_startup=True,
        async_startup=False,
        watch=False,
    )
    try:
        result = app.chunk_index.search("gamma", limit=1)  # type: ignore[attr-defined]
        assert result["results"][0]["file"] == "instructions/readme.md"
        assert app.chunk_index.reindex().as_dict()["skipped"] == 1  # type: ignore[attr-defined]
    finally:
        app.chunk_index.close()  # type: ignore[attr-defined]


def test_configured_hidden_folder_uses_folder_name_as_category(tmp_path: Path) -> None:
    write(tmp_path / ".serena" / "memories" / "project.md", "# Alpha\nAlpha memory.")
    index = ChunkIndex(
        tmp_path,
        tmp_path / ".chunk-mcp" / "index.sqlite3",
        None,
        category_paths=[".serena/memories"],
    )

    try:
        stats = index.reindex()
        assert stats.indexed == 1
        result = index.search("alpha", category="memories", limit=1)
        assert result["results"][0]["file"] == ".serena/memories/project.md"
        assert result["results"][0]["category"] == "memories"
    finally:
        index.close()


def test_create_app_accepts_configured_paths(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "note.txt", "Beta documentation.")

    app = create_app(
        root=tmp_path,
        paths=["docs"],
        index_on_startup=True,
        async_startup=False,
        watch=False,
    )
    try:
        result = app.chunk_index.search("beta", category="docs", limit=1)  # type: ignore[attr-defined]
        assert result["results"][0]["file"] == "docs/note.txt"
    finally:
        app.chunk_index.close()  # type: ignore[attr-defined]


def test_configured_path_can_use_explicit_category_name(tmp_path: Path) -> None:
    write(tmp_path / ".serena" / "memories" / "project.md", "# Gamma\nGamma memory.")
    index = ChunkIndex(
        tmp_path,
        tmp_path / ".chunk-mcp" / "index.sqlite3",
        None,
        category_paths=["memory=.serena/memories"],
    )

    try:
        stats = index.reindex()
        assert stats.indexed == 1
        result = index.search("gamma", category="memory", limit=1)
        assert result["results"][0]["file"] == ".serena/memories/project.md"
        assert result["results"][0]["category"] == "memory"
    finally:
        index.close()


def test_configured_path_expands_user_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write(tmp_path / "memories" / "project.md", "# Gamma\nGamma memory.")
    index = ChunkIndex(
        tmp_path,
        tmp_path / ".chunk-mcp" / "index.sqlite3",
        None,
        category_paths=["memory=~/memories"],
    )

    try:
        stats = index.reindex()
        assert stats.indexed == 1
        result = index.search("gamma", category="memory", limit=1)
        assert result["results"][0]["file"] == "memories/project.md"
        assert index.stats()["categories"]["memory"]["path"] == (tmp_path / "memories").as_posix()
    finally:
        index.close()


def test_no_configured_paths_indexes_nothing(tmp_path: Path) -> None:
    write(tmp_path / "knowledge" / "alpha.md", "# Alpha\nAlpha note.")
    index = ChunkIndex(tmp_path, tmp_path / ".chunk-mcp" / "index.sqlite3", None)

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 0, "indexed": 0, "skipped": 0, "deleted": 0}
        assert index.search("alpha", limit=1) == {
            "count": 0,
            "limit": 1,
            "offset": 0,
            "ready": True,
            "status": "ready",
            "mode": "fts",
            "results": [],
        }
    finally:
        index.close()


def test_create_app_starts_indexing_in_background(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")

    start = time.monotonic()
    app = create_app(root=tmp_path, paths=["docs"], embedder=SlowEmbedder(), vector=True, watch=False)
    elapsed = time.monotonic() - start

    try:
        assert elapsed < 0.25
        search_start = time.monotonic()
        not_ready = app.chunk_index.search("alpha")  # type: ignore[attr-defined]
        assert time.monotonic() - search_start < 0.25
        assert not_ready["ready"] is False
        assert not_ready["status"] == "indexing"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            listed = app.chunk_index.list_files()  # type: ignore[attr-defined]
            if listed["files"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("file metadata was not available before embedding finished")
        assert listed["files"][0]["file"] == "docs/alpha.md"
        assert listed["files"][0]["chunk_count"] == 0
        stats = app.chunk_index.stats()  # type: ignore[attr-defined]
        assert stats["startup_index_running"] is True or stats["last_reindex"] is not None
        app.chunk_index.wait_for_startup(timeout=2)  # type: ignore[attr-defined]
        assert app.chunk_index.stats()["embedding_ready"] is True  # type: ignore[attr-defined]
        assert app.chunk_index.search("alpha")["results"][0]["file"] == "docs/alpha.md"  # type: ignore[attr-defined]
    finally:
        app.chunk_index.close()  # type: ignore[attr-defined]


def test_index_methods_can_be_called_from_worker_thread(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")
    index = ChunkIndex(tmp_path, tmp_path / ".chunk-mcp" / "index.sqlite3", None, category_paths=["docs"])

    try:
        index.reindex()
        with ThreadPoolExecutor(max_workers=1) as executor:
            search_result = executor.submit(index.search, "alpha").result()
            chunk_result = executor.submit(index.get_chunk, "docs/alpha.md", "Alpha").result()

        assert search_result["results"][0]["file"] == "docs/alpha.md"
        assert chunk_result["found"] is True
    finally:
        index.close()


def test_has_changes_detects_added_modified_and_deleted_files(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "alpha.md"
    write(path, "# Alpha\nAlpha note.")
    index = ChunkIndex(tmp_path, tmp_path / ".chunk-mcp" / "index.sqlite3", None, category_paths=["docs"])

    try:
        assert index.has_changes() is True
        index.reindex()
        assert index.has_changes() is False

        write(path, "# Gamma\nGamma note.")
        assert index.has_changes() is True
        index.reindex()
        assert index.has_changes() is False

        path.unlink()
        assert index.has_changes() is True
    finally:
        index.close()


def test_watcher_reindexes_modified_files(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "alpha.md"
    write(path, "# Alpha\nAlpha note.")
    index = ChunkIndex(tmp_path, tmp_path / ".chunk-mcp" / "index.sqlite3", None, category_paths=["docs"])

    try:
        index.reindex()
        index.start_watcher(interval=0.05)
        write(path, "# Gamma\nGamma note.")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = index.search("gamma", limit=1)
            if result["results"] and result["results"][0]["chunk_name"] == "Gamma":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("watcher did not reindex modified file")
    finally:
        index.close()
