# chapter-mcp

A structure-aware chapter search MCP server and Python library.

`chapter-mcp` indexes only the folders you opt into, either via CLI `--path`
flags or a project-local `.chapter-mcp/config.json`, and returns structured
chapter results instead of raw line matches.

How chapters are created:
- Markdown files are split into heading sections
- Python files are split into top-level functions and classes
- Other text files are split into paragraphs

Result shape:
- chapter-returning tools group results by `category` and then `file`
- chapter entries are compact and omit `type` by default
- chapter entries use `name`, `start_line`, `end_line`, plus optional `content`, `snippet`, or `score`

The project is intentionally simple:
- search uses SQLite FTS5
- results are deterministic
- library mode and MCP mode share the same core implementation
- there is no built-in vector or semantic search

## Tools

- `search(query, category?, limit=5, offset=0, include_snippet=False)`
  - general FTS5 search over chapter names and content, returning grouped chapter references and optional snippets
- `search_chapter(query, category?, limit=5, offset=0)`
  - FTS5 search over chapter names only, returning grouped chapter references
- `read_search(query, category?, offset=0)`
  - reads the full chapter content for the ranked search match
- `read_chapter(chapter_name, file?, category?, count=5, offset=0, content_offset=0, content_limit?)`
  - reads chapter content by exact chapter name, optionally sliced by content lines
- `list_chapters(category?, file?, count=5, offset=0)`
  - lists grouped chapter names and line ranges without content
- `list_chapters_as_columns(category?, file?, count=5, offset=0, fields?)`
  - lists chapters grouped by file, with compact rows ordered exactly like `columns`
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

If you prefer project-local config, add `.chapter-mcp/config.json`:

```json
{
  "paths": [
    "code=src",
    "tests=tests",
    "readme=README.md"
  ]
}
```

Then run:

```sh
uv run chapter-mcp
```

Supported config fields:
- `root` optional, defaults to the current working directory
- `db` optional, defaults to `<root>/.chapter-mcp/index.sqlite3`
- `paths` required when using config
- `watch` optional, defaults to `true`
- `watch_interval` optional, defaults to `1.0`
- `sync_startup` optional, defaults to `true`

CLI flags override project config when both are present.

By default the server:
- uses the current working directory as the root
- stores the SQLite index at `.chapter-mcp/index.sqlite3`
- runs startup indexing before accepting MCP connections
- watches configured folders for changes

Useful flags:

```sh
uv run chapter-mcp --path docs
uv run chapter-mcp --path docs --no-watch
uv run chapter-mcp --path docs --no-sync-startup
uv run chapter-mcp --path docs --watch-interval 0.5
```

## Search Modes

Use `search` when you want general chapter lookup by content or title and only need compact references:

- `search("json content-type header")`
- `search("routing config")`
- `search("Find files by extension")`
- `search("json content-type header", include_snippet=True)`

Use `search_chapter` when you want to find chapters by title only:

- `search_chapter("Find")`
- `search_chapter("Introduction")`
- `search_chapter("Routing")`

`search_chapter` is useful when you know the section name or command/page title
you are looking for and want to avoid content-only matches.

Example grouped result shape:

```json
{
  "count": 2,
  "results": [
    {
      "category": "docs",
      "files": [
        {
          "file": "docs/curl.md",
          "chapters": [
            {
              "name": "curl",
              "start_line": 1,
              "end_line": 8,
              "snippet": "Use curl when sending a JSON Content-Type header..."
            }
          ]
        }
      ]
    }
  ]
}
```

Partial chapter reads:

- `content_offset` skips that many lines from the beginning of the stored chapter content
- `content_limit` returns at most that many content lines after the offset
- slicing is line-based within chapter content, not by absolute file line numbers

Example:

