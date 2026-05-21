from __future__ import annotations

import argparse
from pathlib import Path

from chunk_mcp.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the chunk-mcp semantic search server.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Root directory to index.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path. Defaults to <root>/.chunk-mcp/index.sqlite3.",
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
    args = parser.parse_args()
    app = create_app(
        root=args.root,
        db_path=args.db,
        paths=args.paths,
        watch=not args.no_watch,
        watch_interval=args.watch_interval,
    )
    app.run()
