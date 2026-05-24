from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from chapter_mcp.server import create_app


def _configure_logging() -> None:
    level_name = os.environ.get("CHAPTER_MCP_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(level=level)


def main() -> None:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Run the chapter-mcp chapter search server.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Root directory to index.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path. Defaults to <root>/.chapter-mcp/index.sqlite3.",
    )
    parser.add_argument(
        "--path",
        action="append",
        required=True,
        dest="paths",
        help=(
            "Folder to index. May be passed multiple times. "
            "Use category=folder to set a category name; otherwise the folder basename is used."
        ),
    )
    parser.add_argument(
        "--watch-interval",
        type=float,
        default=1.0,
        help="Seconds between filesystem change checks. Defaults to 1.0.",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Disable automatic background reindexing.",
    )
    parser.add_argument(
        "--sync-startup",
        action="store_true",
        help="Run startup indexing before accepting MCP connections.",
    )
    args = parser.parse_args()
    app = create_app(
        root=args.root,
        db_path=args.db,
        paths=args.paths,
        async_startup=not args.sync_startup,
        watch=not args.no_watch,
        watch_interval=args.watch_interval,
    )
    app.run(show_banner=False)