```json
{
  "count": 1,
  "results": [
    {
      "category": "code",
      "files": [
        {
          "file": "src/chapter_mcp/index.py",
          "chapters": [
            {
              "name": "read_chapter",
              "start_line": 373,
              "end_line": 414,
              "content": "def read_chapter(\n    self,",
              "content_offset": 0,
              "content_total_lines": 12,
              "content_truncated": true
            }
          ]
        }
      ]
    }
  ]
}
```

Column mode for high-volume listing:

- allowed `fields`: `name`, `start_line`, `end_line`
- default `fields`: `name`, `start_line`, `end_line`
- results stay grouped by `category` and `file`
- this costs a small amount of extra JSON overhead compared to a fully flat table
- the grouping is intentional because it avoids repeating file paths per row and keeps follow-up reads easier

Example:

```json
{
  "results": [
    {
      "category": "code",
      "files": [
        {
          "file": "src/chapter_mcp/chunks.py",
          "columns": ["name", "start_line", "end_line"],
          "rows": [
            ["Chunk", 9, 15]
          ]
        }
      ]
    }
  ],
  "truncated": false
}
```


## Using chapter-mcp with Serena, rg, and sed

`chapter-mcp` is a lightweight, fresh, chapter-based context layer for agent workflows. It complements Serena and shell tools instead of replacing them.

- Use `chapter-mcp` for indexed project context such as docs, README files, help text, instructions, ADRs, Markdown/TXT, and optionally chapterized source sections.
- Prefer `chapter-mcp` for exploratory or relevance-ranked search. It returns ranked sections with path, `start_line`, `end_line`, `name`, optional `kind`, and optional `score`.
- For Markdown, TXT, and docs, normal full-text chapter search is usually the right default.
- Do not use `chapter-mcp` as an exact source-code matcher. SQLite FTS5 tokenization is a poor fit for exact literals in code and formal languages, especially when special characters, paths, routes, punctuation, or operators matter.
- Use Serena for symbol-aware work such as definitions, references, implementations, diagnostics, and symbol-level edits.
- Use `rg` for exact literals, identifiers, routes, config keys, error messages, regex searches, non-indexed files, and raw verification after edits.
- Use `sed` when you already know the path and line range and need the raw source text.
- `read_chapter` with `content_offset` and `content_limit` can replace many `sed` reads for indexed docs and chapterized sections.
- `chapter-mcp` is designed to keep its index fresh automatically during an agent session, so manual reindexing should rarely be needed.

Tool selection:

- Use `chapter-mcp` when the question is:
  - "Which project section is relevant?"
  - "Where is this behavior documented?"
  - "What guidance applies before editing?"
  - "Find the best matching chapter or section for this task."
- Use Serena when the question is:
  - "Where is this symbol defined?"
  - "Who references this function or type?"
  - "What implementations exist?"
  - "Can this symbol or body be edited safely?"
- Use `rg` when the question is:
  - "Does this exact literal occur?"
  - "Where is this error message, route, or config key?"
  - "I need regex or raw text verification."
- Use `sed` when:
  - "I already know the path and line range and need the raw source text."

Example flow:

1. Use `chapter-mcp` to find the relevant docs, instructions, or section.
2. Use Serena if symbols, references, or diagnostics are involved.
3. Use `read_chapter` with limits for indexed chapters, or `sed` for raw source ranges.
4. Use `rg` for exact literal or regex verification.

```json
{
  "results": [
    {
      "category": "code",
      "files": [
        {
          "file": "src/chapter_mcp/chunks.py",
          "columns": ["name"],
          "rows": [
            ["Chunk"]
          ]
        }
      ]
    }
  ],
  "truncated": false
}
```

Use `read_search` when you want to open the best full chapter match directly:

- `read_search("json content-type header")`
- `read_search("Routing")`
- `read_search("Find files by extension", category="common")`

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
print(index.read_search("install"))
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
- `list_chapters_as_columns(fields=["name"])`
- `search("config handling", category="instructions", limit=3)`
- `search_chapter("Style guide", category="instructions", limit=3)`
- `read_search("config handling", category="instructions")`
- `read_chapter("Style guide", file="instructions/style-guide.md")`
- `read_chapter("Style guide", file="instructions/style-guide.md", content_offset=0, content_limit=20)`

