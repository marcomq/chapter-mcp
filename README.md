# chunk-mcp

A structure-aware semantic file search MCP server.

`chunk-mcp` indexes configured folders and returns meaningful chunks instead
of raw line matches. Only folders passed with `--path` are indexed.

Markdown files are split into heading sections, Python files into top-level
functions and classes with `ast`, and other text files into paragraphs.
Search uses SQLite FTS5 by default. Semantic vector search is available with
`--vector`, using `sentence-transformers/all-MiniLM-L6-v2` and `sqlite-vec`.

## Tools

- `search(query, category?, limit=1, offset=0)` returns a `count` and the
  current page of matches. Each result includes `file`, chunk type, chunk name,
  content, `start_line`, `end_line`, and score/distance. Results include
  `mode: "fts"` by default, or `mode: "vector"` when started with `--vector`.
- `get_chunk(file, chunk_name)` fetches one exact indexed chunk. Use it when a
  prior search result has the right `file` and `chunk_name`, and you want to
  retrieve that same chunk directly without running another search.
- `list_files(category?, limit=100, offset=0)` lists indexed files with byte
  count, line count, chunk count, mtime, and index time. File metadata is
  available before chunk embedding finishes during background startup.
- `stats()` returns index totals and category-level counts.
- `reindex(category?)` manually refreshes changed files.

## Usage

Pass `--path` one or more times to choose folders. Each folder's basename is
used as the category:

```sh
uv run chunk-mcp --path .serena/memories
uv run chunk-mcp --path docs --path examples
```

Use `category=folder` to set the category name explicitly:

```sh
uv run chunk-mcp --path memory=.serena/memories
uv run chunk-mcp --path memory=~/bin/chunk-mcp/.serena/memories
```

By default the server uses the current working directory as the root and writes
its database to `.chunk-mcp/index.sqlite3`. It accepts MCP connections
immediately, starts indexing in the background, checks configured folders every
second, and reindexes changed files automatically. Use `stats()` to see whether
startup indexing is still running and what was indexed.

Enable semantic vector search explicitly:

```sh
uv run chunk-mcp --path docs --vector
```

Tune startup and automatic reindexing with:

```sh
uv run chunk-mcp --path docs --watch-interval 0.5
uv run chunk-mcp --path docs --no-watch
uv run chunk-mcp --path docs --vector --no-warmup
uv run chunk-mcp --path docs --sync-startup
```

The first vector run may download the embedding model into the local Hugging
Face cache. FTS5 mode does not need the model.

## Development

```sh
uv run pytest
```
