# chunk-mcp

A structure-aware semantic file search MCP server.

`chunk-mcp` indexes configured folders and returns meaningful chunks instead
of raw line matches. Only folders passed with `--path` are indexed.

Markdown files are split into heading sections, Python files into top-level
functions and classes with `ast`, and other text files into paragraphs.
Embeddings are generated locally with
`sentence-transformers/all-MiniLM-L6-v2` and stored in SQLite with
`sqlite-vec`.

## Tools

- `search(query, category?, limit=1, offset=0)` returns a `count` and the
  current page of semantic matches with file path, chunk type, chunk name,
  content, and line range.
- `get_chunk(file, chunk_name)` fetches a specific indexed chunk.
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
```

By default the server uses the current working directory as the root and writes
its database to `.chunk-mcp/index.sqlite3`. It indexes once on startup, then
checks configured folders every second and reindexes changed files
automatically.

Tune or disable automatic reindexing with:

```sh
uv run chunk-mcp --path docs --watch-interval 0.5
uv run chunk-mcp --path docs --no-watch
```

The first real run may download the embedding model into the local Hugging Face
cache. After that, search runs locally.

## Development

```sh
uv run pytest
```