Workspace MCP config:
- `.codex/config.toml` configures the server for project-local Codex usage.
- `.chapter-mcp/config.json` defines what a given project indexes.
- `.mcp.json` can stay generic and only describe how to launch the server.

## Codex Setup

If project-local `.codex/config.toml` works in your Codex environment, prefer that.

If your Codex surface only reliably loads global MCP config, you can still keep folder selection project-specific by using one generic global server entry and storing the actual index configuration in each repo's `.chapter-mcp/config.json`.

Example global Codex config:

```toml
[mcp_servers.chapter-mcp]
command = "/opt/homebrew/bin/uv"
args = [
  "run",
  "python",
  "-m",
  "chapter_mcp",
  "--root",
  ".",
]
cwd = "."
startup_timeout_sec = 20
required = false
```

With that setup, `chapter-mcp` starts in the current workspace and reads per-project index settings from `.chapter-mcp/config.json` automatically when no `--path` flags are passed directly.

Example project `.chapter-mcp/config.json`:

```json
{
  "paths": [
    "code=src",
    "tests=tests",
    "readme=README.md"
  ]
}
```

## Benchmarking Observed Tool Traffic

`chapter-mcp` does not measure token savings internally. If you want to benchmark a baseline run against a chapter-first run, capture observed tool traffic outside the server and compare those logs offline.

Supported external capture approaches include:

- Codex session logs in `~/.codex/sessions/...jsonl`
- `rtk` wrapper logging
- shell execution logging
- MCP proxy logging

If you are using Codex locally, prefer extracting from the real session logs first:

```sh
uv run chapter-mcp benchmark extract-codex-session --out baseline.jsonl
uv run chapter-mcp benchmark extract-codex-session ~/.codex/sessions/2026/05/25/example.jsonl --out chapter-first.jsonl
```

The first form uses the latest session log under `~/.codex/sessions`.

That command emits neutral JSONL records like:

```jsonl
{"tool":"sed","cmd":"rtk sed -n '1,220p' src/foo.py","chars_out":12340,"lines_out":220}
{"tool":"rg","cmd":"rtk rg column src","chars_out":1840,"lines_out":35}
{"tool":"chapter-mcp","call_name":"search","chars_out":2300,"lines_out":40}
```

Required fields:

- `tool`
- `chars_out`

Optional fields:

- `bytes_out`
- `lines_out`
- `cmd`
- `path`
- `range`
- `timestamp`

Summarize one observed run:

```sh
uv run chapter-mcp benchmark summarize baseline.jsonl
uv run chapter-mcp benchmark summarize baseline.jsonl --json
```

Compare a baseline run and a chapter-first run:

```sh
uv run chapter-mcp benchmark compare baseline.jsonl chapter-first.jsonl
uv run chapter-mcp benchmark compare baseline.jsonl chapter-first.jsonl --json
```

The comparison reports:

- totals by tool
- total chars, bytes, and lines
- estimated output tokens using a transparent `chars / 4` heuristic
- a chapter-mcp candidate raw-read budget based on `sed` / `cat` / `head` / `tail`
- largest raw reads
- repeated reads when they can be derived from `path`, `range`, or `cmd`
- deltas between the two observed runs

By default `summarize` and `compare` print a compact human-readable summary. Use `--json` when you want the full structured report.

Important limits:

- this compares observed runs only
- it does not infer what the model would have done otherwise
- token estimates are not provider billing tokens
- it does not trace hidden reasoning or task quality
- extracted Codex records measure tool output that Codex wrote into the session log, which is the closest live source available here
- the chapter-mcp candidate budget is only a heuristic upper bound for raw reads that might be replaceable; it keeps `rg` separate because exact search often remains necessary

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
- `read_search("json content-type header", category="common")`

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
