# chapter-mcp

A structure-aware chapter search MCP server and Python library.

`chapter-mcp` indexes only the folders you opt into with `--path` and returns
structured chapter results instead of raw line matches.

How chapters are created:
- Markdown files are split into heading sections
- Python files are split into top-level functions and classes
- Other text files are split into paragraphs

The project is intentionally simple:
- search uses SQLite FTS5
- results are deterministic
- library mode and MCP mode share the same core implementation
- there is no built-in vector or semantic search

## Tools

- `search(query, category?, limit=1, offset=0)`
  - general FTS5 search over chapter names and content
- `search_chapter(query, category?, limit=5, offset=0)`
  - FTS5 search over chapter names only
- `read_chapter(chapter_name, file?, category?, count=5, offset=0)`
  - reads full chapter content by exact chapter name
- `list_chapters(category?, file?, count=5, offset=0)`
  - lists chapter names and line ranges without content
- `list_files(category?, limit=100, offset=0)`
  - lists indexed files and their metadata
- `stats()`
  - returns index totals and category status
- `reindex(category?)`
  - refreshes changed files

## Install

```sh
uv sync
```

## Usage

Pass `--path` one or more times to choose folders. Each folder basename becomes
the category:

```sh
uv run chapter-mcp --path docs
uv run chapter-mcp --path docs --path examples
```

Use `category=path` to set the category name explicitly:

```sh
uv run chapter-mcp --path knowledge=.serena/memories
```

By default the server:
- uses the current working directory as the root
- stores the SQLite index at `.chapter-mcp/index.sqlite3`
- starts indexing in the background
- watches configured folders for changes

Useful flags:

```sh
uv run chapter-mcp --path docs --sync-startup
uv run chapter-mcp --path docs --no-watch
uv run chapter-mcp --path docs --watch-interval 0.5
```

## Search Modes

Use `search` when you want general chapter lookup by content or title:

- `search("json content-type header")`
- `search("routing config")`
- `search("Find files by extension")`

Use `search_chapter` when you want to find chapters by title only:

- `search_chapter("Find")`
- `search_chapter("Introduction")`
- `search_chapter("Routing")`

`search_chapter` is useful when you know the section name or command/page title
you are looking for and want to avoid content-only matches.

## Library Usage

```python
from pathlib import Path

from chapter_mcp import ChapterIndex

index = ChapterIndex(
    Path.cwd(),
    Path(".chapter-mcp/index.sqlite3"),
    category_paths=["docs", "examples"],
)

index.reindex()
print(index.search("install"))
print(index.search_chapter("Guide"))
index.close()
```

## MCP Usage

```sh
uv run chapter-mcp \
  --path instructions \
  --path knowledge \
  --sync-startup
```

Then call tools such as:
- `list_files()`
- `list_chapters()`
- `search("config handling", category="instructions", limit=3)`
- `search_chapter("Style guide", category="instructions", limit=3)`
- `read_chapter("Style guide", file="instructions/style-guide.md")`

## Limitations

`chapter-mcp` is deliberately FTS5-only.

That means:
- literal phrasing matters more than with vector search
- broad conceptual queries may need better wording
- unrelated wording will not be matched semantically
- it does not do nearest-neighbor retrieval or semantic ranking

This tradeoff is intentional: the project favors simple, fast, stable chapter
lookup over more complex semantic retrieval behavior.

## If You Need Vector Search

If you actually need semantic/vector retrieval, evaluate a dedicated tool such
as `txtai` separately.

That can make sense when:
- users ask fuzzy conceptual questions
- wording often differs a lot from the indexed source text
- you want a real RAG or semantic retrieval workflow

`chapter-mcp` intentionally does not try to solve that problem.

## MCP Inspector Example

```sh
npx @modelcontextprotocol/inspector \
  uv run chapter-mcp \
  --root /tmp/chapter-mcp-tldr \
  --path common=pages/common \
  --path instructions \
  --sync-startup
```

Then in the Inspector `Tools` tab try:
- `stats()`
- `list_files()`
- `search("json content-type header", category="common", limit=3)`
- `search_chapter("curl", category="common", limit=3)`

## Optional TLDR Real-World Test

The repository does not commit the TLDR archive. To run the optional real-world
test locally:

```sh
curl -L https://github.com/tldr-pages/tldr/archive/refs/heads/main.zip -o /tmp/tldr-main.zip
CHAPTER_MCP_TLDR_ZIP=/tmp/tldr-main.zip uv run pytest
```

If `CHAPTER_MCP_TLDR_ZIP` is not set, the TLDR-based test is skipped.

## Development

```sh
uv run pytest
```
