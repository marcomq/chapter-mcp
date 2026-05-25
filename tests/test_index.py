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


def first_category(response: dict) -> dict:
    return response["results"][0]


def first_file(response: dict) -> dict:
    return first_category(response)["files"][0]


def first_chapter(response: dict) -> dict:
    return first_file(response)["chapters"][0]


def first_column_file(response: dict) -> dict:
    return response["results"][0]["files"][0]


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
        assert first_category(results)["category"] == "knowledge"
        assert first_file(results)["file"] == "knowledge/alpha.md"
        assert first_chapter(results)["name"] == "Alpha"
        assert first_chapter(results)["start_line"] == 1
        assert first_chapter(results)["end_line"] == 2
        assert "content" not in first_chapter(results)

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
        assert first_chapter(chapters)["name"] == "paragraph-1"
        assert "content" not in first_chapter(chapters)

        chapter = index.read_chapter("Alpha", file="knowledge/alpha.md")
        assert chapter["count"] == 1
        assert first_chapter(chapter)["content"] == "# Alpha\nUseful search notes."

        missing = index.read_chapter("Missing", file="knowledge/alpha.md")
        assert missing == {"count": 0, "results": []}

        (tmp_path / "examples" / "beta.txt").unlink()
        cleanup = index.reindex("examples")
        assert cleanup.deleted == 1
        assert index.search("beta", category="examples", limit=1)["results"] == []
    finally:
        index.close()


def test_search_can_include_compact_snippet(tmp_path: Path) -> None:
    write(
        tmp_path / "docs" / "curl.md",
        "# curl\nUse curl when sending a JSON Content-Type header to remote APIs. Add auth headers as needed.\n",
    )
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        result = index.search("json content-type header", include_snippet=True)
        assert result["count"] == 1
        assert first_file(result)["file"] == "docs/curl.md"
        assert "snippet" in first_chapter(result)
        assert "JSON Content-Type header" in first_chapter(result)["snippet"]
        assert len(first_chapter(result)["snippet"]) <= 120
        assert "content" not in first_chapter(result)
    finally:
        index.close()


def test_search_exact_code_matches_filters_formal_language_hits_only(tmp_path: Path) -> None:
    write(
        tmp_path / "docs" / "notes.md",
        "# Sessions\nExtract the latest Codex session log before comparing runs.\n",
    )
    write(
        tmp_path / "src" / "tools.py",
        "def extract_codex_session_to_jsonl() -> None:\n"
        "    \"\"\"Extract a Codex session log.\"\"\"\n"
        "    return None\n",
    )
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs", "src"])

    try:
        index.reindex()

        loose = index.search("extract codex session", limit=10)
        loose_files = {file["file"] for category in loose["results"] for file in category["files"]}
        assert loose_files == {"docs/notes.md", "src/tools.py"}

        exact = index.search("extract codex session", limit=10, exact_code_matches=True)
        exact_files = {file["file"] for category in exact["results"] for file in category["files"]}
        assert exact_files == {"docs/notes.md"}
        assert exact["count"] == 1

        exact_literal = index.search("extract_codex_session", limit=10, exact_code_matches=True)
        exact_literal_files = {file["file"] for category in exact_literal["results"] for file in category["files"]}
        assert exact_literal_files == {"src/tools.py"}
    finally:
        index.close()


