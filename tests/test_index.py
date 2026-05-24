from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import os
import time
from pathlib import Path
import zipfile

import pytest

from chapter_mcp.index import ChapterIndex
from chapter_mcp.server import create_app


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def tldr_zip_path() -> Path | None:
    env_path = os.environ.get("CHAPTER_MCP_TLDR_ZIP")
    if env_path:
        path = Path(env_path).expanduser()
        if path.exists():
            return path
    local_path = Path(__file__).resolve().parent.parent / "test" / "tldr.zip"
    if local_path.exists():
        return local_path
    return None


def extract_tldr_files(tmp_path: Path, members: Sequence[str]) -> Path:
    archive_path = tldr_zip_path()
    if archive_path is None:
        pytest.skip("TLDR archive not available; set CHAPTER_MCP_TLDR_ZIP to run this real-world test.")
    destination = tmp_path / "tldr"
    with zipfile.ZipFile(archive_path) as archive:
        for member in members:
            content = archive.read(member).decode("utf-8")
            relative = Path(member).relative_to("tldr-main")
            write(destination / relative, content)
    return destination


def test_index_reindex_search_read_chapter_list_chapters_and_cleanup(tmp_path: Path) -> None:
    write(tmp_path / "knowledge" / "alpha.md", "# Alpha\nUseful search notes.")
    write(tmp_path / "examples" / "beta.txt", "Beta example paragraph.")
    db_path = tmp_path / ".chapter-mcp" / "index.sqlite3"
    index = ChapterIndex(tmp_path, db_path, category_paths=["knowledge", "examples"])

    try:
        first = index.reindex()
        assert first.as_dict() == {"scanned": 2, "indexed": 2, "skipped": 0, "deleted": 0}

        second = index.reindex()
        assert second.as_dict() == {"scanned": 2, "indexed": 0, "skipped": 2, "deleted": 0}

        results = index.search("alpha")
        assert results["count"] == 1
        assert results["results"][0]["file"] == "knowledge/alpha.md"
        assert results["results"][0]["type"] == "markdown_section"
        assert results["results"][0]["chapter_name"] == "Alpha"
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
        assert stats["last_indexing_summary"]["chapters_loaded"] == 0

        chapters = index.list_chapters(count=1)
        assert chapters["count"] == 2
        assert chapters["offset"] == 0
        assert chapters["chapters"][0]["chapter_name"] == "paragraph-1"
        assert "content" not in chapters["chapters"][0]

        chapter = index.read_chapter("Alpha", file="knowledge/alpha.md")
        assert chapter["count"] == 1
        assert chapter["chapters"][0]["content"] == "# Alpha\nUseful search notes."

        missing = index.read_chapter("Missing", file="knowledge/alpha.md")
        assert missing == {"count": 0, "chapters": []}

        (tmp_path / "examples" / "beta.txt").unlink()
        cleanup = index.reindex("examples")
        assert cleanup.deleted == 1
        assert index.search("beta", category="examples", limit=1)["results"] == []
    finally:
        index.close()


