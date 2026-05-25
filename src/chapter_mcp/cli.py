from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from chapter_mcp.server import create_app


CHAPTER_MCP_CFG = Path(".chapter-mcp/config.json")


def _configure_logging() -> None:
    level_name = os.environ.get("CHAPTER_MCP_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(level=level)


def _validate_positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return parsed


def _load_json_file(config_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise argparse.ArgumentTypeError(f"project config {config_path} was not found") from exc
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"failed to parse project config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise argparse.ArgumentTypeError(f"project config {config_path} must contain a JSON object")
    return data


def _config_base_dir(config_path: Path) -> Path:
    if config_path.parent.name == ".chapter-mcp":
        return config_path.parent.parent.resolve()
    return config_path.parent.resolve()


def _resolve_optional_path(value: Any, *, field_name: str, base_dir: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise argparse.ArgumentTypeError(f"{field_name} in project config must be a non-empty string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _resolve_bool(value: Any, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise argparse.ArgumentTypeError(f"{field_name} in project config must be a boolean")
    return value


def _resolve_paths(value: Any, *, config_path: Path) -> list[str]:
    if not isinstance(value, list) or not value:
        raise argparse.ArgumentTypeError(f"paths in project config {config_path} must be a non-empty array")
    paths: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry:
            raise argparse.ArgumentTypeError(f"paths entries in project config {config_path} must be strings")
        paths.append(entry)
    return paths


def _load_project_config(config_path: Path) -> dict[str, Any]:
    data = _load_json_file(config_path)
    base_dir = _config_base_dir(config_path)
    watch_interval_value = data.get("watch_interval")
    if watch_interval_value is not None:
        if not isinstance(watch_interval_value, int | float) or watch_interval_value <= 0:
            raise argparse.ArgumentTypeError("watch_interval in project config must be a positive number")
        watch_interval = float(watch_interval_value)
    else:
        watch_interval = None

    if "paths" not in data:
        raise argparse.ArgumentTypeError(f"project config {config_path} must define paths")

    return {
        "root": _resolve_optional_path(data.get("root"), field_name="root", base_dir=base_dir),
        "db": _resolve_optional_path(data.get("db"), field_name="db", base_dir=base_dir),
        "paths": _resolve_paths(data.get("paths"), config_path=config_path),
        "watch": _resolve_bool(data.get("watch"), field_name="watch"),
        "watch_interval": watch_interval,
        "sync_startup": _resolve_bool(data.get("sync_startup"), field_name="sync_startup"),
    }


def main() -> None:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Run the chapter-mcp chapter search server.")
    parser.add_argument("--root", type=Path, default=None, help="Root directory to index. Defaults to the current working directory.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path. Defaults to <root>/.chapter-mcp/index.sqlite3.",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help=(
            "Folder to index. May be passed multiple times. "
            "Use category=folder to set a category name; otherwise the folder basename is used."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Load chapter-mcp project config from a JSON file. "
            "If omitted and no --path values are passed, defaults to <root>/.chapter-mcp/config.json."
        ),
    )
    parser.add_argument(
        "--no-project-config",
        action="store_true",
        help="Disable automatic project config loading when no --path values are passed.",
    )
    parser.add_argument(
        "--watch-interval",
        type=_validate_positive_float,
        default=None,
        help="Seconds between filesystem change checks. Defaults to 1.0 unless overridden by project config.",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Disable automatic background reindexing.",
    )
    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--sync-startup",
        action="store_true",
        default=None,
        help="Run startup indexing before accepting MCP connections. Enabled by default.",
    )
    sync_group.add_argument(
        "--no-sync-startup",
        action="store_false",
        dest="sync_startup",
        default=None,
        help="Disable startup indexing before accepting MCP connections.",
    )
    args = parser.parse_args()
    root = (args.root or Path.cwd()).expanduser().resolve()
    config: dict[str, Any] | None = None
    config_path = args.config or (root / CHAPTER_MCP_CFG)

    if not args.no_project_config:
        if args.config is not None or config_path.exists():
            try:
                config = _load_project_config(config_path)
            except argparse.ArgumentTypeError as exc:
                parser.error(str(exc))

    effective_root = Path(config["root"]) if config and config["root"] is not None and args.root is None else root
    effective_db = args.db
    if effective_db is None and config and config["db"] is not None:
        effective_db = Path(config["db"])

    paths = args.paths
    if not paths:
        if config is None:
            parser.error(
                f"no --path values were provided and project config {config_path} was not found; "
                "pass --path, add .chapter-mcp/config.json, or use --config"
            )
        paths = config["paths"]

    watch = False if args.no_watch else (config["watch"] if config and config["watch"] is not None else True)
    watch_interval = args.watch_interval
    if watch_interval is None:
        watch_interval = config["watch_interval"] if config and config["watch_interval"] is not None else 1.0
    sync_startup = args.sync_startup
    if sync_startup is None:
        sync_startup = config["sync_startup"] if config and config["sync_startup"] is not None else True

    app = create_app(
        root=effective_root,
        db_path=effective_db,
        paths=paths,
        async_startup=not sync_startup,
        watch=watch,
        watch_interval=watch_interval,
    )
    app.run(show_banner=False)