def test_list_chapters_as_columns_uses_default_fields_and_preserves_truncated(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nFirst.\n")
    write(tmp_path / "docs" / "beta.md", "# Beta\nSecond.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        result = index.list_chapters_as_columns(count=1)
        assert first_column_file(result)["columns"] == ["name", "start_line", "end_line"]
        assert first_column_file(result)["rows"] == [["Alpha", 1, 2]]
        assert first_column_file(result)["file"] == "docs/alpha.md"
        assert result["count"] == 2
        assert result["offset"] == 0
        assert result["truncated"] is True
    finally:
        index.close()


def test_list_chapters_as_columns_accepts_explicit_fields_in_requested_order(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nFirst.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        result = index.list_chapters_as_columns(fields=["name", "start_line"])
        assert first_column_file(result)["columns"] == ["name", "start_line"]
        assert first_column_file(result)["rows"] == [["Alpha", 1]]
        assert result["truncated"] is False
    finally:
        index.close()


def test_list_chapters_as_columns_groups_rows_by_file(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nFirst.\n")
    write(tmp_path / "docs" / "beta.md", "# Beta\nSecond.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        result = index.list_chapters_as_columns(category="docs", fields=["end_line", "name"])
        assert result["results"][0]["category"] == "docs"
        assert result["results"][0]["files"][0]["file"] == "docs/alpha.md"
        assert result["results"][0]["files"][0]["columns"] == ["end_line", "name"]
        assert result["results"][0]["files"][0]["rows"] == [[2, "Alpha"]]
        assert result["results"][0]["files"][1]["file"] == "docs/beta.md"
        assert result["results"][0]["files"][1]["rows"] == [[2, "Beta"]]
    finally:
        index.close()


def test_list_chapters_as_columns_rejects_unknown_fields(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nFirst.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        with pytest.raises(ValueError, match="unknown chapter fields: signature"):
            index.list_chapters_as_columns(fields=["name", "signature"])
    finally:
        index.close()


def test_search_omits_snippet_by_default(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "curl.md", "# curl\nUse curl with a JSON header.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        result = index.search("json header")
        assert result["count"] == 1
        assert "snippet" not in first_chapter(result)
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
        assert first_file(results)["file"] == "docs/find.md"
        assert first_chapter(results)["name"] == "Find"
        assert "content" not in first_chapter(results)

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
        assert first_category(docs_only)["category"] == "docs"

        second = index.search_chapter("install", category="docs", limit=1, offset=1)
        assert second["count"] == 2
        assert len(second["results"]) == 1
        assert first_file(second)["file"] != first_file(docs_only)["file"]
    finally:
        index.close()


def test_read_search_returns_full_chapter_for_title_and_content_matches(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "curl.md", "# curl\nSend a JSON header.\n")
    write(tmp_path / "docs" / "find.md", "# Find\nLocate files by extension.\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()

        title_match = index.read_search("Find")
        assert title_match["count"] == 1
        assert first_file(title_match)["file"] == "docs/find.md"
        assert first_chapter(title_match)["name"] == "Find"
        assert first_chapter(title_match)["content"] == "# Find\nLocate files by extension."

        content_match = index.read_search("json header")
        assert content_match["count"] == 1
        assert first_file(content_match)["file"] == "docs/curl.md"
        assert first_chapter(content_match)["name"] == "curl"
        assert first_chapter(content_match)["content"] == "# curl\nSend a JSON header."
    finally:
        index.close()


def test_read_chapter_can_slice_content_by_lines(tmp_path: Path) -> None:
    write(
        tmp_path / "docs" / "alpha.py",
        'def alpha():\n    """Doc line 1\\n    Doc line 2"""\n    first = 1\n    second = 2\n    return first + second\n',
    )
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        chapter = index.read_chapter("alpha", file="docs/alpha.py", content_offset=2, content_limit=2)
        assert chapter["count"] == 1
        assert first_chapter(chapter)["content"] == "Doc line 1\nDoc line 2"
        assert first_chapter(chapter)["content_offset"] == 2
        assert first_chapter(chapter)["content_total_lines"] == 10
        assert first_chapter(chapter)["content_truncated"] is True
    finally:
        index.close()


def test_read_chapter_content_slice_can_return_tail_without_truncation_when_exact(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\none\ntwo\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        chapter = index.read_chapter("Alpha", file="docs/alpha.md", content_offset=2)
        assert first_chapter(chapter)["content"] == "two"
        assert first_chapter(chapter)["content_offset"] == 2
        assert first_chapter(chapter)["content_total_lines"] == 3
        assert first_chapter(chapter)["content_truncated"] is True
    finally:
        index.close()


def test_read_chapter_rejects_non_positive_content_limit(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\none\ntwo\n")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        with pytest.raises(ValueError, match="content_limit must be greater than zero"):
            index.read_chapter("Alpha", file="docs/alpha.md", content_limit=0)
    finally:
        index.close()


def test_read_search_respects_category_offset_and_empty_matches(tmp_path: Path) -> None:
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

        docs_first = index.read_search("install", category="docs", offset=0)
        docs_second = index.read_search("install", category="docs", offset=1)
        assert docs_first["count"] == 2
        assert len(docs_first["results"]) == 1
        assert first_category(docs_first)["category"] == "docs"
        assert docs_second["count"] == 2
        assert len(docs_second["results"]) == 1
        assert first_file(docs_second)["file"] != first_file(docs_first)["file"]

        assert index.read_search("missing") == {"count": 0, "results": []}
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
        assert first_chapter(index.search("gamma", limit=1))["name"] == "gamma"
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
        assert first_file(result)["file"] == "instructions/readme.md"
        columns = app.chapter_index.list_chapters_as_columns(fields=["name"])  # type: ignore[attr-defined]
        assert first_column_file(columns)["columns"] == ["name"]
        assert first_column_file(columns)["rows"] == [["Gamma"]]
        assert first_column_file(columns)["file"] == "instructions/readme.md"
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
        assert first_file(result)["file"] == ".serena/memories/project.md"
        assert first_category(result)["category"] == "memories"
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
        assert first_file(result)["file"] == "docs/note.txt"
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
        assert first_file(result)["file"] == ".serena/memories/project.md"
        assert first_category(result)["category"] == "memory"
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
        assert first_file(result)["file"] == "memories/project.md"
        assert index.stats()["categories"]["memory"]["path"] == (tmp_path / "memories").as_posix()
        assert index.stats()["categories"]["memory"]["paths"] == [(tmp_path / "memories").as_posix()]
    finally:
        index.close()


def test_single_file_can_be_indexed_as_a_category_path(tmp_path: Path) -> None:
    write(tmp_path / "README.md", "# Intro\nHelpful project overview.")
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
        category_paths=["instructions=README.md"],
    )

    try:
        stats = index.reindex()
        assert stats.indexed == 1
        result = index.search("overview", category="instructions", limit=1)
        assert first_file(result)["file"] == "README.md"
        category_stats = index.stats()["categories"]["instructions"]
        assert category_stats["path"] == (tmp_path / "README.md").as_posix()
        assert category_stats["paths"] == [(tmp_path / "README.md").as_posix()]
    finally:
        index.close()


def test_multiple_paths_can_share_one_category(tmp_path: Path) -> None:
    write(tmp_path / "README.md", "# Overview\nRepository instructions.")
    write(tmp_path / "internal_instructions.md", "# Internal\nPrivate workflow notes.")
    index = ChapterIndex(
        tmp_path,
        tmp_path / ".chapter-mcp" / "index.sqlite3",
        category_paths=[
            "instructions=README.md",
            "instructions=internal_instructions.md",
        ],
    )

    try:
        stats = index.reindex()
        assert stats.indexed == 2
        result = index.search("workflow", category="instructions", limit=1)
        assert first_file(result)["file"] == "internal_instructions.md"
        listed = index.list_files(category="instructions")
        assert {item["file"] for item in listed["files"]} == {"README.md", "internal_instructions.md"}
        category_stats = index.stats()["categories"]["instructions"]
        assert "path" not in category_stats
        assert category_stats["paths"] == [
            (tmp_path / "README.md").as_posix(),
            (tmp_path / "internal_instructions.md").as_posix(),
        ]
    finally:
        index.close()


def test_no_configured_paths_indexes_visible_project_files(tmp_path: Path) -> None:
    write(tmp_path / "knowledge" / "alpha.md", "# Alpha\nAlpha note.")
    write(tmp_path / ".hidden" / "secret.md", "# Secret\nHidden note.")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 1, "indexed": 1, "skipped": 0, "deleted": 0}
        assert first_file(index.search("alpha", limit=1))["file"] == "knowledge/alpha.md"
        assert index.search("secret", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_default_indexing_respects_gitignore(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nVisible note.")
    write(tmp_path / "ignored" / "secret.md", "# Secret\nIgnored note.")
    write(tmp_path / ".gitignore", "ignored/\n")

    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 1, "indexed": 1, "skipped": 0, "deleted": 0}
        assert first_file(index.search("alpha", limit=1))["file"] == "docs/alpha.md"
        assert index.search("secret", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_nested_gitignore_can_reinclude_files(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "draft.md", "# Draft\nHidden note.")
    write(tmp_path / "docs" / "keep.md", "# Keep\nVisible note.")
    write(tmp_path / "docs" / ".gitignore", "*.md\n!keep.md\n")

    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 1, "indexed": 1, "skipped": 0, "deleted": 0}
        assert first_file(index.search("keep", limit=1))["file"] == "docs/keep.md"
        assert index.search("draft", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_default_indexing_respects_aiignore(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nVisible note.")
    write(tmp_path / "ignored" / "secret.md", "# Secret\nIgnored note.")
    write(tmp_path / ".aiignore", "ignored/\n")

    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 1, "indexed": 1, "skipped": 0, "deleted": 0}
        assert first_file(index.search("alpha", limit=1))["file"] == "docs/alpha.md"
        assert index.search("secret", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_nested_aiignore_can_reinclude_files(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "draft.md", "# Draft\nHidden note.")
    write(tmp_path / "docs" / "keep.md", "# Keep\nVisible note.")
    write(tmp_path / "docs" / ".aiignore", "*.md\n!keep.md\n")

    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3")

    try:
        stats = index.reindex()
        assert stats.as_dict() == {"scanned": 1, "indexed": 1, "skipped": 0, "deleted": 0}
        assert first_file(index.search("keep", limit=1))["file"] == "docs/keep.md"
        assert index.search("draft", limit=1) == {"count": 0, "results": []}
    finally:
        index.close()


def test_create_app_starts_indexing_in_background(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")

    app = create_app(root=tmp_path, paths=["docs"], watch=False)

    try:
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
        assert first_file(app.chapter_index.search("alpha"))["file"] == "docs/alpha.md"  # type: ignore[attr-defined]
    finally:
        app.chapter_index.close()  # type: ignore[attr-defined]


def test_overlapping_configured_paths_raise_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docs/api"):
        ChapterIndex(
            tmp_path,
            tmp_path / ".chapter-mcp" / "index.sqlite3",
            category_paths=["docs", "api=docs/api"],
        )


def test_prepare_failure_does_not_commit_partial_file_metadata(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "docs" / "alpha.md"
    write(path, "# Alpha\nAlpha note.")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(index, "_prepare_file", fail_prepare)

    try:
        with pytest.raises(RuntimeError, match="boom"):
            index.reindex()
        assert index.list_files()["files"] == []
        assert index.has_changes() is True
    finally:
        index.close()


def test_index_methods_can_be_called_from_worker_thread(tmp_path: Path) -> None:
    write(tmp_path / "docs" / "alpha.md", "# Alpha\nAlpha note.")
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    try:
        index.reindex()
        with ThreadPoolExecutor(max_workers=1) as executor:
            search_result = executor.submit(index.search, "alpha").result()
            chapter_result = executor.submit(index.read_chapter, "Alpha", "docs/alpha.md").result()

        assert first_file(search_result)["file"] == "docs/alpha.md"
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
            if result["results"] and first_chapter(result)["name"] == "Gamma":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("watcher did not reindex modified file")
    finally:
        index.close()


def test_watch_loop_logs_and_continues_after_errors(tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    index = ChapterIndex(tmp_path, tmp_path / ".chapter-mcp" / "index.sqlite3", category_paths=["docs"])

    class FakeStop:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, interval: float) -> bool:
            self.calls += 1
            return self.calls > 2

    calls = {"has_changes": 0, "reindex": 0}

    def fake_has_changes() -> bool:
        calls["has_changes"] += 1
        if calls["has_changes"] == 1:
            raise RuntimeError("watch failed")
        return True

    def fake_reindex() -> None:
        calls["reindex"] += 1

    monkeypatch.setattr(index, "_watch_stop", FakeStop())
    monkeypatch.setattr(index, "has_changes", fake_has_changes)
    monkeypatch.setattr(index, "reindex", fake_reindex)

    try:
        with caplog.at_level("ERROR"):
            index._watch_loop(0.0)
        assert calls["has_changes"] == 2
        assert calls["reindex"] == 1
        assert "watch loop failed" in caplog.text
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
        assert first_file(result)["file"] == "pages/common/curl.md"
        assert first_chapter(result)["name"] == "curl"
        assert "content" not in first_chapter(result)

        chapter = index.read_search("json content-type header", category="common")
        assert chapter["count"] >= 1
        assert first_file(chapter)["file"] == "pages/common/curl.md"
        assert "Content-Type: application/json" in first_chapter(chapter)["content"]
    finally:
        index.close()