def test_search_chapter_searches_chapter_names_only(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "find.md", "# Find\nLocate files by extension.\n")
    write(tmp_path / "docs" / "notes.md", "# Notes\nUse find in shell examples.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        results = index.search_chapter("find")
        assert results["count"] == 1
        assert results["results"][0]["file"] == "docs/find.md"
        assert results["results"][0]["chapter_name"] == "Find"

        no_content_match = index.search_chapter("shell", limit=5)
        assert no_content_match == {"count": 0, "results": []}
    finally:
        index.close()


def test_search_chapter_respects_category_limit_and_offset(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Install\nInstall docs.\n")
    write(tmp_path / "docs" / "beta.md", "# Install details\nMore docs.\n")
    write(tmp_path / "notes" / "gamma.md", "# Install notes\nNotes.\n")
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
        category_paths=["docs", "notes"],
    )

    try:
        index.reindex()
        docs_only = index.search_chapter("install", category="docs", limit=1, offset=0)
        assert docs_only["count"] == 2
        assert len(docs_only["results"]) == 1
        assert docs_only["results"][0]["category"] == "docs"

        second = index.search_chapter("install", category="docs", limit=1, offset=1)
        assert second["count"] == 2
        assert len(second["results"]) == 1
        assert second["results"][0]["file"] != docs_only["results"][0]["file"]
    finally:
        index.close()


def test_changed_file_is_reindexed(tmp_path: Path) -> None:
    path = tmp_path / "source" / "sample.py"
    write(path, "def alpha():\n    return 'alpha'\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["source"])

    try:
        assert index.reindex().indexed == 1
        write(path, "def gamma():\n    return 'gamma'\n")
        stats = index.reindex()
        assert stats.indexed == 1
        assert index.search("gamma", limit=1)["results"][0]["chapter_name"] == "gamma"
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
        result = app.chapter_index.search("gamma", limit=1)  # type: ignore[attr-defined]
        assert result["results"][0]["file"] == "instructions/readme.md"
        assert app.chapter_index.reindex().as_dict()["skipped"] == 1  # type: ignore[attr-defined]
    finally:
        app.chapter_index.close()  # type: ignore[attr-defined]


def test_configured_hidden_folder_uses_folder_name_as_category(tmp_path: Path) -> None:
    write(tmp_path / ".serena" / "memories" / "project.md", "# Alpha\nAlpha memory.")
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
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
        result = app.chapter_index.search("beta", category="docs", limit=1)  # type: ignore[attr-defined]
        assert result["results"][0]["file"] == "docs/note.txt"
    finally:
        app.chapter_index.close()  # type: ignore[attr-defined]


def test_configured_path_can_use_explicit_category_name(tmp_path: Path) -> None:
    write(tmp_path / ".serena" / "memories" / "project.md", "# Gamma\nGamma memory.")
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
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
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
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
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 0, "indexed": 0, "skipped": 0, "deleted": 0}
        assert index.search("alpha", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_create_app_starts_indexing_in_background(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")

    start = time.monotonic()
    app = create_app(root=tmp_path, paths=["docs"], watch=False)
    elapsed = time.monotonic() - start

    try:
        assert elapsed < 0.25
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            listed = app.chapter_index.list_files()  # type: ignore[attr-defined]
            if listed["files"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("file metadata was not available before indexing finished")
        assert listed["files"][0]["file"] == "docs/alpha.md"
        assert listed["files"][0]["chunk_count"] == 1
        stats = app.chapter_index.stats()  # type: ignore[attr-defined]
        assert stats["startup_index_running"] is True or stats["last_reindex"] is not None
        app.chapter_index.wait_for_startup(timeout=2)  # type: ignore[attr-defined]
        assert app.chapter_index.search("alpha")["results"][0]["file"] == "docs/alpha.md"  # type: ignore[attr-defined]
    finally:
        app.chapter_index.close()  # type: ignore[attr-defined]


def test_index_methods_can_be_called_from_worker_thread(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        with ThreadPoolExecutor(max_workers=1) as executor:
            search_result = executor.submit(index.search, "alpha").result()
            chapter_result = executor.submit(index.read_chapter, "Alpha", "docs/alpha.md").result()

        assert search_result["results"][0]["file"] == "docs/alpha.md"
        assert chapter_result["count"] == 1
    finally:
        index.close()


def test_has_changes_detects_added_modified_and_deleted_files(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "alpha.md"
    write(path, "# Alpha\nAlpha note.")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

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
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        index.start_watcher(interval=0.05)
        write(path, "# Gamma\nGamma note.")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = index.search("gamma", limit=1)
            if result["results"] and result["results"][0]["chapter_name"] == "Gamma":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("watcher did not reindex modified file")
    finally:
        index.close()


def test_real_world_tldr_fts_search_finds_curl_json_example(tmp_path: Path) -> None:
    root = extract_tldr_files(
        tmp_path,
        [
            "tldr-main/pages/common/curl.md",
            "tldr-main/pages/common/find.md",
            "tldr-main/pages/common/grep.md",
            "tldr-main/pages/common/tar.md",
        ],
    )
    index = ChapterIndex(root, root / ".chapter-mcp" / "index.sqlite3", category_paths=["pages/common"])

    try:
        index.reindex()
        result = index.search("json content-type header", category="common", limit=3)
        assert result["count"] >= 1
        assert result["results"][0]["file"] == "pages/common/curl.md"
        assert result["results"][0]["chapter_name"] == "curl"
        assert "Content-Type: application/json" in result["results"][0]["content"]
    finally:
        index.close()
