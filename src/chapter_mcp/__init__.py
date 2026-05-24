"""Structure-aware chapter search MCP server and library."""

from chapter_mcp.index import ChapterIndex, IndexingSummary, IndexStats
from chapter_mcp.server import create_app

__all__ = [
    "__version__",
    "ChapterIndex",
    "IndexStats",
    "IndexingSummary",
    "create_app",
]

__version__ = "0.1.0"
